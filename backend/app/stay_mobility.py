"""Generic stay-aware, mode-per-leg planning backed by normalized provider evidence."""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Protocol
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.money import major_to_minor
from app.core.phase_timing import phase
from app.anywhere_capability import (
    CAPABILITY_STATES,
    REASON_COVERAGE_INCOMPLETE,
    REASON_NO_CANONICAL_IDENTITY,
    READY,
)
from app.core.temporal import (
    MAX_JOURNEY_DAYS,
    MIN_JOURNEY_DAYS,
    WINDOW_IN,
    WINDOW_INVALID,
    WINDOW_OUT,
    WINDOW_UNCOMPARABLE,
    JourneyTemporalShape,
    calendar_day_night_shape,
    derive_budget_reference,
    derive_journey_temporal_shape,
)
from app.db.models import (
    AccommodationOfferSnapshot,
    Activity,
    Commitment,
    Event,
    Journey,
    JourneyLeg,
    JourneyOperationsContext,
    JourneyTripContext,
    Place,
    LocationObservation,
    MobilityFact,
    PlanVersion,
    StayJourneyOption,
)
from app.execution.traveler_twin import (
    capability_payload,
    planning_preference_items,
    refresh_snapshot,
    snapshot_payload,
)
from app.planning.stay_mode_solver import solve_mode_choices

MODES = ("WALK", "BICYCLE", "TRANSIT", "AUTO", "RIDE_HAIL")
DATA_PATH = Path(__file__).resolve().parents[2] / "data" / "v9" / "city_operations.json"
CAPTURED_FIXTURE_CURRENCY = "CNY"
SYNTHETIC_ESTIMATE_MAJOR_BY_CURRENCY = {
    "CNY": {"BICYCLE": 6, "TRANSIT": 4, "AUTO": 12},
}


@dataclass(frozen=True)
class AccommodationOfferDraft:
    external_offer_id: str
    property_name: str
    latitude: float
    longitude: float
    nightly_amount_minor: int
    room_type: str
    refundable: bool
    evidence_confidence: float
    coordinate_crs: str
    luggage_drop: dict[str, Any]


@dataclass(frozen=True)
class GraphNode:
    key: str
    title: str
    role: str
    start_at: str
    end_at: str
    latitude: float
    longitude: float
    place_key: str | None = None
    locked: bool = False
    estimated_cost_minor: int = 0
    cost_status: str = "FREE_KNOWN"


class AccommodationProvider(Protocol):
    provider_key: str
    provenance: str

    def discover(
        self, *, city_code: str, party_size: int, nights: int
    ) -> tuple[AccommodationOfferDraft, ...]: ...


from app.integrations.stay_osm import OsmAccommodationProvider


class CapturedAccommodationProvider:
    provider_key = "citynexus-city-fixture"
    provenance = "CAPTURED_REPLAY"

    def discover(
        self, *, city_code: str, party_size: int, nights: int
    ) -> tuple[AccommodationOfferDraft, ...]:
        city = city_fixture(city_code)
        return tuple(
            AccommodationOfferDraft(
                external_offer_id=str(item["external_offer_id"]),
                property_name=str(item["property_name"]),
                latitude=float(item["latitude"]),
                longitude=float(item["longitude"]),
                nightly_amount_minor=int(item["nightly_amount_minor"]),
                room_type=str(item["room_type"]),
                refundable=bool(item["refundable"]),
                evidence_confidence=0.96,
                coordinate_crs=str(city.get("center", {}).get("crs", "WGS84")),
                luggage_drop=dict(item.get("luggage_drop", {})),
            )
            for item in city.get("stays", [])
        )


class LiveAccommodationProvider:
    provider_key = "live-accommodation"
    provenance = "UNAVAILABLE"

    def discover(
        self, *, city_code: str, party_size: int, nights: int
    ) -> tuple[AccommodationOfferDraft, ...]:
        return ()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _load(raw: str | None, fallback: Any) -> Any:
    try:
        return json.loads(raw or "")
    except (TypeError, json.JSONDecodeError):
        return fallback


def city_fixture(city_code: str) -> dict[str, Any]:
    fixtures = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    value = fixtures.get(str(city_code).upper())
    return dict(value) if isinstance(value, dict) else {}


def _context(session: Session, journey: Journey) -> JourneyTripContext:
    row = session.get(JourneyTripContext, journey.id)
    if row is None:
        now = _now()
        row = JourneyTripContext(
            journey_id=journey.id,
            arrival_anchor_json=_json({"at": journey.start_at, "location": "Arrival"}),
            departure_anchor_json=_json({"at": journey.end_at, "location": "Departure"}),
            party_size=1,
            occupancy_json=_json({"adults": 1}),
            stay_budget_amount_minor=0,
            stay_budget_currency=journey.budget_currency,
            # This row was not created by a traveller answering a question, so it declares
            # nothing either way: NULL is "never asked", which is what actually happened here.
            stay_budget_declared=None,
            created_at=now,
            updated_at=now,
        )
        session.add(row)
        session.flush()
    return row


def stay_budget_body(context: JourneyTripContext) -> dict[str, Any]:
    """The trip's lodging budget in the exact shape that leaves this process.

    ORD-0355: a traveller who never typed a lodging budget used to have `amount_minor: 0`
    carried outward anyway, and a supplier that filters on that number answers "no
    availability" - which then reads back as if the traveller's own money was the reason.
    Absence now travels as null with `declared: false` beside it, because null is the
    traveller's silence and 0 is a price.  A row whose state is NULL is *unknown*, not
    undeclared, and keeps sending its stored amount: that amount may be one a traveller
    genuinely typed before this column existed.
    """

    declared = context.stay_budget_declared
    return {
        "declared": declared,
        "amount_minor": (
            None if declared is False else context.stay_budget_amount_minor
        ),
        "currency": context.stay_budget_currency,
    }


def _operations_context(session: Session, journey: Journey) -> JourneyOperationsContext:
    row = session.get(JourneyOperationsContext, journey.id)
    if row is None:
        now = _now()
        row = JourneyOperationsContext(
            journey_id=journey.id,
            baggage_present=False,
            baggage_size_class="NONE",
            luggage_drop_required=False,
            constraints_json="{}",
            created_at=now,
            updated_at=now,
        )
        session.add(row)
        session.flush()
    return row


def _anchor(context: JourneyTripContext, journey: Journey, kind: str) -> dict[str, Any]:
    raw = context.arrival_anchor_json if kind == "arrival" else context.departure_anchor_json
    fallback_at = journey.start_at if kind == "arrival" else journey.end_at
    value = _load(raw, {})
    return {
        "at": str(value.get("at") or fallback_at),
        "location": str(value.get("location") or kind.title()),
        "latitude": value.get("latitude"),
        "longitude": value.get("longitude"),
        "reservation_id": value.get("reservation_id"),
    }


def journey_boundaries(
    context: JourneyTripContext, arrival_fallback: str, departure_fallback: str
) -> tuple[datetime, datetime]:
    """The two instants that bound a journey: an anchor's own moment wins over the window.

    This is the same rule `_anchor` applies, restated so a caller that has not written the
    row yet (or is about to change it) can measure the identical window without mutating
    anything first.  `test_qoder_journey_window_boundary` pins the two against each other
    across anchor-present and anchor-absent journeys, because a silent disagreement here is
    exactly the shape that let a nine-day trip through one door and 500 at the other.
    """

    arrival = _load(context.arrival_anchor_json, {}).get("at") or arrival_fallback
    departure = _load(context.departure_anchor_json, {}).get("at") or departure_fallback
    return datetime.fromisoformat(str(arrival)), datetime.fromisoformat(str(departure))


def journey_temporal_shape(
    context: JourneyTripContext, journey: Journey
) -> JourneyTemporalShape:
    """The one way this product decides which two instants bound a journey.

    An anchor's own moment wins over the journey window (`_anchor`), and both the day/night
    count used for pricing and the ceiling checked at the doors come from here - otherwise a
    create endpoint could validate one window while stay generation priced another, which is
    the disagreement ORD-0033 turned into a user-visible 500.
    """

    start, end = journey_boundaries(context, journey.start_at, journey.end_at)
    return derive_journey_temporal_shape(start, end)


def _nights(context: JourneyTripContext, journey: Journey) -> int:
    return journey_temporal_shape(context, journey).night_count


def journey_projection_window(
    context: JourneyTripContext, journey: Journey
) -> tuple[JourneyTemporalShape | None, str]:
    """The trip shape as a *read* projection has to report it: same arithmetic, judged not enforced.

    `journey_temporal_shape` is the write path's gate and it raises.  A `GET` cannot answer
    500 because a row stored before ORD-0033 is nine days long - the traveller would get no
    screen at all - so this reuses the identical boundaries and the identical formula, and
    reports the window judgement as a field instead of as an exception.  A count is only ever
    dropped to `None` when the window itself cannot be ordered, because a fabricated "1 天"
    is the exact class of defect this file exists to stop.
    """

    start, end = journey_boundaries(context, journey.start_at, journey.end_at)
    try:
        return derive_journey_temporal_shape(start, end), WINDOW_IN
    except ValueError as exc:
        code, _, _ = str(exc).partition(":")
        if code == "V13_JOURNEY_DAYS_OUT_OF_BOUNDS":
            return calendar_day_night_shape(start, end), WINDOW_OUT
        return None, WINDOW_INVALID
    except TypeError:
        # Naive against offset-aware: no day count is knowable, and that is not the same
        # statement as "departure before arrival".
        return None, WINDOW_UNCOMPARABLE


