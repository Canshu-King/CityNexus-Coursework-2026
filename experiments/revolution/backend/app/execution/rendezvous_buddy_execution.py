"""Pair-private finite execution and self-reported outcome for an R79 handoff."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.core.audit import record_audit
from app.db.models import (
    Journey,
    JourneyMember,
    JourneyRendezvousBuddyExecution,
    JourneyRendezvousBuddyExecutionReceipt,
    JourneyRendezvousBuddyHandoff,
    JourneyRendezvousMemberSignal,
    JourneyRendezvousSignalAcknowledgement,
    Place,
)
from app.execution.clock import execution_clock
from app.traveler_principal import TravelerPrincipal

WINDOW_MINUTES = {30, 60, 90}
RECEIPT_KINDS = {"BUDDY_MET_ACK", "CANNOT_MEET", "CANCEL_PARTICIPATION"}
TERMINAL_EXECUTION_STATES = {
    "MUTUALLY_ACKNOWLEDGED", "REPLAN_REQUIRED", "CANCELLED", "EXPIRED", "STALE",
}


def _now() -> datetime:
    return execution_clock.now().astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _parse_time(value: str | None) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or ""))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _request_hash(handoff: JourneyRendezvousBuddyHandoff, window: int) -> str:
    facts = {
        "handoff_id": str(handoff.id),
        "journey_id": str(handoff.journey_id),
        "helper_member_id": str(handoff.helper_member_id),
        "requester_member_id": str(handoff.requester_member_id),
        "window_minutes": int(window),
        "plan_version_id": str(handoff.active_plan_version_id),
        "current_activity_id": str(handoff.current_activity_id or ""),
        "rendezvous_activity_id": str(handoff.original_rendezvous_activity_id),
        "original_place_id": str(handoff.original_public_place_id),
        "handoff_place_id": str(handoff.handoff_place_id),
        "audience_sha256": str(handoff.audience_sha256),
        "handoff_snapshot_sha256": _sha(str(handoff.handoff_place_snapshot_json)),
    }
    return _sha(json.dumps(facts, sort_keys=True, separators=(",", ":")))


def create_execution_window_offer_record(
    session: Session,
    *,
    handoff: JourneyRendezvousBuddyHandoff,
    window_minutes: int,
    idempotency_key: str,
    proposed_at: datetime | None = None,
) -> JourneyRendezvousBuddyExecution:
    """Add helper's explicit finite-window offer inside the caller's transaction."""
    if int(window_minutes) not in WINDOW_MINUTES:
        raise HTTPException(422, "BUDDY_EXECUTION_WINDOW_UNSUPPORTED")
    from app.execution.rendezvous_buddy_handoff import _candidate, _distance_m, _is_public_place
    from app.execution.group_presence import public_destination_coordinates

    original = session.get(Place, str(handoff.original_public_place_id))
    handoff_place = session.get(Place, str(handoff.handoff_place_id))
    if original is None or handoff_place is None or not _is_public_place(original) or not _is_public_place(handoff_place):
        raise HTTPException(409, "BUDDY_EXECUTION_PUBLIC_PLACE_STALE")
    original_coords = public_destination_coordinates(original)
    handoff_coords = public_destination_coordinates(handoff_place)
    if original_coords is None or handoff_coords is None:
        raise HTTPException(409, "BUDDY_EXECUTION_PUBLIC_PLACE_STALE")
    original_snapshot = _candidate(original, original=original, distance_m=0)
    handoff_snapshot = _candidate(
        handoff_place,
        original=original,
        distance_m=_distance_m(*original_coords, *handoff_coords),
    )
    try:
        recorded_handoff_snapshot = json.loads(handoff.handoff_place_snapshot_json)
    except (TypeError, json.JSONDecodeError):
        raise HTTPException(409, "BUDDY_EXECUTION_HANDOFF_SNAPSHOT_INVALID")
    if handoff_snapshot != recorded_handoff_snapshot:
        raise HTTPException(409, "BUDDY_EXECUTION_PUBLIC_PLACE_STALE")

    now = proposed_at or _now()
    row = JourneyRendezvousBuddyExecution(
        id=str(uuid4()),
        journey_id=str(handoff.journey_id),
        handoff_id=str(handoff.id),
        signal_id=str(handoff.signal_id),
        acknowledgement_id=str(handoff.acknowledgement_id),
        requester_member_id=str(handoff.requester_member_id),
        requester_traveler_id=str(handoff.requester_traveler_id),
        helper_member_id=str(handoff.helper_member_id),
        helper_traveler_id=str(handoff.helper_traveler_id),
        active_plan_version_id=str(handoff.active_plan_version_id),
        current_activity_id=str(handoff.current_activity_id) if handoff.current_activity_id else None,
        original_rendezvous_activity_id=str(handoff.original_rendezvous_activity_id),
        original_public_place_id=str(handoff.original_public_place_id),
        original_place_snapshot_json=json.dumps(original_snapshot, sort_keys=True, separators=(",", ":")),
        handoff_place_id=str(handoff.handoff_place_id),
        handoff_place_snapshot_json=json.dumps(handoff_snapshot, sort_keys=True, separators=(",", ":")),
        audience_member_ids_json=str(handoff.audience_member_ids_json),
        audience_sha256=str(handoff.audience_sha256),
        execution_window_minutes=int(window_minutes),
        status="WINDOW_OFFERED",
        idempotency_key=str(idempotency_key),
        request_hash=_request_hash(handoff, int(window_minutes)),
        helper_consent_traveler_id=str(handoff.helper_traveler_id),
        requester_consent_traveler_id=None,
        helper_consented_at=_iso(now),
        requester_consented_at=None,
        proposed_at=_iso(now),
        proposal_expires_at=str(handoff.expires_at),
        started_at=None,
        expires_at=None,
        closed_at=None,
        outcome_code=None,
        created_at=_iso(now),
        updated_at=_iso(now),
    )
    session.add(row)
    return row


def activate_execution_on_handoff_accept(
    session: Session,
    *,
    handoff: JourneyRendezvousBuddyHandoff,
    requester: JourneyMember,
    principal: TravelerPrincipal,
    accepted_at: datetime,
) -> None:
    """Bind the requester's ACCEPT click to the exact window shown on the offer."""
    execution = session.scalar(select(JourneyRendezvousBuddyExecution).where(
        JourneyRendezvousBuddyExecution.handoff_id == handoff.id,
    ))
    if execution is None:
        return
    if (
        execution.status != "WINDOW_OFFERED"
        or str(requester.id) != str(execution.requester_member_id)
        or str(principal.traveler_id) != str(execution.requester_traveler_id)
        or str(execution.helper_consent_traveler_id) != str(execution.helper_traveler_id)
        or (_parse_time(execution.proposal_expires_at) or accepted_at) <= accepted_at
    ):
        raise HTTPException(409, "BUDDY_EXECUTION_WINDOW_OFFER_STALE")
    execution.requester_consent_traveler_id = str(principal.traveler_id)
    execution.requester_consented_at = _iso(accepted_at)
    execution.status = "ACTIVE"
    execution.started_at = _iso(accepted_at)
    execution.expires_at = _iso(accepted_at + timedelta(minutes=int(execution.execution_window_minutes)))
    execution.updated_at = _iso(accepted_at)


