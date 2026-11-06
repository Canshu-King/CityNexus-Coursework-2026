from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from itertools import permutations
from pathlib import Path
from typing import Any, Callable
from uuid import UUID, uuid5
from zoneinfo import ZoneInfo

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.execution.traveler_twin import capability_constraint_items
from app.core.audit import record_audit
from app.db.models import (
    Activity,
    CityService,
    CollectionFolder,
    CollectionItem,
    Commitment,
    Journey,
    JourneyCheckin,
    JourneyConstraint,
    Place,
    PlanVersion,
    Preference,
    Traveler,
    TravelerTwinSnapshot,
)
from app.integrations.mock.providers import MockCityDataProvider
from app.integrations.mobility.runtime import capture_runtime_mobility_snapshot
from app.replanning.feasibility import (
    FeasibilityActivity,
    FeasibilityResult,
    activity_from_record,
    build_context,
    evaluate_feasibility,
)
from app.replanning.provider_snapshots import MobilitySnapshot
from app.planning.anchor_schedule import (
    ANCHOR_DURATION_MIN,
    CandidateActivity,
    MID_TRIP_ROLE,
    active_c4_commitments,
    effective_journey_window,
    resolve_plan_anchors,
    schedule_template_with_anchors,
    validate_anchor_timeline,
)
from app.planning.anchors import anchor_claim_identity
from app.replanning.protected_commitments import active_c4_hard_constraints
from app.replanning.reference_replanner import WEIGHTS
from app.schemas import Activity as ActivitySchema
from app.schemas import CityService as CityServiceSchema
from app.schemas import CollectionFolder as CollectionFolderSchema
from app.schemas import CollectionItem as CollectionItemSchema
from app.schemas import Constraint as ConstraintSchema
from app.schemas import Journey as JourneySchema
from app.schemas import JourneyCheckin as JourneyCheckinSchema
from app.schemas import JourneyLiveView
from app.schemas import Money, ScoreBreakdown
from app.schemas import Place as PlaceSchema
from app.schemas import PlanVersion as PlanSchema
from app.schemas import Preference as PreferenceSchema
from app.schemas import RecycleBinJourney
from app.schemas import Traveler as TravelerSchema

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MOCK_ROOT = PROJECT_ROOT / "data" / "mock"
DEMO_NAMESPACE = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_dt(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value)


def journey_zone(item: Journey) -> "ZoneInfo | None":
    """The journey's own zone, or None when it cannot be used."""
    try:
        return ZoneInfo(item.timezone)
    except (KeyError, ValueError, TypeError):
        return None


def parse_dt_in_journey_zone(value: str | datetime, zone: "ZoneInfo | None") -> datetime:
    """`parse_dt`, but a naive wall-clock is placed in the journey's zone instead of staying naive.

    A column may hold local wall-clock (`2026-10-09T18:00:00`) rather than an offset-bearing
    instant.  Left naive it goes out as `18:00` with nothing to say which moment that is, while the
    payload names a zone beside it -- the two fields disagreeing about the same moment.  A naive
    value is a fact about a place ("18:00 in Tianjin"), so it is placed in that place's zone.  A
    value that already carries an offset is untouched: that one is already an instant.
    """
    parsed = parse_dt(value)
    if parsed.tzinfo is not None or zone is None:
        return parsed
    return parsed.replace(tzinfo=zone)


def dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _journey_constraints_with_capabilities(
    session: Session, journey: Journey, *, active_only: bool = False, include_c4: bool = True
) -> list[Any]:
    query = select(JourneyConstraint).where(JourneyConstraint.journey_id == journey.id)
    if active_only:
        query = query.where(JourneyConstraint.active.is_(True))
    constraints: list[Any] = list(session.scalars(query).all())
    constraints.extend(capability_constraint_items(session, journey.traveler_id, journey.id))
    if include_c4:
        commitments = session.scalars(
            select(Commitment).where(
                Commitment.journey_id == journey.id,
                Commitment.status == "ACTIVE",
                Commitment.level == "C4_Anchored",
            )
        ).all()
        constraints.extend(active_c4_hard_constraints(commitments))
    return constraints


def load(name: str) -> Any:
    return json.loads((MOCK_ROOT / name).read_text(encoding="utf-8"))


def deterministic_id(label: str) -> str:
    return str(uuid5(DEMO_NAMESPACE, label))


def reset_all(session: Session) -> None:
    # Delete children first because the package schema intentionally enables FKs.
    for table in (
        # ORD-0476: a footprint points at a journey, an activity and a place, so it has to go before
        # all three -- otherwise `demo/reset` hits a foreign key and answers 500 with nothing deleted.
        "journey_checkins",
        "edge_actions",
        "edge_observations",
        "edge_devices",
        "push_subscriptions",
        "location_observations",
        "offline_journey_snapshots",
        "notification_events",
        "journey_legs",
        "transport_reservations",
        "external_reservation_drafts",
        "journey_operations_contexts",
        "counterfactual_simulations",
        "guardian_runs",
        "agent_run_steps",
        "agent_runs",
        "journey_missions",
        "guardian_pulses",
        "provider_observations",
        "journey_outcome_events",
        "traveler_preference_evidence",
        "traveler_capabilities",
        "traveler_twin_snapshots",
        "city_signal_fixtures",
        "autopilot_enrollments",
        "real_place_candidates",
        "mobility_facts",
        "stay_journey_options",
        "accommodation_offer_snapshots",
        "journey_trip_contexts",
        "journey_resilience_snapshots",
        "audit_logs",
        "provider_calls",
        "idempotency_keys",
        "action_approvals",
        "replan_proposals",
        "recommendations",
        "impact_assessments",
        "transactions",
        "orders",
        "bookings",
        "commitments",
        "events",
        "journey_twins",
        "activities",
        "plan_versions",
        "constraints",
        "city_services",
        # ORD-0444: the collection shelf is cleared with the rest of the scene, and it has to be
        # cleared *before* `places`, because `collection_items.place_id` references `places(id)` with
        # no `ON DELETE` clause and `PRAGMA foreign_keys=ON` is set on every connection
        # (`backend/app/db/session.py`).  Measured before this line existed: a shelf holding one
        # place made `POST /api/v1/demo/reset` answer 500 with
        # `sqlite3.IntegrityError: FOREIGN KEY constraint failed`
        # (`scratch/reset_counter_before_r1.json`, keys `4_demo_reset_with_a_non_empty_shelf` and
        # `6_server_log_attribution`).  That door is the fixture reset for roughly 140 test modules
        # and for the demo stage, so a container that could not be emptied would have broken all of
        # them the first time a traveller favourited something.
        # The other reading -- that a collection is user data and survives a scene reset -- would
        # need `place_id` to stop being a hard reference, because a bookmark pointing at a place the
        # scene no longer holds is then a state every reader must render honestly.  That is a
        # product decision this slice does not make; it names the two shapes and ships the one the
        # foreign key already implies.
        "collection_items",
        "collection_folders",
        "places",
        "journeys",
        "preferences",
        "traveler_agent_policies",
        "travelers",
    ):
        session.connection().execute(text(f'DELETE FROM "{table}"'))
    session.expire_all()
    session.commit()


_DEMO_ACCESSIBILITY_FACTS = ("stroller_accessible", "luggage_accessible")


def _demo_accessibility_evidence(item: Mapping[str, Any]) -> dict[str, Any] | None:
    """Project the demo fixture's accessibility facts onto a sealed demo place.

    Only the whitelisted booleans cross over, and the provenance is stamped here
    rather than read from the fixture: editing JSON must not be able to label a
    demo place as provider truth.
    """

    claimed = item.get("accessibility")
    if not isinstance(claimed, Mapping):
        return None
    facts = {
        key: claimed[key]
        for key in _DEMO_ACCESSIBILITY_FACTS
        if isinstance(claimed.get(key), bool)
    }
    if not facts:
        return None
    facts["source_kind"] = "DEMO"
    facts["source"] = "data/mock/places.json#%s" % item["id"]
    return facts


def hard_rule_codes_on_file(constraints: Sequence[Any]) -> list[str]:
    """The HARD rule codes in force, read across the two row shapes this list actually holds.

    Capability constraints come in as dicts and journey constraints as ORM rows; an
    attribute-only read drops the former without complaining.
    """

    codes: set[str] = set()
    for item in constraints:
        is_mapping = isinstance(item, Mapping)
        kind = item.get("kind") if is_mapping else getattr(item, "kind", None)
        if str(kind or "") != "HARD":
            continue
        code = item.get("rule_code") if is_mapping else getattr(item, "rule_code", None)
        if code:
            codes.add(str(code))
    return sorted(codes)


DEMO_FIXTURE_NOW = "2026-10-01T00:00:00+00:00"


