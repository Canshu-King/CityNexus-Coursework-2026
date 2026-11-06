from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable

from app.capabilities import accessibility_state
from app.replanning.candidate_evidence import verified_interest_preference_fit


@dataclass(frozen=True)
class ReplacementChoice:
    place_id: str
    title: str
    duration_min: int
    cost_minor: int
    reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class ExecutionPartition:
    terminal_prefix_ids: tuple[str, ...]
    current_activity_id: str | None
    c4_anchor_ids: tuple[str, ...]
    mutable_suffix_ids: tuple[str, ...]


def partition_execution(
    activities: Iterable[Any],
    *,
    current_activity_id: str | None,
    c4_anchors: Iterable[tuple[str, str]],
) -> ExecutionPartition:
    """Partition an active plan into immutable/current/C4/mutable sets."""

    rows = sorted(activities, key=lambda item: (int(item.sequence_no), str(item.id)))
    anchors = {
        (str(key), datetime.fromisoformat(str(start)).isoformat()) for key, start in c4_anchors
    }
    terminal: list[str] = []
    protected: list[str] = []
    mutable: list[str] = []
    current: str | None = None
    for item in rows:
        activity_id = str(item.id)
        status = str(getattr(item, "status", ""))
        semantic = str(
            getattr(item, "place_id", None) or getattr(item, "city_service_id", None) or activity_id
        )
        start_at = datetime.fromisoformat(str(item.start_at)).isoformat()
        if status in {"COMPLETED", "SKIPPED", "CANCELLED"}:
            terminal.append(activity_id)
        elif activity_id == current_activity_id:
            current = activity_id
        elif (semantic, start_at) in anchors:
            protected.append(activity_id)
        else:
            mutable.append(activity_id)
    return ExecutionPartition(tuple(terminal), current, tuple(protected), tuple(mutable))


