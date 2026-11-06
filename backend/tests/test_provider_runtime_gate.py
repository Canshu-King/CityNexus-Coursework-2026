from __future__ import annotations

from datetime import datetime
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app.integrations.mobility.snapshot_capture import (
    MobilitySnapshotCaptureError,
    capture_mobility_snapshot,
)
from app.integrations.mobility.valhalla_http import ValhallaHTTPMobilityProvider
from app.integrations.mock.providers import MockMobilityProvider
from app.replanning.provider_snapshots import LIVE_PROVIDER_CAPTURE
from app.replanning.feasibility import FeasibilityActivity, build_context, evaluate_feasibility
from demo_stage import demo_reset_headers  # noqa: E402  (ORD-0389)

client = TestClient(main_module.app)


class _Provider:
    def __init__(self, key: str, *, fail: set[tuple[str, str]] | None = None):
        self.provider_key = key
        self.fail = fail or set()
        self.mock = MockMobilityProvider()
        self.calls: list[tuple[str, str]] = []

    def estimate_minutes(self, origin_key: str, destination_key: str) -> int:
        pair = (origin_key, destination_key)
        self.calls.append(pair)
        if pair in self.fail:
            raise RuntimeError("provider unavailable")
        return self.mock.estimate_minutes(origin_key, destination_key)

    def fact_quality(self, origin_key: str, destination_key: str) -> str:
        # This double models a capturing routing provider (e.g. Valhalla): a
        # duration it returns is a real measurement, so it honestly attests
        # LIVE, mirroring app.integrations.mobility.valhalla_http.  A failed
        # pair raises before attesting, so the capture falls back rather than
        # fabricating a fact (V4 #2 fail-closed classifier requires the double
        # to declare its own quality — no provider-name laundering).
        if (origin_key, destination_key) in self.fail:
            raise RuntimeError("provider unavailable")
        return LIVE_PROVIDER_CAPTURE


def _prepare_disruption() -> tuple[str, dict, dict]:
    ids = client.post("/api/v1/demo/reset", headers=demo_reset_headers()).json()
    jid = ids["journey_id"]
    generated = client.post(f"/api/v1/journeys/{jid}/plans/generate", json={"candidate_count": 1})
    assert generated.status_code == 201, generated.text
    plan = generated.json()[0]
    selected = client.post(f"/api/v1/journeys/{jid}/plans/{plan['id']}/select")
    assert selected.status_code == 200, selected.text

    cruise = next(item for item in plan["activities"] if item["place_id"] == "place-river-cruise")
    booking = client.post(f"/api/v1/journeys/{jid}/bookings", json={"activity_id": cruise["id"]})
    assert booking.status_code == 201, booking.text
    booking_body = booking.json()
    capture = client.post(
        f"/api/v1/journeys/{jid}/transactions",
        json={
            "booking_id": booking_body["id"],
            "transaction_type": "CAPTURE",
            "amount": booking_body["amount"],
        },
        headers={"Idempotency-Key": f"prt-capture-{uuid4()}"},
    )
    assert capture.status_code == 201, capture.text
    started = client.post(f"/api/v1/journeys/{jid}/start")
    assert started.status_code == 200, started.text

    tech = next(item for item in plan["activities"] if item["place_id"] == "place-tech-art")
    for activity in plan["activities"]:
        if activity["id"] == tech["id"]:
            break
        completed = client.post(f"/api/v1/journeys/{jid}/activities/{activity['id']}/complete")
        assert completed.status_code == 200, completed.text

    event = client.post(
        f"/api/v1/journeys/{jid}/events",
        json={
            "event_type": "CLOSURE",
            "severity": "MAJOR",
            "affected_activity_id": tech["id"],
            "occurred_at": "2026-10-10T13:25:00+08:00",
            "payload": {"reason": "provider runtime gate"},
        },
    )
    assert event.status_code == 201, event.text
    return jid, plan, event.json()


def _generate_replan(jid: str, event: dict, count: int = 3):
    response = client.post(
        f"/api/v1/journeys/{jid}/replans",
        json={"trigger_event_id": event["id"], "candidate_count": count},
        headers={"Idempotency-Key": f"prt-replan-{uuid4()}"},
    )
    assert response.status_code == 201, response.text
    proposals = [
        item
        for item in client.get(f"/api/v1/journeys/{jid}/replans").json()
        if item["trigger_event_id"] == event["id"]
    ]
    assert proposals
    return response.json(), proposals


