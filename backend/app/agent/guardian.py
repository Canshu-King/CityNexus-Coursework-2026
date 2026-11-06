from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent.gateway import AgentToolGateway
from app.agent.mission import mission_payload
from app.agent.orchestrator import configured_journey_agent
from app.agent.runtime import AgentRuntimeUnavailable
from app.core.audit import record_audit
from app.core.config import settings
from app.db.models import (
    AgentRun,
    AgentRunStep,
    Event,
    GuardianRun,
    Journey,
    JourneyMission,
    JourneyResilienceSnapshot,
    ReplanProposal,
)
from app.schemas import AgentToolCall

ACTIONABLE_EVENT_TYPES = frozenset({"WEATHER", "CLOSURE", "TRAFFIC_DELAY", "SCHEDULE_DELAY"})
SEVERITY_ORDER = {"CRITICAL": 4, "MAJOR": 3, "MINOR": 2, "INFO": 1}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _load(value: str | None, default: Any) -> Any:
    try:
        return json.loads(value or "{}")
    except (TypeError, json.JSONDecodeError):
        return default


def _standing_mission(session: Session, journey_id: str) -> JourneyMission | None:
    return session.scalar(
        select(JourneyMission)
        .where(
            JourneyMission.journey_id == journey_id,
            JourneyMission.is_standing.is_(True),
            JourneyMission.status == "ACTIVE",
        )
        .order_by(JourneyMission.created_at.desc(), JourneyMission.id.desc())
    )


def _open_events(session: Session, journey_id: str) -> list[Event]:
    return list(
        session.scalars(
            select(Event)
            .where(Event.journey_id == journey_id, Event.status.in_(("DETECTED", "ACKNOWLEDGED")))
            .order_by(Event.detected_at, Event.id)
        ).all()
    )


def _is_predictive(event: Event) -> bool:
    try:
        return bool(json.loads(event.payload_json or "{}").get("predictive_only"))
    except json.JSONDecodeError:
        return False


def _fingerprint(journey: Journey, mission: JourneyMission | None, events: list[Event]) -> str:
    from app.agent.signal_pipeline import incident_fingerprint

    incident_keys = []
    for event in events:
        try:
            payload = json.loads(event.payload_json or "{}")
        except json.JSONDecodeError:
            payload = {}
        incident_keys.append(
            payload.get("incident_fingerprint")
            or incident_fingerprint(
                journey_id=journey.id,
                provider_key=event.provider_key,
                event_type=event.event_type,
                scope_key=payload.get("scope_place_id") or event.affected_activity_id,
                occurred_at=event.occurred_at,
                disruption_class=payload.get("disruption_class"),
            )
        )
    material = {
        "journey_id": journey.id,
        "mission_id": mission.id if mission else None,
        "mission_resolved_intent": mission.resolved_intent_json if mission else None,
        "incident_keys": sorted(str(item) for item in incident_keys if item),
    }
    return hashlib.sha256(_json(material).encode("utf-8")).hexdigest()


def _cooldown_active(
    session: Session,
    *,
    journey_id: str,
    evidence_fingerprint: str,
    now: datetime,
) -> GuardianRun | None:
    """Consult the persisted cooldown before doing any recovery work."""

    prior = session.scalar(
        select(GuardianRun)
        .where(
            GuardianRun.journey_id == journey_id,
            GuardianRun.evidence_fingerprint == evidence_fingerprint,
        )
        .order_by(GuardianRun.created_at.desc(), GuardianRun.id.desc())
    )
    if prior is None or not prior.cooldown_until:
        return None
    try:
        cooldown_until = datetime.fromisoformat(prior.cooldown_until)
    except ValueError:
        return None
    if cooldown_until.tzinfo is None:
        cooldown_until = cooldown_until.replace(tzinfo=timezone.utc)
    return prior if now < cooldown_until else None