def _at_on_day(
    anchor_at: datetime, day_offset: int, hhmm: str, timezone_name: str | None = None
) -> datetime:
    """A wall-clock time on a given day, in the *journey's* local timezone.

    Hotel check-in / check-out are local wall-clock times.  Using the anchor's
    own tzinfo was wrong when the anchor is stored in UTC: a 14:00 check-in
    became 22:00 local, and a 12:00 check-out landed exactly on the departure
    anchor, making every hotel look like a pinned conflict.
    """

    hour, minute = (int(part) for part in hhmm.split(":"))
    zone = None
    if timezone_name:
        try:
            from zoneinfo import ZoneInfo

            zone = ZoneInfo(timezone_name)
        except Exception:
            zone = None
    reference = anchor_at.astimezone(zone) if zone is not None else anchor_at
    local_date = reference.date() + timedelta(days=day_offset)
    # Build the wall-clock time in the journey's zone, then express it in the
    # SAME offset the journey anchors use.  Mixing a +08:00 hotel time with a
    # UTC-stored itinerary made every comparison look like a conflict even
    # though both described the same instant.
    local = datetime.combine(local_date, time(hour, minute), tzinfo=zone or anchor_at.tzinfo)
    return local.astimezone(anchor_at.tzinfo)


def capture_accommodation_offers(
    session: Session,
    journey: Journey,
    *,
    attempts_out: list[dict[str, Any]] | None = None,
) -> list[AccommodationOfferSnapshot]:
    """Snapshot the properties available to this journey.

    `attempts_out` exists because an empty return value is ambiguous by construction: zero
    nights, an unreachable mirror, and a city with no mapped hotels all look identical to
    the caller and to the traveller. Anything this function learns about *why* it found what
    it found is appended there, so the response can say so. It is a parameter rather than a
    function attribute because the API serves requests in worker threads, and a shared
    attribute would let one traveller read another's outcome.
    """

    notes = attempts_out if attempts_out is not None else []
    context = _context(session, journey)
    arrival = datetime.fromisoformat(_anchor(context, journey, "arrival")["at"])
    departure = datetime.fromisoformat(_anchor(context, journey, "departure")["at"])
    nights = _nights(context, journey)
    if nights == 0:
        notes.append({"step": "nights", "target": "journey", "outcome": "zero_nights"})
        return []
    captured = _now()
    expires = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
    # A sealed fixture only exists for Tianjin.  For any other city we ask a
    # real provider, otherwise a Xiamen trip could never compare stays at all.
    fixture = city_fixture(journey.city_code)
    if fixture.get("stays"):
        provider: AccommodationProvider = CapturedAccommodationProvider()
    else:
        provider = OsmAccommodationProvider()
    result: list[AccommodationOfferSnapshot] = []
    try:
        drafts = provider.discover(
            city_code=journey.city_code, party_size=context.party_size, nights=nights
        )
    finally:
        close = getattr(provider, "close", None)
        if callable(close):
            close()
    notes.append(
        {
            "step": "provider",
            "target": provider.provider_key,
            "outcome": "ok" if drafts else "empty",
            "detail": f"fixture={'yes' if fixture.get('stays') else 'no'} drafts={len(drafts)}",
        }
    )
    # The provider knows which mirrors it tried and how each ended; this is the only place
    # that knowledge can still reach a response.
    notes.extend(dict(item) for item in getattr(provider, "attempts", []) or [])
    for draft in drafts:
        existing = session.scalar(
            select(AccommodationOfferSnapshot).where(
                AccommodationOfferSnapshot.journey_id == journey.id,
                AccommodationOfferSnapshot.provider_key == provider.provider_key,
                AccommodationOfferSnapshot.external_offer_id == draft.external_offer_id,
            )
        )
        total = draft.nightly_amount_minor * nights
        check_in = _at_on_day(arrival, 0, "14:00", journey.timezone)
        check_out = _at_on_day(arrival, nights, "12:00", journey.timezone)
        if check_out > departure:
            check_out = departure - timedelta(hours=2)
        if existing is None:
            existing = AccommodationOfferSnapshot(
                id=str(uuid4()),
                journey_id=journey.id,
                provider_key=provider.provider_key,
                external_offer_id=draft.external_offer_id,
                property_name=draft.property_name,
                latitude=draft.latitude,
                longitude=draft.longitude,
                coordinate_crs=draft.coordinate_crs,
                room_type=draft.room_type,
                occupancy_json=_json(
                    {"party_size": context.party_size, **_load(context.occupancy_json, {})}
                ),
                check_in_at=check_in.isoformat(),
                check_out_at=check_out.isoformat(),
                nightly_amount_minor=draft.nightly_amount_minor,
                taxes_amount_minor=0,
                total_amount_minor=total,
                # The current accommodation fixture is intentionally CNY-only;
                # a future non-CNY fixture must supply its own captured currency.
                currency=CAPTURED_FIXTURE_CURRENCY,
                cancellation_policy_json=_json(
                    {
                        "refundable": draft.refundable,
                        "deadline": arrival.isoformat(),
                        "summary": "Free cancellation before arrival"
                        if draft.refundable
                        else "Non-refundable captured rate",
                    }
                ),
                luggage_drop_json=_json(draft.luggage_drop),
                refundable=draft.refundable,
                evidence_json=_json(
                    {
                        "source": provider.provenance,
                        "provider": provider.provider_key,
                        "confidence": draft.evidence_confidence,
                        "price_basis": "per_night × nights",
                        "currency_explicit": True,
                        "city_fixture": journey.city_code,
                    }
                ),
                captured_at=captured,
                expires_at=expires,
                status="CAPTURED",
            )
            session.add(existing)
        result.append(existing)
    session.flush()
    return result


def _carried_itinerary_nodes(
    session: Session, journey: Journey, *, exclude_keys: set[str]
) -> list[GraphNode]:
    """The traveller's already-chosen stops, as stay-graph nodes.

    A stay selection must *augment* the selected itinerary.  Rebuilding the plan
    from ``city_fixture()`` alone silently deleted every real-city POI the
    traveller had picked (truth blocker T1).  We therefore carry the current
    selected/active plan's own activities forward.

    Only genuinely schedulable stops are carried: the arrival/departure anchors
    are re-created by the stay graph itself, and anything already superseded is
    skipped.
    """

    plan_id = journey.active_plan_version_id
    if not plan_id:
        return []
    rows = session.scalars(
        select(Activity).where(Activity.plan_version_id == plan_id).order_by(Activity.sequence_no)
    ).all()
    if not rows:
        return []

    # Coordinates live on the Place row, not on the Activity.
    place_ids = {str(item.place_id) for item in rows if item.place_id}
    places: dict[str, Place] = {}
    if place_ids:
        places = {
            place.id: place
            for place in session.scalars(select(Place).where(Place.id.in_(place_ids))).all()
        }

    carried: list[GraphNode] = []
    for item in rows:
        metadata = _load(item.metadata_json, {}) or {}
        semantic = str(metadata.get("semantic_key") or "").strip()
        role = str(metadata.get("role") or "").upper()
        # Anchors and the stay materialisation are rebuilt by this graph.
        if role in {"ARRIVAL", "DEPARTURE"} or item.activity_type in {
            "arrival",
            "departure",
            "check_in",
            "check_out",
            "stay",
        }:
            continue
        key = semantic or str(item.place_id or item.id)
        if key in exclude_keys:
            continue
        place = places.get(str(item.place_id)) if item.place_id else None
        lat = getattr(place, "latitude", None) if place else None
        lon = getattr(place, "longitude", None) if place else None
        if lat is None or lon is None:
            continue
        carried.append(
            GraphNode(
                key=key,
                title=item.title,
                role="ACTIVITY",
                start_at=item.start_at,
                end_at=item.end_at,
                latitude=float(lat),
                longitude=float(lon),
                place_key=item.place_id,
                locked=bool(item.locked),
                estimated_cost_minor=int(item.estimated_cost_minor or 0),
                cost_status=str(metadata.get("cost_status") or "FREE_KNOWN"),
            )
        )
    return carried


