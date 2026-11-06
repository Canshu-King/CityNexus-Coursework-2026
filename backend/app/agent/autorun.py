from __future__ import annotations

import asyncio
import json
import threading
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import httpx
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from app.agent.guardian import run_preemptive_guardian, scan_guardian
from app.agent.predictive import (
    build_resilience_snapshot,
    counterfactual_payload,
    simulate_counterfactuals,
    snapshot_payload,
)
from app.agent.signal_pipeline import SignalPipelineResult, refresh_demo_signals
from app.core.audit import record_audit
from app.core.config import settings
from app.db.models import (
    Activity,
    AutopilotEnrollment,
    GuardianPulse,
    Journey,
    JourneyMission,
    ProviderObservation,
)
from app.db.session import SessionLocal
from app.execution.traveler_twin import refresh_snapshot
from app.integrations.mobility.runtime import capture_runtime_mobility_snapshot
from app.observability.tracing import persist_span, phoenix_status

_worker_lock = threading.Lock()
_cycle_lock = threading.Lock()
_worker_thread: threading.Thread | None = None
_worker_stop: threading.Event | None = None
_worker_interval_seconds = settings.guardian_interval_seconds
_last_pulse_at: str | None = None
_last_error: str | None = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _live_weather_refresh(session, journey: Journey) -> SignalPipelineResult:
    """Capture weather and mobility independently in one composable live pulse."""

    from app.integrations.context.open_meteo import OpenMeteoContextProvider
    from app.integrations.context.weather_runtime import refresh_weather_runtime

    async def capture():
        async with httpx.AsyncClient(timeout=settings.open_meteo_timeout_seconds) as client:
            provider = OpenMeteoContextProvider(client=client, base_url=settings.open_meteo_url)
            return await refresh_weather_runtime(
                session,
                journey,
                provider=provider,
                captured_at=_now(),
            )

    weather_error: str | None = None
    try:
        result = asyncio.run(capture())
    except Exception as exc:  # noqa: BLE001 - provider boundary; mobility remains useful
        weather_error = type(exc).__name__
        result = None
    activities = []
    if journey.active_plan_version_id:
        activities = list(
            session.scalars(
                select(Activity)
                .where(Activity.plan_version_id == journey.active_plan_version_id)
                .order_by(Activity.sequence_no)
            ).all()
        )
    locations = [item.place_id or item.city_service_id for item in activities]
    filtered_locations = [item for item in locations if item]
    pairs = list(zip(filtered_locations, filtered_locations[1:]))
    mobility_error: str | None = None
    mobility = None
    mobility_observation_id: str | None = None
    weather_observation_id: str | None = None
    if result is not None:
        precipitation_risks = [
            int(fact.evidence.get("precipitation_probability", 0) or 0)
            for snapshot in result.snapshots
            for fact in snapshot.facts
        ]
        weather_payload = {
            "state": "RISK_FORECAST" if result.event_ids else "NORMAL",
            "risk_score": max(precipitation_risks or [0]),
            "forecast_horizon_min": 45,
            "provider_key": result.provider_key,
            "runtime": result.to_evidence(),
        }
        weather_row = ProviderObservation(
            id=str(uuid4()),
            journey_id=journey.id,
            provider_key=result.provider_key,
            observation_type="CITY_SIGNAL",
            state="DEGRADED" if result.degraded else "CAPTURED",
            provenance="MIXED" if result.degraded else "LIVE_PROVIDER",
            captured_at=_now(),
            expires_at=None,
            payload_json=json.dumps(weather_payload, ensure_ascii=False),
            created_at=_now(),
        )
        session.add(weather_row)
        session.flush()
        weather_observation_id = weather_row.id
    try:
        mobility = capture_runtime_mobility_snapshot(
            session,
            journey,
            pairs,
            operation_key=f"guardian-live:{journey.id}",
            captured_at=_now(),
        )
        mobility_row = ProviderObservation(
            id=str(uuid4()),
            journey_id=journey.id,
            provider_key=mobility.snapshot.source_provider_key,
            observation_type="MOBILITY",
            state="DEGRADED" if mobility.degraded else "CAPTURED",
            provenance="LIVE_PROVIDER" if not mobility.degraded else "MIXED",
            captured_at=mobility.snapshot.captured_at,
            expires_at=None,
            payload_json=json.dumps(mobility.snapshot.to_evidence(), ensure_ascii=False),
            created_at=_now(),
        )
        session.add(mobility_row)
        session.flush()
        mobility_observation_id = mobility_row.id
    except Exception as exc:  # noqa: BLE001 - provider boundary; weather evidence remains useful
        mobility_error = type(exc).__name__
    provider_states = {
        "weather": "DEGRADED" if result is None or result.degraded else "CAPTURED",
        "mobility": ("DEGRADED" if mobility is None or mobility.degraded else "CAPTURED"),
    }
    provider_provenance = {
        "weather": "LIVE_PROVIDER" if result is not None else "UNAVAILABLE",
        "mobility": "LIVE_PROVIDER" if mobility is not None and not mobility.degraded else "MIXED",
    }
    return SignalPipelineResult(
        provider_key="live-provider-mesh",
        state=(
            "RISK_FORECAST"
            if result is not None and result.event_ids
            else "DISRUPTED"
            if result is not None and result.created_event_ids
            else "NORMAL"
        ),
        observation_ids=tuple(
            item for item in (weather_observation_id, mobility_observation_id) if item
        ),
        event_ids=result.event_ids if result is not None else (),
        created_event_ids=result.created_event_ids if result is not None else (),
        resolved_event_ids=(),
        degraded=bool(weather_error or mobility_error or (result and result.degraded)),
        provider_states=provider_states,
        provider_provenance=provider_provenance,
        captured_at=_now(),
    )