def build_demo_fixture_rows(now: str = DEMO_FIXTURE_NOW) -> list[Any]:
    """Every row the demo fixture ships, as ORM objects, built without touching a session.

    Split out of `seed_demo` because an empty store needs these rows too, and needs them
    additively: `seed_demo` is the reset door (it deletes 52 tables before seeding), while
    ORD-0521 `demo/scene/ensure` has to install the same scenario on a store that already
    holds somebody elses journeys.  One list of rows for both, so the two cannot drift into
    disagreeing about what "the demo" is.

    The frozen `now` and the deterministic ids are unchanged: two runs still have to produce
    byte-identical exported records.
    """

    rows: list[Any] = []
    traveler_data = load("traveler_demo.json")
    traveler = Traveler(
        id=traveler_data["id"],
        display_name=traveler_data["display_name"],
        email=traveler_data["email"],
        locale=traveler_data["locale"],
        timezone=traveler_data["timezone"],
        created_at=now,
        updated_at=now,
    )
    rows.append(traveler)
    rows.append(
        TravelerTwinSnapshot(
            traveler_id=traveler.id,
            explicit_preferences_json="[]",
            learned_soft_preferences_json="[]",
            conflicts_json="[]",
            confidence=0.0,
            evidence_count=0,
            provenance_json=dump({"source": "seed", "updated_at": now}),
            updated_at=now,
        )
    )
    for index, item in enumerate(traveler_data["preferences"], start=1):
        rows.append(
            Preference(
                id=deterministic_id(f"preference:{index}"),
                traveler_id=traveler.id,
                category=item["category"],
                preference_key=item["key"],
                value_json=dump(item["value"]),
                weight=item["weight"],
                source="user",
                created_at=now,
                updated_at=now,
            )
        )
    city_provider = MockCityDataProvider()
    for item in city_provider.places():
        metadata: dict[str, Any] = {
            "duration_min": item["duration_min"],
            "cost_minor": item["cost_minor"],
        }
        evidence = _demo_accessibility_evidence(item)
        if evidence:
            metadata["accessibility_evidence"] = evidence
        rows.append(
            Place(
                id=item["id"],
                provider_key=item["provider_key"],
                external_id=item["external_id"],
                city_code=item["city_code"],
                name=item["name"],
                category=item["category"],
                latitude=item["location"]["latitude"],
                longitude=item["location"]["longitude"],
                opening_hours_json=dump({"open": item["open"], "close": item["close"]}),
                tags_json=dump(item["tags"]),
                metadata_json=dump(metadata),
                updated_at=now,
            )
        )
    for item in city_provider.city_services():
        rows.append(
            CityService(
                id=item["id"],
                provider_key=item["provider_key"],
                external_id=item["external_id"],
                place_id=item["place_id"],
                city_code=item["city_code"],
                service_type=item["service_type"],
                name=item["name"],
                availability_json=dump(item["availability"]),
                metadata_json=dump({}),
                updated_at=now,
            )
        )
    # WP04's supplied commerce overlay extends the frozen mock city catalog
    # with the bookable River Cruise and tradable Food Hall services.  Keep the
    # overlay additive so the WP00-WP03 city fixtures remain unchanged.
    for item in load("wp04_city_services_overlay.json"):
        rows.append(
            CityService(
                id=item["id"],
                provider_key=item["provider_key"],
                external_id=item["external_id"],
                place_id=item.get("place_id"),
                city_code=item["city_code"],
                service_type=item["service_type"],
                name=item["name"],
                availability_json=dump(item.get("availability", {})),
                metadata_json=dump({"source": "wp04_commerce_catalog.json"}),
                updated_at=now,
            )
        )
    journey_data = load("demo_journey.json")["journey"]
    journey = Journey(
        id=journey_data["id"],
        traveler_id=journey_data["traveler_id"],
        title=journey_data["title"],
        city_code=journey_data["city_code"],
        timezone=journey_data["timezone"],
        start_at=journey_data["start_at"],
        end_at=journey_data["end_at"],
        budget_currency=journey_data["budget"]["currency"],
        budget_amount_minor=journey_data["budget"]["amount_minor"],
        planning_mode=journey_data.get("planning_mode", "balanced"),
        status="DRAFT",
        active_plan_version_id=None,
        current_activity_id=None,
        created_at=now,
        updated_at=now,
    )
    rows.append(journey)
    for index, item in enumerate(load("demo_journey.json")["constraints"], start=1):
        rows.append(
            JourneyConstraint(
                id=deterministic_id(f"constraint:{index}"),
                journey_id=journey.id,
                kind=item["kind"],
                category=item["category"],
                rule_code=item["rule_code"],
                params_json=dump(item["params"]),
                weight=item["weight"],
                active=True,
                created_at=now,
                updated_at=now,
            )
        )
    return rows


def seed_demo(session: Session) -> dict[str, str]:
    reset_all(session)
    # The fixture is deliberately frozen so two reset/seed runs produce the
    # same logical rows and byte-identical exported records.
    rows = build_demo_fixture_rows()
    traveler = next(row for row in rows if isinstance(row, Traveler))
    journey = next(row for row in rows if isinstance(row, Journey))
    # Read the summary before the commit rather than after: nothing here needs live ORM state.
    summary = {
        "traveler_id": traveler.id,
        "journey_id": journey.id,
        "city_code": journey.city_code,
    }
    # One flush per row, in the fixture's own order: these rows lean on table-level foreign
    # keys only, so a batched insert can put a child ahead of the parent it points at.
    for row in rows:
        session.add(row)
        session.flush()
    session.commit()
    return summary


def traveler_schema(item: Traveler) -> TravelerSchema:
    return TravelerSchema(
        id=item.id,
        display_name=item.display_name,
        email=item.email,
        locale=item.locale,
        timezone=item.timezone,
        created_at=parse_dt(item.created_at),
        updated_at=parse_dt(item.updated_at),
    )


def preference_schema(item: Preference) -> PreferenceSchema:
    return PreferenceSchema(
        id=item.id,
        traveler_id=item.traveler_id,
        category=item.category,
        key=item.preference_key,
        value=json.loads(item.value_json),
        weight=item.weight,
    )


def journey_live_view(item: Journey) -> JourneyLiveView:
    """One computation, one place: whether this trip belongs on 实时行程 (ORD-0520).

    The rule reads `journeys.aborted_at` -- the column the abort door owns and never clears -- and
    falls back to `status` for a row written before that column existed.  It is not `status` alone:
    `select_plan` writes `journeys.status = "PLANNED"` without reading the journey's status
    (`backend/app/main.py`), and the two activity-advance doors write `"COMPLETED"` the same
    way (`backend/app/main.py`, `backend/app/main.py`), so a trip called off and then
    re-planned would come back onto the live screen while still carrying the abort moment and the
    traveller's reason -- two answers on one screen, which is what the follow-up race probe found for
    the write gate (`review/ord0520/ABORT_RACE_RECEIPT.json`) and what `abort_resurrect_probe_r1.py`
    measures on this order's own stack.  The judgement stays the server's rather than a screen's
    because a device that re-derived it would hold a second copy of the rule.  A trip in the recycle
    bin is not decided here -- `listJourneys` already refuses to list it, and the bin has its own
    column, its own door and its own word (`backend/app/schemas.py` -- `RecycleBinJourney`).
    """

    if item.aborted_at is not None or item.status == "CANCELLED":
        return JourneyLiveView(included=False, reason_code="JOURNEY_ABORTED")
    return JourneyLiveView(included=True)


def journey_schema(item: Journey) -> JourneySchema:
    zone = journey_zone(item)
    return JourneySchema(
        id=item.id,
        traveler_id=item.traveler_id,
        title=item.title,
        city_code=item.city_code,
        timezone=item.timezone,
        start_at=parse_dt_in_journey_zone(item.start_at, zone),
        end_at=parse_dt_in_journey_zone(item.end_at, zone),
        timezone_honoured=zone is not None,
        budget=Money(amount_minor=item.budget_amount_minor, currency=item.budget_currency),
        planning_mode=item.planning_mode,
        status=item.status,
        active_plan_version_id=item.active_plan_version_id,
        current_activity_id=item.current_activity_id,
        aborted_at=parse_dt(item.aborted_at) if item.aborted_at else None,
        abort_reason=item.abort_reason,
        live_view=journey_live_view(item),
    )


def recycle_bin_entry_schema(item: Journey) -> RecycleBinJourney:
    """The bin's row: the accepted journey shape, then the two facts about the bin itself.

    It goes through `journey_schema` rather than mapping the columns again, so the trip part of
    the body cannot drift away from what `getJourney` already answers for the same row.
    """

    payload = journey_schema(item).model_dump()
    payload["deleted_at"] = parse_dt(item.deleted_at)
    payload["bin_state"] = "IN_RECYCLE_BIN"
    return RecycleBinJourney(**payload)


def constraint_schema(item: JourneyConstraint) -> ConstraintSchema:
    return ConstraintSchema(
        id=item.id,
        journey_id=item.journey_id,
        kind=item.kind,
        category=item.category,
        rule_code=item.rule_code,
        params=json.loads(item.params_json),
        weight=item.weight,
        active=bool(item.active),
    )


def place_schema(item: Place) -> PlaceSchema:
    return PlaceSchema(
        id=item.id,
        name=item.name,
        category=item.category,
        city_code=item.city_code,
        location={"latitude": item.latitude, "longitude": item.longitude},
        tags=json.loads(item.tags_json),
        provider_key=item.provider_key,
        coordinate_system=item.coordinate_system,
        source_coordinate_system=item.source_coordinate_system,
    )


