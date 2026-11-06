"""Anchor-aware template scheduling.

A confirmed ``C4_Anchored`` commitment is a *requirement to build around*, not
only a veto.  This module resolves each active anchor to a concrete, routable
``Place`` identity and pins it into a candidate schedule before the deterministic
gate runs.  Optional POIs are re-timed around the anchor and dropped rather than
allowed to breach the journey window.

The shape mirrors ``app.planning.authoritative_v12``: ARRIVAL / DEPARTURE use the
structural node keys ``arrival-anchor`` / ``departure-anchor`` and the concrete
Place id ``v12:{journey_id}:{key}``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Mapping, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import CityService, Commitment, Journey, Place
from app.planning.anchors import (
    ARRIVAL_ROLE,
    DEPARTURE_ROLE,
    MID_TRIP_ROLE,
    STRUCTURAL_ANCHOR_KEYS,
    normalise_role,
    structural_external_id,
    structural_place_id,
)

ANCHOR_PROVIDER_KEY = "citynexus-v12-authoritative"
# A structural anchor occupies a bounded slot so a later leg cannot overlap it.
ANCHOR_DURATION_MIN = 20
# Deterministic fixture default when a pair is not frozen yet.
DEFAULT_TRAVEL_MIN = 20


@dataclass(frozen=True)
class CandidateActivity:
    """One scheduled candidate activity (gate + persistence share this shape)."""

    place_id: str
    title: str
    start_at: datetime
    end_at: datetime
    cost_minor: int = 0
    locked: bool = False
    anchor_role: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ResolvedAnchor:
    """A C4 anchor resolved to a concrete Place identity."""

    role: str
    semantic_key: str
    place_id: str
    place: Place
    scheduled_at: datetime
    time_boundary: str


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _load(value: Any, default: Any) -> Any:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str) and value:
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return default
        return parsed if isinstance(parsed, type(default)) else default
    return default


def active_c4_commitments(session: Session, journey: Journey) -> list[Commitment]:
    return list(
        session.scalars(
            select(Commitment).where(
                Commitment.journey_id == journey.id,
                Commitment.status == "ACTIVE",
                Commitment.level == "C4_Anchored",
            )
        ).all()
    )


def _city_center(session: Session, journey: Journey) -> tuple[float, float]:
    first = session.scalar(
        select(Place).where(Place.city_code == journey.city_code).limit(1)
    )
    if first is not None:
        return float(first.latitude), float(first.longitude)
    return 0.0, 0.0


def _structural_place(session: Session, journey: Journey, role: str) -> Place:
    """Materialise (or reuse) the structural anchor Place, matching V12's identity."""

    external_id = structural_external_id(journey.id, role)
    row = session.scalar(
        select(Place).where(
            Place.provider_key == ANCHOR_PROVIDER_KEY,
            Place.external_id == external_id,
        )
    )
    if row is not None:
        return row
    place_id = structural_place_id(journey.id, role)
    row = session.get(Place, place_id)
    if row is not None:
        return row
    latitude, longitude = _city_center(session, journey)
    offset = -0.015 if role == ARRIVAL_ROLE else 0.06
    offset_lon = -0.02 if role == ARRIVAL_ROLE else 0.08
    row = Place(
        id=place_id,
        provider_key=ANCHOR_PROVIDER_KEY,
        external_id=external_id,
        city_code=journey.city_code,
        name="Arrival" if role == ARRIVAL_ROLE else "Departure",
        category=role.lower(),
        latitude=latitude + offset,
        longitude=longitude + offset_lon,
        opening_hours_json=_json({"status": "NORMALIZED", "always_available": True}),
        tags_json=_json(["v12-authoritative", role.lower()]),
        metadata_json=_json({"anchor": role, "available": True, "cost_minor": 0}),
        updated_at=datetime.now().astimezone().isoformat(),
    )
    session.add(row)
    session.flush()
    return row


