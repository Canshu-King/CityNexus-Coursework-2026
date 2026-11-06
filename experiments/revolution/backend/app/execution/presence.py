"""Grounded, authority-aware Journey presence projection."""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Activity, Journey, LocationObservation, Place
from app.execution.clock import execution_clock
from app.execution.location_scope import PRIVATE_BUDDY_ROUTE_LOCATION_SOURCE

_TRUSTED_LOCATION_SOURCES = {"DEVICE_ATTESTED", "LOCATION_ATTESTED"}
_MAX_LOCATION_AGE_SECONDS = 300.0
_MAX_FOREGROUND_ACCURACY_M = 100.0


def _distance_m(
    latitude_a: float,
    longitude_a: float,
    latitude_b: float,
    longitude_b: float,
) -> float:
    radius_m = 6_371_000.0
    phi_a = math.radians(latitude_a)
    phi_b = math.radians(latitude_b)
    delta_phi = math.radians(latitude_b - latitude_a)
    delta_lambda = math.radians(longitude_b - longitude_a)
    hav = (
        math.sin(delta_phi / 2.0) ** 2
        + math.cos(phi_a) * math.cos(phi_b) * math.sin(delta_lambda / 2.0) ** 2
    )
    return radius_m * 2.0 * math.atan2(
        math.sqrt(hav), math.sqrt(max(0.0, 1.0 - hav))
    )


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


def _target_activity(
    session: Session,
    journey: Journey,
    target_activity_id: str | None,
) -> Activity | None:
    if target_activity_id:
        row = session.get(Activity, str(target_activity_id))
        if row is not None and row.plan_version_id == journey.active_plan_version_id:
            return row
        return None
    if journey.current_activity_id:
        row = session.get(Activity, str(journey.current_activity_id))
        if row is not None and row.plan_version_id == journey.active_plan_version_id:
            return row
    if not journey.active_plan_version_id:
        return None
    return session.scalar(
        select(Activity)
        .where(
            Activity.plan_version_id == journey.active_plan_version_id,
            Activity.status.not_in(("COMPLETED", "SKIPPED", "CANCELLED")),
        )
        .order_by(Activity.sequence_no)
    )


def presence_projection(
    session: Session,
    journey: Journey,
    *,
    viewer_member_id: str | None = None,
    target_activity_id: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Project only the signed viewer's location relative to an exact planned Place."""

    target = _target_activity(session, journey, target_activity_id)
    base: dict[str, Any] = {
        "state": "LOCATION_UNAVAILABLE",
        "authority": "NONE",
        "location_observation_id": None,
        "source": None,
        "captured_at": None,
        "age_seconds": None,
        "accuracy_m": None,
        "target_activity_id": str(target.id) if target is not None else target_activity_id,
        "target_place_id": str(target.place_id) if target is not None and target.place_id else None,
        "distance_to_target_m": None,
        "maximum_near_distance_m": None,
        "arrival_transition_allowed": False,
        "reason_code": "SIGNED_MEMBER_IDENTITY_REQUIRED",
    }
    # Location is member-private. System projections, legacy rows without a
    # bound member, and callers without a signed viewer must never fall back to
    # the latest Journey-wide observation.
    if not viewer_member_id:
        return base
    latest = session.scalar(
        select(LocationObservation)
        .where(
            LocationObservation.journey_id == journey.id,
            LocationObservation.journey_member_id == str(viewer_member_id),
            LocationObservation.source != PRIVATE_BUDDY_ROUTE_LOCATION_SOURCE,
        )
        .order_by(LocationObservation.captured_at.desc(), LocationObservation.id.desc())
    )
    if latest is None:
        return base

    captured = _aware(latest.captured_at)
    stamp = (now or execution_clock.now()).astimezone(timezone.utc)
    age_seconds = (stamp - captured).total_seconds() if captured is not None else None
    source = str(latest.source or "")
    trusted = source in _TRUSTED_LOCATION_SOURCES
    base.update(
        {
            "location_observation_id": latest.id,
            "source": source,
            "captured_at": latest.captured_at,
            "age_seconds": round(age_seconds, 1) if age_seconds is not None else None,
            "accuracy_m": float(latest.accuracy_m),
            "authority": "TRUSTED_LOCATION" if trusted else "ADVISORY_FOREGROUND",
        }
    )
    if captured is None or age_seconds is None or age_seconds < -60 or age_seconds > _MAX_LOCATION_AGE_SECONDS:
        return {
            **base,
            "state": "LOCATION_STALE",
            "reason_code": "LOCATION_NOT_FRESH",
        }
    if float(latest.accuracy_m) > _MAX_FOREGROUND_ACCURACY_M:
        return {
            **base,
            "state": "LOCATION_IMPRECISE",
            "reason_code": "LOCATION_ACCURACY_TOO_LOW",
        }
    if target is None or not target.place_id:
        return {
            **base,
            "state": "TARGET_UNBOUND",
            "reason_code": "TARGET_ACTIVITY_HAS_NO_EXACT_PLACE",
        }
    place = session.get(Place, str(target.place_id))
    if place is None:
        return {
            **base,
            "state": "TARGET_UNBOUND",
            "reason_code": "TARGET_PLACE_NOT_FOUND",
        }

    distance = _distance_m(
        float(latest.latitude),
        float(latest.longitude),
        float(place.latitude),
        float(place.longitude),
    )
    maximum = max(150.0, float(latest.accuracy_m) * 2.0)
    near = distance <= maximum
    return {
        **base,
        "state": "VERIFIED_AT_TARGET" if near and trusted else "NEAR_TARGET" if near else "AWAY_FROM_TARGET",
        "target_activity_id": str(target.id),
        "target_place_id": str(place.id),
        "distance_to_target_m": round(distance, 1),
        "maximum_near_distance_m": round(maximum, 1),
        # Eligibility is not execution. The existing transition endpoint still
        # performs principal/audit/freshness/distance checks before writing truth.
        "arrival_transition_allowed": bool(near and trusted),
        "reason_code": (
            "TRUSTED_LOCATION_NEAR_TARGET"
            if near and trusted
            else "FOREGROUND_LOCATION_NEAR_TARGET_NOT_EXECUTION_TRUTH"
            if near
            else "LOCATION_AWAY_FROM_TARGET"
        ),
    }
