"""Grounded launch-origin projection for the first/next Journey move."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    AccommodationOfferSnapshot,
    Activity,
    Journey,
    JourneyTripContext,
    JourneyLaunchContext,
    LocationObservation,
    StayJourneyOption,
    JourneyLeg,
    MobilityFact,
    PlanVersion,
)
from app.execution.clock import execution_clock
from app.execution.location_scope import PRIVATE_BUDDY_ROUTE_LOCATION_SOURCE

_MAX_LOCATION_AGE_SECONDS = 300.0
_MAX_LOCATION_ACCURACY_M = 100.0


def _aware(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _load(value: str | None) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _fresh_location(
    session: Session, journey: Journey, *, now: datetime
) -> LocationObservation | None:
    row = session.scalar(
        select(LocationObservation)
        .where(
            LocationObservation.journey_id == journey.id,
            LocationObservation.source != PRIVATE_BUDDY_ROUTE_LOCATION_SOURCE,
        )
        .order_by(LocationObservation.captured_at.desc(), LocationObservation.id.desc())
    )
    if row is None:
        return None
    captured = _aware(row.captured_at)
    if captured is None:
        return None
    age = (now - captured).total_seconds()
    if age < -60 or age > _MAX_LOCATION_AGE_SECONDS:
        return None
    if float(row.accuracy_m) > _MAX_LOCATION_ACCURACY_M:
        return None
    return row


def _selected_stay(
    session: Session,
    journey: Journey,
    *,
    target_start: datetime | None,
) -> tuple[StayJourneyOption, AccommodationOfferSnapshot] | None:
    option = session.scalar(
        select(StayJourneyOption)
        .where(
            StayJourneyOption.journey_id == journey.id,
            StayJourneyOption.status == "SELECTED",
        )
        .order_by(StayJourneyOption.rank, StayJourneyOption.id)
    )
    if option is None:
        return None
    offer = session.get(AccommodationOfferSnapshot, option.accommodation_offer_id)
    if offer is None:
        return None
    if target_start is None:
        return None
    check_in = _aware(offer.check_in_at)
    check_out = _aware(offer.check_out_at)
    if check_in is None or check_out is None:
        return None
    if not (check_in <= target_start < check_out):
        return None
    return option, offer


def _arrival_anchor(
    session: Session,
    journey: Journey,
    *,
    target_start: datetime | None,
) -> dict[str, Any] | None:
    context = session.get(JourneyTripContext, journey.id)
    if context is None:
        return None
    anchor = _load(context.arrival_anchor_json)
    if not anchor:
        return None
    at = _aware(str(anchor.get("at") or ""))
    if at is None or target_start is None or at > target_start:
        return None
    if anchor.get("latitude") is None or anchor.get("longitude") is None:
        return None
    return anchor


def _planned_contract(
    session: Session,
    journey: Journey,
    *,
    target_activity: Activity | None,
) -> JourneyLaunchContext | None:
    """Return a user-confirmed launch contract only when it binds this target."""

    contract = session.get(JourneyLaunchContext, journey.id)
    if contract is None or str(contract.authority_kind) != "USER_CONFIRMED_PLANNED_ORIGIN":
        return None
    target_id = str(target_activity.id) if target_activity is not None else None
    if contract.target_activity_id and str(contract.target_activity_id) != target_id:
        return None
    target_start = _aware(target_activity.start_at if target_activity is not None else None)
    if target_start is None:
        return None
    valid_from = _aware(contract.valid_from)
    valid_until = _aware(contract.valid_until)
    if valid_from is not None and target_start < valid_from:
        return None
    if valid_until is not None and target_start >= valid_until:
        return None
    if contract.latitude is None or contract.longitude is None:
        return None
    return contract


def grounded_launch_origin(
    session: Session,
    journey: Journey,
    *,
    target_activity: Activity | None,
    previous_activity: Activity | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Resolve one routable launch origin without asserting physical execution."""

    stamp = (now or execution_clock.now()).astimezone(timezone.utc)
    target_start = _aware(target_activity.start_at if target_activity is not None else None)
    target_id = str(target_activity.id) if target_activity is not None else None

    if previous_activity is not None and previous_activity.place_id:
        return {
            "state": "READY",
            "source": "PREVIOUS_ACTIVITY",
            "authority": "PLAN_EXECUTION_CONTEXT",
            "origin_key": str(previous_activity.place_id),
            "name": getattr(previous_activity, "title", None),
            "latitude": None,
            "longitude": None,
            "captured_at": None,
            "provider_key": None,
            "target_activity_id": target_id,
            "reason_code": "PREVIOUS_ACTIVITY_HAS_EXACT_PLACE",
        }

    location = _fresh_location(session, journey, now=stamp)
    if location is not None:
        return {
            "state": "READY",
            "source": "FOREGROUND_LOCATION",
            "authority": "ADVISORY_FOREGROUND",
            "origin_key": f"location:{location.id}",
            "name": "Shared location",
            "latitude": float(location.latitude),
            "longitude": float(location.longitude),
            "captured_at": location.captured_at,
            "provider_key": None,
            "target_activity_id": target_id,
            "reason_code": "FRESH_FOREGROUND_LOCATION",
        }

    contract = _planned_contract(session, journey, target_activity=target_activity)
    if contract is not None:
        if contract.place_id:
            origin_key = str(contract.place_id)
        elif contract.source_ref_type == "ACCOMMODATION_OFFER" and contract.source_ref_id:
            origin_key = f"stay-offer:{contract.source_ref_id}"
        else:
            origin_key = str(contract.source_ref_id or "planned-launch")
        return {
            "state": "READY",
            "source": str(contract.source_kind),
            "authority": str(contract.authority_kind),
            "origin_key": origin_key,
            "name": contract.display_name,
            "latitude": float(contract.latitude),
            "longitude": float(contract.longitude),
            "captured_at": contract.updated_at,
            "provider_key": None,
            "target_activity_id": target_id,
            "reason_code": "USER_CONFIRMED_PLANNED_ORIGIN",
            "launch_context_id": journey.id,
            "source_ref_type": contract.source_ref_type,
            "source_ref_id": contract.source_ref_id,
            "valid_from": contract.valid_from,
            "valid_until": contract.valid_until,
        }

    stay = _selected_stay(session, journey, target_start=target_start)
    if stay is not None:
        option, offer = stay
        if getattr(offer, "latitude", None) is not None and getattr(offer, "longitude", None) is not None:
            return {
                "state": "READY",
                "source": "SELECTED_STAY",
                "authority": "PLANNED_GROUNDED_ORIGIN",
                "origin_key": f"stay-offer:{offer.id}",
                "name": offer.property_name,
                "latitude": float(offer.latitude),
                "longitude": float(offer.longitude),
                "captured_at": offer.captured_at,
                "provider_key": offer.provider_key,
                "target_activity_id": target_id,
                "reason_code": "SELECTED_STAY_COVERS_TARGET_START",
                "stay_option_id": option.id,
                "stay_offer_id": offer.id,
            }

    anchor = _arrival_anchor(session, journey, target_start=target_start)
    if anchor is not None:
        return {
            "state": "READY",
            "source": "ARRIVAL_ANCHOR",
            "authority": "PLANNED_GROUNDED_ORIGIN",
            "origin_key": str(anchor.get("semantic_key") or anchor.get("reservation_id") or "arrival-anchor"),
            "name": anchor.get("location") or anchor.get("name") or "Arrival point",
            "latitude": float(anchor["latitude"]),
            "longitude": float(anchor["longitude"]),
            "captured_at": anchor.get("at"),
            "provider_key": anchor.get("provider_key"),
            "target_activity_id": target_id,
            "reason_code": "ARRIVAL_ANCHOR_PRECEDES_TARGET",
        }

    return {
        "state": "UNKNOWN_ORIGIN",
        "source": None,
        "authority": "NONE",
        "origin_key": None,
        "name": None,
        "latitude": None,
        "longitude": None,
        "captured_at": None,
        "provider_key": None,
        "target_activity_id": target_id,
        "reason_code": "NO_GROUNDED_LAUNCH_ORIGIN",
    }