def refresh_signals(session, journey: Journey) -> SignalPipelineResult:
    if settings.guardian_signal_provider in {"open-meteo", "live", "weather"}:
        return _live_weather_refresh(session, journey)
    return refresh_demo_signals(session, journey)


def ensure_enrollment(
    session,
    journey: Journey,
    *,
    enabled: bool = True,
    interval_seconds: float | None = None,
) -> AutopilotEnrollment:
    now = _now()
    row = session.get(AutopilotEnrollment, journey.id)
    if row is None:
        row = AutopilotEnrollment(
            journey_id=journey.id,
            enabled=enabled,
            interval_seconds=float(interval_seconds or settings.guardian_interval_seconds),
            provider_mesh_json=json.dumps(
                {
                    "weather": "open-meteo",
                    "mobility": "valhalla",
                    "city_discovery": "nominatim+overpass",
                },
                sort_keys=True,
            ),
            created_at=now,
            updated_at=now,
        )
        session.add(row)
    else:
        row.enabled = enabled
        if interval_seconds is not None:
            row.interval_seconds = float(interval_seconds)
        row.updated_at = now
    session.flush()
    return row


def enrolled_journey_ids(session) -> list[str]:
    return list(
        session.scalars(
            select(AutopilotEnrollment.journey_id)
            .where(AutopilotEnrollment.enabled.is_(True))
            .order_by(AutopilotEnrollment.journey_id)
        ).all()
    )


def _due(enrollment: AutopilotEnrollment, *, now: datetime) -> bool:
    if not enrollment.enabled or not enrollment.last_pulse_at:
        return bool(enrollment.enabled)
    try:
        previous = datetime.fromisoformat(enrollment.last_pulse_at)
    except ValueError:
        return True
    if previous.tzinfo is None:
        previous = previous.replace(tzinfo=timezone.utc)
    return (now - previous).total_seconds() >= float(enrollment.interval_seconds)


def next_due_seconds(session, *, now: datetime | None = None) -> float | None:
    stamp = now or datetime.now(timezone.utc)
    rows = list(
        session.scalars(
            select(AutopilotEnrollment).where(AutopilotEnrollment.enabled.is_(True))
        ).all()
    )
    if not rows:
        return None
    waits: list[float] = []
    for row in rows:
        if not row.last_pulse_at:
            waits.append(0.0)
            continue
        try:
            previous = datetime.fromisoformat(row.last_pulse_at)
            if previous.tzinfo is None:
                previous = previous.replace(tzinfo=timezone.utc)
            waits.append(max(0.0, float(row.interval_seconds) - (stamp - previous).total_seconds()))
        except ValueError:
            waits.append(0.0)
    return min(waits)


