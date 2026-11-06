from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from itertools import permutations
from pathlib import Path
from typing import Any, Callable
from uuid import UUID, uuid5

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.execution.traveler_twin import capability_constraint_items, planning_preference_items
from app.core.audit import record_audit
from app.db.models import (
    Activity,
    CityService,
    Commitment,
    Journey,
    JourneyConstraint,
    JourneyMember,
    JourneyMission,
    Place,
    PlanVersion,
    Preference,
    RealPlaceCandidate,
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
from app.replanning.candidate_evidence import (
    canonical_place_tags,
    verified_interest_preference_breakdown,
)
from app.replanning.provider_snapshots import MobilitySnapshot
from app.planning.anchor_schedule import (
    ANCHOR_DURATION_MIN,
    ANCHOR_PROVIDER_KEY,
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
from app.schemas import Constraint as ConstraintSchema
from app.schemas import Journey as JourneySchema
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
        "member_place_group_share_consents",
        "member_place_promotions",
        "member_place_invitation_progress",
        "member_place_invitation_recipients",
        "member_place_invitations",
        "member_side_quest_return_reports",
        "member_side_quests",
        "member_live_moment_scout_receipts",
        "planned_split_meetup_handshakes",
        "collective_authority_receipts",
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
        "journey_intents",
        "journey_members",
        "activities",
        "plan_versions",
        "constraints",
        "city_services",
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


def seed_demo(session: Session) -> dict[str, str]:
    reset_all(session)
    # The fixture is deliberately frozen so two reset/seed runs produce the
    # same logical rows and byte-identical exported records.
    now = "2026-10-01T00:00:00+00:00"
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
    session.add(traveler)
    session.flush()
    session.add(
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
        session.add(
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
            "cost_evidence": {
                "status": "KNOWN",
                "amount_minor": item["cost_minor"],
                "currency": "CNY",
                "source": "SEALED_DEMO_FIXTURE",
            },
            "price_known": True,
            "authority_envelope": SEALED_DEMO_AUTHORITY_ENVELOPE,
        }
        evidence = _demo_accessibility_evidence(item)
        if evidence:
            metadata["accessibility_evidence"] = evidence
        session.add(
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
    session.flush()
    for item in city_provider.city_services():
        session.add(
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
        session.add(
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
    session.add(journey)
    session.flush()
    session.add(
        JourneyMember(
            id=deterministic_id("journey-member:owner"),
            journey_id=journey.id,
            traveler_id=journey.traveler_id,
            role="OWNER",
            invited_by=journey.traveler_id,
            expires_at=None,
            joined_at=now,
            removed_at=None,
            preferences_json="{}",
            created_at=now,
            updated_at=now,
        )
    )
    for index, item in enumerate(load("demo_journey.json")["constraints"], start=1):
        session.add(
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
    session.commit()
    return {
        "traveler_id": traveler.id,
        "journey_id": journey.id,
        "city_code": journey.city_code,
    }


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


def journey_schema(item: Journey) -> JourneySchema:
    return JourneySchema(
        id=item.id,
        traveler_id=item.traveler_id,
        title=item.title,
        city_code=item.city_code,
        timezone=item.timezone,
        start_at=parse_dt(item.start_at),
        end_at=parse_dt(item.end_at),
        budget=Money(amount_minor=item.budget_amount_minor, currency=item.budget_currency),
        planning_mode=item.planning_mode,
        status=item.status,
        active_plan_version_id=item.active_plan_version_id,
        current_activity_id=item.current_activity_id,
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
    metadata = json.loads(item.metadata_json or "{}")
    opening_hours = json.loads(item.opening_hours_json or "{}")
    cost_evidence = metadata.get("cost_evidence")
    if not isinstance(cost_evidence, dict):
        cost_evidence = {"status": "UNKNOWN", "amount_minor": None}
    return PlaceSchema(
        id=item.id,
        name=item.name,
        category=item.category,
        city_code=item.city_code,
        location={"latitude": item.latitude, "longitude": item.longitude},
        tags=json.loads(item.tags_json),
        external_id=item.external_id,
        provider_key=item.provider_key,
        coordinate_system=item.coordinate_system,
        source_coordinate_system=item.source_coordinate_system,
        opening_hours=opening_hours,
        cost_evidence=cost_evidence,
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


# These are provider-backed cost states.  ``cost_minor=0`` is retained as a
# compatibility storage value, but it is never evidence that an attraction is
# free.  A place is cost-complete only when the evidence object says so.
KNOWN_COST_EVIDENCE_STATUSES = frozenset({"KNOWN", "VERIFIED_PROVIDER"})
KNOWN_OPENING_EVIDENCE_STATUSES = frozenset(
    {"NORMALIZED_VALID", "OFFICIAL_WEB_VERIFIED", "VERIFIED_PROVIDER", "CLOSED"}
)

SYNTHETIC_LIVE_SELECTION_BLOCKED = "SYNTHETIC_MOBILITY_NOT_AUTHORITATIVE_FOR_LIVE_CONTENT"
UNRESOLVED_ACTIVITY_FACTS = "UNRESOLVED_ACTIVITY_FACTS"
COST_UNKNOWN_ACCEPTED = "COST_UNKNOWN_ACCEPTED"
COST_UNKNOWN_USER_POLICY = "USER_ACCEPTS_UNKNOWN_PRICE_NOT_INCLUDED"
SEALED_DEMO_AUTHORITY_ENVELOPE = "SEALED_DEMO_FIXTURE"
SEALED_REAL_REPLAY_AUTHORITY_ENVELOPE = "SEALED_REAL_REPLAY"
COURSEWORK_ESTIMATE_AUTHORITY_ENVELOPE = "COURSEWORK_ESTIMATE"
SEALED_AUTHORITY_ENVELOPES = frozenset(
    {SEALED_DEMO_AUTHORITY_ENVELOPE, SEALED_REAL_REPLAY_AUTHORITY_ENVELOPE}
)


def _place_metadata(place: Place) -> dict[str, Any]:
    return json.loads(place.metadata_json or "{}") if place.metadata_json else {}


def _place_strategy_estimate(place: Place) -> dict[str, Any] | None:
    estimate = _place_metadata(place).get("strategy_estimate")
    if not isinstance(estimate, Mapping):
        return None
    if str(estimate.get("status") or "").upper() != "ESTIMATED_AGENT":
        return None
    return dict(estimate)


def _coursework_estimate_covers(place: Place) -> bool:
    estimate = _place_strategy_estimate(place)
    if estimate is None:
        return False
    opening = estimate.get("opening_hours")
    cost = estimate.get("cost_evidence")
    if not isinstance(opening, Mapping) or not isinstance(cost, Mapping):
        return False
    return (
        opening.get("open_minutes") is not None
        and opening.get("close_minutes") is not None
        and cost.get("amount_minor") is not None
    )


def _coursework_facts_cover(place: Place) -> bool:
    """Return whether a coursework candidate has either real or lab facts.

    A provider-complete Place must not need a duplicate agent estimate merely
    because another stop is being evaluated in the isolated coursework
    envelope.  Estimates cover only the unresolved stop; they never mutate or
    upgrade canonical provider truth.
    """

    return not _place_has_unresolved_visit_facts(place) or _coursework_estimate_covers(place)


def _place_verified_cost(place: Place) -> int | None:
    metadata = _place_metadata(place)
    provider_key = getattr(place, "provider_key", None)
    evidence = metadata.get("cost_evidence")
    if not isinstance(evidence, Mapping):
        legacy_cost = metadata.get("cost_minor")
        if (
            provider_key == "mock-city"
            and isinstance(legacy_cost, (int, float))
            and legacy_cost > 0
        ):
            return int(legacy_cost)
        return None
    status = str(evidence.get("status") or "UNKNOWN").upper()
    amount = evidence.get("amount_minor")
    if status not in KNOWN_COST_EVIDENCE_STATUSES or amount is None:
        # Once a cost_evidence object exists it is the canonical contract.
        # Compatibility cost_minor must not override an explicit UNKNOWN or
        # otherwise non-authoritative evidence state.
        return None
    return int(amount)


def _place_has_unresolved_visit_facts(place: Place) -> bool:
    """Return whether a used provider Place still lacks opening or cost truth.

    Compatibility ``cost_minor=0`` is intentionally ignored here.  A live
    provider Place may enter a provisional/catalogue path with UNKNOWN facts,
    but those facts cannot cross the selection authority boundary.
    """

    return _place_has_unresolved_opening(place) or _place_has_unresolved_cost(place)


def _place_has_unresolved_opening(place: Place) -> bool:
    opening = json.loads(place.opening_hours_json or "{}") if place.opening_hours_json else {}
    opening_status = str(opening.get("status") or "UNKNOWN").upper()
    return opening_status not in KNOWN_OPENING_EVIDENCE_STATUSES


def _place_has_unresolved_cost(place: Place) -> bool:
    metadata = _place_metadata(place)
    evidence = metadata.get("cost_evidence")
    if not isinstance(evidence, Mapping):
        return True
    cost_status = str(evidence.get("status") or "UNKNOWN").upper()
    return cost_status not in KNOWN_COST_EVIDENCE_STATUSES or evidence.get("amount_minor") is None


def _catalogue_visit_eligible(place: Place) -> bool:
    """Return true only for content that can be a visit stop.

    Lodging, parking and transport rows are useful provider facts, but they are
    not attractions.  Keeping that role decision here prevents the provisional
    lane from turning an accommodation row into a generic visit.
    """

    category = str(place.category or "").casefold()
    if category in {
        "hotel", "lodging", "accommodation", "guest_house", "hostel",
        "parking", "car_park", "bus_station", "train_station", "transit_station",
    }:
        return False
    role = str(_place_metadata(place).get("place_role") or "").casefold()
    if role in {"lodging", "accommodation", "parking", "transit"}:
        return False
    return category in {
        "aquarium", "attraction", "cafe", "experience", "food", "gallery", "garden",
        "heritage", "landmark", "library", "museum", "nightlife", "park",
        "restaurant", "temple", "theatre", "tourism", "viewpoint", "zoo",
    }


def plan_authority_blocker(
    item: PlanVersion,
    places_by_id: Mapping[str, Place],
    *,
    used_place_ids: Sequence[str] | None = None,
) -> str | None:
    """Return the shared authority blocker for a plan, if any.

    Only mobility edges actually traversed by the plan participate in the
    synthetic check.  An unused fallback edge in a complete matrix cannot veto
    a route whose used edges are live/provider-backed.
    """

    explanation = json.loads(item.explanation_json or "{}")
    used = [str(value) for value in (used_place_ids or ()) if value]
    used_unique = list(dict.fromkeys(used))
    coursework_places = [places_by_id.get(place_id) for place_id in used_unique]
    coursework_estimate = (
        settings.lab_estimate_authority
        and str(explanation.get("authority_envelope") or "").upper()
        == COURSEWORK_ESTIMATE_AUTHORITY_ENVELOPE
        and bool(coursework_places)
        and all(place is not None and _coursework_facts_cover(place) for place in coursework_places)
    )
    # Replan candidates can be materialised by a different composition path
    # and therefore may carry a provisional explanation marker even after the
    # actual used Places have acquired canonical facts.  The used Place rows
    # remain the authoritative content boundary: live or provider-backed
    # UNKNOWN opening/cost facts remain non-selectable.  An explicit sealed
    # demo/replay envelope is the only exception, so fixtures and captured
    # replay tests keep their existing contract.
    used_content_places = [
        places_by_id[place_id]
        for place_id in used_unique
        if place_id in places_by_id
        and places_by_id[place_id].provider_key != ANCHOR_PROVIDER_KEY
    ]
    cost_unknown_mode = (
        explanation.get("authority_state") == COST_UNKNOWN_ACCEPTED
        and explanation.get("cost_unknown_policy") == COST_UNKNOWN_USER_POLICY
        and isinstance(explanation.get("cost_summary"), Mapping)
        and int(explanation["cost_summary"].get("unknown_item_count") or 0) > 0
    )
    if cost_unknown_mode and used_content_places and any(
        _place_has_unresolved_opening(place) for place in used_content_places
    ):
        # The explicit R86 acknowledgement covers admission price only.  An
        # OSM identity is not evidence that a venue is open, so opening facts
        # still have to be independently grounded before selection/start.
        return UNRESOLVED_ACTIVITY_FACTS
    if explanation.get("authority_state") == "PROVISIONAL_UNRESOLVED_FACTS" and not coursework_estimate:
        if not used_content_places or any(
            _place_has_unresolved_visit_facts(place) for place in used_content_places
        ):
            return UNRESOLVED_ACTIVITY_FACTS
    elif explanation.get("authority_state") == "COURSEWORK_ESTIMATED_FACTS" and not coursework_estimate:
        return UNRESOLVED_ACTIVITY_FACTS

    sealed = bool(used_content_places) and all(
        str(_place_metadata(place).get("authority_envelope") or "").upper()
        in SEALED_AUTHORITY_ENVELOPES
        for place in used_content_places
    )
    if used_content_places and not sealed and not coursework_estimate and not cost_unknown_mode and any(
        _place_has_unresolved_visit_facts(place) for place in used_content_places
    ):
        return UNRESOLVED_ACTIVITY_FACTS

    evidence = json.loads(item.mobility_evidence_json or "{}")
    if not isinstance(evidence, Mapping):
        return None
    facts = [fact for fact in evidence.get("facts", []) if isinstance(fact, Mapping)]
    used_edges = set(zip(used, used[1:]))
    used_facts = [
        fact for fact in facts
        if (str(fact.get("origin_key")), str(fact.get("destination_key"))) in used_edges
    ]
    relevant = used_facts if used_place_ids is not None else facts
    synthetic = any(
        str(fact.get("route_provenance") or "").upper() == "SYNTHETIC_FIXTURE"
        or str(fact.get("fact_quality") or "").upper() == "SYNTHETIC_DEFAULT"
        for fact in relevant
    )
    if not synthetic and used_place_ids is None:
        provider = str(evidence.get("source_provider") or "").casefold()
        synthetic = provider.startswith(("mock", "fixture"))
    if not synthetic:
        return None
    content_places = used_content_places if used_place_ids is not None else [
        place for place in places_by_id.values()
        if place.provider_key != ANCHOR_PROVIDER_KEY
    ]
    sealed = bool(content_places) and all(
        str(_place_metadata(place).get("authority_envelope") or "").upper()
        in SEALED_AUTHORITY_ENVELOPES
        for place in content_places
    )
    return None if sealed else SYNTHETIC_LIVE_SELECTION_BLOCKED


def plan_unresolved_activity_facts(item: PlanVersion) -> list[dict[str, Any]]:
    explanation = json.loads(item.explanation_json or "{}")
    unresolved = explanation.get("unresolved_facts")
    if not isinstance(unresolved, list):
        return []
    return [dict(row) for row in unresolved if isinstance(row, Mapping)]


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
LODGING_ROWS_USED_AS_GENERIC_VISITS = "LODGING_ROWS_USED_AS_GENERIC_VISITS"
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
    activity_keys = [
        str(activity.place_id or activity.city_service_id or "")
        for activity in activities
    ]
    authority_blocker = plan_authority_blocker(
        item,
        places_by_id,
        used_place_ids=activity_keys,
    )
    explanation = json.loads(item.explanation_json or "{}")
    if explanation.get("authority_state") == "PROVISIONAL_UNRESOLVED_FACTS" and authority_blocker is None:
        explanation = {**explanation, "authority_state": "LIVE_PROVIDER_FACTS"}
    projected_activities = [
        activity_schema(a, places_by_id.get(str(a.place_id)))
        for a in activities
    ]
    cost_summary = explanation.get("cost_summary")
    unknown_activity_ids = [
        str(activity.id)
        for activity in projected_activities
        if str(activity.cost_status or "UNKNOWN").upper() == "UNKNOWN"
    ]
    total_cost_status = (
        "PARTIAL_UNKNOWN"
        if unknown_activity_ids
        or (
            isinstance(cost_summary, Mapping)
            and int(cost_summary.get("unknown_item_count") or 0) > 0
        )
        else "KNOWN"
    )
    # A few older/replan writers persisted the compatibility integer without the
    # R86 explanation block.  Repair that public projection from the activity
    # evidence so every consumer sees PARTIAL_UNKNOWN instead of interpreting a
    # legacy zero as a free total.  This does not mutate the historical row.
    if unknown_activity_ids and not isinstance(cost_summary, Mapping):
        explanation = {
            **explanation,
            "cost_summary": {
                "known_subtotal_minor": int(item.total_cost_minor or 0),
                "unknown_item_count": len(unknown_activity_ids),
                "unknown_activity_ids": unknown_activity_ids,
                "unknown_cost_included": False,
                "total_is_not_free": True,
            },
        }
    return PlanSchema(
        id=item.id,
        journey_id=item.journey_id,
        version_no=item.version_no,
        is_current=str(current_plan_version_id or "") == item.id,
        planning_mode=item.planning_mode,
        status=item.status,
        lifecycle=item.lifecycle or "ACTIVE",
        trigger_contract=json.loads(item.trigger_contract_json or "{}"),
        source=item.source,
        parent_plan_version_id=item.parent_plan_version_id,
        total_cost=Money(amount_minor=item.total_cost_minor, currency=item.currency),
        total_cost_status=total_cost_status,
        utility_score=item.utility_score,
        score_breakdown=breakdown,
        activities=projected_activities,
        mobility_snapshot_id=item.mobility_snapshot_id,
        mobility_evidence=json.loads(item.mobility_evidence_json or "{}"),
        explanation=explanation,
        created_at=item.created_at or None,
        stale=stale,
        selectable=(item.lifecycle or "ACTIVE") != "STANDBY" and not stale and authority_blocker is None,
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
    if hours.get("always_available") is True:
        return 0, 24 * 60
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


def _latest_strategy_intent(session: Session, journey: Journey) -> dict[str, Any]:
    """Read the latest user mission as strategy input, never as a truth source."""

    mission = session.scalar(
        select(JourneyMission)
        .where(JourneyMission.journey_id == journey.id)
        .order_by(JourneyMission.created_at.desc(), JourneyMission.id.desc())
        .limit(1)
    )
    if mission is None:
        return {"user_text": str(journey.title or ""), "preferences": {}}
    try:
        preferences = json.loads(mission.preferences_json or "{}")
    except (TypeError, ValueError):
        preferences = {}
    return {
        "user_text": str(mission.user_text or journey.title or ""),
        "preferences": preferences if isinstance(preferences, dict) else {},
        "mission_id": mission.id,
    }


def _strategy_profile(intent: Mapping[str, Any]) -> str:
    text = " ".join(
        [
            str(intent.get("user_text") or ""),
            json.dumps(intent.get("preferences") or {}, ensure_ascii=False),
        ]
    ).casefold()
    if any(token in text for token in ("少走", "步行少", "low walking", "compact", "紧凑")):
        return "compact"
    if any(token in text for token in ("室内", "indoor", "museum", "美术馆", "工艺", "工藝")):
        return "indoor"
    if any(token in text for token in ("庭园", "庭園", "garden", "park", "公园", "公園", "heritage", "历史", "歷史", "long walk", "散步")):
        return "heritage_outdoor"
    return "balanced"


def _strategy_place_score(
    place: Place,
    profile: str,
    traveler_preferences: Sequence[Any] | None = None,
) -> tuple[int, int, str]:
    """Rank a provider Place from persisted preference evidence.

    Real-city ordering must be causal: a changed TravelerTwin should be able to
    change the order without changing hard facts.  The old profile/name heuristic
    remains only as a neutral legacy fallback when no preference evidence exists;
    it is never allowed to become a hard feasibility or authority signal.
    """

    if traveler_preferences is not None:
        tags = canonical_place_tags(
            category=place.category,
            tags=json.loads(place.tags_json or "[]"),
        )
        breakdown = verified_interest_preference_breakdown(
            traveler_preferences,
            candidate_tags=tags,
        )
        effective = breakdown.get("effective_preference_fit")
        if effective is not None:
            preference = round(float(effective) * 1000)
        else:
            preference = 0
    else:
        haystack = " ".join(
            [
                str(getattr(place, "name", "") or ""),
                str(getattr(place, "category", "") or ""),
                str(getattr(place, "tags_json", "") or ""),
            ]
        ).casefold()
        indoor = any(token in haystack for token in ("museum", "gallery", "craft", "工艺", "工藝", "美術", "美术", "文化館", "library"))
        outdoor = any(token in haystack for token in ("garden", "park", "庭園", "庭园", "viewpoint", "historic", "temple", "shrine", "heritage"))
        preference = (
            3 if profile == "indoor" and indoor else
            3 if profile == "heritage_outdoor" and outdoor else
            3 if profile == "compact" and indoor else
            1 if profile in {"indoor", "compact"} and outdoor else
            1
        ) * 100
    completeness = int(_place_window(place) is not None) + int(_place_verified_cost(place) is not None)
    return preference, completeness, place.id


def _strategy_order(
    places: Sequence[Place],
    profile: str,
    travel: Callable[[str, str], int],
    traveler_preferences: Sequence[Any] | None = None,
) -> list[list[Place]]:
    ranked = sorted(
        places,
        key=lambda place: (
            -_strategy_place_score(place, profile, traveler_preferences)[0],
            -_strategy_place_score(place, profile, traveler_preferences)[1],
            place.id,
        ),
    )
    if not ranked:
        return []
    compact = [ranked[0]]
    remaining = ranked[1:]
    while remaining and len(compact) < min(4, len(ranked)):
        previous = compact[-1]
        next_place = min(
            remaining,
            key=lambda place: (
                travel(previous.id, place.id),
                -_strategy_place_score(place, profile, traveler_preferences)[0],
                place.id,
            ),
        )
        compact.append(next_place)
        remaining.remove(next_place)
    diverse: list[Place] = []
    seen_categories: set[str] = set()
    for place in ranked:
        category = str(place.category or "").casefold()
        if category not in seen_categories or len(diverse) >= 4:
            diverse.append(place)
            seen_categories.add(category)
        if len(diverse) >= min(5, len(ranked)):
            break
    return [ranked, compact, diverse]


def _real_strategy_metrics(
    schedule: Sequence[CandidateActivity],
    places: Mapping[str, Place],
    mobility_provider: Any,
    journey: Journey,
    profile: str,
    traveler_preferences: Sequence[Any] | None = None,
) -> tuple[dict[str, float], dict[str, Any]]:
    """Derive candidate dimensions from measured facts, with neutral evidence made explicit."""

    total_cost = sum(int(item.cost_minor) for item in schedule)
    travel_minutes = 0
    degraded_edges = 0
    edge_count = 0
    previous: str | None = None
    for item in schedule:
        if previous is not None:
            fact = mobility_provider.facts.get((previous, item.place_id))
            if fact is not None:
                travel_minutes += int(fact.minutes)
                degraded_edges += int(bool(fact.degraded))
                edge_count += 1
        previous = item.place_id
    slack_minutes = 0
    known_opening = 0
    for item in schedule:
        window = _place_window(places[item.place_id])
        if window is None:
            continue
        known_opening += 1
        slack_minutes += max(0, window[1] - (item.end_at.hour * 60 + item.end_at.minute))
    known_cost = sum(
        int(_place_verified_cost(places[item.place_id]) is not None) for item in schedule
    )
    known_ratio = (known_opening + known_cost) / max(1, 2 * len(schedule))
    budget_fit = max(0.0, min(1.0, 1 - total_cost / max(1, journey.budget_amount_minor)))
    mobility_fit = max(0.0, min(1.0, 1 - travel_minutes / max(1, len(schedule) * 90)))
    time_reliability = max(0.0, min(1.0, slack_minutes / max(1, len(schedule) * 120)))
    robustness = known_ratio * (1 - degraded_edges / max(1, edge_count))
    preference_breakdowns = []
    if traveler_preferences is not None:
        for item in schedule:
            place = places[item.place_id]
            preference_breakdowns.append(
                verified_interest_preference_breakdown(
                    traveler_preferences,
                    candidate_tags=canonical_place_tags(
                        category=place.category,
                        tags=json.loads(place.tags_json or "[]"),
                    ),
                )
            )
    effective_values = [
        float(item["effective_preference_fit"])
        for item in preference_breakdowns
        if item.get("effective_preference_fit") is not None
    ]
    explicit_values = [
        float(item["explicit_preference_fit"])
        for item in preference_breakdowns
        if item.get("explicit_preference_fit") is not None
    ]
    learned_values = [
        float(item["learned_soft_preference_fit"])
        for item in preference_breakdowns
        if item.get("learned_soft_preference_fit") is not None
    ]
    if effective_values:
        preference_fit = sum(effective_values) / len(effective_values)
        preference_basis = "canonical Place category/tags with explicit and LEARNED_SOFT evidence"
    elif profile == "balanced":
        preference_fit = 0.5
        preference_basis = "NEUTRAL_NO_PREFERENCE_SIGNAL"
    else:
        preference_fit = sum(
            _strategy_place_score(places[item.place_id], profile, traveler_preferences)[0] >= 300
            for item in schedule
        ) / max(1, len(schedule))
        preference_basis = "canonical Place category/tags and explicit mission profile"
    metrics = {
        "commitment_preservation": 1.0,
        "stability": round(time_reliability, 6),
        "time_reliability": round(time_reliability, 6),
        "preference_fit": round(preference_fit, 6),
        "mobility_fit": round(mobility_fit, 6),
        "budget_fit": round(budget_fit, 6),
        "robustness": round(robustness, 6),
        "opportunity_value": round(preference_fit * known_ratio, 6),
    }
    provenance = {
        "strategy_profile": profile,
        "metric_values": dict(metrics),
        "travel_minutes": travel_minutes,
        "degraded_edges": degraded_edges,
        "edge_count": edge_count,
        "slack_minutes": slack_minutes,
        "verified_opening_count": known_opening,
        "verified_cost_count": known_cost,
        "preference_fit_basis": preference_basis,
        "explicit_preference_fit": round(sum(explicit_values) / len(explicit_values), 6) if explicit_values else None,
        "learned_soft_preference_fit": round(sum(learned_values) / len(learned_values), 6) if learned_values else None,
        "effective_preference_fit": round(sum(effective_values) / len(effective_values), 6) if effective_values else None,
        "learned_soft_is_authority": False,
        "score_sources": {
            "budget_fit": "verified activity cost / journey budget",
            "mobility_fit": "LIVE measured travel minutes",
            "time_reliability": "opening close minus scheduled end",
            "robustness": "fact completeness and degraded live edges",
        },
    }
    return metrics, provenance


def _strategy_voi_decisions(
    session: Session | None,
    journey: Journey | None,
    places: Mapping[str, Place],
    intent: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Choose the next decision-sensitive fact without promoting it to truth."""

    decisions: list[dict[str, Any]] = []
    traveler_preferences = (
        planning_preference_items(session, journey.traveler_id, journey.id)
        if session is not None and journey is not None
        else []
    )
    for place in places.values():
        missing: list[str] = []
        if _place_window(place) is None:
            missing.append("opening_hours")
        if _place_verified_cost(place) is None:
            missing.append("cost")
        if not missing or not _catalogue_visit_eligible(place):
            continue
        metadata = _place_metadata(place)
        binding = metadata.get("fact_source_binding") if isinstance(metadata, Mapping) else None
        fact_acquisition = metadata.get("fact_acquisition") if isinstance(metadata, Mapping) else None
        if not binding and isinstance(fact_acquisition, Mapping):
            binding = fact_acquisition.get("source_binding")
        tags = json.loads(place.tags_json or "[]") if place.tags_json else []
        source_url = None
        if isinstance(binding, Mapping):
            source_url = binding.get("source_url")
        if not source_url and isinstance(tags, Mapping):
            source_url = tags.get("website") or tags.get("contact:website") or tags.get("url")
        profile = _strategy_profile(intent)
        preference_signal = _strategy_place_score(place, profile, traveler_preferences)[0]
        value = (
            (3000 if "opening_hours" in missing else 0)
            + (2000 if "cost" in missing else 0)
            + preference_signal
        )
        decisions.append(
            {
                "place_id": place.id,
                "missing_fact": missing,
                "value": value,
                "why": "missing facts can change strict feasibility or ranking",
                "source_candidate": source_url,
                "action": "acquire_exact_bound_source" if source_url else "search_and_corroborate_source",
                "strategy_profile": profile,
            }
        )
    decisions.sort(key=lambda item: (-int(item["value"]), str(item["place_id"])))
    return decisions[:3]


def _strategy_pivot_decisions(
    places: Mapping[str, Place],
    intent: Mapping[str, Any],
    traveler_preferences: Sequence[Any] | None = None,
) -> list[dict[str, Any]]:
    """Record a deliberate pivot after a source path yields no structured facts."""

    profile = _strategy_profile(intent)
    abandoned: list[Place] = []
    for place in places.values():
        if not _place_has_unresolved_visit_facts(place):
            continue
        acquisition = _place_metadata(place).get("fact_acquisition")
        official = acquisition.get("official_web") if isinstance(acquisition, Mapping) else None
        if isinstance(official, Mapping) and official.get("status") in {
            "NO_STRUCTURED_FACTS",
            "UNAVAILABLE",
        }:
            abandoned.append(place)
    if not abandoned:
        return []
    replacements = [
        place
        for place in places.values()
        if _catalogue_visit_eligible(place)
        and _place_window(place) is not None
        and _place_verified_cost(place) is not None
        and place not in abandoned
    ]
    replacements.sort(
        key=lambda place: (
            -_strategy_place_score(place, profile, traveler_preferences)[0],
            str(place.id),
        )
    )
    return [{
        "decision": "ABANDON_SOURCE_PATH",
        "abandoned_place_ids": [place.id for place in abandoned],
        "replacement_place_ids": [place.id for place in replacements[:3]],
        "reason": "source returned no structured facts; preserve UNKNOWN and switch to verified catalogue rows",
        "strategy_profile": profile,
    }]


def _catalogue_templates(
    places: Mapping[str, Place],
    travel: Callable[[str, str], int],
    *,
    day_start_minutes: int = 9 * 60,
    variants: int = 3,
    intent: Mapping[str, Any] | None = None,
    traveler_preferences: Sequence[Any] | None = None,
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
    the literal windows are ones the gate can honour.  Strict candidates use only places with
    provider-backed cost evidence; UNKNOWN-cost rows remain in the catalog and provisional lane
    but cannot be smuggled into a selectable candidate.  The latest natural-language mission
    orders a bounded primary, compact, and diverse search; no static quality metrics or queue
    rotation is used to pretend that candidates differ.
    """

    eligible = sorted(
        (
            place
            for place in places.values()
            if _place_window(place)
            and _catalogue_visit_eligible(place)
            and _place_verified_cost(place) is not None
        ),
        key=lambda place: place.id,
    )
    if not eligible:
        return []
    profile = _strategy_profile(intent or {})
    orders = _strategy_order(
        eligible,
        profile,
        travel,
        traveler_preferences,
    )[: max(1, variants)]

    def pack(sequence: Sequence[Place]) -> list[tuple[str, str, str, str]]:
        schedule: list[tuple[str, str, str, str]] = []
        previous_end: int | None = None
        previous: str | None = None
        for place in sequence:
            window = _place_window(place)
            if window is None:
                continue
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
    for ordered in orders:
        packed = pack(ordered)
        if len(packed) >= 2 and packed not in templates:
            templates.append(packed)
    return templates


def _mobility_catalogue_place_ids(
    places: Mapping[str, Place],
    raw_templates: Sequence[Sequence[tuple[str, str, str, str]]],
    anchors: Sequence[Any],
) -> tuple[str, ...]:
    """Return only nodes that can participate in a strict catalogue schedule.

    Unknown opening windows cannot enter ``_catalogue_templates`` and therefore do
    not need a live edge in the strict matrix.  Keeping them out here bounds the
    real provider work for a destination whose catalog also contains unresolved
    rows; the provisional lane captures its own two selected rows later when it
    is actually needed.
    """

    if raw_templates:
        candidate_ids = [
            place_id
            for template in raw_templates
            for place_id, *_ in template
            if place_id in places
        ]
    else:
        candidate_ids = [
            place.id
            for place in places.values()
            if _place_window(place) and _catalogue_visit_eligible(place)
        ]
    return tuple(
        dict.fromkeys(
            [
                *candidate_ids,
                *(anchor.place_id for anchor in anchors if anchor.place_id in places),
            ]
        )
    )


def _provisional_catalogue_plan(
    session: Session,
    journey: Journey,
    planning_mode: str,
    preferred_place_ids: Sequence[str] | None = None,
) -> list[PlanVersion]:
    """Materialize a useful, explicitly non-authoritative real-catalogue draft.

    This is only reached when strict catalogue composition has no opening-window
    sequence.  It uses the actual provider Place rows, keeps their unresolved
    facts visible, and leaves selection to the shared authority gate.
    """

    places = [
        place
        for place in session.scalars(
            select(Place).where(Place.city_code == journey.city_code)
        ).all()
        if _catalogue_visit_eligible(place)
        and place.latitude is not None
        and place.longitude is not None
        and (preferred_place_ids is None or place.id in preferred_place_ids)
    ]
    if len(places) < 2:
        return []
    preferred = {"park": 0, "viewpoint": 1, "attraction": 2, "landmark": 3}
    requested_order = {
        str(place_id): index
        for index, place_id in enumerate(preferred_place_ids or ())
    }
    strategy_intent = _latest_strategy_intent(session, journey)
    strategy_profile = _strategy_profile(strategy_intent)
    traveler_preferences = planning_preference_items(session, journey.traveler_id, journey.id)
    places.sort(
        key=lambda item: (
            -int(_coursework_facts_cover(item)),
            -_strategy_place_score(item, strategy_profile, traveler_preferences)[0],
            -_strategy_place_score(item, strategy_profile, traveler_preferences)[1],
            -int(_coursework_estimate_covers(item)),
            0 if item.id in requested_order else 1,
            requested_order.get(item.id, preferred.get(str(item.category).casefold(), 9)),
            item.id,
        )
    )
    selected = places[:2]
    pairs = tuple(
        (origin.id, destination.id)
        for origin in selected
        for destination in selected
        if origin.id != destination.id
    )
    mobility = capture_runtime_mobility_snapshot(
        session,
        journey,
        pairs,
        operation_key=f"provisional-compose:{journey.id}",
    ).snapshot
    cursor = parse_dt(journey.start_at)
    rows: list[tuple[Place, datetime, datetime, int]] = []
    previous: str | None = None
    strategy_estimates: list[dict[str, Any]] = []
    estimated_total_cost = 0
    for place in selected:
        travel = _safe_estimate_minutes(mobility, previous, place.id) if previous else 0
        begin = cursor + timedelta(minutes=travel)
        estimate = _place_strategy_estimate(place)
        if estimate is not None:
            opening_estimate = estimate.get("opening_hours")
            if isinstance(opening_estimate, Mapping):
                open_minutes = opening_estimate.get("open_minutes")
                try:
                    if open_minutes is not None:
                        estimated_open = datetime.combine(
                            begin.date(),
                            datetime.min.time(),
                            begin.tzinfo,
                        ) + timedelta(minutes=int(open_minutes))
                        begin = max(begin, estimated_open)
                except (TypeError, ValueError, OverflowError):
                    pass
            cost_estimate = estimate.get("cost_evidence")
            if isinstance(cost_estimate, Mapping) and cost_estimate.get("amount_minor") is not None:
                try:
                    amount = int(cost_estimate["amount_minor"])
                except (TypeError, ValueError):
                    amount = 0
                if amount >= 0:
                    estimated_total_cost += amount
            strategy_estimates.append(
                {
                    "place_id": place.id,
                    "status": "ESTIMATED_AGENT",
                    "authority_eligible": False,
                    "basis": estimate.get("basis"),
                    "opening_hours": opening_estimate or {"status": "ESTIMATED_AGENT"},
                    "cost_evidence": cost_estimate or {"status": "ESTIMATED_AGENT"},
                }
            )
        end = begin + timedelta(minutes=_place_duration(place))
        rows.append((place, begin, end, travel))
        cursor = end
        previous = place.id
    if rows[-1][2] > parse_dt(journey.end_at):
        return []

    unresolved: list[dict[str, Any]] = []
    unknown_cost_count = 0
    for place in selected:
        opening = json.loads(place.opening_hours_json or "{}") if place.opening_hours_json else {}
        opening_status = str(opening.get("status") or "UNKNOWN").upper()
        if opening_status not in {"NORMALIZED_VALID", "OFFICIAL_WEB_VERIFIED", "VERIFIED_PROVIDER"}:
            unresolved.append(
                {
                    "place_id": place.id,
                    "place_name": place.name,
                    "fact": "opening",
                    "state": "REQUIRES_CONFIRMATION",
                    "reason": "No provider opening window was captured",
                }
            )
        cost = _place_metadata(place).get("cost_evidence")
        cost_status = str(cost.get("status") or "UNKNOWN").upper() if isinstance(cost, Mapping) else "UNKNOWN"
        if cost_status not in KNOWN_COST_EVIDENCE_STATUSES or not isinstance(cost, Mapping) or cost.get("amount_minor") is None:
            unknown_cost_count += 1
            unresolved.append(
                {
                    "place_id": place.id,
                    "place_name": place.name,
                    "fact": "cost",
                    "state": "UNKNOWN",
                    "reason": "No provider cost evidence was captured",
                }
            )

    if not unresolved:
        return []
    coursework_authority = (
        settings.lab_estimate_authority
        and bool(strategy_estimates)
        and all(_coursework_facts_cover(place) for place in selected)
    )
    version_no = (
        session.scalar(
            select(PlanVersion.version_no)
            .where(PlanVersion.journey_id == journey.id)
            .order_by(PlanVersion.version_no.desc())
            .limit(1)
        )
        or 0
    ) + 1
    plan_id = deterministic_id(
        f"plan:{journey.id}:{version_no}:provisional:{','.join(item.id for item in selected)}"
    )
    explanation = {
        "reason": "Real provider Places arranged as a provisional itinerary",
        "authority_state": (
            "COURSEWORK_ESTIMATED_FACTS"
            if coursework_authority
            else "PROVISIONAL_UNRESOLVED_FACTS"
        ),
        "authority_envelope": (
            COURSEWORK_ESTIMATE_AUTHORITY_ENVELOPE if coursework_authority else None
        ),
        "authority_note": (
            "Coursework-only selection using explicit Agent estimates; this is not a LIVE provider-ready result."
            if coursework_authority
            else None
        ),
        "unresolved_facts": unresolved,
        "cost_summary": {
            "status": "PARTIAL",
            "known_subtotal_minor": sum(_place_verified_cost(place) or 0 for place in selected),
            "unknown_item_count": unknown_cost_count,
            "currency": journey.budget_currency,
        },
        "opening_summary": {
            "status": "REQUIRES_CONFIRMATION",
            "unknown_item_count": sum(1 for item in unresolved if item["fact"] == "opening"),
        },
        "strategy_estimate_summary": {
            "status": "ESTIMATED_AGENT" if strategy_estimates else "NONE",
            "authority_eligible": coursework_authority,
            "authority_envelope": (
                COURSEWORK_ESTIMATE_AUTHORITY_ENVELOPE if coursework_authority else None
            ),
            "estimated_total_cost_minor": estimated_total_cost,
            "facts": strategy_estimates,
            "note": (
                "Coursework estimate envelope is enabled for this isolated lab; values remain explicitly estimated."
                if coursework_authority
                else "Estimates guide this provisional strategy only; canonical authority still requires provider evidence."
            ),
        },
        "mobility_snapshot_id": mobility.snapshot_id,
        "mobility_provider": mobility.source_provider_key,
    }
    now = iso_now()
    plan = PlanVersion(
        id=plan_id,
        journey_id=journey.id,
        version_no=version_no,
        planning_mode=planning_mode,
        status="CANDIDATE",
        source="INITIAL",
        parent_plan_version_id=None,
        total_cost_minor=sum(_place_verified_cost(place) or 0 for place in selected),
        currency=journey.budget_currency,
        estimated_duration_min=sum(
            int((end - begin).total_seconds() // 60) + travel
            for _place, begin, end, travel in rows
        ),
        utility_score=None,
        score_breakdown_json=None,
        explanation_json=dump(explanation),
        mobility_snapshot_id=mobility.snapshot_id,
        mobility_evidence_json=dump(mobility.to_evidence()),
        created_at=now,
    )
    session.add(plan)
    session.flush()
    for sequence, (place, begin, end, travel) in enumerate(rows, start=1):
        metadata = _place_metadata(place)
        cost = metadata.get("cost_evidence")
        known_cost = (
            isinstance(cost, Mapping)
            and str(cost.get("status") or "UNKNOWN").upper() in KNOWN_COST_EVIDENCE_STATUSES
            and cost.get("amount_minor") is not None
        )
        opening = json.loads(place.opening_hours_json or "{}") if place.opening_hours_json else {}
        session.add(
            Activity(
                id=deterministic_id(f"activity:{plan_id}:{sequence}"),
                plan_version_id=plan.id,
                sequence_no=sequence,
                activity_type="visit",
                title=place.name,
                place_id=place.id,
                city_service_id=None,
                start_at=begin.isoformat(),
                end_at=end.isoformat(),
                travel_from_prev_min=travel,
                estimated_cost_minor=int(cost.get("amount_minor") if known_cost else 0),
                currency=journey.budget_currency,
                status="PLANNED",
                locked=False,
                metadata_json=dump(
                    {
                        "compose_source": "PROVISIONAL_REAL_CATALOGUE",
                        "category": place.category,
                        "opening_state": str(opening.get("status") or "UNKNOWN"),
                        "opening_hours": opening,
                        "cost_evidence": cost if isinstance(cost, Mapping) else {"status": "UNKNOWN"},
                        "cost_status": "KNOWN" if known_cost else "UNKNOWN",
                        "price_known": known_cost,
                        "strategy_estimate": _place_strategy_estimate(place),
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
        action="plans.generated.provisional",
        entity_type="PlanVersion",
        entity_id=plan.id,
        journey_id=journey.id,
        after={
            "place_ids": [item.id for item in selected],
            "authority_state": explanation["authority_state"],
            "unresolved_facts": unresolved,
        },
    )
    return [plan]


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
    *,
    allow_single_place: bool = False,
    allow_unknown_cost: bool = False,
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
    if len(places) < 2 and not (allow_single_place and len(places) == 1):
        raise ValueError("GENERIC_COMPOSE_REQUIRES_TWO_PLACES")

    traveler_preferences = planning_preference_items(session, journey.traveler_id, journey.id)

    # Agent discovery commonly hands this seam the whole real-city catalogue.
    # Preserve caller order for demo/fixture and mixed inputs, but let a
    # provider-backed city use the same fact/profile signal as the native
    # strategy path. Without this bridge, the explicit-POI seam quietly
    # reintroduced Place.id order and a verified venue could lose to unresolved
    # rows merely because their IDs sorted first.
    provider_backed = len(places) >= 2 and all(
        str(place.provider_key or "").casefold() not in {"mock-city", "demo", "fixture"}
        and str(place.external_id or "").casefold().startswith("overpass:")
        for place in places.values()
    )
    if provider_backed:
        strategy_intent = _latest_strategy_intent(session, journey)
        strategy_profile = _strategy_profile(strategy_intent)
        ranked_places = sorted(
            places.values(),
            key=lambda place: (
                -_strategy_place_score(place, strategy_profile, traveler_preferences)[0],
                -_strategy_place_score(place, strategy_profile, traveler_preferences)[1],
                str(place.id),
            ),
        )
        ranked = [place.id for place in ranked_places]
        if ranked != selected_ids:
            record_audit(
                session,
                action="strategy.explicit_catalogue_reordered",
                entity_type="Journey",
                entity_id=journey.id,
                journey_id=journey.id,
                after={
                    "strategy_profile": strategy_profile,
                    "requested_place_ids": selected_ids,
                    "ranked_place_ids": ranked,
                    "ranking_basis": "provider fact completeness, preference category/tags, stable id tie-break",
                },
                actor_type="strategy_engine",
                actor_id="r14-autonomous-strategy",
            )
            selected_ids = ranked

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
    # Mobility remains the primary physical constraint.  When two orders carry
    # the same measured travel burden, preserve the already computed strategy
    # ranking as the deterministic tie-break so LEARNED_SOFT evidence can cause
    # a real order change instead of disappearing behind Place.id.
    strategy_rank = {place_id: index for index, place_id in enumerate(selected_ids)}
    ordered_ids = min(
        bounded_orders,
        key=lambda order: (
            sum(
                mobility_provider.estimate_minutes(order[index - 1], order[index])
                for index in range(1, len(order))
            ),
            tuple(strategy_rank.get(place_id, len(selected_ids)) for place_id in order),
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
    unknown_cost_place_ids: list[str] = []

    for place_id in ordered_ids:
        place = places[place_id]
        metadata = json.loads(place.metadata_json or "{}")
        verified_cost = _place_verified_cost(place)
        unknown_cost = verified_cost is None
        if unknown_cost and not allow_unknown_cost:
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
        if unknown_cost and allow_unknown_cost:
            selected_bootstrap = session.scalar(
                select(RealPlaceCandidate.id).where(
                    RealPlaceCandidate.journey_id == journey.id,
                    RealPlaceCandidate.materialized_place_id == place_id,
                    RealPlaceCandidate.status == "BOOTSTRAP_SELECTED",
                ).limit(1)
            )
            if (
                str(place.provider_key or "").casefold() != "overpass"
                or not str(place.external_id or "").startswith("overpass:")
                or not selected_bootstrap
            ):
                raise CompositionRefusal(
                    f"GENERIC_COMPOSE_UNKNOWN_COST_NOT_SELECTED_BOOTSTRAP_PLACE:{place_id}",
                    reason_code=PLACE_COST_UNVERIFIED,
                    details={"city_code": journey.city_code, "place_ids": [place_id]},
                )
            if _place_has_unresolved_opening(place):
                raise CompositionRefusal(
                    f"GENERIC_COMPOSE_OPENING_UNVERIFIED:{place_id}",
                    reason_code=UNRESOLVED_ACTIVITY_FACTS,
                    details={"city_code": journey.city_code, "place_ids": [place_id]},
                )
        if unknown_cost:
            unknown_cost_place_ids.append(place_id)
        # The Activity schema retains an integer compatibility slot.  An
        # UNKNOWN price is accompanied by explicit metadata and is never a
        # claim that the attraction is free or included in the subtotal.
        cost_minor = int(verified_cost) if verified_cost is not None else 0
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
    strategy_intent = _latest_strategy_intent(session, journey)
    strategy_profile = _strategy_profile(strategy_intent)
    schedule = [
        CandidateActivity(
            place_id=place.id,
            title=place.name,
            start_at=start_at,
            end_at=end_at,
            cost_minor=cost_minor,
            metadata={"category": place.category},
        )
        for place, start_at, end_at, _travel_min, cost_minor in rows
    ]
    metrics, score_provenance = _real_strategy_metrics(
        schedule,
        places,
        mobility_provider,
        journey,
        strategy_profile,
        traveler_preferences,
    )
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
                "authority_state": (
                    COST_UNKNOWN_ACCEPTED
                    if unknown_cost_place_ids
                    else "LIVE_PROVIDER_FACTS"
                ),
                "cost_unknown_policy": (
                    COST_UNKNOWN_USER_POLICY if unknown_cost_place_ids else None
                ),
                "unresolved_facts": [
                    {
                        "kind": "ADMISSION_PRICE",
                        "place_id": place_id,
                        "status": "UNKNOWN",
                        "resolution": "USER_ACCEPTED_PRICE_NOT_INCLUDED",
                    }
                    for place_id in unknown_cost_place_ids
                ],
                "cost_summary": {
                    "known_subtotal_minor": total_cost,
                    "unknown_item_count": len(unknown_cost_place_ids),
                    "unknown_place_ids": list(unknown_cost_place_ids),
                    "unknown_cost_included": False,
                    "total_is_not_free": bool(unknown_cost_place_ids),
                },
                "strategy_profile": strategy_profile,
                "strategy_intent": strategy_intent,
                "score_provenance": score_provenance,
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
                        "cost_status": (
                            "UNKNOWN" if _place_verified_cost(place) is None else "KNOWN"
                        ),
                        "price_known": _place_verified_cost(place) is not None,
                        "cost_included_in_total": _place_verified_cost(place) is not None,
                        "cost_evidence": _place_metadata(place).get("cost_evidence")
                        or {"status": "UNKNOWN", "amount_minor": None},
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
    *,
    allow_single_place: bool = False,
    allow_unknown_cost: bool = False,
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
    if len(ordered) < 2 and not (allow_single_place and len(ordered) == 1):
        raise ValueError("GENERIC_COMPOSE_REQUIRES_TWO_PLACES")

    places_for_scope = {
        place.id: place
        for place in session.scalars(select(Place).where(Place.id.in_(ordered))).all()
    }
    provider_backed = len(places_for_scope) >= 2 and all(
        str(place.provider_key or "").casefold() not in {"mock-city", "demo", "fixture"}
        and str(place.external_id or "").casefold().startswith("overpass:")
        for place in places_for_scope.values()
    )
    if provider_backed:
        strategy_intent = _latest_strategy_intent(session, journey)
        strategy_profile = _strategy_profile(strategy_intent)
        traveler_preferences = planning_preference_items(session, journey.traveler_id, journey.id)
        ranked = [
            place.id
            for place in sorted(
                places_for_scope.values(),
                key=lambda place: (
                    -_strategy_place_score(place, strategy_profile, traveler_preferences)[0],
                    -_strategy_place_score(place, strategy_profile, traveler_preferences)[1],
                    str(place.id),
                ),
            )
        ]
        if ranked != ordered:
            record_audit(
                session,
                action="strategy.explicit_catalogue_reordered",
                entity_type="Journey",
                entity_id=journey.id,
                journey_id=journey.id,
                after={
                    "strategy_profile": strategy_profile,
                    "requested_place_ids": ordered,
                    "ranked_place_ids": ranked,
                    "ranking_basis": "provider fact completeness, preference category/tags, stable id tie-break",
                },
                actor_type="strategy_engine",
                actor_id="r14-autonomous-strategy",
            )
            ordered = ranked
            places_for_scope = {
                place_id: places_for_scope[place_id]
                for place_id in ordered
            }
    lodging_ids = [
        place_id
        for place_id, place in places_for_scope.items()
        if not _catalogue_visit_eligible(place)
        and (
            str(place.category or "").casefold()
            in {"hotel", "lodging", "accommodation", "guest_house", "hostel"}
            or str(_place_metadata(place).get("place_role") or "").casefold()
            in {"lodging", "accommodation"}
        )
    ]
    if lodging_ids:
        raise CompositionRefusal(
            "GENERIC_COMPOSE_LODGING_IS_NOT_A_VISIT",
            reason_code=LODGING_ROWS_USED_AS_GENERIC_VISITS,
            details={"city_code": journey.city_code, "place_ids": lodging_ids},
        )

    # Distinct scopes, longest first. The one-place scope exists only for the
    # explicit zero-catalogue bootstrap opt-in.
    scopes: list[list[str]] = []
    if allow_single_place and len(ordered) == 1:
        scopes = [ordered]
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
            compose_options: dict[str, bool] = {}
            if allow_single_place:
                compose_options["allow_single_place"] = True
            if allow_unknown_cost:
                compose_options["allow_unknown_cost"] = True
            created.extend(
                _generic_compose_plans(
                    session,
                    journey,
                    planning_mode,
                    scope,
                    **compose_options,
                )
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
        provider_places = [
            places_for_scope.get(place_id)
            for place_id in ordered
            if places_for_scope.get(place_id) is not None
        ]
        all_cost_failures = bool(failures) and len(names) == len(failures) and all(
            name == PLACE_COST_UNVERIFIED for name in names
        )
        provider_backed = len(provider_places) >= (1 if allow_single_place else 2) and all(
            str(place.provider_key or "").casefold() not in {"mock-city", "demo", "fixture"}
            and str(place.external_id or "").casefold().startswith("overpass:")
            for place in provider_places
        )
        if all_cost_failures and provider_backed and not allow_unknown_cost:
            provisional = _provisional_catalogue_plan(
                session,
                journey,
                planning_mode,
                preferred_place_ids=ordered,
            )
            if provisional:
                record_audit(
                    session,
                    action="plans.generated.provisional_from_explicit_catalogue",
                    entity_type="Journey",
                    entity_id=journey.id,
                    journey_id=journey.id,
                    after={
                        "requested_place_ids": ordered,
                        "reason_code": PLACE_COST_UNVERIFIED,
                        "authority_state": "PROVISIONAL_UNRESOLVED_FACTS",
                    },
                )
                return provisional
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
    allow_single_place: bool = False,
    allow_unknown_cost: bool = False,
) -> list[PlanVersion]:
    if place_ids:
        return _generic_compose_variants(
            session,
            journey,
            planning_mode,
            place_ids,
            candidate_count,
            allow_single_place=allow_single_place,
            allow_unknown_cost=allow_unknown_cost,
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
    strategy_intent = _latest_strategy_intent(session, journey)
    strategy_profile = _strategy_profile(strategy_intent)
    traveler_preferences = planning_preference_items(session, journey.traveler_id, journey.id)
    voi_decisions = _strategy_voi_decisions(session, journey, places, strategy_intent)
    pivot_decisions = _strategy_pivot_decisions(
        places,
        strategy_intent,
        traveler_preferences,
    )
    record_audit(
        session,
        action="strategy.value_of_information",
        entity_type="Journey",
        entity_id=journey.id,
        journey_id=journey.id,
        after={
            "strategy_profile": strategy_profile,
            "intent": strategy_intent,
            "decisions": voi_decisions,
            "pivots": pivot_decisions,
            "outcome": "prioritize_missing_facts_before broad acquisition",
        },
        actor_type="strategy_engine",
        actor_id="r14-autonomous-strategy",
    )
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
    # Capture one frozen mobility matrix over every node a candidate can visit
    # (template POIs plus resolved anchors) *before* scheduling, so the
    # scheduler inserts real travel gaps and the gate consumes the exact same
    # frozen evidence.
    schedulable_ids = _mobility_catalogue_place_ids(places, raw_templates, anchors)
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
            intent=strategy_intent,
            traveler_preferences=traveler_preferences,
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
        unverified_cost_places = [
            item.place_id
            for item in schedule
            if item.anchor_role is None
            and item.place_id in places
            and _place_verified_cost(places[item.place_id]) is None
        ]
        if unverified_cost_places:
            rejection = {
                "reason_codes": [PLACE_COST_UNVERIFIED],
                "summary": "candidate dropped before persistence: provider cost evidence is UNKNOWN",
                "place_ids": unverified_cost_places,
                "candidate_sequence": offset,
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
        total_cost = sum(int(item.cost_minor) for item in schedule)
        if total_cost > journey.budget_amount_minor:
            continue
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
        metrics, score_provenance = _real_strategy_metrics(
            schedule,
            places,
            mobility_provider,
            journey,
            strategy_profile,
            traveler_preferences,
        )
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
        explanation_json=dump(
            {
                "reason": "Strategy-ranked provider-backed candidate",
                "strategy_profile": strategy_profile,
                "strategy_intent": strategy_intent,
                "voi_decisions": voi_decisions,
                "pivot_decisions": pivot_decisions,
                "score_provenance": score_provenance,
                "mobility_snapshot_id": mobility_provider.snapshot_id,
            }
        ),
            mobility_snapshot_id=mobility_provider.snapshot_id,
            mobility_evidence_json=dump(mobility_provider.to_evidence()),
            created_at=now,
        )
        session.add(plan)
        session.flush()
        for sequence, item in enumerate(schedule, start=1):
            aid = deterministic_id(f"activity:{plan_id}:{sequence}")
            travel = activity_facts[sequence - 1].travel_from_prev_min
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
                    metadata_json=dump(dict(item.metadata)),
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
        # A real catalogue with no opening-window sequence is not the same as
        # an empty catalogue.  Preserve its provider Places as a visible draft
        # and keep strict composition as the only path to authority.
        if not raw_templates:
            provisional = _provisional_catalogue_plan(session, journey, planning_mode)
            if provisional:
                return provisional
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
