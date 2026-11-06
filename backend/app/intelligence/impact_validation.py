from __future__ import annotations

import math
import re
from datetime import datetime
from difflib import SequenceMatcher
from typing import Any, Mapping, Sequence

from app.intelligence.impact_contract import (
    ImpactActivityContext,
    ImpactCandidate,
    ImpactMaturity,
    ImpactValidationResult,
    ValidatedImpactCandidate,
)

try:
    from rapidfuzz.fuzz import ratio as _rapidfuzz_ratio
except ImportError:  # pragma: no cover - compatibility fallback
    _rapidfuzz_ratio = None

try:
    from shapely.geometry import LineString, Point, Polygon
    from shapely.ops import unary_union
except ImportError:  # pragma: no cover - compatibility fallback
    LineString = Point = Polygon = unary_union = None

MIN_ACTIONABLE_CONFIDENCE = 0.55
TERMINAL_ACTIVITY_STATUSES = frozenset({"COMPLETED", "SKIPPED", "CANCELLED"})
_STRUCTURED_TIME_KEYS = (
    "valid_from",
    "valid_until",
    "starts_at",
    "ends_at",
    "event_start_at",
    "event_end_at",
)
_STRUCTURED_TEXT_KEYS = ("text", "description", "reason", "message", "scope_text")
_INJECTION_MARKERS = (
    "ignore previous",
    "ignore prior",
    "ignore all policy",
    "call payment",
    "mutate c4",
    "grant tool access",
)


def _parse_time(value: object) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _haversine_meters(
    latitude_a: float, longitude_a: float, latitude_b: float, longitude_b: float
) -> float:
    earth_radius_m = 6_371_000.0
    lat_a = math.radians(latitude_a)
    lat_b = math.radians(latitude_b)
    delta_lat = math.radians(latitude_b - latitude_a)
    delta_lon = math.radians(longitude_b - longitude_a)
    haversine = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(lat_a) * math.cos(lat_b) * math.sin(delta_lon / 2) ** 2
    )
    return 2 * earth_radius_m * math.asin(math.sqrt(haversine))


def _overlaps_event_window(activity: ImpactActivityContext, payload: Mapping[str, Any]) -> bool:
    valid_from = _parse_time(payload.get("valid_from") or payload.get("starts_at"))
    valid_until = _parse_time(payload.get("valid_until") or payload.get("ends_at"))
    if valid_from is None:
        return True
    if valid_until is None:
        return activity.end_at > valid_from
    return activity.start_at < valid_until and activity.end_at > valid_from


def _within_scope(activity: ImpactActivityContext, payload: Mapping[str, Any]) -> bool:
    try:
        latitude = float(payload["latitude"])
        longitude = float(payload["longitude"])
    except (KeyError, TypeError, ValueError):
        return True
    if activity.latitude is None or activity.longitude is None:
        return False
    try:
        radius_m = float(payload.get("radius_m", payload.get("radius_meters", 2_000)))
    except (TypeError, ValueError):
        return False
    return _haversine_meters(latitude, longitude, activity.latitude, activity.longitude) <= max(
        0.0, radius_m
    )


def _polygon_rings(payload: Mapping[str, Any]) -> tuple[tuple[tuple[float, float], ...], ...]:
    raw: Any = payload.get("disruption_polygon") or payload.get("polygon")
    if isinstance(raw, Mapping):
        if raw.get("type") == "Feature":
            raw = raw.get("geometry")
        if isinstance(raw, Mapping):
            geometry_type = raw.get("type")
            coordinates = raw.get("coordinates")
            if geometry_type == "Polygon":
                raw = coordinates
            elif geometry_type == "MultiPolygon":
                raw = [ring for polygon in coordinates or [] for ring in polygon]
            else:
                raw = None
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return ()
    if (
        raw
        and isinstance(raw[0], Sequence)
        and len(raw[0]) >= 2
        and isinstance(raw[0][0], (int, float))
    ):
        raw = [raw]
    rings: list[tuple[tuple[float, float], ...]] = []
    for ring in raw:
        if not isinstance(ring, Sequence) or isinstance(ring, (str, bytes)):
            continue
        points: list[tuple[float, float]] = []
        for point in ring:
            if not isinstance(point, Sequence) or len(point) < 2:
                continue
            try:
                points.append((float(point[0]), float(point[1])))
            except (TypeError, ValueError):
                continue
        if len(points) >= 3:
            rings.append(tuple(points))
    return tuple(rings)


