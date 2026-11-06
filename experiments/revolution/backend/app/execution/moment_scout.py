from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import and_, case, or_, select
from sqlalchemy.orm import Session

from app.db.models import Activity, Journey, JourneyMember, LocationObservation, MemberSideQuest, Place
from app.execution.clock import execution_clock
from app.execution.member_side_quest import _active_split, _fresh_member_observation


LOCALITY_RADIUS_M = 6_000
MAX_CITY_SHORTLIST_ROWS = 500
OPEN_QUEST_STATES = ("DESTINATION_UNRESOLVED", "READY", "EN_ROUTE", "ARRIVED")
RETURN_REQUIRED_QUEST_STATES = OPEN_QUEST_STATES + ("COMPLETED",)
STRUCTURAL_CATEGORIES = {
    "accommodation", "arrival", "departure", "lodging", "meeting_point",
    "origin", "rendezvous", "start", "transfer", "transport",
}
INTEREST_TERMS = {
    "COFFEE_FOOD": {"bakery", "cafe", "coffee", "food", "market", "restaurant"},
    "CULTURE": {"culture", "gallery", "heritage", "landmark", "library", "museum"},
    "NATURE": {"garden", "nature", "park", "river", "trail", "waterfront"},
}


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6_371_000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    value = (
        math.sin(d_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    )
    return radius * 2 * math.asin(math.sqrt(min(1.0, max(0.0, value))))


def _valid_coordinate(latitude: object, longitude: object) -> tuple[float, float] | None:
    try:
        lat, lon = float(latitude), float(longitude)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(lat) or not math.isfinite(lon):
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return lat, lon


def _scheduled_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _is_structural_place(place: Place) -> bool:
    category = re.sub(r"[^a-z0-9]+", "_", str(place.category or "").casefold()).strip("_")
    if category in STRUCTURAL_CATEGORIES:
        return True
    try:
        tags = json.loads(place.tags_json or "[]")
    except (TypeError, ValueError):
        tags = []
    try:
        metadata = json.loads(place.metadata_json or "{}")
    except (TypeError, ValueError):
        metadata = {}
    normalized_tags = {str(value).casefold().replace("-", "_") for value in tags} if isinstance(tags, list) else set()
    if normalized_tags & {"structural", "structural_anchor", "demo_anchor", "journey_anchor"}:
        return True
    if isinstance(metadata, dict) and any(
        metadata.get(key) is True for key in ("structural_anchor", "is_structural", "journey_anchor")
    ):
        return True
    name = " ".join(str(place.name or "").casefold().split())
    return bool(re.match(r"^(?:journey\s+)?(?:start|end|arrival|departure)\s+(?:point|anchor|location)$", name))


def _place_provenance(place: Place) -> str:
    provider = str(place.provider_key or "unknown").strip()
    normalized = provider.casefold()
    if any(token in normalized for token in ("mock", "fixture", "demo", "seed", "replay")):
        return "DEMO_FIXTURE"
    return f"CITY_PLACE_CATALOG:{provider[:80]}"


def _category_match(category: str, interest: str) -> bool:
    terms = INTEREST_TERMS.get(interest)
    if not terms:
        return False
    normalized = re.sub(r"[^a-z0-9]+", "_", category.casefold()).strip("_")
    return any(term in normalized for term in terms)


def _context_fingerprint(
    *,
    journey_id: str,
    member_id: str,
    observation_id: str,
    plan_id: str,
    split_id: str,
    rendezvous_activity_id: str,
    rendezvous_place_id: str,
    rendezvous_title: str,
    rendezvous_place_name: str,
    rendezvous_scheduled_at: str | None,
    budget_minutes: int,
    interest: str,
    candidates: list[dict],
) -> str:
    material = {
        "journey_id": journey_id,
        "member_id": member_id,
        "observation_id": observation_id,
        "plan_id": plan_id,
        "split_id": split_id,
        "rendezvous_activity_id": rendezvous_activity_id,
        "rendezvous_place_id": rendezvous_place_id,
        "rendezvous_title": rendezvous_title,
        "rendezvous_place_name": rendezvous_place_name,
        "rendezvous_scheduled_at": rendezvous_scheduled_at,
        "budget_minutes": budget_minutes,
        "interest": interest,
        "candidates": [
            [
                item["place_id"],
                item["name"],
                item["category"],
                item["straight_line_distance_m"],
                item["place_provenance"],
            ]
            for item in candidates
        ],
    }
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _bounded_place_query(session: Session, *, city_code: str, latitude: float, longitude: float):
    lat_delta = LOCALITY_RADIUS_M / 110_574.0
    lon_delta = min(180.0, LOCALITY_RADIUS_M / (111_320.0 * max(0.1, math.cos(math.radians(latitude)))))
    low_lat, high_lat = max(-90.0, latitude - lat_delta), min(90.0, latitude + lat_delta)
    low_lon, high_lon = longitude - lon_delta, longitude + lon_delta
    if low_lon < -180.0:
        longitude_filter = or_(Place.longitude >= low_lon + 360.0, Place.longitude <= high_lon)
    elif high_lon > 180.0:
        longitude_filter = or_(Place.longitude >= low_lon, Place.longitude <= high_lon - 360.0)
    else:
        longitude_filter = and_(Place.longitude >= low_lon, Place.longitude <= high_lon)
    # Cap the city catalogue only *after* locality ordering.  Cutting the
    # latest 500 updated rows first hides older genuinely nearby places in
    # dense cities and can produce a false NO_GROUNDED_OPTIONS.
    # This planar approximation only preselects; the final ranking still uses
    # exact Haversine distance and never treats either as a route ETA.
    latitude_delta_m = (Place.latitude - latitude) * 110_574.0
    raw_lon_delta = Place.longitude - longitude
    wrapped_lon_delta = case(
        (raw_lon_delta > 180.0, raw_lon_delta - 360.0),
        (raw_lon_delta < -180.0, raw_lon_delta + 360.0),
        else_=raw_lon_delta,
    )
    longitude_delta_m = wrapped_lon_delta * (
        111_320.0 * max(0.01, math.cos(math.radians(latitude)))
    )
    distance_squared = (
        latitude_delta_m * latitude_delta_m
        + longitude_delta_m * longitude_delta_m
    )
    return session.scalars(
        select(Place)
        .where(
            Place.city_code == city_code,
            Place.coordinate_system == "WGS84",
            Place.latitude >= low_lat,
            Place.latitude <= high_lat,
            longitude_filter,
        )
        .order_by(distance_squared.asc(), Place.id.asc())
        .limit(MAX_CITY_SHORTLIST_ROWS)
    ).all()


def build_moment_scout(
    session: Session,
    *,
    journey: Journey,
    member: JourneyMember,
    traveler_id: str,
    location_observation_id: str,
    budget_minutes: int,
    interest: str,
    rendezvous_context: dict,
) -> dict:
    """Build an owner-only shortlist from one fresh, member-bound location sample."""
    now = execution_clock.now().astimezone(timezone.utc)
    if journey.deleted_at is not None:
        raise HTTPException(409, "MOMENT_SCOUT_JOURNEY_UNAVAILABLE")
    split_intent_id = str(rendezvous_context.get("split_intent_id") or "") or None
    if split_intent_id:
        split = _active_split(session, journey, member, now=now)
        if str(split["intent"].id) != split_intent_id:
            raise HTTPException(409, "MOMENT_SCOUT_RENDEZVOUS_STALE")
        target: Activity = split["target"]
        rendezvous_place: Place = split["place"]
    else:
        split = None
        if str(journey.status or "").upper() != "ACTIVE" or not journey.active_plan_version_id:
            raise HTTPException(409, "MOMENT_SCOUT_REQUIRES_ACTIVE_JOURNEY")
        target = rendezvous_context.get("target")
        rendezvous_place = rendezvous_context.get("place")
        if (
            not isinstance(target, Activity)
            or not isinstance(rendezvous_place, Place)
            or str(target.plan_version_id) != str(journey.active_plan_version_id)
            or str(target.place_id or "") != str(rendezvous_place.id)
            or str(rendezvous_place.city_code) != str(journey.city_code)
            or str(target.status or "").upper() in {"COMPLETED", "SKIPPED", "CANCELLED"}
        ):
            raise HTTPException(409, "MOMENT_SCOUT_RENDEZVOUS_STALE")
    observation: LocationObservation = _fresh_member_observation(
        session,
        journey=journey,
        member=member,
        traveler_id=traveler_id,
        observation_id=location_observation_id,
    )
    origin = _valid_coordinate(observation.latitude, observation.longitude)
    if origin is None:
        raise HTTPException(409, "MOMENT_SCOUT_LOCATION_INVALID")
    try:
        accuracy = float(observation.accuracy_m)
    except (TypeError, ValueError):
        accuracy = math.inf
    if not math.isfinite(accuracy) or accuracy <= 0:
        raise HTTPException(409, "MOMENT_SCOUT_LOCATION_INVALID")

    active_plan_id = str(journey.active_plan_version_id or "")
    if not active_plan_id or str(target.plan_version_id) != active_plan_id:
        raise HTTPException(409, "MOMENT_SCOUT_RENDEZVOUS_STALE")

    quest_states = RETURN_REQUIRED_QUEST_STATES if split_intent_id else OPEN_QUEST_STATES
    quest_query = select(MemberSideQuest).where(
        MemberSideQuest.journey_id == str(journey.id),
        MemberSideQuest.member_id == str(member.id),
        MemberSideQuest.state.in_(quest_states),
    )
    if split_intent_id:
        quest_query = quest_query.where(
            MemberSideQuest.planned_separation_intent_id == split_intent_id
        )
    open_quest = session.scalar(
        quest_query
        .order_by(MemberSideQuest.created_at.desc())
    )
    if open_quest is not None and open_quest.state == "COMPLETED":
        raise HTTPException(409, "MOMENT_SCOUT_RETURN_TO_SHARED_GROUP_FIRST")
    if open_quest is not None and (
        not split_intent_id or open_quest.state not in {"DESTINATION_UNRESOLVED", "READY"}
    ):
        raise HTTPException(409, "MOMENT_SCOUT_STOP_ALREADY_IN_PROGRESS")

    shared_place_ids = {
        str(value)
        for value in session.scalars(
            select(Activity.place_id).where(
                Activity.plan_version_id == active_plan_id,
                Activity.place_id.is_not(None),
            )
        ).all()
        if value
    }
    shared_place_ids.add(str(rendezvous_place.id))

    nearby = _bounded_place_query(
        session,
        city_code=str(journey.city_code),
        latitude=origin[0],
        longitude=origin[1],
    )
    ranked: list[tuple[int, float, Place, str]] = []
    for place in nearby:
        if str(place.id) in shared_place_ids or _is_structural_place(place):
            continue
        coordinates = _valid_coordinate(place.latitude, place.longitude)
        if coordinates is None:
            continue
        distance = _haversine_m(origin[0], origin[1], coordinates[0], coordinates[1])
        if not math.isfinite(distance) or distance > LOCALITY_RADIUS_M:
            continue
        provenance = _place_provenance(place)
        category_rank = 0 if _category_match(str(place.category or ""), interest) else 1
        ranked.append((category_rank, distance, place, provenance))

    ranked.sort(key=lambda row: (row[0], row[1], str(row[2].name or "").casefold(), str(row[2].id)))
    candidates = [
        {
            "place_id": str(place.id),
            "name": str(place.name),
            "category": str(place.category or "unknown"),
            "straight_line_distance_m": int(round(distance)),
            "place_provenance": provenance,
            "opening_state": "UNKNOWN",
            "fit_authority": "UNVERIFIED",
        }
        for _category_rank, distance, place, provenance in ranked[:3]
    ]
    context = _context_fingerprint(
        journey_id=str(journey.id),
        member_id=str(member.id),
        observation_id=str(observation.id),
        plan_id=active_plan_id,
        split_id=split_intent_id or "NO_CURRENT_SPLIT",
        rendezvous_activity_id=str(target.id),
        rendezvous_place_id=str(rendezvous_place.id),
        rendezvous_title=str(target.title),
        rendezvous_place_name=str(rendezvous_place.name),
        rendezvous_scheduled_at=str(target.start_at) if target.start_at is not None else None,
        budget_minutes=budget_minutes,
        interest=interest,
        candidates=candidates,
    )
    captured_at = str(observation.captured_at)
    scheduled_at = _scheduled_datetime(target.start_at)
    minutes_until = None
    rendezvous_time_state = "UNKNOWN"
    if scheduled_at is not None:
        remaining_seconds = (scheduled_at - now).total_seconds()
        rendezvous_time_state = "PAST" if remaining_seconds < 0 else "AVAILABLE"
        minutes_until = max(0, int(remaining_seconds // 60))
    return {
        "scout_state": "OPTIONS_AVAILABLE" if candidates else "NO_GROUNDED_OPTIONS",
        "budget_minutes": budget_minutes,
        "interest": interest,
        "observed_at": captured_at,
        "scout_context": context,
        "evidence_boundary": "STRAIGHT_LINE_DISCOVERY_ONLY; NOT A ROUTE, ETA, OPENING CONFIRMATION, OR ARRIVAL GUARANTEE",
        "rendezvous": {
            "activity_id": str(target.id),
            "title": str(target.title),
            "place_name": str(rendezvous_place.name),
            "scheduled_at": target.start_at,
            "time_state": rendezvous_time_state,
            "minutes_until": minutes_until,
        },
        "candidates": candidates,
    }


def select_moment_scout_candidate(
    *,
    current_scout: dict,
    scout_context: str,
    place_id: str,
):
    """Validate that explicit confirmation still targets the current private shortlist."""
    if not hmac.compare_digest(str(current_scout["scout_context"]), str(scout_context)):
        raise HTTPException(409, "MOMENT_SCOUT_CONTEXT_STALE")
    if not any(str(item["place_id"]) == str(place_id) for item in current_scout["candidates"]):
        raise HTTPException(409, "MOMENT_SCOUT_CANDIDATE_UNAVAILABLE")
    if current_scout["rendezvous"]["time_state"] == "PAST":
        raise HTTPException(409, "MOMENT_SCOUT_RENDEZVOUS_TIME_PASSED")