def collection_item_schema(item: CollectionItem, place: Place) -> CollectionItemSchema:
    """One shelf row, with the place behind it resolved from the `places` table.

    The two arguments are the reason this takes a place instead of looking one up: the list door
    already reads the shelf together with the places it points at, so a row and its place arrive
    in the same pass and this function cannot answer a shelf with a place that was never the one
    stored.  A missing place is the caller's problem to name (`PLACE_NOT_FOUND`), not something to
    paper over here.

    `note` passes through as stored, including `None`, because "no note was written" is the fact
    the screen has to be able to show.  There is no `address` in this shape and none is derived:
    `places` has no address column, and the demo rows' `metadata_json` hold
    `cost_minor`/`duration_min`/`accessibility_evidence` and nothing address-shaped.
    """

    return CollectionItemSchema(
        id=item.id,
        folder_id=item.folder_id,
        place_id=item.place_id,
        name=place.name,
        category=place.category,
        city_code=place.city_code,
        location={"latitude": place.latitude, "longitude": place.longitude},
        provider_key=place.provider_key,
        coordinate_system=place.coordinate_system,
        note=item.note,
        added_at=item.added_at,
    )


def collection_folder_schema(
    folder: CollectionFolder, items: list[tuple[CollectionItem, Place]]
) -> CollectionFolderSchema:
    """A shelf and its rows, where `item_count` is counted out of the same array `items` becomes.

    The count is a length of the payload rather than a number from a second query on purpose: a
    shelf that reported 4 next to 3 rows would be reporting two different reads as one answer.
    `items` arrives in `added_at` order from the list door, and nothing here re-sorts it -- the
    order a traveller reads is the order the server chose.
    """

    rows = [collection_item_schema(item, place) for item, place in items]
    return CollectionFolderSchema(
        id=folder.id,
        name=folder.name,
        owner_traveler_id=folder.owner_traveler_id,
        created_at=folder.created_at,
        updated_at=folder.updated_at,
        item_count=len(rows),
        items=rows,
    )


def service_schema(item: CityService) -> CityServiceSchema:
    return CityServiceSchema(
        id=item.id,
        name=item.name,
        service_type=item.service_type,
        city_code=item.city_code,
        place_id=item.place_id,
    )


def _activity_cost_status(
    item: Activity, metadata: dict[str, Any], place: Place | None
) -> str:
    """Whether this activity's price is a real fact.

    Order of evidence:
    1. an explicit status on the activity metadata wins;
    2. the Place's own price evidence (``price_known``) decides, because a
       discovered place knows whether its price was actually captured;
    3. otherwise a positive amount is treated as a known price.
    """

    explicit = str(
        metadata.get("cost_status") or metadata.get("price_status") or ""
    ).upper()
    if explicit in {"KNOWN", "KNOWN_FREE", "KNOWN_PAID", "UNKNOWN"}:
        if explicit == "KNOWN":
            return "KNOWN_PAID" if item.estimated_cost_minor > 0 else "KNOWN_FREE"
        return explicit
    price_known = metadata.get("price_known")
    if price_known is None and place is not None:
        try:
            place_metadata = json.loads(place.metadata_json or "{}")
        except (TypeError, ValueError):
            place_metadata = {}
        price_known = place_metadata.get("price_known")
    if price_known is True:
        return "KNOWN_PAID" if item.estimated_cost_minor > 0 else "KNOWN_FREE"
    if price_known is False:
        return "UNKNOWN"
    return "KNOWN_PAID" if item.estimated_cost_minor > 0 else "UNKNOWN"


def activity_schema(item: Activity, place: Place | None = None) -> ActivitySchema:
    """Project one activity, resolving coordinates from its Place row.

    Activity Detail must show real Place coordinates (Snapshot M contract).
    The activity metadata only carries them on some paths, so the Place row is
    the authoritative source and is merged in here.
    """

    metadata = json.loads(item.metadata_json or "{}") if item.metadata_json else {}
    if place is not None:
        metadata.setdefault("latitude", place.latitude)
        metadata.setdefault("longitude", place.longitude)
        metadata.setdefault("crs", "WGS84")
        if place.category:
            metadata.setdefault("category", place.category)
        if place.opening_hours_json and "opening_hours" not in metadata:
            try:
                metadata["opening_hours"] = json.loads(place.opening_hours_json)
            except (TypeError, ValueError):
                pass
    return ActivitySchema(
        id=item.id,
        sequence_no=item.sequence_no,
        title=item.title,
        # Resolved from the same Place row the coordinates above come from, so a stop's name and its
        # position are answered by one read rather than by two sources that can disagree.  A row that
        # is missing (a dangling `place_id`) answers None: the screen then says the typed title, which
        # is the fact the store actually has, instead of inventing a name.
        place_name=place.name if place is not None else None,
        activity_type=item.activity_type,
        place_id=item.place_id,
        city_service_id=item.city_service_id,
        start_at=parse_dt(item.start_at),
        end_at=parse_dt(item.end_at),
        travel_from_prev_min=item.travel_from_prev_min,
        estimated_cost=Money(amount_minor=item.estimated_cost_minor, currency=item.currency),
        cost_status=_activity_cost_status(item, metadata, place),
        status=item.status,
        locked=bool(item.locked),
        metadata=metadata,
    )


def _plan_staleness(item: PlanVersion) -> tuple[bool, str | None]:
    """Whether a plan's frozen provider evidence makes it unusable as-is.

    This is a *projection* only; ``select`` revalidation remains the authority.
    """

    try:
        evidence = json.loads(item.mobility_evidence_json or "{}")
    except json.JSONDecodeError:
        return True, "STALE_RECOMPOSE_REQUIRED: mobility evidence is not readable"
    if not isinstance(evidence, dict) or not evidence.get("snapshot_id"):
        return True, "STALE_RECOMPOSE_REQUIRED: no frozen provider evidence"
    expires_at = evidence.get("expires_at")
    if expires_at:
        try:
            if datetime.fromisoformat(str(expires_at)) <= datetime.now(timezone.utc):
                return True, "STALE_RECOMPOSE_REQUIRED: frozen provider evidence expired"
        except ValueError:
            return True, "STALE_RECOMPOSE_REQUIRED: evidence timestamp invalid"
    return False, None


_CURRENT_UNRESOLVED = object()

#: Why composition refused, named (ORD-0101). `No feasible plan candidates` is one sentence
#: covering two different realities: a city with nothing to plan with at all, and a city whose
#: places exist but none of whose combinations survive the deterministic gate. A traveller can
#: act on the second ("loosen the budget, pick places") and cannot on the first ("this
#: destination has no data yet") - which is why they must not share a name.
CATALOG_EMPTY = "CATALOG_EMPTY"
CATALOG_INSUFFICIENT = "CATALOG_INSUFFICIENT"
# ORD-0108: the explicit-POI path (`_generic_compose_plans`, which is what the AI sheet ends up
# on) refuses for at least three *different* reasons, and every one of them reached the phone as
# the same `VALIDATION_ERROR` + `GENERIC_COMPOSE_INFEASIBLE:...` blob.  These are the three names
# the order adopted, one per condition the raise site can actually observe.
PLACES_NOT_IN_CATALOG = "PLACES_NOT_IN_CATALOG"
PLACE_COST_UNVERIFIED = "PLACE_COST_UNVERIFIED"
PLACE_OUTSIDE_JOURNEY_WINDOW = "PLACE_OUTSIDE_JOURNEY_WINDOW"