def _point_in_ring(point: tuple[float, float], ring: Sequence[tuple[float, float]]) -> bool:
    x, y = point
    inside = False
    for index, (x_a, y_a) in enumerate(ring):
        x_b, y_b = ring[(index + 1) % len(ring)]
        if (y_a > y) != (y_b > y):
            crossing = (x_b - x_a) * (y - y_a) / ((y_b - y_a) or 1e-12) + x_a
            if x <= crossing:
                inside = not inside
    return inside


def _orientation(a: tuple[float, float], b: tuple[float, float], c: tuple[float, float]) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _segments_intersect(
    first_a: tuple[float, float],
    first_b: tuple[float, float],
    second_a: tuple[float, float],
    second_b: tuple[float, float],
) -> bool:
    first = _orientation(first_a, first_b, second_a)
    second = _orientation(first_a, first_b, second_b)
    third = _orientation(second_a, second_b, first_a)
    fourth = _orientation(second_a, second_b, first_b)
    return (first == 0 or second == 0 or (first > 0) != (second > 0)) and (
        third == 0 or fourth == 0 or (third > 0) != (fourth > 0)
    )


def _route_geometry(payload: Mapping[str, Any]) -> tuple[tuple[float, float], ...]:
    raw: Any = payload.get("route_geometry") or payload.get("route_linestring")
    if isinstance(raw, Mapping):
        if raw.get("type") == "Feature":
            raw = raw.get("geometry")
        if isinstance(raw, Mapping) and raw.get("type") == "LineString":
            raw = raw.get("coordinates")
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return ()
    points: list[tuple[float, float]] = []
    for point in raw:
        if not isinstance(point, Sequence) or len(point) < 2:
            continue
        try:
            points.append((float(point[0]), float(point[1])))
        except (TypeError, ValueError):
            continue
    return tuple(points)


def _geometry_scope_ref(activity: ImpactActivityContext, payload: Mapping[str, Any]) -> str | None:
    rings = _polygon_rings(payload)
    if not rings:
        return None
    if Polygon is not None and Point is not None and unary_union is not None:
        polygons = [Polygon(ring) for ring in rings]
        scope = unary_union([polygon for polygon in polygons if polygon.is_valid])
        if not scope.is_empty:
            if (
                activity.latitude is not None
                and activity.longitude is not None
                and scope.covers(Point(activity.longitude, activity.latitude))
            ):
                return "event.payload.disruption_polygon"
            route = _route_geometry(payload)
            if len(route) >= 2 and LineString is not None and scope.intersects(LineString(route)):
                return "event.payload.route_polygon_intersection"
            return None
    if activity.latitude is not None and activity.longitude is not None:
        point = (activity.longitude, activity.latitude)
        if any(_point_in_ring(point, ring) for ring in rings):
            return "event.payload.disruption_polygon"
    route = _route_geometry(payload)
    if len(route) >= 2:
        for start, end in zip(route, route[1:]):
            for ring in rings:
                if any(
                    _segments_intersect(start, end, edge, ring[(index + 1) % len(ring)])
                    for index, edge in enumerate(ring)
                ):
                    return "event.payload.route_polygon_intersection"
    return None


def _normalized_text(value: object) -> str:
    return " ".join(re.findall(r"[\w]+", str(value or "").casefold()))


def _fuzzy_contains(needle: str, haystack: str) -> bool:
    compact_needle = re.sub(r"\s+", "", needle.casefold())
    compact_haystack = re.sub(r"\s+", "", haystack.casefold())
    if not compact_needle or compact_needle in compact_haystack:
        return bool(compact_needle)
    if len(compact_needle) < 4:
        return False
    for width in range(max(2, len(compact_needle) - 1), len(compact_needle) + 2):
        for start in range(max(0, len(compact_haystack) - width + 1)):
            candidate = compact_haystack[start : start + width]
            similarity = (
                _rapidfuzz_ratio(compact_needle, candidate) / 100.0
                if _rapidfuzz_ratio is not None
                else SequenceMatcher(None, compact_needle, candidate).ratio()
            )
            if similarity >= 0.82:
                return True
    return False