def build_candidate_nodes(
    session: Session, journey: Journey, offer: AccommodationOfferSnapshot
) -> list[GraphNode]:
    context = _context(session, journey)
    operations = _operations_context(session, journey)
    city = city_fixture(journey.city_code)
    center = city.get("center", {})
    arrival = _anchor(context, journey, "arrival")
    departure = _anchor(context, journey, "departure")
    arrival_at = datetime.fromisoformat(arrival["at"])
    departure_at = datetime.fromisoformat(departure["at"])
    center_lat = float(center.get("latitude", offer.latitude))
    center_lon = float(center.get("longitude", offer.longitude))
    arrival_lat = float(arrival.get("latitude") or center_lat - 0.018)
    arrival_lon = float(arrival.get("longitude") or center_lon - 0.028)
    departure_lat = float(departure.get("latitude") or center_lat + 0.072)
    departure_lon = float(departure.get("longitude") or center_lon + 0.095)
    events: list[GraphNode] = [
        GraphNode(
            key="arrival-anchor",
            title=arrival["location"],
            role="ARRIVAL",
            start_at=arrival_at.isoformat(),
            end_at=(arrival_at + timedelta(minutes=20)).isoformat(),
            latitude=arrival_lat,
            longitude=arrival_lon,
            locked=True,
        )
    ]
    check_in_at = datetime.fromisoformat(offer.check_in_at)
    drop = _load(offer.luggage_drop_json, {})
    if operations.baggage_present and arrival_at < check_in_at:
        if not bool(drop.get("available")):
            return []
        # Luggage drop happens once the traveller actually reaches the
        # property.  A hard-coded "+60 minutes" was an optimistic guess that
        # could place the drop before the arrival leg was even possible
        # (Scheduler Truth V3 rule 2).
        arrival_node = next(
            (n for n in events if str(n.role).upper() == "ARRIVAL"), None
        )
        drop_node_probe = GraphNode(
            key=f"stay:{offer.external_offer_id}:luggage-drop",
            title=f"Luggage drop · {offer.property_name}",
            role="LUGGAGE_DROP",
            start_at="",
            end_at="",
            latitude=offer.latitude,
            longitude=offer.longitude,
            locked=True,
        )
        inbound_min = 60
        if arrival_node is not None:
            try:
                inbound_min = max(
                    20,
                    int(_travel_minutes(arrival_node, drop_node_probe, journey=journey)),
                )
            except Exception:
                inbound_min = 60
        drop_at = datetime.fromisoformat(arrival_node.end_at) + timedelta(
            minutes=inbound_min
        ) if arrival_node is not None else arrival_at + timedelta(minutes=60)
        drop_at = max(drop_at, arrival_at + timedelta(minutes=60))
        events.append(
            GraphNode(
                key=f"stay:{offer.external_offer_id}:luggage-drop",
                title=f"Luggage drop · {offer.property_name}",
                role="LUGGAGE_DROP",
                start_at=drop_at.isoformat(),
                end_at=(drop_at + timedelta(minutes=20)).isoformat(),
                latitude=offer.latitude,
                longitude=offer.longitude,
                locked=True,
            )
        )
    events.append(
        GraphNode(
            key=f"stay:{offer.external_offer_id}:check-in",
            title=f"Check in · {offer.property_name}",
            role="CHECK_IN",
            start_at=max(check_in_at, arrival_at + timedelta(minutes=45)).isoformat(),
            end_at=(
                max(check_in_at, arrival_at + timedelta(minutes=45)) + timedelta(minutes=20)
            ).isoformat(),
            latitude=offer.latitude,
            longitude=offer.longitude,
            locked=True,
        )
    )
    for item in city.get("activities", []):
        start_at = _at_on_day(arrival_at, int(item["day_offset"]), str(item["start"]), journey.timezone)
        end_at = _at_on_day(arrival_at, int(item["day_offset"]), str(item["end"]), journey.timezone)
        if start_at <= arrival_at + timedelta(minutes=20) or end_at >= departure_at:
            continue
        events.append(
            GraphNode(
                key=f"place:{item['key']}",
                title=str(item["title"]),
                role="ACTIVITY",
                start_at=start_at.isoformat(),
                end_at=end_at.isoformat(),
                latitude=float(item["latitude"]),
                longitude=float(item["longitude"]),
                place_key=str(item["key"]),
                estimated_cost_minor=int(item.get("cost_amount_minor", 0)),
                cost_status=str(item.get("cost_status", "UNKNOWN")),
            )
        )
    for day_offset in range(_nights(context, journey)):
        overnight_at = _at_on_day(arrival_at, day_offset, "22:30", journey.timezone)
        if arrival_at < overnight_at < departure_at:
            events.append(
                GraphNode(
                    key=f"stay:{offer.external_offer_id}:night:{day_offset + 1}",
                    title=f"Overnight · {offer.property_name}",
                    role="STAY",
                    start_at=overnight_at.isoformat(),
                    end_at=(overnight_at + timedelta(hours=8)).isoformat(),
                    latitude=offer.latitude,
                    longitude=offer.longitude,
                    locked=True,
                )
            )
    check_out_at = datetime.fromisoformat(offer.check_out_at)
    events.append(
        GraphNode(
            key=f"stay:{offer.external_offer_id}:check-out",
            title=f"Check out · {offer.property_name}",
            role="CHECK_OUT",
            start_at=check_out_at.isoformat(),
            end_at=(check_out_at + timedelta(minutes=20)).isoformat(),
            latitude=offer.latitude,
            longitude=offer.longitude,
            locked=True,
        )
    )
    events.append(
        GraphNode(
            key="departure-anchor",
            title=departure["location"],
            role="DEPARTURE",
            start_at=(departure_at - timedelta(minutes=10)).isoformat(),
            end_at=departure_at.isoformat(),
            latitude=departure_lat,
            longitude=departure_lon,
            locked=True,
        )
    )
    # Augment, never replace: keep the traveller's chosen stops.
    carried = _carried_itinerary_nodes(
        session, journey, exclude_keys={str(node.key) for node in events}
    )
    if carried:
        # The calendar reserves pinned truth first and places flexible stops
        # into the remaining windows, so its output is already a valid,
        # non-overlapping timeline.  No further cursor filtering is applied:
        # dropping nodes here would silently delete the traveller's stops.
        return _reschedule_with_carried(
            events, carried, arrival_at, departure_at, journey=journey
        )
    events.sort(key=lambda node: datetime.fromisoformat(node.start_at))
    result: list[GraphNode] = []
    cursor = arrival_at - timedelta(minutes=1)
    for node in ordered_events(events):
        start = datetime.fromisoformat(node.start_at)
        end = datetime.fromisoformat(node.end_at)
        if start < cursor and node.role not in {"STAY", "ACTIVITY"}:
            continue
        result.append(node)
        cursor = max(cursor, end)
    return result


def ordered_events(events: list[GraphNode]) -> list[GraphNode]:
    """Stable temporal order: anchors keep their slot, everything else follows."""

    role_rank = {"ARRIVAL": 0, "ACTIVITY": 1, "STAY": 2, "DEPARTURE": 3}
    return sorted(
        events,
        key=lambda node: (
            datetime.fromisoformat(node.start_at),
            role_rank.get(str(node.role).upper(), 2),
        ),
    )



def _offer_of(key: str) -> str | None:
    """The property a structural stay key belongs to, if any.

    ``stay:osm:node/123:check-in`` -> ``osm:node/123``
    """

    text = str(key)
    if not text.startswith("stay:"):
        return None
    body = text[len("stay:") :]
    for suffix in (":luggage-drop", ":check-in", ":check-out", ":night:"):
        index = body.find(suffix)
        if index != -1:
            return body[:index]
    return None