def _json(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return default
    return value


def _capability_candidate_fit(
    preferences: Iterable[Any], meta: dict[str, Any], tags: set[str]
) -> tuple[bool, float, tuple[str, ...]]:
    """Apply capability constraints to ranking without inventing evidence."""

    score = 0.0
    reasons: list[str] = []
    accessibility = meta.get("accessibility") or meta.get("accessibility_evidence")
    for item in preferences:
        if str(getattr(item, "category", "")) != "capability":
            continue
        code = str(getattr(item, "preference_key", "")).upper()
        params = _json(getattr(item, "value_json", "{}"), {})
        if not isinstance(params, dict):
            params = {}
        severity = str(params.get("severity") or getattr(item, "severity", "SOFT")).upper()
        if code in {"STEP_FREE_REQUIRED", "AVOID_STAIRS"}:
            state = accessibility_state(accessibility)
            if severity == "HARD" and state != "ACCESSIBILITY_VERIFIED":
                return False, 0.0, ("ACCESSIBILITY_EVIDENCE_REQUIRED",)
            if state == "ACCESSIBILITY_VERIFIED":
                score += 1.0
                reasons.append("ACCESSIBILITY_VERIFIED")
        elif code == "MAX_WALKING_PER_LEG_M":
            limit = params.get("max_m")
            distance = meta.get("walking_distance_m", meta.get("distance_m"))
            if severity == "HARD":
                try:
                    if distance is None:
                        return False, 0.0, ("WALKING_EVIDENCE_REQUIRED",)
                    if int(distance) > int(limit):
                        return False, 0.0, ("MAX_WALKING_PER_LEG_EXCEEDED",)
                    score += 1.0
                except (TypeError, ValueError):
                    return False, 0.0, ("WALKING_EVIDENCE_REQUIRED",)
        elif code == "INDOOR_PREFERRED" and "indoor" in tags:
            score += 0.25
            reasons.append("INDOOR_PREFERRED")
        elif code in {"QUIET_ENVIRONMENT", "SENSORY_LOW_STIMULATION"} and (
            "quiet" in tags or "low-stimulation" in tags or "sensory-friendly" in tags
        ):
            score += 0.25
            reasons.append("LOW_STIMULATION_PREFERRED")
    return True, score, tuple(reasons)


#: Decimal places used when comparing coordinates.  ~5 places is about one
#: metre, which is finer than any two distinct venues are ever recorded.
_COORD_QUANTUM = 5

#: Conservative ground distance (metres) within which two different-provider rows
#: that share a venue name are treated as possibly the same physical venue and
#: downgraded DIFFERENT -> AMBIGUOUS (V3-4).  Proximity alone is never SAME.
_NEAR_VENUE_METERS = 60.0


#: ``<namespace>:<object-type>/<object-id>`` — a provider-scoped reference such
#: as ``overpass:node/1588295483``.  The numeric suffix alone is never an
#: identity: OSM ``node/123`` and ``way/123`` are different objects, and two
#: unrelated providers may both use the number ``123``.
_PROVIDER_REFERENCE = re.compile(
    r"^(?P<namespace>[a-z0-9_]+):(?P<object_type>[a-z]+)/(?P<object_id>.+)$",
    re.IGNORECASE,
)

#: ``CN-<CITY-CODE>:<row-id>`` — a CityNexus domain wrapper that explicitly
#: names the provider Place row it wraps, for example
#: ``CN-XIAMEN:real-place-07e7689756fa27109a8bff96``.  Only this documented
#: CityNexus prefix is treated as a wrapper cross-reference: an arbitrary
#: ``SOMEPREFIX:value`` external id from any other source must not be mistaken
#: for one (T1 hardening rule C — a broad uppercase-prefix regex wrongly turned
#: every ``FOO:bar`` id into a CityNexus wrapper).
_WRAPPER_REFERENCE = re.compile(r"^CN-[A-Z0-9]+:(?P<target>.+)$")

#: Verdicts returned by :func:`compare_physical_venue`.
SAME = "SAME"
DIFFERENT = "DIFFERENT"
AMBIGUOUS = "AMBIGUOUS"


@dataclass(frozen=True)
class PhysicalVenueIdentity:
    """The structured identity evidence one Place row actually carries.

    ``row_id``, ``wrapper_reference`` and the provider-scoped tuple are strong
    evidence.  ``normalized_name``, ``category`` and ``coordinates`` are
    supporting evidence only: one building routinely holds several venues at a
    single centroid, so coordinates can never prove identity by themselves.
    """

    row_id: str | None
    wrapper_reference: str | None
    provider_namespace: str | None
    provider_object_type: str | None
    provider_object_id: str | None
    normalized_name: str | None
    category: str | None
    coordinates: tuple[float, float] | None

    @property
    def provider_scoped(self) -> tuple[str, str, str] | None:
        """``(namespace, object-type, object-id)`` when all three are known."""

        if (
            self.provider_namespace
            and self.provider_object_type
            and self.provider_object_id
        ):
            return (
                self.provider_namespace,
                self.provider_object_type,
                self.provider_object_id,
            )
        return None


def _non_empty(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _normalized_label(value: Any) -> str | None:
    text = _non_empty(value)
    return None if text is None else re.sub(r"\s+", " ", text.casefold())


def _coordinates(place: Any) -> tuple[float, float] | None:
    try:
        return (
            round(float(getattr(place, "latitude")), _COORD_QUANTUM),
            round(float(getattr(place, "longitude")), _COORD_QUANTUM),
        )
    except (TypeError, ValueError, AttributeError):
        return None


def physical_venue_identity(place: Any) -> PhysicalVenueIdentity | None:
    """Extract the identity evidence carried by one Place row."""

    if place is None:
        return None
    row_id = _non_empty(getattr(place, "id", None))
    external_id = _non_empty(getattr(place, "external_id", None))
    wrapper_reference: str | None = None
    namespace: str | None = None
    object_type: str | None = None
    object_id: str | None = None
    if external_id:
        provider_match = _PROVIDER_REFERENCE.match(external_id)
        if provider_match:
            namespace = provider_match.group("namespace").casefold()
            object_type = provider_match.group("object_type").casefold()
            object_id = _non_empty(provider_match.group("object_id"))
        else:
            wrapper_match = _WRAPPER_REFERENCE.match(external_id)
            if wrapper_match:
                target = _non_empty(wrapper_match.group("target"))
                if target and target != row_id:
                    wrapper_reference = target
    return PhysicalVenueIdentity(
        row_id=row_id,
        wrapper_reference=wrapper_reference,
        provider_namespace=namespace,
        provider_object_type=object_type,
        provider_object_id=object_id,
        normalized_name=_normalized_label(getattr(place, "name", None)),
        category=_normalized_label(getattr(place, "category", None)),
        coordinates=_coordinates(place),
    )


def _near_coordinates(
    a: "tuple[float, float] | None", b: "tuple[float, float] | None"
) -> bool:
    """Approximate ground distance within a conservative venue threshold.

    Two providers routinely geocode the same restaurant a few to a few tens of
    metres apart (door vs building centroid), so exact rounded equality would
    wrongly split one venue into two.  This only feeds the DIFFERENT -> AMBIGUOUS
    downgrade; proximity alone is never treated as SAME (V3-4).
    """

    if a is None or b is None:
        return False
    lat_m = abs(a[0] - b[0]) * 111320.0
    lon_m = abs(a[1] - b[1]) * 111320.0 * math.cos(math.radians((a[0] + b[0]) / 2.0))
    return (lat_m * lat_m + lon_m * lon_m) ** 0.5 <= _NEAR_VENUE_METERS


def _corroborated_same_venue(
    left: "PhysicalVenueIdentity", right: "PhysicalVenueIdentity"
) -> bool:
    """Whether two rows carry a same-venue signature despite differing ids.

    Used only to downgrade ``DIFFERENT`` to ``AMBIGUOUS`` across distinct
    provider identities.  It requires every corroborating signal to line up:
    a near coordinate (within the conservative threshold), an equal, present
    normalized name, and no category conflict.  It is deliberately conservative —
    a match here means "we cannot safely call these different", not "they are
    proven the same".
    """

    if not _near_coordinates(left.coordinates, right.coordinates):
        return False
    left_name = left.normalized_name
    right_name = right.normalized_name
    if not left_name or not right_name or left_name != right_name:
        return False
    if (
        left.category
        and right.category
        and left.category != right.category
    ):
        return False
    return True


def compare_physical_venue(left: Any, right: Any) -> str:
    """Ordered strong-evidence comparison of two Place rows.

    Returns ``SAME``, ``DIFFERENT`` or ``AMBIGUOUS``.  Identity is proven by
    structure — a non-empty row id, an explicit wrapper cross-reference, or a
    provider-scoped ``(namespace, object-type, object-id)`` tuple.  Coordinates
    only ever support identity together with a matching name and category.
    """

    left_identity = physical_venue_identity(left)
    right_identity = physical_venue_identity(right)
    if left_identity is None or right_identity is None:
        return DIFFERENT

    # Rule 1 — null safety: missing ids never match by equality.
    if (
        left_identity.row_id
        and right_identity.row_id
        and left_identity.row_id == right_identity.row_id
    ):
        return SAME

    # Rule 2 — an explicit wrapper cross-reference is structural identity.
    if left_identity.wrapper_reference and (
        left_identity.wrapper_reference == right_identity.row_id
        or left_identity.wrapper_reference == right_identity.wrapper_reference
    ):
        return SAME
    if (
        right_identity.wrapper_reference
        and right_identity.wrapper_reference == left_identity.row_id
    ):
        return SAME

    # Rule 3 — provider identity is a tuple, never a bare numeric suffix.
    left_scoped = left_identity.provider_scoped
    right_scoped = right_identity.provider_scoped
    if left_scoped is not None and right_scoped is not None:
        if left_scoped == right_scoped:
            return SAME
        # Different provider tuples are NOT proof of different physical venues:
        # two providers routinely describe the same restaurant with different ids.
        # Only a corroborating venue signature (same coordinate + same name, no
        # category conflict) can downgrade this from DIFFERENT to AMBIGUOUS so the
        # caller fails safe (T1 hardening rule B / B2).
        if _corroborated_same_venue(left_identity, right_identity):
            return AMBIGUOUS
        return DIFFERENT

    # Rules 4/5 — coordinates are supporting evidence, and a name alone is not
    # proof.  A shared coordinate plus a corroborating name and compatible
    # category is the strongest statement this evidence can support.
    if (
        left_identity.coordinates is not None
        and left_identity.coordinates == right_identity.coordinates
    ):
        left_name = left_identity.normalized_name
        right_name = right_identity.normalized_name
        if left_name and right_name:
            if left_name != right_name:
                return DIFFERENT
            if (
                left_identity.category
                and right_identity.category
                and left_identity.category != right_identity.category
            ):
                return AMBIGUOUS
            return SAME
        # A shared coordinate with nothing to corroborate it is not identity:
        # several venues routinely share one building centroid.
        return DIFFERENT

    return DIFFERENT


def is_same_physical_venue(left: Any, right: Any) -> bool:
    """Whether two Place rows are *proven* to describe one physical venue.

    Honest identity truth only: ``True`` strictly for a ``SAME`` verdict.  It
    must never report ``AMBIGUOUS`` as same — callers that need the fail-safe
    "do not treat these as different" behaviour use
    :func:`should_exclude_replacement_candidate` instead
    (T1 hardening rule A / B2).
    """

    return compare_physical_venue(left, right) == SAME


def should_exclude_replacement_candidate(candidate: Any, reference: Any) -> bool:
    """Whether ``candidate`` must be kept out of ``reference``'s replacement family.

    Exclusion is a fail-safe policy, not an identity claim: a candidate that is
    proven the SAME venue, or merely AMBIGUOUS (we cannot safely tell it apart
    from the venue the traveller just lost), is excluded — so a closed venue can
    never reappear as its own replacement under a wrapper or cross-provider id.
    Only a clean ``DIFFERENT`` verdict lets a candidate through.
    """

    return compare_physical_venue(candidate, reference) != DIFFERENT


def choose_closure_replacement(
    *,
    affected_place: Any,
    candidate_places: Iterable[Any],
    occupied_place_ids: set[str],
    max_duration_min: int,
) -> ReplacementChoice | None:
    """Choose one deterministic same-slot replacement for a closed flexible place.

    This is a mock candidate-family generator, not the feasibility authority.
    The returned candidate must still pass the shared deterministic gate.
    """

    choices = rank_closure_replacements(
        affected_place=affected_place,
        candidate_places=candidate_places,
        occupied_place_ids=occupied_place_ids,
        max_duration_min=max_duration_min,
    )
    return choices[0] if choices else None


def rank_closure_replacements(
    *,
    affected_place: Any,
    candidate_places: Iterable[Any],
    occupied_place_ids: set[str],
    max_duration_min: int,
    traveler_preferences: Iterable[Any] = (),
    occupied_places: Iterable[Any] | None = None,
) -> tuple[ReplacementChoice, ...]:
    """Return every distinct same-slot replacement in deterministic rank order."""

    traveler_preferences = list(traveler_preferences)
    affected_tags = set(_json(getattr(affected_place, "tags_json", "[]"), []))
    occupied_places = list(occupied_places or ())
    ranked: list[tuple[tuple[Any, ...], ReplacementChoice]] = []
    for place in candidate_places:
        place_id = str(place.id)
        # A closed venue must never be offered as its own replacement, and a
        # venue already used elsewhere in the plan must not be reused.  Both
        # checks are physical, because one venue can be stored under several
        # id namespaces (domain wrapper vs provider row).
        if place_id in occupied_place_ids or should_exclude_replacement_candidate(
            place, affected_place
        ):
            continue
        if any(
            should_exclude_replacement_candidate(place, other) for other in occupied_places
        ):
            continue
        meta = _json(getattr(place, "metadata_json", "{}"), {})
        tags = set(_json(getattr(place, "tags_json", "[]"), []))
        duration = int(meta.get("duration_min", 60))
        cost_evidence = meta.get("cost_evidence") or {}
        if (
            str(cost_evidence.get("status")) in {"KNOWN", "ESTIMATED"}
            and cost_evidence.get("amount_minor") is not None
        ):
            cost = int(cost_evidence["amount_minor"])
        else:
            cost = int(meta.get("cost_minor", 0))
        if duration <= 0 or duration > max_duration_min:
            continue
        capability_ok, capability_fit, capability_reasons = _capability_candidate_fit(
            traveler_preferences, meta, tags
        )
        if not capability_ok:
            continue
        overlap = len(affected_tags & tags)
        indoor = 1 if "indoor" in tags else 0
        choice = ReplacementChoice(
            place_id=place_id,
            title=str(place.name),
            duration_min=duration,
            cost_minor=cost,
            reason_codes=(
                "DISRUPTED_ACTIVITY_REPLACED",
                "MINIMAL_SCHEDULE_CHANGE",
                *(("INDOOR_ALTERNATIVE",) if indoor else ()),
                *capability_reasons,
            ),
        )
        preference_fit = verified_interest_preference_fit(traveler_preferences, candidate_tags=tags)
        # Highest tag overlap, then indoor, then lower cost, then stable ID.
        rank = (
            -overlap,
            -indoor,
            -(0.5 if preference_fit is None else preference_fit) - capability_fit,
            cost,
            place_id,
        )
        ranked.append((rank, choice))
    ranked.sort(key=lambda item: item[0])
    return tuple(choice for _, choice in ranked)
