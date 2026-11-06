from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from fastapi import HTTPException
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import (
    Activity,
    Journey,
    JourneyMember,
    MemberLiveMomentScoutReceipt,
    MemberSideQuest,
    Place,
)
from app.execution.clock import execution_clock
from app.execution.group_presence import _member_in_force, planned_separation_for_member
from app.execution.member_side_quest import _fresh_member_observation
from app.execution.moment_scout import _haversine_m, build_moment_scout
from app.integrations.discovery.real_city import DiscoveryCandidate, OverpassPOIProvider

LIVE_RECEIPT_TTL = timedelta(minutes=5)
LIVE_QUERY_BY_INTEREST = {
    "ANYTHING": "attraction",
    "COFFEE_FOOD": "cafe",
    "CULTURE": "museum",
    "NATURE": "park",
}
ALLOWED_CATEGORIES = {
    "ANYTHING": {"attraction", "viewpoint"},
    "COFFEE_FOOD": {"cafe", "restaurant", "food_court"},
    "CULTURE": {"museum", "gallery", "arts_centre", "library", "theatre"},
    "NATURE": {"park", "garden", "viewpoint"},
}
OSM_ID = re.compile(r"^overpass:(node|way|relation)/([1-9][0-9]*)$")
PRIVATE_LOCATION_FAILURES = {
    "LOCATION_OBSERVATION_NOT_FOUND",
    "LOCATION_OBSERVATION_MEMBER_MISMATCH",
    "SIDE_QUEST_LOCATION_SOURCE_UNSUPPORTED",
    "SIDE_QUEST_LOCATION_TIME_UNKNOWN",
    "SIDE_QUEST_LOCATION_STALE",
    "SIDE_QUEST_LOCATION_IMPRECISE",
    "MOMENT_SCOUT_LOCATION_INVALID",
}
PUBLIC_OSM_TAGS = {
    "name",
    "tourism",
    "amenity",
    "leisure",
    "opening_hours",
    "fee",
}


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _status(
    state: str,
    *,
    budget_minutes: int,
    interest: str,
    radius_m: int,
    message: str,
    **extra: object,
) -> dict:
    return {
        "scout_state": state,
        "budget_minutes": budget_minutes,
        "interest": interest,
        "radius_m": radius_m,
        "message": message,
        "candidates": [],
        **extra,
    }


def _safe_attempts(attempts: object) -> list[dict[str, object]]:
    if not isinstance(attempts, list):
        return []
    result: list[dict[str, object]] = []
    for item in attempts[:6]:
        if not isinstance(item, dict):
            continue
        raw_endpoint = str(item.get("endpoint") or "")
        parts = urlsplit(raw_endpoint)
        endpoint = ""
        if parts.scheme in {"http", "https"} and parts.hostname:
            endpoint = f"{parts.scheme}://{parts.hostname}"
            if parts.path and parts.path != "/":
                endpoint += parts.path[:100]
        error = str(item.get("error") or "")
        failure_class = error.split(":", 1)[0].strip()[:60] if error else None
        result.append(
            {
                "endpoint": endpoint or "configured_overpass_endpoint",
                "answered": bool(item.get("answered")),
                "http_status": item.get("http_status")
                if isinstance(item.get("http_status"), int)
                else None,
                "elapsed_ms": item.get("elapsed_ms")
                if isinstance(item.get("elapsed_ms"), (int, float))
                else None,
                "elements": item.get("elements") if isinstance(item.get("elements"), int) else None,
                "failure_class": failure_class,
            }
        )
    return result