def _claim_context(session: Session, journey: Journey, handoff: JourneyRendezvousBuddyHandoff) -> Journey:
    from app.execution.rendezvous_coordination import _claim_journey_revision

    return _claim_journey_revision(
        session,
        journey_id=str(journey.id),
        plan_version_id=str(handoff.active_plan_version_id),
        current_activity_id=str(handoff.current_activity_id or "") or None,
    )


def offer_execution_window(
    session: Session,
    *,
    journey: Journey,
    handoff_id: str,
    viewer: JourneyMember,
    principal: TravelerPrincipal,
    window_minutes: int,
    idempotency_key: str,
) -> dict[str, object]:
    key = str(idempotency_key or "").strip()
    if len(key) < 8 or len(key) > 128:
        raise HTTPException(400, "BUDDY_EXECUTION_IDEMPOTENCY_KEY_REQUIRED")
    if int(window_minutes) not in WINDOW_MINUTES:
        raise HTTPException(422, "BUDDY_EXECUTION_WINDOW_UNSUPPORTED")
    if str(viewer.traveler_id) != str(principal.traveler_id):
        raise HTTPException(403, "BUDDY_EXECUTION_SIGNED_ACTOR_MISMATCH")
    handoff = session.scalar(select(JourneyRendezvousBuddyHandoff).where(
        JourneyRendezvousBuddyHandoff.id == handoff_id,
        JourneyRendezvousBuddyHandoff.journey_id == journey.id,
    ))
    if handoff is None or str(viewer.id) != str(handoff.helper_member_id):
        raise HTTPException(404, "BUDDY_EXECUTION_HANDOFF_NOT_FOUND")
    if handoff.status != "ACCEPTED":
        raise HTTPException(409, "BUDDY_EXECUTION_REQUIRES_ACCEPTED_HANDOFF")
    journey = _claim_context(session, journey, handoff)
    handoff = session.get(JourneyRendezvousBuddyHandoff, handoff_id)
    assert handoff is not None
    from app.execution.rendezvous_buddy_handoff import _current_projection

    if _current_projection(session, journey, row=handoff, viewer=viewer) is None:
        raise HTTPException(409, "BUDDY_EXECUTION_HANDOFF_STALE")

    expected_hash = _request_hash(handoff, int(window_minutes))
    prior = session.scalar(select(JourneyRendezvousBuddyExecution).where(
        JourneyRendezvousBuddyExecution.journey_id == journey.id,
        JourneyRendezvousBuddyExecution.helper_member_id == viewer.id,
        JourneyRendezvousBuddyExecution.idempotency_key == key,
    ))
    if prior is not None:
        if prior.request_hash != expected_hash:
            raise HTTPException(409, "BUDDY_EXECUTION_IDEMPOTENCY_KEY_REUSED")
        return _execution_payload(session, prior, viewer_member_id=str(viewer.id), effective_status=prior.status,
                                  context_current=True, stale_reason=None)
    existing = session.scalar(select(JourneyRendezvousBuddyExecution).where(
        JourneyRendezvousBuddyExecution.handoff_id == handoff.id,
    ))
    if existing is not None:
        raise HTTPException(409, "BUDDY_EXECUTION_WINDOW_ALREADY_OFFERED")

    now = _now()
    execution = create_execution_window_offer_record(
        session,
        handoff=handoff,
        window_minutes=int(window_minutes),
        idempotency_key=key,
        proposed_at=now,
    )
    record_audit(
        session,
        action="journey.rendezvous_buddy_execution.window_offered",
        entity_type="JourneyRendezvousBuddyExecution",
        entity_id=execution.id,
        journey_id=journey.id,
        actor_type="AUTHENTICATED_TRAVELER",
        actor_id=principal.traveler_id,
        after={"status": "WINDOW_OFFERED", "window_minutes": int(window_minutes), "shared_plan_changed": False},
    )
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        winner = session.scalar(select(JourneyRendezvousBuddyExecution).where(
            JourneyRendezvousBuddyExecution.handoff_id == handoff_id,
        ))
        if winner is not None and winner.request_hash == expected_hash:
            return _execution_payload(session, winner, viewer_member_id=str(viewer.id), effective_status=winner.status,
                                      context_current=True, stale_reason=None)
        raise HTTPException(409, "BUDDY_EXECUTION_CONCURRENT_UPDATE_RETRY") from exc
    except OperationalError as exc:
        session.rollback()
        raise HTTPException(409, "BUDDY_EXECUTION_WRITE_BUSY_RETRY") from exc
    return _execution_payload(session, execution, viewer_member_id=str(viewer.id), effective_status="WINDOW_OFFERED",
                              context_current=True, stale_reason=None)