def _recovery_mission(
    session: Session,
    journey: Journey,
    standing: JourneyMission,
    trigger: Event,
) -> JourneyMission:
    """Create a child mission carrying standing authority plus live evidence."""

    now = _now().isoformat()
    mission = JourneyMission(
        id=str(uuid4()),
        journey_id=journey.id,
        traveler_id=journey.traveler_id,
        trigger_event_id=trigger.id,
        user_text=(
            f"{standing.user_text}\n\nLive incident evidence: "
            f"{trigger.event_type} {trigger.severity} affecting {trigger.affected_activity_id}."
        ),
        objective_codes_json=standing.objective_codes_json,
        protected_activity_ids_json=standing.protected_activity_ids_json,
        mutable_activity_ids_json=standing.mutable_activity_ids_json,
        intent_json=standing.intent_json,
        resolved_intent_json=standing.resolved_intent_json,
        allowed_action_classes_json=standing.allowed_action_classes_json,
        unresolved_references_json=standing.unresolved_references_json,
        compiler_provider_key=standing.compiler_provider_key,
        compiler_model_key=standing.compiler_model_key,
        compiler_confidence=standing.compiler_confidence,
        reasoning_summary="Recovery mission derived from the active standing mission and provider incident.",
        is_standing=False,
        parent_mission_id=standing.id,
        max_cost_delta_minor=standing.max_cost_delta_minor,
        max_loss_minor=standing.max_loss_minor,
        max_schedule_drift_min=standing.max_schedule_drift_min,
        preferences_json=standing.preferences_json,
        requested_autonomy=standing.requested_autonomy,
        status="CREATED",
        created_at=now,
    )
    session.add(mission)
    record_audit(
        session,
        action="guardian.mission.compiled",
        entity_type="JourneyMission",
        entity_id=mission.id,
        journey_id=journey.id,
        actor_type="SYSTEM",
        actor_id="guardian",
        after={
            "mission_id": mission.id,
            "parent_mission_id": standing.id,
            "trigger_event_id": trigger.id,
        },
    )
    session.flush()
    return mission


def _trace_value(step: dict[str, Any], key: str, default: Any = None) -> Any:
    result = step.get("result")
    return result.get(key, default) if isinstance(result, dict) else default