def pulse_journey(session, journey: Journey) -> dict[str, Any]:
    """Persist one sensing -> Guardian evaluation cycle."""

    started_at = _now()
    trace_id = str(uuid4())
    trace_spans: list[dict[str, Any]] = []
    pulse = GuardianPulse(
        id=str(uuid4()),
        journey_id=journey.id,
        status="RUNNING",
        provider_key="composite-provider-mesh",
        observation_ids_json="[]",
        result_json="{}",
        started_at=started_at,
    )
    session.add(pulse)
    pulse_id = pulse.id
    record_audit(
        session,
        action="guardian.pulse.started",
        entity_type="GuardianPulse",
        entity_id=pulse.id,
        journey_id=journey.id,
        actor_type="SYSTEM",
        actor_id="guardian-worker",
        after={"pulse_id": pulse.id, "provider_key": pulse.provider_key},
        trace_id=trace_id,
    )
    session.flush()
    try:
        signals = refresh_signals(session, journey)
        trace_spans.append(
            persist_span(
                session,
                journey_id=journey.id,
                trace_id=trace_id,
                name="provider.capture",
                attributes={
                    "provider_key": signals.provider_key,
                    "observation_ids": list(signals.observation_ids),
                    "provider_states": signals.provider_states,
                    "provider_provenance": signals.provider_provenance,
                    "degraded": signals.degraded,
                },
            )
        )
        pulse.observation_ids_json = json.dumps(list(signals.observation_ids))
        guardian_result = None
        resilience = None
        counterfactuals: list[dict[str, Any]] = []
        # A provider outage is evidence, not permission to mutate a stale Plan.
        twin_snapshot = refresh_snapshot(session, journey.traveler_id)
        trace_spans.append(
            persist_span(
                session,
                journey_id=journey.id,
                trace_id=trace_id,
                name="traveler_twin.lookup",
                attributes={
                    "traveler_id": journey.traveler_id,
                    "evidence_count": twin_snapshot.evidence_count,
                    "confidence": twin_snapshot.confidence,
                },
            )
        )
        resilience = build_resilience_snapshot(session, journey)
        trace_spans.append(
            persist_span(
                session,
                journey_id=journey.id,
                trace_id=trace_id,
                name="resilience.evaluate",
                attributes={"snapshot_id": resilience.id, "score": resilience.score},
            )
        )
        if signals.state == "RISK_FORECAST" and resilience.status == "PREDICTIVE_WEAKNESS":
            if not signals.degraded:
                counterfactuals = [
                    counterfactual_payload(item)
                    for item in simulate_counterfactuals(session, journey, resilience)
                ]
                trace_spans.append(
                    persist_span(
                        session,
                        journey_id=journey.id,
                        trace_id=trace_id,
                        name="resilience.counterfactuals",
                        attributes={"count": len(counterfactuals), "solver": "CP-SAT_V2"},
                    )
                )
                guardian_result = run_preemptive_guardian(
                    session,
                    journey,
                    resilience,
                )
            else:
                guardian_result = scan_guardian(session, journey)
        else:
            guardian_result = scan_guardian(session, journey)
        if guardian_result is not None:
            pulse.guardian_run_id = guardian_result.get("id")
            evidence = guardian_result.get("evidence") or {}
            standing_mission = guardian_result.get("standing_mission") or {}
            trace_spans.append(
                persist_span(
                    session,
                    journey_id=journey.id,
                    trace_id=trace_id,
                    name="mission.compile",
                    attributes={
                        "mission_id": standing_mission.get("id"),
                        "requested_autonomy": standing_mission.get("requested_autonomy"),
                        "protected_activity_count": len(
                            standing_mission.get("protected_activity_ids") or []
                        ),
                    },
                )
            )
            agent_runtime = guardian_result.get("agent_runtime") or {}
            trace_spans.append(
                persist_span(
                    session,
                    journey_id=journey.id,
                    trace_id=trace_id,
                    name="agent.model_call",
                    attributes={
                        "provider_key": agent_runtime.get("provider_key"),
                        "model_key": agent_runtime.get("model_key"),
                        "status": agent_runtime.get("status"),
                    },
                )
            )
            tool_steps = guardian_result.get("tool_steps") or []
            trace_spans.append(
                persist_span(
                    session,
                    journey_id=journey.id,
                    trace_id=trace_id,
                    name="agent.tool_calls",
                    attributes={
                        "count": len(tool_steps),
                        "tools": [item.get("tool_name") for item in tool_steps],
                    },
                )
            )
            if any(item.get("tool_name") == "search_city_services" for item in tool_steps):
                trace_spans.append(
                    persist_span(
                        session,
                        journey_id=journey.id,
                        trace_id=trace_id,
                        name="real_city.discovery",
                        attributes={"tool": "search_city_services"},
                    )
                )
            if any(item.get("tool_name") == "request_replan" for item in tool_steps):
                solver_evidence = evidence.get("solver") or {}
                trace_spans.append(
                    persist_span(
                        session,
                        journey_id=journey.id,
                        trace_id=trace_id,
                        name="cp_sat.solve",
                        attributes={
                            "generator": solver_evidence.get("generator")
                            if isinstance(solver_evidence, dict)
                            else solver_evidence,
                            "proposal_id": guardian_result.get("proposal_id"),
                        },
                    )
                )
            trace_spans.append(
                persist_span(
                    session,
                    journey_id=journey.id,
                    trace_id=trace_id,
                    name="gateway.policy_decision",
                    attributes={
                        "decision": (evidence.get("policy") or {}).get("decision"),
                        "action_status": guardian_result.get("action_status"),
                    },
                )
            )
            trace_spans.append(
                persist_span(
                    session,
                    journey_id=journey.id,
                    trace_id=trace_id,
                    name="domain.mutation",
                    attributes={
                        "executed": bool((evidence.get("mutation") or {}).get("executed")),
                        "active_plan_after_id": guardian_result.get("active_plan_after_id"),
                    },
                )
            )
        result = {
            "signals": signals.to_dict(),
            "guardian": guardian_result,
            "resilience": snapshot_payload(resilience),
            "counterfactuals": counterfactuals,
            "trace": {
                "trace_id": trace_id,
                "phoenix": phoenix_status(),
                "spans": trace_spans,
            },
        }
        pulse.status = "COMPLETED"
        pulse.result_json = json.dumps(result, ensure_ascii=False, default=str)
        pulse.completed_at = _now()
        record_audit(
            session,
            action="guardian.pulse.completed",
            entity_type="GuardianPulse",
            entity_id=pulse.id,
            journey_id=journey.id,
            actor_type="SYSTEM",
            actor_id="guardian-worker",
            after={
                "pulse_id": pulse.id,
                "guardian_run_id": pulse.guardian_run_id,
                "created_event_ids": list(signals.created_event_ids),
                "degraded": signals.degraded,
            },
            trace_id=trace_id,
        )
        session.commit()
        return {"pulse_id": pulse.id, **result}
    except Exception as exc:  # provider/agent boundary: preserve worker liveness
        failed_pulse_id = pulse_id
        session.rollback()
        pulse = session.get(GuardianPulse, failed_pulse_id)
        if pulse is not None:
            pulse.status = "FAILED"
            pulse.result_json = json.dumps({"error": str(exc)}, ensure_ascii=False)
            pulse.completed_at = _now()
            session.commit()
        raise