def _text_mentions_activity(activity: ImpactActivityContext, payload: Mapping[str, Any]) -> bool:
    text = _normalized_text(" ".join(str(payload.get(key) or "") for key in _STRUCTURED_TEXT_KEYS))
    if not text:
        return False
    names = (activity.title, activity.place_name, *activity.place_aliases)
    for name in names:
        tokens = [token for token in _normalized_text(name).split() if len(token) > 2]
        if tokens and all(token in text.split() for token in tokens):
            return True
        if _fuzzy_contains(str(name), text):
            return True
    return False


def _text_contains_injection(payload: Mapping[str, Any]) -> bool:
    text = _normalized_text(" ".join(str(payload.get(key) or "") for key in _STRUCTURED_TEXT_KEYS))
    return any(marker in text for marker in _INJECTION_MARKERS)


def _text_mentions_other_activity(
    activity: ImpactActivityContext,
    activities: Sequence[ImpactActivityContext],
    payload: Mapping[str, Any],
) -> bool:
    """Detect a narrow contradiction against another server-known Activity."""

    if _text_mentions_activity(activity, payload):
        return False
    return any(
        other.activity_id != activity.activity_id and _text_mentions_activity(other, payload)
        for other in activities
    )


def _values(payload: Mapping[str, Any], keys: tuple[str, ...]) -> tuple[str, ...]:
    values: list[str] = []
    for key in keys:
        raw = payload.get(key)
        if raw in (None, ""):
            continue
        if isinstance(raw, (list, tuple, set)):
            values.extend(str(item) for item in raw if item not in (None, ""))
        else:
            values.append(str(raw))
    return tuple(dict.fromkeys(values))


def _corroboration(
    activity: ImpactActivityContext, payload: Mapping[str, Any]
) -> tuple[tuple[str, ...], tuple[str, ...], bool]:
    """Return independent evidence refs, reasons, and whether a ref is strong.

    Candidate-provided ``evidence_refs`` are intentionally not read here. Only
    server-owned event scope, time, geometry, route, service, and name facts
    can corroborate an AI hypothesis.
    """

    refs: list[str] = []
    reasons: list[str] = []
    strong_reference = False

    explicit_activity = payload.get("_event_affected_activity_id")
    explicit_activity = explicit_activity or payload.get("affected_activity_id")
    explicit_activity = explicit_activity or payload.get("activity_id")
    if explicit_activity not in (None, ""):
        if str(explicit_activity) != activity.activity_id:
            return (), ("STRUCTURED_ACTIVITY_MISMATCH",), True
        refs.append("event.affected_activity_id")
        strong_reference = True

    place_values = _values(
        payload,
        (
            "affected_place_id",
            "place_id",
            "scope_place_id",
        ),
    )
    if place_values:
        activity_places = {
            value for value in (activity.place_id, activity.city_service_id) if value
        }
        if not any(value in activity_places for value in place_values):
            return (), ("STRUCTURED_PLACE_MISMATCH",), True
        refs.append("event.payload.place_id")
        strong_reference = True

    city_service_values = _values(payload, ("city_service_id", "affected_city_service_id"))
    if city_service_values:
        if activity.city_service_id is None or activity.city_service_id not in city_service_values:
            return (), ("STRUCTURED_CITY_SERVICE_MISMATCH",), True
        refs.append("event.payload.city_service_id")
        strong_reference = True

    scoped_values = _values(
        payload,
        (
            "scope_place_ids",
            "affected_place_ids",
            "route_intersection_place_ids",
            "provider_scope_place_ids",
        ),
    )
    if scoped_values:
        activity_keys = {value for value in (activity.place_id, activity.city_service_id) if value}
        if not any(value in activity_keys for value in scoped_values):
            return (), ("PROVIDER_SCOPE_MISMATCH",), True
        if payload.get("route_intersection_place_ids"):
            refs.append("event.payload.route_intersection")
        elif payload.get("provider_scope_place_ids"):
            refs.append("event.payload.provider_scope")
        else:
            refs.append("event.payload.scope_place_ids")
        strong_reference = True

    if _polygon_rings(payload):
        geometry_ref = _geometry_scope_ref(activity, payload)
        if geometry_ref is None:
            return (), ("GEOSPATIAL_POLYGON_MISMATCH",), False
        refs.append(geometry_ref)

    if any(payload.get(key) not in (None, "") for key in ("latitude", "longitude")):
        if not _within_scope(activity, payload):
            return (), ("GEOSPATIAL_SCOPE_MISMATCH",), False
        refs.append("event.payload.geo_scope")

    if any(payload.get(key) not in (None, "") for key in _STRUCTURED_TIME_KEYS):
        if not _overlaps_event_window(activity, payload):
            return (), ("TEMPORAL_SCOPE_MISMATCH",), False
        refs.append("event.payload.time_scope")

    if _text_mentions_activity(activity, payload):
        refs.append("event.payload.server_name_match")

    return tuple(dict.fromkeys(refs)), tuple(reasons), strong_reference