def accept_execution_window(
    session: Session,
    *,
    journey: Journey,
    handoff_id: str,
    viewer: JourneyMember,
    principal: TravelerPrincipal,
) -> dict[str, object]:
    if str(viewer.traveler_id) != str(principal.traveler_id):
        raise HTTPException(403, "BUDDY_EXECUTION_SIGNED_ACTOR_MISMATCH")
    handoff = session.scalar(select(JourneyRendezvousBuddyHandoff).where(
        JourneyRendezvousBuddyHandoff.id == handoff_id,
        JourneyRendezvousBuddyHandoff.journey_id == journey.id,
    ))
    if handoff is None or str(viewer.id) != str(handoff.requester_member_id):
        raise HTTPException(404, "BUDDY_EXECUTION_HANDOFF_NOT_FOUND")
    if handoff.status != "ACCEPTED":
        raise HTTPException(409, "BUDDY_EXECUTION_REQUIRES_ACCEPTED_HANDOFF")
    journey = _claim_context(session, journey, handoff)
    handoff = session.get(JourneyRendezvousBuddyHandoff, handoff_id)
    execution = session.scalar(select(JourneyRendezvousBuddyExecution).where(
        JourneyRendezvousBuddyExecution.handoff_id == handoff_id,
    ))
    if handoff is None or execution is None:
        raise HTTPException(404, "BUDDY_EXECUTION_WINDOW_NOT_FOUND")
    from app.execution.rendezvous_buddy_handoff import _current_projection

    if execution.status == "ACTIVE" and str(execution.requester_consent_traveler_id) == str(principal.traveler_id):
        return _project_execution(session, journey, handoff, viewer) or {
            "execution_id": str(execution.id), "status": "STALE", "can_report": False,
        }
    if execution.status != "WINDOW_OFFERED":
        raise HTTPException(409, "BUDDY_EXECUTION_WINDOW_STALE")
    now = _now()
    proposal_expiry = _parse_time(execution.proposal_expires_at)
    if proposal_expiry is None or proposal_expiry <= now:
        execution.status = "EXPIRED"
        execution.closed_at = _iso(now)
        execution.updated_at = _iso(now)
        record_audit(
            session,
            action="journey.rendezvous_buddy_execution.window_expired",
            entity_type="JourneyRendezvousBuddyExecution",
            entity_id=execution.id,
            journey_id=journey.id,
            actor_type="AUTHENTICATED_TRAVELER",
            actor_id=principal.traveler_id,
            after={"status": "EXPIRED", "shared_plan_changed": False},
        )
        session.commit()
        raise HTTPException(409, "BUDDY_EXECUTION_WINDOW_EXPIRED")
    if _current_projection(session, journey, row=handoff, viewer=viewer) is None:
        raise HTTPException(409, "BUDDY_EXECUTION_WINDOW_STALE")
    execution.requester_consent_traveler_id = str(principal.traveler_id)
    execution.requester_consented_at = _iso(now)
    execution.status = "ACTIVE"
    execution.started_at = _iso(now)
    execution.expires_at = _iso(now + timedelta(minutes=int(execution.execution_window_minutes)))
    execution.updated_at = _iso(now)
    record_audit(
        session,
        action="journey.rendezvous_buddy_execution.window_accepted",
        entity_type="JourneyRendezvousBuddyExecution",
        entity_id=execution.id,
        journey_id=journey.id,
        actor_type="AUTHENTICATED_TRAVELER",
        actor_id=principal.traveler_id,
        after={"status": "ACTIVE", "window_minutes": int(execution.execution_window_minutes), "shared_plan_changed": False},
    )
    try:
        session.commit()
    except (IntegrityError, OperationalError) as exc:
        session.rollback()
        raise HTTPException(409, "BUDDY_EXECUTION_CONCURRENT_UPDATE_RETRY") from exc
    return _project_execution(session, journey, handoff, viewer) or {
        "execution_id": str(execution.id), "status": "STALE", "can_report": False,
    }