def test_prt01_same_replan_operation_captures_one_snapshot_and_reuses_it(monkeypatch):
    jid, _plan, event = _prepare_disruption()
    primary = _Provider("valhalla")
    calls: list[tuple[tuple[str, str], ...]] = []

    def capture_runtime(_session, _journey, pairs, *, operation_key, captured_at=None):
        normalized = tuple(sorted(set(pairs)))
        calls.append(normalized)
        return capture_mobility_snapshot(
            normalized,
            primary=primary,
            captured_at=captured_at,
            operation_key=operation_key,
        )

    monkeypatch.setattr(
        main_module, "capture_runtime_mobility_snapshot", capture_runtime, raising=False
    )
    _chosen, proposals = _generate_replan(jid, event)

    assert len(calls) == 1
    snapshot_ids = {
        item["diff"]["verified_evidence"]["mobility"]["snapshot"]["snapshot_id"]
        for item in proposals
    }
    assert len(snapshot_ids) == 1
    assert len(primary.calls) == len(set(primary.calls))


def test_prt02_replan_evidence_reports_real_provider_provenance(monkeypatch):
    jid, _plan, event = _prepare_disruption()
    primary = _Provider("valhalla")

    def capture_runtime(_session, _journey, pairs, *, operation_key, captured_at=None):
        return capture_mobility_snapshot(
            pairs,
            primary=primary,
            captured_at=captured_at,
            operation_key=operation_key,
        )

    monkeypatch.setattr(
        main_module, "capture_runtime_mobility_snapshot", capture_runtime, raising=False
    )
    _chosen, proposals = _generate_replan(jid, event)

    for proposal in proposals:
        mobility = proposal["diff"]["verified_evidence"]["mobility"]
        assert mobility["provider"] == "valhalla"
        assert mobility["snapshot"]["source_provider"] == "valhalla"


def test_prt03_partial_fallback_is_pair_scoped_and_auditable():
    primary = _Provider("valhalla", fail={("b", "c")})
    fallback = _Provider("mock-mobility")
    result = capture_mobility_snapshot(
        [("a", "b"), ("b", "c")],
        primary=primary,
        fallback=fallback,
        captured_at="2026-10-10T13:25:00+08:00",
        operation_key="replan:event-1",
    )

    evidence = result.snapshot.to_evidence()
    facts = {(item["origin_key"], item["destination_key"]): item for item in evidence["facts"]}
    assert facts[("a", "b")]["provider"] == "valhalla"
    assert facts[("a", "b")]["degraded"] is False
    assert facts[("b", "c")]["provider"] == "mock-mobility"
    assert facts[("b", "c")]["degraded"] is True
    assert facts[("b", "c")]["fallback_reason"]


def test_prt04_primary_and_fallback_failure_never_fabricates_eta():
    primary = _Provider("valhalla", fail={("a", "b")})
    fallback = _Provider("mock-mobility", fail={("a", "b")})
    with pytest.raises(MobilitySnapshotCaptureError):
        capture_mobility_snapshot(
            [("a", "b")],
            primary=primary,
            fallback=fallback,
            operation_key="replan:event-2",
        )


def test_prt05_authoritative_gate_never_calls_live_provider():
    class ExplodingLiveProvider:
        provider_key = "valhalla"
        calls = 0

        def estimate_minutes(self, _origin, _destination):
            self.calls += 1
            raise AssertionError("network-like provider was called inside gate")

    live = ExplodingLiveProvider()
    journey = {
        "id": "j",
        "city_code": "C",
        "start_at": "2026-10-10T09:00:00+08:00",
        "end_at": "2026-10-10T12:00:00+08:00",
        "budget_amount_minor": 1000,
        "budget_currency": "CNY",
    }
    places = {
        key: {
            "id": key,
            "city_code": "C",
            "opening_hours": {"open": "08:00", "close": "20:00"},
        }
        for key in ("a", "b")
    }
    activities = [
        FeasibilityActivity(
            "a",
            datetime.fromisoformat("2026-10-10T09:00:00+08:00"),
            datetime.fromisoformat("2026-10-10T09:30:00+08:00"),
            0,
            place_id="a",
            currency="CNY",
        ),
        FeasibilityActivity(
            "b",
            datetime.fromisoformat("2026-10-10T09:45:00+08:00"),
            datetime.fromisoformat("2026-10-10T10:15:00+08:00"),
            0,
            place_id="b",
            currency="CNY",
        ),
    ]
    result = evaluate_feasibility(
        build_context(journey, [], places, {}, mobility_provider=live, plan_currency="CNY"),
        activities,
    )
    assert "NON_DETERMINISTIC_MOBILITY_PROVIDER" in result.failures
    assert live.calls == 0