def _reschedule_with_carried(
    anchors: list[GraphNode],
    carried: list[GraphNode],
    arrival_at: datetime,
    departure_at: datetime,
    *,
    journey: Journey | None = None,
) -> list[GraphNode]:
    """Merge a chosen itinerary with the stay's structural operations.

    Stay Calendar V2 (docs/STAY_CALENDAR_V2.md).  Beyond "no overlap":

    * **transition time** - a flexible stop must be reachable from the node
      before it and must still reach the node after it, so free intervals carry
      their bounding nodes instead of being bare [start, end] pairs;
    * **day semantics** - an activity keeps its original local date.  Choosing a
      hotel is not permission to rewrite the traveller's daily plan, so a stop
      that no longer fits its own day is reported as a conflict rather than
      silently moved to another day.

    Pinned truth (ARRIVAL, DEPARTURE, locked/C4 activities, LUGGAGE_DROP,
    CHECK_IN, STAY nights, CHECK_OUT) is validated first and never moved.
    """

    structural_roles = {"LUGGAGE_DROP", "CHECK_IN", "STAY", "CHECK_OUT"}

    def _copy(node: GraphNode, new_start: datetime, new_end: datetime) -> GraphNode:
        return GraphNode(
            key=node.key,
            title=node.title,
            role=node.role,
            start_at=new_start.isoformat(),
            end_at=new_end.isoformat(),
            latitude=node.latitude,
            longitude=node.longitude,
            place_key=node.place_key,
            locked=node.locked,
            estimated_cost_minor=node.estimated_cost_minor,
            cost_status=node.cost_status,
        )

    def _key(node: GraphNode) -> str:
        return str(node.key)

    arrival = next((n for n in anchors if str(n.role).upper() == "ARRIVAL"), None)
    departure = next((n for n in anchors if str(n.role).upper() == "DEPARTURE"), None)
    pinned: list[GraphNode] = [n for n in anchors if str(n.role).upper() in structural_roles]
    if arrival is not None:
        pinned.append(arrival)
    if departure is not None:
        pinned.append(departure)

    def _structural_signature(node: GraphNode) -> str | None:
        """Stable identity for a structural operation.

        The same operation can reach us under two key spellings: the semantic
        key (``stay:{offer}:luggage-drop``) and the materialised Place id.
        Normalising on the semantic suffix prevents one physical operation from
        being scheduled twice and reported as a pinned overlap.
        """

        key = _key(node)
        for suffix in (":luggage-drop", ":check-in", ":check-out", ":night:"):
            if suffix in key:
                return key[key.index("stay:") :] if "stay:" in key else key
        role = str(node.role).upper()
        if role in structural_roles:
            return f"{role}:{node.start_at}"
        # The stay graph's own operations are also carried forward as generic
        # activities whose key is the materialised Place id.  The product title
        # is the reliable discriminator, because the role was flattened.
        title = str(node.title or "")
        for prefix, role_name in (
            ("Luggage drop", "LUGGAGE_DROP"),
            ("Check in", "CHECK_IN"),
            ("Check out", "CHECK_OUT"),
            ("Overnight", "STAY"),
        ):
            if title.startswith(prefix):
                return f"{role_name}:{node.start_at}"
        return None

    # Structural operations belong to a specific property.  When the traveller
    # switches hotel, the previous property's check-in / luggage-drop / night /
    # check-out nodes must be replaced, not run alongside the new ones (they
    # otherwise collide on the same clock times).
    current_offers = {
        _offer_of(_key(n)) for n in pinned if _offer_of(_key(n)) is not None
    }

    flexible: list[GraphNode] = []
    pinned_keys = {_key(n) for n in pinned}
    pinned_signatures = {
        sig for sig in (_structural_signature(n) for n in pinned) if sig is not None
    }
    for node in carried:
        signature = _structural_signature(node)
        if signature is not None:
            offer = _offer_of(_key(node))
            if offer is not None and current_offers and offer not in current_offers:
                # A different property's structural operation: superseded.
                continue
            # Already reserved by the stay graph itself.
            if signature in pinned_signatures:
                continue
            pinned_signatures.add(signature)
            pinned.append(node)
            pinned_keys.add(_key(node))
            continue
        if bool(node.locked):
            if _key(node) in pinned_keys:
                continue
            pinned_keys.add(_key(node))
            pinned.append(node)
        else:
            flexible.append(node)

    # The same physical operation can arrive twice (semantic key and
    # materialised key).  Exact duplicates are one operation, not a conflict.
    deduped: list[GraphNode] = []
    seen_ops: set[tuple[str, str, str]] = set()
    for node in sorted(pinned, key=lambda n: datetime.fromisoformat(n.start_at)):
        signature = _structural_signature(node) or _key(node)
        op = (signature, node.start_at, node.end_at)
        if op in seen_ops:
            continue
        seen_ops.add(op)
        deduped.append(node)
    pinned = deduped

    conflicts: list[str] = []
    # The arrival anchor itself defines the start of the window; flexible time
    # only becomes available after it ends.
    window_start = datetime.fromisoformat(arrival.start_at) if arrival else arrival_at
    free_from = datetime.fromisoformat(arrival.end_at) if arrival else arrival_at
    previous_end: datetime | None = None
    previous_role: str | None = None
    previous_node: GraphNode | None = None
    duplicates: list[str] = []
    for node in pinned:
        node_start = datetime.fromisoformat(node.start_at)
        node_end = datetime.fromisoformat(node.end_at)
        if node_end < node_start:
            conflicts.append(f"INVALID_WINDOW:{_key(node)}")
            continue
        if node_start < window_start:
            conflicts.append(f"PINNED_BEFORE_ARRIVAL:{_key(node)}")
        if node_end > departure_at:
            conflicts.append(f"PINNED_AFTER_DEPARTURE:{_key(node)}")
        if previous_node is not None and previous_end is not None:
            # Scheduler Truth V3 rule 2: pinned truth must be *reachable*, not
            # merely non-overlapping.  Two confirmed nodes ten minutes apart but
            # thirty minutes of travel apart is a conflict.
            # Two operations at the *same property* are not a journey: dropping
            # luggage and checking in at one hotel costs no travel time.
            same_property = (
                _offer_of(_key(previous_node)) is not None
                and _offer_of(_key(previous_node)) == _offer_of(_key(node))
            )
            required = (
                0
                if same_property
                else _travel_minutes(previous_node, node, journey=journey)
            )
            if node_start < previous_end + timedelta(minutes=required):
                if previous_role == str(node.role).upper():
                    conflicts.append(f"PINNED_DUPLICATE:{_key(node)}")
                    duplicates.append(_key(node))
                else:
                    conflicts.append(f"STAY_PINNED_MOBILITY_CONFLICT:{_key(node)}")
        if previous_end is not None and node_start < previous_end:
            # The same structural operation can reach us under two key
            # spellings (semantic key and materialised Place id) with slightly
            # different windows.  That is one operation, not a conflict, so the
            # later duplicate is dropped.  Genuinely different operations
            # overlapping is still an explicit conflict.
            if previous_role == str(node.role).upper():
                conflicts.append(f"PINNED_DUPLICATE:{_key(node)}")
                duplicates.append(_key(node))
            else:
                conflicts.append(f"PINNED_OVERLAP:{_key(node)}")
        previous_end = max(previous_end, node_end) if previous_end else node_end
        previous_role = str(node.role).upper()
        previous_node = node

    roles = [str(n.role).upper() for n in pinned]
    check_in_index = roles.index("CHECK_IN") if "CHECK_IN" in roles else None
    stay_indices = [i for i, r in enumerate(roles) if r == "STAY"]
    check_out_index = roles.index("CHECK_OUT") if "CHECK_OUT" in roles else None
    if check_in_index is not None and stay_indices and check_in_index > stay_indices[0]:
        conflicts.append("CHECK_IN_AFTER_FIRST_STAY")
    if check_out_index is not None and stay_indices and check_out_index < stay_indices[-1]:
        conflicts.append("CHECK_OUT_BEFORE_LAST_STAY")

    if duplicates:
        drop = set(duplicates)
        pinned = [node for node in pinned if _key(node) not in drop]
        conflicts = [c for c in conflicts if not c.startswith("PINNED_DUPLICATE:")]

    if conflicts:
        raise StayItineraryConflict("STAY_PINNED_CONFLICT:" + ",".join(sorted(set(conflicts))))

    # ---- 3. free intervals that know their boundaries ----------------
    # Each interval is [left_node, free_start, free_end, right_node].  The
    # bounding nodes let placement reserve travel on both sides.
    intervals: list[list[Any]] = []
    cursor = free_from
    left_node: GraphNode | None = arrival
    for node in pinned:
        node_start = datetime.fromisoformat(node.start_at)
        if node_start > cursor:
            intervals.append([left_node, cursor, node_start, node])
        cursor = max(cursor, datetime.fromisoformat(node.end_at))
        left_node = node
    if departure_at > cursor:
        intervals.append([left_node, cursor, departure_at, None])

    def _travel(a: GraphNode | None, b: GraphNode | None) -> timedelta:
        if a is None or b is None:
            return timedelta(0)
        return timedelta(minutes=_travel_minutes(a, b, journey=journey))

    # ---- 4. place flexible activities, day-scoped --------------------
    placed: list[GraphNode] = []
    for node in flexible:
        original_start = datetime.fromisoformat(node.start_at)
        original_end = datetime.fromisoformat(node.end_at)
        duration = max(timedelta(minutes=15), original_end - original_start)
        wanted_day = original_start.date()
        scheduled: GraphNode | None = None
        for interval in intervals:
            left, _free_start, free_end, right = interval
            inbound = _travel(left, node)
            candidate = interval[1] + inbound
            # Day semantics: the stop stays on its original local date.  A free
            # interval can legitimately begin on the previous day (the overnight
            # window does), so advance the candidate to the wanted day instead
            # of discarding the interval.  Skipping it made every day-2 stop
            # unplaceable and reported a false STAY_ITINERARY_CONFLICT.
            if candidate.date() != wanted_day:
                wanted_at = datetime.combine(wanted_day, original_start.timetz())
                if wanted_at < candidate:
                    continue
                candidate = wanted_at
            outbound = _travel(node, right)
            # Scheduler Truth V3 rule 1: "flexible" means movable in time, not
            # disposable in duration.  A traveller-selected visit is never
            # silently shortened; if the original duration cannot fit on its own
            # day the calendar reports a conflict.
            if candidate + duration + outbound > free_end:
                continue
            scheduled = _copy(node, candidate, candidate + duration)
            interval[0] = scheduled
            interval[1] = candidate + duration
            break
        if scheduled is None:
            conflicts.append(f"NO_SAME_DAY_SLOT:{_key(node)}")
            continue
        placed.append(scheduled)

    if conflicts:
        # Never silently move Day-2 content to Day 1, never drop a stop.
        raise StayItineraryConflict(
            "STAY_ITINERARY_CONFLICT:" + ",".join(sorted(set(conflicts)))
        )

    # ---- 5. emit one chronologically ordered calendar -----------------
    out: list[GraphNode] = list(pinned) + placed
    out.sort(
        key=lambda n: (
            datetime.fromisoformat(n.start_at),
            {"ARRIVAL": 0, "LUGGAGE_DROP": 1, "CHECK_IN": 2, "ACTIVITY": 3,
             "STAY": 4, "CHECK_OUT": 5, "DEPARTURE": 6}.get(str(n.role).upper(), 3),
        ),
    )
    return out


class StayItineraryConflict(RuntimeError):
    """The pinned calendar cannot fit the traveller's itinerary."""