def run_once(
    *, enrolled_only: bool = False, respect_schedule: bool = False
) -> list[dict[str, Any]]:
    """Run one deterministic-clock-friendly worker cycle."""

    global _last_pulse_at, _last_error
    results: list[dict[str, Any]] = []
    with _cycle_lock, SessionLocal() as session:
        statement = select(Journey).where(Journey.status == "ACTIVE")
        active_enrollments = enrolled_journey_ids(session)
        if enrolled_only:
            if not active_enrollments:
                _last_pulse_at = _now()
                return []
            statement = statement.where(Journey.id.in_(active_enrollments))
        elif active_enrollments:
            statement = statement.where(Journey.id.in_(active_enrollments))
        journeys = session.scalars(statement.order_by(Journey.id)).all()
        for journey in journeys:
            enrollment = session.get(AutopilotEnrollment, journey.id)
            if respect_schedule and (
                enrollment is None or not _due(enrollment, now=datetime.now(timezone.utc))
            ):
                continue
            mission = session.scalar(
                select(JourneyMission).where(
                    JourneyMission.journey_id == journey.id,
                    JourneyMission.is_standing.is_(True),
                    JourneyMission.status == "ACTIVE",
                )
            )
            if mission is None:
                continue
            try:
                results.append(pulse_journey(session, journey))
                if enrollment is not None:
                    enrollment.last_pulse_at = _now()
                    enrollment.last_error = None
                    enrollment.updated_at = _now()
                    session.commit()
                _last_error = None
            except Exception as exc:  # noqa: BLE001 - one journey cannot stop the worker
                _last_error = f"{type(exc).__name__}: {exc}"
                enrollment = session.get(AutopilotEnrollment, journey.id)
                if enrollment is not None:
                    enrollment.last_error = _last_error
                    enrollment.updated_at = _now()
                    session.commit()
        _last_pulse_at = _now()
    return results


