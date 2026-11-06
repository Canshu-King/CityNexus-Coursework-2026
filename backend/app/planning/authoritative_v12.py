"""Bounded authoritative multi-day composition for the V13 product path.

This module deliberately keeps the search space small: a user-supplied POI
set is assigned to a bounded number of Journey days by CP-SAT, route order is
bounded per day, and one captured mode fact is selected for every concrete
leg. The result is persisted as ordinary PlanVersion/Activity/JourneyLeg
truth so the existing selection, JourneyTwin, offline, Edge, and execution
surfaces can consume it without a second planning model.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from itertools import permutations
from typing import Any, Iterable
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.temporal import derive_journey_temporal_shape
from app.db.models import (
    Activity,
    AccommodationOfferSnapshot,
    Commitment,
    Journey,
    JourneyLeg,
    JourneyTripContext,
    MobilityFact,
    Place,
    PlanVersion,
    TransportReservation,
)
from app.execution.traveler_twin import planning_preference_items
from app.planning.day_rhythm import DayRhythm, rhythm_from_preferences
from app.planning.stay_mode_solver import solve_mode_choices
from app.replanning.provider_snapshots import MobilitySnapshot
from app.stay_mobility import GraphNode, _mode_fact, capture_accommodation_offers, city_fixture
from app.integrations.mobility.runtime import capture_journey_mobility_matrix

MODES = ("WALK", "BICYCLE", "TRANSIT", "AUTO", "RIDE_HAIL")
MIN_DAY_COUNT = 1
MAX_DAY_COUNT = 7


@dataclass(frozen=True)
class ComposeNode:
    key: str
    title: str
    role: str
    latitude: float
    longitude: float
    place_id: str
    day_index: int
    start_at: datetime
    end_at: datetime
    locked: bool = False
    cost_minor: int = 0
    cost_status: str = "FREE_KNOWN"
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True)
class ComposeResult:
    plans: tuple[PlanVersion, ...]
    stay_offer: AccommodationOfferSnapshot | None
    evidence_ids: tuple[str, ...]
    optimizer_kind: str


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _load(value: str | None, fallback: Any) -> Any:
    try:
        return json.loads(value or "")
    except (TypeError, json.JSONDecodeError):
        return fallback


def _journey_day_rhythm(session: Session, journey: Journey) -> DayRhythm | None:
    for item in planning_preference_items(session, journey.traveler_id, journey.id):
        if str(getattr(item, "preference_key", "")) != "day_rhythm":
            continue
        rhythm = rhythm_from_preferences({"day_rhythm": _load(getattr(item, "value_json", ""), {})})
        if rhythm is not None:
            return rhythm
    return None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _anchor(context: JourneyTripContext | None, journey: Journey, kind: str) -> dict[str, Any]:
    raw = context.arrival_anchor_json if context and kind == "arrival" else (
        context.departure_anchor_json if context else "{}"
    )
    fallback = journey.start_at if kind == "arrival" else journey.end_at
    value = _load(raw, {})
    return {
        "at": str(value.get("at") or fallback),
        "location": str(value.get("location") or kind.title()),
        "latitude": value.get("latitude"),
        "longitude": value.get("longitude"),
    }


def _city_center(journey: Journey) -> tuple[float, float]:
    center = city_fixture(journey.city_code).get("center", {})
    return float(center.get("latitude", 0.0)), float(center.get("longitude", 0.0))


def _ensure_place(
    session: Session,
    journey: Journey,
    *,
    key: str,
    title: str,
    category: str,
    latitude: float,
    longitude: float,
    metadata: dict[str, Any] | None = None,
) -> Place:
    provider_key = "citynexus-v12-authoritative"
    external_id = f"{journey.id}:{key}"
    row = session.scalar(
        select(Place).where(
            Place.provider_key == provider_key,
            Place.external_id == external_id,
        )
    )
    if row is None:
        row = Place(
            id=f"v12:{journey.id}:{key}",
            provider_key=provider_key,
            external_id=external_id,
            city_code=journey.city_code,
            name=title,
            category=category,
            latitude=latitude,
            longitude=longitude,
            opening_hours_json=_json({"status": "NORMALIZED", "always_available": True}),
            tags_json=_json(["v12-authoritative", category]),
            metadata_json=_json(metadata or {}),
            updated_at=_now(),
        )
        session.add(row)
        session.flush()
    return row


def _place_facts(place: Place) -> tuple[int, int, str]:
    metadata = _load(place.metadata_json, {})
    evidence = metadata.get("cost_evidence") if isinstance(metadata, dict) else None
    evidence = evidence if isinstance(evidence, dict) else {}
    raw_status = metadata.get("cost_status") or evidence.get("status")
    raw_cost = metadata.get(
        "cost_minor",
        metadata.get("cost_amount_minor", evidence.get("amount_minor")),
    )
    status = str(raw_status or ("KNOWN" if raw_cost is not None else "UNKNOWN"))
    if status == "FREE_KNOWN":
        return 0, 0, status
    if raw_cost is None:
        raise ValueError(f"V12_UNKNOWN_POI_COST:{place.id}")
    return int(raw_cost), max(15, int(metadata.get("duration_min", 60))), status


def _distance_minutes(a: ComposeNode | GraphNode, b: ComposeNode | GraphNode) -> int:
    lat = abs(float(a.latitude) - float(b.latitude)) * 111
    lon = abs(float(a.longitude) - float(b.longitude)) * 85
    return max(3, int((lat + lon) * 3.0))


def _solve_day_assignment(
    place_ids: list[str],
    *,
    day_count: int,
    variation: int,
    fixed_days: dict[str, int] | None = None,
) -> tuple[dict[str, int], str]:
    """Assign bounded POIs to Journey days and report the optimizer actually used."""

    fixed_days = fixed_days or {}
    quotas = [
        len(place_ids) // day_count + (1 if day < len(place_ids) % day_count else 0)
        for day in range(day_count)
    ]
    fixed_counts = [sum(1 for place_id in fixed_days if fixed_days[place_id] == day) for day in range(day_count)]
    # Preserve balanced defaults while making a real fixed commitment the
    # source of truth when it needs more space than the even quota provides.
    for target, count in enumerate(fixed_counts):
        while count > quotas[target]:
            donor = next(
                (day for day in range(day_count) if day != target and quotas[day] > fixed_counts[day]),
                None,
            )
            if donor is None:
                break
            quotas[donor] -= 1
            quotas[target] += 1
    try:
        from ortools.sat.python import cp_model
    except ImportError:
        result: dict[str, int] = {}
        for index, place_id in enumerate(place_ids):
            result[place_id] = fixed_days.get(place_id, index % day_count)
        return result, "CITYNEXUS_DETERMINISTIC_ASSIGNMENT"

    model = cp_model.CpModel()
    choices: dict[tuple[int, int], Any] = {}
    for index, _place_id in enumerate(place_ids):
        literals = []
        for day in range(day_count):
            literal = model.NewBoolVar(f"poi_{index}_day_{day}")
            choices[(index, day)] = literal
            literals.append(literal)
        model.AddExactlyOne(literals)
        if place_ids[index] in fixed_days:
            model.Add(choices[(index, fixed_days[place_ids[index]])] == 1)
    for day, quota in enumerate(quotas):
        model.Add(sum(choices[(index, day)] for index in range(len(place_ids))) == quota)
    target_offset = variation % day_count
    objective = []
    for index in range(len(place_ids)):
        target = (index + target_offset) % day_count
        for day in range(day_count):
            objective.append(abs(day - target) * 1000 * choices[(index, day)])
    model.Minimize(sum(objective))
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 0.6
    solver.parameters.num_search_workers = 1
    status = solver.Solve(model)
    if status not in {cp_model.OPTIMAL, cp_model.FEASIBLE}:
        return (
            {
                place_id: fixed_days.get(place_id, index % day_count)
                for index, place_id in enumerate(place_ids)
            },
            "CITYNEXUS_DETERMINISTIC_ASSIGNMENT",
        )
    return (
        {
            place_id: next(day for day in range(day_count) if solver.Value(choices[(index, day)]))
            for index, place_id in enumerate(place_ids)
        },
        "ORTOOLS_CP_SAT",
    )


def _order_by_day(
    places: dict[str, Place], assignment: dict[str, int], *, day_count: int, variation: int,
) -> list[list[str]]:
    grouped = [[place_id for place_id, day in assignment.items() if day == index] for index in range(day_count)]
    result: list[list[str]] = []
    for day, items in enumerate(grouped):
        if len(items) <= 1:
            result.append(items)
            continue
        # The bounded permutation is intentionally tiny (the flagship uses two
        # POIs/day).  It makes order a real planner decision without creating an
        # unbounded routing model.
        candidates = list(permutations(items))
        if variation % 3 == 1:
            candidates = list(reversed(candidates))
        result.append(list(min(
            candidates,
            key=lambda order: sum(
                _distance_minutes(places[order[index - 1]], places[order[index]])
                for index in range(1, len(order))
            ),
        )))
    return result


def _structural_nodes(
    session: Session,
    journey: Journey,
    stay: AccommodationOfferSnapshot | None,
    ordered_days: list[list[str]],
    day_count: int,
    places: dict[str, Place],
    arrival: dict[str, Any],
    departure: dict[str, Any],
    c4_ids: list[str],
    commitment_anchors: dict[str, list[Commitment]],
) -> tuple[list[ComposeNode], dict[str, Place]]:
    arrival_at = datetime.fromisoformat(arrival["at"])
    departure_at = datetime.fromisoformat(departure["at"])
    center_lat, center_lon = _city_center(journey)
    arrival_place = _ensure_place(
        session=session,
        journey=journey,
        key="arrival-anchor",
        title=arrival["location"],
        category="arrival",
        latitude=float(arrival.get("latitude") or center_lat - 0.015),
        longitude=float(arrival.get("longitude") or center_lon - 0.02),
        metadata={"anchor": "ARRIVAL", "commitment_ids": c4_ids},
    )
    departure_place = _ensure_place(
        session=session,
        journey=journey,
        key="departure-anchor",
        title=departure["location"],
        category="departure",
        latitude=float(departure.get("latitude") or center_lat + 0.06),
        longitude=float(departure.get("longitude") or center_lon + 0.08),
        metadata={"anchor": "DEPARTURE"},
    )
    stay_place = None
    check_in_place = None
    check_out_place = None
    night_places: dict[int, Place] = {}
    if stay is not None:
        stay_place = _ensure_place(
            session=session,
            journey=journey,
            key=f"stay:{stay.external_offer_id}",
            title=stay.property_name,
            category="stay",
            latitude=stay.latitude,
            longitude=stay.longitude,
            metadata={"stay_offer_id": stay.id, "cost_minor": stay.total_amount_minor, "cost_status": "KNOWN"},
        )
        check_in_place = _ensure_place(
            session=session, journey=journey, key="check-in", title=f"Check in · {stay.property_name}",
            category="check-in", latitude=stay.latitude, longitude=stay.longitude,
            metadata={"stay_offer_id": stay.id},
        )
        check_out_place = _ensure_place(
            session=session, journey=journey, key="check-out", title=f"Check out · {stay.property_name}",
            category="check-out", latitude=stay.latitude, longitude=stay.longitude,
            metadata={"stay_offer_id": stay.id},
        )
        night_places = {
            day: _ensure_place(
                session=session, journey=journey, key=f"stay-night-{day}",
                title=f"Overnight · {stay.property_name}", category="stay",
                latitude=stay.latitude, longitude=stay.longitude,
                metadata={"stay_offer_id": stay.id, "night": day},
            )
            for day in range(1, day_count)
        }
    place_map = {
        **places,
        arrival_place.id: arrival_place,
        departure_place.id: departure_place,
        **({stay_place.id: stay_place} if stay_place else {}),
        **({check_in_place.id: check_in_place} if check_in_place else {}),
        **({check_out_place.id: check_out_place} if check_out_place else {}),
        **{item.id: item for item in night_places.values()},
    }
    # Node timestamps are replaced by _schedule_nodes after per-leg modes are
    # selected.  These values only provide a stable graph shape for evidence.
    placeholder = arrival_at
    nodes = [
        ComposeNode(
            arrival_place.id,
            arrival["location"],
            "ARRIVAL",
            arrival_place.latitude,
            arrival_place.longitude,
            arrival_place.id,
            0,
            arrival_at,
            arrival_at + timedelta(minutes=20),
            True,
            0,
            "FREE_KNOWN",
            {
                "commitment_ids": c4_ids,
                "provider_node_key": "anchor:arrival",
                "provider_route_role": "ARRIVAL",
            },
        ),
    ]
    if stay is not None and check_in_place is not None:
        nodes.append(
            ComposeNode(check_in_place.id, f"Check in · {stay.property_name}", "CHECK_IN", stay.latitude, stay.longitude, check_in_place.id, 0, placeholder, placeholder + timedelta(minutes=20), True, stay.total_amount_minor, "KNOWN", {"stay_offer_id": stay.id, "provider_node_key": f"stay:{stay.external_offer_id}:check-in"})
        )
    for day, ids in enumerate(ordered_days):
        for place_id in ids:
            place = places[place_id]
            cost, duration, status = _place_facts(place)
            anchors = commitment_anchors.get(place.id, [])
            anchor_start = next(
                (
                    str(_load(item.policy_json, {}).get("scheduled_start_at"))
                    for item in anchors
                    if _load(item.policy_json, {}).get("scheduled_start_at")
                ),
                None,
            )
            nodes.append(ComposeNode(
                place.id,
                place.name,
                "ACTIVITY",
                place.latitude,
                place.longitude,
                place.id,
                day,
                placeholder,
                placeholder + timedelta(minutes=duration),
                bool(anchors),
                cost,
                status,
                {
                    "place_id": place.id,
                    "category": place.category,
                    "commitment_ids": [item.id for item in anchors],
                    "commitment_anchor_start_at": anchor_start,
                    "provider_node_key": f"place:{place.id}",
                    "provider_route_role": "COMMITMENT" if anchors else "ACTIVITY",
                },
            ))
        if stay is not None and day < day_count - 1:
            night_place = night_places[day + 1]
            nodes.append(ComposeNode(night_place.id, f"Overnight · {stay.property_name}", "STAY", stay.latitude, stay.longitude, night_place.id, day, placeholder, placeholder + timedelta(minutes=20), True, 0, "FREE_KNOWN", {"stay_offer_id": stay.id, "night": day + 1, "provider_node_key": f"stay:{stay.external_offer_id}:night:{day + 1}"}))
    if stay is not None and check_out_place is not None:
        nodes.append(ComposeNode(check_out_place.id, f"Check out · {stay.property_name}", "CHECK_OUT", stay.latitude, stay.longitude, check_out_place.id, day_count - 1, placeholder, placeholder + timedelta(minutes=20), True, 0, "FREE_KNOWN", {"stay_offer_id": stay.id}))
    nodes.append(ComposeNode(departure_place.id, departure["location"], "DEPARTURE", departure_place.latitude, departure_place.longitude, departure_place.id, day_count - 1, departure_at - timedelta(minutes=10), departure_at, True, 0, "FREE_KNOWN", {"anchor": "DEPARTURE", "provider_node_key": "anchor:departure", "provider_route_role": "DEPARTURE"}))
    return nodes, place_map


def _graph_nodes(nodes: Iterable[ComposeNode]) -> dict[str, GraphNode]:
    return {
        node.key: GraphNode(
            key=node.key,
            title=node.title,
            role=node.role,
            start_at=node.start_at.isoformat(),
            end_at=node.end_at.isoformat(),
            latitude=node.latitude,
            longitude=node.longitude,
            place_key=node.place_id,
            locked=node.locked,
            estimated_cost_minor=node.cost_minor,
            cost_status=node.cost_status,
        )
        for node in nodes
    }


def _capture_mode_matrix(
    session: Session,
    journey: Journey,
    graph: dict[str, GraphNode],
    keys: list[str],
    provider_evidence: dict[str, Any] | None = None,
    *,
    require_provider: bool = False,
) -> list[MobilityFact]:
    facts: list[MobilityFact] = []
    mobility = dict(provider_evidence) if isinstance(provider_evidence, dict) else {}
    raw_provider_facts = [item for item in mobility.get("facts", []) if isinstance(item, dict)]
    if not raw_provider_facts:
        live_capture = capture_journey_mobility_matrix(
            [
                {
                    "key": key,
                    "role": graph[key].role,
                    "place_id": graph[key].place_key,
                    "latitude": graph[key].latitude,
                    "longitude": graph[key].longitude,
                }
                for key in keys
            ],
            operation_key=f"v13.5:{journey.id}:mobility",
        )
        if live_capture.get("facts"):
            mobility.update(live_capture)
            raw_provider_facts = [item for item in live_capture["facts"] if isinstance(item, dict)]

    def normalize_mode(value: Any) -> str | None:
        mode = str(value or "").upper()
        return {
            "PEDESTRIAN": "WALK",
            "WALK": "WALK",
            "AUTO": "AUTO",
            "CAR": "AUTO",
        }.get(mode)

    provider_facts: dict[tuple[str, str, str], dict[str, Any]] = {}
    for item in raw_provider_facts:
        origin = str(item.get("from_key") or "")
        destination = str(item.get("to_key") or "")
        mode = normalize_mode(item.get("mode")) or "AUTO"
        if origin and destination:
            provider_facts[(origin, destination, mode)] = item

    provider_nodes = [item for item in mobility.get("nodes", []) if isinstance(item, dict)]
    aliases = {
        str(key): str(value)
        for key, value in (mobility.get("node_aliases") or {}).items()
        if key and value
    }
    provider_node_keys: dict[str, str] = {}

    def same_coordinates(provider_node: dict[str, Any], node: GraphNode) -> bool:
        try:
            return (
                abs(float(provider_node["latitude"]) - float(node.latitude)) <= 0.002
                and abs(float(provider_node["longitude"]) - float(node.longitude)) <= 0.002
            )
        except (KeyError, TypeError, ValueError):
            return False

    for graph_key, node in graph.items():
        if graph_key in aliases:
            provider_node_keys[graph_key] = aliases[graph_key]
            continue
        place = session.get(Place, node.place_key) if node.place_key else None
        external_id = str(place.external_id) if place is not None else ""
        matches: list[dict[str, Any]] = []
        for provider_node in provider_nodes:
            provider_key = str(provider_node.get("key") or "")
            if not provider_key:
                continue
            if str(provider_node.get("canonical_key") or "") == graph_key and same_coordinates(provider_node, node):
                matches.append(provider_node)
                continue
            if str(provider_node.get("place_id") or "") == str(node.place_key or "") and same_coordinates(provider_node, node):
                matches.append(provider_node)
                continue
            if external_id and str(provider_node.get("external_id") or "") == external_id and same_coordinates(provider_node, node):
                matches.append(provider_node)
                continue
            role = str(provider_node.get("role") or "").upper()
            if role and role == node.role and node.role in {"ARRIVAL", "DEPARTURE"} and same_coordinates(provider_node, node):
                matches.append(provider_node)
        if matches:
            provider_node_keys[graph_key] = str(matches[0].get("key"))
            continue
        if any((origin == graph_key or destination == graph_key) for origin, destination, _ in provider_facts):
            provider_node_keys[graph_key] = graph_key

    provider_provenance = str(
        mobility.get("route_provenance") or mobility.get("provenance") or "CAPTURED_REPLAY"
    )
    if provider_provenance not in {"CAPTURED_REPLAY", "LIVE_CAPTURE"}:
        provider_provenance = "CAPTURED_REPLAY"
    route_provider = str(mobility.get("provider") or "valhalla")

    def captured_fact(origin: str, destination: str, mode: str) -> MobilityFact | None:
        source = provider_facts.get(
            (provider_node_keys.get(origin, origin), provider_node_keys.get(destination, destination), mode)
        )
        if source is None:
            return None
        try:
            duration = max(0, int(source["duration_min"]))
            raw_distance = source.get("distance_m")
            if raw_distance is None:
                raw_distance = float(source.get("distance_km", 0)) * 1000
            distance = max(0, round(float(raw_distance)))
        except (KeyError, TypeError, ValueError):
            return None
        captured_at = str(source.get("captured_at") or mobility.get("captured_at") or _now())
        provenance = str(source.get("route_provenance") or source.get("provenance") or provider_provenance)
        if provenance not in {"CAPTURED_REPLAY", "LIVE_CAPTURE"}:
            provenance = provider_provenance
        route_geometry = source.get("route_geometry") or source.get("geometry_coordinates") or []
        if isinstance(source.get("geometry"), dict):
            geometry = source["geometry"]
        else:
            geometry = {
                "type": "Feature",
                "properties": {
                    "provider": route_provider,
                    "source_node_from": provider_node_keys.get(origin, origin),
                    "source_node_to": provider_node_keys.get(destination, destination),
                    "route_provenance": provenance,
                    "cost_provenance": str(
                        source.get("cost_provenance")
                        or mobility.get("cost_provenance")
                        or "CITYNEXUS_COST_ESTIMATE"
                    ),
                    "time_basis": str(
                        source.get("time_basis")
                        or mobility.get("time_basis")
                        or "VALHALLA_CAPTURED_MATRIX"
                    ),
                },
                "geometry": {"type": "LineString", "coordinates": route_geometry},
            }
        is_walk = mode == "WALK"
        cost_amount = 0 if is_walk else int(source.get("cost_amount_minor", 1200))
        cost_status = "FREE_KNOWN" if is_walk else str(source.get("cost_status") or "ESTIMATED")
        cost_provenance = str(
            source.get("cost_provenance")
            or mobility.get("cost_provenance")
            or "CITYNEXUS_COST_ESTIMATE"
        )
        return MobilityFact(
            id=str(uuid4()),
            journey_id=journey.id,
            option_id=None,
            from_key=origin,
            to_key=destination,
            mode=mode,
            duration_min=duration,
            distance_m=distance,
            cost_amount_minor=cost_amount,
            currency="CNY",
            cost_status=cost_status,
            walking_min=duration if is_walk else 0,
            transfers=0,
            weather_exposure="LOW",
            reliability_score=0.9,
            provider_key=str(source.get("route_provider") or route_provider),
            provenance=provenance,
            geometry_json=_json(geometry),
            crs="WGS84",
            paid_mode=not is_walk,
            evidence_json=_json(
                {
                    "fact_type": "valhalla_matrix_route",
                    "route_provider": str(source.get("route_provider") or route_provider),
                    "route_provenance": provenance,
                    "fact_quality": source.get("fact_quality"),
                    "cost_provenance": cost_provenance,
                    "source_node_from": provider_node_keys.get(origin, origin),
                    "source_node_to": provider_node_keys.get(destination, destination),
                    "cost_status": cost_status,
                    "cost_basis": cost_provenance,
                    "time_basis": str(
                        source.get("time_basis")
                        or mobility.get("time_basis")
                        or "VALHALLA_CAPTURED_MATRIX"
                    ),
                    "mode": mode,
                }
            ),
            captured_at=captured_at,
            expires_at=source.get("expires_at") or mobility.get("expires_at"),
        )

    # Capture the complete directed matrix for the bounded node set.  Trip
    # Studio is allowed to reorder unlocked activities later, so a selected
    # route-only snapshot would silently turn an edit into a second provider
    # lookup.  The deterministic gate consumes this frozen matrix instead.
    provider_modes = sorted({mode for _, _, mode in provider_facts})
    modes_to_capture = provider_modes or ["WALK", "AUTO"]
    for origin in keys:
        for destination in keys:
            if origin == destination:
                continue
            for mode in (modes_to_capture if require_provider else MODES):
                existing = session.scalar(
                    select(MobilityFact).where(
                        MobilityFact.journey_id == journey.id,
                        MobilityFact.from_key == origin,
                        MobilityFact.to_key == destination,
                        MobilityFact.mode == mode,
                    )
                )
                captured = captured_fact(origin, destination, mode)
                if captured is not None:
                    if existing is None:
                        existing = captured
                        session.add(existing)
                    elif existing.provenance != "CAPTURED_REPLAY":
                        for field in (
                            "duration_min",
                            "distance_m",
                            "cost_amount_minor",
                            "currency",
                            "cost_status",
                            "walking_min",
                            "transfers",
                            "weather_exposure",
                            "reliability_score",
                            "provider_key",
                            "provenance",
                            "geometry_json",
                            "crs",
                            "paid_mode",
                            "evidence_json",
                            "captured_at",
                            "expires_at",
                        ):
                            setattr(existing, field, getattr(captured, field))
                elif require_provider:
                    raise ValueError(
                        f"V13_FULL_GROUNDING_MISSING_MOBILITY_PAIR:{origin}->{destination}"
                    )
                elif existing is None:
                    existing = _mode_fact(
                        journey.id,
                        None,
                        graph[origin],
                        graph[destination],
                        mode,
                        journey.budget_currency,
                    )
                    session.add(existing)
                if existing is not None:
                    facts.append(existing)
            if require_provider and not any(
                item.from_key == origin and item.to_key == destination and item.provenance in {"CAPTURED_REPLAY", "LIVE_CAPTURE"}
                for item in facts
            ):
                raise ValueError(
                    f"V13_FULL_GROUNDING_MISSING_MOBILITY_PAIR:{origin}->{destination}"
                )
    session.flush()
    return facts


def _select_modes(
    facts: list[MobilityFact],
    budget_minor: int,
    variation: int,
    *,
    require_provider: bool = False,
) -> dict[tuple[str, str], MobilityFact]:
    solved = solve_mode_choices(facts, budget_minor=max(0, budget_minor))
    selected = list(solved.selected)
    by_leg: dict[tuple[str, str], list[MobilityFact]] = {}
    for item in facts:
        by_leg.setdefault((item.from_key, item.to_key), []).append(item)
    selected_by_leg = {(item.from_key, item.to_key): item for item in selected}
    # When the operation carries a sealed Valhalla fact for a concrete leg,
    # keep that provider fact in the authoritative plan. Synthetic modes remain
    # available only for pairs the capture did not cover.
    for pair, choices in by_leg.items():
        captured = [
            item for item in choices if item.provenance in {"CAPTURED_REPLAY", "LIVE_CAPTURE"}
        ]
        if captured:
            selected_by_leg[pair] = min(
                captured,
                key=lambda item: (int(item.duration_min), int(item.cost_amount_minor or 0)),
            )
    if require_provider or variation % 3 == 0:
        return selected_by_leg
    spent = sum(int(item.cost_amount_minor or 0) for item in selected_by_leg.values())
    for index, item in enumerate(list(selected_by_leg.values())):
        alternatives = [
            row for row in by_leg[(item.from_key, item.to_key)]
            if row.mode != item.mode
            and row.cost_status != "UNKNOWN"
            and row.provenance not in {"CAPTURED_REPLAY", "LIVE_CAPTURE"}
        ]
        if alternatives and index % 2 == variation % 2:
            replacement = min(alternatives, key=lambda row: (row.duration_min, row.cost_amount_minor or 0))
            replacement_cost = int(replacement.cost_amount_minor or 0)
            current_cost = int(item.cost_amount_minor or 0)
            if spent - current_cost + replacement_cost <= max(0, int(budget_minor)):
                selected_by_leg[(item.from_key, item.to_key)] = replacement
                spent = spent - current_cost + replacement_cost
    return selected_by_leg


def _schedule_nodes(
    nodes: list[ComposeNode],
    ordered_days: list[list[str]],
    place_by_id: dict[str, Place],
    selected_modes: dict[tuple[str, str], MobilityFact],
    journey: Journey,
    arrival: dict[str, Any],
    departure: dict[str, Any],
    *,
    day_count: int,
    stay: AccommodationOfferSnapshot | None,
    rhythm: DayRhythm | None = None,
) -> list[ComposeNode]:
    by_key = {node.key: node for node in nodes}
    arrival_key = next(node.key for node in nodes if node.role == "ARRIVAL")
    check_in_key = next((node.key for node in nodes if node.role == "CHECK_IN"), None)
    check_out_key = next((node.key for node in nodes if node.role == "CHECK_OUT"), None)
    departure_key = next(node.key for node in nodes if node.role == "DEPARTURE")
    night_keys = [node.key for node in nodes if node.role == "STAY"]
    arrival_at = datetime.fromisoformat(arrival["at"])
    departure_at = datetime.fromisoformat(departure["at"])
    scheduled: list[ComposeNode] = []

    def add(key: str, start: datetime, end: datetime) -> None:
        node = by_key[key]
        scheduled.append(ComposeNode(node.key, node.title, node.role, node.latitude, node.longitude, node.place_id, node.day_index, start, end, node.locked, node.cost_minor, node.cost_status, node.metadata))

    add(arrival_key, arrival_at, arrival_at + timedelta(minutes=20))
    previous_key = arrival_key
    cursor = arrival_at + timedelta(minutes=20)
    if stay is not None and check_in_key is not None:
        check_in_start = max(
            cursor + timedelta(minutes=20),
            datetime.combine(arrival_at.date(), time(14, 0), tzinfo=arrival_at.tzinfo),
        )
        if check_in_start + timedelta(minutes=20) > departure_at:
            raise ValueError("V13_STAY_CHECK_IN_OUTSIDE_JOURNEY")
        leg = selected_modes[(previous_key, check_in_key)]
        check_in_start = max(check_in_start, cursor + timedelta(minutes=leg.duration_min))
        add(check_in_key, check_in_start, check_in_start + timedelta(minutes=20))
        previous_key, cursor = check_in_key, check_in_start + timedelta(minutes=20)

    for day, ids in enumerate(ordered_days):
        if day and night_keys:
            previous_key = night_keys[day - 1]
            cursor = max(cursor, datetime.combine(arrival_at.date() + timedelta(days=day), time(8, 30), tzinfo=arrival_at.tzinfo))
        else:
            cursor = max(cursor, arrival_at + timedelta(minutes=45))
        for place_id in ids:
            key = place_id
            place = place_by_id[place_id]
            metadata = _load(place.metadata_json, {})
            _cost, duration, _status = _place_facts(place)
            leg = selected_modes[(previous_key, key)]
            earliest_travel_start = cursor + timedelta(minutes=leg.duration_min)
            start = earliest_travel_start
            earliest = datetime.combine(start.date(), time(9, 0), tzinfo=start.tzinfo)
            if day == 0:
                earliest = max(earliest, arrival_at + timedelta(minutes=45))
            anchor_start_raw = (by_key[key].metadata or {}).get("commitment_anchor_start_at")
            if anchor_start_raw:
                start = datetime.fromisoformat(str(anchor_start_raw))
                expected_date = arrival_at.date() + timedelta(days=day)
                if start.date() != expected_date:
                    raise ValueError(f"V13_COMMITMENT_ANCHOR_DAY_CONFLICT:{key}")
                if earliest_travel_start > start:
                    raise ValueError(f"V13_COMMITMENT_ANCHOR_TRAVEL_BUFFER:{key}")
                if start < earliest:
                    raise ValueError(f"V13_COMMITMENT_ANCHOR_TOO_EARLY:{key}")
            else:
                start = max(start, earliest)
                if day == day_count - 1:
                    latest = departure_at - timedelta(minutes=duration + 30)
                    if start > latest:
                        raise ValueError(f"V13_ACTIVITY_OUTSIDE_DEPARTURE_WINDOW:{place_id}")
            end = start + timedelta(minutes=duration)
            if end >= departure_at - timedelta(minutes=30):
                raise ValueError(f"V13_ACTIVITY_OUTSIDE_DEPARTURE_WINDOW:{place_id}")
            opening = metadata.get("opening_hours") or _load(place.opening_hours_json, {})
            if isinstance(opening, dict) and opening.get("open"):
                hour, minute = (int(part) for part in str(opening["open"]).split(":", 1))
                opening_start = datetime.combine(start.date(), time(hour, minute), tzinfo=start.tzinfo)
                if anchor_start_raw and start < opening_start:
                    raise ValueError(f"V13_COMMITMENT_OUTSIDE_OPENING_HOURS:{place_id}")
                start = max(start, opening_start)
                end = start + timedelta(minutes=duration)
            if rhythm is not None and not by_key[key].locked and not (by_key[key].metadata or {}).get("commitment_ids"):
                wake_at = datetime.combine(
                    start.date(),
                    time(rhythm.wake_minute // 60, rhythm.wake_minute % 60),
                    tzinfo=start.tzinfo,
                )
                start = max(start, wake_at)
                bedtime = datetime.combine(
                    start.date(),
                    time(rhythm.bed_minute // 60, rhythm.bed_minute % 60),
                    tzinfo=start.tzinfo,
                )
                if rhythm.bed_minute <= rhythm.wake_minute:
                    bedtime += timedelta(days=1)
                end = start + timedelta(minutes=duration)
                if end > bedtime:
                    raise ValueError(f"V15_DAY_RHYTHM_OUTSIDE_WAKE_WINDOW:{place_id}")
            add(key, start, end)
            previous_key, cursor = key, end
        if stay is not None and day < day_count - 1:
            night_key = night_keys[day]
            leg = selected_modes[(previous_key, night_key)]
            default_night_start = datetime.combine(
                arrival_at.date() + timedelta(days=day),
                time(22, 30),
                tzinfo=arrival_at.tzinfo,
            )
            sleep_minutes = 10 * 60
            if rhythm is not None:
                default_night_start = datetime.combine(
                    arrival_at.date() + timedelta(days=day),
                    time(rhythm.bed_minute // 60, rhythm.bed_minute % 60),
                    tzinfo=arrival_at.tzinfo,
                )
                if rhythm.bed_minute <= rhythm.wake_minute:
                    default_night_start += timedelta(days=1)
                sleep_minutes = rhythm.overnight_minutes
            night_start = max(cursor + timedelta(minutes=leg.duration_min), default_night_start)
            night_end = night_start + timedelta(minutes=sleep_minutes)
            add(night_key, night_start, night_end)
            previous_key, cursor = night_key, night_end

    if stay is not None and check_out_key is not None:
        leg = selected_modes[(previous_key, check_out_key)]
        departure_leg = selected_modes[(check_out_key, departure_key)]
        # Size the final hotel -> departure-anchor window from the frozen route
        # fact, rather than assuming a two-hour checkout window.  This is the
        # deterministic protected-anchor slack gate for the authoritative plan.
        latest_checkout_start = departure_at - timedelta(
            minutes=30 + 20 + departure_leg.duration_min
        )
        checkout_start = max(cursor + timedelta(minutes=leg.duration_min), latest_checkout_start)
        if checkout_start + timedelta(minutes=20) > departure_at - timedelta(minutes=30):
            raise ValueError("V13_CHECK_OUT_DEPARTURE_BUFFER")
        add(check_out_key, checkout_start, checkout_start + timedelta(minutes=20))
        departure_start = max(
            checkout_start + timedelta(minutes=20) + timedelta(minutes=departure_leg.duration_min),
            departure_at - timedelta(minutes=10),
        )
    else:
        departure_leg = selected_modes[(previous_key, departure_key)]
        departure_start = max(
            cursor + timedelta(minutes=departure_leg.duration_min),
            departure_at - timedelta(minutes=10),
        )
    if departure_start > departure_at:
        raise ValueError("V12_DEPARTURE_ANCHOR_INFEASIBLE")
    add(departure_key, departure_start, departure_at)
    return scheduled


def _evidence(
    facts: list[MobilityFact],
    selected_modes: dict[tuple[str, str], MobilityFact],
    *,
    journey: Journey,
    operation_key: str,
    stay: AccommodationOfferSnapshot | None,
    discovery_evidence: dict[str, Any] | None = None,
) -> tuple[str, dict[str, Any]]:
    selected = list(selected_modes.values())

    def fact_payload(item: MobilityFact) -> dict[str, Any]:
        geometry = _load(item.geometry_json, {})
        fact_evidence = _load(item.evidence_json, {})
        route_geometry = []
        if isinstance(geometry, dict):
            route_geometry = geometry.get("geometry", {}).get("coordinates", [])
        return {
            "origin_key": item.from_key,
            "destination_key": item.to_key,
            "mode": item.mode,
            "minutes": item.duration_min,
            "duration_min": item.duration_min,
            "distance_m": item.distance_m,
            "cost_amount_minor": item.cost_amount_minor,
            "currency": item.currency,
            "cost_status": item.cost_status,
            "walking_min": item.walking_min,
            "transfers": item.transfers,
            "reliability_score": item.reliability_score,
            "weather_exposure": item.weather_exposure,
            "provider": item.provider_key,
            "provider_key": item.provider_key,
            "provenance": item.provenance,
            "route_provider": fact_evidence.get("route_provider") or item.provider_key,
            "route_provenance": fact_evidence.get("route_provenance") or item.provenance,
            "fact_quality": fact_evidence.get("fact_quality"),
            "cost_provenance": fact_evidence.get("cost_provenance") or "CITYNEXUS_COST_ESTIMATE",
            "time_basis": fact_evidence.get("time_basis") or "FROZEN_MOBILITY_FACT",
            "provider_node_from": fact_evidence.get("source_node_from") or item.from_key,
            "provider_node_to": fact_evidence.get("source_node_to") or item.to_key,
            "route_geometry": route_geometry,
            "geometry": geometry,
            "crs": item.crs,
            "fact_id": item.id,
            "captured_at": item.captured_at,
            "expires_at": item.expires_at,
        }

    matrix_payload = [fact_payload(item) for item in facts]
    selected_payload = [fact_payload(item) for item in selected]
    provenances = {str(item.provenance) for item in facts}
    selected_provenances = {str(item.provenance) for item in selected}
    provider_provenances = {"CAPTURED_REPLAY", "LIVE_CAPTURE"}
    require_fully_grounded = bool(
        isinstance(discovery_evidence, dict)
        and discovery_evidence.get("require_fully_grounded")
    )
    selected_route_provider_grounded = bool(selected) and selected_provenances.issubset(
        provider_provenances
    )
    edit_matrix_provider_grounded = bool(facts) and provenances.issubset(provider_provenances)
    if provenances == {"SYNTHETIC_FIXTURE"}:
        selected_provider_path = "SYNTHETIC_FIXTURE"
        provider_fallback_semantics = (
            "bounded deterministic authoritative fixture; not live production provider evidence"
        )
    elif require_fully_grounded and selected_route_provider_grounded:
        selected_provider_path = "FULLY_GROUNDED_JOURNEY"
        provider_fallback_semantics = "Journey-specific route matrix and cost evidence are frozen before feasibility"
    elif provenances and provenances.issubset({"CAPTURED_REPLAY", "LIVE_CAPTURE"}):
        selected_provider_path = "CAPTURED_PROVIDER_FACTS"
        provider_fallback_semantics = "provider facts are sealed before deterministic feasibility"
    else:
        # A matrix is only as trustworthy as its weakest fact.  Do not promote
        # a partially captured matrix to a single-provider claim when a test,
        # fallback, or legacy row contributes synthetic data.
        selected_provider_path = "MIXED_PROVIDER_FACTS"
        provider_fallback_semantics = (
            "mixed provider provenance; each fact remains individually labelled and the matrix is not a single live/replay claim"
        )
    if require_fully_grounded and selected_route_provider_grounded:
        runtime_label = "FULLY_GROUNDED_JOURNEY"
    elif provenances and provenances.issubset({"CAPTURED_REPLAY", "LIVE_CAPTURE"}):
        runtime_label = "SEALED_REAL_PROVIDER_REPLAY"
    elif "CAPTURED_REPLAY" in provenances or "LIVE_CAPTURE" in provenances:
        runtime_label = "SEALED_REAL_REPLAY_WITH_EXPLICIT_SYNTHETIC_FALLBACK"
    else:
        runtime_label = "SEALED_REPLAY_OR_SYNTHETIC_FIXTURE_EXPLICIT"
    identity = _json({"journey": journey.id, "operation": operation_key, "fact_ids": [item["fact_id"] for item in matrix_payload], "stay": stay.id if stay else None})
    snapshot_id = "v12-authoritative-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]
    evidence = {
        "snapshot_id": snapshot_id,
        "operation_key": operation_key,
        "captured_at": min((item.captured_at for item in facts), default=_now()),
        "expires_at": min((item.expires_at for item in facts if item.expires_at), default=(stay.expires_at if stay else None)),
        "source_provider": ";".join(sorted({item.provider_key for item in facts})),
        "aggregate_provenance": ";".join(sorted({item.provenance for item in facts})),
        "facts": matrix_payload,
        "selected_facts": selected_payload,
        "mode_matrix_count": len(facts),
        "stay_offer_id": stay.id if stay else None,
        "stay_evidence": _load(stay.evidence_json, {}) if stay else {},
        "normalizer_version": "citynexus-v13-authoritative-evidence-1",
        "runtime_label": runtime_label,
        "provider_evidence": {
            "selected_path": selected_provider_path,
            "provenance_components": sorted(provenances),
            "selected_fact_provenances": sorted({str(item.provenance) for item in selected}),
            "selected_route_provider_grounded": selected_route_provider_grounded,
            "edit_matrix_provider_grounded": edit_matrix_provider_grounded,
            "fully_provider_grounded": bool(provenances) and provenances.issubset(provider_provenances),
            "synthetic_fact_count": sum(1 for item in facts if item.provenance == "SYNTHETIC_FIXTURE"),
            "selected_synthetic_fact_count": sum(
                1 for item in selected if item.provenance == "SYNTHETIC_FIXTURE"
            ),
            "route_provenances": sorted(
                {str(item.get("route_provenance")) for item in selected_payload}
            ),
            "cost_provenances": sorted(
                {str(item.get("cost_provenance")) for item in selected_payload}
            ),
            "provider_node_set": sorted(
                {
                    str(value)
                    for item in selected_payload
                    for value in (item.get("provider_node_from"), item.get("provider_node_to"))
                    if value
                }
            ),
            "fully_grounded": require_fully_grounded and selected_route_provider_grounded,
            "preferred_paths": ["SEALED_REAL_REPLAY", "LIVE_CAPTURE"],
            "fallback_semantics": provider_fallback_semantics,
        },
        "discovery": discovery_evidence or {},
    }
    evidence["integrity_hash"] = hashlib.sha256(
        _json(evidence).encode("utf-8")
    ).hexdigest()
    return snapshot_id, evidence


def _temporal_shape(arrival_at: datetime, departure_at: datetime) -> tuple[int, int]:
    """Return the canonical calendar day/night shape for the Journey."""

    shape = derive_journey_temporal_shape(
        arrival_at,
        departure_at,
        min_day_count=MIN_DAY_COUNT,
        max_day_count=MAX_DAY_COUNT,
    )
    return shape.day_count, shape.night_count


def _commitment_anchor_inputs(
    commitments: list[Commitment],
    *,
    selected_ids: list[str],
    arrival_at: datetime,
    day_count: int,
) -> tuple[dict[str, list[Commitment]], dict[str, int]]:
    """Resolve existing C4 semantic anchors without creating a new system."""

    anchors: dict[str, list[Commitment]] = {}
    fixed_days: dict[str, int] = {}
    selected = set(selected_ids)
    for commitment in commitments:
        policy = _load(commitment.policy_json, {})
        semantic_key = str(policy.get("semantic_key") or policy.get("semantic_node_key") or "")
        anchor_role = str(policy.get("anchor_role") or "").upper()
        semantic_node_key = str(policy.get("semantic_node_key") or "")
        # Structural reservation anchors are not selected POIs. Their
        # planner-local semantic_key can legitimately differ between V9 and
        # V12, so identify them by the durable role/node identity first.
        if (
            anchor_role in {"ARRIVAL", "DEPARTURE"}
            or semantic_node_key in {"arrival-anchor", "departure-anchor"}
            or semantic_key in {"arrival-anchor", "departure-anchor"}
            or semantic_key.endswith(":arrival-anchor")
            or semantic_key.endswith(":departure-anchor")
        ):
            continue
        if not semantic_key:
            continue
        if semantic_key not in selected:
            if policy.get("scheduled_start_at"):
                raise ValueError(f"V13_C4_ANCHOR_NOT_IN_INPUT:{semantic_key}")
            continue
        anchors.setdefault(semantic_key, []).append(commitment)
        scheduled = policy.get("scheduled_start_at")
        if scheduled:
            try:
                start_at = datetime.fromisoformat(str(scheduled))
            except ValueError as exc:
                raise ValueError(f"V13_INVALID_C4_ANCHOR_TIME:{commitment.id}") from exc
            offset = (start_at.date() - arrival_at.date()).days
            if offset < 0 or offset >= day_count:
                raise ValueError(f"V13_C4_ANCHOR_OUTSIDE_JOURNEY:{semantic_key}")
            fixed_days[semantic_key] = offset
    return anchors, fixed_days


def compose_authoritative_multiday(
    session: Session,
    journey: Journey,
    *,
    place_ids: Iterable[str],
    candidate_count: int = 3,
    stay_offer_id: str | None = None,
    reservation_id: str | None = None,
    discovery_evidence: dict[str, Any] | None = None,
) -> ComposeResult:
    selected_ids = list(dict.fromkeys(str(item) for item in place_ids))
    if len(selected_ids) < 2:
        raise ValueError("V12_COMPOSE_REQUIRES_TWO_POIS")
    context = session.get(JourneyTripContext, journey.id)
    arrival = _anchor(context, journey, "arrival")
    departure = _anchor(context, journey, "departure")
    require_fully_grounded = bool(
        isinstance(discovery_evidence, dict)
        and discovery_evidence.get("require_fully_grounded")
    )
    if discovery_evidence:
        anchor_options = discovery_evidence.get("anchor_options")
        arrival_anchor = next(
            (item for item in anchor_options or [] if item.get("anchor_key") == "ARRIVAL"),
            None,
        )
        departure_anchor = next(
            (item for item in anchor_options or [] if item.get("anchor_key") == "DEPARTURE"),
            None,
        )
        fallback_anchor = discovery_evidence.get("anchor")
        if not isinstance(fallback_anchor, dict):
            fallback_anchor = next(
                (
                    item
                    for item in discovery_evidence.get("candidates", [])
                    if isinstance(item, dict)
                ),
                None,
            ) or {}
        for target, role_anchor in ((arrival, arrival_anchor), (departure, departure_anchor)):
            server_anchor = role_anchor if isinstance(role_anchor, dict) else fallback_anchor
            if target.get("latitude") is not None or server_anchor.get("latitude") is None:
                continue
            target.update(
                {
                    "latitude": server_anchor.get("latitude"),
                    "longitude": server_anchor.get("longitude"),
                    "location": target.get("location") or server_anchor.get("location") or "Journey anchor",
                }
            )
        if arrival.get("latitude") is None and fallback_anchor.get("latitude") is not None:
            arrival.update(
                {
                    "latitude": fallback_anchor.get("latitude"),
                    "longitude": fallback_anchor.get("longitude"),
                    "location": arrival.get("location") or fallback_anchor.get("location") or "Journey anchor",
                }
            )
        if departure.get("latitude") is None and fallback_anchor.get("latitude") is not None:
            departure.update(
                {
                    "latitude": fallback_anchor.get("latitude"),
                    "longitude": fallback_anchor.get("longitude"),
                    "location": departure.get("location") or fallback_anchor.get("location") or "Journey departure",
                }
            )
    arrival_at = datetime.fromisoformat(arrival["at"])
    departure_at = datetime.fromisoformat(departure["at"])
    day_count, night_count = _temporal_shape(arrival_at, departure_at)
    places = {
        item.id: item
        for item in session.scalars(
            select(Place).where(Place.id.in_(selected_ids), Place.city_code == journey.city_code)
        ).all()
    }
    missing = [item for item in selected_ids if item not in places]
    if missing:
        raise ValueError("V12_COMPOSE_POI_NOT_FOUND:" + ",".join(missing))
    for place in places.values():
        _place_facts(place)
    offers = capture_accommodation_offers(session, journey) if night_count else []
    if night_count and not offers:
        raise ValueError("V13_STAY_CANDIDATE_UNAVAILABLE")
    if not night_count and stay_offer_id:
        raise ValueError("V13_DAY_TRIP_CANNOT_SELECT_STAY")
    stay = next((item for item in offers if stay_offer_id and item.id == stay_offer_id), offers[0] if offers else None)
    commitments = list(session.scalars(select(Commitment).where(Commitment.journey_id == journey.id, Commitment.status == "ACTIVE", Commitment.level == "C4_Anchored")).all())
    c4_ids = [item.id for item in commitments]
    commitment_anchors, fixed_days = _commitment_anchor_inputs(
        commitments,
        selected_ids=selected_ids,
        arrival_at=arrival_at,
        day_count=day_count,
    )
    if reservation_id:
        reservation = session.get(TransportReservation, reservation_id)
        reservation_commitment_id = reservation.commitment_id if reservation else None
        if not any(item.id == reservation_id or item.id == reservation_commitment_id for item in commitments):
            raise ValueError("V12_RESERVATION_NOT_PROTECTED")
    result: list[PlanVersion] = []
    all_evidence_ids: list[str] = []
    rhythm = _journey_day_rhythm(session, journey)
    for variation in range(max(1, min(5, candidate_count))):
        assignment, optimizer_kind = _solve_day_assignment(
            selected_ids,
            day_count=day_count,
            variation=variation,
            fixed_days=fixed_days,
        )
        ordered_days = _order_by_day(places, assignment, day_count=day_count, variation=variation)
        structural, place_map = _structural_nodes(
            session,
            journey,
            stay,
            ordered_days,
            day_count,
            places,
            arrival,
            departure,
            c4_ids,
            commitment_anchors,
        )
        structural_keys = [item.key for item in structural]
        graph = _graph_nodes(structural)
        facts = _capture_mode_matrix(
            session,
            journey,
            graph,
            structural_keys,
            provider_evidence=(
                discovery_evidence.get("provider_mobility")
                if isinstance(discovery_evidence, dict)
                else None
            ),
            require_provider=require_fully_grounded,
        )
        route_pairs = set(zip(structural_keys, structural_keys[1:]))
        route_facts = [item for item in facts if (item.from_key, item.to_key) in route_pairs]
        poi_cost = sum(_place_facts(place)[0] for place in places.values())
        selected_modes = _select_modes(
            route_facts,
            max(0, int(journey.budget_amount_minor) - int(stay.total_amount_minor if stay else 0) - poi_cost),
            variation,
            require_provider=require_fully_grounded,
        )
        try:
            scheduled = _schedule_nodes(
                structural,
                ordered_days,
                place_map,
                selected_modes,
                journey,
                arrival,
                departure,
                    day_count=day_count,
                    stay=stay,
                    rhythm=rhythm,
                )
        except ValueError:
            if require_fully_grounded:
                continue
            raise
        total_cost = int(stay.total_amount_minor if stay else 0) + poi_cost + sum(int(item.cost_amount_minor or 0) for item in selected_modes.values())
        if total_cost > int(journey.budget_amount_minor):
            raise ValueError("V12_COMPOSE_BUDGET_EXCEEDED")
        snapshot_id, evidence = _evidence(
            facts,
            selected_modes,
            journey=journey,
            operation_key=f"v12-compose:{journey.id}:{variation}",
            stay=stay,
            discovery_evidence=discovery_evidence,
        )
        now = _now()
        latest_version = int(session.scalar(select(PlanVersion.version_no).where(PlanVersion.journey_id == journey.id).order_by(PlanVersion.version_no.desc()).limit(1)) or 0)
        plan = PlanVersion(
            id=str(uuid4()),
            journey_id=journey.id,
            version_no=latest_version + 1,
            planning_mode=journey.planning_mode,
            status="CANDIDATE",
            source="INITIAL",
            parent_plan_version_id=None,
            total_cost_minor=total_cost,
            currency=journey.budget_currency,
            # Keep the PlanVersion metric aligned with Trip Studio's frozen
            # route accounting: activity dwell plus the selected concrete
            # mobility legs.  A no-op edit must therefore have zero time
            # delta, while a reordered plan reflects its changed route cost.
            estimated_duration_min=(
                sum(int((item.end_at - item.start_at).total_seconds() // 60) for item in scheduled)
                + sum(int(item.duration_min or 0) for item in selected_modes.values())
            ),
            utility_score=max(0.0, min(1.0, 0.96 - 0.04 * variation)),
            score_breakdown_json=_json({
                "commitment_preservation": 1.0,
                "stability": 0.94 - (0.01 * variation),
                "time_reliability": 0.92,
                "preference_fit": 0.84,
                "mobility_fit": 0.88,
                "budget_fit": max(0.0, min(1.0, 1.0 - (total_cost / max(1, int(journey.budget_amount_minor))))),
                "robustness": 0.86,
                "opportunity_value": 0.9,
                "total": max(0.0, min(1.0, 0.96 - 0.04 * variation)),
                "optimizer": optimizer_kind,
                "day_assignment": assignment,
                "days": day_count,
                "nights": night_count,
                "stay": stay.total_amount_minor if stay else 0,
                "mobility": sum(int(item.cost_amount_minor or 0) for item in selected_modes.values()),
                "protected_c4": c4_ids,
                "day_rhythm": rhythm.as_dict() if rhythm else None,
            }),
            explanation_json=_json({"product_path": "V13_AUTHORITATIVE_BOUNDED_MULTIDAY", "optimizer_kind": optimizer_kind, "complete": True, "days": day_count, "nights": night_count, "poi_ids": selected_ids, "day_assignment": assignment, "stay_offer_id": stay.id if stay else None, "reservation_id": reservation_id, "protected_commitment_ids": c4_ids, "mid_trip_commitment_ids": [item.id for values in commitment_anchors.values() for item in values], "mobility_snapshot_id": snapshot_id, "deterministic_gate": "PASSED", "evidence_runtime": evidence.get("runtime_label"), "day_rhythm": rhythm.as_dict() if rhythm else None, "discovery": discovery_evidence or {}}),
            mobility_snapshot_id=snapshot_id,
            mobility_evidence_json=_json(evidence),
            created_at=now,
        )
        session.add(plan)
        session.flush()
        activity_rows: list[Activity] = []
        for sequence, node in enumerate(scheduled, start=1):
            metadata = dict(node.metadata or {})
            metadata.update({"v12_day": node.day_index + 1, "execution_state": "NOT_STARTED", "cost_status": node.cost_status, "plan_evidence_id": snapshot_id, "semantic_key": node.key})
            if node.role == "ARRIVAL":
                metadata["protected_commitment_ids"] = c4_ids
            row = Activity(
                id=str(uuid4()), plan_version_id=plan.id, sequence_no=sequence, activity_type=node.role.lower(), title=node.title, place_id=node.place_id, city_service_id=None, start_at=node.start_at.isoformat(), end_at=node.end_at.isoformat(), travel_from_prev_min=0 if sequence == 1 else selected_modes[(scheduled[sequence - 2].key, node.key)].duration_min, estimated_cost_minor=node.cost_minor, currency=journey.budget_currency, status="NOT_STARTED", locked=node.locked, metadata_json=_json(metadata), created_at=now, updated_at=now,
            )
            session.add(row)
            activity_rows.append(row)
        session.flush()
        # Older reservation imports carry only semantic_node_key (for example
        # arrival-anchor). Persist the canonical V12 place key once it is
        # known, so the existing Guardian/replan tools can consume this plan's
        # frozen matrix without a second provider lookup.
        node_by_legacy_key = {
            node.key.rsplit(":", 1)[-1]: node
            for node in scheduled
            if node.role in {"ARRIVAL", "DEPARTURE"}
        }
        for commitment in commitments:
            policy = _load(commitment.policy_json, {})
            legacy_key = str(policy.get("semantic_node_key") or "")
            anchor_role = str(policy.get("anchor_role") or "").upper()
            node = node_by_legacy_key.get(legacy_key)
            if node is None:
                continue
            # ARRIVAL/DEPARTURE semantic keys are planner-local concrete Place
            # identities. Refresh them when V12 materializes the structural
            # node even when V9 previously wrote its own canonical Place id.
            # MID_TRIP semantic keys remain the user-selected POI identity.
            if (
                anchor_role in {"ARRIVAL", "DEPARTURE"}
                or legacy_key in {"arrival-anchor", "departure-anchor"}
                or not policy.get("semantic_key")
            ):
                policy["semantic_key"] = node.place_id
                commitment.policy_json = _json(policy)
                commitment.updated_at = _now()
        for sequence, (from_node, to_node) in enumerate(zip(scheduled, scheduled[1:]), start=1):
            fact = selected_modes[(from_node.key, to_node.key)]
            session.add(JourneyLeg(id=str(uuid4()), journey_id=journey.id, plan_version_id=plan.id, sequence_no=sequence, from_activity_id=activity_rows[sequence - 1].id, to_activity_id=activity_rows[sequence].id, from_key=from_node.key, to_key=to_node.key, mode=fact.mode, depart_at=from_node.end_at.isoformat(), arrive_at=(from_node.end_at + timedelta(minutes=fact.duration_min)).isoformat(), duration_min=fact.duration_min, distance_m=fact.distance_m, cost_amount_minor=fact.cost_amount_minor, currency=fact.currency, cost_status=fact.cost_status, walking_min=fact.walking_min, transfers=fact.transfers, reliability_score=fact.reliability_score, weather_exposure=fact.weather_exposure, geometry_json=fact.geometry_json, crs=fact.crs, provider_key=fact.provider_key, provenance=fact.provenance, provider_snapshot_id=fact.id, captured_at=fact.captured_at, expires_at=fact.expires_at))
        session.flush()
        result.append(plan)
        all_evidence_ids.append(snapshot_id)
    session.commit()
    result_optimizer_kind = (
        optimizer_kind if result else "CITYNEXUS_DETERMINISTIC_ASSIGNMENT"
    )
    return ComposeResult(tuple(result), stay, tuple(all_evidence_ids), result_optimizer_kind)
