"""Read-only launch setup projection for resolving an unknown first-trip origin."""
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
    LocationObservation,
    StayJourneyOption,
)
from app.execution.clock import execution_clock
from app.execution.launch_origin import grounded_launch_origin
from app.execution.location_scope import PRIVATE_BUDDY_ROUTE_LOCATION_SOURCE


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


def launch_setup_projection(
    session: Session,
    journey: Journey,
    *,
    target_activity: Activity | None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Describe only launch-origin choices that actually exist for this Journey."""

    stamp = (now or execution_clock.now()).astimezone(timezone.utc)
    current = grounded_launch_origin(
        session,
        journey,
        target_activity=target_activity,
        previous_activity=None,
        now=stamp,
    )
    if current.get("state") == "READY" and current.get("authority") == "USER_CONFIRMED_PLANNED_ORIGIN":
        return {
            "status": "ORIGIN_READY",
            "current_origin": current,
            "candidates": [],
            "actions": [],
            "target_activity_id": str(target_activity.id) if target_activity is not None else None,
        }

    candidates: list[dict[str, Any]] = []
    actions: list[str] = []
    target_start = _aware(target_activity.start_at if target_activity is not None else None)

    location = session.scalar(
        select(LocationObservation)
        .where(
            LocationObservation.journey_id == journey.id,
            LocationObservation.source != PRIVATE_BUDDY_ROUTE_LOCATION_SOURCE,
        )
        .order_by(LocationObservation.captured_at.desc(), LocationObservation.id.desc())
    )
    if location is not None:
        captured = _aware(location.captured_at)
        age = (stamp - captured).total_seconds() if captured is not None else None
        eligible = bool(
            captured is not None
            and age is not None
            and -60 <= age <= 300
            and float(location.accuracy_m) <= 100.0
        )
        candidates.append(
            {
                "kind": "FOREGROUND_LOCATION",
                "display_name": "Current shared location",
                "eligible": eligible,
                "reason": "FRESH_PRECISE_LOCATION" if eligible else "LOCATION_NOT_USABLE",
                "source_ref_id": str(location.id),
                "authority": "ADVISORY_FOREGROUND",
            }
        )
        if not eligible:
            actions.append("SHARE_LOCATION")
    else:
        actions.append("SHARE_LOCATION")

    option = session.scalar(
        select(StayJourneyOption)
        .where(
            StayJourneyOption.journey_id == journey.id,
            StayJourneyOption.status == "SELECTED",
        )
        .order_by(StayJourneyOption.rank, StayJourneyOption.id)
    )
    if option is not None:
        offer = session.get(AccommodationOfferSnapshot, option.accommodation_offer_id)
        if offer is not None:
            check_in = _aware(offer.check_in_at)
            check_out = _aware(offer.check_out_at)
            eligible = bool(
                target_start is not None
                and check_in is not None
                and check_out is not None
                and check_in <= target_start < check_out
            )
            candidates.append(
                {
                    "kind": "SELECTED_STAY",
                    "display_name": offer.property_name,
                    "eligible": eligible,
                    "reason": (
                        "STAY_COVERS_TARGET_START"
                        if eligible
                        else "STAY_DOES_NOT_COVER_TARGET_START"
                    ),
                    "source_ref_id": str(offer.id),
                    "place_id": None,
                    "provider_key": offer.provider_key,
                    "authority": "PLANNED_CONTEXT",
                }
            )
            if eligible:
                actions.append("USE_SELECTED_STAY")

    context = session.get(JourneyTripContext, journey.id)
    if context is not None:
        anchor = _load(context.arrival_anchor_json)
        anchor_at = _aware(str(anchor.get("at") or ""))
        eligible = bool(
            anchor
            and anchor.get("latitude") is not None
            and anchor.get("longitude") is not None
            and target_start is not None
            and anchor_at is not None
            and anchor_at <= target_start
        )
        if anchor:
            candidates.append(
                {
                    "kind": "ARRIVAL_ANCHOR",
                    "display_name": anchor.get("location") or anchor.get("name") or "Arrival point",
                    "eligible": eligible,
                    "reason": (
                        "ARRIVAL_ANCHOR_PRECEDES_TARGET"
                        if eligible
                        else "ARRIVAL_ANCHOR_NOT_ROUTABLE_FOR_TARGET"
                    ),
                    "source_ref_id": anchor.get("reservation_id"),
                    "authority": "PLANNED_CONTEXT",
                }
            )
        if eligible:
            actions.append("USE_ARRIVAL_POINT")

    # Choosing a real Place is always a possible product action even when no
    # pre-existing context candidate exists. The write path must still require
    # an exact persisted Place; this projection never accepts free-text coords.
    actions.append("CHOOSE_PLACE")

    return {
        "status": "NEEDS_ORIGIN",
        "current_origin": None,
        "candidates": candidates,
        "actions": list(dict.fromkeys(actions)),
        "target_activity_id": str(target_activity.id) if target_activity is not None else None,
        "authority": "READ_ONLY_PROJECTION",
    }