def _stage_for(
    evidence_refs: tuple[str, ...], *, strong_reference: bool
) -> tuple[ImpactMaturity, int]:
    if not evidence_refs:
        return "AI_ONLY", 1
    # A direct server reference is sufficient. A text/name, time, or geo hint
    # alone is only corroboration; validation requires a second independent
    # signal so confidence cannot promote a weak semantic guess.
    minimum = 1 if strong_reference else 2
    if len(evidence_refs) >= minimum:
        return "VALIDATED_IMPACT", minimum
    return "CORROBORATED", minimum


def validate_impact_candidates(
    *,
    candidates: Sequence[ImpactCandidate],
    activities: Sequence[ImpactActivityContext],
    event_payload: Mapping[str, Any] | None = None,
) -> ImpactValidationResult:
    """Validate AI hypotheses using frozen server-owned corroboration facts.

    The validator has no provider/network imports. An allowlisted activity and a
    high model confidence are not corroboration, so missing structured
    scope/time remains ``AI_ONLY``. A server-known, non-terminal,
    non-protected Activity can nevertheless be proposal-eligible at AI_ONLY;
    the later deterministic feasibility gate and Delegation Policy decide
    whether that proposal may execute automatically.
    """

    payload = event_payload or {}
    by_id = {item.activity_id: item for item in activities}
    valid: list[ValidatedImpactCandidate] = []
    rejected: list[str] = []
    reasons: list[str] = []
    seen: set[str] = set()

    ordered_candidates = sorted(
        candidates,
        key=lambda item: (
            by_id[item.activity_id].sequence_no if item.activity_id in by_id else 10**9,
            item.activity_id,
            -item.confidence,
        ),
    )
    for candidate in ordered_candidates:
        activity = by_id.get(candidate.activity_id)
        if activity is None:
            rejected.append(candidate.activity_id)
            reasons.append("UNKNOWN_ACTIVITY_ID_REJECTED")
            continue
        if candidate.activity_id in seen:
            continue
        seen.add(candidate.activity_id)
        if activity.status in TERMINAL_ACTIVITY_STATUSES:
            rejected.append(candidate.activity_id)
            reasons.append("TERMINAL_ACTIVITY_REJECTED")
            continue

        # Event text is data, not an instruction channel. Keep this narrow so
        # ordinary natural-language semantic interpretation remains available.
        if _text_contains_injection(payload):
            rejected.append(candidate.activity_id)
            reasons.append("PROMPT_INJECTION_TEXT_REJECTED")
            continue
        evidence_refs, evidence_reasons, strong_reference = _corroboration(activity, payload)
        if evidence_reasons:
            rejected.append(candidate.activity_id)
            reasons.extend(evidence_reasons)
            continue
        # A provider-owned Activity/place scope is authoritative for this
        # candidate. Natural-language aliases can be broad (for example,
        # "outdoor"), so they must not overturn an explicit server target.
        if (
            _text_mentions_other_activity(activity, activities, payload)
            and not _polygon_rings(payload)
            and not strong_reference
        ):
            rejected.append(candidate.activity_id)
            reasons.append("SEMANTIC_ACTIVITY_CONTRADICTION")
            continue
        stage, minimum_required = _stage_for(evidence_refs, strong_reference=strong_reference)
        candidate_reasons = list(candidate.reason_codes)
        candidate_reasons.extend(evidence_refs)
        actionable = True
        lifecycle_path = ["AI_HYPOTHESIS", "STRUCTURALLY_ELIGIBLE"]
        if stage == "AI_ONLY":
            candidate_reasons.append("AI_ONLY_UNCORROBORATED")
            reasons.extend(("AI_ONLY_UNCORROBORATED", "AI_HYPOTHESIS_STRUCTURALLY_ELIGIBLE"))
        elif stage == "CORROBORATED":
            candidate_reasons.append("CORROBORATED_REQUIRES_POLICY_REVIEW")
            lifecycle_path.append("CORROBORATED")
            reasons.append("CORROBORATED")
        else:
            candidate_reasons.append("VALIDATED_IMPACT_EVIDENCE")
            lifecycle_path.extend(("CORROBORATED", "VALIDATED_IMPACT"))
            reasons.append("VALIDATED_IMPACT_EVIDENCE")
        if candidate.confidence < MIN_ACTIONABLE_CONFIDENCE:
            candidate_reasons.append("LOW_CONFIDENCE")
            actionable = False
            reasons.append("LOW_CONFIDENCE")
        if activity.is_c4 or activity.locked:
            # C4 can be represented as risk evidence, but the resolver never
            # authorizes it as a mutation target.
            candidate_reasons.append("C4_PROTECTED_RISK_ONLY")
            actionable = False
            reasons.append("C4_PROTECTED_RISK_ONLY")
        valid.append(
            ValidatedImpactCandidate(
                activity_id=activity.activity_id,
                impact_type=candidate.impact_type,
                confidence=candidate.confidence,
                reason_codes=tuple(sorted(set(candidate_reasons))),
                evidence_refs=tuple(sorted(set(candidate.evidence_refs))),
                human_explanation=candidate.human_explanation,
                actionable=actionable,
                corroboration_stage=stage,
                corroboration_refs=evidence_refs,
                independent_evidence_count=len(evidence_refs),
                minimum_evidence_required=minimum_required,
                lifecycle_path=tuple(lifecycle_path),
            )
        )

    actionable = [item for item in valid if item.actionable]
    actionable.sort(
        key=lambda item: (
            0 if item.corroboration_stage == "VALIDATED_IMPACT" else 1,
            0 if by_id[item.activity_id].is_current else 1,
            by_id[item.activity_id].sequence_no,
            item.activity_id,
        )
    )
    selected_item = actionable[0] if actionable else None
    if selected_item is not None:
        status = {
            "AI_ONLY": "AI_ONLY",
            "CORROBORATED": "CORROBORATED",
            "VALIDATED_IMPACT": "VALIDATED",
        }[selected_item.corroboration_stage]
        if status == "AI_ONLY":
            reasons.append("AI_HYPOTHESIS_STRUCTURALLY_ELIGIBLE")
        else:
            reasons.append("DETERMINISTIC_IMPACT_VALIDATION_PASSED")
    elif valid and any(
        "C4_PROTECTED_RISK_ONLY" in item.reason_codes or "LOW_CONFIDENCE" in item.reason_codes
        for item in valid
    ):
        status = "RISK_ONLY_OR_UNACTIONABLE"
        reasons.append("NO_ACTIONABLE_IMPACT_CANDIDATE")
    elif valid and any(item.corroboration_refs for item in valid):
        status = "CORROBORATED"
        reasons.append("CORROBORATION_REQUIRES_POLICY_REVIEW")
    elif valid:
        status = "AI_ONLY"
        reasons.append("NO_INDEPENDENT_CORROBORATION")
    else:
        status = "REJECTED"
        reasons.append("NO_VALID_IMPACT_CANDIDATE")
    return ImpactValidationResult(
        candidates=tuple(valid),
        selected_activity_id=selected_item.activity_id if selected_item else None,
        rejected_activity_ids=tuple(sorted(set(rejected))),
        reason_codes=tuple(sorted(set(reasons))),
        status=status,
        selected_stage=selected_item.corroboration_stage if selected_item else None,
        selected_corroboration_refs=(selected_item.corroboration_refs if selected_item else ()),
        selected_independent_evidence_count=(
            selected_item.independent_evidence_count if selected_item else 0
        ),
    )
