"""User-facing Autonomous Journey Operator projection over durable domain truth."""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent.operator_brain import (
    find_recovery_resume,
    operator_brain_state,
    resume_projection,
    runtime_candidates_for_session,
)
from app.core.config import settings
from app.db.models import (
    ActionApproval,
    AgentPendingAction,
    AgentRun,
    AutopilotEnrollment,
    AuditLog,
    Event,
    GuardianRun,
    Journey,
    JourneyInboxItem,
    RealityClaim,
    RealityConflict,
    TransportReservation,
    JourneyMission,
    ReplanProposal,
    RecoveryDiscoveryAttempt,
    RecoveryExecutionPlan,
    RecoveryEpoch,
)
from app.agent.transactional_operator import execution_plan_projection
from app.execution.continuous_journey import continuous_flow_projection
from app.execution.in_transit_reroute import in_transit_projection
from app.execution.progress_drift import progress_drift_projection
from app.agent.journey_ahead import journey_ahead_projection
from app.agent.overnight_steward import overnight_steward_projection
from app.agent.journey_intent import active_journey_intents, pending_journey_intent_clarifications
from app.execution.clock import execution_clock
from app.execution.group_presence import (
    group_presence_projection,
    group_recovery_incident_key,
    group_recovery_projection,
)
from app.execution.group_recovery_followthrough import followthrough_projection
from app.execution.member_side_quest import attach_member_side_quest_projection
from app.execution.member_place_invitation import invitation_projection
from app.execution.planned_split_meetup_handshake import meetup_handshake_projection
from app.operator_attention import attention_from_operator_projection


def classify_operator_state(
    *,
    enabled: bool,
    mission_active: bool,
    guardian_status: str | None = None,
    guardian_action_status: str | None = None,
    agent_status: str | None = None,
    pending_status: str | None = None,
    approval_status: str | None = None,
    material_event_open: bool = False,
    active_plan_changed: bool = False,
    degraded: bool = False,
) -> str:
    """Collapse existing durable states into one traveller-facing operator state."""

    if not enabled or not mission_active:
        return "OFF"
    if degraded:
        return "DEGRADED"
    if guardian_action_status == "OPERATOR_EXTERNAL_AGENT_BLOCKED":
        return "DEGRADED"
    if pending_status == "RESUMING":
        return "APPLYING"
    if approval_status == "REJECTED":
        return "NEEDS_ATTENTION"
    if (
        pending_status == "PENDING"
        and approval_status not in {"APPROVED", "CONSUMED"}
    ):
        return "WAITING_FOR_APPROVAL"
    if pending_status == "RESUMED" or (
        guardian_action_status == "EXECUTED" and active_plan_changed
    ):
        return "RECOVERED"
    if guardian_status == "RUNNING" or agent_status in {"RUNNING", "PLANNING"}:
        return "INVESTIGATING"
    if guardian_action_status in {"REQUIRE_CONFIRMATION", "REQUIRE_STEP_UP"}:
        return "WAITING_FOR_APPROVAL"
    if material_event_open:
        if guardian_action_status in {"NO_FEASIBLE_RECOVERY", "UNRESOLVED"}:
            return "NEEDS_ATTENTION"
        return "INVESTIGATING"
    if guardian_status in {"FAILED_RETRYABLE", "INTERRUPTED"} or agent_status in {
        "FAILED",
        "FAILED_EXTERNAL",
        "PARTIAL",
    }:
        return "DEGRADED"
    return "WATCHING"


def _json(value: str | None, fallback: Any) -> Any:
    try:
        return json.loads(value or "")
    except (TypeError, json.JSONDecodeError):
        return fallback


def _mission_is_within_recovery_lineage(
    session: Session,
    mission_id: str | None,
    recovery_mission_id: str,
) -> bool:
    """Keep a collective room attached only to its originating incident."""
    current_id = str(mission_id or "")
    seen: set[str] = set()
    for _ in range(12):
        if not current_id or current_id in seen:
            return False
        if current_id == str(recovery_mission_id):
            return True
        seen.add(current_id)
        row = session.get(JourneyMission, current_id)
        if row is None:
            return False
        current_id = str(row.parent_mission_id or "")
    return False