def _model_recovery(
    session: Session,
    journey: Journey,
    standing: JourneyMission,
    trigger: Event,
    run: GuardianRun,
    evidence: dict[str, Any],
) -> tuple[JourneyMission, AgentRun, dict[str, Any]]:
    """Run the configured Hermes/DeepSeek runtime and normalize its trace."""

    recovery = _recovery_mission(session, journey, standing, trigger)
    runtime = configured_journey_agent()
    try:
        result = runtime.run(session, journey=journey, mission=recovery)
    finally:
        close = getattr(runtime, "close", None)
        if callable(close):
            close()
    agent = session.get(AgentRun, result.get("run_id")) if result.get("run_id") else None
    if agent is None:
        agent = session.scalar(
            select(AgentRun)
            .where(AgentRun.mission_id == recovery.id)
            .order_by(AgentRun.created_at.desc(), AgentRun.id.desc())
        )
    if agent is None:
        raise AgentRuntimeUnavailable("configured Agent runtime returned no persisted AgentRun")
    run.agent_run_id = agent.id
    tool_steps = result.get("tool_steps") or []
    request_step = next(
        (step for step in reversed(tool_steps) if step.get("tool_name") == "request_replan"),
        None,
    )
    accept_step = next(
        (step for step in reversed(tool_steps) if step.get("tool_name") == "accept_replan"),
        None,
    )
    request_result = request_step.get("result") if request_step else {}
    proposal_payload = request_result.get("proposal") if isinstance(request_result, dict) else None
    if isinstance(proposal_payload, dict) and proposal_payload.get("id"):
        run.proposal_id = str(proposal_payload["id"])
    accept_arguments = accept_step.get("arguments") if accept_step else None
    if isinstance(accept_arguments, dict) and accept_arguments.get("proposal_id"):
        # The model may accept a proposal other than the first one it generated,
        # for example when a higher-scoring candidate breaks a protected
        # commitment. The audit record must name the proposal actually applied,
        # so the accept step's own argument wins over the request step's result.
        run.proposal_id = str(accept_arguments["proposal_id"])
    if run.proposal_id is not None:
        proposal = session.get(ReplanProposal, run.proposal_id)
        if proposal is not None:
            diff = json.loads(proposal.diff_json or "{}")
            evidence["solver"] = diff.get("verified_evidence", {}).get("generator")
    if accept_step is not None and isinstance(accept_step.get("result"), dict):
        accepted = accept_step["result"]
        evidence["policy"] = {
            "decision": accepted.get("decision"),
            "reason_codes": accepted.get("reason_codes", []),
            "policy_version": accepted.get("policy_version"),
        }
        evidence["mutation"] = {
            "executed": bool(accepted.get("executed")),
            "proposal_id": run.proposal_id,
        }
        run.action_status = (
            "EXECUTED"
            if accepted.get("executed")
            else accepted.get("decision", "MODEL_NO_MUTATION")
        )
    elif request_step is not None:
        run.action_status = "PROPOSAL_PRESENTED"
        evidence["mutation"] = {"executed": False, "proposal_id": run.proposal_id}
    else:
        run.action_status = "MODEL_NO_ACTION"
        evidence["mutation"] = {"executed": False, "proposal_id": None}
    evidence["inference"] = {
        "decision": "RECOVERY_REQUIRED",
        "controller": "configured_agent_runtime",
        "provider_key": agent.provider_key,
        "model_key": agent.model_key,
        "tool_selection": [step.get("tool_name") for step in tool_steps],
    }
    run.reasoning_summary = (
        result.get("outcome") or "Configured Agent runtime completed recovery evaluation."
    )
    return recovery, agent, result


def guardian_payload(
    session: Session, run: GuardianRun, *, deduplicated: bool = False
) -> dict[str, Any]:
    mission = (
        session.get(JourneyMission, run.standing_mission_id) if run.standing_mission_id else None
    )
    agent = session.get(AgentRun, run.agent_run_id) if run.agent_run_id else None
    steps = []
    if agent is not None:
        rows = session.scalars(
            select(AgentRunStep)
            .where(AgentRunStep.run_id == agent.id)
            .order_by(AgentRunStep.sequence_no)
        ).all()
        steps = [
            {
                "sequence_no": row.sequence_no,
                "tool_name": row.tool_name,
                "arguments": json.loads(row.arguments_json or "{}"),
                "result": json.loads(row.result_json or "{}"),
                "decision": row.decision,
                "status": row.status,
            }
            for row in rows
        ]
    return {
        "id": run.id,
        "journey_id": run.journey_id,
        "decision": run.decision,
        "status": run.status,
        "action_status": run.action_status,
        "deduplicated": deduplicated,
        "standing_mission": mission_payload(mission) if mission else None,
        "trigger_event_id": run.trigger_event_id,
        "trigger_kind": run.trigger_kind,
        "resilience_snapshot_id": run.resilience_snapshot_id,
        "proposal_id": run.proposal_id,
        "agent_run_id": run.agent_run_id,
        "agent_runtime": (
            {
                "provider_key": agent.provider_key,
                "model_key": agent.model_key,
                "status": agent.status,
            }
            if agent
            else None
        ),
        "tool_steps": steps,
        "evidence": json.loads(run.evidence_json or "{}"),
        "reasoning_summary": run.reasoning_summary,
        "active_plan_before_id": run.active_plan_before_id,
        "active_plan_after_id": run.active_plan_after_id,
        "created_at": run.created_at,
        "completed_at": run.completed_at,
        "cooldown_until": run.cooldown_until,
    }