def _enforce_sequence(
    nodes: list[GraphNode], *, journey: Journey | None = None
) -> list[GraphNode]:
    """Final ordering check for an already-built calendar.

    The calendar reserves pinned truth first, so this pass must never move a
    pinned node.  It only nudges genuinely flexible nodes that would otherwise
    sit closer than the journey's mobility evidence allows.
    """

    if len(nodes) < 2:
        return nodes
    ordered = sorted(
        nodes,
        key=lambda node: (
            datetime.fromisoformat(node.start_at),
            0 if str(node.role).upper() == "ARRIVAL" else 1 if str(node.role).upper() == "DEPARTURE" else 2,
        ),
    )
    # Both structural anchors are locked Journey truth: they define the window
    # the gate validates against, so they must never be pushed later.
    pinned_roles = {"ARRIVAL", "DEPARTURE"}
    fixed: list[GraphNode] = []
    cursor: datetime | None = None
    previous: GraphNode | None = None
    for node in ordered:
        start = datetime.fromisoformat(node.start_at)
        end = datetime.fromisoformat(node.end_at)
        duration = max(timedelta(minutes=15), end - start)
        if (
            str(node.role).upper() in pinned_roles
            or bool(node.locked)
            or cursor is None
        ):
            # Pinned anchors and locked truth keep their authoritative window;
            # everything else is scheduled around them.
            fixed.append(node)
            cursor = max(cursor, end) if cursor is not None else end
            previous = node
            continue
        # A stay node is not a destination to travel to; it is where the night
        # is spent.  Adding a sightseeing travel gap here pushed check-in to
        # 17:40 and the second night to 01:45.
        # A stay node is where the night is spent, not a place to travel to:
        # neither side of a stay leg contributes a sightseeing travel gap.
        stay_involved = (
            str(node.role).upper() == "STAY"
            or (previous is not None and str(previous.role).upper() == "STAY")
        )
        if stay_involved:
            gap = timedelta(0)
        else:
            gap = (
                timedelta(minutes=_travel_minutes(previous, node, journey=journey))
                if previous is not None
                else timedelta(0)
            )
        new_start = max(start, cursor + gap)
        new_end = new_start + duration
        fixed.append(
            GraphNode(
                key=node.key,
                title=node.title,
                role=node.role,
                start_at=new_start.isoformat(),
                end_at=new_end.isoformat(),
                latitude=node.latitude,
                longitude=node.longitude,
                place_key=node.place_key,
                locked=node.locked,
                estimated_cost_minor=node.estimated_cost_minor,
                cost_status=node.cost_status,
            )
        )
        cursor = new_end
        previous = fixed[-1]
    return fixed


def _travel_minutes(
    a: GraphNode | None,
    b: GraphNode,
    *,
    journey: Journey | None = None,
) -> int:
    """Inter-stop travel time, from the same generator the gate will validate.

    ``_mode_fact`` produces every stay-graph leg duration, and the feasibility
    gate checks those exact facts.  Using anything else here (a distance
    heuristic, a different provider) yields a schedule that looks feasible at
    planning time and then fails ``INSUFFICIENT_TRAVEL_GAP``.
    """

    if a is None:
        return 0
    if _same_complex(a, b):
        # Two stops of one venue (check-in and the night that follows it) have
        # no meaningful inter-stop leg.
        return 5
    if journey is not None:
        # A traveller between two confirmed nodes picks a sensible mode, not
        # necessarily walking.  Using WALK alone made a hotel-to-airport leg
        # look like a 170-minute impossibility.  Take the fastest feasible mode
        # so the reachability rule reflects real behaviour while still refusing
        # genuinely unreachable pinned truth.
        durations: list[int] = []
        for mode in ("AUTO", "RIDE_HAIL", "TRANSIT", "BICYCLE", "WALK"):
            try:
                fact = _mode_fact(
                    journey.id, None, a, b, mode, journey.budget_currency
                )
                durations.append(max(0, int(fact.duration_min)))
            except Exception:
                continue
        if durations:
            # Reserve the SAME leg the gate will validate.
            #
            # The plan's selected leg fact is the fastest feasible mode (see
            # ``_selected_facts``, which takes the smallest duration).  Reserving
            # the slowest mode here made the calendar demand up to 134 minutes
            # between stops that the planner had legitimately placed ~20 minutes
            # apart, so every hotel was rejected with a false
            # STAY_ITINERARY_CONFLICT:NO_SAME_DAY_SLOT.  Calendar reservation and
            # gate validation must describe one journey, not two.
            return min(durations)
        try:
            fact = _mode_fact(
                journey.id, None, a, b, "WALK", journey.budget_currency
            )
            return max(0, int(fact.duration_min))
        except Exception:
            pass
    return max(5, math.ceil(_distance_m(a, b) / 350) + 4)


def _same_complex(a: GraphNode, b: GraphNode) -> bool:
    """Whether two stops belong to one physical venue.

    Only a complete shared venue segment is evidence of co-location.  Opaque
    place identities such as ``real-place-<hex>`` share the literal prefix
    ``real-place-`` while describing different venues, so comparing raw prefix
    lengths reported unrelated attractions as one building, reserved a 5 minute
    leg for them, and then failed the very plan it had just built with a false
    ``INSUFFICIENT_TRAVEL_GAP``.
    """

    left = str(a.key or "")
    right = str(b.key or "")
    if not left or not right:
        return False
    if left == right:
        return True
    if a.place_key and a.place_key == b.place_key:
        return True
    left_venue = _venue_of(left)
    right_venue = _venue_of(right)
    return left_venue is not None and left_venue == right_venue


#: Trailing key segments that name one stop *within* a venue rather than a
#: venue of their own (``...:check-in`` and ``...:night:1`` are one hotel).
_VENUE_STOP_SEGMENTS = frozenset(
    {
        "arrival",
        "departure",
        "check-in",
        "check-out",
        "checkin",
        "checkout",
        "night",
        "stay",
        "luggage-drop",
        "luggage_drop",
        "drop",
    }
)


def _venue_of(key: str) -> str | None:
    """The venue a stop key belongs to, or ``None`` when the key is a venue.

    ``stay:osm:node/4304450093:night:1`` and ``stay:osm:node/4304450093:check-in``
    both belong to ``stay:osm:node/4304450093``.  An opaque place identity such
    as ``real-place-<hex>`` has no stop segment and is its own venue, so two
    unrelated attractions are never merged just because their ids share a prefix.
    """

    parts = [segment for segment in str(key).split(":") if segment]
    if len(parts) > 1 and parts[-1].isdigit():
        parts = parts[:-1]
    if len(parts) < 2:
        return None
    if parts[-1].split("/")[0] not in _VENUE_STOP_SEGMENTS:
        return None
    return ":".join(parts[:-1]) or None


def _distance_m(a: GraphNode, b: GraphNode) -> int:
    lat_scale = 111_320.0
    lon_scale = 111_320.0 * math.cos(math.radians((a.latitude + b.latitude) / 2))
    return max(
        80,
        int(
            math.hypot(
                (a.latitude - b.latitude) * lat_scale, (a.longitude - b.longitude) * lon_scale
            )
        ),
    )


def _mode_fact(
    journey_id: str,
    option_id: str | None,
    from_node: GraphNode,
    to_node: GraphNode,
    mode: str,
    currency: str = "CNY",
) -> MobilityFact:
    distance = _distance_m(from_node, to_node)
    profiles = {
        "WALK": (
            max(5, math.ceil(distance / 75)),
            0,
            "FREE_KNOWN",
            None,
            0,
            "HIGH",
            0.82,
            False,
        ),
        "BICYCLE": (
            max(4, math.ceil(distance / 210)),
            None,
            "UNKNOWN",
            5,
            0,
            "MEDIUM",
            0.78,
            True,
        ),
        "TRANSIT": (
            max(8, math.ceil(distance / 330) + 6),
            None,
            "UNKNOWN",
            5,
            1,
            "LOW",
            0.91,
            True,
        ),
        "AUTO": (
            max(5, math.ceil(distance / 430) + 3),
            None,
            "UNKNOWN",
            2,
            0,
            "LOW",
            0.88,
            True,
        ),
        "RIDE_HAIL": (
            max(5, math.ceil(distance / 420) + 4),
            None,
            "UNKNOWN",
            2,
            0,
            "LOW",
            0.84,
            True,
        ),
    }
    duration, cost_major, cost_status, walking, transfers, exposure, reliability, paid = (
        profiles[mode]
    )
    normalized_currency = str(currency or "CNY").upper()
    estimate = SYNTHETIC_ESTIMATE_MAJOR_BY_CURRENCY.get(normalized_currency, {}).get(mode)
    if mode not in {"WALK", "RIDE_HAIL"} and estimate is not None:
        cost_major = estimate
        cost_status = "ESTIMATED"
    cost = major_to_minor(cost_major, normalized_currency) if cost_major is not None else None
    if mode == "WALK":
        walking = duration
    captured = _now()
    geometry = {
        "type": "Feature",
        "properties": {"provider": "citynexus-synthetic-fixture", "mode": mode, "crs": "WGS84"},
        "geometry": {
            "type": "LineString",
            "coordinates": [
                [from_node.longitude, from_node.latitude],
                [to_node.longitude, to_node.latitude],
            ],
        },
    }
    return MobilityFact(
        id=str(uuid4()),
        journey_id=journey_id,
        option_id=option_id,
        from_key=from_node.key,
        to_key=to_node.key,
        mode=mode,
        duration_min=int(duration),
        distance_m=distance,
        cost_amount_minor=cost,
        currency=normalized_currency if cost is not None else None,
        cost_status=cost_status,
        walking_min=int(walking or 0),
        transfers=int(transfers),
        weather_exposure=exposure,
        reliability_score=float(reliability),
        provider_key="citynexus-synthetic-fixture",
        provenance="SYNTHETIC_FIXTURE",
        geometry_json=_json(geometry),
        crs="WGS84",
        paid_mode=paid,
        evidence_json=_json(
            {
                "fact_type": "normalized_route_leg",
                "provider_status": "SYNTHETIC_FIXTURE",
                "cost_status": cost_status,
                "cost_provenance": "CITYNEXUS_ESTIMATE" if cost_status == "ESTIMATED" else ("FREE_KNOWN" if mode == "WALK" else "UNKNOWN"),
                "coordinate_boundary": "captured provider coordinates normalized to WGS84",
            }
        ),
        captured_at=captured,
        expires_at=(datetime.now(timezone.utc) + timedelta(hours=4)).isoformat(),
    )