def attach_collective_recovery_projection(
    session: Session,
    group_recovery: dict[str, Any],
    collective: dict[str, Any],
    recovery_mission_id: str,
) -> bool:
    """Attach a compact room summary when it belongs to this same incident."""
    collective_mission_id = str(collective.get("mission_id") or "")
    if not _mission_is_within_recovery_lineage(
        session, collective_mission_id, recovery_mission_id
    ):
        return False
    collective_state = str(collective.get("state") or "")
    group_recovery["collective_decision"] = {
        "mission_id": collective_mission_id,
        "state": collective_state,
        "required_count": int(collective.get("required_count") or 0),
        "response_count": int(collective.get("response_count") or 0),
        "waiting_for_member_count": int(
            collective.get("waiting_for_member_count") or 0
        ),
        "viewer_has_responded": bool(collective.get("viewer_has_responded")),
        "viewer_can_apply": bool(collective.get("viewer_can_apply")),
        "authority_receipt_id": collective.get("authority_receipt_id"),
        "selected_proposal_id": collective.get("selected_proposal_id"),
        "selected_plan_version_id": collective.get("selected_plan_version_id"),
        "selected_option_label": collective.get("selected_option_label"),
        "reconsent_required": bool(collective.get("reconsent_required")),
        "proposal_ids": collective.get("proposal_ids") or [],
        "decision_epoch": collective.get("decision_epoch"),
    }
    state_map = {
        "WAITING_FOR_MEMBERS": "COLLECTIVE_WAITING_FOR_MEMBERS",
        "MEDIATING": "COLLECTIVE_MEDIATING",
        "COLLECTIVE_AUTHORIZED": "COLLECTIVE_AUTHORIZED",
        "COLLECTIVE_STALE": "COLLECTIVE_STALE",
        "COLLECTIVE_READY": "COLLECTIVE_DECISION_REQUIRED",
        "COLLECTIVE_NEEDS_ATTENTION": "COLLECTIVE_DECISION_REQUIRED",
    }
    group_state = state_map.get(collective_state)
    if group_state:
        group_recovery["state"] = group_state
        group_recovery["can_request_planning"] = False
        group_recovery["headline"] = {
            "COLLECTIVE_WAITING_FOR_MEMBERS": "A shared recovery choice is waiting for the travellers it affects.",
            "COLLECTIVE_MEDIATING": "The travellers chose differently; CityNexus is preserving that disagreement while seeking a fair next step.",
            "COLLECTIVE_AUTHORIZED": "The Journey has not changed. The group agreed on this exact recovery; a traveller with Journey editing authority can apply it.",
            "COLLECTIVE_STALE": "The trip or its evidence changed; the old group choices can no longer be used.",
            "COLLECTIVE_DECISION_REQUIRED": "The group response is recorded, but current collective authority is not available to carry this proposal forward.",
        }[group_state]
    return True


def _timeline(session: Session, journey_id: str) -> list[dict[str, Any]]:
    """Turn durable audits into a compact traveller-facing story."""

    rows = session.scalars(
        select(AuditLog)
        .where(AuditLog.journey_id == journey_id)
        .order_by(AuditLog.created_at, AuditLog.id)
    ).all()
    items: list[dict[str, Any]] = []
    for row in rows:
        after = _json(row.after_json, {})
        if not isinstance(after, dict):
            after = {}
        label: str | None = None
        if row.action == "operator.started":
            label = "Watching your trip"
        elif row.action == "operator.stopped":
            label = "Journey Operator paused"
        elif row.action == "city.signal.incident.created":
            label = f"Detected: {str(after.get('event_type') or 'a trip change').replace('_', ' ').title()}"
        elif row.action in {"guardian.mission.compiled", "guardian.scan.started"}:
            label = "Working on a recovery"
        elif row.action == "agent.action_approval.created":
            label = "Waiting for your approval"
        elif row.action == "agent.action_approval.approved":
            label = "Approved"
        elif row.action == "replan.accepted":
            label = "Plan updated"
        elif row.action == "agent.pending_action.resumed":
            label = "Watching your trip again"
        elif row.action == "operator.external_agent_blocked":
            label = "CityNexus could not reach its trip Agent"
        elif row.action == "agent.recovery_discovery.started":
            label = "I’m looking for more recovery options nearby"
        elif row.action == "agent.recovery_discovery.completed":
            label = (
                "I found new places to check"
                if int(after.get("raw_candidate_count") or 0) > 0
                else "I couldn’t find another grounded option"
            )
        if label is None:
            continue
        items.append({"at": row.created_at, "label": label, "kind": row.action})
    return items[-12:]


