from __future__ import annotations

import math
import unicodedata
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.audit import record_audit
from app.db.models import (
    Journey,
    JourneyMember,
    LocationObservation,
    MemberSideQuest,
    MemberSideQuestReturnReport,
    Place,
)
from app.concurrency_guards import journey_write_lock
from app.execution.clock import execution_clock
from app.execution.group_presence import (
    LOCATION_FRESHNESS_SECONDS,
    MAX_USEFUL_ACCURACY_M,
    planned_separation_for_member,
    rendezvous_commitment_risk,
)
from app.execution.route_session_contract import fact_is_eligible_for_live_readiness


OPEN_STATES = frozenset({"DESTINATION_UNRESOLVED", "READY", "EN_ROUTE", "ARRIVED"})
TERMINAL_STATES = frozenset({"COMPLETED", "CANCELLED", "INVALIDATED"})
SAME_STATE_ACTIONS = {
    "START": "EN_ROUTE",
    "ARRIVE": "ARRIVED",
    "COMPLETE": "COMPLETED",
    "CANCEL": "CANCELLED",
}


def _stamp(now: datetime | None = None) -> str:
    return (now or execution_clock.now()).astimezone(timezone.utc).isoformat()


def _destination_key(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _active_split(
    session: Session,
    journey: Journey,
    member: JourneyMember,
    *,
    now: datetime | None = None,
) -> dict:
    if str(journey.status or "").upper() != "ACTIVE" or not journey.active_plan_version_id:
        raise HTTPException(409, "SIDE_QUEST_REQUIRES_ACTIVE_JOURNEY")
    split = planned_separation_for_member(
        session,
        journey,
        member,
        now=(now or execution_clock.now()).astimezone(timezone.utc),
    )
    if split is None:
        raise HTTPException(409, "SIDE_QUEST_REQUIRES_CURRENT_PLANNED_SEPARATION")
    if (
        str(split["target"].plan_version_id) != str(journey.active_plan_version_id)
        or str(split["target"].status or "").upper() in {"COMPLETED", "SKIPPED", "CANCELLED"}
    ):
        raise HTTPException(409, "SIDE_QUEST_REQUIRES_CURRENT_PLANNED_SEPARATION")
    return split


def _quest_rows_for_split(
    session: Session,
    journey_id: str,
    member_id: str,
    split_intent_id: str,
) -> list[MemberSideQuest]:
    return session.scalars(
        select(MemberSideQuest)
        .where(
            MemberSideQuest.journey_id == journey_id,
            MemberSideQuest.member_id == member_id,
            MemberSideQuest.planned_separation_intent_id == split_intent_id,
        )
        .order_by(MemberSideQuest.created_at.desc(), MemberSideQuest.id.desc())
    ).all()


def _resolve_destination(
    session: Session,
    journey: Journey,
    *,
    destination_text: str | None,
    place_id: str | None,
) -> tuple[Place | None, str, str, str]:
    text = " ".join(str(destination_text or "").split())
    requested_place_id = str(place_id or "").strip()
    if requested_place_id:
        place = session.get(Place, requested_place_id)
        if place is None:
            raise HTTPException(404, "SIDE_QUEST_PLACE_NOT_FOUND")
        if str(place.city_code) != str(journey.city_code):
            raise HTTPException(422, "SIDE_QUEST_PLACE_OUTSIDE_JOURNEY_CITY")
        if text and _destination_key(text) != _destination_key(place.name):
            raise HTTPException(422, "SIDE_QUEST_PLACE_LABEL_MISMATCH")
        return place, str(place.name), "EXACT_KNOWN_PLACE", "READY"
    if not text:
        raise HTTPException(422, "SIDE_QUEST_DESTINATION_REQUIRED")

    # A bare name is bound only when it exactly identifies one existing Place in
    # the Journey's city. Ambiguity remains descriptive; no coordinates are made.
    candidates = session.scalars(
        select(Place).where(Place.city_code == str(journey.city_code))
    ).all()
    exact = [row for row in candidates if _destination_key(row.name) == _destination_key(text)]
    if len(exact) == 1:
        return exact[0], str(exact[0].name), "EXACT_KNOWN_PLACE", "READY"
    return None, text, "DESCRIPTIVE_ONLY", "DESTINATION_UNRESOLVED"


def _record_quest_audit(
    session: Session,
    *,
    action: str,
    journey: Journey,
    quest: MemberSideQuest,
    actor_id: str,
    state: str,
    reason_code: str | None = None,
    actor_type: str = "TRAVELER",
) -> None:
    after = {
        "member_id": str(quest.member_id),
        "planned_separation_intent_id": str(quest.planned_separation_intent_id),
        "state": state,
        "physical_presence_verified": False,
    }
    if reason_code:
        after["reason_code"] = reason_code
    record_audit(
        session,
        action=f"journey.member_side_quest.{action}",
        entity_type="MemberSideQuest",
        entity_id=quest.id,
        journey_id=journey.id,
        actor_type=actor_type,
        actor_id=actor_id,
        after=after,
    )


def _create_or_replace_member_side_quest(
    session: Session,
    *,
    journey: Journey,
    member: JourneyMember,
    traveler_id: str,
    destination_text: str | None,
    place_id: str | None,
    now: datetime | None = None,
) -> MemberSideQuest:
    stamp = _stamp(now)
    split = _active_split(session, journey, member, now=now)
    place, title, destination_authority, state = _resolve_destination(
        session,
        journey,
        destination_text=destination_text,
        place_id=place_id,
    )
    split_id = str(split["intent"].id)
    rows = _quest_rows_for_split(session, journey.id, member.id, split_id)
    open_quest = next((row for row in rows if row.state in OPEN_STATES), None)
    if open_quest is not None and open_quest.state not in {"DESTINATION_UNRESOLVED", "READY"}:
        raise HTTPException(409, "SIDE_QUEST_ALREADY_IN_PROGRESS")

    if open_quest is None:
        target = split["target"]
        rendezvous_place = split["place"]
        quest = MemberSideQuest(
            id=str(uuid4()),
            journey_id=journey.id,
            member_id=member.id,
            traveler_id=str(traveler_id),
            planned_separation_intent_id=split_id,
            active_plan_version_id=str(journey.active_plan_version_id),
            rendezvous_activity_id=str(target.id),
            rendezvous_place_id=str(rendezvous_place.id),
            rendezvous_title=str(target.title),
            rendezvous_place_name=str(rendezvous_place.name),
            rendezvous_scheduled_at=target.start_at,
            side_place_id=str(place.id) if place is not None else None,
            side_title=title,
            destination_authority=destination_authority,
            state=state,
            created_at=stamp,
            updated_at=stamp,
        )
        session.add(quest)
        session.flush()
        _record_quest_audit(
            session,
            action="created",
            journey=journey,
            quest=quest,
            actor_id=traveler_id,
            state=state,
        )
        session.commit()
        return quest

    if (
        open_quest.side_place_id == (str(place.id) if place is not None else None)
        and open_quest.side_title == title
        and open_quest.destination_authority == destination_authority
        and open_quest.state == state
    ):
        return open_quest

    open_quest.side_place_id = str(place.id) if place is not None else None
    open_quest.side_title = title
    open_quest.destination_authority = destination_authority
    open_quest.state = state
    open_quest.started_at = None
    open_quest.arrived_at = None
    open_quest.completed_at = None
    open_quest.cancelled_at = None
    open_quest.updated_at = stamp
    _record_quest_audit(
        session,
        action="replaced",
        journey=journey,
        quest=open_quest,
        actor_id=traveler_id,
        state=state,
    )
    session.commit()
    return open_quest


def create_or_replace_member_side_quest(
    session: Session,
    *,
    journey: Journey,
    member: JourneyMember,
    traveler_id: str,
    destination_text: str | None,
    place_id: str | None,
    now: datetime | None = None,
) -> MemberSideQuest:
    with journey_write_lock(str(journey.id)):
        session.expire_all()
        current_journey = session.get(Journey, str(journey.id))
        current_member = session.get(JourneyMember, str(member.id))
        if current_journey is None or current_member is None:
            raise HTTPException(404, "SIDE_QUEST_MEMBER_NOT_FOUND")
        return _create_or_replace_member_side_quest(
            session,
            journey=current_journey,
            member=current_member,
            traveler_id=traveler_id,
            destination_text=destination_text,
            place_id=place_id,
            now=now,
        )


def invalidate_member_side_quests_for_intents(
    session: Session,
    *,
    journey: Journey,
    intent_ids: list[str],
    reason_code: str,
    now: datetime | None = None,
) -> int:
    ids = [str(item) for item in intent_ids if str(item)]
    if not ids:
        return 0
    stamp = _stamp(now)
    rows = session.scalars(
        select(MemberSideQuest).where(
            MemberSideQuest.journey_id == journey.id,
            MemberSideQuest.planned_separation_intent_id.in_(ids),
            MemberSideQuest.state.in_(tuple(OPEN_STATES)),
        )
    ).all()
    for quest in rows:
        quest.state = "INVALIDATED"
        quest.invalidated_at = stamp
        quest.updated_at = stamp
        _record_quest_audit(
            session,
            action="invalidated",
            journey=journey,
            quest=quest,
            actor_id="r63-planned-separation",
            state="INVALIDATED",
            reason_code=reason_code,
            actor_type="SYSTEM",
        )
    return len(rows)


def _current_binding_matches(
    session: Session,
    *,
    journey: Journey,
    member: JourneyMember,
    quest: MemberSideQuest,
    now: datetime | None = None,
) -> bool:
    try:
        split = _active_split(session, journey, member, now=now)
    except HTTPException:
        return False
    return bool(
        str(split["intent"].id) == str(quest.planned_separation_intent_id)
        and str(journey.active_plan_version_id) == str(quest.active_plan_version_id)
        and str(split["target"].id) == str(quest.rendezvous_activity_id)
        and str(split["place"].id) == str(quest.rendezvous_place_id)
    )


def _transition_member_side_quest(
    session: Session,
    *,
    journey: Journey,
    member: JourneyMember,
    traveler_id: str,
    quest_id: str,
    action: str,
    now: datetime | None = None,
) -> MemberSideQuest:
    normalized_action = str(action or "").upper()
    if normalized_action not in SAME_STATE_ACTIONS:
        raise HTTPException(422, "SIDE_QUEST_ACTION_UNSUPPORTED")
    quest = session.scalar(
        select(MemberSideQuest).where(
            MemberSideQuest.id == str(quest_id),
            MemberSideQuest.journey_id == journey.id,
            MemberSideQuest.member_id == member.id,
            MemberSideQuest.traveler_id == str(traveler_id),
        )
    )
    if quest is None:
        raise HTTPException(404, "SIDE_QUEST_NOT_FOUND")

    target_state = SAME_STATE_ACTIONS[normalized_action]
    if quest.state in TERMINAL_STATES:
        if quest.state == target_state:
            return quest
        raise HTTPException(409, "SIDE_QUEST_IS_TERMINAL")

    stamp = _stamp(now)
    if not _current_binding_matches(
        session, journey=journey, member=member, quest=quest, now=now
    ):
        quest.state = "INVALIDATED"
        quest.invalidated_at = stamp
        quest.updated_at = stamp
        _record_quest_audit(
            session,
            action="invalidated",
            journey=journey,
            quest=quest,
            actor_id=traveler_id,
            state="INVALIDATED",
            reason_code="R63_PLANNED_SEPARATION_NO_LONGER_CURRENT",
        )
        session.commit()
        raise HTTPException(409, "SIDE_QUEST_INVALIDATED_R63_SEPARATION_CHANGED")
    if quest.state == target_state:
        return quest

    transitions = {
        "START": {"READY": "EN_ROUTE"},
        "ARRIVE": {"EN_ROUTE": "ARRIVED"},
        "COMPLETE": {"ARRIVED": "COMPLETED"},
        "CANCEL": {state: "CANCELLED" for state in OPEN_STATES},
    }
    next_state = transitions[normalized_action].get(quest.state)
    if next_state is None:
        if normalized_action == "START" and quest.state == "DESTINATION_UNRESOLVED":
            raise HTTPException(409, "SIDE_QUEST_DESTINATION_NOT_EXACT")
        raise HTTPException(409, "SIDE_QUEST_INVALID_TRANSITION")

    quest.state = next_state
    quest.updated_at = stamp
    if next_state == "EN_ROUTE":
        quest.started_at = stamp
    elif next_state == "ARRIVED":
        quest.arrived_at = stamp
    elif next_state == "COMPLETED":
        quest.completed_at = stamp
    elif next_state == "CANCELLED":
        quest.cancelled_at = stamp
    _record_quest_audit(
        session,
        action=normalized_action.lower(),
        journey=journey,
        quest=quest,
        actor_id=traveler_id,
        state=next_state,
    )
    session.commit()
    return quest


def transition_member_side_quest(
    session: Session,
    *,
    journey: Journey,
    member: JourneyMember,
    traveler_id: str,
    quest_id: str,
    action: str,
    now: datetime | None = None,
) -> MemberSideQuest:
    with journey_write_lock(str(journey.id)):
        session.expire_all()
        current_journey = session.get(Journey, str(journey.id))
        current_member = session.get(JourneyMember, str(member.id))
        if current_journey is None or current_member is None:
            raise HTTPException(404, "SIDE_QUEST_MEMBER_NOT_FOUND")
        return _transition_member_side_quest(
            session,
            journey=current_journey,
            member=current_member,
            traveler_id=traveler_id,
            quest_id=quest_id,
            action=action,
            now=now,
        )


def _member_side_quest_for_action(
    session: Session,
    *,
    journey: Journey,
    member: JourneyMember,
    traveler_id: str,
    quest_id: str,
    allowed_states: frozenset[str],
) -> tuple[MemberSideQuest, dict, Place]:
    quest = session.scalar(
        select(MemberSideQuest).where(
            MemberSideQuest.id == str(quest_id),
            MemberSideQuest.journey_id == journey.id,
            MemberSideQuest.member_id == member.id,
            MemberSideQuest.traveler_id == str(traveler_id),
        )
    )
    if quest is None:
        raise HTTPException(404, "SIDE_QUEST_NOT_FOUND")
    split = _active_split(session, journey, member)
    if not _current_binding_matches(
        session, journey=journey, member=member, quest=quest
    ):
        raise HTTPException(409, "SIDE_QUEST_INVALIDATED_R63_SEPARATION_CHANGED")
    rows = _quest_rows_for_split(
        session, journey.id, member.id, str(split["intent"].id)
    )
    if not rows or str(rows[0].id) != str(quest.id):
        raise HTTPException(409, "SIDE_QUEST_IS_NOT_CURRENT")
    if quest.state not in allowed_states:
        raise HTTPException(409, "SIDE_QUEST_INVALID_TRANSITION")
    if (
        quest.destination_authority != "EXACT_KNOWN_PLACE"
        or not quest.side_place_id
    ):
        raise HTTPException(409, "SIDE_QUEST_DESTINATION_NOT_EXACT")
    place = session.get(Place, quest.side_place_id)
    if place is None or str(place.city_code) != str(journey.city_code):
        raise HTTPException(409, "SIDE_QUEST_PLACE_NO_LONGER_AVAILABLE")
    return quest, split, place


def resolve_member_side_quest_navigation_target(
    session: Session,
    *,
    journey: Journey,
    member: JourneyMember,
    traveler_id: str,
    quest_id: str,
) -> dict:
    """Resolve only the signed-in member's current, plan-bound map target."""
    quest = session.scalar(
        select(MemberSideQuest).where(
            MemberSideQuest.id == str(quest_id),
            MemberSideQuest.journey_id == journey.id,
            MemberSideQuest.member_id == member.id,
            MemberSideQuest.traveler_id == str(traveler_id),
        )
    )
    if quest is None:
        raise HTTPException(404, "SIDE_QUEST_NOT_FOUND")

    split = _active_split(session, journey, member)
    if not _current_binding_matches(
        session, journey=journey, member=member, quest=quest
    ):
        raise HTTPException(409, "SIDE_QUEST_NAVIGATION_CONTEXT_STALE")
    rows = _quest_rows_for_split(
        session, journey.id, member.id, str(split["intent"].id)
    )
    if not rows or str(rows[0].id) != str(quest.id):
        raise HTTPException(409, "SIDE_QUEST_NAVIGATION_CONTEXT_STALE")

    if quest.state == "DESTINATION_UNRESOLVED":
        raise HTTPException(409, "SIDE_QUEST_DESTINATION_NOT_EXACT")
    if quest.state in {"READY", "EN_ROUTE"}:
        if quest.destination_authority != "EXACT_KNOWN_PLACE" or not quest.side_place_id:
            raise HTTPException(409, "SIDE_QUEST_DESTINATION_NOT_EXACT")
        place = session.get(Place, str(quest.side_place_id))
        target_kind = "PRIVATE_STOP"
    elif quest.state in {"COMPLETED", "CANCELLED"}:
        place = split["place"]
        target_kind = "SHARED_RENDEZVOUS"
        if (
            str(place.id) != str(quest.rendezvous_place_id)
            or str(split["target"].id) != str(quest.rendezvous_activity_id)
        ):
            raise HTTPException(409, "SIDE_QUEST_NAVIGATION_CONTEXT_STALE")
    else:
        raise HTTPException(409, "SIDE_QUEST_NAVIGATION_STATE_UNAVAILABLE")

    if place is None or str(place.city_code) != str(journey.city_code):
        raise HTTPException(409, "SIDE_QUEST_NAVIGATION_PLACE_UNAVAILABLE")

    coordinate_system = str(place.coordinate_system or "").strip().upper()
    latitude: float | None = None
    longitude: float | None = None
    unavailable_reason: str | None = None
    if coordinate_system != "WGS84":
        unavailable_reason = "PLACE_COORDINATES_NOT_WGS84"
    else:
        try:
            candidate_latitude = float(place.latitude)
            candidate_longitude = float(place.longitude)
        except (TypeError, ValueError, OverflowError):
            unavailable_reason = "PLACE_COORDINATES_INVALID"
        else:
            if (
                not math.isfinite(candidate_latitude)
                or not math.isfinite(candidate_longitude)
                or not -90 <= candidate_latitude <= 90
                or not -180 <= candidate_longitude <= 180
            ):
                unavailable_reason = "PLACE_COORDINATES_INVALID"
            else:
                latitude = candidate_latitude
                longitude = candidate_longitude

    target = {
        "kind": target_kind,
        "place_id": str(place.id),
        "name": str(place.name),
        "latitude": latitude,
        "longitude": longitude,
        "coordinate_system": coordinate_system,
        "provenance": {
            "provider_key": str(place.provider_key or ""),
            "external_id": str(place.external_id or ""),
            "source_coordinate_system": (
                str(place.source_coordinate_system)
                if place.source_coordinate_system is not None
                else None
            ),
        },
        "navigation_available": unavailable_reason is None,
        "unavailable_reason": unavailable_reason,
    }
    return {"target": target}


def _fresh_member_observation(
    session: Session,
    *,
    journey: Journey,
    member: JourneyMember,
    traveler_id: str,
    observation_id: str,
) -> LocationObservation:
    observation = session.get(LocationObservation, str(observation_id))
    if observation is None or str(observation.journey_id) != str(journey.id):
        raise HTTPException(404, "LOCATION_OBSERVATION_NOT_FOUND")
    if (
        str(observation.traveler_id or "") != str(traveler_id)
        or str(observation.journey_member_id or "") != str(member.id)
    ):
        raise HTTPException(403, "LOCATION_OBSERVATION_MEMBER_MISMATCH")
    source = str(observation.source or "").upper()
    if source not in {"FOREGROUND_WEB", "DEVICE_ATTESTED", "LOCATION_ATTESTED"}:
        raise HTTPException(409, "SIDE_QUEST_LOCATION_SOURCE_UNSUPPORTED")
    captured_at = _aware_datetime(observation.captured_at)
    if captured_at is None:
        raise HTTPException(409, "SIDE_QUEST_LOCATION_TIME_UNKNOWN")
    age_seconds = (
        execution_clock.now().astimezone(timezone.utc) - captured_at
    ).total_seconds()
    if age_seconds < -30 or age_seconds > LOCATION_FRESHNESS_SECONDS:
        raise HTTPException(409, "SIDE_QUEST_LOCATION_STALE")
    if float(observation.accuracy_m) > MAX_USEFUL_ACCURACY_M:
        raise HTTPException(409, "SIDE_QUEST_LOCATION_IMPRECISE")
    return observation


def _aware_datetime(value: object) -> datetime | None:
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(
            str(value or "").replace("Z", "+00:00")
        )
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _capture_private_route_leg(
    session: Session,
    *,
    journey: Journey,
    origin_key: str,
    target_key: str,
    requested_mode: str,
) -> dict | None:
    """Capture a route without writing its private keys, geometry, or response to shared tables."""
    pair = (str(origin_key), str(target_key))
    try:
        from app.integrations.mobility.runtime import capture_runtime_mobility_snapshot

        captured = capture_runtime_mobility_snapshot(
            session,
            journey,
            [pair],
            operation_key=f"private-side-quest:{uuid4()}",
            require_explicit_facts=True,
            requested_mode=requested_mode,
        )
        fact = captured.snapshot.facts.get(pair)
    except Exception:
        # Route-provider failures remain an honest UNKNOWN in the owner UI.
        return None
    if fact is None:
        return None
    provider = str(getattr(fact, "provider_key", "") or "")
    provenance = str(
        getattr(fact, "route_provenance", None)
        or getattr(fact, "fact_quality", None)
        or ""
    ).upper()
    fact_summary = {
        "provider": provider,
        "provenance": provenance,
        "duration_min": getattr(fact, "minutes", None),
    }
    if (
        not fact_is_eligible_for_live_readiness(fact_summary)
        or provider.casefold().startswith(("mock", "fixture"))
        or provenance not in {"LIVE_PROVIDER_CAPTURE", "LIVE_CAPTURE"}
    ):
        return None
    captured_at = _aware_datetime(getattr(fact, "captured_at", None))
    if captured_at is None:
        return None
    age_seconds = (
        execution_clock.now().astimezone(timezone.utc) - captured_at
    ).total_seconds()
    if age_seconds < -30 or age_seconds > LOCATION_FRESHNESS_SECONDS:
        return None
    try:
        duration = int(fact.minutes)
        distance = int(fact.distance_m) if fact.distance_m is not None else None
    except (TypeError, ValueError):
        return None
    if duration < 0:
        return None
    geometry = list(getattr(fact, "route_geometry", None) or ())
    return {
        "duration_min": duration,
        "distance_m": distance,
        "provider": provider,
        "provenance": provenance,
        "captured_at": captured_at.isoformat(),
        "age_seconds": max(0, int(age_seconds)),
        "mode": str(getattr(fact, "mode", None) or requested_mode).upper(),
        "geometry": geometry,
    }


def _assert_private_route_binding_current(
    session: Session,
    *,
    journey_id: str,
    member_id: str,
    traveler_id: str,
    quest_id: str,
    allowed_states: frozenset[str],
    expected_side_place_id: str,
) -> tuple[Journey, JourneyMember, MemberSideQuest]:
    session.expire_all()
    journey = session.get(Journey, str(journey_id))
    member = session.get(JourneyMember, str(member_id))
    quest = session.get(MemberSideQuest, str(quest_id))
    if journey is None or member is None or quest is None:
        raise HTTPException(409, "SIDE_QUEST_BINDING_CHANGED_DURING_ROUTE_CAPTURE")
    if (
        str(member.traveler_id) != str(traveler_id)
        or member.removed_at is not None
        or str(quest.traveler_id) != str(traveler_id)
        or quest.state not in allowed_states
        or quest.destination_authority != "EXACT_KNOWN_PLACE"
        or str(quest.side_place_id or "") != str(expected_side_place_id)
        or not _current_binding_matches(session, journey=journey, member=member, quest=quest)
    ):
        raise HTTPException(409, "SIDE_QUEST_BINDING_CHANGED_DURING_ROUTE_CAPTURE")
    current_rows = _quest_rows_for_split(
        session,
        journey.id,
        member.id,
        str(quest.planned_separation_intent_id),
    )
    place = session.get(Place, str(expected_side_place_id))
    if (
        not current_rows
        or str(current_rows[0].id) != str(quest.id)
        or place is None
        or str(place.city_code) != str(journey.city_code)
    ):
        raise HTTPException(409, "SIDE_QUEST_BINDING_CHANGED_DURING_ROUTE_CAPTURE")
    return journey, member, quest


def navigate_member_side_quest_destination(
    session: Session,
    *,
    journey: Journey,
    member: JourneyMember,
    traveler_id: str,
    quest_id: str,
    location_observation_id: str,
    requested_mode: str = "WALK",
) -> dict:
    quest, _split, place = _member_side_quest_for_action(
        session,
        journey=journey,
        member=member,
        traveler_id=traveler_id,
        quest_id=quest_id,
        allowed_states=frozenset({"READY", "EN_ROUTE"}),
    )
    observation = _fresh_member_observation(
        session,
        journey=journey,
        member=member,
        traveler_id=traveler_id,
        observation_id=location_observation_id,
    )
    route = _capture_private_route_leg(
        session,
        journey=journey,
        origin_key=f"location-observation:{observation.id}",
        target_key=str(place.id),
        requested_mode=requested_mode,
    )
    _assert_private_route_binding_current(
        session,
        journey_id=journey.id,
        member_id=member.id,
        traveler_id=traveler_id,
        quest_id=quest.id,
        allowed_states=frozenset({"READY", "EN_ROUTE"}),
        expected_side_place_id=str(place.id),
    )
    return {
        "status": "COMPLETED" if route is not None else "UNKNOWN",
        "destination": {"title": str(quest.side_title)},
        "route": route,
        "authority": "PRIVATE_MEMBER_REAL_PROVIDER_ROUTE" if route else "UNKNOWN",
        "origin_basis": "AUTHENTICATED_FRESH_MEMBER_LOCATION",
        "plan_unchanged": True,
    }


def preview_member_side_quest_fit(
    session: Session,
    *,
    journey: Journey,
    member: JourneyMember,
    traveler_id: str,
    quest_id: str,
    location_observation_id: str | None,
    dwell_minutes: int,
    requested_mode: str = "WALK",
) -> dict:
    if dwell_minutes not in {10, 20, 30}:
        raise HTTPException(422, "SIDE_QUEST_DWELL_MINUTES_UNSUPPORTED")
    quest, split, side_place = _member_side_quest_for_action(
        session,
        journey=journey,
        member=member,
        traveler_id=traveler_id,
        quest_id=quest_id,
        allowed_states=frozenset({"READY", "EN_ROUTE"}),
    )
    target_activity = split["target"]
    target_place = split["place"]
    observation = (
        _fresh_member_observation(
            session,
            journey=journey,
            member=member,
            traveler_id=traveler_id,
            observation_id=location_observation_id,
        )
        if location_observation_id
        else None
    )
    outbound = (
        _capture_private_route_leg(
            session,
            journey=journey,
            origin_key=f"location-observation:{observation.id}",
            target_key=str(side_place.id),
            requested_mode=requested_mode,
        )
        if observation is not None
        else None
    )
    hypothetical_return = _capture_private_route_leg(
        session,
        journey=journey,
        origin_key=str(side_place.id),
        target_key=str(target_place.id),
        requested_mode=requested_mode,
    ) if observation is not None else None
    _assert_private_route_binding_current(
        session,
        journey_id=journey.id,
        member_id=member.id,
        traveler_id=traveler_id,
        quest_id=quest.id,
        allowed_states=frozenset({"READY", "EN_ROUTE"}),
        expected_side_place_id=str(side_place.id),
    )

    scheduled_at = _aware_datetime(target_activity.start_at)
    preview_now = execution_clock.now().astimezone(timezone.utc)
    protected_buffer: int | None = None
    protected = False
    if outbound is not None and hypothetical_return is not None:
        side_departure = preview_now + timedelta(
            minutes=int(outbound["duration_min"]) + dwell_minutes
        )
        commitment = rendezvous_commitment_risk(
            session,
            journey,
            target_activity,
            route_duration_min=int(hypothetical_return["duration_min"]),
            authority="ADVISORY_PRIVATE_DETOUR_PREVIEW",
            now=side_departure,
        )
        next_commitment = commitment.get("next_commitment") or {}
        if next_commitment.get("safety_buffer_min") is not None:
            try:
                protected_buffer = max(0, int(next_commitment["safety_buffer_min"]))
                protected = True
            except (TypeError, ValueError):
                protected_buffer = None
        if next_commitment.get("scheduled_at"):
            scheduled_at = _aware_datetime(next_commitment["scheduled_at"])

    provenance = {"outbound": outbound, "return": hypothetical_return}
    state = "UNKNOWN"
    recommended_leave_by = None
    reason_code = None
    authority = "ADVISORY"
    if observation is None:
        reason_code = "CURRENT_LOCATION_UNAVAILABLE"
    elif outbound is None or hypothetical_return is None:
        reason_code = "QUALIFYING_LIVE_ROUTE_EVIDENCE_UNAVAILABLE"
    elif scheduled_at is None:
        reason_code = "RENDEZVOUS_TIME_UNKNOWN"
    else:
        raw_need = (
            int(outbound["duration_min"])
            + dwell_minutes
            + int(hypothetical_return["duration_min"])
        )
        available = int((scheduled_at - preview_now).total_seconds() // 60)
        buffer_minutes = protected_buffer or 0
        earliest_private_departure = preview_now + timedelta(
            minutes=int(outbound["duration_min"]) + dwell_minutes
        )
        candidate_leave_by = scheduled_at - timedelta(
            minutes=int(hypothetical_return["duration_min"]) + buffer_minutes
        )
        authority = (
            "ADVISORY_PROTECTED_COMMITMENT"
            if protected
            else "ADVISORY_NON_PROTECTED_RENDEZVOUS"
        )
        if available < raw_need:
            state = "DOES_NOT_FIT"
            reason_code = "INSUFFICIENT_TIME_FOR_PRIVATE_DETOUR"
        elif protected and available >= raw_need + buffer_minutes:
            state = "FITS_WITH_BUFFER"
        else:
            # An unprotected rendezvous has no server-promised safety buffer.
            state = "TIGHT_RETURN"
        # Do not label an already impossible deadline "recommended".
        # A TIGHT_RETURN may reach the rendezvous but miss its protected
        # buffer; showing the unreachable protected leave-by is misleading.
        if state != "DOES_NOT_FIT" and earliest_private_departure <= candidate_leave_by:
            recommended_leave_by = candidate_leave_by.isoformat()

    return {
        "state": state,
        "dwell_minutes": dwell_minutes,
        "outbound_eta_minutes": outbound["duration_min"] if outbound else None,
        "return_eta_minutes": hypothetical_return["duration_min"] if hypothetical_return else None,
        "recommended_leave_by": recommended_leave_by,
        "rendezvous_scheduled_at": scheduled_at.isoformat() if scheduled_at else None,
        "safety_buffer_minutes": protected_buffer,
        "provenance": provenance,
        "origin_basis": {
            "outbound": "AUTHENTICATED_FRESH_MEMBER_LOCATION" if observation else None,
            "return": "HYPOTHETICAL_SIDE_PLACE",
        },
        "authority": authority,
        "reason_code": reason_code,
        "plan_unchanged": True,
    }


def start_member_side_quest_return_report(
    session: Session,
    *,
    journey: Journey,
    member: JourneyMember,
    traveler_id: str,
    quest_id: str,
) -> MemberSideQuestReturnReport:
    quest, split, _place = _member_side_quest_for_action(
        session,
        journey=journey,
        member=member,
        traveler_id=traveler_id,
        quest_id=quest_id,
        allowed_states=frozenset({"COMPLETED", "CANCELLED"}),
    )
    existing = session.scalar(
        select(MemberSideQuestReturnReport).where(
            MemberSideQuestReturnReport.side_quest_id == quest.id,
            MemberSideQuestReturnReport.member_id == member.id,
            MemberSideQuestReturnReport.traveler_id == str(traveler_id),
        )
    )
    if existing is not None:
        return existing
    stamp = _stamp()
    report = MemberSideQuestReturnReport(
        id=str(uuid4()),
        journey_id=journey.id,
        member_id=member.id,
        traveler_id=str(traveler_id),
        side_quest_id=quest.id,
        planned_separation_intent_id=str(split["intent"].id),
        active_plan_version_id=str(journey.active_plan_version_id),
        status="RETURN_STARTED_REPORTED",
        reported_at=stamp,
        created_at=stamp,
    )
    session.add(report)
    _record_quest_audit(
        session,
        action="return_started_reported",
        journey=journey,
        quest=quest,
        actor_id=traveler_id,
        state="RETURN_STARTED_REPORTED",
    )
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        winner = session.scalar(
            select(MemberSideQuestReturnReport).where(
                MemberSideQuestReturnReport.side_quest_id == quest.id,
                MemberSideQuestReturnReport.member_id == member.id,
                MemberSideQuestReturnReport.traveler_id == str(traveler_id),
            )
        )
        if winner is None:
            raise
        return winner
    return report


def _owner_payload(
    quest: MemberSideQuest,
    *,
    state: str | None = None,
    return_state: str | None = None,
    return_report: MemberSideQuestReturnReport | None = None,
) -> dict:
    return {
        "id": str(quest.id),
        "state": state or str(quest.state),
        "destination": {
            "place_id": str(quest.side_place_id) if quest.side_place_id else None,
            "title": str(quest.side_title),
            "authority": str(quest.destination_authority),
        },
        "rendezvous": {
            "activity_id": str(quest.rendezvous_activity_id),
            "title": str(quest.rendezvous_title),
            "place_name": str(quest.rendezvous_place_name),
            "scheduled_at": quest.rendezvous_scheduled_at,
        },
        "return_state": return_state or "RETURN_UNKNOWN",
        "return_report": (
            {
                "status": "RETURN_STARTED_REPORTED",
                "reported_at": return_report.reported_at,
                "physical_presence_verified": False,
            }
            if return_report is not None
            else None
        ),
        "shared_plan_changed": False,
        "arrival_claim": (
            "MEMBER_REPORTED_NOT_PHYSICAL_PRESENCE_PROOF"
            if state == "ARRIVED" or (state is None and quest.state == "ARRIVED")
            else None
        ),
        "created_at": quest.created_at,
        "started_at": quest.started_at,
        "arrived_at": quest.arrived_at,
        "completed_at": quest.completed_at,
        "cancelled_at": quest.cancelled_at,
    }


def attach_member_side_quest_projection(
    session: Session,
    journey: Journey,
    *,
    viewer_member_id: str | None,
    group_presence: dict,
) -> dict | None:
    """Add only coarse non-owner states; return destination detail only to its owner."""
    projected_members = group_presence.get("members")
    projected_members = projected_members if isinstance(projected_members, list) else []
    current_splits: dict[str, dict] = {}
    member_rows: dict[str, JourneyMember] = {}
    for item in projected_members:
        member_id = str(item.get("member_id") or "") if isinstance(item, dict) else ""
        member = session.get(JourneyMember, member_id) if member_id else None
        if member is None:
            continue
        member_rows[member_id] = member
        try:
            split = planned_separation_for_member(
                session,
                journey,
                member,
                now=execution_clock.now().astimezone(timezone.utc),
            )
        except HTTPException:
            split = None
        if split is not None:
            current_splits[member_id] = split

    quest_rows = session.scalars(
        select(MemberSideQuest)
        .where(MemberSideQuest.journey_id == journey.id)
        .order_by(MemberSideQuest.created_at, MemberSideQuest.id)
    ).all()
    report_rows = session.scalars(
        select(MemberSideQuestReturnReport).where(
            MemberSideQuestReturnReport.journey_id == journey.id
        )
    ).all()
    reports_by_quest = {str(row.side_quest_id): row for row in report_rows}

    def current_report_for(quest: MemberSideQuest) -> MemberSideQuestReturnReport | None:
        report = reports_by_quest.get(str(quest.id))
        if (
            report is None
            or str(quest.state) not in {"COMPLETED", "CANCELLED"}
            or str(quest.active_plan_version_id) != str(journey.active_plan_version_id or "")
            or str(report.journey_id) != str(journey.id)
            or str(report.member_id) != str(quest.member_id)
            or str(report.traveler_id) != str(quest.traveler_id)
            or str(report.planned_separation_intent_id)
            != str(quest.planned_separation_intent_id)
            or str(report.active_plan_version_id)
            != str(quest.active_plan_version_id)
            or str(report.status) != "RETURN_STARTED_REPORTED"
        ):
            return None
        return report

    latest_by_split: dict[tuple[str, str], MemberSideQuest] = {}
    latest_by_member: dict[str, MemberSideQuest] = {}
    for quest in quest_rows:
        latest_by_split[(str(quest.member_id), str(quest.planned_separation_intent_id))] = quest
        latest_by_member[str(quest.member_id)] = quest

    viewer_payload: dict | None = None
    viewer_split = current_splits.get(str(viewer_member_id or ""))
    viewer_member = member_rows.get(str(viewer_member_id or ""))
    viewer_quest = None
    if viewer_split is not None:
        viewer_quest = latest_by_split.get(
            (str(viewer_member_id), str(viewer_split["intent"].id))
        )
        if viewer_quest is not None and (
            str(viewer_quest.active_plan_version_id)
            != str(journey.active_plan_version_id or "")
            or str(viewer_quest.rendezvous_activity_id)
            != str(viewer_split["target"].id)
            or str(viewer_quest.rendezvous_place_id)
            != str(viewer_split["place"].id)
        ):
            viewer_quest = None
    elif viewer_member is not None:
        viewer_quest = latest_by_member.get(str(viewer_member_id))
        if viewer_quest is not None and viewer_quest.state in OPEN_STATES:
            viewer_payload = _owner_payload(
                viewer_quest,
                state="INVALIDATED",
                return_state="RETURN_UNKNOWN",
            )

    for item in projected_members:
        if not isinstance(item, dict):
            continue
        member_id = str(item.get("member_id") or "")
        split = current_splits.get(member_id)
        if split is None:
            continue
        quest = latest_by_split.get((member_id, str(split["intent"].id)))
        if quest is None:
            continue
        if (
            str(quest.active_plan_version_id) != str(journey.active_plan_version_id or "")
            or str(quest.rendezvous_activity_id) != str(split["target"].id)
            or str(quest.rendezvous_place_id) != str(split["place"].id)
        ):
            continue
        if member_id == str(viewer_member_id or ""):
            if viewer_quest is not None:
                planned_row = item.get("return_state")
                viewer_payload = _owner_payload(
                    viewer_quest,
                    return_state=str(planned_row or "RETURN_UNKNOWN"),
                    return_report=current_report_for(viewer_quest),
                )
            continue
        # A scheduled stop is not proof of departure. Finishing it is not
        # proof that the traveller has started returning to the group.
        if quest.state in {"DESTINATION_UNRESOLVED", "READY"}:
            summary_state = "PLANNED"
        elif quest.state in {"EN_ROUTE", "ARRIVED"}:
            summary_state = "ACTIVE_SELF_REPORTED"
        elif quest.state in {"COMPLETED", "CANCELLED"}:
            summary_state = (
                "RETURN_STARTED_REPORTED"
                if current_report_for(quest) is not None
                else "FINISHED_RETURN_UNCONFIRMED"
            )
        else:
            continue
        item["personal_stop"] = {
            "planned_personal_stop": True,
            "state": summary_state,
        }

    # An owner may still inspect their latest durable side-quest result when the
    # R63 binding has gone stale. The effective INVALIDATED display never makes it
    # executable or re-targets it to a new shared plan.
    if viewer_payload is None and viewer_member is not None and viewer_split is None:
        viewer_quest = latest_by_member.get(str(viewer_member.id))
        if viewer_quest is not None:
            viewer_payload = _owner_payload(
                viewer_quest,
                return_report=current_report_for(viewer_quest),
            )
    return viewer_payload