def _valid_live_candidate(
    candidate: DiscoveryCandidate,
    *,
    interest: str,
    latitude: float,
    longitude: float,
    radius_m: int,
) -> dict | None:
    identity = OSM_ID.fullmatch(str(candidate.external_id or ""))
    category = str(candidate.category or "").strip().casefold()
    name = " ".join(str(candidate.name or "").split())
    if (
        candidate.provider_key != "overpass"
        or identity is None
        or not name
        or name.casefold() == f"osm {category}"
        or category not in ALLOWED_CATEGORIES.get(interest, set())
    ):
        return None
    try:
        place_latitude, place_longitude = float(candidate.latitude), float(candidate.longitude)
    except (TypeError, ValueError):
        return None
    if (
        not math.isfinite(place_latitude)
        or not math.isfinite(place_longitude)
        or not -90 <= place_latitude <= 90
        or not -180 <= place_longitude <= 180
    ):
        return None
    distance = _haversine_m(latitude, longitude, place_latitude, place_longitude)
    if not math.isfinite(distance) or distance > radius_m:
        return None

    metadata = candidate.metadata if isinstance(candidate.metadata, dict) else {}
    raw_tags = metadata.get("tags") if isinstance(metadata.get("tags"), dict) else {}
    # Explicit OSM restrictions must survive any metadata normalization. A
    # downstream public Place must never be born from access=private/no etc.
    # Absent access tags remain UNKNOWN, never proof that a venue is open.
    forbidden_access = {"private", "no", "restricted", "staff", "customers", "permit", "residents"}
    for facts in (metadata, raw_tags, metadata.get("osm_tags")):
        if not isinstance(facts, dict):
            continue
        if facts.get("is_public") is False or facts.get("public") is False:
            return None
        if str(facts.get("public") or "").strip().casefold() in {"no", "false", "0"}:
            return None
        if any(
            str(facts.get(key) or "").strip().casefold() in forbidden_access
            for key in ("access", "visibility")
        ):
            return None
    public_tags = {
        str(key): str(value)[:300]
        for key, value in raw_tags.items()
        if str(key) in PUBLIC_OSM_TAGS and isinstance(value, (str, int, float))
    }
    osm_type, osm_id = identity.groups()
    source_url = f"https://www.openstreetmap.org/{osm_type}/{osm_id}"
    return {
        "candidate_id": str(uuid4()),
        "provider_key": "overpass",
        "external_id": candidate.external_id,
        "osm_type": osm_type,
        "osm_id": osm_id,
        "name": name[:240],
        "category": category[:80],
        "latitude": place_latitude,
        "longitude": place_longitude,
        "straight_line_distance_m": round(distance),
        "tags": public_tags,
        "opening_state": "UNKNOWN",
        "availability_confidence": "UNVERIFIED",
        "fit_authority": "UNVERIFIED",
        "source_url": source_url,
    }


