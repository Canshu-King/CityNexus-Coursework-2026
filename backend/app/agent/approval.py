from __future__ import annotations

import hmac
import os
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.commerce.idempotency import canonical_request_signature
from app.core.audit import record_audit
from app.db.models import ActionApproval

APPROVAL_TTL = timedelta(minutes=10)
DEMO_STEP_UP_ENV = "CITYNEXUS_DEMO_STEP_UP_CODE"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def approval_fingerprint(
    *,
    action: str,
    journey_id: str,
    target_type: str,
    target_id: str,
    policy_version: int,
    decision: str,
    facts: dict[str, Any],
    proposal_diff: dict[str, Any],
) -> str:
    return canonical_request_signature(
        {
            "action": action,
            "journey_id": journey_id,
            "target_type": target_type,
            "target_id": target_id,
            "policy_version": policy_version,
            "decision": decision,
            "facts": facts,
            "proposal_diff": proposal_diff,
        }
    )


def approval_payload(row: ActionApproval) -> dict[str, Any]:
    return {
        "approval_id": row.id,
        "journey_id": row.journey_id,
        "action": row.action,
        "target_type": row.target_type,
        "target_id": row.target_id,
        "status": row.status,
        "required_decision": row.required_decision,
        "approval_method": row.approval_method,
        "created_at": row.created_at,
        "expires_at": row.expires_at,
        "approved_at": row.approved_at,
        "consumed_at": row.consumed_at,
    }


def create_or_reuse_approval(
    session: Session,
    *,
    traveler_id: str,
    journey_id: str,
    action: str,
    target_type: str,
    target_id: str,
    request_fingerprint: str,
    actor_id: str,
    required_decision: str,
) -> ActionApproval:
    now = _now()
    existing = session.scalar(
        select(ActionApproval)
        .where(
            ActionApproval.journey_id == journey_id,
            ActionApproval.action == action,
            ActionApproval.target_type == target_type,
            ActionApproval.target_id == target_id,
            ActionApproval.request_fingerprint == request_fingerprint,
            ActionApproval.status.in_(("PENDING", "APPROVED")),
        )
        .order_by(ActionApproval.created_at.desc())
    )
    if existing is not None:
        if _as_datetime(existing.expires_at) > now:
            return existing
        existing.status = "EXPIRED"

    row = ActionApproval(
        id=str(uuid4()),
        traveler_id=traveler_id,
        journey_id=journey_id,
        action=action,
        target_type=target_type,
        target_id=target_id,
        request_fingerprint=request_fingerprint,
        status="PENDING",
        required_decision=required_decision,
        created_at=now.isoformat(),
        expires_at=(now + APPROVAL_TTL).isoformat(),
    )
    session.add(row)
    record_audit(
        session,
        action="agent.action_approval.created",
        entity_type="ActionApproval",
        entity_id=row.id,
        journey_id=journey_id,
        actor_type="AI_AGENT",
        actor_id=actor_id,
        after={
            "approval_id": row.id,
            "action": action,
            "target_type": target_type,
            "target_id": target_id,
            "status": row.status,
            "expires_at": row.expires_at,
        },
    )
    session.flush()
    return row


def approve_action_approval(
    session: Session,
    row: ActionApproval,
    *,
    approval_method: str,
    step_up_code: str | None,
    actor_id: str,
) -> dict[str, Any]:
    now = _now()
    if _as_datetime(row.expires_at) <= now:
        row.status = "EXPIRED"
        record_audit(
            session,
            action="agent.action_approval.expired",
            entity_type="ActionApproval",
            entity_id=row.id,
            journey_id=row.journey_id,
            actor_type="USER",
            actor_id=actor_id,
            after={"approval_id": row.id, "status": row.status},
        )
        session.commit()
        raise HTTPException(409, "agent approval has expired")
    if row.status == "APPROVED":
        return approval_payload(row)
    if row.status != "PENDING":
        raise HTTPException(409, "agent approval is no longer usable")

    if row.required_decision == "REQUIRE_STEP_UP":
        if approval_method != "STEP_UP":
            raise HTTPException(422, "verified step-up proof is required")
        expected_code = os.getenv(DEMO_STEP_UP_ENV)
        if not expected_code:
            raise HTTPException(503, "step-up verifier is not configured")
        if not step_up_code or not hmac.compare_digest(step_up_code, expected_code):
            raise HTTPException(403, "step-up proof was not verified")
    elif approval_method != "USER_CONFIRMATION":
        raise HTTPException(422, "approval method does not match the required proof")

    row.status = "APPROVED"
    row.approval_method = approval_method
    row.approved_by = actor_id
    row.approved_at = now.isoformat()
    record_audit(
        session,
        action="agent.action_approval.approved",
        entity_type="ActionApproval",
        entity_id=row.id,
        journey_id=row.journey_id,
        actor_type="USER",
        actor_id=actor_id,
        after={
            "approval_id": row.id,
            "action": row.action,
            "target_type": row.target_type,
            "target_id": row.target_id,
            "status": row.status,
            "approval_method": approval_method,
        },
    )
    session.commit()
    return approval_payload(row)


def consume_action_approval(
    session: Session,
    *,
    approval_id: str,
    journey_id: str,
    action: str,
    target_type: str,
    target_id: str,
    request_fingerprint: str,
    actor_id: str,
) -> ActionApproval:
    row = session.get(ActionApproval, approval_id)
    if row is None or row.journey_id != journey_id:
        raise HTTPException(409, "agent approval does not match this action")
    if row.request_fingerprint != request_fingerprint:
        raise HTTPException(409, "agent approval was issued for a different action")
    if row.action != action or row.target_type != target_type or row.target_id != target_id:
        raise HTTPException(409, "agent approval scope does not match this action")
    if _as_datetime(row.expires_at) <= _now():
        row.status = "EXPIRED"
        session.flush()
        raise HTTPException(409, "agent approval has expired")
    claimed = session.execute(
        update(ActionApproval)
        .where(
            ActionApproval.id == approval_id,
            ActionApproval.journey_id == journey_id,
            ActionApproval.action == action,
            ActionApproval.target_type == target_type,
            ActionApproval.target_id == target_id,
            ActionApproval.request_fingerprint == request_fingerprint,
            ActionApproval.status == "APPROVED",
        )
        .values(status="CONSUMED", consumed_at=_now().isoformat())
    )
    if claimed.rowcount != 1:
        raise HTTPException(409, "agent approval is no longer usable")
    row.status = "CONSUMED"
    row.consumed_at = _now().isoformat()
    record_audit(
        session,
        action="agent.action_approval.consumed",
        entity_type="ActionApproval",
        entity_id=row.id,
        journey_id=journey_id,
        actor_type="AI_AGENT",
        actor_id=actor_id,
        after={
            "approval_id": row.id,
            "action": action,
            "target_type": target_type,
            "target_id": target_id,
            "status": row.status,
        },
    )
    session.flush()
    return row
