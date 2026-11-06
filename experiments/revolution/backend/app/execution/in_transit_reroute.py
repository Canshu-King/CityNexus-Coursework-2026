"""Read-only in-transit route deviation semantics.

The module deliberately separates advisory browser location from trusted
device/location evidence. It does not mutate execution state or plans.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import math
from typing import Any

from sqlalchemy import select

from app.execution.clock import execution_clock
from app.execution.location_scope import PRIVATE_BUDDY_ROUTE_LOCATION_SOURCE

from app.db.models import (
    Activity,
    Commitment,
    Journey,
    JourneyLeg,
    LocationObservation,
    MobilityFact,
    Place,
    RealityConflict,
    TransportReservation,
)


_TRUSTED_LOCATION_SOURCES = {"DEVICE_ATTESTED", "LOCATION_ATTESTED"}
_KNOWN_LOCATION_SOURCES = _TRUSTED_LOCATION_SOURCES | {"FOREGROUND_WEB"}
_WGS84_CRS = {"WGS84", "EPSG:4326", "OGC:CRS84", "CRS84", "4326"}
_GROUNDED_ROUTE_PROVENANCE = {
    "LIVE_PROVIDER_CAPTURE",
    "LIVE_CAPTURE",
    "CAPTURED_REPLAY",
    "SEALED_REAL_REPLAY",
}
LOCATION_FRESHNESS_SECONDS = 300


@dataclass(frozen=True)
class TransitDeviationDecision:
    state: str
    authoritative: bool
    material: bool
    reason: str
    deviation_threshold_m: float | None


def deviation_threshold_m(*, accuracy_m: float) -> float:
    return max(150.0, float(accuracy_m) * 3.0)


def classify_transit_deviation(
    *,
    activity_status: str,
    location_source: str | None,
    location_fresh: bool,
    accuracy_m: float | None,
    route_shape_available: bool,
    distance_to_route_m: float | None,
    consecutive_deviation_count: int = 1,
    near_target: bool = False,
) -> TransitDeviationDecision:
    """Classify one current-location observation against the active route."""

    if str(activity_status or "").upper() != "EN_ROUTE":
        return TransitDeviationDecision(
            "INACTIVE", False, False, "ACTIVITY_NOT_EN_ROUTE", None
        )

    if location_source is None or accuracy_m is None:
        return TransitDeviationDecision(
            "LOCATION_UNAVAILABLE", False, False, "NO_LOCATION_OBSERVATION", None
        )

    source = str(location_source).upper()
    trusted = source in _TRUSTED_LOCATION_SOURCES

    if not location_fresh:
        return TransitDeviationDecision(
            "LOCATION_STALE", trusted, False, "LOCATION_NOT_FRESH", None
        )

    if float(accuracy_m) > 100.0:
        return TransitDeviationDecision(
            "LOCATION_IMPRECISE", trusted, False, "LOCATION_ACCURACY_TOO_LOW", None
        )

    if near_target:
        return TransitDeviationDecision(
            "ARRIVAL_READY", trusted, False, "LOCATION_NEAR_TARGET", None
        )

    if not route_shape_available or distance_to_route_m is None:
        return TransitDeviationDecision(
            "ROUTE_SHAPE_UNAVAILABLE",
            trusted,
            False,
            "ACTIVE_ROUTE_GEOMETRY_UNAVAILABLE",
            None,
        )

    threshold = deviation_threshold_m(accuracy_m=float(accuracy_m))
    off_route = float(distance_to_route_m) > threshold

    if not off_route:
        return TransitDeviationDecision(
            "ON_ROUTE", trusted, False, "LOCATION_WITHIN_ROUTE_CORRIDOR", threshold
        )

    if not trusted:
        return TransitDeviationDecision(
            "ADVISORY_DEVIATION",
            False,
            False,
            "FOREGROUND_LOCATION_OUTSIDE_ROUTE_CORRIDOR",
            threshold,
        )

    if int(consecutive_deviation_count) < 2:
        return TransitDeviationDecision(
            "POSSIBLE_VERIFIED_DEVIATION",
            True,
            False,
            "TRUSTED_LOCATION_DEVIATION_NOT_YET_PERSISTENT",
            threshold,
        )

    return TransitDeviationDecision(
        "VERIFIED_DEVIATION",
        True,
        True,
        "TRUSTED_LOCATION_PERSISTENTLY_OUTSIDE_ROUTE_CORRIDOR",
        threshold,
    )


def reroute_origin_authority(location_source: str) -> str:
    source = str(location_source or "").upper()
    if source in _TRUSTED_LOCATION_SOURCES:
        return "TRUSTED_CURRENT_LOCATION_ROUTE"
    if source == "FOREGROUND_WEB":
        return "ADVISORY_ROUTE"
    return "UNKNOWN_LOCATION_ROUTE"


def reroute_semantic_key(
    *,
    journey_id: str,
    activity_id: str,
    location_observation_id: str,
    target_place_id: str,
    mode: str,
) -> str:
    """Stable reroute identity for the same current-location generation."""
    import hashlib
    import json

    payload: dict[str, Any] = {
        "journey_id": str(journey_id),
        "activity_id": str(activity_id),
        "location_observation_id": str(location_observation_id),
        "target_place_id": str(target_place_id),
        "mode": str(mode).upper(),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def route_coordinates(value: Any) -> tuple[tuple[float, float], ...]:
    """Read only actual WGS84 line coordinates; unsupported shapes stay absent."""

    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return ()
    if isinstance(value, dict):
        if value.get("type") == "Feature":
            return route_coordinates(value.get("geometry"))
        if value.get("type") == "LineString":
            return route_coordinates(value.get("coordinates"))
        if "coordinates" in value:
            return route_coordinates(value.get("coordinates"))
        if "geometry" in value:
            return route_coordinates(value.get("geometry"))
        return ()
    if not isinstance(value, (list, tuple)):
        return ()
    points: list[tuple[float, float]] = []
    for item in value:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        try:
            lon, lat = float(item[0]), float(item[1])
        except (TypeError, ValueError):
            continue
        if math.isfinite(lon) and math.isfinite(lat) and -180 <= lon <= 180 and -90 <= lat <= 90:
            points.append((lon, lat))
    return tuple(points) if len(points) >= 2 else ()


def distance_to_route_m(
    *, latitude: float, longitude: float, route: tuple[tuple[float, float], ...]
) -> float | None:
    """Approximate point-to-polyline distance with a local WGS84 tangent plane.

    For the short urban legs represented by JourneyLeg this avoids the false
    destination/chord/bounding-box shortcuts while remaining dependency-free.
    """

    if len(route) < 2:
        return None
    earth_radius_m = 6_371_008.8
    lat0 = math.radians(float(latitude))
    cos_lat = max(1e-8, abs(math.cos(lat0)))

    def xy(point: tuple[float, float]) -> tuple[float, float]:
        lon, lat = point
        x = math.radians(lon - longitude) * earth_radius_m * cos_lat
        y = math.radians(lat - latitude) * earth_radius_m
        return x, y

    projected = [xy(point) for point in route]
    best = math.inf
    for (x1, y1), (x2, y2) in zip(projected, projected[1:]):
        dx, dy = x2 - x1, y2 - y1
        length_squared = dx * dx + dy * dy
        if length_squared <= 1e-12:
            distance = math.hypot(x1, y1)
        else:
            fraction = max(0.0, min(1.0, -(x1 * dx + y1 * dy) / length_squared))
            distance = math.hypot(x1 + fraction * dx, y1 + fraction * dy)
        best = min(best, distance)
    return best if math.isfinite(best) else None


def _aware(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _active_leg_geometry(
    session: Any, journey: Journey, activity: Activity
) -> tuple[tuple[tuple[float, float], ...], dict[str, Any]]:
    """Return only exact active-edge geometry with explicit provider/CRS provenance."""
    leg = active_transit_leg(session, journey, activity)
    if leg is None:
        return (), {}
    leg_crs = str(leg.crs or "").strip().upper()
    leg_provenance = str(leg.provenance or "").strip().upper()
    leg_provider = str(leg.provider_key or "").strip()
    coordinates = route_coordinates(leg.geometry_json)
    if (
        coordinates
        and leg_crs in _WGS84_CRS
        and leg_provenance in _GROUNDED_ROUTE_PROVENANCE
        and leg_provider
        and not leg_provider.casefold().startswith(("mock", "fixture"))
    ):
        return coordinates, {
            "basis": "ACTIVE_JOURNEY_LEG",
            "crs": leg_crs,
            "provider": leg_provider,
            "provenance": leg_provenance,
        }

    fact = session.get(MobilityFact, leg.provider_snapshot_id) if leg.provider_snapshot_id else None
    if fact is None:
        return (), {}
    fact_crs = str(fact.crs or "").strip().upper()
    fact_provenance = str(fact.provenance or "").strip().upper()
    fact_provider = str(fact.provider_key or "").strip()
    coordinates = route_coordinates(fact.geometry_json)
    matches_leg = (
        str(fact.journey_id) == str(journey.id)
        and str(fact.from_key) == str(leg.from_key)
        and str(fact.to_key) == str(leg.to_key)
        and str(fact.mode or "").upper() == str(leg.mode or "").upper()
    )
    if (
        matches_leg
        and coordinates
        and fact_crs in _WGS84_CRS
        and fact_provenance in _GROUNDED_ROUTE_PROVENANCE
        and fact_provider
        and not fact_provider.casefold().startswith(("mock", "fixture"))
    ):
        return coordinates, {
            "basis": "LINKED_MOBILITY_FACT",
            "crs": fact_crs,
            "provider": fact_provider,
            "provenance": fact_provenance,
        }
    return (), {}


def _trusted_persistent_deviation_count(
    observations: list[LocationObservation],
    *,
    departed_at: datetime | None,
    now: datetime,
    route: tuple[tuple[float, float], ...],
) -> int:
    """Count consecutive fresh, precise trusted samples outside the exact route."""
    if not route:
        return 0
    count = 0
    ordered = sorted(
        observations,
        key=lambda row: (_aware(row.captured_at) or datetime.min.replace(tzinfo=timezone.utc), str(row.id)),
        reverse=True,
    )
    for row in ordered:
        captured = _aware(row.captured_at)
        if (
            departed_at is None
            or captured is None
            or captured < departed_at
            or (now - captured).total_seconds() < -30
            or (now - captured).total_seconds() > LOCATION_FRESHNESS_SECONDS
            or str(row.source or "").upper() not in _TRUSTED_LOCATION_SOURCES
            or float(row.accuracy_m) > 100.0
        ):
            break
        distance = distance_to_route_m(
            latitude=float(row.latitude), longitude=float(row.longitude), route=route
        )
        if distance is None or distance <= deviation_threshold_m(accuracy_m=float(row.accuracy_m)):
            break
        count += 1
    return count


def reroute_commitment_risk(
    session: Any,
    journey: Journey,
    current_activity: Activity,
    *,
    current_leg_duration_min: int,
    now: datetime | None = None,
    authority: str,
) -> dict[str, Any]:
    """Estimate arrival slack to the next protected stop using the new current edge.

    Later edges remain explicitly frozen-plan evidence; they are not represented
    as future live traffic. Missing or mismatched edges produce UNKNOWN instead
    of a guessed schedule.
    """
    unknown = {
        "state": "UNKNOWN",
        "authority": authority,
        "next_commitment": None,
        "predicted_arrival_at": None,
        "slack_after_buffer_min": None,
        "future_mobility_basis": "FROZEN_PLAN_EVIDENCE_NOT_FUTURE_LIVE_TRAFFIC",
    }
    if int(current_leg_duration_min) < 0 or not journey.active_plan_version_id:
        return unknown
    activities = list(
        session.scalars(
            select(Activity)
            .where(Activity.plan_version_id == journey.active_plan_version_id)
            .order_by(Activity.sequence_no)
        ).all()
    )
    current_index = next(
        (index for index, row in enumerate(activities) if str(row.id) == str(current_activity.id)),
        None,
    )
    if current_index is None:
        return unknown
    commitments = session.scalars(
        select(Commitment).where(
            Commitment.journey_id == journey.id,
            Commitment.status == "ACTIVE",
            Commitment.level.in_(("C3_Guaranteed", "C4_Anchored")),
        )
    ).all()
    index_by_activity = {str(row.id): index for index, row in enumerate(activities)}
    protected = sorted(
        (
            (index_by_activity[str(item.activity_id)], item)
            for item in commitments
            if item.activity_id
            and str(item.activity_id) in index_by_activity
            and index_by_activity[str(item.activity_id)] > current_index
        ),
        key=lambda pair: (pair[0], str(pair[1].id)),
    )
    if not protected:
        return {**unknown, "state": "NO_UPCOMING_PROTECTED_COMMITMENT"}
    target_index, commitment = protected[0]
    reservations = session.scalars(
        select(TransportReservation).where(
            TransportReservation.journey_id == journey.id,
            TransportReservation.commitment_id == commitment.id,
        )
    ).all()
    if reservations:
        reservation_fact_keys = [
            f"reservation:{row.id}:scheduled_departure_at" for row in reservations
        ]
        contested = session.scalar(
            select(RealityConflict.id).where(
                RealityConflict.journey_id == journey.id,
                RealityConflict.status == "UNRESOLVED_MATERIAL_CONFLICT",
                RealityConflict.fact_domain == "RESERVATION_TIME",
                RealityConflict.fact_key.in_(reservation_fact_keys),
            ).limit(1)
        )
        if contested:
            return {**unknown, "state": "COMMITMENT_TIME_CONTESTED"}
    expected_legs = []
    for index in range(current_index, target_index):
        origin, destination = activities[index], activities[index + 1]
        leg = session.scalar(
            select(JourneyLeg).where(
                JourneyLeg.journey_id == journey.id,
                JourneyLeg.plan_version_id == journey.active_plan_version_id,
                JourneyLeg.from_activity_id == origin.id,
                JourneyLeg.to_activity_id == destination.id,
            )
        )
        if (
            leg is None
            or not leg.provider_snapshot_id
            or not str(leg.provider_key or "").strip()
            or str(leg.provenance or "").upper() not in _GROUNDED_ROUTE_PROVENANCE
            or str(leg.provider_key or "").casefold().startswith(("mock", "fixture"))
        ):
            return {**unknown, "state": "FROZEN_DOWNSTREAM_ROUTE_UNAVAILABLE"}
        fact = session.get(MobilityFact, leg.provider_snapshot_id)
        if (
            fact is None
            or str(fact.journey_id) != str(journey.id)
            or str(fact.from_key) != str(leg.from_key)
            or str(fact.to_key) != str(leg.to_key)
            or str(fact.mode or "").upper() != str(leg.mode or "").upper()
        ):
            return {**unknown, "state": "FROZEN_DOWNSTREAM_ROUTE_UNAVAILABLE"}
        try:
            expected_legs.append(max(0, int(leg.duration_min)))
        except (TypeError, ValueError):
            return {**unknown, "state": "FROZEN_DOWNSTREAM_ROUTE_UNAVAILABLE"}
    try:
        stamp = now or datetime.now(timezone.utc)
        if stamp.tzinfo is None:
            return {**unknown, "state": "COMMITMENT_TIME_UNKNOWN"}
        commitment_at = _aware(activities[target_index].start_at)
        if commitment_at is None:
            return {**unknown, "state": "COMMITMENT_TIME_UNKNOWN"}
        visit_and_transfer_min = sum(expected_legs)
        for row in activities[current_index:target_index]:
            start, end = _aware(row.start_at), _aware(row.end_at)
            if start is None or end is None:
                return {**unknown, "state": "SCHEDULE_DURATION_UNKNOWN"}
            visit_and_transfer_min += max(0, int((end - start).total_seconds() // 60))
        predicted = stamp.astimezone(timezone.utc) + timedelta(
            minutes=int(current_leg_duration_min) + visit_and_transfer_min
        )
        try:
            policy = json.loads(commitment.policy_json or "{}")
        except (TypeError, json.JSONDecodeError):
            policy = {}
        buffer_min = 5
        for key in ("safety_buffer_min", "required_buffer_min", "resilience_buffer_min"):
            if policy.get(key) is not None:
                try:
                    buffer_min = max(0, int(policy[key]))
                    break
                except (ValueError, TypeError):
                    continue
        slack = int((commitment_at - predicted).total_seconds() // 60) - buffer_min
    except (TypeError, ValueError):
        return {**unknown, "state": "COMMITMENT_TIME_UNKNOWN"}
    state = "COMMITMENT_AT_RISK" if slack < 0 else "LOW_BUFFER" if slack < max(10, buffer_min * 2) else "WITHIN_BUFFER"
    return {
        "state": state,
        "authority": authority,
        "next_commitment": {
            "commitment_id": str(commitment.id),
            "activity_id": str(activities[target_index].id),
            "title": activities[target_index].title,
            "scheduled_at": activities[target_index].start_at,
            "safety_buffer_min": buffer_min,
        },
        "predicted_arrival_at": predicted.isoformat(),
        "slack_after_buffer_min": slack,
        "current_leg_duration_min": int(current_leg_duration_min),
        "downstream_leg_count": len(expected_legs),
        "future_mobility_basis": "FROZEN_PLAN_EVIDENCE_NOT_FUTURE_LIVE_TRAFFIC",
    }


def active_transit_leg(session: Any, journey: Journey, activity: Activity) -> JourneyLeg | None:
    if not journey.active_plan_version_id:
        return None
    return session.scalar(
        select(JourneyLeg).where(
            JourneyLeg.journey_id == journey.id,
            JourneyLeg.plan_version_id == journey.active_plan_version_id,
            JourneyLeg.to_activity_id == activity.id,
        ).order_by(JourneyLeg.sequence_no.desc())
    )


def in_transit_projection(
    session: Any,
    journey: Journey,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Read-only current-leg location/reroute card; never emits events or calls providers."""

    inactive = {
        "state": "INACTIVE",
        "activity_id": None,
        "activity_title": None,
        "activity_status": None,
        "location_observation_id": None,
        "location_source": None,
        "location_fresh": False,
        "route_shape_available": False,
        "distance_to_route_m": None,
        "deviation_threshold_m": None,
        "authoritative": False,
        "material": False,
        "can_reroute": False,
        "reason": "ACTIVITY_NOT_EN_ROUTE",
        "read_only": True,
    }
    if str(journey.status).upper() != "ACTIVE" or not journey.current_activity_id:
        return inactive
    activity = session.get(Activity, str(journey.current_activity_id))
    if (
        activity is None
        or str(activity.plan_version_id) != str(journey.active_plan_version_id or "")
        or str(activity.status).upper() != "EN_ROUTE"
    ):
        return inactive

    metadata: dict[str, Any] = {}
    try:
        parsed = json.loads(activity.metadata_json or "{}")
        if isinstance(parsed, dict):
            metadata = parsed
    except (TypeError, json.JSONDecodeError):
        pass
    departed_at = _aware(metadata.get("en_route_at") or metadata.get("actual_departed_at"))
    reference = (now or execution_clock.now()).astimezone(timezone.utc)
    observations = session.scalars(
        select(LocationObservation)
        .where(
            LocationObservation.journey_id == journey.id,
            LocationObservation.source != PRIVATE_BUDDY_ROUTE_LOCATION_SOURCE,
        )
    ).all()
    eligible_observations = [
        row
        for row in observations
        if departed_at is not None
        and (_aware(row.captured_at) or datetime.min.replace(tzinfo=timezone.utc)) >= departed_at
    ]
    observation = max(
        eligible_observations,
        key=lambda row: (_aware(row.captured_at) or datetime.min.replace(tzinfo=timezone.utc), str(row.id)),
        default=None,
    )
    if observation is None:
        return {
            **inactive,
            "state": "LOCATION_UNAVAILABLE",
            "activity_id": str(activity.id),
            "activity_title": activity.title,
            "activity_status": str(activity.status).upper(),
            "reason": "NO_LOCATION_CAPTURED_DURING_CURRENT_LEG",
            "route_shape_available": bool(_active_leg_geometry(session, journey, activity)[0]),
        }

    captured = _aware(observation.captured_at)
    age_seconds = (reference - captured).total_seconds() if captured is not None else math.inf
    fresh = -30.0 <= age_seconds <= LOCATION_FRESHNESS_SECONDS
    source = str(observation.source or "").upper()
    route, route_evidence = _active_leg_geometry(session, journey, activity)
    distance = (
        distance_to_route_m(
            latitude=float(observation.latitude),
            longitude=float(observation.longitude),
            route=route,
        )
        if route
        else None
    )
    near_target = False
    if activity.place_id:
        target = session.get(Place, str(activity.place_id))
        if target is not None:
            target_distance = distance_to_route_m(
                latitude=float(observation.latitude),
                longitude=float(observation.longitude),
                route=((float(target.longitude), float(target.latitude)), (float(target.longitude), float(target.latitude))),
            )
            near_target = target_distance is not None and target_distance <= max(150.0, float(observation.accuracy_m) * 2.0)
    decision = classify_transit_deviation(
        activity_status=activity.status,
        location_source=source if source in _KNOWN_LOCATION_SOURCES else None,
        location_fresh=fresh,
        accuracy_m=float(observation.accuracy_m),
        route_shape_available=bool(route),
        distance_to_route_m=distance,
        consecutive_deviation_count=_trusted_persistent_deviation_count(
            observations,
            departed_at=departed_at,
            now=reference,
            route=route,
        ),
        near_target=near_target,
    )
    consecutive_deviation_count = _trusted_persistent_deviation_count(
        observations,
        departed_at=departed_at,
        now=reference,
        route=route,
    )
    target_available = bool(activity.place_id and session.get(Place, str(activity.place_id)))
    location_usable = (
        source in _KNOWN_LOCATION_SOURCES
        and fresh
        and float(observation.accuracy_m) <= 100.0
        and departed_at is not None
    )
    return {
        "state": decision.state,
        "activity_id": str(activity.id),
        "activity_title": activity.title,
        "activity_status": str(activity.status).upper(),
        "location_observation_id": str(observation.id),
        "location_source": source,
        "location_captured_at": observation.captured_at,
        "location_age_seconds": max(0, int(age_seconds)) if math.isfinite(age_seconds) else None,
        "location_accuracy_m": round(float(observation.accuracy_m), 1),
        "location_fresh": fresh,
        "route_shape_available": bool(route),
        "route_geometry": route_evidence or None,
        "consecutive_deviation_count": consecutive_deviation_count,
        "distance_to_route_m": round(distance, 1) if distance is not None else None,
        "deviation_threshold_m": decision.deviation_threshold_m,
        "authoritative": decision.authoritative,
        "material": decision.material,
        "can_reroute": bool(location_usable and target_available),
        "route_authority": reroute_origin_authority(source),
        "reason": decision.reason,
        "active_plan_version_id": journey.active_plan_version_id,
        "read_only": True,
    }