def resolve_plan_anchors(
    session: Session, journey: Journey
) -> tuple[list[ResolvedAnchor], list[str]]:
    """Resolve active C4 anchors; returns (resolved, unresolved_descriptions)."""

    resolved: list[ResolvedAnchor] = []
    unresolved: list[str] = []
    for commitment in active_c4_commitments(session, journey):
        policy = _load(commitment.policy_json, {})
        role = normalise_role(policy.get("anchor_role"))
        if not role:
            # A booking/order C4 anchor binds a concrete Activity, not a
            # planner node; it is protected through activity_id instead.
            continue
        raw_key = str(policy.get("semantic_key") or "").strip()
        scheduled = str(policy.get("scheduled_start_at") or "").strip()
        if not scheduled:
            unresolved.append(f"{role}:NO_SCHEDULED_START")
            continue
        try:
            scheduled_at = datetime.fromisoformat(scheduled)
        except ValueError:
            unresolved.append(f"{role}:INVALID_SCHEDULED_START")
            continue
        if role in (ARRIVAL_ROLE, DEPARTURE_ROLE):
            place = _structural_place(session, journey, role)
            resolved.append(
                ResolvedAnchor(
                    role=role,
                    semantic_key=structural_place_id(journey.id, role),
                    place_id=place.id,
                    place=place,
                    scheduled_at=scheduled_at,
                    time_boundary="END" if role == DEPARTURE_ROLE else "START",
                )
            )
            continue
        place = session.get(Place, raw_key)
        if place is None:
            service = session.get(CityService, raw_key)
            if service is not None and service.place_id:
                place = session.get(Place, service.place_id)
        if place is None or place.city_code != journey.city_code:
            unresolved.append(f"{role}:{raw_key}")
            continue
        resolved.append(
            ResolvedAnchor(
                role=MID_TRIP_ROLE,
                semantic_key=raw_key,
                place_id=place.id,
                place=place,
                scheduled_at=scheduled_at,
                time_boundary="START",
            )
        )
    return resolved, unresolved


def _place_cost(place: Place) -> int:
    metadata = _load(place.metadata_json, {})
    try:
        return int(metadata.get("cost_minor", 0))
    except (TypeError, ValueError):
        return 0


def _place_duration(place: Place) -> int:
    metadata = _load(place.metadata_json, {})
    try:
        return max(15, int(metadata.get("duration_min", 60)))
    except (TypeError, ValueError):
        return 60


def _template_start(journey_start: datetime, text: str) -> datetime:
    hour, minute = (int(part) for part in str(text).split(":", 1))
    return journey_start.replace(hour=hour, minute=minute, second=0, microsecond=0)


def _estimate_gap_minutes(
    travel_minutes: Mapping[tuple[str, str], int] | None,
    origin: str | None,
    destination: str | None,
) -> int:
    """Frozen travel time for one leg, or the deterministic fixture default."""

    if not origin or not destination:
        return 0
    if travel_minutes is not None:
        value = travel_minutes.get((origin, destination))
        if value is not None:
            return int(value)
    return DEFAULT_TRAVEL_MIN


def _anchor_windows(
    anchors: Sequence[ResolvedAnchor],
) -> list[tuple[ResolvedAnchor, datetime, datetime]]:
    """The exact time span each resolved anchor occupies."""

    windows = []
    for anchor in anchors:
        duration = (
            ANCHOR_DURATION_MIN
            if anchor.role != MID_TRIP_ROLE
            else max(ANCHOR_DURATION_MIN, _place_duration(anchor.place))
        )
        start_at, end_at = anchor_window(anchor.role, anchor.scheduled_at, duration)
        windows.append((anchor, start_at, end_at))
    return windows


def validate_anchor_timeline(
    anchors: Sequence[ResolvedAnchor],
    journey_start: datetime,
    journey_end: datetime,
) -> str | None:
    """Return a precise conflict reason, or ``None`` when the anchors are usable.

    A confirmed ARRIVAL is a **lower** boundary for city activity and a
    confirmed DEPARTURE is an **upper** boundary.  The anchors must also be
    mutually consistent: they may not overlap, they may not sit outside the
    (anchor-widened) journey window, and the usable interval between arrival and
    departure must be non-empty.
    """

    if not anchors:
        return None
    windows = sorted(_anchor_windows(anchors), key=lambda item: (item[1], item[2]))
    for anchor, start_at, end_at in windows:
        if start_at < journey_start or end_at > journey_end:
            return (
                f"C4_ANCHOR_OUTSIDE_JOURNEY:{anchor.semantic_key}"
                f"@{start_at.isoformat()}"
            )
    for (first, _fs, first_end), (second, second_start, _se) in zip(windows, windows[1:]):
        if second_start < first_end:
            return (
                f"C4_ANCHOR_OVERLAP:{first.semantic_key}|{second.semantic_key}"
            )
    arrivals = [item for item in windows if item[0].role == ARRIVAL_ROLE]
    departures = [item for item in windows if item[0].role == DEPARTURE_ROLE]
    usable_start = max((item[2] for item in arrivals), default=journey_start)
    usable_end = min((item[1] for item in departures), default=journey_end)
    if usable_start > usable_end:
        arrival_key = arrivals[0][0].semantic_key if arrivals else "-"
        departure_key = departures[0][0].semantic_key if departures else "-"
        return (
            f"C4_ANCHOR_ARRIVAL_AFTER_DEPARTURE:{arrival_key}"
            f"@{usable_start.isoformat()}|{departure_key}@{usable_end.isoformat()}"
        )
    return None