def _new_agent_run(session: Session, journey: Journey, mission: JourneyMission) -> AgentRun:
    now = _now().isoformat()
    run = AgentRun(
        id=str(uuid4()),
        mission_id=mission.id,
        journey_id=journey.id,
        provider_key="citynexus-guardian",
        model_key="deterministic-recovery-controller-v1",
        status="RUNNING",
        created_at=now,
        started_at=now,
    )
    session.add(run)
    session.flush()
    return run


def _step(
    session: Session,
    run: AgentRun,
    sequence: int,
    tool: str,
    arguments: dict[str, Any],
    result: dict[str, Any],
) -> None:
    now = _now().isoformat()
    session.add(
        AgentRunStep(
            id=str(uuid4()),
            run_id=run.id,
            sequence_no=sequence,
            tool_name=tool,
            arguments_json=_json(arguments),
            result_json=_json(result),
            decision=result.get("decision"),
            status="COMPLETED",
            created_at=now,
            completed_at=now,
        )
    )


def _deterministic_recovery(
    session: Session,
    journey: Journey,
    mission: JourneyMission,
    trigger: Event,
    run: GuardianRun,
    evidence: dict[str, Any],
) -> AgentRun:
    """Offline controller retained as the explicit runtime-unavailable path."""

    agent = _new_agent_run(session, journey, mission)
    run.agent_run_id = agent.id
    gateway = AgentToolGateway(session)
    inspect_args = {
        "request_id": f"guardian-inspect-{run.id}",
        "agent_id": "guardian",
        "journey_id": UUID(journey.id),
        "mission_id": UUID(mission.id),
    }
    inspect = gateway.dispatch("inspect_twin", AgentToolCall(**inspect_args))
    _step(session, agent, 1, "inspect_twin", inspect_args, inspect)
    request_args = {
        "request_id": f"guardian-replan-{run.id}",
        "agent_id": "guardian",
        "journey_id": UUID(journey.id),
        "mission_id": UUID(mission.id),
        "trigger_event_id": UUID(trigger.id),
        "candidate_count": 3,
        "solver_mode": "v2",
    }
    try:
        generated = gateway.dispatch("request_replan", AgentToolCall(**request_args))
    except HTTPException as exc:
        generated = {"decision": "DENY", "error": str(exc.detail), "executed": False}
        _step(session, agent, 2, "request_replan", request_args, generated)
        run.action_status = "NO_FEASIBLE_RECOVERY"
        run.reasoning_summary = (
            f"Recovery was required, but no feasible proposal was produced: {exc.detail}"
        )
        agent.status = "COMPLETED"
        agent.outcome = run.reasoning_summary
        agent.completed_at = _now().isoformat()
        run.status = "COMPLETED"
        run.completed_at = agent.completed_at
        evidence["policy"] = {"decision": "DENY", "reason_codes": ["NO_FEASIBLE_RECOVERY"]}
        evidence["mutation"] = {"executed": False, "proposal_id": None}
        return agent
    _step(session, agent, 2, "request_replan", request_args, generated)
    proposal_id = str(generated["proposal"]["id"])
    proposal = session.get(ReplanProposal, proposal_id)
    run.proposal_id = proposal_id
    diff = json.loads(proposal.diff_json or "{}") if proposal else {}
    evidence["solver"] = diff.get("verified_evidence", {}).get("generator")
    resolved = json.loads(mission.resolved_intent_json or "{}")
    mission_cost_limit = int(resolved.get("max_cost_delta_minor", 0))
    mission_drift_limit = int(resolved.get("max_schedule_drift_min", 0))
    cost_delta = max(0, int(diff.get("cost_delta_minor") or 0))
    drift = abs(int(diff.get("end_time_delta_min") or 0))
    envelope_allows = (
        mission.requested_autonomy == "EXECUTE_WITHIN_BOUNDS"
        and cost_delta <= mission_cost_limit
        and drift <= mission_drift_limit
    )
    if envelope_allows:
        accept_args = {
            "request_id": f"guardian-accept-{run.id}",
            "agent_id": "guardian",
            "journey_id": UUID(journey.id),
            "mission_id": UUID(mission.id),
            "proposal_id": UUID(proposal_id),
        }
        accepted = gateway.dispatch("accept_replan", AgentToolCall(**accept_args))
        _step(session, agent, 3, "accept_replan", accept_args, accepted)
        evidence["policy"] = {
            "decision": accepted.get("decision"),
            "reason_codes": accepted.get("reason_codes", []),
            "policy_version": accepted.get("policy_version"),
        }
        evidence["mutation"] = {
            "executed": bool(accepted.get("executed")),
            "proposal_id": proposal_id,
        }
        run.action_status = (
            "EXECUTED"
            if accepted.get("executed")
            else accepted.get("decision", "REQUIRE_CONFIRMATION")
        )
    else:
        reason_codes = []
        if mission.requested_autonomy != "EXECUTE_WITHIN_BOUNDS":
            reason_codes.append("MISSION_PLAN_ONLY")
        if cost_delta > mission_cost_limit:
            reason_codes.append("MISSION_COST_LIMIT_EXCEEDED")
        if drift > mission_drift_limit:
            reason_codes.append("MISSION_SCHEDULE_DRIFT_EXCEEDED")
        evidence["policy"] = {"decision": "REQUIRE_CONFIRMATION", "reason_codes": reason_codes}
        evidence["mutation"] = {"executed": False, "proposal_id": proposal_id}
        run.action_status = "REQUIRE_CONFIRMATION"
    agent.status = "COMPLETED"
    agent.outcome = run.action_status
    agent.completed_at = _now().isoformat()
    return agent