def capture_mobility_facts(
    session: Session, journey: Journey, offer: AccommodationOfferSnapshot
) -> list[MobilityFact]:
    nodes = build_candidate_nodes(session, journey, offer)
    facts: list[MobilityFact] = []
    for from_node, to_node in zip(nodes, nodes[1:]):
        for mode in MODES:
            existing = session.scalar(
                select(MobilityFact).where(
                    MobilityFact.journey_id == journey.id,
                    MobilityFact.from_key == from_node.key,
                    MobilityFact.to_key == to_node.key,
                    MobilityFact.mode == mode,
                )
            )
            if existing is None:
                existing = _mode_fact(
                    journey.id,
                    None,
                    from_node,
                    to_node,
                    mode,
                    journey.budget_currency,
                )
                session.add(existing)
            facts.append(existing)
    session.flush()
    return facts


def _day_payload(nodes: list[GraphNode], selected: dict[str, MobilityFact]) -> list[dict[str, Any]]:
    grouped: dict[str, list[GraphNode]] = {}
    for node in nodes:
        grouped.setdefault(datetime.fromisoformat(node.start_at).date().isoformat(), []).append(
            node
        )
    days: list[dict[str, Any]] = []
    for index, (local_date, items) in enumerate(sorted(grouped.items()), start=1):
        outgoing = [
            selected[f"{item.key}->{next_item.key}"].mode
            for item, next_item in zip(items, items[1:])
            if f"{item.key}->{next_item.key}" in selected
        ]
        anchor = next(
            (item.role for item in items if item.role in {"ARRIVAL", "DEPARTURE"}), "STAY"
        )
        days.append(
            {
                "day": index,
                "date": local_date,
                "label": datetime.fromisoformat(items[0].start_at).strftime("%A · %d %b"),
                "anchor": anchor,
                "activities": [item.title for item in items],
                "mode": outgoing[0] if outgoing else "WALK",
            }
        )
    return days


