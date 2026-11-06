"""Server-owned transactional recovery execution semantics."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent.approval import approval_fingerprint, create_or_reuse_approval
from app.core.audit import record_audit
from app.db.models import (
    ActionApproval,
    Activity,
    AgentPendingAction,
    Booking,
    Journey,
    JourneyMission,
    PlanVersion,
    ReplanProposal,
    RecoveryEpoch,
    RecoveryExecutionAction as RecoveryExecutionActionRow,
    RecoveryExecutionPlan,
)

PLANNED = "PLANNED"
WAITING_FOR_APPROVAL = "WAITING_FOR_APPROVAL"
EXECUTING = "EXECUTING"
PARTIALLY_APPLIED = "PARTIALLY_APPLIED"
COMPLETED = "COMPLETED"
FAILED_SAFE = "FAILED_SAFE"
NEEDS_ATTENTION = "NEEDS_ATTENTION"

TERMINAL = frozenset({COMPLETED, PARTIALLY_APPLIED, FAILED_SAFE, NEEDS_ATTENTION})


@dataclass(frozen=True)
class RecoveryExecutionAction:
    kind: str
    target_type: str
    target_id: str
    provider_key: str | None = None
    requires_approval: bool = True

    def semantic_payload(self) -> dict[str, Any]:
        return {
            "kind": self.kind.upper(),
            "target_type": self.target_type,
            "target_id": self.target_id,
            "provider_key": self.provider_key,
            "requires_approval": bool(self.requires_approval),
        }


def action_fingerprint(
    *,
    journey_id: str,
    recovery_epoch_id: str,
    action: RecoveryExecutionAction,
) -> str:
    payload = {
        "journey_id": str(journey_id),
        "recovery_epoch_id": str(recovery_epoch_id),
        "action": action.semantic_payload(),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def classify_execution_state(
    action_statuses: Iterable[str],
    *,
    unresolved_required_actions: bool = False,
) -> str:
    statuses = [str(item).upper() for item in action_statuses]
    if not statuses:
        return PLANNED
    if any(item == "RUNNING" for item in statuses):
        return EXECUTING
    succeeded = sum(item in {"SUCCEEDED", "COMPLETED", "CONFIRMED", "CANCELLED"} for item in statuses)
    failed = sum(item in {"FAILED", "ERROR", "DECLINED"} for item in statuses)
    pending = sum(item in {"PENDING", "WAITING_FOR_APPROVAL"} for item in statuses)
    if pending and succeeded == 0 and failed == 0:
        return WAITING_FOR_APPROVAL
    if failed and succeeded:
        return PARTIALLY_APPLIED
    if failed and succeeded == 0:
        return FAILED_SAFE
    if unresolved_required_actions:
        return NEEDS_ATTENTION
    return COMPLETED


def is_sandbox_provider(provider_key: str | None) -> bool:
    key = str(provider_key or "").lower()
    return key.startswith(("mock-", "sandbox-", "lab-"))


def provider_truth_label(provider_key: str | None) -> str:
    return "LAB_SANDBOX_PROVIDER" if is_sandbox_provider(provider_key) else "EXTERNAL_PROVIDER"


def model_supplied_transaction_fields_are_forbidden(payload: dict[str, Any]) -> bool:
    """The model may request intent, never claim provider-side transaction truth."""
    forbidden = {
        "external_ref",
        "provider_external_ref",
        "provider_status",
        "booking_status",
        "payment_status",
        "provider_success",
        "confirmed",
    }
    return any(key in payload for key in forbidden)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _activity_changed(old: Activity, new: Activity) -> bool:
    return any(
        getattr(old, field) != getattr(new, field)
        for field in ("place_id", "city_service_id", "start_at", "end_at", "estimated_cost_minor", "currency")
    )


def recovery_requires_execution(
    session: Session, *, base_plan: PlanVersion, proposed_plan: PlanVersion
) -> bool:
    """Return whether accepted plan work needs external booking operations."""
    old = {
        item.sequence_no: item
        for item in session.scalars(select(Activity).where(Activity.plan_version_id == base_plan.id)).all()
    }
    new = {
        item.sequence_no: item
        for item in session.scalars(select(Activity).where(Activity.plan_version_id == proposed_plan.id)).all()
    }
    if not old or not new:
        return False
    booked = session.scalars(
        select(Booking).where(
            Booking.journey_id == base_plan.journey_id,
            Booking.status.in_(("CONFIRMED", "PENDING")),
        )
    ).all()
    return any(
        booking.activity_id
        and (new.get(old_seq) is not None)
        and _activity_changed(old_activity, new[old_seq])
        for old_seq, old_activity in old.items()
        for booking in booked
        if booking.activity_id == old_activity.id
    )


def _action_fingerprint(*, journey_id: str, recovery_epoch_id: str, kind: str, target_type: str, target_id: str, provider_key: str | None) -> str:
    action = RecoveryExecutionAction(kind, target_type, target_id, provider_key)
    return action_fingerprint(journey_id=journey_id, recovery_epoch_id=recovery_epoch_id, action=action)


def compile_recovery_execution_plan(
    session: Session,
    *,
    journey: Journey,
    proposal: ReplanProposal,
    agent_run_id: str | None = None,
    mission_id: str | None = None,
    require_approval: bool = True,
) -> dict[str, Any]:
    """Freeze exact booking targets from an accepted proposal before execution."""
    existing = session.scalar(
        select(RecoveryExecutionPlan).where(RecoveryExecutionPlan.proposal_id == proposal.id)
    )
    if existing is not None:
        return execution_plan_projection(session, existing)
    from app.recovery_epoch import ensure_recovery_epoch
    epoch = session.get(RecoveryEpoch, str(proposal.recovery_epoch_id)) if proposal.recovery_epoch_id else None
    # Acceptance changes the active PlanVersion before this compiler runs.  A
    # proposal's decision epoch is the causal input; the operational approval
    # must bind the new current reality that contains the accepted plan.
    if epoch is None or epoch.status != "OPEN" or epoch.active_plan_version_id_at_open != journey.active_plan_version_id:
        epoch = ensure_recovery_epoch(session, journey)
        proposal.recovery_epoch_id = epoch.id
    epoch_id = str(epoch.id)
    base = session.get(PlanVersion, proposal.base_plan_version_id)
    proposed = session.get(PlanVersion, proposal.proposed_plan_version_id)
    if base is None or proposed is None:
        raise ValueError("recovery proposal plans are missing")
    old = {item.sequence_no: item for item in session.scalars(select(Activity).where(Activity.plan_version_id == base.id)).all()}
    new = {item.sequence_no: item for item in session.scalars(select(Activity).where(Activity.plan_version_id == proposed.id)).all()}
    bookings = session.scalars(
        select(Booking).where(
            Booking.journey_id == journey.id,
            Booking.activity_id.is_not(None),
            Booking.status.in_(("CONFIRMED", "PENDING")),
        )
    ).all()
    actions: list[RecoveryExecutionAction] = []
    for booking in bookings:
        source = session.get(Activity, booking.activity_id)
        if source is None or source.plan_version_id != base.id:
            continue
        replacement = new.get(source.sequence_no)
        if replacement is None or not _activity_changed(source, replacement):
            continue
        provider_key = str(booking.provider_key or "mock-booking")
        # Replacement first is server policy: an unpaid refundable sandbox
        # booking can be secured before the old reservation is cancelled.
        actions.append(RecoveryExecutionAction("CREATE_BOOKING", "Activity", replacement.id, provider_key))
        actions.append(RecoveryExecutionAction("CANCEL_BOOKING", "Booking", booking.id, provider_key))
    payload = [item.semantic_payload() for item in actions]
    semantic_fingerprint = hashlib.sha256(_json({"journey_id": journey.id, "epoch": epoch_id, "proposal": proposal.id, "actions": payload}).encode()).hexdigest()
    now = _now()
    status = PLANNED if actions else COMPLETED
    plan = RecoveryExecutionPlan(
        id=__import__("uuid").uuid4().hex,
        journey_id=journey.id,
        recovery_epoch_id=epoch_id,
        proposal_id=proposal.id,
        agent_run_id=agent_run_id,
        status=status,
        semantic_fingerprint=semantic_fingerprint,
        created_at=now,
        updated_at=now,
        completed_at=now if not actions else None,
    )
    session.add(plan)
    session.flush()
    for sequence_no, item in enumerate(actions, start=1):
        session.add(
            RecoveryExecutionActionRow(
                id=__import__("uuid").uuid4().hex,
                execution_plan_id=plan.id,
                sequence_no=sequence_no,
                kind=item.kind,
                target_type=item.target_type,
                target_id=item.target_id,
                provider_key=item.provider_key,
                semantic_fingerprint=action_fingerprint(journey_id=journey.id, recovery_epoch_id=epoch_id, action=item),
                authority_decision="REQUIRE_CONFIRMATION" if require_approval else "ALLOW_AUTO",
                status="PENDING",
                created_at=now,
            )
        )
    if actions and require_approval:
        fingerprint = approval_fingerprint(
            action="RECOVERY_EXECUTION",
            journey_id=journey.id,
            target_type="RecoveryExecutionPlan",
            target_id=plan.id,
            policy_version=1,
            decision="REQUIRE_CONFIRMATION",
            facts={"semantic_fingerprint": semantic_fingerprint, "action_count": len(actions)},
            proposal_diff={"actions": payload},
        )
        approval = create_or_reuse_approval(
            session,
            traveler_id=journey.traveler_id,
            journey_id=journey.id,
            action="RECOVERY_EXECUTION",
            target_type="RecoveryExecutionPlan",
            target_id=plan.id,
            request_fingerprint=fingerprint,
            actor_id="transactional-operator",
            required_decision="REQUIRE_CONFIRMATION",
            recovery_epoch_id=epoch_id,
        )
        plan.approval_id = approval.id
        plan.status = WAITING_FOR_APPROVAL
        if agent_run_id and mission_id:
            session.add(
                AgentPendingAction(
                    id=__import__("uuid").uuid4().hex,
                    approval_id=approval.id,
                    run_id=agent_run_id,
                    mission_id=mission_id,
                    journey_id=journey.id,
                    tool_name="execute_recovery_execution",
                    arguments_json=_json({"journey_id": journey.id, "plan_id": plan.id, "agent_id": "transactional-operator"}),
                    status="PENDING",
                    result_json="{}",
                    error_json="{}",
                    created_at=now,
                    updated_at=now,
                    recovery_epoch_id=epoch_id,
                )
            )
    record_audit(session, action="operator.execution_plan.created", entity_type="RecoveryExecutionPlan", entity_id=plan.id, journey_id=journey.id, actor_type="SYSTEM", actor_id="transactional-operator", after={"status": plan.status, "action_count": len(actions), "provider_truth": provider_truth_label(actions[0].provider_key if actions else None)})
    session.commit()
    return execution_plan_projection(session, plan)


def execution_plan_projection(session: Session, plan: RecoveryExecutionPlan) -> dict[str, Any]:
    actions = session.scalars(select(RecoveryExecutionActionRow).where(RecoveryExecutionActionRow.execution_plan_id == plan.id).order_by(RecoveryExecutionActionRow.sequence_no)).all()
    return {
        "id": plan.id,
        "journey_id": plan.journey_id,
        "recovery_epoch_id": plan.recovery_epoch_id,
        "proposal_id": plan.proposal_id,
        "status": plan.status,
        "approval_id": plan.approval_id,
        "semantic_fingerprint": plan.semantic_fingerprint,
        "actions": [{"id": row.id, "sequence_no": row.sequence_no, "kind": row.kind, "target_type": row.target_type, "target_id": row.target_id, "provider_label": provider_truth_label(row.provider_key), "status": row.status, "result": json.loads(row.result_json or "{}"), "error": json.loads(row.error_json or "{}")} for row in actions],
    }


def execute_recovery_execution_plan(session: Session, plan_id: str, *, approval_id: str | None = None) -> dict[str, Any]:
    """Continue a frozen saga without invoking a model or recompiling intent."""
    plan = session.get(RecoveryExecutionPlan, str(plan_id))
    if plan is None:
        raise ValueError("recovery execution plan was not found")
    if plan.status in TERMINAL:
        return execution_plan_projection(session, plan) | {"replayed": True}
    if plan.approval_id and approval_id != plan.approval_id:
        approval = session.get(ActionApproval, plan.approval_id)
        if approval is None or approval.status != "CONSUMED":
            plan.status = WAITING_FOR_APPROVAL
            session.commit()
            return execution_plan_projection(session, plan)
    if plan.approval_id:
        approval = session.get(ActionApproval, plan.approval_id)
        if approval is None or approval.status not in {"APPROVED", "CONSUMED"}:
            plan.status = WAITING_FOR_APPROVAL
            session.commit()
            return execution_plan_projection(session, plan)
        if approval.status == "APPROVED":
            approval.status = "CONSUMED"
            approval.consumed_at = _now()
            record_audit(session, action="agent.action_approval.consumed", entity_type="ActionApproval", entity_id=approval.id, journey_id=plan.journey_id, actor_type="SYSTEM", actor_id="transactional-operator", after={"action": approval.action, "target_type": approval.target_type, "target_id": approval.target_id, "status": "CONSUMED"})
    plan.status = EXECUTING
    plan.updated_at = _now()
    session.commit()
    from app.commerce.service_impl import cancel_booking as commerce_cancel_booking
    from app.commerce.service_impl import create_booking as commerce_create_booking
    from app.main import _assert_selected_plan_feasible, _mock_booking_provider
    from app.commerce.strengths_generated import SOURCE_COMMITMENT_STRENGTHS
    journey = session.get(Journey, plan.journey_id)
    for row in session.scalars(select(RecoveryExecutionActionRow).where(RecoveryExecutionActionRow.execution_plan_id == plan.id).order_by(RecoveryExecutionActionRow.sequence_no)).all():
        if row.status in {"SUCCEEDED", "CONFIRMED", "CANCELLED"}:
            continue
        row.status = "RUNNING"
        row.started_at = row.started_at or _now()
        session.commit()
        try:
            provider, provider_failure = _mock_booking_provider(str(row.provider_key or "mock-booking"))
            if row.kind == "CREATE_BOOKING":
                booking = commerce_create_booking(session, journey_id=journey.id, activity_id=row.target_id, provider=provider, source_strength_codes=SOURCE_COMMITMENT_STRENGTHS, booking_source_type_code="booking", assert_selected_plan_feasible=_assert_selected_plan_feasible, force_failure=provider_failure, allow_active_recovery=True)
                result = {"booking_id": booking.id, "status": booking.status, "provider_label": provider_truth_label(booking.provider_key)}
                row.status = "CONFIRMED" if booking.status == "CONFIRMED" else booking.status
            elif row.kind == "CANCEL_BOOKING":
                booking = commerce_cancel_booking(session, booking_id=row.target_id, provider=provider, force_failure=provider_failure)
                result = {"booking_id": booking.id, "status": booking.status, "provider_label": provider_truth_label(booking.provider_key)}
                row.status = "CANCELLED"
            else:
                raise ValueError("unsupported recovery execution action")
            row.result_json = _json(result)
            row.completed_at = _now()
            row.error_json = "{}"
            session.commit()
        except Exception as exc:
            session.rollback()
            row = session.get(RecoveryExecutionActionRow, row.id)
            row.status = "FAILED"
            row.error_json = _json({"code": type(exc).__name__, "message": str(exc)[:240]})
            row.completed_at = _now()
            session.commit()
            break
    statuses = [row.status for row in session.scalars(select(RecoveryExecutionActionRow).where(RecoveryExecutionActionRow.execution_plan_id == plan.id)).all()]
    plan = session.get(RecoveryExecutionPlan, plan.id)
    plan.status = classify_execution_state(statuses)
    plan.updated_at = _now()
    if plan.status in TERMINAL:
        plan.completed_at = plan.completed_at or _now()
        from app.db.models import RecoveryEpoch
        from app.recovery_epoch import close_recovery_epoch

        epoch = session.get(RecoveryEpoch, plan.recovery_epoch_id)
        if epoch is not None and epoch.status == "OPEN":
            close_recovery_epoch(
                session,
                epoch,
                status="COMMITTED" if plan.status == COMPLETED else "ABANDONED",
                reason=f"TRANSACTIONAL_EXECUTION_{plan.status}",
            )
    session.commit()
    return execution_plan_projection(session, plan)