def scan_guardian(session: Session, journey: Journey) -> dict[str, Any]:
    """Observe persisted journey facts and launch at most one recovery per incident state."""

    mission = _standing_mission(session, journey.id)
    events = _open_events(session, journey.id)
    actionable_events = [
        event
        for event in events
        if event.event_type in ACTIONABLE_EVENT_TYPES and not _is_predictive(event)
    ]
    fingerprint = _fingerprint(journey, mission, events)
    now = _now()
    existing = _cooldown_active(
        session,
        journey_id=journey.id,
        evidence_fingerprint=fingerprint,
        now=now,
    )
    if existing is not None:
        return guardian_payload(session, existing, deduplicated=True)
    # The semantic fingerprint is also a durable idempotency key.  Once an
    # equivalent incident has completed, a later refresh must not hit the
    # Guardian unique constraint or create another PlanVersion.
    existing = session.scalar(
        select(GuardianRun).where(
            GuardianRun.journey_id == journey.id,
            GuardianRun.evidence_fingerprint == fingerprint,
        )
    )
    if existing is not None:
        return guardian_payload(session, existing, deduplicated=True)

    actionable = actionable_events
    incident_fingerprints = []
    for event in events:
        try:
            incident_fingerprints.append(
                json.loads(event.payload_json or "{}").get("incident_fingerprint")
            )
        except json.JSONDecodeError:
            continue
    evidence = {
        "observation": {
            "journey_status": journey.status,
            "active_plan_version_id": journey.active_plan_version_id,
            "open_event_ids": [event.id for event in events],
            "actionable_event_ids": [event.id for event in actionable],
            "incident_fingerprints": [item for item in incident_fingerprints if item],
        },
        "inference": {},
        "solver": None,
        "policy": None,
        "mutation": None,
    }
    if not actionable:
        decision, action_status = "NO_ACTION", "NO_ACTION"
        summary = (
            "No unresolved weather, closure, traffic, or schedule disruption requires recovery."
        )
    elif mission is None:
        decision, action_status = "WATCH", "MISSION_REQUIRED"
        summary = "A disruption is present, but no standing Journey mission defines the autonomy envelope."
    else:
        decision, action_status = "RECOVERY_REQUIRED", "PLANNING"
        summary = "Guardian found an actionable disruption and launched one bounded recovery run."

    run = GuardianRun(
        id=str(uuid4()),
        journey_id=journey.id,
        standing_mission_id=mission.id if mission else None,
        trigger_event_id=None,
        agent_run_id=None,
        proposal_id=None,
        decision=decision,
        status="RUNNING" if decision == "RECOVERY_REQUIRED" else "COMPLETED",
        action_status=action_status,
        evidence_fingerprint=fingerprint,
        evidence_json=_json(evidence),
        reasoning_summary=summary,
        active_plan_before_id=journey.active_plan_version_id,
        active_plan_after_id=journey.active_plan_version_id,
        created_at=now.isoformat(),
        completed_at=None if decision == "RECOVERY_REQUIRED" else now.isoformat(),
        cooldown_until=(now + timedelta(seconds=settings.guardian_cooldown_seconds)).isoformat(),
    )
    session.add(run)
    session.flush()
    if decision != "RECOVERY_REQUIRED":
        session.commit()
        return guardian_payload(session, run)

    trigger = max(
        actionable,
        key=lambda item: (SEVERITY_ORDER.get(item.severity, 0), item.detected_at, item.id),
    )
    run.trigger_event_id = trigger.id
    evidence["inference"] = {
        "decision": "RECOVERY_REQUIRED",
        "selected_trigger_event_id": trigger.id,
        "reason": "highest-severity actionable unresolved event",
    }
    session.commit()
    proposal_id = None
    try:
        _recovery, agent, _model_result = _model_recovery(
            session,
            journey,
            mission,
            trigger,
            run,
            evidence,
        )
        proposal_id = run.proposal_id
        if agent.status not in {"COMPLETED", "PARTIAL"}:
            agent.status = "COMPLETED"
    except AgentRuntimeUnavailable as exc:
        evidence["inference"] = {
            **evidence.get("inference", {}),
            "controller": "deterministic_guardian_fallback",
            "runtime_unavailable": str(exc),
        }
        agent = _deterministic_recovery(session, journey, mission, trigger, run, evidence)
        proposal_id = run.proposal_id
    except Exception as exc:  # noqa: BLE001 - preserve a retryable Guardian state
        # The run row is committed before the model/tool boundary so another
        # watcher can observe the idempotency key.  If a provider, solver, or
        # SQLite contention failure occurs after that commit, never leave the
        # durable row stuck at PLANNING: mark this attempt retryable and move
        # its unique fingerprint aside so the next pulse can make a fresh,
        # auditable attempt for the same incident.
        session.rollback()
        failed = session.get(GuardianRun, run.id)
        if failed is not None:
            failed.evidence_fingerprint = f"{fingerprint}:failed:{failed.id}"
            failed.status = "FAILED_RETRYABLE"
            failed.action_status = "RETRYABLE_FAILURE"
            failed.reasoning_summary = f"Guardian recovery attempt failed and is retryable: {type(exc).__name__}"
            failed.completed_at = _now().isoformat()
            failed.evidence_json = _json(
                {
                    **evidence,
                    "inference": {
                        **evidence.get("inference", {}),
                        "controller": "guardian_retry_boundary",
                        "failure_type": type(exc).__name__,
                    },
                }
            )
            session.commit()
        raise

    session.refresh(journey)
    completed = _now().isoformat()
    run.active_plan_after_id = journey.active_plan_version_id
    run.evidence_json = _json(evidence)
    run.status = "COMPLETED"
    run.completed_at = completed
    agent.status = "COMPLETED"
    agent.outcome = run.action_status
    agent.completed_at = completed
    record_audit(
        session,
        action="guardian.scan.completed",
        entity_type="GuardianRun",
        entity_id=run.id,
        journey_id=journey.id,
        actor_type="AI_AGENT",
        actor_id="guardian",
        after={
            "decision": run.decision,
            "action_status": run.action_status,
            "proposal_id": proposal_id,
        },
    )
    session.commit()
    return guardian_payload(session, run)