def _loop(stop: threading.Event, interval_seconds: float) -> None:
    global _worker_thread
    while not stop.is_set():
        with SessionLocal() as session:
            due_wait = next_due_seconds(session)
        wait_seconds = max(
            0.05,
            min(float(interval_seconds), due_wait if due_wait is not None else interval_seconds),
        )
        if stop.wait(wait_seconds):
            break
        try:
            run_once(enrolled_only=True, respect_schedule=True)
        except Exception as exc:  # noqa: BLE001 - database/provider boundary
            global _last_error
            _last_error = f"{type(exc).__name__}: {exc}"
    with _worker_lock:
        _worker_thread = None


def start_autorun(*, interval_seconds: float | None = None) -> dict[str, Any]:
    global _worker_thread, _worker_stop, _worker_interval_seconds
    with _worker_lock:
        if _worker_thread is not None and _worker_thread.is_alive():
            return autorun_status()
        _worker_interval_seconds = float(
            interval_seconds if interval_seconds is not None else settings.guardian_interval_seconds
        )
        _worker_stop = threading.Event()
        _worker_thread = threading.Thread(
            target=_loop,
            args=(_worker_stop, _worker_interval_seconds),
            name="citynexus-guardian-worker",
            daemon=True,
        )
        _worker_thread.start()
        return autorun_status()


def stop_autorun() -> dict[str, Any]:
    thread: threading.Thread | None
    with _worker_lock:
        if _worker_stop is not None:
            _worker_stop.set()
        thread = _worker_thread
    if thread is not None and thread is not threading.current_thread():
        # Reset and test boundaries must not start a new worker while the old
        # one is still committing a pulse against the replaced Journey rows.
        # The worker checks the stop event between provider/Guardian cycles;
        # give one slow SQLite/provider cycle a bounded but meaningful drain
        # window before a reset or a new subscription can reuse the thread.
        thread.join(timeout=max(30.0, _worker_interval_seconds + 10.0))
    return autorun_status()


def autorun_status() -> dict[str, Any]:
    thread = _worker_thread
    try:
        with SessionLocal() as session:
            enrolled = enrolled_journey_ids(session)
    except SQLAlchemyError:
        enrolled = []
    return {
        "enabled": bool(thread is not None and thread.is_alive()),
        "interval_seconds": _worker_interval_seconds,
        "signal_provider": settings.guardian_signal_provider,
        "enrolled_journey_ids": enrolled,
        "last_pulse_at": _last_pulse_at,
        "last_error": _last_error,
    }


if settings.guardian_autorun:
    start_autorun()
