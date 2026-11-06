"""Authoritative deterministic feasibility gate for MVP plan candidates.

The gate is deliberately independent of FastAPI route handlers.  It accepts
snapshots of a Journey, its active constraints, the current mock catalog and
the current mock mobility provider so the exact same check can be reused by
initial planning, plan selection and the later WP06 replanning flow.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Any, Iterable, Mapping, Sequence

from app.capabilities import (
    CAPABILITY_CODES,
    accessibility_state,
    evaluate_accessibility_requirement,
    normalize_osm_tags,
)
from app.integrations.mock.providers import MockMobilityProvider
from app.replanning.reference_replanner import Activity as ReferenceActivity
from app.replanning.reference_replanner import Candidate as ReferenceCandidate
from app.replanning.reference_replanner import Commitment, validate_candidate


@dataclass(frozen=True)
class FeasibilityActivity:
    """The activity facts needed by the deterministic gate."""

    key: str
    start_at: datetime
    end_at: datetime
    cost_minor: int
    place_id: str | None = None
    city_service_id: str | None = None
    currency: str | None = None
    travel_from_prev_min: int = 0
    locked: bool = False
    walking_distance_m: int | None = None
    walking_min: int | None = None
    metadata: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class FeasibilityContext:
    """A read-only planning context captured immediately before a gate run."""

    journey_id: str
    city_code: str
    journey_start: datetime
    journey_end: datetime
    budget_amount_minor: int
    budget_currency: str
    constraints: tuple[Any, ...]
    places: Mapping[str, Any]
    services: Mapping[str, Any]
    mobility_provider: Any
    plan_currency: str | None = None


@dataclass(frozen=True)
class FeasibilityResult:
    passed: bool
    failures: tuple[str, ...]
    effective_start_at: datetime
    effective_end_at: datetime
    budget_max_minor: int


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _parse_json(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return default
    return value


def _parse_dt(value: Any, *, reference: datetime | None = None) -> datetime:
    if isinstance(value, datetime):
        result = value
    else:
        text = str(value)
        if reference is not None and ":" in text and "T" not in text and "-" not in text:
            hour, minute = (int(part) for part in text.split(":", 1))
            result = datetime.combine(reference.date(), time(hour, minute), reference.tzinfo)
        else:
            result = datetime.fromisoformat(text)
    if result.tzinfo is None:
        result = result.replace(tzinfo=reference.tzinfo if reference else timezone.utc)
    return result


def _minutes_from_start(value: datetime, start: datetime) -> int:
    return int((value - start).total_seconds() / 60)


def _constraint_parts(constraint: Any) -> tuple[str, str, str, dict[str, Any], bool]:
    params = _parse_json(_field(constraint, "params_json", _field(constraint, "params", {})), {})
    if not isinstance(params, dict):
        params = {}
    return (
        str(_field(constraint, "kind", "")),
        str(_field(constraint, "category", "")),
        str(_field(constraint, "rule_code", "")).upper(),
        params,
        bool(_field(constraint, "active", True)),
    )


def build_context(
    journey: Any,
    constraints: Iterable[Any],
    places: Mapping[str, Any] | Iterable[Any],
    services: Mapping[str, Any] | Iterable[Any],
    *,
    mobility_provider: Any | None = None,
    plan_currency: str | None = None,
) -> FeasibilityContext:
    """Build a planning snapshot from ORM records or plain dictionaries."""

    place_map = (
        dict(places)
        if isinstance(places, Mapping)
        else {str(_field(item, "id")): item for item in places}
    )
    service_map = (
        dict(services)
        if isinstance(services, Mapping)
        else {str(_field(item, "id")): item for item in services}
    )
    return FeasibilityContext(
        journey_id=str(_field(journey, "id")),
        city_code=str(_field(journey, "city_code")),
        journey_start=_parse_dt(_field(journey, "start_at")),
        journey_end=_parse_dt(_field(journey, "end_at")),
        budget_amount_minor=int(_field(journey, "budget_amount_minor", 0)),
        budget_currency=str(_field(journey, "budget_currency", "")),
        plan_currency=plan_currency.upper() if plan_currency else None,
        constraints=tuple(constraints),
        places=place_map,
        services=service_map,
        mobility_provider=mobility_provider or MockMobilityProvider(),
    )


def activity_from_record(item: Any) -> FeasibilityActivity:
    """Adapt an ORM Activity (or compatible mapping) into gate facts."""

    # One invariant: ``Activity.metadata.semantic_key`` is the stable routing /
    # feasibility identity and ``Activity.place_id`` is the database Place
    # identity.  Provider mobility evidence is frozen against semantic keys, so
    # the gate must key on exactly the same value at generation, selection,
    # booking, start and replanning - otherwise the persisted snapshot appears
    # to be missing its pairs (MOBILITY_DATA_UNAVAILABLE identity drift).
    place_id = _field(item, "place_id")
    city_service_id = _field(item, "city_service_id")
    metadata_raw = _field(item, "metadata_json", _field(item, "metadata", {}))
    metadata = _parse_json(metadata_raw, {})
    if not isinstance(metadata, Mapping):
        metadata = {}
    routed_key = place_id or city_service_id or _field(item, "activity_key")
    semantic_key = str(metadata.get("semantic_key") or "").strip() or routed_key
    if semantic_key is None:
        semantic_key = _field(item, "id")
    raw_walking_distance = _field(item, "walking_distance_m", metadata.get("walking_distance_m"))
    raw_walking_min = _field(item, "walking_min", metadata.get("walking_min"))
    try:
        walking_distance = int(raw_walking_distance) if raw_walking_distance is not None else None
    except (TypeError, ValueError):
        walking_distance = None
    try:
        walking_minutes = int(raw_walking_min) if raw_walking_min is not None else None
    except (TypeError, ValueError):
        walking_minutes = None
    return FeasibilityActivity(
        key=str(semantic_key),
        start_at=_parse_dt(_field(item, "start_at")),
        end_at=_parse_dt(_field(item, "end_at")),
        cost_minor=int(_field(item, "estimated_cost_minor", _field(item, "cost_minor", 0))),
        place_id=place_id,
        city_service_id=city_service_id,
        currency=(str(_field(item, "currency")) if _field(item, "currency") is not None else None),
        travel_from_prev_min=int(_field(item, "travel_from_prev_min", 0)),
        locked=bool(_field(item, "locked", False)),
        walking_distance_m=walking_distance,
        walking_min=walking_minutes,
        metadata=dict(metadata),
    )


def _daily_minutes(value: Any) -> int | None:
    if value is None:
        return None
    text = str(value)
    if "T" in text:
        text = text.split("T", 1)[1]
    text = text.split("+", 1)[0].split("-", 1)[0]
    pieces = text.split(":")
    if len(pieces) < 2:
        return None
    try:
        hour, minute = int(pieces[0]), int(pieces[1])
    except ValueError:
        return None
    if hour == 24 and minute == 0:
        return 24 * 60
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        return None
    return hour * 60 + minute


def _within_open_interval(
    start_at: datetime, end_at: datetime, interval: Mapping[str, Any]
) -> bool:
    open_min = _daily_minutes(interval.get("open", interval.get("first_service")))
    close_min = _daily_minutes(interval.get("close", interval.get("last_service")))
    if open_min is None or close_min is None:
        return True
    start_min = start_at.hour * 60 + start_at.minute + start_at.second / 60
    end_min = end_at.hour * 60 + end_at.minute + end_at.second / 60
    if close_min >= open_min:
        return start_min >= open_min and end_min <= close_min
    # Overnight intervals are supported without introducing a second date
    # interpretation into the fixture contract.
    return start_min >= open_min or end_min <= close_min


def _location_key(activity: FeasibilityActivity) -> str | None:
    """Routing identity for one activity.

    Prefer the frozen-evidence semantic key so a persisted plan reloads against
    the exact same mobility facts that produced it; fall back to the concrete
    Place identity for callers that never carried a semantic key.
    """

    metadata = getattr(activity, "metadata", None) or {}
    if isinstance(metadata, Mapping):
        semantic = str(metadata.get("semantic_key") or "").strip()
        if semantic:
            return semantic
    return activity.place_id or activity.city_service_id or None


# A structural C4 anchor occupies a bounded slot; this mirrors
# ``app.planning.anchor_schedule.ANCHOR_DURATION_MIN`` so both sides agree.
_ANCHOR_DURATION_MIN = 20

_CAPABILITY_GATE_CODES = {
    "STEP_FREE_REQUIRED",
    "AVOID_STAIRS",
    "MAX_WALKING_PER_LEG_M",
    "MAX_CONTINUOUS_WALKING_MIN",
    "MAX_DAILY_WALKING_M",
    "MIDDAY_REST_WINDOW",
    "REST_EVERY_MIN",
    "INFANT_NAP_WINDOW",
    "TOILET_BREAK_EVERY_MIN",
    "STROLLER",
    "LARGE_LUGGAGE",
}

# ----------------------------------------------------------------------------------------------
# ORD-0219 / arbiter `5778314066`: the write layer asks this module two questions, in this order.
#
#   1. is `rule_code` in the canonical vocabulary at all          -> else UNKNOWN_RULE_CODE
#   2. if `kind=HARD`, can this gate actually execute it          -> else HARD_RULE_NOT_EXECUTABLE
#
# The two names stay separate on purpose: "I have never heard of that rule" and "that rule is
# real but a hard trip constraint would be refused later as UNSUPPORTED_HARD_CONSTRAINT" are
# different refusals with different fixes, and collapsing them is what let a free-text
# `ConstraintInput.rule_code` reach the database and then blacken a whole journey.
# SOFT is never refused for not being executable - a preference the gate ignores is still a
# preference the ranker and the explanation may carry.
#
# Both sets are *derived*, never re-typed: `EXECUTED_HARD_RULE_CODES` is the literal list of
# codes the dispatch in `evaluate_feasibility` (:573 onwards) interprets plus the capability
# gates it reads evidence for, and the canonical vocabulary adds whatever the parser owns that
# is not executable.  If the evaluator gains a branch, the write layer follows it automatically;
# a hand-copied second list here is the drift this order exists to end.
EXECUTED_HARD_RULE_CODES = frozenset(
    {
        "BUDGET_MAX",
        "JOURNEY_END_BY",
        "JOURNEY_START_AT",
        "PLACE_UNAVAILABLE",
        "PLACE_CLOSED",
        "SERVICE_UNAVAILABLE",
        "SERVICE_CLOSED",
        "REQUIRE_PLACE",
        "LOCKED_COMMITMENT",
    }
) | frozenset(_CAPABILITY_GATE_CODES)

# Written by the demo seed as a SOFT preference (`data/mock/demo_journey.json`); the gate does not
# execute it, and refusing it would break the sealed demo for no product reason.
SOFT_ONLY_RULE_CODES = (frozenset(CAPABILITY_CODES) - frozenset(_CAPABILITY_GATE_CODES)) | frozenset(
    {"MINIMISE_PLAN_CHANGES"}
)

CANONICAL_RULE_CODES = EXECUTED_HARD_RULE_CODES | SOFT_ONLY_RULE_CODES


def constraint_write_refusal(kind: object, rule_code: object) -> str | None:
    """Return the reason this (kind, rule_code) pair must not be written, or None."""

    code = str(rule_code or "").strip().upper()
    if code not in CANONICAL_RULE_CODES:
        return "UNKNOWN_RULE_CODE"
    if str(kind or "").strip().upper() == "HARD" and code not in EXECUTED_HARD_RULE_CODES:
        return "HARD_RULE_NOT_EXECUTABLE"
    return None


def _record_metadata(record: Any) -> dict[str, Any]:
    raw = _field(record, "metadata_json", _field(record, "metadata", {}))
    value = _parse_json(raw, {})
    return dict(value) if isinstance(value, Mapping) else {}


def _accessibility_metadata(record: Any) -> Mapping[str, Any] | None:
    direct = _field(record, "accessibility", None)
    if isinstance(direct, Mapping):
        return direct
    metadata = _record_metadata(record)
    nested = metadata.get("accessibility") or metadata.get("accessibility_evidence")
    if isinstance(nested, Mapping):
        return nested
    # Top-level and nested forms must reach the evaluator identically.  The
    # whitelist previously dropped ``step_free_verified`` (so the same
    # assertion was honoured under ``tags`` but silently ignored at the top
    # level) and never projected route/highway/step-count evidence at all.
    keys = {
        "evidence_status",
        "status",
        "subject",
        "scope",
        "step_free",
        "step_free_verified",
        "route_step_free",
        "route_step_free_verified",
        "step_free_route",
        "wheelchair_access",
        "wheelchair",
        "stairs",
        "has_stairs",
        "steps",
        "step_count",
        "highway",
        "stroller_accessible",
        "luggage_accessible",
    }
    result = {key: metadata[key] for key in keys if key in metadata}
    raw_tags = metadata.get("osm_tags") or metadata.get("tags")
    tags = normalize_osm_tags(raw_tags)
    if tags:
        result["osm_tags"] = tags
    return result or None


def _capability_failure_for_record(
    code: str, record: Any, location: str
) -> list[str]:
    metadata = _accessibility_metadata(record)
    state = evaluate_accessibility_requirement(code, metadata or {})
    if code in {"STEP_FREE_REQUIRED", "AVOID_STAIRS"}:
        if state == "ACCESSIBILITY_VERIFIED":
            return []
        if state == "ACCESSIBILITY_CONFLICT":
            return [f"ACCESSIBILITY_CONFLICT:{location}"]
        # Requirement-scoped evidence is authoritative; the legacy whole-record
        # view is consulted only as a second, never-lenient opinion.  Both must
        # agree that the place is verified before the gate accepts it.
        if accessibility_state(metadata or {}) == "ACCESSIBILITY_VERIFIED":
            return []
        return [
            f"ACCESSIBILITY_UNVERIFIED:{location}",
            f"NEEDS_LIVE_EVIDENCE:{location}",
        ]
    if code == "STROLLER" and isinstance(metadata, Mapping):
        if metadata.get("stroller_accessible") is False:
            return [f"STROLLER_ACCESS_UNAVAILABLE:{location}"]
    if code == "LARGE_LUGGAGE" and isinstance(metadata, Mapping):
        if metadata.get("luggage_accessible") is False:
            return [f"LUGGAGE_ACCESS_UNAVAILABLE:{location}"]
    return []


def _window_from_params(params: Mapping[str, Any]) -> tuple[int, int] | None:
    def parse(value: Any) -> int | None:
        if value is None:
            return None
        text = str(value)
        if ":" in text:
            pieces = text.split(":", 1)
            try:
                hour, minute = int(pieces[0]), int(pieces[1])
            except ValueError:
                return None
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                return hour * 60 + minute
            return None
        try:
            raw = int(text)
        except ValueError:
            return None
        return raw if 0 <= raw <= 24 * 60 else None

    start = parse(params.get("start"))
    end = parse(params.get("end"))
    return (start, end) if start is not None and end is not None and end > start else None


def _schedule_gaps(ordered: Sequence[FeasibilityActivity]) -> list[tuple[datetime, datetime]]:
    return [
        (previous.end_at, current.start_at)
        for previous, current in zip(ordered, ordered[1:])
        if current.start_at > previous.end_at
    ]


def _continuous_spans(ordered: Sequence[FeasibilityActivity], *, min_break_min: int = 1) -> list[float]:
    """Return active/travel spans separated only by real breaks."""
    if not ordered:
        return []
    spans: list[float] = []
    span_start = ordered[0].start_at
    previous = ordered[0]
    for current in ordered[1:]:
        gap_min = max(0.0, (current.start_at - previous.end_at).total_seconds() / 60)
        if gap_min >= min_break_min:
            spans.append(max(0.0, (previous.end_at - span_start).total_seconds() / 60))
            span_start = current.start_at
        previous = current
    spans.append(max(0.0, (previous.end_at - span_start).total_seconds() / 60))
    return spans


def _activity_walking_fact(
    previous: FeasibilityActivity | None,
    current: FeasibilityActivity,
    provider: Any,
) -> tuple[int | None, int | None, str | None]:
    """Return (distance_m, walking_min, pair) from explicit/frozen evidence."""

    pair = None
    if previous is not None:
        origin = _location_key(previous)
        destination = _location_key(current)
        if origin and destination:
            pair = f"{origin}|{destination}"
            facts = getattr(provider, "facts", {})
            fact = facts.get((origin, destination)) if isinstance(facts, Mapping) else None
            if fact is not None:
                # Walking distance has one definition: explicit walking evidence,
                # or (for a genuine WALK leg only) the leg distance itself.  A
                # total route distance on a motorised leg is never walking.
                mode = str(getattr(fact, "mode", "") or "").upper()
                distance = getattr(fact, "walking_distance_m", None)
                if distance is None and mode == "WALK":
                    distance = getattr(fact, "distance_m", None)
                walking_min = getattr(fact, "walking_min", None)
                if walking_min is None and mode == "WALK":
                    walking_min = getattr(fact, "minutes", None)
                if distance is not None or walking_min is not None:
                    return (
                        int(distance) if distance is not None else None,
                        int(walking_min) if walking_min is not None else None,
                        pair,
                    )
    metadata = current.metadata or {}
    distance = current.walking_distance_m
    if distance is None and isinstance(metadata, Mapping):
        # Only an explicit walking distance counts here.  ``distance_m`` is a
        # total route length and must not be promoted to a walking budget.
        distance = metadata.get("walking_distance_m")
    walking_min = current.walking_min
    if walking_min is None and isinstance(metadata, Mapping):
        walking_min = metadata.get("walking_min")
    try:
        distance_value = int(distance) if distance is not None else None
    except (TypeError, ValueError):
        distance_value = None
    try:
        walking_value = int(walking_min) if walking_min is not None else None
    except (TypeError, ValueError):
        walking_value = None
    return distance_value, walking_value, pair


def _active_hard_constraints(
    context: FeasibilityContext,
) -> list[tuple[str, str, str, dict[str, Any]]]:
    return [
        (kind, category, code, params)
        for constraint in context.constraints
        for kind, category, code, params, active in [_constraint_parts(constraint)]
        if active and kind.upper() == "HARD"
    ]


def _locked_commitment_specs(
    hard_constraints: Sequence[tuple[str, str, str, dict[str, Any]]],
    *,
    journey_start: datetime,
) -> list[tuple[Commitment, str]]:
    """Return ``(commitment, anchor_role)`` pairs for every active C4 lock."""

    specs: list[tuple[Commitment, str]] = []
    for _, _, code, params in hard_constraints:
        if code != "LOCKED_COMMITMENT":
            continue
        activity_key = (
            params.get("place_id")
            or params.get("service_id")
            or params.get("semantic_ref")
            or params.get("activity_key")
        )
        start_value = params.get("start_at") or params.get("time")
        time_boundary = str(params.get("time_boundary") or "START").upper()
        if time_boundary not in {"START", "END"}:
            raise ValueError("LOCKED_COMMITMENT time boundary is invalid")
        if activity_key is None or (start_value is None and params.get("start_min") is None):
            raise ValueError("LOCKED_COMMITMENT requires an activity and start time")
        if params.get("start_min") is not None:
            try:
                start = int(params["start_min"])
            except (TypeError, ValueError):
                raise ValueError("LOCKED_COMMITMENT relative start time is invalid") from None
        else:
            try:
                start = _minutes_from_start(
                    _parse_dt(start_value, reference=journey_start), journey_start
                )
            except (TypeError, ValueError, OverflowError):
                raise ValueError("LOCKED_COMMITMENT start time is invalid") from None
        specs.append(
            (
                Commitment(str(activity_key), "LOCKED", start, time_boundary),
                str(params.get("anchor_role") or "").upper(),
            )
        )
    return specs


def _locked_commitments(
    hard_constraints: Sequence[tuple[str, str, str, dict[str, Any]]],
    *,
    journey_start: datetime,
) -> list[Commitment]:
    """Normalize every lock to semantic identity + minutes from journey start.

    The contract accepts an ISO datetime (``start_at``) or a time-only value
    (``time``).  Both are converted exactly once to the reference replanner's
    relative-minute basis.  ``start_min`` is retained as an explicit relative
    form for framework-independent callers; ambiguous minute-of-day integers
    are intentionally not inferred.
    """

    return [
        commitment
        for commitment, _role in _locked_commitment_specs(
            hard_constraints, journey_start=journey_start
        )
    ]


def evaluate_feasibility(
    context: FeasibilityContext,
    activities: Sequence[FeasibilityActivity],
) -> FeasibilityResult:
    """Run every authoritative deterministic MVP feasibility invariant.

    The function has no persistence side effects.  Callers must evaluate it
    before scoring or adding a PlanVersion and must use the same function again
    immediately before selecting an existing candidate.
    """

    failures: list[str] = []
    provider = context.mobility_provider
    provider_key = getattr(provider, "provider_key", None)
    provider_is_frozen = bool(getattr(provider, "is_frozen_snapshot", False))
    if provider_key != "mock-mobility" and not provider_is_frozen:
        failures.append("NON_DETERMINISTIC_MOBILITY_PROVIDER")

    hard_constraints = _active_hard_constraints(context)
    effective_start = context.journey_start
    effective_end = context.journey_end
    budget_max = context.budget_amount_minor
    # A confirmed reservation is an authoritative real-world fact: the draft
    # Journey window must contain it (a booked 08:00 departure cannot be made
    # unplannable by a 09:00 draft start).  This widening applies only to the
    # *anchor* window checks below - it never relaxes the ARRIVAL/DEPARTURE
    # boundaries that constrain flexible city activity.
    for _kind, _category, code, params in hard_constraints:
        if code != "LOCKED_COMMITMENT":
            continue
        raw_start = params.get("start_at") or params.get("time")
        if raw_start is None:
            continue
        try:
            anchor_at = _parse_dt(raw_start, reference=context.journey_start)
        except (TypeError, ValueError):
            continue
        boundary = str(params.get("time_boundary") or "START").upper()
        if boundary == "END":
            anchor_start = anchor_at - timedelta(minutes=_ANCHOR_DURATION_MIN)
            anchor_end = anchor_at
        else:
            anchor_start = anchor_at
            anchor_end = anchor_at + timedelta(minutes=_ANCHOR_DURATION_MIN)
        effective_start = min(effective_start, anchor_start)
        effective_end = max(effective_end, anchor_end)
    blocked_places: set[str] = set()
    blocked_services: set[str] = set()
    required_places: set[str] = set()

    for _, category, code, params in hard_constraints:
        if code == "BUDGET_MAX":
            raw_amount = params.get("amount_minor")
            try:
                amount = int(raw_amount)
            except (TypeError, ValueError):
                failures.append("INVALID_BUDGET_MAX")
                continue
            if amount < 0:
                failures.append("INVALID_BUDGET_MAX")
            else:
                budget_max = min(budget_max, amount)
            currency = params.get("currency")
            if currency and str(currency).upper() != context.budget_currency.upper():
                failures.append("BUDGET_CURRENCY_MISMATCH")
        elif code == "JOURNEY_END_BY":
            raw_end = params.get("time") or params.get("end_at")
            try:
                end_by = _parse_dt(raw_end, reference=context.journey_start)
            except (TypeError, ValueError):
                failures.append("INVALID_JOURNEY_END_BY")
            else:
                effective_end = min(effective_end, end_by)
        elif code == "JOURNEY_START_AT":
            raw_start = params.get("time") or params.get("start_at")
            try:
                start_at = _parse_dt(raw_start, reference=context.journey_start)
            except (TypeError, ValueError):
                failures.append("INVALID_JOURNEY_START_AT")
            else:
                effective_start = max(effective_start, start_at)
        elif code in {"PLACE_UNAVAILABLE", "PLACE_CLOSED"}:
            value = params.get("place_id") or params.get("place_ids")
            if isinstance(value, list):
                blocked_places.update(str(item) for item in value)
            elif value is not None:
                blocked_places.add(str(value))
        elif code in {"SERVICE_UNAVAILABLE", "SERVICE_CLOSED"}:
            value = params.get("service_id") or params.get("service_ids")
            if isinstance(value, list):
                blocked_services.update(str(item) for item in value)
            elif value is not None:
                blocked_services.add(str(value))
        elif code == "REQUIRE_PLACE":
            value = params.get("place_id") or params.get("place_ids")
            if isinstance(value, list):
                required_places.update(str(item) for item in value)
            elif value is not None:
                required_places.add(str(value))
        elif code not in {"LOCKED_COMMITMENT"} and code not in _CAPABILITY_GATE_CODES:
            # A hard rule that the gate cannot interpret must never be bypassed
            # by scoring or persistence.
            failures.append(f"UNSUPPORTED_HARD_CONSTRAINT:{code or category}")

    if effective_end < effective_start:
        failures.append("JOURNEY_WINDOW_INVALID")

    ordered = sorted(activities, key=lambda item: (item.start_at, item.end_at, item.key))
    if not ordered:
        failures.append("NO_ACTIVITIES")
    if any(item.cost_minor < 0 for item in ordered):
        failures.append("INVALID_ACTIVITY_COST")
    # A PLANNED Journey must not carry a selected plan whose activities are
    # denominated in another currency.  This check intentionally lives in the
    # shared deterministic gate so generation, selection, and mutation
    # revalidation apply the same invariant even without a HARD BUDGET_MAX rule.
    expected_currency = context.budget_currency.upper()
    if context.plan_currency is not None and context.plan_currency != expected_currency:
        failures.append("PLAN_CURRENCY_MISMATCH")
    for item in ordered:
        if item.currency is not None and item.currency.upper() != expected_currency:
            failures.append(f"ACTIVITY_CURRENCY_MISMATCH:{item.key}")
    if any(item.end_at <= item.start_at for item in ordered):
        failures.append("INVALID_ACTIVITY_WINDOW")
    if any(previous.end_at > current.start_at for previous, current in zip(ordered, ordered[1:])):
        failures.append("ACTIVITY_OVERLAP")

    # Structural C4 temporal truth.  The scheduler already tries to construct
    # coherent anchors, but this shared gate must independently reject legacy,
    # edited, imported, or stale plans that violate them.
    try:
        locked_specs = _locked_commitment_specs(
            hard_constraints, journey_start=context.journey_start
        )
    except ValueError:
        failures.append("INVALID_LOCKED_COMMITMENT")
        locked_specs = []

    anchor_roles = {
        commitment.activity_key: role for commitment, role in locked_specs
    }
    arrival_end_min: int | None = None
    departure_start_min: int | None = None
    # An anchor is only "outside the Journey" relative to the anchor-widened
    # window, otherwise a reservation that legitimately defines the trip edges
    # (for example an 08:00 departure on a 09:00 draft) would be rejected.
    effective_span_min = _minutes_from_start(effective_end, effective_start)

    for commitment, role in locked_specs:
        boundary = str(commitment.time_boundary or "START").upper()
        if boundary == "END":
            anchor_start_min = commitment.start_min - _ANCHOR_DURATION_MIN
            anchor_end_min = commitment.start_min
        else:
            anchor_start_min = commitment.start_min
            anchor_end_min = commitment.start_min + _ANCHOR_DURATION_MIN

        anchor_start_rel = anchor_start_min - _minutes_from_start(
            effective_start, context.journey_start
        )
        anchor_end_rel = anchor_end_min - _minutes_from_start(
            effective_start, context.journey_start
        )
        if anchor_start_rel < 0 or anchor_end_rel > effective_span_min:
            failures.append(f"C4_ANCHOR_OUTSIDE_JOURNEY:{commitment.activity_key}")

        if role == "ARRIVAL":
            arrival_end_min = (
                anchor_end_min
                if arrival_end_min is None
                else max(arrival_end_min, anchor_end_min)
            )
        elif role == "DEPARTURE":
            departure_start_min = (
                anchor_start_min
                if departure_start_min is None
                else min(departure_start_min, anchor_start_min)
            )

    if (
        arrival_end_min is not None
        and departure_start_min is not None
        and arrival_end_min > departure_start_min
    ):
        failures.append("C4_ANCHOR_ARRIVAL_AFTER_DEPARTURE")

    for item in ordered:
        item_key = _location_key(item) or item.key
        # ARRIVAL and DEPARTURE are structural edge markers and therefore do
        # not constrain themselves. Every interior C4 activity, including
        # MID_TRIP and role-less booking/order locks, must still live inside
        # those structural boundaries.
        item_anchor_role = anchor_roles.get(item_key, "")
        if item_anchor_role in {"ARRIVAL", "DEPARTURE"}:
            continue
        start_min = _minutes_from_start(item.start_at, context.journey_start)
        end_min = _minutes_from_start(item.end_at, context.journey_start)
        if arrival_end_min is not None and start_min < arrival_end_min:
            failures.append(f"C4_ACTIVITY_BEFORE_ARRIVAL:{item_key}")
        if departure_start_min is not None and end_min > departure_start_min:
            failures.append(f"C4_ACTIVITY_AFTER_DEPARTURE:{item_key}")

    reference_activities = tuple(
        ReferenceActivity(
            # Prefer the semantic location identity even when a caller built
            # FeasibilityActivity with a transient display key.
            key=_location_key(item) or item.key,
            start_min=_minutes_from_start(item.start_at, context.journey_start),
            end_min=_minutes_from_start(item.end_at, context.journey_start),
            cost_minor=item.cost_minor,
            available=True,
        )
        for item in ordered
    )
    try:
        locked_commitments = _locked_commitments(
            hard_constraints, journey_start=context.journey_start
        )
    except ValueError:
        failures.append("INVALID_LOCKED_COMMITMENT")
        locked_commitments = []
    reference_result = validate_candidate(
        ReferenceCandidate(f"{context.journey_id}:feasibility", reference_activities, {}),
        journey_start_min=_minutes_from_start(effective_start, context.journey_start),
        journey_end_min=_minutes_from_start(effective_end, context.journey_start),
        budget_max_minor=budget_max,
        locked_commitments=locked_commitments,
    )
    failures.extend(reference_result.failures)

    if any(
        item.start_at < effective_start or item.end_at > effective_end for item in ordered
    ) and any(code == "JOURNEY_END_BY" for _, _, code, _ in hard_constraints):
        failures.append("JOURNEY_END_BY_EXCEEDED")
    if required_places and not required_places.issubset(
        {item.place_id for item in ordered if item.place_id is not None}
    ):
        failures.append("REQUIRED_PLACE_MISSING")

    for item in ordered:
        if not item.place_id and not item.city_service_id:
            failures.append(f"LOCATION_MISSING:{item.key}")
        if item.place_id:
            place = context.places.get(str(item.place_id))
            if place is None:
                failures.append(f"PLACE_UNAVAILABLE:{item.place_id}")
                continue
            if str(_field(place, "city_code", context.city_code)) != context.city_code:
                failures.append(f"PLACE_CITY_MISMATCH:{item.place_id}")
            if item.place_id in blocked_places:
                failures.append(f"PLACE_UNAVAILABLE:{item.place_id}")
            place_status = _field(
                place, "status", _field(place, "availability_status", "available")
            )
            place_metadata = _parse_json(_field(place, "metadata_json", {}), {})
            if str(place_status).lower() not in {"available", "open", "ready"} or (
                isinstance(place_metadata, dict) and place_metadata.get("available") is False
            ):
                failures.append(f"PLACE_UNAVAILABLE:{item.place_id}")
            opening = _parse_json(
                _field(place, "opening_hours_json", _field(place, "opening_hours", {})), {}
            )
            if not isinstance(opening, dict) or not opening:
                failures.append(f"PLACE_AVAILABILITY_UNKNOWN:{item.place_id}")
            elif not _within_open_interval(item.start_at, item.end_at, opening):
                failures.append(f"PLACE_CLOSED:{item.place_id}")
        if item.city_service_id:
            service = context.services.get(str(item.city_service_id))
            if service is None:
                failures.append(f"SERVICE_UNAVAILABLE:{item.city_service_id}")
                continue
            if str(_field(service, "city_code", context.city_code)) != context.city_code:
                failures.append(f"SERVICE_CITY_MISMATCH:{item.city_service_id}")
            if item.city_service_id in blocked_services:
                failures.append(f"SERVICE_UNAVAILABLE:{item.city_service_id}")
            availability = _parse_json(
                _field(service, "availability_json", _field(service, "availability", {})), {}
            )
            if not isinstance(availability, dict) or not availability:
                failures.append(f"SERVICE_AVAILABILITY_UNKNOWN:{item.city_service_id}")
            else:
                status = str(availability.get("status", "available")).lower()
                if status not in {"available", "open", "ready"}:
                    failures.append(f"SERVICE_UNAVAILABLE:{item.city_service_id}")
                elif not _within_open_interval(item.start_at, item.end_at, availability):
                    failures.append(f"SERVICE_CLOSED:{item.city_service_id}")

    capability_constraints = [
        (code, params)
        for _kind, _category, code, params in hard_constraints
        if code in _CAPABILITY_GATE_CODES
    ]
    # Accessibility checks are deliberately evidence-negative: a missing
    # provider field is UNKNOWN, never an implicit accessible pass.
    for code, _params in capability_constraints:
        if code not in {"STEP_FREE_REQUIRED", "AVOID_STAIRS", "STROLLER", "LARGE_LUGGAGE"}:
            continue
        for item in ordered:
            location = _location_key(item) or item.key
            record = context.places.get(str(item.place_id)) if item.place_id else None
            if record is None and item.city_service_id:
                record = context.services.get(str(item.city_service_id))
            failures.extend(_capability_failure_for_record(code, record, location))

    previous: FeasibilityActivity | None = None
    walking_facts: list[tuple[int | None, int | None, str | None]] = []
    for item in ordered:
        if previous is not None:
            walking_facts.append(_activity_walking_fact(previous, item, provider))
        previous = item
    for code, params in capability_constraints:
        if code == "MAX_WALKING_PER_LEG_M":
            try:
                limit = int(params.get("max_m"))
            except (TypeError, ValueError):
                limit = -1
            if limit < 0:
                failures.append("CAPABILITY_PARAMS_INVALID:MAX_WALKING_PER_LEG_M")
                continue
            for distance, _walking_min, pair in walking_facts:
                if distance is None:
                    failures.append(f"NEEDS_LIVE_EVIDENCE:MAX_WALKING_PER_LEG_M:{pair or 'unknown'}")
                elif distance > limit:
                    failures.append(f"MAX_WALKING_PER_LEG_EXCEEDED:{pair or 'unknown'}")
        elif code == "MAX_CONTINUOUS_WALKING_MIN":
            try:
                limit = int(params.get("max_min"))
            except (TypeError, ValueError):
                limit = -1
            if limit < 0:
                failures.append("CAPABILITY_PARAMS_INVALID:MAX_CONTINUOUS_WALKING_MIN")
                continue
            for _distance, walking_min, pair in walking_facts:
                if walking_min is None:
                    failures.append(
                        f"NEEDS_LIVE_EVIDENCE:MAX_CONTINUOUS_WALKING_MIN:{pair or 'unknown'}"
                    )
                elif walking_min > limit:
                    failures.append(f"MAX_CONTINUOUS_WALKING_EXCEEDED:{pair or 'unknown'}")
        elif code == "MAX_DAILY_WALKING_M":
            try:
                limit = int(params.get("max_m"))
            except (TypeError, ValueError):
                limit = -1
            if limit < 0:
                failures.append("CAPABILITY_PARAMS_INVALID:MAX_DAILY_WALKING_M")
                continue
            distances = [distance for distance, _walking_min, _pair in walking_facts]
            if any(value is None for value in distances):
                failures.append("NEEDS_LIVE_EVIDENCE:MAX_DAILY_WALKING_M")
            elif sum(int(value) for value in distances) > limit:
                failures.append("MAX_DAILY_WALKING_EXCEEDED")
        elif code in {"MIDDAY_REST_WINDOW", "INFANT_NAP_WINDOW"}:
            window = _window_from_params(params)
            if window is None:
                failures.append(f"CAPABILITY_PARAMS_INVALID:{code}")
                continue
            start_min, end_min = window
            has_gap = any(
                gap_start.hour * 60 + gap_start.minute <= start_min
                and gap_end.hour * 60 + gap_end.minute >= end_min
                for gap_start, gap_end in _schedule_gaps(ordered)
            )
            if not has_gap:
                failures.append(f"REST_WINDOW_UNSATISFIED:{code}")
        elif code in {"REST_EVERY_MIN", "TOILET_BREAK_EVERY_MIN"}:
            try:
                interval_min = int(params.get("interval_min", params.get("max_min")))
                min_break_min = int(params.get("min_break_min", 1))
            except (TypeError, ValueError):
                interval_min = -1
                min_break_min = 1
            if interval_min < 0 or min_break_min < 0:
                failures.append(f"CAPABILITY_PARAMS_INVALID:{code}")
            elif any(span > interval_min for span in _continuous_spans(ordered, min_break_min=min_break_min)):
                failures.append(f"BREAK_INTERVAL_EXCEEDED:{code}")

    if provider_key == "mock-mobility" or provider_is_frozen:
        for previous, current in zip(ordered, ordered[1:]):
            origin = _location_key(previous)
            destination = _location_key(current)
            if not origin or not destination:
                failures.append("MOBILITY_CONTEXT_MISSING")
                continue
            try:
                travel_minutes = int(provider.estimate_minutes(origin, destination))
            except (KeyError, TypeError, ValueError):
                failures.append(f"MOBILITY_DATA_UNAVAILABLE:{origin}|{destination}")
                continue
            if travel_minutes < 0:
                failures.append(f"MOBILITY_DATA_INVALID:{origin}|{destination}")
            elif previous.end_at + timedelta(minutes=travel_minutes) > current.start_at:
                failures.append(f"INSUFFICIENT_TRAVEL_GAP:{origin}|{destination}")

    return FeasibilityResult(
        passed=not failures,
        failures=tuple(sorted(set(failures))),
        effective_start_at=effective_start,
        effective_end_at=effective_end,
        budget_max_minor=budget_max,
    )


def check_records(
    journey: Any,
    constraints: Iterable[Any],
    places: Mapping[str, Any] | Iterable[Any],
    services: Mapping[str, Any] | Iterable[Any],
    activities: Sequence[Any],
    *,
    mobility_provider: Any | None = None,
) -> FeasibilityResult:
    """Convenience adapter for route handlers and future WP06 callers."""

    context = build_context(
        journey,
        constraints,
        places,
        services,
        mobility_provider=mobility_provider,
    )
    return evaluate_feasibility(context, [activity_from_record(item) for item in activities])
