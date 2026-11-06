from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.audit import record_audit
from app.db.models import (
    Activity,
    Journey,
    JourneyIntent,
    JourneyMember,
    MemberSideQuest,
    Place,
    PlannedSplitMeetupHandshake,
    Traveler,
)
from app.execution.clock import execution_clock
from app.execution.group_presence import _member_in_force, planned_separation_for_member
from app.execution.group_recovery_followthrough import (
    active_group_recovery_spend,
    followthrough_projection,
)
from app.execution.member_side_quest import invalidate_member_side_quests_for_intents


PENDING = "PENDING"
TERMINAL_QUEST_STATES = frozenset({"COMPLETED", "CANCELLED"})
R62_WAITING_STATES = frozenset({
    "RECOVERY_APPLIED_REJOIN_PENDING",
    "REJOIN_IN_PROGRESS",
    "REJOIN_CONFIRMATION_REQUIRED",
})


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _parse_stamp(value: str | None) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or ""))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _current_context(
    session: Session,
    journey: Journey,
    owner: JourneyMember,
    *,
    now: datetime,
    record: PlannedSplitMeetupHandshake | None = None,
) -> tuple[dict, MemberSideQuest] | None:
    if (
        str(journey.status or "").upper() != "ACTIVE"
        or not journey.active_plan_version_id
        or not _member_in_force(owner, now)
    ):
        return None
    try:
        split = planned_separation_for_member(session, journey, owner, now=now)
    except HTTPException:
        return None
    if split is None:
        return None
    if (
        str(split["intent"].intent_kind) != "PLANNED_SEPARATION"
        or str(split["intent"].status) != "ACTIVE"
        or str(split["target"].plan_version_id) != str(journey.active_plan_version_id)
        or str(split["target"].status or "").upper() in {"COMPLETED", "SKIPPED", "CANCELLED"}
    ):
        return None
    quest = session.scalar(
        select(MemberSideQuest)
        .where(
            MemberSideQuest.journey_id == journey.id,
            MemberSideQuest.member_id == owner.id,
            MemberSideQuest.planned_separation_intent_id == split["intent"].id,
        )
        .order_by(MemberSideQuest.created_at.desc(), MemberSideQuest.id.desc())
    )
    if quest is None or str(quest.state) not in TERMINAL_QUEST_STATES:
        return None
    if (
        str(quest.traveler_id) != str(owner.traveler_id)
        or str(quest.active_plan_version_id) != str(journey.active_plan_version_id)
        or str(quest.rendezvous_activity_id) != str(split["target"].id)
        or str(quest.rendezvous_place_id) != str(split["place"].id)
    ):
        return None
    if record is not None and (
        str(record.journey_id) != str(journey.id)
        or str(record.owner_member_id) != str(owner.id)
        or str(record.owner_traveler_id) != str(owner.traveler_id)
        or str(record.planned_separation_intent_id) != str(split["intent"].id)
        or str(record.member_side_quest_id) != str(quest.id)
        or str(record.active_plan_version_id) != str(journey.active_plan_version_id)
        or str(record.rendezvous_activity_id) != str(split["target"].id)
        or str(record.rendezvous_place_id) != str(split["place"].id)
    ):
        return None
    return split, quest


def _require_r62_available(session: Session, journey: Journey, viewer_member_id: str) -> None:
    if active_group_recovery_spend(session, journey) is None:
        return
    projection = followthrough_projection(
        session, journey, viewer_member_id=viewer_member_id
    )
    if projection is not None and projection.get("state") in R62_WAITING_STATES:
        raise HTTPException(
            409,
            "R62_REJOIN_REQUIRED: continue in the existing group recovery arrival flow",
        )


def _r62_waiting(session: Session, journey: Journey, viewer_member_id: str) -> bool:
    if active_group_recovery_spend(session, journey) is None:
        return False
    projection = followthrough_projection(
        session, journey, viewer_member_id=viewer_member_id
    )
    return bool(projection and projection.get("state") in R62_WAITING_STATES)