class CompositionRefusal(ValueError):
    """A plan could not be composed, naming *which* condition refused and where it bit.

    A `ValueError` on purpose: the doors that translate it to 422, and the demo advance chain
    that catches it, keep working unchanged - this only adds what they may choose to publish.
    The name is required rather than optional, because the whole point is that a refusal either
    knows its cause at its raise site or stays anonymous; an empty `reason_code` would be the
    same lie in a new field (ORD-0101: absence speaks, a fabricated value may not).
    """

    def __init__(
        self,
        message: str,
        *,
        reason_code: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.details = dict(details or {})


class CatalogShortfall(CompositionRefusal):
    """The ORD-0101 pair: `CATALOG_EMPTY` vs `CATALOG_INSUFFICIENT`.

    Kept as its own name because that order's evidence, and the message it refuses with, are a
    distinct rule from the explicit-POI refusals below.
    """


def plan_schema(
    session: Session,
    item: PlanVersion,
    *,
    current_plan_version_id: str | None | object = _CURRENT_UNRESOLVED,
) -> PlanSchema:
    """Project one PlanVersion onto the public contract.

    `is_current` answers "is this the plan the journey runs" from the journey's own
    `active_plan_version_id` pointer - the single statement of that fact in this system.
    A caller that already loaded the Journey passes the value in (the list endpoint, so the
    projection costs no query); `current_plan_version_id=None` is a *known* answer - no
    pointer, therefore nothing is current - which is why "not supplied" needs its own
    sentinel rather than a falsy default.
    """

    activities = session.scalars(
        select(Activity).where(Activity.plan_version_id == item.id).order_by(Activity.sequence_no)
    ).all()
    # Activity Detail needs real Place coordinates and opening hours, so the
    # Place rows are resolved once here rather than per activity.
    place_ids = {str(a.place_id) for a in activities if a.place_id}
    places_by_id: dict[str, Place] = {}
    if place_ids:
        places_by_id = {
            place.id: place
            for place in session.scalars(select(Place).where(Place.id.in_(place_ids))).all()
        }
    breakdown = _plan_score_breakdown(item)
    # Stale/recompose-required projection: an expired or evidence-less plan is
    # flagged so the API and UI never present it as a current recommendation.
    stale, stale_reason = _plan_staleness(item)
    if current_plan_version_id is _CURRENT_UNRESOLVED:
        owner = session.get(Journey, item.journey_id)
        current_plan_version_id = owner.active_plan_version_id if owner is not None else None
    return PlanSchema(
        id=item.id,
        journey_id=item.journey_id,
        version_no=item.version_no,
        is_current=str(current_plan_version_id or "") == item.id,
        planning_mode=item.planning_mode,
        status=item.status,
        source=item.source,
        parent_plan_version_id=item.parent_plan_version_id,
        total_cost=Money(amount_minor=item.total_cost_minor, currency=item.currency),
        utility_score=item.utility_score,
        score_breakdown=breakdown,
        activities=[
            activity_schema(a, places_by_id.get(str(a.place_id)))
            for a in activities
        ],
        mobility_snapshot_id=item.mobility_snapshot_id,
        mobility_evidence=json.loads(item.mobility_evidence_json or "{}"),
        explanation=json.loads(item.explanation_json or "{}"),
        created_at=item.created_at or None,
        stale=stale,
        selectable=not stale,
        stale_reason=stale_reason,
    )


def _plan_score_breakdown(item: PlanVersion) -> ScoreBreakdown | None:
    """Project legacy persisted score rows into the current public contract.

    V8/V9 materialization stored provider totals (for example ``stay`` and
    ``mobility``) rather than the normalized score dimensions introduced by
    the current PlanVersion schema.  Those rows remain valid historical
    plans, so listing a Journey must not fail merely because a newer client
    reads them.  New writers continue to persist the full breakdown.
    """

    if not item.score_breakdown_json:
        return None
    raw = json.loads(item.score_breakdown_json)
    required = {
        "commitment_preservation",
        "stability",
        "time_reliability",
        "preference_fit",
        "mobility_fit",
        "budget_fit",
        "robustness",
        "opportunity_value",
        "total",
    }
    if required.issubset(raw):
        return ScoreBreakdown(**raw)

    # Legacy provider-backed plans did not expose normalized dimensions.  Use
    # their utility score as the stable aggregate and retain the provider
    # facts as extension fields in the raw database JSON.
    aggregate = max(0.0, min(1.0, float(item.utility_score or raw.get("score", 0.8))))
    normalized = {
        "commitment_preservation": 1.0,
        "stability": aggregate,
        "time_reliability": aggregate,
        "preference_fit": aggregate,
        "mobility_fit": aggregate,
        "budget_fit": aggregate,
        "robustness": aggregate,
        "opportunity_value": aggregate,
        "total": aggregate,
    }
    return ScoreBreakdown(**normalized)


def _score(metrics: dict[str, float]) -> float:
    return round(sum(WEIGHTS[name] * max(0.0, min(1.0, metrics[name])) for name in WEIGHTS), 6)


def _templates() -> list[list[tuple[str, str, str, str]]]:
    return [
        [
            ("place-old-town", "Old Town Gate", "09:00", "10:15"),
            ("place-riverside-museum", "Riverside Museum", "10:30", "11:45"),
            ("place-food-hall", "Central Food Hall", "12:00", "13:00"),
            ("place-tech-art", "Tech Art Center", "13:20", "14:40"),
            ("place-river-cruise", "Nexus River Cruise Pier", "15:30", "16:30"),
            ("place-skyline-dinner", "Skyline Dinner House", "17:00", "18:30"),
        ],
        [
            ("place-old-town", "Old Town Gate", "09:00", "10:15"),
            ("place-riverside-museum", "Riverside Museum", "10:30", "11:45"),
            ("place-food-hall", "Central Food Hall", "12:00", "13:00"),
            ("place-city-park", "Nexus City Park", "13:20", "14:20"),
            ("place-river-cruise", "Nexus River Cruise Pier", "15:30", "16:30"),
            ("place-skyline-dinner", "Skyline Dinner House", "17:00", "18:30"),
        ],
        [
            ("place-old-town", "Old Town Gate", "09:00", "10:15"),
            ("place-riverside-museum", "Riverside Museum", "10:30", "11:45"),
            ("place-food-hall", "Central Food Hall", "12:00", "13:00"),
            ("place-design-library", "Design Library", "13:20", "14:20"),
            ("place-river-cruise", "Nexus River Cruise Pier", "15:30", "16:30"),
            ("place-skyline-dinner", "Skyline Dinner House", "17:00", "18:30"),
        ],
    ]


def _hm_to_minutes(text: str) -> int | None:
    try:
        hour, minute = (int(part) for part in str(text).split(":", 1))
    except (TypeError, ValueError):
        return None
    return hour * 60 + minute


def _place_window(place: Place) -> tuple[int, int] | None:
    """A place's own open/close as minutes-of-day, or None if it never says."""

    hours = json.loads(place.opening_hours_json or "{}") if place.opening_hours_json else {}
    opened = _hm_to_minutes(hours.get("open"))
    closed = _hm_to_minutes(hours.get("close"))
    if opened is None or closed is None or closed <= opened:
        return None
    return opened, closed


def _place_duration(place: Place) -> int:
    metadata = json.loads(place.metadata_json or "{}") if place.metadata_json else {}
    try:
        return max(15, int(metadata.get("duration_min", 60)))
    except (TypeError, ValueError):
        return 60


def _templates_resting_on_unsourced_hours(
    templates: Sequence[Sequence[tuple[str, str, str, str]]], places: Mapping[str, Any]
) -> int:
    """How many day sequences contain a stop whose place never states its opening hours.

    ORD-0543 widened the template leg so a city that holds places without hours can still be drawn,
    but the minutes used for those stops are the product's own working band, not the venue's claim.
    The count keeps that difference on the answer instead of in a comment: a reader of the shortfall
    can see how much of the schedule rests on unsourced minutes.
    """

    total = 0
    for template in templates:
        if any(
            pid in places and _place_window(places[pid]) is None
            for pid, *_ in (tuple(row) for row in template)
        ):
            total += 1
    return total


def _catalogue_templates(
    places: Mapping[str, Place],
    travel: Callable[[str, str], int],
    *,
    day_start_minutes: int = 9 * 60,
    day_end_minutes: int = 18 * 60,
    variants: int = 3,
) -> list[list[tuple[str, str, str, str]]]:
    """Compose the template shape out of whatever the city's catalogue really holds.

    `_templates()` is a fixture of `CNX-DEMO` place ids, and the no-anchor scheduler returns
    `None` at the first id it cannot resolve.  So a city with its own real places - however
    complete its catalogue and however valid its travel facts - could not be planned by the
    default path at all: `plans/generate` answered 422 with "No feasible plan candidates",
    which reads as "nothing works here" when the truth was "the only schedule we know how to
    draw is somebody else's city" (measured with CN-BEIJING, ORD-0089).

    The contract is kept identical, only its source changes: up to three day sequences of
    `(place_id, title, start HH:MM, end HH:MM)`, built from each place's own open/close and
    duration, with the frozen travel minutes between consecutive stops actually subtracted so
    the literal windows are ones the gate can honour.  Stops are taken earliest-opening first
    (ids break ties, so the order is reproducible), a stop that cannot fit before its own
    closing time is dropped rather than squeezed in, and the variants rotate the queue so the
    candidates are genuinely different plans instead of one plan three times.

    ORD-0543: a place that never states hours is now placed inside `day_start_minutes` to
    `day_end_minutes` - declared here, and *not* presented as the venue's opening hours. The
    count of day sequences that rest on those minutes travels with the shortfall answer as
    `day_sequences_unsourced_hours`, so "composed" and "composed on unsourced minutes" stay
    tellable apart by a reader of the response rather than of this comment.
    """

    band = (day_start_minutes, day_end_minutes)
    if day_end_minutes <= day_start_minutes:
        # A band that closes before it opens cannot host a stop: refuse the widening rather than
        # schedule anything inside a window that does not exist.
        band = None

    ordered = sorted(
        (place for place in places.values() if _place_window(place)),
        key=lambda place: (_place_window(place) or (0, 0))[0],
    )
    # ORD-0543 (commander ruling: widen this leg): a city whose places do not state opening hours
    # used to compose zero day sequences, and every door behind a schedule then reported "no
    # feasible candidates" as if the city were empty. Those stops are now packed, but strictly after
    # every place that does state hours (so a real window is never displaced to make room for a
    # guess), and strictly inside the band named on the parameters below - the product's own working
    # band, not a claim about the venue. Nothing here writes opening hours back into the store.
    unsourced = sorted(
        (place for place in places.values() if not _place_window(place)),
        key=lambda place: str(place.id),
    )
    ordered = ordered + unsourced
    if not ordered:
        return []

    def pack(sequence: Sequence[Place]) -> list[tuple[str, str, str, str]]:
        schedule: list[tuple[str, str, str, str]] = []
        previous_end: int | None = None
        previous: str | None = None
        for place in sequence:
            window = _place_window(place)
            if window is None:
                if band is None:
                    continue
                opened, closed = band
            else:
                opened, closed = window
            earliest = day_start_minutes if previous is None else previous_end + travel(
                previous, place.id
            )
            start = max(earliest, opened)
            end = start + _place_duration(place)
            if end > closed:
                # Dropped, not squeezed: an activity that cannot fit inside its own opening
                # hours is not made honest by moving its minutes around.
                continue
            schedule.append(
                (
                    place.id,
                    place.name,
                    f"{start // 60:02d}:{start % 60:02d}",
                    f"{end // 60:02d}:{end % 60:02d}",
                )
            )
            previous_end = end
            previous = place.id
        return schedule

    templates: list[list[tuple[str, str, str, str]]] = []
    for offset in range(max(1, variants)):
        rotated = list(ordered[offset % len(ordered) :]) + list(ordered[: offset % len(ordered)])
        packed = pack(rotated)
        if len(packed) >= 2 and packed not in templates:
            templates.append(packed)
    return templates


def _safe_estimate_minutes(provider, origin: str | None, destination: str | None) -> int:
    """Frozen-provider lookup that never raises for an unknown pair.

    A missing pair is a real planning fact (``MOBILITY_DATA_UNAVAILABLE`` in the
    gate), not a reason to abort the whole generation loop.
    """

    if not origin or not destination:
        return 0
    try:
        return int(provider.estimate_minutes(origin, destination))
    except (KeyError, TypeError, ValueError):
        return 0


def _blocking_anchor_names(session: Session, journey: Journey, identities: set[str]) -> str:
    """Name the confirmed reservations sitting on the nodes an anchor conflict is about.

    The conflict codes identify a *node* (``v12:<journey>:arrival-anchor``) - nothing a traveller
    ever typed - and while one Journey could hold two ACTIVE claims on the same node the sentence
    read as a single anchor conflicting with itself.  Each commitment behind the conflict is
    therefore named by its own confirmation reference and the time it claims: the two facts a
    traveller can check against the ticket in their hand.
    """

    if not identities:
        return ""
    try:
        rows = active_c4_commitments(session, journey)
    except Exception:  # noqa: BLE001 - reason text must never break generation
        return ""
    named: set[str] = set()
    for row in rows:
        try:
            policy = json.loads(row.policy_json or "{}")
        except (TypeError, ValueError):
            continue
        if not isinstance(policy, Mapping):
            continue
        identity = anchor_claim_identity(policy, journey_id=journey.id)
        if identity not in identities:
            continue
        reference = str(row.external_ref or "no confirmation reference")
        claimed = str(policy.get("scheduled_start_at") or "no scheduled time")
        role = str(policy.get("anchor_role") or "ANCHOR")
        named.add(f"{role} '{reference}' claiming {claimed} (commitment {row.id})")
    if not named:
        return ""
    return "; blocking commitment: " + "; ".join(sorted(named))


def _anchor_block_reason(session: Session, journey: Journey) -> str:
    """Name the C4 anchor that blocked generation, when there is one.

    ``select`` already returns an accurate ``LOCKED_COMMITMENT_LOST:<key>``;
    ``generate`` must not collapse that into an anonymous sentence.
    """

    try:
        anchors, unresolved = resolve_plan_anchors(session, journey)
    except Exception:  # noqa: BLE001 - reason text must never break generation
        return ""
    if unresolved:
        wanted = {item.split(":", 1)[1] for item in unresolved if ":" in item}
        return (
            ": C4_ANCHOR_UNRESOLVED:" + ",".join(sorted(set(unresolved)))
            + _blocking_anchor_names(session, journey, wanted)
        )
    if not anchors:
        return ""
    # Prefer the precise structural reason (overlap, arrival-after-departure,
    # outside journey) over a generic "blocked" list.
    try:
        effective_start, effective_end = effective_journey_window(journey, anchors)
        conflict = validate_anchor_timeline(
            anchors,
            parse_dt(effective_start),
            parse_dt(effective_end),
        )
    except Exception:  # noqa: BLE001
        conflict = None
    keys = {anchor.semantic_key for anchor in anchors if anchor.semantic_key}
    if conflict:
        return f": {conflict}" + _blocking_anchor_names(session, journey, keys)
    joined = ", ".join(sorted(keys))
    return f": C4_ANCHOR_BLOCKED:{joined}" + _blocking_anchor_names(session, journey, keys)


# How much of a day a traveller actually spends moving between and visiting
# stops.  Used to spread a multi-day trip across its days instead of packing
# everything into the arrival morning.
DAY_ACTIVITY_BUDGET_MIN = 330


def _generic_day_windows(
    journey_start: datetime, journey_end: datetime
) -> list[tuple[datetime, datetime]]:
    """Usable sightseeing windows, one per calendar day of the trip.

    Day 1 starts at arrival; the last day ends at departure.  Middle days are
    full days bounded by a normal sightseeing span so stops are not scheduled
    at 03:00.
    """

    windows: list[tuple[datetime, datetime]] = []
    day = journey_start.date()
    last_day = journey_end.date()
    while day <= last_day:
        day_start = datetime.combine(day, datetime.min.time(), journey_start.tzinfo)
        day_end = day_start + timedelta(hours=23)
        if day == journey_start.date():
            day_start = max(day_start, journey_start)
        if day == last_day:
            day_end = min(day_end, journey_end)
        if day_end > day_start:
            windows.append((day_start, day_end))
        day += timedelta(days=1)
    return windows or [(journey_start, journey_end)]


def _generic_compose_plans(
    session: Session,
    journey: Journey,
    planning_mode: str,
    place_ids: Sequence[str],
) -> list[PlanVersion]:
    """Compose a bounded plan from explicit POIs rather than city templates.

    The legacy fixture templates remain available for deterministic demo
    seeding.  This path is the product-facing seam: its candidate set comes
    from selected/discovered Place records, then the same deterministic gate
    validates opening hours, budget, windows, commitments, and route gaps
    before anything is persisted.
    """

    selected_ids = list(dict.fromkeys(str(item) for item in place_ids))
    places = {
        item.id: item
        for item in session.scalars(
            select(Place).where(Place.city_code == journey.city_code, Place.id.in_(selected_ids))
        ).all()
    }
    missing = [item for item in selected_ids if item not in places]
    if missing:
        # The ids come from a caller that may have invented them (the AI sheet hands over
        # whatever `/places` returned, unsorted and unverified).  "Not found" is then a fact
        # about *which* ids, so they ride along instead of dissolving into one blob.
        raise CompositionRefusal(
            f"GENERIC_COMPOSE_PLACE_NOT_FOUND:{','.join(missing)}",
            reason_code=PLACES_NOT_IN_CATALOG,
            details={
                "city_code": journey.city_code,
                "place_ids": list(missing),
                "requested_place_ids": list(selected_ids),
            },
        )
    if len(places) < 2:
        raise ValueError("GENERIC_COMPOSE_REQUIRES_TWO_PLACES")

    constraints = _journey_constraints_with_capabilities(session, journey, active_only=True)
    # A confirmed C4 anchor is a requirement to build around on this path too.
    anchors, unresolved_anchors = resolve_plan_anchors(session, journey)
    for anchor in anchors:
        places.setdefault(anchor.place_id, anchor.place)
    services = {
        service.id: service
        for service in session.scalars(
            select(CityService).where(CityService.city_code == journey.city_code)
        ).all()
    }
    lookup = {
        item.id: {"location": {"latitude": item.latitude, "longitude": item.longitude}}
        for item in places.values()
    }
    schedulable_ids = tuple(dict.fromkeys([*selected_ids, *(a.place_id for a in anchors)]))
    mobility_pairs = tuple(
        (origin, destination)
        for origin in schedulable_ids
        for destination in schedulable_ids
        if origin != destination
    )
    mobility_provider = capture_runtime_mobility_snapshot(
        session,
        journey,
        mobility_pairs,
        operation_key=f"generic-compose:{journey.id}",
    ).snapshot
    bounded_orders = tuple(permutations(selected_ids))
    ordered_ids = min(
        bounded_orders,
        key=lambda order: sum(
            mobility_provider.estimate_minutes(order[index - 1], order[index])
            for index in range(1, len(order))
        ),
    )
    journey_start = parse_dt(journey.start_at)
    journey_end = parse_dt(journey.end_at)
    # A multi-day trip must actually use its days.  Laying every stop out from
    # the arrival instant crammed an 8-stop Xiamen trip into one morning, which
    # then made every hotel's pinned check-in genuinely unreachable.
    day_windows = _generic_day_windows(journey_start, journey_end)
    day_index = 0
    cursor = day_windows[0][0]
    day_used_min = 0
    previous: str | None = None
    facts: list[FeasibilityActivity] = []
    rows: list[tuple[Place, datetime, datetime, int, int]] = []
    total_cost = 0

    for place_id in ordered_ids:
        place = places[place_id]
        metadata = json.loads(place.metadata_json or "{}")
        raw_cost = metadata.get("cost_minor")
        if raw_cost is None:
            # "Unknown cost" is not "your budget is too tight": it means this place has no
            # price we can vouch for, which is a catalog fact the caller can act on.
            raise CompositionRefusal(
                f"GENERIC_COMPOSE_UNKNOWN_COST:{place_id}",
                reason_code=PLACE_COST_UNVERIFIED,
                details={
                    "city_code": journey.city_code,
                    "place_ids": [place_id],
                    "journey_budget_minor": journey.budget_amount_minor,
                },
            )
        cost_minor = int(raw_cost)
        duration_min = max(15, int(metadata.get("duration_min", 60)))
        travel_min = (
            mobility_provider.estimate_minutes(previous, place_id) if previous else 0
        )
        start_at = cursor + timedelta(minutes=travel_min)
        opening = json.loads(place.opening_hours_json or "{}")
        if isinstance(opening, dict) and opening.get("open"):
            hour, minute = (int(part) for part in str(opening["open"]).split(":", 1))
            open_at = datetime.combine(
                start_at.date(), datetime.min.time().replace(hour=hour, minute=minute), start_at.tzinfo
            )
            start_at = max(start_at, open_at)
        end_at = start_at + timedelta(minutes=duration_min)
        # Roll over to the next day when this one is full (a real sightseeing
        # day, not a 23-hour window) or when the stop would overflow it.  A
        # multi-day trip must not cram every stop into its first morning.
        while day_index < len(day_windows) - 1 and (
            end_at > day_windows[day_index][1]
            or day_used_min + duration_min + travel_min > DAY_ACTIVITY_BUDGET_MIN
        ):
            day_index += 1
            next_start, _next_end = day_windows[day_index]
            start_at = next_start
            end_at = start_at + timedelta(minutes=duration_min)
            cursor = next_start
            day_used_min = 0
            travel_min = 0
        if end_at > journey_end:
            # The stop genuinely does not fit before the trip ends - a fact about *this* place
            # and *these* dates, not a verdict on the traveller's requirements.
            raise CompositionRefusal(
                f"GENERIC_COMPOSE_JOURNEY_WINDOW_EXCEEDED:{place_id}",
                reason_code=PLACE_OUTSIDE_JOURNEY_WINDOW,
                details={
                    "city_code": journey.city_code,
                    "place_ids": [place_id],
                    "journey_start_at": journey.start_at,
                    "journey_end_at": journey.end_at,
                    "would_end_at": end_at.isoformat(),
                },
            )
        facts.append(
            FeasibilityActivity(
                key=place_id,
                place_id=place_id,
                start_at=start_at,
                end_at=end_at,
                cost_minor=cost_minor,
                currency=journey.budget_currency,
                travel_from_prev_min=travel_min,
                locked=False,
            )
        )
        rows.append((place, start_at, end_at, travel_min, cost_minor))
        total_cost += cost_minor
        cursor = end_at
        day_used_min += duration_min + travel_min
        previous = place_id

    # Pin every resolved anchor into the candidate schedule before the gate.
    anchored = _inject_anchors_into_generic(
        journey=journey,
        selected_ids=ordered_ids,
        places=places,
        anchors=anchors,
        facts=facts,
        rows=rows,
    )
    if anchored is None:
        if unresolved_anchors:
            raise ValueError(
                "C4_ANCHOR_UNRESOLVED:" + ",".join(sorted(set(unresolved_anchors)))
            )
        raise ValueError("GENERIC_COMPOSE_INFEASIBLE:C4_ANCHOR_UNSATISFIABLE")
    facts, rows = anchored
    total_cost = sum(int(row[4]) for row in rows)
    gate = evaluate_feasibility(
        build_context(
            journey,
            constraints,
            places,
            services,
            mobility_provider=mobility_provider,
        ),
        facts,
    )
    if not gate.passed:
        raise ValueError(f"GENERIC_COMPOSE_INFEASIBLE:{'|'.join(gate.failures)}")

    existing_max = (
        session.scalar(
            select(PlanVersion.version_no)
            .where(PlanVersion.journey_id == journey.id)
            .order_by(PlanVersion.version_no.desc())
            .limit(1)
        )
        or 0
    )
    version_no = existing_max + 1
    plan_id = deterministic_id(
        f"plan:{journey.id}:{version_no}:generic:{','.join(ordered_ids)}"
    )
    budget_fit = max(0.0, min(1.0, 1 - total_cost / max(1, journey.budget_amount_minor)))
    metrics = {
        "commitment_preservation": 1.0,
        "stability": 0.9,
        "time_reliability": 0.9,
        "preference_fit": 0.8,
        "mobility_fit": 0.85,
        "budget_fit": budget_fit,
        "robustness": 0.85,
        "opportunity_value": 0.8,
    }
    breakdown = {**metrics, "total": _score(metrics)}
    now = iso_now()
    plan = PlanVersion(
        id=plan_id,
        journey_id=journey.id,
        version_no=version_no,
        planning_mode=planning_mode,
        status="CANDIDATE",
        source="INITIAL",
        parent_plan_version_id=None,
        total_cost_minor=total_cost,
        currency=journey.budget_currency,
        estimated_duration_min=sum(
            int((end_at - start_at).total_seconds() // 60) + travel_min
            for _place, start_at, end_at, travel_min, _cost in rows
        ),
        utility_score=breakdown["total"],
        score_breakdown_json=dump(breakdown),
        explanation_json=dump(
            {
                "reason": "Generic Compose Engine from explicit POIs",
                "input_place_ids": selected_ids,
                "compose_order": list(ordered_ids),
                "deterministic_gate": "PASSED",
                "mobility_provider": mobility_provider.source_provider_key,
                "mobility_snapshot_id": mobility_provider.snapshot_id,
                "mobility_provenance": [
                    {
                        "from": fact.origin_key,
                        "to": fact.destination_key,
                        "provider": fact.provider_key,
                        "degraded": fact.degraded,
                    }
                    for fact in mobility_provider.facts.values()
                ],
            }
        ),
        mobility_snapshot_id=mobility_provider.snapshot_id,
        mobility_evidence_json=dump(mobility_provider.to_evidence()),
        created_at=now,
    )
    session.add(plan)
    session.flush()
    for sequence, (place, start_at, end_at, travel_min, cost_minor) in enumerate(rows, start=1):
        session.add(
            Activity(
                id=deterministic_id(f"activity:{plan_id}:{sequence}"),
                plan_version_id=plan.id,
                sequence_no=sequence,
                activity_type="visit",
                title=place.name,
                place_id=place.id,
                city_service_id=None,
                start_at=start_at.isoformat(),
                end_at=end_at.isoformat(),
                travel_from_prev_min=travel_min,
                estimated_cost_minor=cost_minor,
                currency=journey.budget_currency,
                status="PLANNED",
                locked=False,
                metadata_json=dump(
                    {
                        "compose_source": "EXPLICIT_POI_SET",
                        "semantic_key": (
                            f"v12:{journey.id}:{place.category.upper()}-anchor"
                            if place.category.upper() in {"ARRIVAL", "DEPARTURE"}
                            else place.id
                        ),
                        "category": place.category,
                        "opening_hours": json.loads(place.opening_hours_json or "{}"),
                        # The map resolves pins from activity metadata; without
                        # them a real Xiamen plan renders no Xiamen coordinates.
                        "latitude": place.latitude,
                        "longitude": place.longitude,
                        "crs": "WGS84",
                    }
                ),
                created_at=now,
                updated_at=now,
            )
        )
    record_audit(
        session,
        action="plans.generated.generic_compose",
        entity_type="PlanVersion",
        entity_id=plan.id,
        journey_id=journey.id,
        after={"place_ids": selected_ids, "gate": "PASSED"},
    )
    session.commit()
    return [plan]




def _generic_compose_variants(
    session: Session,
    journey: Journey,
    planning_mode: str,
    place_ids: Sequence[str],
    candidate_count: int,
) -> list[PlanVersion]:
    """Compose genuinely different candidates from the same discovered POIs.

    Real provider discovery returns POIs without a "tour style", so inventing
    marketing names like 轻松版/经典版/深度版 would be fabrication.  What *is*
    real is how much fits in the trip: we therefore build candidates of
    decreasing scope (full set, then progressively fewer stops).  They differ in
    stop count, total cost and walking burden, which is exactly what the
    comparison card is supposed to show.
    """

    ordered = list(dict.fromkeys(str(item) for item in place_ids))
    if len(ordered) < 2:
        raise ValueError("GENERIC_COMPOSE_REQUIRES_TWO_PLACES")

    # Distinct scopes, longest first, never below two stops.
    scopes: list[list[str]] = []
    for drop in range(min(3, len(ordered) - 1)):
        scope = ordered[: len(ordered) - drop]
        if len(scope) >= 2 and scope not in scopes:
            scopes.append(scope)
    scopes = scopes[: max(1, candidate_count)]

    created: list[PlanVersion] = []
    failures: list[str] = []
    # `str(exc)` is what flattens the three conditions into one blob: the scope loop kept the
    # sentence and dropped the name, so the outermost refusal - the one a phone actually reads -
    # was anonymous even when every inner failure knew exactly which place it blamed.
    names: list[str] = []
    offending: list[str] = []
    shared: dict[str, Any] = {}
    for scope in scopes:
        try:
            created.extend(
                _generic_compose_plans(session, journey, planning_mode, scope)
            )
        except ValueError as exc:
            failures.append(str(exc))
            name = getattr(exc, "reason_code", None)
            if not name:
                continue
            names.append(str(name))
            detail = getattr(exc, "details", None) or {}
            for item in detail.get("place_ids") or []:
                if str(item) not in offending:
                    offending.append(str(item))
            for key, value in detail.items():
                if key != "place_ids" and key not in shared:
                    shared[key] = value
    if not created:
        message = "GENERIC_COMPOSE_INFEASIBLE:" + "|".join(failures[:3] or ["NO_VARIANT"])
        # A name is published only when every scope tried blames the same condition.  Mixed or
        # anonymous causes stay anonymous here, because "the widest scope failed on cost" would
        # be a guess dressed up as a fact.
        if names and len(names) == len(failures) and len(set(names)) == 1:
            raise CompositionRefusal(
                message,
                reason_code=names[0],
                details={"place_ids": offending, "scopes_tried": len(scopes), **shared},
            )
        raise ValueError(message)
    return created

def _inject_anchors_into_generic(
    *,
    journey: Journey,
    selected_ids: Sequence[str],
    places: Mapping[str, Any],
    anchors: Sequence[Any],
    facts: list[FeasibilityActivity],
    rows: list[Any],
) -> tuple[list[FeasibilityActivity], list[Any]] | None:
    """Insert resolved C4 anchors into an explicit-POI schedule.

    The optional POIs keep their relative order; anchors are pinned to their
    required time.  Returns ``None`` when an anchor cannot be satisfied.
    """

    if not anchors:
        return facts, rows
    # Rebuild the schedule from the (place, start, end, travel, cost) rows and
    # re-interleave the anchors, preserving the original POI ordering.
    scheduled: list[tuple[str, Any, Any, Any, int, str | None]] = []
    by_place = {place_id: row for row, place_id in zip(facts, selected_ids)}
    for place_id in selected_ids:
        fact = by_place.get(place_id)
        if fact is None:
            continue
        place = places.get(place_id)
        if place is None:
            return None
        scheduled.append(
            (place_id, fact.start_at, fact.end_at, fact.cost_minor, place.id, None)
        )
    anchor_rows = []
    for anchor in anchors:
        duration = (
            ANCHOR_DURATION_MIN
            if anchor.role != MID_TRIP_ROLE
            else max(ANCHOR_DURATION_MIN, 60)
        )
        if anchor.time_boundary == "END":
            start_at = anchor.scheduled_at - timedelta(minutes=duration)
            end_at = anchor.scheduled_at
        else:
            start_at = anchor.scheduled_at
            end_at = anchor.scheduled_at + timedelta(minutes=duration)
        anchor_rows.append(
            (
                anchor.place_id,
                start_at,
                end_at,
                0,
                anchor.place_id,
                anchor.semantic_key,
                anchor.role,
            )
        )
    combined = [
        (
            item[0],
            item[1],
            item[2],
            item[3],
            item[4],
            item[5],
            None,
        )
        for item in scheduled
    ] + anchor_rows
    combined.sort(key=lambda item: (item[1], 0 if item[6] else 1, item[0]))
    new_facts: list[FeasibilityActivity] = []
    new_rows: list[Any] = []
    for place_id, start_at, end_at, cost_minor, db_place_id, semantic, role in combined:
        place = places.get(db_place_id) or places.get(place_id)
        if place is None:
            return None
        new_facts.append(
            FeasibilityActivity(
                key=place_id,
                place_id=db_place_id,
                start_at=start_at,
                end_at=end_at,
                cost_minor=int(cost_minor),
                currency=journey.budget_currency,
                travel_from_prev_min=0,
                locked=bool(role),
                metadata=(
                    {"semantic_key": semantic, "anchor_role": role, "commitment_anchor": True}
                    if role
                    else {"semantic_key": semantic or db_place_id}
                ),
            )
        )
        new_rows.append((place, start_at, end_at, 0, int(cost_minor)))
    return new_facts, new_rows


def validate_plan_feasibility(
    session: Session,
    journey: Journey,
    plan: PlanVersion,
    *,
    journey_overrides: Mapping[str, Any] | None = None,
    additional_constraints: Sequence[Any] = (),
    include_active_c4: bool = True,
    mobility_provider: Any | None = None,
) -> FeasibilityResult:
    """Evaluate any persisted plan against the current authoritative Journey facts."""

    if plan.journey_id != journey.id:
        raise ValueError("PlanVersion does not belong to Journey")
    overrides = dict(journey_overrides or {})
    # The effective journey window is widened inside ``evaluate_feasibility``
    # from the active C4 constraints themselves, so generation and every
    # revalidation (select / booking / start) automatically agree.
    proposed_journey = {
        "id": journey.id,
        "city_code": overrides.get("city_code", journey.city_code),
        "start_at": overrides.get("start_at", journey.start_at),
        "end_at": overrides.get("end_at", journey.end_at),
        "budget_amount_minor": overrides.get("budget_amount_minor", journey.budget_amount_minor),
        "budget_currency": overrides.get("budget_currency", journey.budget_currency),
    }
    constraints: list[Any] = _journey_constraints_with_capabilities(
        session, journey, include_c4=include_active_c4
    )
    constraints.extend(additional_constraints)
    places = {
        item.id: item
        for item in session.scalars(
            select(Place).where(Place.city_code == proposed_journey["city_code"])
        ).all()
    }
    services = {
        item.id: item
        for item in session.scalars(
            select(CityService).where(CityService.city_code == proposed_journey["city_code"])
        ).all()
    }
    activities = session.scalars(
        select(Activity).where(Activity.plan_version_id == plan.id).order_by(Activity.sequence_no)
    ).all()
    return evaluate_feasibility(
        build_context(
            proposed_journey,
            constraints,
            places,
            services,
            mobility_provider=mobility_provider or _snapshot_for_plan(session, journey, plan),
            plan_currency=plan.currency,
        ),
        [activity_from_record(item) for item in activities],
    )


def _snapshot_for_plan(session: Session, journey: Journey, plan: PlanVersion):
    """Load the PlanVersion's frozen mobility evidence; never recapture here."""
    raw = json.loads(plan.mobility_evidence_json or "{}")
    snapshot_id = raw.get("snapshot_id")
    if not snapshot_id or snapshot_id != plan.mobility_snapshot_id:
        raise ValueError("STALE_RECOMPOSE_REQUIRED: PlanVersion frozen mobility evidence is missing or inconsistent")
    expires_at = raw.get("expires_at")
    if expires_at and parse_dt(str(expires_at)) <= datetime.now(timezone.utc):
        raise ValueError("STALE_RECOMPOSE_REQUIRED: PlanVersion frozen mobility evidence expired")
    return MobilitySnapshot.from_evidence(raw)


def validate_selected_plan_feasibility(
    session: Session,
    journey: Journey,
    *,
    journey_overrides: Mapping[str, Any] | None = None,
    additional_constraints: Sequence[Any] = (),
    mobility_provider: Any | None = None,
) -> FeasibilityResult | None:
    """Re-evaluate the current selected/active plan against a proposed mutation snapshot."""

    if journey.status not in {"PLANNED", "ACTIVE"} or not journey.active_plan_version_id:
        return None

    selected = session.get(PlanVersion, journey.active_plan_version_id)
    expected_plan_status = "SELECTED" if journey.status == "PLANNED" else "ACTIVE"
    if (
        selected is None
        or selected.journey_id != journey.id
        or selected.status != expected_plan_status
    ):
        raise ValueError("Journey active plan state is invalid")
    return validate_plan_feasibility(
        session,
        journey,
        selected,
        journey_overrides=journey_overrides,
        additional_constraints=additional_constraints,
        include_active_c4=True,
        mobility_provider=mobility_provider,
    )


def generate_plans(
    session: Session,
    journey: Journey,
    planning_mode: str,
    candidate_count: int,
    *,
    place_ids: Sequence[str] | None = None,
) -> list[PlanVersion]:
    if place_ids:
        return _generic_compose_variants(
            session, journey, planning_mode, place_ids, candidate_count
        )
    existing_max = (
        session.scalar(
            select(PlanVersion.version_no)
            .where(PlanVersion.journey_id == journey.id)
            .order_by(PlanVersion.version_no.desc())
            .limit(1)
        )
        or 0
    )
    places = {
        p.id: p
        for p in session.scalars(select(Place).where(Place.city_code == journey.city_code)).all()
    }
    services = {
        service.id: service
        for service in session.scalars(
            select(CityService).where(CityService.city_code == journey.city_code)
        ).all()
    }
    constraints = _journey_constraints_with_capabilities(session, journey)
    # The frozen fixture templates stay authoritative for the city they were authored for:
    # taking only the templates whose every place id exists keeps `CNX-DEMO` on the identical
    # path (all three resolve there) while a city with its own real catalogue - where none
    # resolve - falls through to `_catalogue_templates` below instead of answering 422.
    raw_templates = [
        template
        for template in _templates()
        if all(place_id in places for place_id, *_ in template)
    ]
    created: list[PlanVersion] = []
    now = iso_now()
    journey_start = parse_dt(journey.start_at)
    journey_end = parse_dt(journey.end_at)
    # A confirmed C4 anchor is a requirement to *build around*, not only a veto.
    # Resolve every active anchor to a concrete, routable Place identity and pin
    # it into the candidate schedule before the gate runs.
    anchors, unresolved_anchors = resolve_plan_anchors(session, journey)
    for anchor in anchors:
        places.setdefault(anchor.place_id, anchor.place)
    # A confirmed reservation is an authoritative real-world fact: the draft
    # journey window expands to contain it rather than making it unplannable.
    effective_start_text, effective_end_text = effective_journey_window(
        journey, anchors
    )
    journey_start = parse_dt(effective_start_text)
    journey_end = parse_dt(effective_end_text)
    metrics_list = [
        {
            "commitment_preservation": 0.98,
            "stability": 0.95,
            "time_reliability": 0.94,
            "preference_fit": 0.92,
            "mobility_fit": 0.88,
            "budget_fit": 0.90,
            "robustness": 0.90,
            "opportunity_value": 0.82,
        },
        {
            "commitment_preservation": 0.98,
            "stability": 0.93,
            "time_reliability": 0.96,
            "preference_fit": 0.90,
            "mobility_fit": 0.93,
            "budget_fit": 0.96,
            "robustness": 0.92,
            "opportunity_value": 0.78,
        },
        {
            "commitment_preservation": 0.98,
            "stability": 0.92,
            "time_reliability": 0.95,
            "preference_fit": 0.91,
            "mobility_fit": 0.91,
            "budget_fit": 0.95,
            "robustness": 0.91,
            "opportunity_value": 0.80,
        },
    ]
    # Capture one frozen mobility matrix over every node a candidate can visit
    # (template POIs plus resolved anchors) *before* scheduling, so the
    # scheduler inserts real travel gaps and the gate consumes the exact same
    # frozen evidence.
    schedulable_ids = tuple(
        dict.fromkeys(
            [
                *(
                    place_id
                    for template in raw_templates
                    for place_id, *_ in template
                    if place_id in places
                ),
                # A city with no frozen template of its own is priced over its whole
                # catalogue: without this the matrix would be captured empty, and the
                # derived day sequences below would have no travel minutes to respect.
                *(sorted(places) if not raw_templates else ()),
                *(anchor.place_id for anchor in anchors),
            ]
        )
    )
    mobility_provider = capture_runtime_mobility_snapshot(
        session,
        journey,
        tuple(
            (origin, destination)
            for origin in schedulable_ids
            for destination in schedulable_ids
            if origin != destination
        ),
        operation_key=f"template-compose:{journey.id}",
    ).snapshot
    travel_minutes = {
        (origin, destination): fact.minutes
        for (origin, destination), fact in mobility_provider.facts.items()
    }
    if not raw_templates:
        # Derived from this city's own places, with the minutes above subtracted between
        # stops, so the windows handed to the scheduler are ones the gate can honour.
        raw_templates = _catalogue_templates(
            places,
            lambda origin, destination: travel_minutes.get((origin, destination), 0),
        )
    schedules: list[list[CandidateActivity]] = []
    for template in raw_templates:
        schedule = schedule_template_with_anchors(
            journey=journey,
            template=template,
            places=places,
            anchors=anchors,
            journey_start=journey_start,
            journey_end=journey_end,
            travel_minutes=travel_minutes,
        )
        if schedule is not None:
            schedules.append(schedule)
    candidate_rejections: list[dict] = []
    for offset, schedule in enumerate(schedules):
        if len(created) >= candidate_count:
            break
        total_cost = sum(int(item.cost_minor) for item in schedule)
        if total_cost > journey.budget_amount_minor:
            continue
        metrics = metrics_list[offset % len(metrics_list)]
        activity_facts: list[FeasibilityActivity] = []
        previous_place: str | None = None
        for item in schedule:
            travel = (
                _safe_estimate_minutes(mobility_provider, previous_place, item.place_id)
                if previous_place
                else 0
            )
            activity_facts.append(
                FeasibilityActivity(
                    key=item.place_id,
                    place_id=item.place_id,
                    start_at=item.start_at,
                    end_at=item.end_at,
                    cost_minor=item.cost_minor,
                    currency=journey.budget_currency,
                    travel_from_prev_min=travel,
                    locked=item.locked,
                    metadata=dict(item.metadata),
                )
            )
            previous_place = item.place_id
        # This is the sole authoritative gate.  It runs before score
        # calculation and before a PlanVersion or Activity is persisted.
        gate = evaluate_feasibility(
            build_context(
                journey,
                constraints,
                places,
                services,
                mobility_provider=mobility_provider,
            ),
            activity_facts,
        )
        if not gate.passed:
            # SILENT_CANDIDATE_LOSS (API half): the candidate disappears either way, so the
            # durable fact is written here. Only names the product already emitted go into it -
            # the gate's own failure strings plus the hard rule codes on file for this journey.
            # Nothing raises and the response stays a list; surviving candidates still ship.
            rejection = {
                "reason_codes": ["CANDIDATE_REJECTED_BY_FEASIBILITY_GATE"],
                "summary": "candidate dropped before persistence: "
                + ", ".join(gate.failures[:4]),
                "failures": list(gate.failures),
                "hard_rule_codes": hard_rule_codes_on_file(constraints),
                "candidate_sequence": offset,
                "stops": [item.place_id for item in activity_facts if item.place_id],
            }
            candidate_rejections.append(rejection)
            record_audit(
                session,
                action="plans.candidate_rejected",
                entity_type="Journey",
                entity_id=journey.id,
                journey_id=journey.id,
                after=rejection,
            )
            continue
        # Version numbers remain contiguous among accepted candidates even
        # when an earlier raw template is rejected by a hard constraint.
        version_no = existing_max + len(created) + 1
        plan_id = deterministic_id(f"plan:{journey.id}:{version_no}")
        total = _score(metrics)
        breakdown = {**metrics, "total": total}
        plan = PlanVersion(
            id=plan_id,
            journey_id=journey.id,
            version_no=version_no,
            planning_mode=planning_mode,
            status="CANDIDATE",
            source="INITIAL",
            parent_plan_version_id=None,
            total_cost_minor=total_cost,
            currency=journey.budget_currency,
            estimated_duration_min=570,
            utility_score=total,
            score_breakdown_json=dump(breakdown),
            explanation_json=dump({"reason": "Deterministic provider-backed candidate", "mobility_snapshot_id": mobility_provider.snapshot_id}),
            mobility_snapshot_id=mobility_provider.snapshot_id,
            mobility_evidence_json=dump(mobility_provider.to_evidence()),
            created_at=now,
        )
        session.add(plan)
        session.flush()
        for sequence, item in enumerate(schedule, start=1):
            aid = deterministic_id(f"activity:{plan_id}:{sequence}")
            travel = activity_facts[sequence - 1].travel_from_prev_min
            # ORD-0543: a stop drawn by the widened template leg sits in the product's working band,
            # not in a window the venue stated. The row says which of the two it is, so "the plan
            # exists" cannot quietly become "the plan is sourced" - anchor stops keep their own
            # provenance and are not labelled here.
            item_place = places.get(item.place_id)
            activity_metadata = dict(item.metadata)
            if item.anchor_role is None and item_place is not None:
                activity_metadata["hours_basis"] = (
                    "STATED_OPENING_HOURS" if _place_window(item_place) else "PRODUCT_WORKING_BAND"
                )
            session.add(
                Activity(
                    id=aid,
                    plan_version_id=plan_id,
                    sequence_no=sequence,
                    activity_type=(
                        "arrival"
                        if item.anchor_role == "ARRIVAL"
                        else "departure"
                        if item.anchor_role == "DEPARTURE"
                        else "visit"
                    ),
                    title=item.title,
                    place_id=item.place_id,
                    city_service_id=None,
                    start_at=item.start_at.isoformat(),
                    end_at=item.end_at.isoformat(),
                    travel_from_prev_min=travel,
                    estimated_cost_minor=item.cost_minor,
                    currency=journey.budget_currency,
                    status="PLANNED",
                    locked=item.locked,
                    metadata_json=dump(activity_metadata),
                    created_at=now,
                    updated_at=now,
                )
            )
        created.append(plan)
    if not created:
        if unresolved_anchors:
            raise ValueError(
                "C4_ANCHOR_UNRESOLVED:" + ",".join(sorted(set(unresolved_anchors)))
            )
        detail = _anchor_block_reason(session, journey)
        # "No feasible plan candidates" is true for both "this city has nothing to plan with"
        # and "there were places, and none of the combinations survived the gate" - and a
        # traveller can only act on the second one. Named here, where the counts are actually
        # in hand; the message stays byte-identical because a screen and an existing gate both
        # read it (ORD-0101: add a name, do not rewrite a sentence).
        raise CatalogShortfall(
            "No feasible plan candidates satisfy the journey constraints" + detail,
            reason_code=CATALOG_EMPTY if not places else CATALOG_INSUFFICIENT,
            details={
                "city_code": journey.city_code,
                "places_in_catalog": len(places),
                "city_services_in_catalog": len(services),
                "day_sequences_available": len(raw_templates),
                # ORD-0543: how many of those sequences contain at least one stop whose place never
                # states opening hours - the difference between "the city can be drawn" and "the
                # drawing rests on the product's working band", counted from `places`, not asserted.
                "day_sequences_unsourced_hours": _templates_resting_on_unsourced_hours(
                    raw_templates, places
                ),
                "requested_candidates": candidate_count,
                # Which candidate died on which named rule (services assembles this; the
                # route forwards `details` verbatim, so no main.py change is needed).
                "candidate_rejections": candidate_rejections,
            },
        )
    record_audit(
        session,
        action="plans.generated",
        entity_type="PlanVersion",
        entity_id=journey.id,
        journey_id=journey.id,
        after={"count": len(created), "plan_ids": [plan.id for plan in created]},
    )
    session.commit()
    return created


def checkin_schema(
    row: JourneyCheckin, activity: Activity | None, place: Place | None
) -> JourneyCheckinSchema:
    """One stamp, with its target resolved from the rows rather than copied into the stamp.

    The four `activity_*` / `place_*` fields are read from whatever the target row still holds, so a
    stamp never shows a name its target has changed or lost.  They are nullable because losing the
    target is a state the store allows on purpose (`ON DELETE SET NULL`,
    `backend/db/schema.sql`): a replan removes the activity a traveller stamped, and the footprint is
    the traveller's own history, so it stays and reads as "the row is gone" rather than as an empty
    title or an invented one.
    """

    return JourneyCheckinSchema(
        id=row.id,
        journey_id=row.journey_id,
        activity_id=row.activity_id,
        place_id=row.place_id,
        target_kind=row.target_kind,
        target_key=row.target_key,
        stamped_at=row.stamped_at,
        note=row.note,
        source=row.source,
        activity_title=activity.title if activity is not None else None,
        activity_type=activity.activity_type if activity is not None else None,
        place_name=place.name if place is not None else None,
        place_category=place.category if place is not None else None,
    )
