from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.audit import record_audit
from app.db.models import PlanVersion, ReplanProposal


def invalidate_pending_replans_for_event(
    session: Session,
    *,
    event_id: str,
    journey_id: str,
    decided_at: str,
    reason: str,
) -> tuple[str, ...]:
    """Retire proposals whose trigger is no longer actionable.

    The proposal and its candidate plan remain in history, but neither may be
    presented or accepted after the server-owned trigger has resolved.
    """

    pending = session.scalars(
        select(ReplanProposal).where(
            ReplanProposal.journey_id == str(journey_id),
            ReplanProposal.trigger_event_id == str(event_id),
            ReplanProposal.status.in_(("GENERATED", "PRESENTED")),
        )
    ).all()
    invalidated: list[str] = []
    for proposal in pending:
        proposal.status = "INVALIDATED"
        proposal.decided_at = decided_at
        candidate = session.get(PlanVersion, proposal.proposed_plan_version_id)
        if candidate is not None and candidate.status == "CANDIDATE":
            candidate.status = "REJECTED"
        record_audit(
            session,
            action="replan.invalidated",
            entity_type="ReplanProposal",
            entity_id=proposal.id,
            journey_id=str(journey_id),
            actor_type="SYSTEM",
            after={
                "status": proposal.status,
                "reason": reason,
                "trigger_event_id": str(event_id),
            },
        )
        invalidated.append(proposal.id)
    return tuple(invalidated)