def _execution_context_current(
    session: Session,
    journey: Journey,
    handoff: JourneyRendezvousBuddyHandoff,
    execution: JourneyRendezvousBuddyExecution,
    viewer: JourneyMember,
) -> tuple[bool, str | None]:
    if str(viewer.id) not in {str(execution.helper_member_id), str(execution.requester_member_id)}:
        return False, "PAIR_MEMBER_REQUIRED"
    if handoff.status != "ACCEPTED":
        return False, "HANDOFF_WITHDRAWN"
    if str(journey.status).upper() != "ACTIVE":
        return False, "JOURNEY_NOT_ACTIVE"
    if (
        str(journey.active_plan_version_id or "") != str(execution.active_plan_version_id)
        or str(journey.current_activity_id or "") != str(execution.current_activity_id or "")
    ):
        return False, "SHARED_PLAN_OR_CURSOR_CHANGED"
    now = _now()
    helper = session.get(JourneyMember, str(execution.helper_member_id))
    requester = session.get(JourneyMember, str(execution.requester_member_id))
    if not _member_in_force(helper, now) or not _member_in_force(requester, now):
        return False, "PAIR_MEMBERSHIP_CHANGED"
    if (
        str(helper.journey_id) != str(execution.journey_id)
        or str(requester.journey_id) != str(execution.journey_id)
        or str(helper.traveler_id) != str(execution.helper_traveler_id)
        or str(requester.traveler_id) != str(execution.requester_traveler_id)
    ):
        return False, "PAIR_IDENTITY_CHANGED"
    signal = session.get(JourneyRendezvousMemberSignal, str(execution.signal_id))
    acknowledgement = session.get(JourneyRendezvousSignalAcknowledgement, str(execution.acknowledgement_id))
    if signal is None or signal.status in {"REVOKED", "REPLACED", "STALE"} or acknowledgement is None:
        return False, "ORIGINAL_HELP_REVOKED_OR_REPLACED"
    if (
        str(signal.journey_id) != str(execution.journey_id)
        or str(signal.journey_member_id) != str(execution.requester_member_id)
        or str(signal.traveler_id) != str(execution.requester_traveler_id)
        or str(acknowledgement.signal_id) != str(execution.signal_id)
        or str(acknowledgement.requester_member_id) != str(execution.requester_member_id)
        or str(acknowledgement.responder_member_id) != str(execution.helper_member_id)
        or str(acknowledgement.responder_traveler_id) != str(execution.helper_traveler_id)
    ):
        return False, "ORIGINAL_HELP_IDENTITY_CHANGED"
    from app.execution.rendezvous_coordination import _current_context

    try:
        _members, activity, original, _context_now = _current_context(
            session,
            journey,
            viewer,
            expected_plan_version_id=str(execution.active_plan_version_id),
            expected_current_activity_id=str(execution.current_activity_id or ""),
            expected_rendezvous_activity_id=str(execution.original_rendezvous_activity_id),
            expected_public_place_id=str(execution.original_public_place_id),
        )
    except HTTPException as exc:
        return False, str(exc.detail)
    if str(activity.id) != str(execution.original_rendezvous_activity_id):
        return False, "ORIGINAL_RENDEZVOUS_CHANGED"
    from app.execution.rendezvous_buddy_handoff import _candidate, _distance_m, _is_public_place
    from app.execution.group_presence import public_destination_coordinates

    handoff_place = session.get(Place, str(execution.handoff_place_id))
    if handoff_place is None or original is None or not _is_public_place(handoff_place) or not _is_public_place(original):
        return False, "PUBLIC_PLACE_ACCESS_CHANGED"
    original_coords = public_destination_coordinates(original)
    handoff_coords = public_destination_coordinates(handoff_place)
    if original_coords is None or handoff_coords is None:
        return False, "PUBLIC_PLACE_COORDINATES_CHANGED"
    current_original = _candidate(original, original=original, distance_m=0)
    current_handoff = _candidate(
        handoff_place,
        original=original,
        distance_m=_distance_m(*original_coords, *handoff_coords),
    )
    try:
        old_original = json.loads(execution.original_place_snapshot_json)
        old_handoff = json.loads(execution.handoff_place_snapshot_json)
    except (TypeError, json.JSONDecodeError):
        return False, "PUBLIC_PLACE_SNAPSHOT_INVALID"
    if current_original != old_original or current_handoff != old_handoff:
        return False, "PUBLIC_PLACE_SNAPSHOT_CHANGED"
    return True, None