def report_meetup_arrival(
    session: Session,
    *,
    journey: Journey,
    owner: JourneyMember,
    traveler_id: str,
    side_quest_id: str,
    now: datetime | None = None,
) -> PlannedSplitMeetupHandshake:
    current = (now or execution_clock.now()).astimezone(timezone.utc)
    _require_r62_available(session, journey, str(owner.id))
    context = _current_context(session, journey, owner, now=current)
    if context is None or str(context[1].id) != str(side_quest_id):
        raise HTTPException(409, "MEETUP_HANDSHAKE_REQUIRES_CURRENT_COMPLETED_SIDE_QUEST")
    if str(owner.traveler_id) != str(traveler_id):
        raise HTTPException(403, "MEETUP_HANDSHAKE_OWNER_MISMATCH")
    active_members = [
        member for member in session.scalars(
            select(JourneyMember).where(JourneyMember.journey_id == journey.id)
        ).all() if _member_in_force(member, current)
    ]
    if len(active_members) < 2:
        raise HTTPException(409, "MEETUP_HANDSHAKE_REQUIRES_CURRENT_PEER")

    rows = session.scalars(
        select(PlannedSplitMeetupHandshake)
        .where(
            PlannedSplitMeetupHandshake.journey_id == journey.id,
            PlannedSplitMeetupHandshake.owner_member_id == owner.id,
        )
        .order_by(
            PlannedSplitMeetupHandshake.reported_at.desc(),
            PlannedSplitMeetupHandshake.id.desc(),
        )
    ).all()
    now_text = _stamp(current)
    for row in rows:
        if row.state != PENDING:
            continue
        expiry = _parse_stamp(row.expires_at)
        if expiry is None or expiry <= current:
            row.state = "EXPIRED"
            row.updated_at = now_text
            continue
        if _current_context(session, journey, owner, now=current, record=row) is not None:
            session.commit()
            return row
        row.state = "INVALIDATED"
        row.updated_at = now_text

    # Clear any expired/stale pending row before inserting its replacement so
    # the partial unique index remains the final concurrency guard.
    session.flush()
    handshake = PlannedSplitMeetupHandshake(
        id=str(uuid4()),
        journey_id=str(journey.id),
        owner_member_id=str(owner.id),
        owner_traveler_id=str(traveler_id),
        planned_separation_intent_id=str(context[0]["intent"].id),
        member_side_quest_id=str(context[1].id),
        active_plan_version_id=str(journey.active_plan_version_id),
        rendezvous_activity_id=str(context[0]["target"].id),
        rendezvous_place_id=str(context[0]["place"].id),
        state=PENDING,
        reported_at=now_text,
        expires_at=_stamp(current + timedelta(minutes=30)),
        responder_member_id=None,
        responder_traveler_id=None,
        decided_at=None,
        created_at=now_text,
        updated_at=now_text,
    )
    session.add(handshake)
    record_audit(
        session,
        action="journey.planned_split_meetup.reported",
        entity_type="PlannedSplitMeetupHandshake",
        entity_id=handshake.id,
        journey_id=journey.id,
        actor_type="TRAVELER",
        actor_id=str(traveler_id),
        after={
            "owner_member_id": str(owner.id),
            "planned_separation_intent_id": handshake.planned_separation_intent_id,
            "rendezvous_activity_id": handshake.rendezvous_activity_id,
            "rendezvous_place_id": handshake.rendezvous_place_id,
            "state": PENDING,
            "result": "MEETUP_REPORTED",
            "physical_presence_verified": False,
            "expires_at": handshake.expires_at,
        },
    )
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        winner = session.scalar(
            select(PlannedSplitMeetupHandshake).where(
                PlannedSplitMeetupHandshake.journey_id == journey.id,
                PlannedSplitMeetupHandshake.owner_member_id == owner.id,
                PlannedSplitMeetupHandshake.state == PENDING,
            )
        )
        if winner is None:
            raise
        return winner
    return handshake