def operator_projection(
    session: Session,
    journey: Journey,
    *,
    viewer_member_id: str | None = None,
) -> dict[str, Any]:
    """Join Guardian/Agent/approval truth without creating a second control plane."""

    enrollment = session.get(AutopilotEnrollment, journey.id)
    standing = session.scalar(
        select(JourneyMission)
        .where(
            JourneyMission.journey_id == journey.id,
            JourneyMission.is_standing.is_(True),
            JourneyMission.status == "ACTIVE",
        )
        .order_by(JourneyMission.created_at.desc(), JourneyMission.id.desc())
    )
    guardian = session.scalar(
        select(GuardianRun)
        .where(GuardianRun.journey_id == journey.id)
        .order_by(GuardianRun.created_at.desc(), GuardianRun.id.desc())
    )
    agent = (
        session.get(AgentRun, guardian.agent_run_id)
        if guardian is not None and guardian.agent_run_id
        else None
    )
    pending = (
        session.scalar(
            select(AgentPendingAction)
            .where(
                AgentPendingAction.journey_id == journey.id,
                AgentPendingAction.run_id == agent.id,
                AgentPendingAction.status.in_(
                    ("PENDING", "RESUMING", "RESUMED", "FAILED", "SUPERSEDED")
                ),
            )
            .order_by(AgentPendingAction.updated_at.desc(), AgentPendingAction.id.desc())
        )
        if agent is not None
        else None
    )
    approval = (
        session.get(ActionApproval, pending.approval_id)
        if pending is not None
        else None
    )
    proposal = (
        session.get(ReplanProposal, guardian.proposal_id)
        if guardian is not None and guardian.proposal_id
        else None
    )
    open_event = None
    for candidate in session.scalars(
        select(Event)
        .where(
            Event.journey_id == journey.id,
            Event.status.in_(("DETECTED", "ACKNOWLEDGED")),
            Event.event_type.in_(("WEATHER", "CLOSURE", "TRAFFIC_DELAY", "SCHEDULE_DELAY")),
        )
        .order_by(Event.detected_at.desc(), Event.id.desc())
    ).all():
        try:
            if bool(json.loads(candidate.payload_json or "{}").get("predictive_only")):
                continue
        except (TypeError, json.JSONDecodeError):
            pass
        open_event = candidate
        break

    enabled = bool(enrollment is not None and enrollment.enabled)
    mission_active = standing is not None
    active_plan_changed = bool(
        guardian is not None
        and guardian.active_plan_before_id
        and journey.active_plan_version_id
        and str(guardian.active_plan_before_id) != str(journey.active_plan_version_id)
    )
    degraded = bool(enrollment is not None and enrollment.last_error)

    # A continuation is a short-lived recovered transition. Once the next
    # Guardian pulse is recorded, the same Journey is back in WATCHING.
    resumed_audit = session.scalar(
        select(AuditLog)
        .where(
            AuditLog.journey_id == journey.id,
            AuditLog.action == "agent.pending_action.resumed",
        )
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
    )
    recovered_transition = bool(
        resumed_audit is not None
        and (
            enrollment is None
            or not enrollment.last_pulse_at
            or str(enrollment.last_pulse_at) <= str(resumed_audit.created_at)
        )
    )
    projected_guardian_action = guardian.action_status if guardian else None
    projected_pending_status = pending.status if pending else None
    if not recovered_transition and projected_pending_status == "RESUMED":
        projected_pending_status = None
    if not recovered_transition and projected_guardian_action == "EXECUTED":
        projected_guardian_action = None

    state = classify_operator_state(
        enabled=enabled,
        mission_active=mission_active,
        guardian_status=guardian.status if guardian else None,
        guardian_action_status=projected_guardian_action,
        agent_status=agent.status if agent else None,
        pending_status=projected_pending_status,
        approval_status=approval.status if approval else None,
        material_event_open=open_event is not None,
        active_plan_changed=active_plan_changed,
        degraded=degraded,
    )

    runtime_candidates = runtime_candidates_for_session(session, settings)
    recovery_epoch_id = (
        guardian.recovery_epoch_id
        if guardian is not None and guardian.recovery_epoch_id
        else session.scalar(
            select(RecoveryEpoch.id)
            .where(
                RecoveryEpoch.journey_id == journey.id,
                RecoveryEpoch.status == "OPEN",
            )
            .order_by(RecoveryEpoch.generation.desc())
        )
    )
    resume = find_recovery_resume(
        session, journey_id=journey.id, recovery_epoch_id=recovery_epoch_id
    )
    brain = operator_brain_state(
        configured_candidates=runtime_candidates,
        selected_runtime=(
            resume.provider_key
            if resume is not None
            and any(
                candidate.key == str(resume.provider_key)
                and candidate.configured
                for candidate in runtime_candidates
            )
            else None
        ),
        active_agent_status=agent.status if agent else None,
        blocked_recovery_pending=bool(
            resume is not None and resume.status in {"PENDING", "RUNNING"}
        ),
        last_failure_reason=(resume.failure_classification if resume is not None else None),
        safe_to_resume=bool(resume is not None and resume.safe_to_resume),
    )
    brain["recovery_resume"] = resume_projection(resume)

    discovery_rows = (
        session.scalars(
            select(RecoveryDiscoveryAttempt)
            .where(
                RecoveryDiscoveryAttempt.journey_id == journey.id,
                RecoveryDiscoveryAttempt.recovery_epoch_id == recovery_epoch_id,
            )
            .order_by(RecoveryDiscoveryAttempt.created_at, RecoveryDiscoveryAttempt.id)
        ).all()
        if recovery_epoch_id
        else []
    )
    latest_discovery = discovery_rows[-1] if discovery_rows else None
    discovery_state = "IDLE"
    if latest_discovery is not None:
        if latest_discovery.status == "DEGRADED":
            discovery_state = "DISCOVERY_DEGRADED"
        elif len(discovery_rows) >= 2 and latest_discovery.raw_candidate_count == 0:
            discovery_state = "DISCOVERY_EXHAUSTED"
        else:
            discovery_state = "DISCOVERY_IN_PROGRESS"
    discovery = {
        "state": discovery_state,
        "attempt_count": len(discovery_rows),
        "max_attempts": 2,
        "raw_candidate_count": latest_discovery.raw_candidate_count if latest_discovery else 0,
        "materialized_place_count": (
            len(_json(latest_discovery.materialized_place_ids_json, []))
            if latest_discovery is not None
            else 0
        ),
        "provider_mode": latest_discovery.provider_mode if latest_discovery else None,
        "runtime_provenance": latest_discovery.runtime_provenance if latest_discovery else None,
        "query": latest_discovery.query if latest_discovery else None,
    }

    execution_plan = session.scalar(
        select(RecoveryExecutionPlan)
        .where(RecoveryExecutionPlan.journey_id == journey.id)
        .order_by(RecoveryExecutionPlan.updated_at.desc(), RecoveryExecutionPlan.id.desc())
    )
    operations = None
    if execution_plan is not None:
        raw_operations = execution_plan_projection(session, execution_plan)
        actions = raw_operations.get("actions") or []
        done = sum(1 for item in actions if item.get("status") in {"CONFIRMED", "CANCELLED", "SUCCEEDED"})
        operations = {
            "plan_id": raw_operations["id"],
            "status": raw_operations["status"],
            "planned_action_count": len(actions),
            "completed_action_count": done,
            "approval_id": raw_operations.get("approval_id"),
            "provider_label": next((item.get("provider_label") for item in actions if item.get("provider_label")), None),
            "actions": [
                {
                    "sequence_no": item["sequence_no"],
                    "kind": item["kind"],
                    "status": item["status"],
                    "provider_label": item.get("provider_label"),
                    "target_type": item["target_type"],
                }
                for item in actions
            ],
        }
        # A completed frozen saga is the stronger operational truth.  A
        # planning runtime may still be unavailable, but once all commerce
        # actions are terminal the Operator is safe to return to monitoring.
        if enabled and operations["status"] == "COMPLETED":
            state = "WATCHING"
        elif operations["status"] in {"PARTIALLY_APPLIED", "FAILED_SAFE", "NEEDS_ATTENTION"}:
            state = "NEEDS_ATTENTION"

    from app.collective_operator import collective_operator_projection

    collective = collective_operator_projection(
        session,
        journey,
        viewer_member_id=viewer_member_id,
    )
    inbox_rows = session.scalars(
        select(JourneyInboxItem).where(JourneyInboxItem.journey_id == journey.id)
        .order_by(JourneyInboxItem.created_at.desc()).limit(10)
    ).all()
    inbox_latest = None
    if inbox_rows:
        inbox_result = json.loads(inbox_rows[0].result_json or "{}")
        inbox_latest = {"id": inbox_rows[0].id, "kind": inbox_rows[0].semantic_kind,
                    "headline": ("Your train time changed" if inbox_rows[0].semantic_kind == "SCHEDULE_CHANGE" else "Booking update needs a detail"),
                    "status": inbox_rows[0].status, "impact": inbox_rows[0].impact_status,
                    "provenance": inbox_rows[0].provenance, "candidates": inbox_result.get("candidates", [])}
    inbox = {
        "latest": inbox_latest,
        "pending_count": sum(1 for item in inbox_rows if item.status in {"BINDING_REQUIRED", "NEEDS_REVIEW"}),
    }
    reality_conflict_row = session.scalar(select(RealityConflict).where(
        RealityConflict.journey_id == journey.id,
        RealityConflict.status == "UNRESOLVED_MATERIAL_CONFLICT",
    ).order_by(RealityConflict.generation.desc(), RealityConflict.created_at.desc()))
    reality_conflict = None
    if reality_conflict_row is not None:
        claim_ids = json.loads(reality_conflict_row.claim_ids_json or "[]")
        claims = session.scalars(select(RealityClaim).where(RealityClaim.id.in_(claim_ids))).all() if claim_ids else []
        claims.sort(key=lambda item: (0 if item.provenance in {"USER_PASTED_TEXT", "USER_REPORTED"} else 1, item.observed_at))
        choices = []
        labels = []
        for claim in claims:
            value = json.loads(claim.value_json)
            stamp = str(value).replace("T", " ")[:16]
            clock = stamp[-5:]
            if claim.provenance == "USER_PASTED_TEXT":
                label = f"Use {clock}"
                source_label = f"Your forwarded update: {clock}"
            else:
                label = f"Keep {clock}"
                source_label = f"Provider replay: {clock}"
            choices.append({"claim_id": claim.id, "label": label})
            labels.append(source_label)
        reservation_id = reality_conflict_row.fact_key.split(":")[1]
        reservation = session.get(TransportReservation, reservation_id)
        reference = reservation.confirmation_ref if reservation is not None else "train"
        reality_conflict = {
            "id": reality_conflict_row.id, "generation": reality_conflict_row.generation,
            "status": reality_conflict_row.status, "fact_domain": reality_conflict_row.fact_domain,
            "fact_key": reality_conflict_row.fact_key,
            "title": "I have two different departure times for the same train",
            "body": f"I have conflicting departure times for train {reference}. " + ". ".join(labels) + ". I’m holding off on replanning until you choose.",
            "choices": choices,
        }
    timeline = [*_timeline(session, journey.id), *(collective.get("timeline") or [])]
    progress = progress_drift_projection(session, journey)
    drift_event_rows = session.scalars(
        select(Event).where(
            Event.journey_id == journey.id,
            Event.event_type == "SCHEDULE_DELAY",
            Event.provenance == "SYSTEM_DERIVED",
        ).order_by(Event.detected_at.desc(), Event.id.desc()).limit(1)
    ).all()
    if drift_event_rows:
        try:
            drift_event_payload = _json(drift_event_rows[0].payload_json, {})
            if drift_event_payload.get("execution_drift"):
                timeline.append({
                    "at": drift_event_rows[0].occurred_at,
                    "kind": "EXECUTION_DRIFT",
                    "label": f"Activity finished {abs(int((drift_event_payload.get('execution_drift') or {}).get('drift_min') or 0))} min later than planned",
                })
        except (TypeError, ValueError):
            pass
    timeline.extend({"at": item.received_at, "kind": "RESERVATION_UPDATE",
                     "label": ("Reservation schedule updated" if item.status == "APPLIED" else "Booking update needs a detail")}
                    for item in inbox_rows[:5])
    timeline.sort(key=lambda item: (str(item.get("at") or ""), str(item.get("kind") or "")))

    unresolved_collective = str(collective.get("state") or "") in {
        "MEDIATING", "COLLECTIVE_STALE", "NEEDS_ATTENTION"
    }
    unresolved_operations = bool(
        operations is not None
        and operations.get("status") in {"PARTIALLY_APPLIED", "FAILED_SAFE", "NEEDS_ATTENTION"}
    )
    activity_incident = bool(
        open_event is not None
        and journey.current_activity_id is not None
        and str(open_event.affected_activity_id or "") == str(journey.current_activity_id)
    )
    flow = continuous_flow_projection(
        session,
        journey,
        viewer_member_id=viewer_member_id,
        operator_needs_attention=bool(
            state == "NEEDS_ATTENTION"
            or unresolved_collective
            or unresolved_operations
            or (activity_incident and state in {"INVESTIGATING", "DEGRADED"})
        ),
    )
    transit = in_transit_projection(session, journey)
    group_presence = group_presence_projection(
        session, journey, viewer_member_id=viewer_member_id
    )
    member_side_quest = attach_member_side_quest_projection(
        session,
        journey,
        viewer_member_id=viewer_member_id,
        group_presence=group_presence,
    )
    member_place_invitations = invitation_projection(
        session,
        journey,
        viewer_member_id=viewer_member_id,
    )
    group_presence["meetup_handshake"] = meetup_handshake_projection(
        session,
        journey,
        viewer_member_id=viewer_member_id,
        group_presence=group_presence,
    )
    group_recovery = group_recovery_projection(group_presence)
    affected_member = group_recovery.get("affected_member")
    shared_commitment = group_recovery.get("shared_commitment")
    if (
        group_recovery.get("can_request_planning")
        and isinstance(affected_member, dict)
        and isinstance(shared_commitment, dict)
        and journey.active_plan_version_id
    ):
        incident_key = group_recovery_incident_key(
            journey_id=journey.id,
            active_plan_version_id=str(journey.active_plan_version_id),
            affected_member_id=str(affected_member.get("member_id") or ""),
            rendezvous_activity_id=str(shared_commitment.get("activity_id") or ""),
        )
        r61_mission_found = False
        for candidate in session.scalars(
            select(JourneyMission)
            .where(JourneyMission.journey_id == journey.id, JourneyMission.is_standing.is_(False))
            .order_by(JourneyMission.created_at.desc(), JourneyMission.id.desc())
            .limit(30)
        ).all():
            try:
                resolved = json.loads(candidate.resolved_intent_json or "{}")
            except (TypeError, json.JSONDecodeError):
                continue
            context = resolved.get("group_recovery") if isinstance(resolved, dict) else None
            if not isinstance(context, dict) or context.get("incident_key") != incident_key:
                continue
            if context.get("kind") == "R61_REGROUNDED_COLLECTIVE_RECOVERY":
                r61_mission_found = True
                clarification = _json(candidate.clarification_json, {})
                bound_epoch_id = str(context.get("recovery_epoch_id") or "")
                current_epoch = session.scalars(
                    select(RecoveryEpoch)
                    .where(
                        RecoveryEpoch.journey_id == journey.id,
                        RecoveryEpoch.status == "OPEN",
                    )
                    .order_by(RecoveryEpoch.generation.desc(), RecoveryEpoch.id.desc())
                ).first()
                epoch_is_current = bool(
                    current_epoch is not None and str(current_epoch.id) == bound_epoch_id
                )
                reground_state = str(
                    clarification.get("state")
                    or (
                        "RECOVERY_REGROUND_EXTERNAL_AGENT_BLOCKED"
                        if candidate.status == "FAILED_EXTERNAL"
                        else "RECOVERY_REGROUNDING"
                        if candidate.status in {"CREATED", "RUNNING", "WAITING_FOR_APPROVAL", "WAITING_FOR_MEMBERS"}
                        else "RECOVERY_REGROUNDED_NO_ACTION"
                    )
                )
                if not epoch_is_current:
                    reground_state = "RECOVERY_REPLAN_REQUIRED"
                state_headlines = {
                    "RECOVERY_REPLAN_REQUIRED": "The previous group choice is preserved as history; the trip changed again and needs current re-grounding.",
                    "RECOVERY_REGROUNDING": "CityNexus is checking a new shared recovery against the current trip. The Journey has not changed.",
                    "RECOVERY_REGROUNDED_NO_ACTION": "The current Journey remains the safest shared choice; no plan was changed.",
                    "RECOVERY_REGROUNDED_DOMINANT": "A current grounded shared option is ready; the Journey has not changed.",
                    "RECOVERY_REGROUNDED_DECISION_REQUIRED": "The situation changed and a fresh group decision is ready; earlier answers did not carry over.",
                    "RECOVERY_REGROUND_EXTERNAL_AGENT_BLOCKED": "The configured Agent could not re-check the changed trip; the old choice is history and the Journey remains unchanged.",
                    "RECOVERY_REGROUND_NO_FEASIBLE_OPTION": "No safe shared adjustment is available right now. The current Journey has not changed.",
                }
                group_recovery.update(
                    {
                        "state": reground_state,
                        "headline": state_headlines.get(reground_state, state_headlines["RECOVERY_REGROUNDING"]),
                        "can_request_planning": False,
                        "incident_key": incident_key,
                        "failed_proposal_id": context.get("failed_proposal_id"),
                        "cause_class": context.get("cause_class"),
                        "old_decision": {
                            "clarification_id": context.get("failed_clarification_id"),
                            "authority_receipt_id": context.get("failed_authority_receipt_id"),
                            "selected_proposal_id": context.get("failed_proposal_id"),
                        },
                        "current_recovery_epoch_id": bound_epoch_id,
                        "reground_result": {
                            "state": reground_state,
                            "collective_classification": clarification.get("collective_classification") if epoch_is_current else None,
                            "current_proposal_ids": (clarification.get("current_proposal_ids") or []) if epoch_is_current else [],
                            "grounded_feasible_proposal_ids": (clarification.get("grounded_feasible_proposal_ids") or []) if epoch_is_current else [],
                            "current_options": (clarification.get("current_options") or []) if epoch_is_current else [],
                            "plan_unchanged": bool(epoch_is_current and clarification.get("plan_unchanged") is True),
                        },
                        "reground_mission": {
                            "id": candidate.id,
                            "status": candidate.status,
                            "recovery_epoch_generation": context.get("recovery_epoch_generation"),
                        },
                        "mission": {
                            "mission_id": candidate.id,
                            "status": candidate.status,
                            "failure_reason": candidate.failure_reason,
                            "created_at": candidate.created_at,
                            "run_id": context.get("hermes_run_id"),
                        },
                    }
                )
                if str(collective.get("mission_id") or "") != str(
                    context.get("failed_decision_mission_id") or ""
                ):
                    attach_collective_recovery_projection(
                        session, group_recovery, collective, candidate.id
                    )
                break
            group_recovery["mission"] = {
                "mission_id": candidate.id,
                "status": candidate.status,
                "failure_reason": candidate.failure_reason,
                "created_at": candidate.created_at,
                "run_id": context.get("hermes_run_id"),
            }
            group_recovery["can_request_planning"] = False
            if candidate.status == "FAILED_EXTERNAL":
                group_recovery["state"] = "RECOVERY_EXTERNAL_AGENT_BLOCKED"
                group_recovery["headline"] = "The configured Agent could not start; the current plan remains unchanged."
                group_recovery["can_request_planning"] = False
            elif candidate.status in {"RUNNING", "WAITING_FOR_APPROVAL", "WAITING_FOR_MEMBERS"}:
                group_recovery["state"] = "RECOVERY_OPTIONS_READY"
                group_recovery["headline"] = "CityNexus is preparing a governed group-recovery option."
            attach_collective_recovery_projection(
                session, group_recovery, collective, candidate.id
            )
            break
        if not r61_mission_found:
            for audit in session.scalars(
                select(AuditLog)
                .where(
                    AuditLog.journey_id == journey.id,
                    AuditLog.action == "journey.group_recovery.replan_required",
                )
                .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
            ).all():
                transition = _json(audit.after_json, {})
                if not isinstance(transition, dict) or transition.get("incident_key") != incident_key:
                    continue
                if str(transition.get("old_active_plan_version_id") or journey.active_plan_version_id) != str(journey.active_plan_version_id):
                    continue
                group_recovery.update(
                    {
                        "state": "RECOVERY_REPLAN_REQUIRED",
                        "headline": "The earlier group choice is preserved as history, but CityNexus cannot safely continue it under the current trip state.",
                        "can_request_planning": False,
                        "incident_key": incident_key,
                        "failed_proposal_id": transition.get("failed_proposal_id"),
                        "cause_class": transition.get("cause_class"),
                        "old_decision": {
                            "clarification_id": transition.get("old_clarification_id"),
                            "authority_receipt_id": transition.get("old_authority_receipt_id"),
                            "selected_proposal_id": transition.get("failed_proposal_id"),
                        },
                        "current_recovery_epoch_id": transition.get("current_recovery_epoch_id"),
                        "reground_mission": None,
                    }
                )
                break
    # An applied group recovery is a fact only when the authenticated spend
    # audit names the currently active plan. A receipt on its own is never a
    # terminal projection, and a later plan transition naturally ages this
    # card out because the active id no longer matches.
    for accepted in session.scalars(
        select(AuditLog)
        .where(
            AuditLog.journey_id == journey.id,
            AuditLog.action == "replan.accepted",
        )
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
    ).all():
        accepted_after = _json(accepted.after_json, {})
        spend = accepted_after.get("group_recovery_spend") if isinstance(accepted_after, dict) else None
        if not (
            isinstance(spend, dict)
            and spend.get("kind") == "COLLECTIVE_GROUP_RECOVERY_APPLY"
            and spend.get("result") == "APPLIED"
            and str(spend.get("after_plan_version_id") or "")
            == str(journey.active_plan_version_id or "")
        ):
            continue
        spender_id = str(spend.get("journey_member_id") or "")
        group_recovery.update(
            {
                "state": "RECOVERY_APPLIED",
                "headline": "The shared recovery was applied. Everyone is now following the updated Journey.",
                "can_request_planning": False,
                "mission": {
                    "mission_id": spend.get("root_mission_id"),
                    "status": "COMPLETED",
                    "failure_reason": None,
                    "run_id": None,
                },
                "collective_decision": {
                    "mission_id": spend.get("decision_mission_id"),
                    "state": "RECOVERY_APPLIED",
                    "required_count": int(collective.get("required_count") or 0),
                    "response_count": int(collective.get("response_count") or 0),
                    "waiting_for_member_count": 0,
                    "viewer_has_responded": bool(collective.get("viewer_has_responded")),
                    "viewer_can_apply": False,
                    "authority_receipt_id": spend.get("authority_receipt_id"),
                    "selected_proposal_id": spend.get("selected_proposal_id"),
                    "selected_plan_version_id": spend.get("after_plan_version_id"),
                    "decision_epoch": spend.get("decision_epoch"),
                },
                "applied": {
                    "selected_proposal_id": spend.get("selected_proposal_id"),
                    "before_plan_version_id": spend.get("before_plan_version_id"),
                    "after_plan_version_id": spend.get("after_plan_version_id"),
                    "authority_receipt_id": spend.get("authority_receipt_id"),
                    "decision_epoch": spend.get("decision_epoch"),
                    "material_world_epoch": spend.get("material_world_epoch"),
                    "recovery_epoch_id": spend.get("recovery_epoch_id"),
                    "recovery_epoch_generation": spend.get("recovery_epoch_generation"),
                    "applied_at": spend.get("applied_at"),
                    "applied_by_member_id": spender_id if spender_id == str(viewer_member_id or "") else None,
                    "applied_by_role": spend.get("member_role"),
                },
            }
        )
        break
    # The shared recovery remains a live user-facing workflow until each
    # required in-force traveller explicitly confirms their own arrival at the
    # server-derived rendezvous.  GET only projects those durable receipts; it
    # never marks a member arrived or closes the incident.
    if viewer_member_id:
        followthrough = followthrough_projection(
            session, journey, viewer_member_id=viewer_member_id
        )
        if followthrough is not None:
            closed = followthrough.get("state") == "RECOVERY_CLOSED"
            group_recovery.update(
                {
                    "state": followthrough["state"],
                    "headline": followthrough["headline"],
                    "can_request_planning": False,
                    "followthrough": followthrough,
                    # Closure remains durable history, but the recovery room no
                    # longer owns the foreground after the group has rejoined.
                    # Normal Journey execution becomes primary again.
                    "foreground": not closed,
                }
            )
    # Journey Ahead is a read-only projection.  Reuse the already persisted
    # predictive Guardian/brain state, but never start a provider call or an
    # action from this GET path.
    prevention_status = None
    if guardian is not None and str(guardian.trigger_kind or "").upper() == "PREDICTIVE":
        predictive_action = str(guardian.action_status or "").upper()
        prevention_status = {
            "PLANNING": "PLANNING",
            "RUNNING": "PLANNING",
            "DISCOVERING": "DISCOVERING",
            "REQUIRE_CONFIRMATION": "WAITING_FOR_APPROVAL",
            "REQUIRE_STEP_UP": "WAITING_FOR_APPROVAL",
            "EXECUTED": "APPLIED",
        }.get(predictive_action)
    resume_projection_state = brain.get("recovery_resume") or {}
    ahead = journey_ahead_projection(
        session,
        journey,
        prevention_status=prevention_status,
        provider_degraded=degraded,
        brain_blocked=bool(
            resume_projection_state.get("pending")
            and not resume_projection_state.get("safe_to_resume")
        ),
        execution_drift=progress,
    )
    viewer_traveler_id = journey.traveler_id
    if viewer_member_id:
        from app.db.models import JourneyMember

        viewer_member = session.get(JourneyMember, str(viewer_member_id))
        if viewer_member is not None and viewer_member.journey_id == journey.id:
            viewer_traveler_id = viewer_member.traveler_id
    overnight = overnight_steward_projection(
        session, journey, viewer_traveler_id=viewer_traveler_id
    )
    active_intents = active_journey_intents(
        session,
        journey,
        now=execution_clock.now(journey.timezone),
        traveler_id=viewer_traveler_id,
    )
    pending_intents = pending_journey_intent_clarifications(
        session, journey, traveler_id=viewer_traveler_id
    )
    projected_intents = sorted(
        [*active_intents, *pending_intents],
        key=lambda item: (str(item.get("created_at") or ""), str(item.get("id") or "")),
    )

    proposal_diff = _json(proposal.diff_json, {}) if proposal is not None else {}
    approval_payload = None
    if approval is not None and pending is not None and pending.status in {"PENDING", "RESUMING"}:
        approval_payload = {
            "required": True,
            "approval_id": approval.id,
            "status": approval.status,
            "action": approval.action,
            "target_type": approval.target_type,
            "target_id": approval.target_id,
            "pending_action_id": pending.id,
            "action_label": "Approve change",
        }
    if approval_payload is None and execution_plan is not None and execution_plan.approval_id:
        execution_approval = session.get(ActionApproval, execution_plan.approval_id)
        if execution_approval is not None and execution_approval.status in {"PENDING", "APPROVED"}:
            approval_payload = {
                "required": True,
                "approval_id": execution_approval.id,
                "status": execution_approval.status,
                "action": execution_approval.action,
                "target_type": execution_approval.target_type,
                "target_id": execution_approval.target_id,
                "pending_action_id": None,
                "action_label": "Approve reservation work",
            }

    incident = (
        {
            "id": open_event.id,
            "kind": open_event.event_type,
            "severity": open_event.severity,
            "activity_id": open_event.affected_activity_id,
            "provenance": open_event.provenance,
        }
        if open_event is not None
        else None
    )
    decision = (
        {
            "proposal_id": proposal.id,
            "status": proposal.status,
            "summary": proposal_diff.get("summary")
            or proposal_diff.get("explanation")
            or (guardian.reasoning_summary if guardian else None),
            "cost_delta_minor": proposal_diff.get("cost_delta_minor"),
            "schedule_delta_min": proposal_diff.get("end_time_delta_min"),
            "changes": {
                "removed": proposal_diff.get("removed", []),
                "added": proposal_diff.get("added", []),
                "moved": proposal_diff.get("moved", []),
            },
            "protected_commitments_preserved": bool(
                proposal.locked_commitments_preserved
            ),
        }
        if proposal is not None
        else None
    )

    headlines = {
        "OFF": "Journey Operator is off.",
        "WATCHING": "I’m watching your trip.",
        "INVESTIGATING": "I noticed a change and I’m working out what to do.",
        "WAITING_FOR_APPROVAL": "I found a recovery and need your approval.",
        "APPLYING": "Applying the change you approved.",
        "RECOVERED": "Change applied. I’m watching the trip again.",
        "NEEDS_ATTENTION": "I found a problem but could not safely resolve it.",
        "DEGRADED": "Journey Operator needs attention.",
    }

    return {
        "journey_id": journey.id,
        "state": state,
        "headline": headlines[state],
        "enabled": enabled,
        "standing_mission_id": standing.id if standing else None,
        "autonomy": standing.requested_autonomy if standing else None,
        "incident": incident,
        "decision": decision,
        "approval": approval_payload,
        "brain": brain,
        "discovery": discovery,
        "operations": operations,
        "flow": flow,
        "transit": transit,
        "group_presence": group_presence,
        "member_side_quest": member_side_quest,
        "member_place_invitations": member_place_invitations,
        "group_recovery": group_recovery,
        "ahead": ahead,
        "progress": progress,
        "overnight": overnight,
        "intents": projected_intents,
        "inbox": inbox,
        "reality_conflict": reality_conflict,
        "attention": attention_from_operator_projection({
            "operations": operations,
            "approval": approval_payload,
            "collective": collective,
            "intents": projected_intents,
            "inbox": inbox,
            "reality_conflict": reality_conflict,
            "flow": flow,
            "overnight": overnight,
            "brain": brain,
        }),
        "quiet_mode": "QUIET",
        "collective": collective,
        "timeline": timeline[-12:],
        "advanced": {
            "guardian": (
                {
                    "run_id": guardian.id,
                    "status": guardian.status,
                    "action_status": guardian.action_status,
                    "agent_run_id": guardian.agent_run_id,
                }
                if guardian is not None
                else None
            ),
            "agent": (
                {
                    "run_id": agent.id,
                    "provider_key": agent.provider_key,
                    "model_key": agent.model_key,
                    "status": agent.status,
                }
                if agent is not None
                else None
            ),
            "recovered_transition": recovered_transition,
        },
        "operator": {
            "watching": enabled,
            "will_continue_after_approval": bool(
                approval_payload is not None and pending is not None
            ),
        },
        "active_plan_version_id": journey.active_plan_version_id,
    }