def _member_in_force(member: JourneyMember | None, now: datetime) -> bool:
    if member is None or member.removed_at:
        return False
    if member.expires_at is None:
        return True
    expiry = _parse_time(member.expires_at)
    return expiry is not None and expiry > now


def _execution_payload(
    session: Session,
    execution: JourneyRendezvousBuddyExecution,
    *,
    viewer_member_id: str,
    effective_status: str,
    context_current: bool,
    stale_reason: str | None,
) -> dict[str, object]:
    receipts = session.scalars(select(JourneyRendezvousBuddyExecutionReceipt).where(
        JourneyRendezvousBuddyExecutionReceipt.execution_id == execution.id,
    )).all()
    by_member = {str(item.member_id): item for item in receipts}
    helper_receipt = by_member.get(str(execution.helper_member_id))
    requester_receipt = by_member.get(str(execution.requester_member_id))
    viewer_receipt = by_member.get(str(viewer_member_id))
    partner_id = str(execution.requester_member_id) if viewer_member_id == str(execution.helper_member_id) else str(execution.helper_member_id)
    partner_receipt = by_member.get(partner_id)
    now = _now()
    expiry = _parse_time(execution.expires_at)
    proposal_expiry = _parse_time(execution.proposal_expires_at)
    has_current_window = effective_status == "ACTIVE" and context_current and expiry is not None and expiry > now
    return {
        "execution_id": str(execution.id),
        "status": effective_status,
        "execution_window_minutes": int(execution.execution_window_minutes),
        "proposed_at": str(execution.proposed_at),
        "proposal_expires_at": str(execution.proposal_expires_at),
        "started_at": str(execution.started_at) if execution.started_at else None,
        "expires_at": str(execution.expires_at) if execution.expires_at else None,
        "closed_at": str(execution.closed_at) if execution.closed_at else None,
        "outcome_code": str(execution.outcome_code) if execution.outcome_code else None,
        "helper_consented_at": str(execution.helper_consented_at),
        "requester_consented_at": str(execution.requester_consented_at) if execution.requester_consented_at else None,
        "helper_receipt_kind": str(helper_receipt.receipt_kind) if helper_receipt else None,
        "requester_receipt_kind": str(requester_receipt.receipt_kind) if requester_receipt else None,
        "viewer_receipt_kind": str(viewer_receipt.receipt_kind) if viewer_receipt else None,
        "partner_receipt_kind": str(partner_receipt.receipt_kind) if partner_receipt else None,
        "helper_confirmed": bool(helper_receipt and helper_receipt.receipt_kind == "BUDDY_MET_ACK"),
        "requester_confirmed": bool(requester_receipt and requester_receipt.receipt_kind == "BUDDY_MET_ACK"),
        "viewer_has_reported": viewer_receipt is not None,
        "context_current": bool(context_current),
        "stale_reason": stale_reason,
        "can_accept_window": False,
        "can_report": bool(has_current_window and viewer_receipt is None),
        "can_return_to_shared_stop": bool(
            context_current
            and effective_status in TERMINAL_EXECUTION_STATES - {"STALE"}
        ),
        "proposal_expired": bool(proposal_expiry is None or proposal_expiry <= now),
        "physical_arrival_verified": False,
        "shared_plan_changed": False,
    }