def discover_live_moment_scout(
    session: Session,
    *,
    journey: Journey,
    member: JourneyMember,
    traveler_id: str,
    location_observation_id: str,
    budget_minutes: int,
    interest: str,
    radius_m: int,
    consent_external_location: bool,
    rendezvous_context: dict,
) -> dict:
    """Make one consented Overpass attempt and keep the results in an owner-bound receipt."""
    if consent_external_location is not True:
        raise HTTPException(422, "LIVE_MOMENT_SCOUT_EXTERNAL_LOCATION_CONSENT_REQUIRED")
    if not settings.real_city_enabled or str(settings.real_city_mode).casefold() != "live":
        return _status(
            "PROVIDER_DISABLED",
            budget_minutes=budget_minutes,
            interest=interest,
            radius_m=radius_m,
            message="Live OpenStreetMap search is disabled in this environment.",
            provider="overpass",
        )

    try:
        local_context = build_moment_scout(
            session,
            journey=journey,
            member=member,
            traveler_id=traveler_id,
            location_observation_id=location_observation_id,
            budget_minutes=budget_minutes,
            interest=interest,
            rendezvous_context=rendezvous_context,
        )
    except HTTPException as exc:
        if str(exc.detail) in PRIVATE_LOCATION_FAILURES:
            return _status(
                "LOCATION_UNAVAILABLE",
                budget_minutes=budget_minutes,
                interest=interest,
                radius_m=radius_m,
                message="A fresh, precise location from your signed-in Journey membership is required.",
                reason_code="LOCATION_UNAVAILABLE",
            )
        raise

    if local_context["rendezvous"]["time_state"] == "PAST":
        raise HTTPException(409, "LIVE_MOMENT_SCOUT_RENDEZVOUS_TIME_PASSED")

    observation = _fresh_member_observation(
        session,
        journey=journey,
        member=member,
        traveler_id=traveler_id,
        observation_id=location_observation_id,
    )
    origin_latitude = float(observation.latitude)
    origin_longitude = float(observation.longitude)
    provider_latitude = round(origin_latitude, 3)
    provider_longitude = round(origin_longitude, 3)
    now_text = execution_clock.now().astimezone(timezone.utc).isoformat()
    session.execute(
        delete(MemberLiveMomentScoutReceipt).where(
            MemberLiveMomentScoutReceipt.journey_id == str(journey.id),
            MemberLiveMomentScoutReceipt.member_id == str(member.id),
            MemberLiveMomentScoutReceipt.expires_at <= now_text,
        )
    )
    session.commit()
    provider = OverpassPOIProvider()
    try:
        candidates, attempts = provider.search_with_evidence(
            LIVE_QUERY_BY_INTEREST[interest],
            latitude=provider_latitude,
            longitude=provider_longitude,
            radius_m=radius_m,
            limit=12,
        )
    except (httpx.HTTPError, ValueError, TypeError, RuntimeError) as exc:
        attempts = getattr(exc, "provider_attempts", [])
        safe_attempts = _safe_attempts(attempts)
        return _status(
            "PROVIDER_UNAVAILABLE",
            budget_minutes=budget_minutes,
            interest=interest,
            radius_m=radius_m,
            message="OpenStreetMap did not answer this one-time search. Your local and manual options remain available.",
            provider="overpass",
            captured_at=execution_clock.now().astimezone(timezone.utc).isoformat(),
            attempts=safe_attempts,
        )
    finally:
        provider.close()

    # A network call can span membership revocation, split replacement or
    # a shared-plan change. Re-load from the DB before returning private leads
    # or writing a member-owned receipt; the initial authorization is not a
    # timeless grant.
    session.expire_all()
    current_now = execution_clock.now().astimezone(timezone.utc)
    if (
        str(journey.status or "").upper() != "ACTIVE"
        or journey.deleted_at is not None
        or not journey.active_plan_version_id
        or str(member.traveler_id) != str(traveler_id)
        or str(member.journey_id) != str(journey.id)
        or not _member_in_force(member, current_now)
    ):
        raise HTTPException(409, "LIVE_MOMENT_SCOUT_CONTEXT_CHANGED")
    current_split = planned_separation_for_member(
        session, journey, member, now=current_now
    )
    current_split_id = str(current_split["intent"].id) if current_split else ""
    expected_split_id = str(rendezvous_context.get("split_intent_id") or "")
    if current_split_id != expected_split_id:
        raise HTTPException(409, "LIVE_MOMENT_SCOUT_CONTEXT_CHANGED")
    try:
        local_context = build_moment_scout(
            session,
            journey=journey,
            member=member,
            traveler_id=traveler_id,
            location_observation_id=location_observation_id,
            budget_minutes=budget_minutes,
            interest=interest,
            rendezvous_context=rendezvous_context,
        )
    except HTTPException:
        raise HTTPException(409, "LIVE_MOMENT_SCOUT_CONTEXT_CHANGED") from None
    if (
        local_context["rendezvous"]["time_state"] == "PAST"
        or str(local_context["rendezvous"]["activity_id"])
        != str(rendezvous_context["target"].id)
    ):
        raise HTTPException(409, "LIVE_MOMENT_SCOUT_CONTEXT_CHANGED")

    safe_attempts = _safe_attempts(attempts)
    answered = any(item["answered"] for item in safe_attempts)
    captured_at = execution_clock.now().astimezone(timezone.utc)
    captured_text = captured_at.isoformat()
    if not answered:
        return _status(
            "PROVIDER_UNAVAILABLE",
            budget_minutes=budget_minutes,
            interest=interest,
            radius_m=radius_m,
            message="OpenStreetMap did not answer this one-time search. Your local and manual options remain available.",
            provider="overpass",
            captured_at=captured_text,
            attempts=safe_attempts,
        )

    valid = [
        item
        for candidate in candidates
        if (
            item := _valid_live_candidate(
                candidate,
                interest=interest,
                latitude=origin_latitude,
                longitude=origin_longitude,
                radius_m=radius_m,
            )
        )
        is not None
    ]
    # External OSM discovery must not re-suggest the already selected shared
    # itinerary or the rendezvous as a "new private adventure".
    active_plan_id = str(journey.active_plan_version_id or "")
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
    shared_place_ids.add(str(rendezvous_context["place"].id))
    shared_places = session.scalars(
        select(Place).where(Place.id.in_(shared_place_ids))
    ).all()
    shared_external_ids = {
        str(place.external_id)
        for place in shared_places
        if str(place.provider_key) == "overpass"
    }
    def is_shared_stop(item: dict) -> bool:
        if str(item["external_id"]) in shared_external_ids:
            return True
        normalized_name = " ".join(str(item["name"]).casefold().split())
        return any(
            normalized_name == " ".join(str(place.name or "").casefold().split())
            and _haversine_m(
                float(item["latitude"]),
                float(item["longitude"]),
                float(place.latitude),
                float(place.longitude),
            ) <= 20
            for place in shared_places
        )
    valid = [item for item in valid if not is_shared_stop(item)]
    valid.sort(key=lambda item: (item["straight_line_distance_m"], item["name"].casefold()))
    valid = valid[:3]
    shared_rendezvous = local_context["rendezvous"]
    response_base = {
        "provider": "overpass",
        "provider_provenance": "LIVE_OPENSTREETMAP_OVERPASS",
        "captured_at": captured_text,
        "attribution": "© OpenStreetMap contributors",
        "attribution_url": "https://www.openstreetmap.org/copyright",
        "attempts": safe_attempts,
        "rendezvous": shared_rendezvous,
        "evidence_boundary": (
            "REAL_NAMED_OSM_PLACE_LEADS_ONLY; NOT OPEN_NOW, PRICE, ACCESS, ROUTE, ETA, "
            "PHYSICAL_ARRIVAL, OR GUARANTEED_RETURN_FIT"
        ),
    }
    if not valid:
        return _status(
            "LIVE_ZERO_RESULTS",
            budget_minutes=budget_minutes,
            interest=interest,
            radius_m=radius_m,
            message="OpenStreetMap answered, but returned no named, relevant nearby places to act on.",
            empty_reason="NO_ACTIONABLE_POIS",
            **response_base,
        )

    target = rendezvous_context["target"]
    rendezvous_place = rendezvous_context["place"]
    receipt_id = str(uuid4())
    expires_at = (captured_at + LIVE_RECEIPT_TTL).isoformat()
    receipt = MemberLiveMomentScoutReceipt(
        id=receipt_id,
        journey_id=str(journey.id),
        member_id=str(member.id),
        traveler_id=str(traveler_id),
        location_observation_id=str(observation.id),
        active_plan_version_id=active_plan_id,
        split_intent_id=(
            str(rendezvous_context["split_intent_id"])
            if rendezvous_context.get("split_intent_id")
            else None
        ),
        rendezvous_activity_id=str(target.id),
        rendezvous_place_id=str(rendezvous_place.id),
        budget_minutes=int(budget_minutes),
        interest=interest,
        candidates_json=_json(valid),
        provider_attempts_json=_json(safe_attempts),
        provider_key="overpass",
        captured_at=captured_text,
        expires_at=expires_at,
        status="OPEN",
        created_at=captured_text,
        updated_at=captured_text,
    )
    session.add(receipt)
    session.commit()
    public_candidates = [
        {
            "candidate_id": item["candidate_id"],
            "name": item["name"],
            "category": item["category"],
            "straight_line_distance_m": item["straight_line_distance_m"],
            "osm_type": item["osm_type"],
            "osm_id": item["osm_id"],
            "source_url": item["source_url"],
            "opening_state": "UNKNOWN",
            "availability_confidence": "UNVERIFIED",
            "fit_authority": "UNVERIFIED",
        }
        for item in valid
    ]
    return {
        "scout_state": "LIVE_RESULTS",
        "budget_minutes": budget_minutes,
        "interest": interest,
        "radius_m": radius_m,
        "receipt_id": receipt_id,
        "expires_at": expires_at,
        "candidates": public_candidates,
        **response_base,
    }