def generate_stay_options(
    session: Session,
    journey: Journey,
    *,
    weather: str = "NORMAL",
    generation_out: dict[str, Any] | None = None,
    stage_out: Callable[[str], None] | None = None,
) -> list[StayJourneyOption]:
    """Generate ranked stay-aware options for one journey.

    `stage_out` names the stage the work has genuinely entered (`DISCOVERY` /
    `NORMALIZATION` / `MOBILITY_CHECK` / `RANKING`) and exists for ORD-0057's operation
    record, where a traveller waiting on a 115s chain is allowed exactly one honest
    question: how far did it actually get.  It is emitted beside the matching `phase()`
    span on purpose - the timing instrument and the reported stage cannot then disagree,
    and no percentage is invented anywhere.  Optional and inert when unset, and unlike
    `CITYNEXUS_PROVIDER_TIMING` it is not gated off: an operation's phase is product
    state, not diagnostics.
    """

    def stage(name: str) -> None:
        if stage_out is not None:
            stage_out(name)

    provider_attempts: list[dict[str, Any]] = []
    stage("DISCOVERY")
    with phase("stay.offers", detail=f"city={journey.city_code}") as span:
        offers = capture_accommodation_offers(session, journey, attempts_out=provider_attempts)
    if span is not None:
        # "empty" here is the incident: an empty offer list is what makes the whole
        # generation return `top_k: []` with HTTP 200, and it looks identical in a log to
        # "this city genuinely has nothing".  The provider's own attempt list says which.
        span.outcome = "ok" if offers else "empty"
        span.detail = f"city={journey.city_code} offers={len(offers)}"
    preference_items = planning_preference_items(session, journey.traveler_id, journey.id)
    learned_dimensions = {
        str(getattr(item, "preference_key", ""))
        for item in preference_items
        if getattr(item, "source", "") in {"LEARNED_SOFT", "EXPLICIT_MISSION"}
    }
    staged: list[tuple[StayJourneyOption, list[MobilityFact]]] = []
    rejected: list[tuple[Any, str]] = []
    for provisional_rank, offer in enumerate(offers, start=1):
        # One property whose pinned windows cannot coexist with the traveller's
        # itinerary is a fact about that property, not a reason to withhold
        # every other hotel.  Record it as unselectable and keep going.
        stage("NORMALIZATION")
        with phase(f"stay.nodes[{provisional_rank}]", detail=str(offer.property_name)[:40]) as nodes_span:
            try:
                nodes = build_candidate_nodes(session, journey, offer)
            except StayItineraryConflict:
                if nodes_span is not None:
                    nodes_span.outcome = "rejected:STAY_ITINERARY_CONFLICT"
                rejected.append((offer, "STAY_ITINERARY_CONFLICT"))
                continue
        if len(nodes) < 2:
            rejected.append((offer, "INCOMPLETE_STAY_GRAPH"))
            continue
        stage("MOBILITY_CHECK")
        with phase(
            f"stay.legs[{provisional_rank}]",
            detail=f"pairs={max(0, len(nodes) - 1)} modes={len(MODES)}",
        ):
            facts = capture_mobility_facts(session, journey, offer)
        stage("RANKING")
        with phase(f"stay.solve[{provisional_rank}]") as solve_span:
            try:
                solver_result = solve_mode_choices(
                    facts,
                    budget_minor=max(
                        0, int(journey.budget_amount_minor) - int(offer.total_amount_minor)
                    ),
                    weather_state=weather,
                    learned_dimensions=learned_dimensions,
                )
            except ValueError:
                rejected.append((offer, "NO_FEASIBLE_MODE_CHOICE"))
                continue
        selected_facts = list(solver_result.selected)
        selected_map = {f"{item.from_key}->{item.to_key}": item for item in selected_facts}
        mobility_total = sum(int(item.cost_amount_minor or 0) for item in selected_facts)
        walking = sum(item.walking_min for item in selected_facts)
        latest_pre_departure = datetime.fromisoformat(nodes[-2].end_at)
        departure_at = datetime.fromisoformat(nodes[-1].start_at)
        final_leg = selected_map.get(f"{nodes[-2].key}->{nodes[-1].key}")
        margin = max(
            0,
            int((departure_at - latest_pre_departure).total_seconds() // 60)
            - int(final_leg.duration_min if final_leg else 0),
        )
        check_in = next((node for node in nodes if node.role == "CHECK_IN"), nodes[0])
        arrival_fit = (
            1.0
            if datetime.fromisoformat(check_in.start_at) >= datetime.fromisoformat(nodes[0].end_at)
            else 0.0
        )
        departure_fit = min(1.0, margin / 60) if margin else 0.0
        preference_bonus = 0.08 if "low_walking" in learned_dimensions and walking <= 45 else 0.0
        total_cost = int(offer.total_amount_minor) + mobility_total
        score = round(
            max(
                0.0,
                1.0
                - total_cost / max(1, int(journey.budget_amount_minor)) * 0.30
                - walking / 1500
                + min(margin, 90) / 900
                + (0.08 if offer.refundable else 0.0)
                + preference_bonus,
            ),
            4,
        )
        raw_leg_map = [
            {
                "from_key": item.from_key,
                "to_key": item.to_key,
                "mode": item.mode,
                "mobility_fact_id": item.id,
                "cost_status": item.cost_status,
            }
            for item in selected_facts
        ]
        impact = {
            "schedule_evidence": {
                "protected_margin_min": margin,
                "arrival_fit": arrival_fit,
                "departure_fit": departure_fit,
                "source": "actual candidate timeline and selected route durations",
            },
            "weather": {
                "state": weather,
                "protected_commitments_preserved": margin >= 0,
            },
            "modes_available": list(MODES),
            "twin": {
                "dimensions": sorted(learned_dimensions),
                "authority": "ranking_only",
            },
            "mode_solver": {
                "kind": solver_result.optimizer_kind,
                "status": solver_result.solver_status,
                "objective_value": solver_result.objective_value,
                "unknown_paid_cost_selected": solver_result.unknown_paid_cost_selected,
            },
            "leg_mode_map": raw_leg_map,
        }
        existing = session.scalar(
            select(StayJourneyOption).where(
                StayJourneyOption.journey_id == journey.id,
                StayJourneyOption.accommodation_offer_id == offer.id,
            )
        )
        if existing is None:
            existing = StayJourneyOption(
                id=str(uuid4()),
                journey_id=journey.id,
                accommodation_offer_id=offer.id,
                rank=100 + provisional_rank,
                score=score,
                status="CANDIDATE",
                stay_total_minor=offer.total_amount_minor,
                daily_travel_minor=mobility_total,
                mobility_total_minor=mobility_total,
                walking_min=walking,
                protected_margin_min=margin,
                arrival_fit=arrival_fit,
                departure_fit=departure_fit,
                cancellation_score=1.0 if offer.refundable else 0.25,
                days_json=_json(_day_payload(nodes, selected_map)),
                leg_modes_json=_json(raw_leg_map),
                journey_impact_json=_json(impact),
                hard_constraints_passed=margin >= 0,
                optimizer_kind=solver_result.optimizer_kind,
                weather_state=weather,
                materialized_plan_version_id=None,
                created_at=_now(),
            )
            session.add(existing)
        else:
            existing.rank = 100 + provisional_rank
            existing.score = score
            existing.weather_state = weather
            existing.days_json = _json(_day_payload(nodes, selected_map))
            existing.leg_modes_json = _json(raw_leg_map)
            existing.journey_impact_json = _json(impact)
            existing.mobility_total_minor = mobility_total
            existing.daily_travel_minor = mobility_total
            existing.walking_min = walking
            existing.protected_margin_min = margin
            existing.arrival_fit = arrival_fit
            existing.departure_fit = departure_fit
            existing.hard_constraints_passed = margin >= 0
            existing.optimizer_kind = solver_result.optimizer_kind
        session.flush()
        for fact in selected_facts:
            fact.option_id = existing.id
        staged.append((existing, selected_facts))
    session.flush()
    ranked = sorted((item for item, _facts in staged), key=lambda item: (-item.score, item.id))
    for rank, item in enumerate(ranked, start=1):
        item.rank = rank
    session.flush()
    last_rejected = rejected
    generate_stay_options.last_rejected = last_rejected  # type: ignore[attr-defined]
    if generation_out is not None:
        # An empty `top_k` must never be readable as "success, nothing to show".  These are
        # different facts and the traveller sees a different thing in each: a city with no
        # mapped hotels, a provider that answered nothing, a one-day trip that needs no
        # stay, or hotels that all conflicted with the itinerary.
        reasons = [str(reason) for _offer, reason in rejected]
        trip_row = session.get(JourneyTripContext, journey.id)
        stay_declared = None if trip_row is None else trip_row.stay_budget_declared
        if ranked:
            outcome = "OPTIONS_FOUND"
        elif any(item.get("outcome") == "zero_nights" for item in provider_attempts):
            outcome = "NOT_APPLICABLE_ZERO_NIGHTS"
        elif not offers:
            outcome = "NO_OFFERS_FROM_PROVIDER"
        elif stay_declared is False:
            # ORD-0355: `ALL_CANDIDATES_REJECTED` is the one answer in this vocabulary whose
            # sentence on the traveller's screen names 预算 as a constraint the trip failed.
            # A traveller who never typed a lodging budget must not be read past that
            # sentence, so this says the one thing genuinely known here: the budget was not
            # declared.  It is a disclosure, not a claimed cause - no planner leg prices a
            # stay against `stay_budget_amount_minor`, which is why the reasons the solver
            # actually gave stay in `rejections_by_reason` beside this code.
            outcome = "STAY_BUDGET_NOT_DECLARED"
        else:
            outcome = "ALL_CANDIDATES_REJECTED"
        generation_out.update(
            {
                "outcome": outcome,
                "offers_from_provider": len(offers),
                "options_ranked": len(ranked),
                "rejections_by_reason": {
                    reason: reasons.count(reason) for reason in sorted(set(reasons))
                },
                "provider_attempts": provider_attempts,
            }
        )
    return ranked


def activity_coordinate(place_id: str | None, places: dict[str, Place]) -> dict[str, Any]:
    """ORD-0394: where an activity happens, read off the place row that names it.

    A coordinate is a property of the place, so the projection joins the place the activity
    already points at instead of copying a number onto the activity.  When there is nothing to
    join, it says which of the two "nothing" cases this is with tokens the destination-capability
    axis already froze (`CAPABILITY_STATES`, `REASON_*`) -- it does not coin a fourth word, and it
    never falls back to a city centre: a position we invented is not a position.
    """

    def shaped(state: str, reason: str | None) -> dict[str, Any]:
        assert state in CAPABILITY_STATES, state
        return {
            "state": state,
            "reason": reason,
            "latitude": None,
            "longitude": None,
            "coordinate_system": None,
            "source_coordinate_system": None,
        }

    if not place_id:
        return shaped("UNAVAILABLE", REASON_NO_CANONICAL_IDENTITY)
    place = places.get(str(place_id))
    if place is None:
        # An identity the store does not have is missing coverage, not a missing identity; the two
        # read differently on a traveller's screen.
        return shaped("UNAVAILABLE", REASON_COVERAGE_INCOMPLETE)
    if place.latitude is None or place.longitude is None:
        return shaped("UNAVAILABLE", REASON_COVERAGE_INCOMPLETE)
    return {
        "state": READY,
        "reason": None,
        "latitude": place.latitude,
        "longitude": place.longitude,
        # Read off the row, not off the candidate: the stored system and the system a provider handed
        # us are different claims, and only the row knows both (same rule as real_city.py).
        "coordinate_system": place.coordinate_system,
        "source_coordinate_system": place.source_coordinate_system,
    }


def _active_plan_payload(session: Session, journey: Journey) -> dict[str, Any] | None:
    if not journey.active_plan_version_id:
        return None
    plan = session.get(PlanVersion, journey.active_plan_version_id)
    if plan is None:
        return None
    activities = session.scalars(
        select(Activity).where(Activity.plan_version_id == plan.id).order_by(Activity.sequence_no)
    ).all()
    # One batched read of the places these activities name. Coordinates live on the Place row (see
    # the graph projection above), so asking per row would make this projection's cost depend on
    # how many stops the plan has.
    activity_place_ids = sorted({str(item.place_id) for item in activities if item.place_id})
    activity_places: dict[str, Place] = {}
    if activity_place_ids:
        activity_places = {
            place.id: place
            for place in session.scalars(select(Place).where(Place.id.in_(activity_place_ids))).all()
        }
    legs = session.scalars(
        select(JourneyLeg)
        .where(JourneyLeg.plan_version_id == plan.id)
        .order_by(JourneyLeg.sequence_no)
    ).all()
    protected_activity_ids = set(
        session.scalars(
            select(Commitment.activity_id).where(
                Commitment.journey_id == journey.id,
                Commitment.status == "ACTIVE",
                Commitment.activity_id.is_not(None),
            )
        ).all()
    )
    location = session.scalar(
        select(LocationObservation)
        .where(LocationObservation.journey_id == journey.id)
        .order_by(LocationObservation.captured_at.desc(), LocationObservation.id.desc())
    )
    # The cursor and the activity list below come from the same read, so the
    # projection can say whether the persisted position is actually one of this
    # plan's activities.  Without it a consumer gets a cursor value and a list it
    # does not intersect, and has no way to notice.
    cursor_status = (
        "VALID"
        if journey.current_activity_id
        and any(item.id == journey.current_activity_id for item in activities)
        else "STALE"
        if journey.current_activity_id
        else "ABSENT_FALLBACK"
    )
    disruptions = session.scalars(
        select(Event)
        .where(Event.journey_id == journey.id, Event.status.in_(("DETECTED", "ACKNOWLEDGED")))
        .order_by(Event.detected_at.desc(), Event.id.desc())
        .limit(10)
    ).all()
    return {
        "id": plan.id,
        "version_no": plan.version_no,
        "status": plan.status,
        "source": plan.source,
        "mobility_snapshot_id": plan.mobility_snapshot_id,
        "mobility_evidence": {
            "snapshot_id": plan.mobility_snapshot_id,
            "expires_at": _load(plan.mobility_evidence_json, {}).get("expires_at"),
            "aggregate_provenance": _load(plan.mobility_evidence_json, {}).get("aggregate_provenance"),
            "replayable": bool(plan.mobility_evidence_json),
        },
        "current_activity_id": journey.current_activity_id,
        "cursor_status": cursor_status,
        "activities": [
            {
                "id": item.id,
                "sequence_no": item.sequence_no,
                "type": item.activity_type,
                "status": item.status,
                "title": item.title,
                # Stable identity.  The canonical plan projection
                # (GET /api/v1/plans/{id}) has always carried these, and a
                # consumer of /experience has the same right to know *which
                # place* an activity is.  Without them the only handle left is
                # the title, which is localisable, renameable, and not a key —
                # so "is the protected commitment still here?" silently becomes
                # a string match.  Fix at this layer: the child PlanVersion
                # created by an accepted replan keeps place_id intact.
                "place_id": item.place_id,
                "city_service_id": item.city_service_id,
                "start_at": item.start_at,
                "end_at": item.end_at,
                "locked": bool(item.locked),
                "protected": item.id in protected_activity_ids,
                "metadata": _load(item.metadata_json, {}),
                # Beside `metadata`, not inside it: `metadata_json` is the store's own record of what
                # a transition wrote, and folding a read-side join into it would leave a consumer
                # unable to tell the two apart.
                "location": activity_coordinate(item.place_id, activity_places),
            }
            for item in activities
        ],
        "legs": [journey_leg_payload(item) for item in legs],
        "user_location": {
            "latitude": location.latitude,
            "longitude": location.longitude,
            "accuracy_m": location.accuracy_m,
            "captured_at": location.captured_at,
            "source": location.source,
        }
        if location
        else None,
        "disruptions": [
            {
                "id": item.id,
                "type": item.event_type,
                "severity": item.severity,
                "status": item.status,
                "affected_activity_id": item.affected_activity_id,
                "occurred_at": item.occurred_at,
                "resolved_at": item.resolved_at,
                "payload": _load(item.payload_json, {}),
            }
            for item in disruptions
        ],
    }


def experience_payload(session: Session, journey: Journey) -> dict[str, Any]:
    context = _context(session, journey)
    operations = _operations_context(session, journey)
    preference_items = planning_preference_items(session, journey.traveler_id, journey.id)
    traveler_snapshot = refresh_snapshot(session, journey.traveler_id)
    traveler_twin = snapshot_payload(traveler_snapshot)
    offers = session.scalars(
        select(AccommodationOfferSnapshot)
        .where(AccommodationOfferSnapshot.journey_id == journey.id)
        .order_by(AccommodationOfferSnapshot.total_amount_minor)
    ).all()
    options = session.scalars(
        select(StayJourneyOption)
        .where(StayJourneyOption.journey_id == journey.id)
        .order_by(StayJourneyOption.rank)
    ).all()
    facts = session.scalars(
        select(MobilityFact)
        .where(MobilityFact.journey_id == journey.id)
        .order_by(MobilityFact.from_key, MobilityFact.to_key, MobilityFact.mode)
    ).all()
    selected = next((item for item in options if item.status == "SELECTED"), None)
    # One trip shape, computed here and consumed everywhere: the day count that prices a
    # stay, the day count in the headline, and the reference allowance are the same number
    # or they are nothing (ORD-0063 found a screen doing its own `round(hours/24)+1`, which
    # called a 12-hour same-day trip "2 天" and then divided the budget by that).
    shape, window_status = journey_projection_window(context, journey)
    budget_reference = derive_budget_reference(
        day_count=shape.day_count if shape is not None else None,
        budget_amount_minor=journey.budget_amount_minor,
        budget_currency=journey.budget_currency,
        party_size=context.party_size,
    )
    return {
        "journey": {
            "id": journey.id,
            "traveler_id": journey.traveler_id,
            "title": journey.title,
            "city_code": journey.city_code,
            "timezone": journey.timezone,
            "start_at": journey.start_at,
            "end_at": journey.end_at,
            "status": journey.status,
            "active_plan_version_id": journey.active_plan_version_id,
            "budget": {
                "amount_minor": journey.budget_amount_minor,
                "currency": journey.budget_currency,
            },
            "temporal_shape": {
                "day_count": shape.day_count if shape is not None else None,
                "night_count": shape.night_count if shape is not None else None,
                # What the counts mean, in the client's own words: calendar days with the
                # first and last day included, so Oct 1 -> Oct 4 is 4 days / 3 nights and
                # 08:00 -> 20:00 on one date is 1 day / 0 nights.
                "semantics": "CALENDAR_DAYS_INCLUSIVE",
                "window_status": window_status,
                "min_days": MIN_JOURNEY_DAYS,
                "max_days": MAX_JOURNEY_DAYS,
            },
        },
        "trip": {
            "arrival_anchor": _anchor(context, journey, "arrival"),
            "departure_anchor": _anchor(context, journey, "departure"),
            "party_size": context.party_size,
            "occupancy": _load(context.occupancy_json, {}),
            "budget_reference": budget_reference.as_payload(),
            "stay_budget": stay_budget_body(context),
            "baggage": {
                "present": bool(operations.baggage_present),
                "size_class": operations.baggage_size_class,
                "luggage_drop_required": bool(operations.luggage_drop_required),
            },
        },
        "offers": [offer_payload(item) for item in offers],
        "options": [option_payload(item, offers) for item in options],
        "selected_option_id": selected.id if selected else None,
        "mobility_facts": [fact_payload(item) for item in facts],
        "active_plan": _active_plan_payload(session, journey),
        "traveler_twin": {
            "explicit_dimensions": sorted(
                {
                    str(getattr(item, "preference_key", ""))
                    for item in preference_items
                    if getattr(item, "source", "") != "LEARNED_SOFT"
                }
            ),
            "learned_soft_dimensions": sorted(
                {
                    str(getattr(item, "preference_key", ""))
                    for item in preference_items
                    if getattr(item, "source", "") == "LEARNED_SOFT"
                }
            ),
            "authority": "soft_planning_input_only",
            "explicit_capabilities": traveler_twin.get("explicit_capabilities", []),
            "capability_summary": traveler_twin.get("capability_summary", {}),
        },
        "provider_mesh": {
            "accommodation": {
                "captured": {
                    "provider_key": CapturedAccommodationProvider.provider_key,
                    "status": "READY" if offers else "UNAVAILABLE_FOR_CITY",
                    "provenance": "CAPTURED_REPLAY",
                },
                "live": {
                    "provider_key": LiveAccommodationProvider.provider_key,
                    "status": "NOT_CONFIGURED",
                },
            },
            "mobility": {
                "amap": {
                    "status": "READY" if os.getenv("AMAP_WEB_KEY") else "NOT_CONFIGURED",
                    "captured_fallback": "READY"
                    if city_fixture(journey.city_code)
                    else "UNAVAILABLE_FOR_CITY",
                    "coordinate_crs": "GCJ02 at provider boundary; canonical domain WGS84",
                },
                "valhalla": {"status": "NOT_CONFIGURED"},
                "transit_realtime": {"status": "UNKNOWN_REALTIME"},
                "ride_hail": {"status": "UNKNOWN_COST"},
            },
        },
    }


def offer_payload(item: AccommodationOfferSnapshot) -> dict[str, Any]:
    expired = bool(
        item.expires_at and datetime.fromisoformat(item.expires_at) <= datetime.now(timezone.utc)
    )
    return {
        "id": item.id,
        "external_offer_id": item.external_offer_id,
        "provider_key": item.provider_key,
        "property_name": item.property_name,
        "location": {
            "latitude": item.latitude,
            "longitude": item.longitude,
            "crs": item.coordinate_crs,
        },
        "room_type": item.room_type,
        "occupancy": _load(item.occupancy_json, {}),
        "check_in_at": item.check_in_at,
        "check_out_at": item.check_out_at,
        "nightly_amount_minor": item.nightly_amount_minor,
        "taxes_amount_minor": item.taxes_amount_minor,
        "total_amount_minor": item.total_amount_minor,
        "currency": item.currency,
        "cancellation_policy": _load(item.cancellation_policy_json, {}),
        "luggage_drop": _load(item.luggage_drop_json, {}),
        "refundable": bool(item.refundable),
        "evidence": _load(item.evidence_json, {}),
        "captured_at": item.captured_at,
        "expires_at": item.expires_at,
        "status": "EXPIRED" if expired else item.status,
        "bookable": not expired and item.status not in {"EXPIRED", "WITHDRAWN"},
    }


def option_payload(
    item: StayJourneyOption, offers: list[AccommodationOfferSnapshot]
) -> dict[str, Any]:
    offer = next(offer for offer in offers if offer.id == item.accommodation_offer_id)
    raw_leg_modes = _load(item.leg_modes_json, [])
    legacy_modes = [
        str(value.get("mode"))
        for value in raw_leg_modes
        if isinstance(value, dict) and value.get("mode")
    ]
    if not legacy_modes:
        legacy_modes = [str(value) for value in raw_leg_modes]
    return {
        "id": item.id,
        "rank": item.rank,
        "status": item.status,
        "score": item.score,
        "stay": offer_payload(offer),
        "stay_total_minor": item.stay_total_minor,
        "daily_travel_minor": item.daily_travel_minor,
        "mobility_total_minor": item.mobility_total_minor,
        "walking_min": item.walking_min,
        "protected_margin_min": item.protected_margin_min,
        "arrival_fit": item.arrival_fit,
        "departure_fit": item.departure_fit,
        "cancellation_score": item.cancellation_score,
        "days": _load(item.days_json, []),
        "leg_modes": legacy_modes,
        "leg_mode_map": raw_leg_modes,
        "journey_impact": _load(item.journey_impact_json, {}),
        "hard_constraints_passed": bool(item.hard_constraints_passed),
        "optimizer_kind": item.optimizer_kind,
        "weather_state": item.weather_state,
        "materialized_plan_version_id": item.materialized_plan_version_id,
    }


def fact_payload(item: MobilityFact) -> dict[str, Any]:
    return {
        "id": item.id,
        "from_key": item.from_key,
        "to_key": item.to_key,
        "mode": item.mode,
        "duration_min": item.duration_min,
        "distance_m": item.distance_m,
        "cost_amount_minor": item.cost_amount_minor,
        "currency": item.currency,
        "cost_status": item.cost_status,
        "paid_mode": bool(item.paid_mode),
        "walking_min": item.walking_min,
        "transfers": item.transfers,
        "weather_exposure": item.weather_exposure,
        "reliability_score": item.reliability_score,
        "provider_key": item.provider_key,
        "provenance": item.provenance,
        "geometry": _load(item.geometry_json, {}),
        "crs": item.crs,
        "evidence": _load(item.evidence_json, {}),
        "captured_at": item.captured_at,
        "expires_at": item.expires_at,
    }


def journey_leg_payload(item: JourneyLeg) -> dict[str, Any]:
    return {
        "id": item.id,
        "sequence_no": item.sequence_no,
        "from_activity_id": item.from_activity_id,
        "to_activity_id": item.to_activity_id,
        "from_key": item.from_key,
        "to_key": item.to_key,
        "mode": item.mode,
        "depart_at": item.depart_at,
        "arrive_at": item.arrive_at,
        "duration_min": item.duration_min,
        "distance_m": item.distance_m,
        "cost_amount_minor": item.cost_amount_minor,
        "currency": item.currency,
        "cost_status": item.cost_status,
        "walking_min": item.walking_min,
        "transfers": item.transfers,
        "reliability_score": item.reliability_score,
        "weather_exposure": item.weather_exposure,
        "geometry": _load(item.geometry_json, {}),
        "crs": item.crs,
        "provider_key": item.provider_key,
        "provenance": item.provenance,
        "captured_at": item.captured_at,
        "expires_at": item.expires_at,
    }
