from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.db.models import Event, Journey, MobilityFact, RouteAcquisitionOperation, Traveler
from app.db.session import SessionLocal, ensure_schema
from app.execution.route_session_contract import RouteRequest
from app.integrations.mobility.snapshot_capture import (
    MobilitySnapshotCaptureError,
    MobilitySnapshotCaptureResult,
)
from app.replanning.provider_snapshots import MobilitySnapshot, TravelEstimateFact
from app.route_session_operations import submit_operation
import app.route_session_operations as operations
from app.operations.v9 import refresh_offline_snapshot


def _journey() -> Journey:
    traveler_id = str(uuid4())
    journey_id = str(uuid4())
    with SessionLocal() as session:
        session.add(
            Traveler(
                id=traveler_id,
                display_name="R41 traveler",
                email=None,
                locale="en-US",
                timezone="Asia/Tokyo",
                created_at="2026-10-01",
                updated_at="2026-10-01",
            )
        )
        session.commit()
        journey = Journey(
            id=journey_id,
            traveler_id=traveler_id,
            title="R41 route",
            city_code="NARA",
            timezone="Asia/Tokyo",
            start_at="2026-10-07T08:00:00+09:00",
            end_at="2026-10-08T20:00:00+09:00",
            budget_currency="JPY",
            budget_amount_minor=100000,
            planning_mode="balanced",
            status="DRAFT",
            active_plan_version_id=None,
            current_activity_id=None,
            created_at="2026-10-01",
            updated_at="2026-10-01",
        )
        session.add(journey)
        session.commit()
    return journey


def _request(journey_id: str, *, target: str = "target") -> RouteRequest:
    return RouteRequest(
        journey_id=journey_id,
        origin_key="origin",
        target_key=target,
        target_activity_id="activity",
        requested_mode="WALK",
    )


def _live_capture(pair: tuple[str, str]) -> MobilitySnapshotCaptureResult:
    captured_at = datetime.now(timezone.utc).isoformat()
    fact = TravelEstimateFact(
        origin_key=pair[0],
        destination_key=pair[1],
        minutes=18,
        provider_key="valhalla-http",
        captured_at=captured_at,
        distance_m=1200,
        walking_min=18,
        route_provenance="LIVE_PROVIDER_CAPTURE",
        fact_quality="LIVE_PROVIDER_CAPTURE",
    )
    return MobilitySnapshotCaptureResult(
        snapshot=MobilitySnapshot(
            facts={pair: fact},
            source_provider_key="valhalla-http",
            captured_at=captured_at,
            operation_key="test",
        ),
        failed_primary_pairs=(),
        fallback_pairs=(),
    )


def test_route_operation_freezes_exact_fact_and_serial_submit_reuses(monkeypatch):
    ensure_schema()
    journey = _journey()
    calls: list[tuple[str, str]] = []

    def capture(_session, _journey, pairs, **_kwargs):
        pair = tuple(pairs)[0]
        calls.append(pair)
        return _live_capture(pair)

    monkeypatch.setattr(operations, "capture_runtime_mobility_snapshot", capture)
    with SessionLocal() as session:
        row, reused = submit_operation(session, journey, _request(journey.id))
        assert reused is False
        assert row.status == "COMPLETED"
        assert row.outcome == "ROUTE_READY"
        fact = session.query(MobilityFact).filter_by(journey_id=journey.id).one()
        assert fact.from_key == "origin"
        assert fact.to_key == "target"
        assert fact.duration_min == 18
        second, reused_second = submit_operation(session, journey, _request(journey.id))
        assert reused_second is True
        assert second.id == row.id
    assert calls == [("origin", "target")]


def test_route_operation_unavailable_is_not_failed(monkeypatch):
    ensure_schema()
    journey = _journey()

    def capture(_session, _journey, _pairs, **_kwargs):
        return MobilitySnapshotCaptureResult(
            snapshot=MobilitySnapshot(facts={}, source_provider_key="valhalla-http", captured_at="now"),
            failed_primary_pairs=(),
            fallback_pairs=(),
        )

    monkeypatch.setattr(operations, "capture_runtime_mobility_snapshot", capture)
    with SessionLocal() as session:
        row, _ = submit_operation(session, journey, _request(journey.id, target="missing"))
        assert row.status == "UNAVAILABLE"
        assert row.outcome == "ROUTE_UNAVAILABLE"
        assert session.query(MobilityFact).filter_by(journey_id=journey.id).count() == 0


def test_provider_exception_is_failed(monkeypatch):
    ensure_schema()
    journey = _journey()

    def capture(*_args, **_kwargs):
        raise MobilitySnapshotCaptureError("provider down")

    monkeypatch.setattr(operations, "capture_runtime_mobility_snapshot", capture)
    monkeypatch.setattr(operations, "settings", SimpleNamespace(live_valhalla_url="https://valhalla.example"))
    with SessionLocal() as session:
        row, _ = submit_operation(session, journey, _request(journey.id, target="failed"))
        assert row.status == "FAILED"
        assert row.outcome == "ROUTE_PROVIDER_FAILURE"
        assert session.query(MobilityFact).filter_by(journey_id=journey.id).count() == 0


def test_open_route_fingerprint_is_durable_unique():
    ensure_schema()
    journey = _journey()
    common = dict(
        id=str(uuid4()),
        journey_id=journey.id,
        fingerprint="same-fingerprint",
        origin_key="origin",
        target_key="target",
        target_activity_id="activity",
        requested_mode="WALK",
        provider_policy_version="p",
        route_truth_policy_version="t",
        status="QUEUED",
        phase="QUEUED",
        request_json="{}",
        result_json="{}",
        error_json="{}",
        queued_at="2026-10-06T00:00:00+00:00",
        updated_at="2026-10-06T00:00:00+00:00",
    )
    with SessionLocal() as session:
        session.add(RouteAcquisitionOperation(**common))
        session.commit()
        session.add(RouteAcquisitionOperation(**{**common, "id": str(uuid4())}))
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()
        indexes = [row[1] for row in session.execute(text("PRAGMA index_list(route_acquisition_operations)"))]
        assert "ux_route_acquisition_one_open" in indexes


def test_offline_snapshot_includes_last_terminal_route_session_without_capture():
    ensure_schema()
    journey = _journey()
    with SessionLocal() as session:
        session.add(
            RouteAcquisitionOperation(
                id=str(uuid4()),
                journey_id=journey.id,
                fingerprint="fp",
                origin_key="origin",
                target_key="target",
                target_activity_id="activity",
                requested_mode="WALK",
                provider_policy_version="p",
                route_truth_policy_version="t",
                status="UNAVAILABLE",
                phase="FINALIZING",
                outcome="ROUTE_UNAVAILABLE",
                request_json="{}",
                result_json=json.dumps({"provider": "valhalla", "attempt_count": 1}),
                error_json=json.dumps({"classification": "ROUTE_UNAVAILABLE"}),
                queued_at="2026-10-06T00:00:00+00:00",
                started_at="2026-10-06T00:00:01+00:00",
                finished_at="2026-10-06T00:00:02+00:00",
                updated_at="2026-10-06T00:00:02+00:00",
            )
        )
        snapshot = refresh_offline_snapshot(session, journey)
        payload = json.loads(snapshot.payload_json)
        assert payload["route_session"]["status"] == "UNAVAILABLE"
        assert payload["route_session"]["freshness_semantics"] == "LAST_TERMINAL_ROUTE_SESSION"
        assert payload["route_session"]["error"]["classification"] == "ROUTE_UNAVAILABLE"