def validate_live_scout_selection(
    session: Session,
    *,
    journey: Journey,
    member: JourneyMember,
    traveler_id: str,
    receipt_id: str,
    candidate_id: str,
    rendezvous_context: dict,
) -> tuple[MemberLiveMomentScoutReceipt, dict, bool]:
    receipt = session.get(MemberLiveMomentScoutReceipt, str(receipt_id))
    if receipt is None:
        raise HTTPException(409, "SCOUT_EXPIRED")
    if (
        str(receipt.journey_id) != str(journey.id)
        or str(receipt.member_id) != str(member.id)
        or str(receipt.traveler_id) != str(traveler_id)
    ):
        raise HTTPException(404, "SCOUT_EXPIRED")
    if receipt.status not in {"OPEN", "SELECTING", "SELECTED"}:
        raise HTTPException(409, "SCOUT_EXPIRED")
    try:
        expires_at = datetime_from_iso(receipt.expires_at)
    except (TypeError, ValueError):
        raise HTTPException(409, "SCOUT_EXPIRED") from None
    now = execution_clock.now().astimezone(timezone.utc)
    if expires_at <= now:
        receipt.status = "EXPIRED"
        receipt.location_observation_id = ""
        receipt.interest = "EXPIRED"
        receipt.candidates_json = "[]"
        receipt.provider_attempts_json = "[]"
        receipt.selected_candidate_id = None
        receipt.selected_place_id = None
        receipt.side_quest_id = None
        receipt.updated_at = now.isoformat()
        session.commit()
        raise HTTPException(409, "SCOUT_EXPIRED")
    if (
        str(journey.active_plan_version_id or "") != str(receipt.active_plan_version_id)
        or str(rendezvous_context["target"].id) != str(receipt.rendezvous_activity_id)
        or str(rendezvous_context["place"].id) != str(receipt.rendezvous_place_id)
    ):
        raise HTTPException(409, "SCOUT_EXPIRED")
    current_split_id = str(rendezvous_context.get("split_intent_id") or "")
    saved_split_id = str(receipt.split_intent_id or "")
    if saved_split_id and current_split_id != saved_split_id:
        raise HTTPException(409, "SCOUT_EXPIRED")
    if receipt.status == "OPEN" and current_split_id != saved_split_id:
        raise HTTPException(409, "SCOUT_EXPIRED")

    if receipt.status != "SELECTED":
        # R66 rejects selection after the shared meeting time. Live discovery
        # has the same product boundary, including a provider response that
        # arrives just as the rendezvous becomes overdue.
        scheduled = rendezvous_context["target"].start_at
        if scheduled is not None:
            from app.execution.moment_scout import _scheduled_datetime
            meeting_at = _scheduled_datetime(scheduled)
            if meeting_at is not None and meeting_at < now:
                raise HTTPException(409, "LIVE_MOMENT_SCOUT_RENDEZVOUS_TIME_PASSED")

    if receipt.status == "SELECTED":
        _fresh_member_observation(
            session,
            journey=journey,
            member=member,
            traveler_id=traveler_id,
            observation_id=str(receipt.location_observation_id),
        )
        quest = session.get(MemberSideQuest, str(receipt.side_quest_id or ""))
        if (
            quest is None
            or str(quest.journey_id) != str(journey.id)
            or str(quest.member_id) != str(member.id)
            or str(quest.traveler_id) != str(traveler_id)
            or str(quest.active_plan_version_id) != str(receipt.active_plan_version_id)
            or str(quest.side_place_id or "") != str(receipt.selected_place_id or "")
        ):
            raise HTTPException(409, "SCOUT_EXPIRED")
    else:
        # Recheck freshness and the current R64 state before every consume. This
        # reads only the member's bound observation and never writes Journey data.
        build_moment_scout(
            session,
            journey=journey,
            member=member,
            traveler_id=traveler_id,
            location_observation_id=str(receipt.location_observation_id),
            budget_minutes=int(receipt.budget_minutes),
            interest=str(receipt.interest),
            rendezvous_context=rendezvous_context,
        )
    try:
        candidates = json.loads(receipt.candidates_json)
    except (TypeError, ValueError):
        raise HTTPException(409, "SCOUT_EXPIRED") from None
    if not isinstance(candidates, list):
        raise HTTPException(409, "SCOUT_EXPIRED")
    candidate = next(
        (
            item
            for item in candidates
            if isinstance(item, dict) and str(item.get("candidate_id")) == str(candidate_id)
        ),
        None,
    )
    if candidate is None:
        raise HTTPException(409, "SCOUT_EXPIRED")
    if receipt.status in {"SELECTING", "SELECTED"} and str(
        receipt.selected_candidate_id or ""
    ) != str(candidate_id):
        raise HTTPException(409, "SCOUT_SELECTION_ALREADY_STARTED")
    return receipt, candidate, receipt.status == "SELECTED"