def usable_city_interval(
    anchors: Sequence[ResolvedAnchor],
    journey_start: datetime,
    journey_end: datetime,
) -> tuple[datetime, datetime]:
    """The interval flexible city activity may occupy.

    Arrival completes the lower boundary; departure must be reached before the
    upper boundary.  With no anchors this is simply the journey window.
    """

    windows = _anchor_windows(anchors)
    arrivals = [item for item in windows if item[0].role == ARRIVAL_ROLE]
    departures = [item for item in windows if item[0].role == DEPARTURE_ROLE]
    usable_start = max((item[2] for item in arrivals), default=journey_start)
    usable_end = min((item[1] for item in departures), default=journey_end)
    return usable_start, usable_end


def _anchor_activity(anchor: ResolvedAnchor, start_at: datetime, end_at: datetime) -> CandidateActivity:
    return CandidateActivity(
        place_id=anchor.place_id,
        title=anchor.place.name,
        start_at=start_at,
        end_at=end_at,
        cost_minor=_place_cost(anchor.place),
        locked=True,
        anchor_role=anchor.role,
        metadata={
            "semantic_key": anchor.semantic_key,
            "anchor_role": anchor.role,
            "commitment_anchor": True,
        },
    )


def _optional_activities(
    template: Sequence[tuple[str, str, str, str]],
    places: Mapping[str, Place],
    anchors: Sequence[ResolvedAnchor],
    journey_start: datetime,
) -> list[CandidateActivity]:
    anchor_places = {anchor.place_id for anchor in anchors}
    optional: list[CandidateActivity] = []
    for place_id, title, start_text, end_text in template:
        if place_id not in places or place_id in anchor_places:
            continue
        place = places[place_id]
        optional.append(
            CandidateActivity(
                place_id=place_id,
                title=title,
                start_at=_template_start(journey_start, start_text),
                end_at=_template_start(journey_start, end_text),
                cost_minor=_place_cost(place),
                locked=place_id == "place-river-cruise",
            )
        )
    return optional


