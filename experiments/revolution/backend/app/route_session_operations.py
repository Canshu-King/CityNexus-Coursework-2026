"""Durable lifecycle for one exact, on-demand mobility edge."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.audit import record_audit
from app.core.config import settings
from app.db.models import Journey, MobilityFact, ProviderCall, RouteAcquisitionOperation
from app.db.session import SessionLocal
from app.execution.route_session_contract import (
    COMPLETED,
    FAILED,
    OUTCOME_PROVIDER_DEGRADED,
    OUTCOME_PROVIDER_FAILURE,
    OUTCOME_ROUTE_READY,
    OUTCOME_ROUTE_UNAVAILABLE,
    QUEUED,
    ROUTE_PROVIDER_POLICY_VERSION,
    ROUTE_TRUTH_POLICY_VERSION,
    RUNNING,
    UNAVAILABLE,
    RouteRequest,
    classify_capture,
    fact_is_eligible_for_live_readiness,
    route_request_fingerprint,
)
from app.integrations.mobility.runtime import capture_runtime_mobility_snapshot
from app.integrations.mobility.snapshot_capture import MobilitySnapshotCaptureError

OPEN_STATUSES = (QUEUED, RUNNING)
TERMINAL_STATUSES = frozenset({COMPLETED, UNAVAILABLE, FAILED})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _loads(value: str | None) -> dict[str, Any] | None:
    try:
        parsed = json.loads(value or "{}")
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) and parsed else None


def _is_private_buddy_request(value: str | None) -> bool:
    request = _loads(value)
    evidence = request.get("origin_evidence") if request else None
    return isinstance(evidence, dict) and evidence.get("private_scope") == "R80_PRIVATE_BUDDY_HANDOFF"


def status_url(journey_id: str, operation_id: str) -> str:
    return f"/api/v1/journeys/{journey_id}/routes/operations/{operation_id}"


def find_open_operation(
    session: Session, journey_id: str, fingerprint: str
) -> RouteAcquisitionOperation | None:
    return session.scalar(
        select(RouteAcquisitionOperation)
        .where(
            RouteAcquisitionOperation.journey_id == journey_id,
            RouteAcquisitionOperation.fingerprint == fingerprint,
            RouteAcquisitionOperation.status.in_(OPEN_STATUSES),
        )
        .order_by(RouteAcquisitionOperation.queued_at.desc())
    )


def find_reusable_completed_operation(
    session: Session, journey_id: str, fingerprint: str
) -> RouteAcquisitionOperation | None:
    """Reuse a completed operation while its frozen fact is still current."""

    row = session.scalar(
        select(RouteAcquisitionOperation)
        .where(
            RouteAcquisitionOperation.journey_id == journey_id,
            RouteAcquisitionOperation.fingerprint == fingerprint,
            RouteAcquisitionOperation.status == COMPLETED,
        )
        .order_by(RouteAcquisitionOperation.finished_at.desc(), RouteAcquisitionOperation.id.desc())
    )
    if row is None:
        return None
    result = _loads(row.result_json)
    fact_id = result.get("mobility_fact_id") if result else None
    if not fact_id:
        # R80 deliberately keeps member-private route evidence out of the shared
        # MobilityFact/audit projection. Its exact request is still idempotent.
        return row if _is_private_buddy_request(row.request_json) else None
    fact = session.get(MobilityFact, fact_id)
    if fact is None or fact.expires_at:
        if fact is None:
            return None
        try:
            if datetime.fromisoformat(str(fact.expires_at).replace("Z", "+00:00")) <= datetime.now(timezone.utc):
                return None
        except ValueError:
            return None
    return row


def operation_payload(row: RouteAcquisitionOperation, *, reused: bool = False) -> dict[str, Any]:
    result = _loads(row.result_json)
    error = _loads(row.error_json)
    terminal = row.status in TERMINAL_STATUSES
    return {
        "operation_id": row.id,
        "journey_id": row.journey_id,
        "fingerprint": row.fingerprint,
        "reused": reused,
        "status": row.status,
        "phase": row.phase,
        "outcome": row.outcome,
        "terminal": terminal,
        "retryable": row.status == FAILED,
        "origin_key": row.origin_key,
        "target_key": row.target_key,
        "target_activity_id": row.target_activity_id,
        "requested_mode": row.requested_mode,
        "provider": (result or {}).get("provider"),
        "provider_provenance": (result or {}).get("provenance"),
        "result": result,
        "error": error,
        "queued_at": row.queued_at,
        "started_at": row.started_at,
        "finished_at": row.finished_at,
        "updated_at": row.updated_at,
        "status_url": status_url(row.journey_id, row.id),
        "poll": {"status_url": status_url(row.journey_id, row.id), "retry_after_ms": 0 if terminal else 1000},
    }


def _fact_payload(fact: Any, *, degraded: bool = False, operation_id: str | None = None) -> dict[str, Any]:
    return {
        "origin_key": fact.origin_key,
        "target_key": fact.destination_key,
        "duration_min": fact.minutes,
        "distance_m": fact.distance_m,
        "walking_min": fact.walking_min,
        "mode": fact.mode or "WALK",
        "provider": fact.provider_key,
        "provenance": fact.route_provenance or fact.fact_quality,
        "fact_quality": fact.fact_quality,
        "geometry": list(fact.route_geometry or ()),
        "captured_at": fact.captured_at,
        "degraded": degraded,
        "operation_id": operation_id,
    }


def _fact_dict(fact: Any, *, degraded: bool = False) -> dict[str, Any]:
    return {
        "duration_min": fact.minutes,
        "minutes": fact.minutes,
        "provider": fact.provider_key,
        "provider_key": fact.provider_key,
        "provenance": fact.route_provenance or fact.fact_quality,
        "route_provenance": fact.route_provenance,
        "fact_quality": fact.fact_quality,
        "degraded": degraded,
    }


def _persist_fact(
    session: Session,
    row: RouteAcquisitionOperation,
    fact: Any,
    *,
    degraded: bool,
) -> MobilityFact:
    mode = str(fact.mode or row.requested_mode).upper()
    existing = session.scalar(
        select(MobilityFact).where(
            MobilityFact.journey_id == row.journey_id,
            MobilityFact.from_key == row.origin_key,
            MobilityFact.to_key == row.target_key,
            MobilityFact.mode == mode,
        )
    )
    distance = int(fact.distance_m or 0)
    walking = int(fact.walking_min if fact.walking_min is not None else fact.minutes)
    payload = {
        "route_session_operation_id": row.id,
        "degraded": degraded,
        "distance_explicit": fact.distance_m is not None,
        "fact_quality": fact.fact_quality,
    }
    captured_at = str(fact.captured_at)
    try:
        expires_at = (
            datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
            + timedelta(seconds=settings.real_city_cache_ttl_seconds)
        ).isoformat()
    except (TypeError, ValueError):
        expires_at = None
    values = {
        "journey_id": row.journey_id,
        "option_id": None,
        "from_key": row.origin_key,
        "to_key": row.target_key,
        "mode": mode,
        "duration_min": int(fact.minutes),
        "distance_m": distance,
        "cost_amount_minor": None,
        "currency": None,
        "cost_status": "UNKNOWN",
        "walking_min": walking,
        "transfers": 0,
        "weather_exposure": "UNKNOWN",
        "reliability_score": 0.8 if degraded else 1.0,
        "provider_key": str(fact.provider_key),
        "provenance": str(fact.route_provenance or fact.fact_quality or "UNKNOWN"),
        "geometry_json": _json(list(fact.route_geometry or ())),
        "crs": "WGS84",
        "paid_mode": False,
        "evidence_json": _json(payload),
        "captured_at": captured_at,
        "expires_at": expires_at,
    }
    if existing is None:
        existing = MobilityFact(id=str(uuid4()), **values)
        session.add(existing)
        before = None
    else:
        before = {key: getattr(existing, key) for key in values if hasattr(existing, key)}
        for key, value in values.items():
            setattr(existing, key, value)
    record_audit(
        session,
        action="journey.route_session.mobility_fact_frozen",
        entity_type="MobilityFact",
        entity_id=existing.id,
        journey_id=row.journey_id,
        before=before,
        after={"operation_id": row.id, **values},
    )
    return existing


def _write_provider_call(
    session: Session,
    row: RouteAcquisitionOperation,
    *,
    status: str,
    response: dict[str, Any] | None,
) -> None:
    provider_status = {
        "COMPLETED": "SUCCESS",
        "UNAVAILABLE": "FAILURE",
        "FAILED": "FAILURE",
    }.get(status, status)
    if response and response.get("degraded"):
        provider_status = "DEGRADED"
    session.add(
        ProviderCall(
            id=str(uuid4()),
            provider_key="valhalla" if settings.live_valhalla_url else "mock-mobility",
            operation="ROUTE_ACQUISITION",
            request_json=row.request_json,
            response_json=_json(response) if response is not None else None,
            status=provider_status,
            latency_ms=None,
            created_at=_now(),
        )
    )


def _finish(
    session: Session,
    row: RouteAcquisitionOperation,
    *,
    status: str,
    outcome: str,
    result: dict[str, Any] | None = None,
    error: dict[str, Any] | None = None,
) -> None:
    stamp = _now()
    row.status = status
    row.outcome = outcome
    row.phase = "FINALIZING"
    row.result_json = _json(result or {})
    row.error_json = _json(error or {})
    row.finished_at = stamp
    row.updated_at = stamp
    session.commit()


def _execute(operation_id: str) -> None:
    with SessionLocal() as session:
        row = session.get(RouteAcquisitionOperation, operation_id)
        if row is None or row.status not in OPEN_STATUSES:
            return
        journey = session.get(Journey, row.journey_id)
        if journey is None:
            _finish(session, row, status=FAILED, outcome=OUTCOME_PROVIDER_FAILURE, error={"reason": "JOURNEY_NOT_FOUND"})
            return
        row.status = RUNNING
        row.phase = "CAPTURE"
        row.started_at = _now()
        row.updated_at = _now()
        session.commit()
        request = json.loads(row.request_json or "{}")
        pair = (row.origin_key, row.target_key)
        provider_name = "valhalla" if settings.live_valhalla_url else "mock-mobility"
        try:
            captured = capture_runtime_mobility_snapshot(
                session,
                journey,
                [pair],
                operation_key=f"route-session:{row.id}",
                require_explicit_facts=True,
                requested_mode=row.requested_mode,
            )
        except MobilitySnapshotCaptureError as exc:
            live_failure = bool(settings.live_valhalla_url)
            status = FAILED if live_failure else UNAVAILABLE
            outcome = OUTCOME_PROVIDER_FAILURE if live_failure else OUTCOME_ROUTE_UNAVAILABLE
            _write_provider_call(session, row, status=status, response={"error": type(exc).__name__})
            session.commit()
            _finish(
                session,
                row,
                status=status,
                outcome=outcome,
                result={"provider": provider_name, "attempt_count": 1},
                error={"classification": outcome, "message": str(exc)},
            )
            return
        except Exception as exc:  # external provider boundary
            _write_provider_call(session, row, status="FAILED", response={"error": type(exc).__name__})
            session.commit()
            _finish(
                session,
                row,
                status=FAILED,
                outcome=OUTCOME_PROVIDER_FAILURE,
                result={"provider": provider_name, "attempt_count": 1},
                error={"classification": OUTCOME_PROVIDER_FAILURE, "message": str(exc)},
            )
            return

        fact = captured.snapshot.facts.get(pair)
        explicit = _fact_dict(fact, degraded=bool(captured.failed_primary_pairs or captured.degraded)) if fact else None
        if fact is None or not fact_is_eligible_for_live_readiness(explicit):
            _write_provider_call(
                session,
                row,
                status="UNAVAILABLE",
                response={"classification": OUTCOME_ROUTE_UNAVAILABLE, "provider": captured.snapshot.source_provider_key},
            )
            session.commit()
            _finish(
                session,
                row,
                status=UNAVAILABLE,
                outcome=OUTCOME_ROUTE_UNAVAILABLE,
                result={"provider": provider_name, "attempt_count": 1},
                error={"classification": OUTCOME_ROUTE_UNAVAILABLE, "reason": "NO_EXPLICIT_LIVE_FACT"},
            )
            return

        degraded = bool(captured.failed_primary_pairs or captured.degraded)
        status, outcome = classify_capture(
            explicit_fact=explicit,
            provider_failed=False,
            provider_degraded=degraded,
        )
        private_buddy_route = _is_private_buddy_request(row.request_json)
        frozen = None if private_buddy_route else _persist_fact(session, row, fact, degraded=degraded)
        result = _fact_payload(fact, degraded=degraded, operation_id=row.id)
        result.update({"provider": fact.provider_key})
        if frozen is not None:
            result["mobility_fact_id"] = frozen.id
        _write_provider_call(session, row, status="COMPLETED", response=result)
        session.commit()
        _finish(session, row, status=status, outcome=outcome, result=result)


def submit_operation(
    session: Session,
    journey: Journey,
    request: RouteRequest,
) -> tuple[RouteAcquisitionOperation, bool]:
    fingerprint = route_request_fingerprint(request)
    existing = find_open_operation(session, journey.id, fingerprint)
    if existing is not None:
        return existing, True
    completed = find_reusable_completed_operation(session, journey.id, fingerprint)
    if completed is not None:
        return completed, True
    stamp = _now()
    row = RouteAcquisitionOperation(
        id=str(uuid4()),
        journey_id=journey.id,
        fingerprint=fingerprint,
        origin_key=request.origin_key,
        target_key=request.target_key,
        target_activity_id=request.target_activity_id,
        requested_mode=request.semantic_payload()["requested_mode"],
        provider_policy_version=request.provider_policy_version,
        route_truth_policy_version=request.route_truth_policy_version,
        status=QUEUED,
        phase=QUEUED,
        request_json=_json(request.semantic_payload()),
        result_json="{}",
        error_json="{}",
        queued_at=stamp,
        updated_at=stamp,
    )
    session.add(row)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        winner = find_open_operation(session, journey.id, fingerprint)
        if winner is None:
            raise
        return winner, True
    _execute(row.id)
    session.expire_all()
    finished = session.get(RouteAcquisitionOperation, row.id)
    return finished or row, False