def test_prt06_twin_reads_persisted_snapshot_without_runtime_capture(monkeypatch):
    jid, _plan, event = _prepare_disruption()
    primary = _Provider("valhalla")

    def capture_runtime(_session, _journey, pairs, *, operation_key, captured_at=None):
        return capture_mobility_snapshot(
            pairs,
            primary=primary,
            captured_at=captured_at,
            operation_key=operation_key,
        )

    monkeypatch.setattr(
        main_module, "capture_runtime_mobility_snapshot", capture_runtime, raising=False
    )
    _generate_replan(jid, event)

    def explode(*_args, **_kwargs):
        raise AssertionError("Twin read attempted runtime provider capture")

    monkeypatch.setattr(main_module, "capture_runtime_mobility_snapshot", explode, raising=False)
    twin = client.get(
        f"/api/v1/journeys/{jid}/twin",
        params={"now": "2026-10-10T13:25:00+08:00"},
    )
    assert twin.status_code == 200, twin.text
    assert twin.json()["context"]["mobility"]["provider"] == "valhalla"


def test_prt07_offline_golden_replan_remains_operational():
    jid, _plan, event = _prepare_disruption()
    chosen, proposals = _generate_replan(jid, event)
    assert all(item["hard_constraints_passed"] for item in proposals)
    accepted = client.post(
        f"/api/v1/replans/{chosen['id']}/accept",
        headers={"Idempotency-Key": f"prt-accept-{uuid4()}"},
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["proposal"]["status"] == "ACCEPTED"


def test_prt08_mocktransport_valhalla_to_snapshot_to_gate_chain():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/route")
        return httpx.Response(200, json={"trip": {"summary": {"time": 600, "length": 1.0}}})

    points = {
        "a": {"latitude": 39.1, "longitude": 117.2},
        "b": {"latitude": 39.2, "longitude": 117.3},
    }
    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        provider = ValhallaHTTPMobilityProvider(
            client=http_client,
            base_url="http://valhalla.test",
            place_lookup=points.__getitem__,
        )
        snapshot = capture_mobility_snapshot(
            [("a", "b")],
            primary=provider,
            captured_at="2026-10-10T08:59:00+08:00",
            operation_key="replan:event-3",
        ).snapshot

    journey = {
        "id": "j",
        "city_code": "C",
        "start_at": "2026-10-10T09:00:00+08:00",
        "end_at": "2026-10-10T12:00:00+08:00",
        "budget_amount_minor": 1000,
        "budget_currency": "CNY",
    }
    places = {
        key: {
            "id": key,
            "city_code": "C",
            "opening_hours": {"open": "08:00", "close": "20:00"},
        }
        for key in ("a", "b")
    }
    activities = [
        FeasibilityActivity(
            "a",
            datetime.fromisoformat("2026-10-10T09:00:00+08:00"),
            datetime.fromisoformat("2026-10-10T09:30:00+08:00"),
            0,
            place_id="a",
            currency="CNY",
        ),
        FeasibilityActivity(
            "b",
            datetime.fromisoformat("2026-10-10T09:45:00+08:00"),
            datetime.fromisoformat("2026-10-10T10:15:00+08:00"),
            0,
            place_id="b",
            currency="CNY",
        ),
    ]
    gate = evaluate_feasibility(
        build_context(journey, [], places, {}, mobility_provider=snapshot, plan_currency="CNY"),
        activities,
    )
    assert gate.passed, gate.failures
    evidence = snapshot.to_evidence()
    assert evidence["source_provider"] == "valhalla-http"
    assert evidence["facts"][0]["minutes"] == 10
    assert evidence["facts"][0]["route_provenance"] == "LIVE_PROVIDER_CAPTURE"
    assert evidence["facts"][0]["cost_provenance"] == "CITYNEXUS_COST_ESTIMATE"
    assert evidence["facts"][0]["time_basis"] == "VALHALLA_ROUTE_CAPTURED_AT"
