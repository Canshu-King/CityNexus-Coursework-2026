"""Durable, member-owned coordination signals for one current shared stop.

These are self-reported intentions, not location, route, arrival, or risk evidence.
Every write is bound to the signed actor, active plan/cursor, exact public Place,
and the member audience that was present when the actor consented to share.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.core.audit import record_audit
from app.db.models import (
    Journey,
    JourneyMember,
    JourneyRendezvousMemberSignal,
    JourneyRendezvousSignalAcknowledgement,
    Traveler,
)
from app.execution.clock import execution_clock
from app.execution.group_presence import planned_separation_for_member, rendezvous_target
from app.execution.group_recovery_followthrough import active_group_recovery_spend
from app.traveler_principal import TravelerPrincipal

SIGNAL_KINDS = {"HEADING_TO_RENDEZVOUS", "NEEDS_COORDINATION"}
SIGNAL_TTL = timedelta(minutes=45)


def _in_force(member: JourneyMember, now: datetime) -> bool:
    if member.removed_at:
        return False
    if not member.expires_at:
        return True
    try:
        expiry = datetime.fromisoformat(member.expires_at)
    except ValueError:
        return False
    return expiry.tzinfo is not None and expiry.astimezone(timezone.utc) > now


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _claim_journey_revision(
    session: Session,
    *,
    journey_id: str,
    plan_version_id: str,
    current_activity_id: str | None,
) -> Journey:
    """Take a database write lock and compare the visible plan/cursor in one CAS."""
    statement = update(Journey).where(
        Journey.id == journey_id,
        Journey.status == "ACTIVE",
        Journey.active_plan_version_id == plan_version_id,
    )
    if current_activity_id is None:
        statement = statement.where(Journey.current_activity_id.is_(None))
    else:
        statement = statement.where(Journey.current_activity_id == current_activity_id)
    try:
        result = session.execute(
            statement.values(updated_at=Journey.updated_at).execution_options(
                synchronize_session=False
            )
        )
    except OperationalError as exc:
        session.rollback()
        raise HTTPException(409, "RENDEZVOUS_SIGNAL_WRITE_BUSY_RETRY") from exc
    if result.rowcount != 1:
        session.rollback()
        raise HTTPException(409, "RENDEZVOUS_SIGNAL_CONTEXT_CHANGED")
    session.expire_all()
    journey = session.get(Journey, journey_id)
    if journey is None:
        session.rollback()
        raise HTTPException(404, "JOURNEY_NOT_FOUND")
    return journey


def _current_context(
    session: Session,
    journey: Journey,
    member: JourneyMember,
    *,
    expected_plan_version_id: str | None = None,
    expected_current_activity_id: str | None = None,
    expected_rendezvous_activity_id: str | None = None,
    expected_public_place_id: str | None = None,
) -> tuple[list[JourneyMember], object, object, datetime]:
    now = execution_clock.now().astimezone(timezone.utc)
    if str(journey.status).upper() != "ACTIVE" or not journey.active_plan_version_id:
        raise HTTPException(409, "RENDEZVOUS_SIGNAL_REQUIRES_ACTIVE_JOURNEY")
    members = [
        item for item in session.scalars(
            select(JourneyMember).where(JourneyMember.journey_id == journey.id)
        ).all() if _in_force(item, now)
    ]
    member_ids = {str(item.id) for item in members}
    if str(member.id) not in member_ids:
        raise HTTPException(403, "RENDEZVOUS_SIGNAL_MEMBER_NOT_IN_FORCE")
    if len(members) < 2:
        raise HTTPException(409, "RENDEZVOUS_SIGNAL_COMPANIONS_UNAVAILABLE")
    if planned_separation_for_member(session, journey, member, now=now) is not None:
        raise HTTPException(409, "RENDEZVOUS_SIGNAL_PLANNED_SPLIT_TARGET_UNSUPPORTED")
    if active_group_recovery_spend(session, journey) is not None:
        raise HTTPException(409, "RENDEZVOUS_SIGNAL_GROUP_RECOVERY_FOLLOWTHROUGH_ACTIVE")
    pair = rendezvous_target(session, journey)
    if pair is None:
        raise HTTPException(409, "RENDEZVOUS_SIGNAL_TARGET_UNAVAILABLE")
    activity, place = pair
    if expected_plan_version_id is not None and str(journey.active_plan_version_id) != expected_plan_version_id:
        raise HTTPException(409, "RENDEZVOUS_SIGNAL_CONTEXT_CHANGED")
    if str(expected_current_activity_id or "") != str(journey.current_activity_id or ""):
        raise HTTPException(409, "RENDEZVOUS_SIGNAL_CONTEXT_CHANGED")
    if expected_rendezvous_activity_id is not None and str(activity.id) != expected_rendezvous_activity_id:
        raise HTTPException(409, "RENDEZVOUS_SIGNAL_TARGET_CHANGED")
    if expected_public_place_id is not None and str(place.id) != expected_public_place_id:
        raise HTTPException(409, "RENDEZVOUS_SIGNAL_TARGET_CHANGED")
    return members, activity, place, now


def _audience_ids_json(members: list[JourneyMember]) -> str:
    return json.dumps(sorted(str(member.id) for member in members), separators=(",", ":"))


def _parse_audience(value: str) -> list[str] | None:
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(parsed, list) or any(not isinstance(item, str) for item in parsed):
        return None
    return sorted(set(parsed))


def _row_is_current(
    signal: JourneyRendezvousMemberSignal,
    *,
    journey: Journey,
    members: list[JourneyMember],
    activity_id: str,
    place_id: str,
    now: datetime,
) -> bool:
    try:
        expiry = datetime.fromisoformat(signal.expires_at)
    except ValueError:
        return False
    return bool(
        signal.status == "ACTIVE"
        and expiry.tzinfo is not None
        and expiry.astimezone(timezone.utc) > now
        and str(signal.active_plan_version_id) == str(journey.active_plan_version_id or "")
        and str(signal.current_activity_id or "") == str(journey.current_activity_id or "")
        and str(signal.rendezvous_activity_id) == activity_id
        and str(signal.public_place_id) == place_id
        and _parse_audience(signal.audience_member_ids_json)
        == sorted(str(member.id) for member in members)
    )


def _receipt(signal: JourneyRendezvousMemberSignal) -> dict[str, object]:
    return {
        "signal_id": str(signal.id),
        "signal_kind": str(signal.signal_kind),
        "status": str(signal.status),
        "reported_at": str(signal.reported_at),
        "expires_at": str(signal.expires_at),
        "rendezvous_activity_id": str(signal.rendezvous_activity_id),
        "public_place_id": str(signal.public_place_id),
        "evidence_level": "SELF_REPORTED_NOT_VERIFIED",
    }


def share_member_signal(
    session: Session,
    *,
    journey: Journey,
    member: JourneyMember,
    principal: TravelerPrincipal,
    signal_kind: str,
    expected_plan_version_id: str,
    expected_current_activity_id: str | None,
    expected_rendezvous_activity_id: str,
    expected_public_place_id: str,
    expected_audience_sha256: str,
    idempotency_key: str,
) -> dict[str, object]:
    if signal_kind not in SIGNAL_KINDS:
        raise HTTPException(422, "RENDEZVOUS_SIGNAL_KIND_UNSUPPORTED")
    key = str(idempotency_key or "").strip()
    if len(key) < 8 or len(key) > 128:
        raise HTTPException(400, "RENDEZVOUS_SIGNAL_IDEMPOTENCY_KEY_REQUIRED")
    journey = _claim_journey_revision(
        session,
        journey_id=str(journey.id),
        plan_version_id=expected_plan_version_id,
        current_activity_id=expected_current_activity_id,
    )
    members, activity, place, now = _current_context(
        session,
        journey,
        member,
        expected_plan_version_id=expected_plan_version_id,
        expected_current_activity_id=expected_current_activity_id,
        expected_rendezvous_activity_id=expected_rendezvous_activity_id,
        expected_public_place_id=expected_public_place_id,
    )
    audience_json = _audience_ids_json(members)
    audience_sha256 = hashlib.sha256(audience_json.encode("utf-8")).hexdigest()
    if audience_sha256 != expected_audience_sha256:
        raise HTTPException(409, "RENDEZVOUS_SIGNAL_AUDIENCE_CHANGED")
    request_facts = {
        "signal_kind": signal_kind,
        "plan": expected_plan_version_id,
        "cursor": expected_current_activity_id,
        "activity": str(activity.id),
        "place": str(place.id),
        "audience_sha256": audience_sha256,
    }
    request_hash = hashlib.sha256(
        json.dumps(request_facts, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    prior = session.scalar(select(JourneyRendezvousMemberSignal).where(
        JourneyRendezvousMemberSignal.journey_id == journey.id,
        JourneyRendezvousMemberSignal.journey_member_id == member.id,
        JourneyRendezvousMemberSignal.idempotency_key == key,
    ))
    if prior is not None:
        if prior.request_hash != request_hash:
            raise HTTPException(409, "RENDEZVOUS_SIGNAL_IDEMPOTENCY_KEY_REUSED")
        if _row_is_current(
            prior, journey=journey, members=members, activity_id=str(activity.id),
            place_id=str(place.id), now=now,
        ):
            session.commit()
            return _receipt(prior)
        raise HTTPException(409, "RENDEZVOUS_SIGNAL_IDEMPOTENCY_KEY_RETIRED")

    active_rows = session.scalars(select(JourneyRendezvousMemberSignal).where(
        JourneyRendezvousMemberSignal.journey_id == journey.id,
        JourneyRendezvousMemberSignal.journey_member_id == member.id,
        JourneyRendezvousMemberSignal.status == "ACTIVE",
    )).all()
    for old in active_rows:
        is_current = _row_is_current(
            old, journey=journey, members=members, activity_id=str(activity.id),
            place_id=str(place.id), now=now,
        )
        try:
            old_expiry = datetime.fromisoformat(old.expires_at)
        except ValueError:
            old_expiry = now
        old.status = (
            "EXPIRED" if old_expiry.tzinfo is not None and old_expiry.astimezone(timezone.utc) <= now
            else "REPLACED" if is_current
            else "STALE"
        )
        old.revoked_at = _iso(now) if old.status in {"EXPIRED", "STALE"} else old.revoked_at
        old.stale_at = _iso(now) if old.status == "STALE" else old.stale_at
        old.updated_at = _iso(now)
    session.flush()
    signal = JourneyRendezvousMemberSignal(
        id=str(uuid4()),
        journey_id=str(journey.id),
        journey_member_id=str(member.id),
        traveler_id=str(principal.traveler_id),
        active_plan_version_id=str(journey.active_plan_version_id),
        current_activity_id=str(journey.current_activity_id) if journey.current_activity_id else None,
        rendezvous_activity_id=str(activity.id),
        public_place_id=str(place.id),
        signal_kind=signal_kind,
        status="ACTIVE",
        audience_member_ids_json=audience_json,
        idempotency_key=key,
        request_hash=request_hash,
        reported_at=_iso(now),
        expires_at=_iso(now + SIGNAL_TTL),
        revoked_at=None,
        stale_at=None,
        created_at=_iso(now),
        updated_at=_iso(now),
    )
    session.add(signal)
    record_audit(
        session,
        action="journey.rendezvous_signal.shared",
        entity_type="JourneyRendezvousMemberSignal",
        entity_id=signal.id,
        journey_id=journey.id,
        actor_type="AUTHENTICATED_TRAVELER",
        actor_id=principal.traveler_id,
        after={
            "signal_kind": signal_kind,
            "plan_version_id": str(journey.active_plan_version_id),
            "rendezvous_activity_id": str(activity.id),
            "public_place_id": str(place.id),
            "audience_member_count": len(members),
            "audience_sha256": audience_sha256,
            "evidence_level": "SELF_REPORTED_NOT_VERIFIED",
        },
    )
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        winner = session.scalar(select(JourneyRendezvousMemberSignal).where(
            JourneyRendezvousMemberSignal.journey_id == str(journey.id),
            JourneyRendezvousMemberSignal.journey_member_id == str(member.id),
            JourneyRendezvousMemberSignal.idempotency_key == key,
        ))
        if winner is not None and winner.request_hash == request_hash and winner.status == "ACTIVE":
            return _receipt(winner)
        raise HTTPException(409, "RENDEZVOUS_SIGNAL_CONCURRENT_UPDATE_RETRY") from exc
    except OperationalError as exc:
        session.rollback()
        raise HTTPException(409, "RENDEZVOUS_SIGNAL_WRITE_BUSY_RETRY") from exc
    return _receipt(signal)


def _lock_journey_for_withdraw(session: Session, journey_id: str) -> None:
    """Serialize withdrawal with share/ack writes even across SQLite workers.

    Unlike sharing, a historic status may be revoked after the Journey stops
    being ACTIVE. Lock the same Journey row without imposing an ACTIVE plan
    requirement; reload the signal *after* taking that write lock.
    """
    try:
        result = session.execute(
            update(Journey).where(Journey.id == journey_id).values(
                updated_at=Journey.updated_at
            ).execution_options(synchronize_session=False)
        )
    except OperationalError as exc:
        session.rollback()
        raise HTTPException(409, "RENDEZVOUS_SIGNAL_WRITE_BUSY_RETRY") from exc
    if result.rowcount != 1:
        session.rollback()
        raise HTTPException(404, "JOURNEY_NOT_FOUND")
    session.expire_all()


def withdraw_member_signal(
    session: Session,
    *,
    journey: Journey,
    member: JourneyMember,
    principal: TravelerPrincipal,
    signal_id: str,
) -> dict[str, object]:
    journey_id = str(journey.id)
    member_id = str(member.id)
    _lock_journey_for_withdraw(session, journey_id)
    # The signed principal was resolved at the route boundary. Its membership
    # must still be current after cross-worker serialization and refresh.
    current_member = session.get(JourneyMember, member_id)
    if (
        current_member is None
        or str(current_member.journey_id) != journey_id
        or str(current_member.traveler_id) != str(principal.traveler_id)
        or not _in_force(current_member, execution_clock.now().astimezone(timezone.utc))
    ):
        session.rollback()
        raise HTTPException(403, "RENDEZVOUS_SIGNAL_MEMBER_NOT_IN_FORCE")
    signal = session.scalar(select(JourneyRendezvousMemberSignal).where(
        JourneyRendezvousMemberSignal.id == signal_id,
        JourneyRendezvousMemberSignal.journey_id == journey_id,
        JourneyRendezvousMemberSignal.journey_member_id == member_id,
    ))
    if signal is None:
        raise HTTPException(404, "RENDEZVOUS_SIGNAL_NOT_FOUND")
    # An old tab may still hold a replaced/expired signal ID after a newer
    # status was shared. Returning 200 for that old ID would misleadingly tell
    # the client that the currently visible status had been withdrawn.
    if signal.status not in {"ACTIVE", "REVOKED"}:
        raise HTTPException(409, "RENDEZVOUS_SIGNAL_WITHDRAW_TARGET_STALE")
    if signal.status == "REVOKED":
        # A retry after a prior withdrawal is idempotent only while a newer
        # active share has NOT superseded the user's "stop sharing" intent.
        successor = session.scalar(select(JourneyRendezvousMemberSignal).where(
            JourneyRendezvousMemberSignal.journey_id == journey.id,
            JourneyRendezvousMemberSignal.journey_member_id == member.id,
            JourneyRendezvousMemberSignal.status == "ACTIVE",
        ))
        if successor is not None:
            raise HTTPException(409, "RENDEZVOUS_SIGNAL_WITHDRAW_TARGET_STALE")
    if signal.status == "ACTIVE":
        now = _iso(execution_clock.now().astimezone(timezone.utc))
        signal.status = "REVOKED"
        signal.revoked_at = now
        signal.updated_at = now
        record_audit(
            session,
            action="journey.rendezvous_signal.withdrawn",
            entity_type="JourneyRendezvousMemberSignal",
            entity_id=signal.id,
            journey_id=journey.id,
            actor_type="AUTHENTICATED_TRAVELER",
            actor_id=principal.traveler_id,
            after={"signal_kind": signal.signal_kind, "revoked_at": now},
        )
        session.commit()
    return _receipt(signal)


def acknowledge_help_request(
    session: Session,
    *,
    journey: Journey,
    responder: JourneyMember,
    principal: TravelerPrincipal,
    signal_id: str,
) -> dict[str, object]:
    signal = session.scalar(select(JourneyRendezvousMemberSignal).where(
        JourneyRendezvousMemberSignal.id == signal_id,
        JourneyRendezvousMemberSignal.journey_id == journey.id,
    ))
    if signal is None:
        raise HTTPException(404, "RENDEZVOUS_SIGNAL_NOT_FOUND")
    if signal.signal_kind != "NEEDS_COORDINATION":
        raise HTTPException(409, "RENDEZVOUS_SIGNAL_IS_NOT_A_HELP_REQUEST")
    journey = _claim_journey_revision(
        session,
        journey_id=str(journey.id),
        plan_version_id=str(signal.active_plan_version_id),
        current_activity_id=str(signal.current_activity_id or "") or None,
    )
    members, activity, place, now = _current_context(
        session,
        journey,
        responder,
        expected_plan_version_id=str(signal.active_plan_version_id),
        expected_current_activity_id=str(signal.current_activity_id or ""),
        expected_rendezvous_activity_id=str(signal.rendezvous_activity_id),
        expected_public_place_id=str(signal.public_place_id),
    )
    if not _row_is_current(
        signal,
        journey=journey,
        members=members,
        activity_id=str(activity.id),
        place_id=str(place.id),
        now=now,
    ):
        raise HTTPException(409, "RENDEZVOUS_HELP_REQUEST_STALE")
    if str(responder.id) == str(signal.journey_member_id):
        raise HTTPException(409, "RENDEZVOUS_HELP_REQUESTER_CANNOT_ACKNOWLEDGE_OWN_REQUEST")
    audience = _parse_audience(signal.audience_member_ids_json) or []
    if str(responder.id) not in audience:
        raise HTTPException(403, "RENDEZVOUS_HELP_ACKNOWLEDGER_OUTSIDE_AUDIENCE")
    existing = session.scalar(select(JourneyRendezvousSignalAcknowledgement).where(
        JourneyRendezvousSignalAcknowledgement.signal_id == signal.id,
    ))
    if existing is not None:
        if str(existing.responder_member_id) == str(responder.id):
            session.commit()
            return _ack_receipt(session, existing)
        raise HTTPException(409, "RENDEZVOUS_HELP_REQUEST_ALREADY_ACKNOWLEDGED")
    ack = JourneyRendezvousSignalAcknowledgement(
        id=str(uuid4()),
        signal_id=str(signal.id),
        journey_id=str(journey.id),
        requester_member_id=str(signal.journey_member_id),
        responder_member_id=str(responder.id),
        responder_traveler_id=str(principal.traveler_id),
        acknowledged_at=_iso(now),
        created_at=_iso(now),
    )
    session.add(ack)
    record_audit(
        session,
        action="journey.rendezvous_signal.help_acknowledged",
        entity_type="JourneyRendezvousMemberSignal",
        entity_id=signal.id,
        journey_id=journey.id,
        actor_type="AUTHENTICATED_TRAVELER",
        actor_id=principal.traveler_id,
        after={
            "signal_id": str(signal.id),
            "requester_member_id": str(signal.journey_member_id),
            "responder_member_id": str(responder.id),
            "acknowledged_at": _iso(now),
            "promise_of_physical_presence": False,
        },
    )
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        winner = session.scalar(select(JourneyRendezvousSignalAcknowledgement).where(
            JourneyRendezvousSignalAcknowledgement.signal_id == signal.id,
        ))
        if winner is not None and str(winner.responder_member_id) == str(responder.id):
            return _ack_receipt(session, winner)
        raise HTTPException(409, "RENDEZVOUS_HELP_REQUEST_ALREADY_ACKNOWLEDGED") from exc
    except OperationalError as exc:
        session.rollback()
        raise HTTPException(409, "RENDEZVOUS_SIGNAL_WRITE_BUSY_RETRY") from exc
    return _ack_receipt(session, ack)


def _ack_receipt(
    session: Session, acknowledgement: JourneyRendezvousSignalAcknowledgement
) -> dict[str, object]:
    responder = session.get(JourneyMember, str(acknowledgement.responder_member_id))
    traveler = session.get(Traveler, str(acknowledgement.responder_traveler_id))
    return {
        "signal_id": str(acknowledgement.signal_id),
        "acknowledged_at": str(acknowledgement.acknowledged_at),
        "responder_member_id": str(acknowledgement.responder_member_id),
        "responder_display_name": traveler.display_name if traveler else "Traveller",
        "evidence_level": "SIGNED_ACKNOWLEDGEMENT_NOT_PHYSICAL_PRESENCE",
    }