def _project_execution(
    session: Session,
    journey: Journey,
    handoff: JourneyRendezvousBuddyHandoff,
    viewer: JourneyMember,
) -> dict[str, object] | None:
    execution = session.scalar(select(JourneyRendezvousBuddyExecution).where(
        JourneyRendezvousBuddyExecution.handoff_id == handoff.id,
    ))
    if execution is None or str(viewer.id) not in {
        str(execution.helper_member_id), str(execution.requester_member_id)
    }:
        return None
    if execution.status == "WINDOW_OFFERED":
        from app.execution.rendezvous_buddy_handoff import _current_projection

        if handoff.status not in {"OFFERED", "ACCEPTED"} or _current_projection(
            session, journey, row=handoff, viewer=viewer
        ) is None:
            return None
        payload = _execution_payload(
            session, execution, viewer_member_id=str(viewer.id), effective_status="WINDOW_OFFERED",
            context_current=True, stale_reason=None,
        )
        payload["can_accept_window"] = bool(
            handoff.status == "ACCEPTED" and str(viewer.id) == str(execution.requester_member_id)
        )
        return payload

    context_current, reason = _execution_context_current(session, journey, handoff, execution, viewer)
    state = str(execution.status)
    expiry = _parse_time(execution.expires_at)
    if state == "ACTIVE" and (expiry is None or expiry <= _now()):
        state = "EXPIRED"
    elif state == "ACTIVE" and not context_current:
        state = "STALE"
    return _execution_payload(
        session, execution, viewer_member_id=str(viewer.id), effective_status=state,
        context_current=context_current, stale_reason=reason,
    )