def run_preemptive_guardian(
    session: Session,
    journey: Journey,
    snapshot: JourneyResilienceSnapshot,
) -> dict[str, Any]:
    """Use the standing mission to prevent a predicted weakness."""

    mission = _standing_mission(session, journey.id)
    events = [event for event in _open_events(session, journey.id) if _is_predictive(event)]
    trigger = max(
        events,
        key=lambda item: (SEVERITY_ORDER.get(item.severity, 0), item.detected_at, item.id),
        default=None,
    )
    if trigger is not None:
        trigger_payload = _load(trigger.payload_json, {})
        prior_run_id = trigger_payload.get("preventive_guardian_run_id")
        if prior_run_id:
            prior_run = session.get(GuardianRun, str(prior_run_id))
            if prior_run is not None:
                return guardian_payload(session, prior_run, deduplicated=True)
    fingerprint_material = {
        "journey_id": journey.id,
        "snapshot": snapshot.fingerprint,
        "mission_id": mission.id if mission else None,
        "trigger_id": trigger.id if trigger else None,
    }
    fingerprint = hashlib.sha256(_json(fingerprint_material).encode("utf-8")).hexdigest()
    existing = session.scalar(
        select(GuardianRun).where(
            GuardianRun.journey_id == journey.id,
            GuardianRun.evidence_fingerprint == fingerprint,
        )
    )
    if existing is not None:
        return guardian_payload(session, existing, deduplicated=True)
    from app.agent.predictive import snapshot_payload

    evidence = {
        "observation": {
            "trigger_kind": "PREDICTIVE",
            "predictive_event_id": trigger.id if trigger else None,
            "forecast_before_hard_failure": True,
        },
        "prediction": snapshot_payload(snapshot),
        "inference": {
            "decision": "PREEMPTIVE_RECOVERY",
            "reason": "Deterministic resilience score crossed the predictive threshold.",
            "semantic_judgement": "The model may interpret the city event; the server verifies post-action facts.",
        },
        "solver": None,
        "policy": None,
        "mutation": None,
    }
    now = _now()
    if trigger is None or mission is None:
        decision = "WATCH" if trigger is not None else "NO_ACTION"
        action_status = (
            "MISSION_REQUIRED" if trigger is not None and mission is None else "NO_ACTION"
        )
        run = GuardianRun(
            id=str(uuid4()),
            journey_id=journey.id,
            standing_mission_id=mission.id if mission else None,
            trigger_event_id=trigger.id if trigger else None,
            agent_run_id=None,
            proposal_id=None,
            decision=decision,
            status="COMPLETED",
            action_status=action_status,
            evidence_fingerprint=fingerprint,
            evidence_json=_json(evidence),
            reasoning_summary=(
                "Predictive weakness is visible, but a standing mission is required before autonomous action."
                if mission is None and trigger is not None
                else "No predictive trigger is currently open."
            ),
            active_plan_before_id=journey.active_plan_version_id,
            active_plan_after_id=journey.active_plan_version_id,
            trigger_kind="PREDICTIVE",
            resilience_snapshot_id=snapshot.id,
            created_at=now.isoformat(),
            completed_at=now.isoformat(),
            cooldown_until=(
                now + timedelta(seconds=settings.guardian_cooldown_seconds)
            ).isoformat(),
        )
        session.add(run)
        session.commit()
        return guardian_payload(session, run)

    run = GuardianRun(
        id=str(uuid4()),
        journey_id=journey.id,
        standing_mission_id=mission.id,
        trigger_event_id=trigger.id,
        agent_run_id=None,
        proposal_id=None,
        decision="PREEMPTIVE_RECOVERY",
        status="RUNNING",
        action_status="PLANNING",
        evidence_fingerprint=fingerprint,
        evidence_json=_json(evidence),
        reasoning_summary="CityNexus predicted a Journey weakness and started preventive recovery.",
        active_plan_before_id=journey.active_plan_version_id,
        active_plan_after_id=journey.active_plan_version_id,
        trigger_kind="PREDICTIVE",
        resilience_snapshot_id=snapshot.id,
        created_at=now.isoformat(),
        completed_at=None,
        cooldown_until=(now + timedelta(seconds=settings.guardian_cooldown_seconds)).isoformat(),
    )
    session.add(run)
    session.flush()
    evidence["inference"]["selected_trigger_event_id"] = trigger.id
    session.commit()
    try:
        _recovery, agent, _model_result = _model_recovery(
            session,
            journey,
            mission,
            trigger,
            run,
            evidence,
        )
    except AgentRuntimeUnavailable as exc:
        evidence["inference"] = {
            **evidence["inference"],
            "controller": "deterministic_guardian_fallback",
            "runtime_unavailable": str(exc),
        }
        agent = _deterministic_recovery(session, journey, mission, trigger, run, evidence)
    except Exception as exc:  # noqa: BLE001 - preserve a retryable predictive state
        session.rollback()
        failed = session.get(GuardianRun, run.id)
        if failed is not None:
            failed.evidence_fingerprint = f"{fingerprint}:failed:{failed.id}"
            failed.status = "FAILED_RETRYABLE"
            failed.action_status = "RETRYABLE_FAILURE"
            failed.reasoning_summary = f"Predictive recovery attempt failed and is retryable: {type(exc).__name__}"
            failed.completed_at = _now().isoformat()
            failed.evidence_json = _json(
                {
                    **evidence,
                    "inference": {
                        **evidence.get("inference", {}),
                        "controller": "predictive_retry_boundary",
                        "failure_type": type(exc).__name__,
                    },
                }
            )
            session.commit()
        raise
    session.refresh(journey)
    completed = _now().isoformat()
    run.active_plan_after_id = journey.active_plan_version_id
    run.evidence_json = _json(evidence)
    run.status = "COMPLETED"
    run.completed_at = completed
    agent.status = "COMPLETED"
    agent.outcome = run.action_status
    agent.completed_at = completed
    run.reasoning_summary = (
        "CityNexus predicted a Journey weakness and autonomously prevented the failure while preserving protected commitments."
        if run.action_status == "EXECUTED"
        else "CityNexus predicted a Journey weakness; policy or feasibility retained the action behind a gate."
    )
    if run.action_status == "EXECUTED" and trigger is not None:
        trigger_payload = _load(trigger.payload_json, {})
        trigger_payload["predictive_prevented"] = True
        trigger_payload["preventive_guardian_run_id"] = run.id
        trigger_payload["prevented_at"] = completed
        trigger.payload_json = _json(trigger_payload)
        trigger.status = "ACKNOWLEDGED"
    record_audit(
        session,
        action="guardian.predictive.completed",
        entity_type="GuardianRun",
        entity_id=run.id,
        journey_id=journey.id,
        actor_type="AI_AGENT",
        actor_id="guardian",
        after={
            "trigger_kind": run.trigger_kind,
            "action_status": run.action_status,
            "resilience_snapshot_id": snapshot.id,
            "proposal_id": run.proposal_id,
        },
    )
    session.commit()
    return guardian_payload(session, run)


def guardian_status(session: Session, journey: Journey) -> dict[str, Any]:
    mission = _standing_mission(session, journey.id)
    runs = list(
        session.scalars(
            select(GuardianRun)
            .where(GuardianRun.journey_id == journey.id)
            .order_by(GuardianRun.created_at.desc(), GuardianRun.id.desc())
            .limit(20)
        ).all()
    )
    return {
        "journey_id": journey.id,
        "standing_mission": mission_payload(mission) if mission else None,
        "current_active_plan_version_id": journey.active_plan_version_id,
        "latest": guardian_payload(session, runs[0]) if runs else None,
        "runs": [guardian_payload(session, item) for item in runs],
    }