def datetime_from_iso(value: str):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("timezone required")
    return parsed.astimezone(timezone.utc)


def materialize_selected_live_place(
    session: Session,
    *,
    journey: Journey,
    candidate: dict,
    captured_at: str,
) -> Place:
    identity = OSM_ID.fullmatch(str(candidate.get("external_id") or ""))
    if (
        candidate.get("provider_key") != "overpass"
        or identity is None
        or identity.group(1) != candidate.get("osm_type")
        or identity.group(2) != candidate.get("osm_id")
    ):
        raise HTTPException(409, "SCOUT_EXPIRED")
    existing = session.scalar(
        select(Place).where(
            Place.provider_key == "overpass",
            Place.external_id == str(candidate["external_id"]),
        )
    )
    if existing is not None:
        if str(existing.city_code) != str(journey.city_code):
            raise HTTPException(409, "SCOUT_PLACE_CITY_MISMATCH")
        return existing

    try:
        latitude = float(candidate["latitude"])
        longitude = float(candidate["longitude"])
    except (KeyError, TypeError, ValueError):
        raise HTTPException(409, "SCOUT_EXPIRED") from None
    if (
        not math.isfinite(latitude)
        or not math.isfinite(longitude)
        or not -90 <= latitude <= 90
        or not -180 <= longitude <= 180
    ):
        raise HTTPException(409, "SCOUT_EXPIRED")
    tags = candidate.get("tags") if isinstance(candidate.get("tags"), dict) else {}
    place_id = "real-place-" + hashlib.sha1(
        f"overpass|{candidate['external_id']}".encode()
    ).hexdigest()[:24]
    place = Place(
        id=place_id,
        provider_key="overpass",
        external_id=str(candidate["external_id"]),
        city_code=str(journey.city_code),
        name=str(candidate["name"])[:240],
        category=str(candidate["category"])[:80],
        latitude=latitude,
        longitude=longitude,
        opening_hours_json=_json({"status": "UNKNOWN", "source": "OSM_TAG_NOT_EVALUATED"}),
        tags_json=_json([f"{key}={value}" for key, value in sorted(tags.items())]),
        metadata_json=_json(
            {
                "source": "OpenStreetMap",
                "source_url": str(candidate["source_url"]),
                "license": "ODbL-1.0",
                "osm_type": identity.group(1),
                "osm_id": identity.group(2),
                "discovery_mode": "EXPLICIT_MEMBER_OPT_IN_LIVE",
                "city_scope_basis": "ACTIVE_JOURNEY_CONTEXT_ONLY",
                "opening_status": "UNKNOWN",
                "return_fit": "UNVERIFIED",
            }
        ),
        updated_at=captured_at,
        coordinate_system="WGS84",
        source_coordinate_system="WGS84",
    )
    session.add(place)
    session.flush()
    return place