def launch_edge_projection(
    session: Session,
    journey: Journey,
    *,
    origin: dict[str, Any],
    target_activity: Activity | None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Resolve only one exact grounded-origin -> target Place edge."""

    target_key = str(target_activity.place_id) if target_activity is not None and target_activity.place_id else None
    origin_key = str(origin.get("origin_key") or "") or None
    base = {
        "state": "LAUNCH_EDGE_UNAVAILABLE",
        "kind": "LAUNCH_EDGE",
        "origin_key": origin_key,
        "target_key": target_key,
        "from_activity_id": None,
        "to_activity_id": str(target_activity.id) if target_activity is not None else None,
        "provider": None,
        "provenance": None,
        "captured_at": None,
        "expires_at": None,
        "duration_min": None,
        "distance_m": None,
        "mode": None,
        "walking_min": None,
        "degraded": False,
        "geometry": None,
        "reason_code": "NO_EXACT_LAUNCH_EDGE",
    }
    if origin.get("state") != "READY" or not origin_key or not target_key:
        return {**base, "reason_code": "LAUNCH_ORIGIN_OR_TARGET_UNAVAILABLE"}

    fact = session.scalars(
        select(MobilityFact)
        .where(
            MobilityFact.journey_id == journey.id,
            MobilityFact.from_key == origin_key,
            MobilityFact.to_key == target_key,
        )
        .order_by(MobilityFact.captured_at.desc(), MobilityFact.id.desc())
    ).first()
    if fact is not None:
        evidence = _load(fact.evidence_json)
        return {
            **base,
            "state": "LAUNCH_ROUTE_READY_FROZEN" if fact.expires_at and _aware(fact.expires_at) and _aware(fact.expires_at) < (now or execution_clock.now()).astimezone(timezone.utc) else "LAUNCH_ROUTE_READY",
            "provider": fact.provider_key,
            "provenance": fact.provenance,
            "captured_at": fact.captured_at,
            "expires_at": fact.expires_at,
            "duration_min": fact.duration_min,
            "distance_m": fact.distance_m,
            "mode": fact.mode,
            "walking_min": fact.walking_min,
            "degraded": bool(evidence.get("degraded", False)),
            "geometry": _load(fact.geometry_json),
            "reason_code": "EXACT_MOBILITY_FACT_MATCH",
        }

    plan = session.get(PlanVersion, journey.active_plan_version_id) if journey.active_plan_version_id else None
    evidence = _load(plan.mobility_evidence_json) if plan is not None else {}
    facts = evidence.get("facts") if isinstance(evidence, dict) else []
    for raw in facts or []:
        if not isinstance(raw, dict):
            continue
        if str(raw.get("origin_key") or raw.get("from_key") or "") != origin_key:
            continue
        if str(raw.get("destination_key") or raw.get("to_key") or "") != target_key:
            continue
        return {
            **base,
            "state": "LAUNCH_ROUTE_READY_FROZEN",
            "provider": raw.get("provider") or raw.get("route_provider") or evidence.get("source_provider"),
            "provenance": raw.get("route_provenance") or raw.get("fact_quality") or evidence.get("aggregate_provenance"),
            "captured_at": raw.get("captured_at") or evidence.get("captured_at"),
            "expires_at": raw.get("expires_at") or evidence.get("expires_at"),
            "duration_min": raw.get("minutes") if raw.get("minutes") is not None else raw.get("duration_min"),
            "distance_m": raw.get("distance_m"),
            "mode": raw.get("mode"),
            "walking_min": raw.get("walking_min"),
            "degraded": bool(raw.get("degraded", evidence.get("degraded", False))),
            "geometry": raw.get("route_geometry") or raw.get("geometry"),
            "reason_code": "EXACT_PLAN_LAUNCH_FACT_MATCH",
        }

    leg = session.scalars(
        select(JourneyLeg)
        .where(
            JourneyLeg.journey_id == journey.id,
            JourneyLeg.plan_version_id == journey.active_plan_version_id,
            JourneyLeg.from_key == origin_key,
            JourneyLeg.to_key == target_key,
        )
        .order_by(JourneyLeg.captured_at.desc(), JourneyLeg.id.desc())
    ).first() if journey.active_plan_version_id else None
    if leg is not None:
        return {
            **base,
            "state": "LAUNCH_ROUTE_READY_FROZEN",
            "provider": leg.provider_key,
            "provenance": leg.provenance,
            "captured_at": leg.captured_at,
            "expires_at": leg.expires_at,
            "duration_min": leg.duration_min,
            "distance_m": leg.distance_m,
            "mode": leg.mode,
            "walking_min": leg.walking_min,
            "degraded": False,
            "geometry": _load(leg.geometry_json),
            "reason_code": "EXACT_JOURNEY_LEG_MATCH",
        }
    return base


def launch_context_projection(
    session: Session,
    journey: Journey,
    *,
    target_activity: Activity | None,
    previous_activity: Activity | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Combine grounded origin and exact launch edge without mutating execution truth."""

    origin = grounded_launch_origin(
        session,
        journey,
        target_activity=target_activity,
        previous_activity=previous_activity,
        now=now,
    )
    edge = launch_edge_projection(
        session,
        journey,
        origin=origin,
        target_activity=target_activity,
        now=now,
    )
    return {"origin": origin, "edge": edge}