def decide_meetup_handshake(
    session: Session,
    *,
    journey: Journey,
    responder: JourneyMember,
    traveler_id: str,
    handshake_id: str,
    acknowledge: bool,
    now: datetime | None = None,
) -> PlannedSplitMeetupHandshake:
    current = (now or execution_clock.now()).astimezone(timezone.utc)
    record = session.scalar(
        select(PlannedSplitMeetupHandshake).where(
            PlannedSplitMeetupHandshake.id == str(handshake_id),
            PlannedSplitMeetupHandshake.journey_id == journey.id,
        )
    )
    if record is None:
        raise HTTPException(404, "MEETUP_HANDSHAKE_NOT_FOUND")
    if (
        not _member_in_force(responder, current)
        or str(responder.traveler_id) != str(traveler_id)
        or str(responder.id) == str(record.owner_member_id)
    ):
        raise HTTPException(403, "MEETUP_HANDSHAKE_REQUIRES_DIFFERENT_CURRENT_MEMBER")
    desired = "PEER_ACKNOWLEDGED" if acknowledge else "NOT_CONFIRMED"
    if record.state in {"PEER_ACKNOWLEDGED", "NOT_CONFIRMED"}:
        if (
            str(record.state) == desired
            and str(record.responder_member_id or "") == str(responder.id)
        ):
            return record
        raise HTTPException(409, "MEETUP_HANDSHAKE_ALREADY_DECIDED")
    if record.state != PENDING:
        raise HTTPException(409, f"MEETUP_HANDSHAKE_NOT_PENDING:{record.state}")
    expiry = _parse_stamp(record.expires_at)
    if expiry is None or expiry <= current:
        session.execute(
            update(PlannedSplitMeetupHandshake)
            .where(
                PlannedSplitMeetupHandshake.id == record.id,
                PlannedSplitMeetupHandshake.state == PENDING,
            )
            .values(state="EXPIRED", updated_at=_stamp(current))
        )
        session.commit()
        raise HTTPException(409, "MEETUP_HANDSHAKE_EXPIRED")
    owner = session.get(JourneyMember, str(record.owner_member_id))
    if owner is None or _current_context(
        session, journey, owner, now=current, record=record
    ) is None:
        session.execute(
            update(PlannedSplitMeetupHandshake)
            .where(
                PlannedSplitMeetupHandshake.id == record.id,
                PlannedSplitMeetupHandshake.state == PENDING,
            )
            .values(state="INVALIDATED", updated_at=_stamp(current))
        )
        session.commit()
        raise HTTPException(409, "MEETUP_HANDSHAKE_BINDING_STALE")
    _require_r62_available(session, journey, str(responder.id))

    now_text = _stamp(current)
    result = session.execute(
        update(PlannedSplitMeetupHandshake)
        .where(
            PlannedSplitMeetupHandshake.id == record.id,
            PlannedSplitMeetupHandshake.state == PENDING,
            PlannedSplitMeetupHandshake.expires_at > now_text,
        )
        .values(
            state=desired,
            responder_member_id=str(responder.id),
            responder_traveler_id=str(traveler_id),
            decided_at=now_text,
            updated_at=now_text,
        )
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        session.rollback()
        latest = session.get(PlannedSplitMeetupHandshake, record.id)
        if (
            latest is not None
            and str(latest.state) == desired
            and str(latest.responder_member_id or "") == str(responder.id)
        ):
            return latest
        raise HTTPException(409, "MEETUP_HANDSHAKE_ALREADY_DECIDED")

    record.state = desired
    record.responder_member_id = str(responder.id)
    record.responder_traveler_id = str(traveler_id)
    record.decided_at = now_text
    record.updated_at = now_text
    if acknowledge:
        intent = session.get(JourneyIntent, str(record.planned_separation_intent_id))
        if (
            intent is None
            or str(intent.journey_id) != str(journey.id)
            or str(intent.created_by_member_id) != str(owner.id)
            or str(intent.status) != "ACTIVE"
        ):
            session.rollback()
            raise HTTPException(409, "MEETUP_HANDSHAKE_SPLIT_NO_LONGER_ACTIVE")
        intent.status = "CANCELLED"
        intent.cancelled_at = now_text
        intent.updated_at = now_text
        intent.evaluation_state = "PLANNED_SPLIT_ENDED_AFTER_PEER_ACKNOWLEDGMENT"
        invalidate_member_side_quests_for_intents(
            session,
            journey=journey,
            intent_ids=[str(intent.id)],
            reason_code="R69_PEER_ACKNOWLEDGED_REPORTED_ARRIVAL",
            now=current,
        )
    responder_traveler = session.get(Traveler, str(traveler_id))
    record_audit(
        session,
        action=f"journey.planned_split_meetup.{('peer_acknowledged' if acknowledge else 'not_confirmed')}",
        entity_type="PlannedSplitMeetupHandshake",
        entity_id=record.id,
        journey_id=journey.id,
        actor_type="TRAVELER",
        actor_id=str(traveler_id),
        after={
            "owner_member_id": str(owner.id),
            "responder_member_id": str(responder.id),
            "responder_display_name": responder_traveler.display_name if responder_traveler else None,
            "rendezvous_activity_id": str(record.rendezvous_activity_id),
            "rendezvous_place_id": str(record.rendezvous_place_id),
            "state": desired,
            "result": "USER_REPORTED_REJOIN_ACKNOWLEDGED" if acknowledge else "NOT_CONFIRMED",
            "planned_split_state": "PLANNED_SPLIT_ENDED" if acknowledge else None,
            "ended_owner_split": bool(acknowledge),
            "physical_presence_verified": False,
            "shared_plan_changed": False,
        },
    )
    session.commit()
    session.refresh(record)
    return record


def meetup_handshake_projection(
    session: Session,
    journey: Journey,
    *,
    viewer_member_id: str | None,
    group_presence: dict,
    now: datetime | None = None,
) -> dict | None:
    if not viewer_member_id:
        return None
    current = (now or execution_clock.now()).astimezone(timezone.utc)
    viewer = session.get(JourneyMember, str(viewer_member_id))
    if viewer is None or not _member_in_force(viewer, current):
        return None
    members = {
        str(item.get("member_id")): item
        for item in group_presence.get("members", [])
        if isinstance(item, dict) and item.get("member_id")
    }
    rows = session.scalars(
        select(PlannedSplitMeetupHandshake)
        .where(PlannedSplitMeetupHandshake.journey_id == journey.id)
        .order_by(
            PlannedSplitMeetupHandshake.reported_at.desc(),
            PlannedSplitMeetupHandshake.id.desc(),
        )
    ).all()
    owner_payload = None
    viewer_context = _current_context(session, journey, viewer, now=current)
    # A split can be active before its new side quest reaches a terminal state.
    # Do not revive the last split's successful meetup in that window.
    active_viewer_split = planned_separation_for_member(
        session, journey, viewer, now=current
    )
    # A completed handshake belongs to its old split/quest. If this member
    # starts a fresh planned split, the new meetup must have a fresh action
    # entry point rather than inheriting "PEER_ACKNOWLEDGED" from last time.
    owner_rows = [
        row for row in rows if str(row.owner_member_id) == str(viewer.id)
    ]
    if viewer_context is not None:
        current_split, current_quest = viewer_context
        own = next(
            (
                row for row in owner_rows
                if str(row.planned_separation_intent_id) == str(current_split["intent"].id)
                and str(row.member_side_quest_id) == str(current_quest.id)
                and str(row.active_plan_version_id) == str(journey.active_plan_version_id)
                and str(row.rendezvous_activity_id) == str(current_split["target"].id)
                and str(row.rendezvous_place_id) == str(current_split["place"].id)
            ),
            None,
        )
    else:
        # After the split ends, retain the last result as history. An active
        # new split (even one without a finished side quest) takes precedence.
        own = owner_rows[0] if owner_rows and active_viewer_split is None else None
    if own is not None:
        display_state = str(own.state)
        owner = session.get(JourneyMember, str(own.owner_member_id))
        context = (
            _current_context(session, journey, owner, now=current, record=own)
            if owner is not None
            else None
        )
        expiry = _parse_stamp(own.expires_at)
        if display_state == PENDING:
            if expiry is None or expiry <= current:
                display_state = "EXPIRED"
            elif context is None:
                display_state = "INVALIDATED"
        r62_waiting = bool(context and _r62_waiting(session, journey, str(viewer.id)))
        responder = session.get(Traveler, str(own.responder_traveler_id)) if own.responder_traveler_id else None
        rendezvous_activity = session.get(Activity, str(own.rendezvous_activity_id))
        rendezvous_place = session.get(Place, str(own.rendezvous_place_id))
        owner_payload = {
            "id": str(own.id),
            "owner_member_id": str(own.owner_member_id),
            "state": display_state,
            "result": (
                "USER_REPORTED_REJOIN_ACKNOWLEDGED"
                if display_state == "PEER_ACKNOWLEDGED"
                else "MEETUP_REPORTED"
                if display_state == PENDING
                else None
            ),
            "planned_split_state": "PLANNED_SPLIT_ENDED" if display_state == "PEER_ACKNOWLEDGED" else None,
            "reported_at": own.reported_at,
            "expires_at": own.expires_at,
            "rendezvous": {
                "title": context[0]["target"].title if context else rendezvous_activity.title if rendezvous_activity else None,
                "place_name": context[0]["place"].name if context else rendezvous_place.name if rendezvous_place else None,
            },
            "responder_display_name": responder.display_name if responder else None,
            "physical_presence_verified": False,
            "shared_plan_changed": False,
            "can_report": bool(
                context
                and len(members) >= 2
                and display_state in {"EXPIRED", "INVALIDATED", "NOT_CONFIRMED"}
                and not r62_waiting
            ),
            "blocked_reason": (
                "R62_REJOIN_REQUIRED"
                if r62_waiting
                else "NO_PEER_AVAILABLE"
                if context and len(members) < 2
                else None
            ),
        }
    elif viewer_context is not None:
        r62_waiting = _r62_waiting(session, journey, str(viewer.id))
        owner_payload = {
            "id": None,
            "owner_member_id": str(viewer.id),
            "state": "NOT_REPORTED",
            "result": None,
            "planned_split_state": None,
            "reported_at": None,
            "expires_at": None,
            "rendezvous": {
                "title": str(viewer_context[0]["target"].title),
                "place_name": str(viewer_context[0]["place"].name),
            },
            "responder_display_name": None,
            "physical_presence_verified": False,
            "shared_plan_changed": False,
            "can_report": bool(len(members) >= 2 and not r62_waiting),
            "blocked_reason": (
                "R62_REJOIN_REQUIRED"
                if r62_waiting
                else "NO_PEER_AVAILABLE"
                if len(members) < 2
                else None
            ),
        }

    incoming = []
    resolved = []
    resolved_owners: set[str] = set()
    for row in rows:
        owner_id = str(row.owner_member_id)
        if owner_id != str(viewer.id) and owner_id not in resolved_owners and str(row.state) == "PEER_ACKNOWLEDGED":
            owner_member = session.get(JourneyMember, owner_id)
            owner_traveler = session.get(Traveler, str(row.owner_traveler_id))
            responder_traveler = session.get(Traveler, str(row.responder_traveler_id)) if row.responder_traveler_id else None
            rendezvous_activity = session.get(Activity, str(row.rendezvous_activity_id))
            rendezvous_place = session.get(Place, str(row.rendezvous_place_id))
            if (
                owner_member is not None
                and _member_in_force(owner_member, current)
                and rendezvous_activity is not None
                and rendezvous_place is not None
            ):
                resolved.append({
                    "id": str(row.id),
                    "owner_display_name": owner_traveler.display_name if owner_traveler else "Traveller",
                    "responder_display_name": responder_traveler.display_name if responder_traveler else "Traveller",
                    # A third traveller viewing this group history did NOT
                    # countersign. The UI must not say "you and Alice met"
                    # to someone who has never acknowledged the request.
                    "viewer_acknowledged": str(row.responder_member_id or "") == str(viewer.id),
                    "rendezvous": {"title": rendezvous_activity.title, "place_name": rendezvous_place.name},
                    "reported_at": row.reported_at,
                    "decided_at": row.decided_at,
                    "state": "PEER_ACKNOWLEDGED",
                    "result": "USER_REPORTED_REJOIN_ACKNOWLEDGED",
                    "planned_split_state": "PLANNED_SPLIT_ENDED",
                    "physical_presence_verified": False,
                    "shared_plan_changed": False,
                })
                resolved_owners.add(owner_id)
        if str(row.owner_member_id) == str(viewer.id) or str(row.state) != PENDING:
            continue
        expiry = _parse_stamp(row.expires_at)
        if expiry is None or expiry <= current:
            continue
        owner = session.get(JourneyMember, str(row.owner_member_id))
        if owner is None or str(owner.id) not in members:
            continue
        context = _current_context(session, journey, owner, now=current, record=row)
        if context is None:
            continue
        traveler = session.get(Traveler, owner.traveler_id)
        incoming.append({
            "id": str(row.id),
            "owner_display_name": traveler.display_name if traveler else "Traveller",
            "reported_at": row.reported_at,
            "expires_at": row.expires_at,
            "rendezvous": {
                "title": str(context[0]["target"].title),
                "place_name": str(context[0]["place"].name),
            },
            "physical_presence_verified": False,
        })
    return {"owner": owner_payload, "incoming": incoming, "resolved": resolved}