def record_execution_report(
    session: Session,
    *,
    journey: Journey,
    handoff_id: str,
    execution_id: str,
    viewer: JourneyMember,
    principal: TravelerPrincipal,
    receipt_kind: str,
    idempotency_key: str,
) -> dict[str, object]:
    key = str(idempotency_key or "").strip()
    if len(key) < 8 or len(key) > 128:
        raise HTTPException(400, "BUDDY_EXECUTION_IDEMPOTENCY_KEY_REQUIRED")
    if receipt_kind not in RECEIPT_KINDS:
        raise HTTPException(422, "BUDDY_EXECUTION_RECEIPT_UNSUPPORTED")
    if str(viewer.traveler_id) != str(principal.traveler_id):
        raise HTTPException(403, "BUDDY_EXECUTION_SIGNED_ACTOR_MISMATCH")
    handoff = session.scalar(select(JourneyRendezvousBuddyHandoff).where(
        JourneyRendezvousBuddyHandoff.id == handoff_id,
        JourneyRendezvousBuddyHandoff.journey_id == journey.id,
    ))
    execution = session.scalar(select(JourneyRendezvousBuddyExecution).where(
        JourneyRendezvousBuddyExecution.id == execution_id,
        JourneyRendezvousBuddyExecution.handoff_id == handoff_id,
        JourneyRendezvousBuddyExecution.journey_id == journey.id,
    ))
    if handoff is None or execution is None or str(viewer.id) not in {
        str(execution.helper_member_id), str(execution.requester_member_id)
    }:
        raise HTTPException(404, "BUDDY_EXECUTION_NOT_FOUND")
    request_hash = _sha(json.dumps({
        "execution_id": str(execution.id), "member_id": str(viewer.id), "receipt_kind": receipt_kind,
    }, sort_keys=True, separators=(",", ":")))
    by_key = session.scalar(select(JourneyRendezvousBuddyExecutionReceipt).where(
        JourneyRendezvousBuddyExecutionReceipt.execution_id == execution.id,
        JourneyRendezvousBuddyExecutionReceipt.idempotency_key == key,
    ))
    if by_key is not None:
        if by_key.request_hash != request_hash:
            raise HTTPException(409, "BUDDY_EXECUTION_IDEMPOTENCY_KEY_REUSED")
        return _project_execution(session, journey, handoff, viewer) or {
            "execution_id": str(execution.id), "status": "STALE", "can_report": False,
        }
    existing_member_receipt = session.scalar(select(JourneyRendezvousBuddyExecutionReceipt).where(
        JourneyRendezvousBuddyExecutionReceipt.execution_id == execution.id,
        JourneyRendezvousBuddyExecutionReceipt.member_id == viewer.id,
    ))
    if existing_member_receipt is not None:
        if existing_member_receipt.receipt_kind == receipt_kind:
            return _project_execution(session, journey, handoff, viewer) or {
                "execution_id": str(execution.id), "status": "STALE", "can_report": False,
            }
        raise HTTPException(409, "BUDDY_EXECUTION_MEMBER_ALREADY_REPORTED")
    if execution.status != "ACTIVE":
        raise HTTPException(409, "BUDDY_EXECUTION_NOT_IN_FORCE")

    journey = _claim_context(session, journey, handoff)
    handoff = session.get(JourneyRendezvousBuddyHandoff, handoff_id)
    execution = session.get(JourneyRendezvousBuddyExecution, execution_id)
    assert handoff is not None and execution is not None
    context_current, reason = _execution_context_current(session, journey, handoff, execution, viewer)
    now = _now()
    expiry = _parse_time(execution.expires_at)
    if expiry is None or expiry <= now:
        execution.status = "EXPIRED"
        execution.closed_at = _iso(now)
        execution.updated_at = _iso(now)
        session.commit()
        raise HTTPException(409, "BUDDY_EXECUTION_WINDOW_EXPIRED")
    if not context_current:
        execution.status = "STALE"
        execution.closed_at = _iso(now)
        execution.outcome_code = reason or "CONTEXT_CHANGED"
        execution.updated_at = _iso(now)
        session.commit()
        raise HTTPException(409, "BUDDY_EXECUTION_CONTEXT_STALE")

    receipt = JourneyRendezvousBuddyExecutionReceipt(
        id=str(uuid4()),
        journey_id=str(journey.id),
        execution_id=str(execution.id),
        member_id=str(viewer.id),
        traveler_id=str(principal.traveler_id),
        receipt_kind=receipt_kind,
        idempotency_key=key,
        request_hash=request_hash,
        self_reported_at=_iso(now),
        created_at=_iso(now),
    )
    session.add(receipt)
    try:
        session.flush()
        if receipt_kind == "CANNOT_MEET":
            execution.status = "REPLAN_REQUIRED"
            execution.outcome_code = "PARTICIPANT_SELF_REPORTED_CANNOT_MEET"
            execution.closed_at = _iso(now)
        elif receipt_kind == "CANCEL_PARTICIPATION":
            execution.status = "CANCELLED"
            execution.outcome_code = "PARTICIPANT_CANCELLED_OWN_PARTICIPATION"
            execution.closed_at = _iso(now)
        else:
            count = session.scalar(select(func.count()).select_from(JourneyRendezvousBuddyExecutionReceipt).where(
                JourneyRendezvousBuddyExecutionReceipt.execution_id == execution.id,
            )) or 0
            if count == 2:
                pair_receipts = session.scalars(select(JourneyRendezvousBuddyExecutionReceipt).where(
                    JourneyRendezvousBuddyExecutionReceipt.execution_id == execution.id,
                )).all()
                if all(item.receipt_kind == "BUDDY_MET_ACK" for item in pair_receipts):
                    execution.status = "MUTUALLY_ACKNOWLEDGED"
                    execution.outcome_code = "MUTUALLY_ACKNOWLEDGED_ASSIST_NOT_PHYSICALLY_VERIFIED"
                    execution.closed_at = _iso(now)
        execution.updated_at = _iso(now)
        record_audit(
            session,
            action="journey.rendezvous_buddy_execution.self_reported",
            entity_type="JourneyRendezvousBuddyExecutionReceipt",
            entity_id=receipt.id,
            journey_id=journey.id,
            actor_type="AUTHENTICATED_TRAVELER",
            actor_id=principal.traveler_id,
            after={"receipt_kind": receipt_kind, "self_reported_at": receipt.self_reported_at,
                   "physical_arrival_verified": False, "shared_plan_changed": False},
        )
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        winner = session.scalar(select(JourneyRendezvousBuddyExecutionReceipt).where(
            JourneyRendezvousBuddyExecutionReceipt.execution_id == execution_id,
            JourneyRendezvousBuddyExecutionReceipt.member_id == viewer.id,
        ))
        if winner is not None and winner.receipt_kind == receipt_kind:
            execution = session.get(JourneyRendezvousBuddyExecution, execution_id)
            handoff = session.get(JourneyRendezvousBuddyHandoff, handoff_id)
            if execution is not None and handoff is not None:
                return _project_execution(session, journey, handoff, viewer) or {
                    "execution_id": str(execution.id), "status": "STALE", "can_report": False,
                }
        raise HTTPException(409, "BUDDY_EXECUTION_DUPLICATE_OR_CONCURRENT_RECEIPT") from exc
    except OperationalError as exc:
        session.rollback()
        raise HTTPException(409, "BUDDY_EXECUTION_WRITE_BUSY_RETRY") from exc
    return _project_execution(session, journey, handoff, viewer) or {
        "execution_id": str(execution.id), "status": "STALE", "can_report": False,
    }


def execution_for_current_viewer(
    session: Session,
    *,
    journey: Journey,
    handoff: JourneyRendezvousBuddyHandoff,
    viewer: JourneyMember,
) -> dict[str, object] | None:
    """Return only this handoff pair's execution projection; never group-wide."""
    return _project_execution(session, journey, handoff, viewer)


def execution_window_available_for_helper(
    session: Session,
    *,
    handoff: JourneyRendezvousBuddyHandoff,
    viewer: JourneyMember,
) -> bool:
    if handoff.status != "ACCEPTED" or str(viewer.id) != str(handoff.helper_member_id):
        return False
    return session.scalar(select(JourneyRendezvousBuddyExecution.id).where(
        JourneyRendezvousBuddyExecution.handoff_id == handoff.id,
    )) is None