def schedule_template_with_anchors(
    *,
    journey: Journey,
    template: Sequence[tuple[str, str, str, str]],
    places: Mapping[str, Place],
    anchors: Sequence[ResolvedAnchor],
    journey_start: datetime,
    journey_end: datetime,
    travel_minutes: Mapping[tuple[str, str], int] | None = None,
) -> list[CandidateActivity] | None:
    """Build one candidate schedule that respects the C4 temporal boundaries.

    ``ARRIVAL`` is a lower boundary and ``DEPARTURE`` an upper boundary for
    flexible city activity; ``MID_TRIP`` is an interior pinned commitment.
    Flexible POIs are packed into the gaps between anchors with real travel
    time, and are dropped when they do not fit rather than producing a plan
    that is temporally impossible.  Returns ``None`` when the anchors
    themselves are inconsistent.
    """

    if not anchors:
        schedule: list[CandidateActivity] = []
        for place_id, title, start_text, end_text in template:
            if place_id not in places:
                return None
            place = places[place_id]
            schedule.append(
                CandidateActivity(
                    place_id=place_id,
                    title=title,
                    start_at=_template_start(journey_start, start_text),
                    end_at=_template_start(journey_start, end_text),
                    cost_minor=_place_cost(place),
                    locked=place_id == "place-river-cruise",
                )
            )
        return schedule

    if validate_anchor_timeline(anchors, journey_start, journey_end) is not None:
        return None

    ordered_anchors = sorted(
        _anchor_windows(anchors), key=lambda item: (item[1], item[2])
    )
    usable_start, usable_end = usable_city_interval(
        anchors, journey_start, journey_end
    )
    if usable_start > usable_end:
        return None

    pending = _optional_activities(template, places, anchors, journey_start)
    schedule: list[CandidateActivity] = []
    cursor = usable_start
    previous_place: str | None = None
    next_index = 0

    while next_index < len(ordered_anchors):
        anchor, anchor_start, anchor_end = ordered_anchors[next_index]
        # Pack flexible POIs into the gap that ends at this anchor.
        while pending:
            candidate = pending[0]
            duration = candidate.end_at - candidate.start_at
            travel_in = _estimate_gap_minutes(
                travel_minutes, previous_place, candidate.place_id
            )
            start_at = max(
                candidate.start_at, cursor + timedelta(minutes=travel_in)
            )
            end_at = start_at + duration
            travel_out = _estimate_gap_minutes(
                travel_minutes, candidate.place_id, anchor.place_id
            )
            if end_at + timedelta(minutes=travel_out) > anchor_start:
                break
            schedule.append(
                CandidateActivity(
                    place_id=candidate.place_id,
                    title=candidate.title,
                    start_at=start_at,
                    end_at=end_at,
                    cost_minor=candidate.cost_minor,
                    locked=candidate.locked,
                    metadata=candidate.metadata,
                )
            )
            cursor = end_at
            previous_place = candidate.place_id
            pending.pop(0)
        # The anchor itself must still be reachable from the last POI.
        if previous_place is not None:
            needed = _estimate_gap_minutes(
                travel_minutes, previous_place, anchor.place_id
            )
            if cursor + timedelta(minutes=needed) > anchor_start:
                return None
        schedule.append(_anchor_activity(anchor, anchor_start, anchor_end))
        cursor = max(cursor, anchor_end)
        previous_place = anchor.place_id
        next_index += 1

    # Remaining POIs may only run up to the usable end (the DEPARTURE boundary).
    while pending:
        candidate = pending[0]
        duration = candidate.end_at - candidate.start_at
        travel_in = _estimate_gap_minutes(
            travel_minutes, previous_place, candidate.place_id
        )
        start_at = max(candidate.start_at, cursor + timedelta(minutes=travel_in))
        end_at = start_at + duration
        if end_at > usable_end:
            break
        schedule.append(
            CandidateActivity(
                place_id=candidate.place_id,
                title=candidate.title,
                start_at=start_at,
                end_at=end_at,
                cost_minor=candidate.cost_minor,
                locked=candidate.locked,
                metadata=candidate.metadata,
            )
        )
        cursor = end_at
        previous_place = candidate.place_id
        pending.pop(0)

    for item in schedule:
        if item.start_at < journey_start or item.end_at > journey_end:
            return None
        if item.anchor_role in (None, MID_TRIP_ROLE):
            if item.start_at < usable_start or item.end_at > usable_end:
                return None
    return schedule


def anchor_assignment_for_activities(
    activities: Sequence[Any],
    anchors: Sequence[ResolvedAnchor],
) -> dict[str, str]:
    """Map commitment semantic keys to the Activity ids that satisfy them."""

    assigned: dict[str, str] = {}
    for anchor in anchors:
        match = next(
            (
                item
                for item in activities
                if str(getattr(item, "place_id", None) or "") == anchor.place_id
            ),
            None,
        )
        if match is not None:
            assigned[anchor.semantic_key] = match.id
    return assigned


def effective_journey_window(
    journey: Journey,
    anchors: Sequence[ResolvedAnchor],
) -> tuple[str, str]:
    """Widen the draft journey window to contain every confirmed C4 anchor.

    A confirmed reservation is an authoritative real-world fact.  A draft
    journey window ("09:00-19:00") must never make a booked 08:00 departure
    unplannable; the window expands to contain the commitment instead.  The same
    helper is used by generation *and* revalidation so the two never disagree.
    """

    start = datetime.fromisoformat(str(journey.start_at))
    end = datetime.fromisoformat(str(journey.end_at))
    for anchor in anchors:
        duration = (
            ANCHOR_DURATION_MIN
            if anchor.role != MID_TRIP_ROLE
            else max(ANCHOR_DURATION_MIN, _place_duration(anchor.place))
        )
        candidate_start, candidate_end = anchor_window(
            anchor.role, anchor.scheduled_at, duration
        )
        if candidate_start < start:
            start = candidate_start
        if candidate_end > end:
            end = candidate_end
    return start.isoformat(), end.isoformat()


def anchor_window(
    role: str,
    scheduled_at: datetime,
    duration_min: int = 20,
) -> tuple[datetime, datetime]:
    """The real-world time span a C4 anchor occupies.

    A DEPARTURE anchor constrains the END of its activity, so the span starts
    ``duration_min`` before the booked departure.  Everything else is anchored
    on its START.
    """

    delta = timedelta(minutes=max(1, int(duration_min)))
    if normalise_role(role) == DEPARTURE_ROLE:
        return scheduled_at - delta, scheduled_at
    return scheduled_at, scheduled_at + delta
