from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.core.audit import record_audit
from app.replanning.commitment_remap import (
    RemapConflict as CommitmentRemapConflict,
    resolve_commitment_target,
)
from app.operations.reservation_identity import (
    ANCHOR_RETIRE_MARKER,
    quarantine_duplicate_confirmation_identities,
    retire_superseded_active_anchor,
)
from app.db.models import (
    AccommodationOfferSnapshot,
    Activity,
    AutopilotEnrollment,
    Booking,
    Commitment,
    EdgeAction,
    EdgeDevice,
    EdgeObservation,
    Event,
    ExternalReservationDraft,
    GuardianRun,
    Journey,
    JourneyLeg,
    JourneyOperationsContext,
    JourneyTripContext,
    JourneyTwin,
    LocationObservation,
    MobilityFact,
    NotificationEvent,
    OfflineJourneySnapshot,
    Place,
    PlanVersion,
    PushSubscription,
    StayJourneyOption,
    TransportReservation,
)
from app.execution.clock import execution_clock
from app.planning.anchors import (
    LOCATION_RESOLVED,
    STRUCTURAL_ANCHOR_KEYS,
    resolve_anchor_key,
)
from app.integrations.inbox.ics_parser import parse_reservation_calendar
from app.integrations.push import deliver_web_push
from app.services import validate_plan_feasibility
from app.replanning.provider_snapshots import MobilitySnapshot, TravelEstimateFact
from app.stay_mobility import (
    build_candidate_nodes,
    journey_leg_payload,
    offer_payload,
)

# Traveller-facing names for the fields that block confirming a booking.  The
# API error must not surface internal identifiers such as ``semantic_target``.
_RESERVATION_FIELD_LABELS = {
    "reservation_type": "booking type",
    "origin": "departure place",
    "destination": "arrival place",
    "scheduled_departure_at": "departure time",
    "scheduled_arrival_at": "arrival time",
    "confirmation_ref": "booking reference",
    "anchor_role": "which part of the trip this booking belongs to",
    "semantic_target": "which stop in your trip this booking belongs to",
}


def reservation_field_label(field: str) -> str:
    """Human-readable name for one unresolved booking field."""

    key = str(field or "").strip()
    return _RESERVATION_FIELD_LABELS.get(key, key.replace("_", " ") or "booking detail")


CRITICAL_RESERVATION_FIELDS = (
    "reservation_type",
    "origin",
    "destination",
    "scheduled_departure_at",
    "scheduled_arrival_at",
    "confirmation_ref",
)
EDGE_ALLOWED_ACTIONS = {"ACKNOWLEDGE", "NEXT", "REJECT_PROPOSAL", "SOS"}


@dataclass(frozen=True)
class MaterializationResult:
    plan: PlanVersion | None
    gate_status: str
    failures: tuple[str, ...]
    locked_commitments_preserved: bool


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _load(value: str | None, fallback: Any) -> Any:
    try:
        return json.loads(value or "")
    except (TypeError, json.JSONDecodeError):
        return fallback


def _execution_truth(
    activities: list[Activity],
    legs: list[JourneyLeg],
    current_activity_id: str | None,
) -> dict[str, Any]:
    """Return the shared NOW/NEXT projection consumed by mobile and Edge.

    The source of truth is the persisted Activity state machine.  These
    clients must not infer execution from wall-clock time alone because a
    traveler may be en-route, have arrived, or have explicitly skipped a
    step ahead of its scheduled window.
    """

    terminal = {"COMPLETED", "SKIPPED", "CANCELLED"}
    current_index = next(
        (index for index, item in enumerate(activities) if item.id == current_activity_id),
        None,
    )
    # The cursor is an assertion about identity, so "absent" and "unresolvable"
    # are different facts and must not share a branch.  A null cursor is a legal
    # pre-execution state: the journey has started and nothing has been completed
    # yet, so projecting the first pending activity is the truth.  A cursor that
    # names a specific activity which is not on this plan — because it was
    # deleted, or because a replan replaced the plan underneath it — is stale or
    # corrupt state.  Falling back there would render a plausible Now/Next/NextLeg
    # that the authoritative record does not support, which is worse than
    # rendering nothing: a traveller would act on it.
    stale_cursor = current_activity_id is not None and current_index is None
    if current_activity_id is None:
        current_index = next(
            (index for index, item in enumerate(activities) if item.status not in terminal),
            len(activities),
        )
    elif stale_cursor:
        current_index = len(activities)
    now_item = (
        activities[current_index]
        if 0 <= current_index < len(activities) and activities[current_index].status not in terminal
        else None
    )
    next_item = next(
        (item for item in activities[current_index + 1 :] if item.status not in terminal),
        None,
    )
    # The leg is the transfer *out of* Now, so it needs a Now: binding it to the
    # index alone leaks a leg whenever current_index sits past the last activity
    # (a stale cursor, or every activity terminal) and the plan carries more legs
    # than activities — arrival and departure transfers do exactly that.  The
    # traveller would then see "walk 23 min" with no step to walk to.
    next_leg = (
        legs[current_index]
        if now_item is not None and 0 <= current_index < len(legs)
        else None
    )

    def compact(item: Activity | None) -> dict[str, Any] | None:
        if item is None:
            return None
        return {
            "id": item.id,
            "title": item.title,
            "state": item.status,
            "start_at": item.start_at,
            "end_at": item.end_at,
            "locked": bool(item.locked),
        }

    return {
        "now": compact(now_item),
        "next": compact(next_item),
        "now_activity_id": now_item.id if now_item else None,
        "next_activity_id": next_item.id if next_item else None,
        "now_state": now_item.status if now_item else ("STALE_CURSOR" if stale_cursor else "COMPLETE"),
        "next_state": next_item.status if next_item else None,
        "next_mode": next_leg.mode if next_leg else "NONE",
        "next_eta_min": next_leg.duration_min if next_leg else 0,
        # The oracle compares the leg itself, not only what was derived from it:
        # two legs can share a mode and a duration and still be different promises.
        "next_leg_id": next_leg.id if next_leg else None,
        # Fail-closed has to be observable, otherwise a consumer cannot tell
        # "nothing is due" from "the record contradicts itself".
        "cursor_status": (
            "STALE"
            if stale_cursor
            else "ABSENT_FALLBACK"
            if current_activity_id is None
            else "VALID"
        ),
        "persisted_cursor": current_activity_id,
    }


def _uuid(label: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"https://citynexus.local/v9/{label}"))


def _mobility_evidence_bundle(
    facts: list[MobilityFact],
    *,
    journey_id: str,
    operation_key: str,
    stay_offer: AccommodationOfferSnapshot,
) -> tuple[str, dict[str, Any]]:
    """Build one immutable evidence reference for an authoritative plan.

    The existing V9 fact rows are the normalized provider evidence. V12 adds a
    plan-level bundle so every consumer can point back to exactly the facts
    used for the materialized PlanVersion.
    """

    evidence_facts = [
        {
            "origin_key": item.from_key,
            "destination_key": item.to_key,
            "mode": item.mode,
            "minutes": item.duration_min,
            "distance_m": item.distance_m,
            "cost_amount_minor": item.cost_amount_minor,
            "currency": item.currency,
            "cost_status": item.cost_status,
            "walking_min": item.walking_min,
            "transfers": item.transfers,
            "reliability_score": item.reliability_score,
            "weather_exposure": item.weather_exposure,
            "provider_key": item.provider_key,
            "provenance": item.provenance,
            "geometry": _load(item.geometry_json, {}),
            "crs": item.crs,
            "captured_at": item.captured_at,
            "expires_at": item.expires_at,
            "fact_id": item.id,
        }
        for item in facts
    ]
    identity = _json(
        {
            "journey_id": journey_id,
            "operation_key": operation_key,
            "fact_ids": [item["fact_id"] for item in evidence_facts],
            "stay_offer_id": stay_offer.id,
        }
    )
    snapshot_id = "v12-stay-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]
    captured_values = [item.captured_at for item in facts if item.captured_at]
    expiry_values = [item.expires_at for item in facts if item.expires_at]
    evidence = {
        "snapshot_id": snapshot_id,
        "operation_key": operation_key,
        "captured_at": min(captured_values) if captured_values else _now(),
        "expires_at": min(expiry_values) if expiry_values else offer.expires_at,
        "source_provider": ";".join(sorted({item.provider_key for item in facts})),
        "aggregate_provenance": ";".join(sorted({item.provenance for item in facts})),
        "stay_offer_id": stay_offer.id,
        "stay_evidence": _load(stay_offer.evidence_json, {}),
        "facts": evidence_facts,
        "normalizer_version": "citynexus-v12-authoritative-evidence-1",
    }
    return snapshot_id, evidence


def retarget_fact(fact: MobilityFact, *, from_key: str, to_key: str) -> MobilityFact:
    """Return a copy of a mobility fact under canonical semantic identities."""

    return MobilityFact(
        id=fact.id,
        journey_id=fact.journey_id,
        option_id=fact.option_id,
        from_key=from_key,
        to_key=to_key,
        mode=fact.mode,
        duration_min=fact.duration_min,
        distance_m=fact.distance_m,
        cost_amount_minor=fact.cost_amount_minor,
        currency=fact.currency,
        cost_status=fact.cost_status,
        walking_min=fact.walking_min,
        transfers=fact.transfers,
        reliability_score=fact.reliability_score,
        weather_exposure=fact.weather_exposure,
        geometry_json=fact.geometry_json,
        crs=fact.crs,
        provider_key=fact.provider_key,
        provenance=fact.provenance,
        paid_mode=fact.paid_mode,
        evidence_json=fact.evidence_json,
        captured_at=fact.captured_at,
        expires_at=fact.expires_at,
    )


_STRUCTURAL_NODE_KEYS = {"arrival-anchor": "ARRIVAL", "departure-anchor": "DEPARTURE"}


def _canonical_node_semantic_key(journey: Journey, node: Any) -> str:
    """One stable semantic identity for a V9 structural node.

    ``arrival-anchor`` / ``departure-anchor`` are planner-local aliases; the
    canonical identity is the ``v12:{journey_id}:{key}`` Place id used by the
    authoritative planner, C4 commitments and the shared feasibility gate.
    """

    key = str(getattr(node, "key", "") or "")
    if key in _STRUCTURAL_NODE_KEYS:
        return f"v12:{journey.id}:{key}"
    return key


def _place_for_node(session: Session, journey: Journey, node: Any) -> Place:
    provider_key = "citynexus-v9-domain"
    external_id = f"{journey.city_code}:{node.key}"
    row = session.scalar(
        select(Place).where(
            Place.provider_key == provider_key,
            Place.external_id == external_id,
        )
    )
    if row is None:
        row = Place(
            id=_uuid(f"place:{external_id}"),
            provider_key=provider_key,
            external_id=external_id,
            city_code=journey.city_code,
            name=node.title,
            category=node.role.lower(),
            latitude=node.latitude,
            longitude=node.longitude,
            opening_hours_json=_json({"status": "NORMALIZED", "always_available": True}),
            tags_json=_json(["v9-authoritative", node.role.lower()]),
            metadata_json=_json(
                {
                    "available": True,
                    "semantic_key": node.key,
                    "coordinate_crs": "WGS84",
                    "cost_status": node.cost_status,
                    "cost_minor": node.estimated_cost_minor,
                }
            ),
            updated_at=_now(),
        )
        session.add(row)
        session.flush()
    return row


def _selected_facts(
    session: Session,
    journey: Journey,
    option: StayJourneyOption,
    *,
    force_mode: str | None,
    nodes: list[Any] | None = None,
) -> list[MobilityFact]:
    """The mobility facts for the legs the materialiser is about to write.

    When ``nodes`` is supplied the leg list is taken from the *actual* node
    sequence.  The stored ``leg_modes_json`` was computed before the calendar
    re-timed and re-ordered the carried stops, so using it blindly produced
    legs whose keys no longer matched the schedule and the gate reported
    ``INSUFFICIENT_TRAVEL_GAP`` on a plan that was in fact feasible.
    """

    if nodes is not None and len(nodes) >= 2:
        pairs = [
            (str(previous.key), str(current.key))
            for previous, current in zip(nodes, nodes[1:])
        ]
        stored = {
            f"{item.get('from_key')}->{item.get('to_key')}": str(item.get("mode"))
            for item in _load(option.leg_modes_json, [])
            if isinstance(item, dict)
        }
        rows: list[MobilityFact] = []
        for from_key, to_key in pairs:
            available = session.scalars(
                select(MobilityFact).where(
                    MobilityFact.journey_id == journey.id,
                    MobilityFact.from_key == from_key,
                    MobilityFact.to_key == to_key,
                )
            ).all()
            if not available:
                # The calendar re-ordered the stops, so this adjacency may not
                # have existed when the facts were first captured.  Generate it
                # now for every mode rather than failing the whole selection.
                from app.stay_mobility import _mode_fact as _mf

                node_by_key = {str(n.key): n for n in nodes}
                left = node_by_key.get(from_key)
                right = node_by_key.get(to_key)
                if left is None or right is None:
                    raise ValueError(
                        f"provider fact unavailable for {from_key}->{to_key}"
                    )
                for mode in ("WALK", "BICYCLE", "TRANSIT", "AUTO", "RIDE_HAIL"):
                    fact = _mf(
                        journey.id, None, left, right, mode, journey.budget_currency
                    )
                    session.add(fact)
                session.flush()
                available = session.scalars(
                    select(MobilityFact).where(
                        MobilityFact.journey_id == journey.id,
                        MobilityFact.from_key == from_key,
                        MobilityFact.to_key == to_key,
                    )
                ).all()
                if not available:
                    raise ValueError(
                        f"provider fact unavailable for {from_key}->{to_key}"
                    )
            # The calendar reserved time with ``_travel_minutes``, which is
            # derived from ``_mode_fact`` for the *current* node objects.  Any
            # MobilityFact row captured earlier can describe a different
            # adjacency (the calendar re-times and re-orders carried stops), so
            # trusting it produced legs the schedule could not satisfy and the
            # gate reported INSUFFICIENT_TRAVEL_GAP / LEG_ARRIVES_LATE on a
            # feasible plan.  Re-derive the leg from the same generator the
            # calendar used, then persist that as the frozen fact.
            from app.stay_mobility import _mode_fact as _mf
            from app.stay_mobility import _travel_minutes as _tm

            node_by_key = {str(n.key): n for n in nodes}
            left = node_by_key.get(from_key)
            right = node_by_key.get(to_key)
            if left is None or right is None:
                raise ValueError(f"provider fact unavailable for {from_key}->{to_key}")
            reserved = _tm(left, right, journey=journey)
            chosen = None
            for candidate in sorted(available, key=lambda f: (abs(f.duration_min - reserved), f.duration_min)):
                if int(candidate.duration_min) == int(reserved):
                    chosen = candidate
                    break
            if chosen is None:
                # No stored row matches the reserved time; materialise the
                # generator's own facts for this adjacency.  Only modes that do
                # not already exist are inserted, so the unique index holds.
                for mode in ("AUTO", "RIDE_HAIL", "TRANSIT", "BICYCLE", "WALK"):
                    exists = session.scalar(
                        select(MobilityFact.id).where(
                            MobilityFact.journey_id == journey.id,
                            MobilityFact.from_key == from_key,
                            MobilityFact.to_key == to_key,
                            MobilityFact.mode == mode,
                        )
                    )
                    if exists is None:
                        session.add(
                            _mf(journey.id, None, left, right, mode, journey.budget_currency)
                        )
                session.flush()
                rows_available = session.scalars(
                    select(MobilityFact).where(
                        MobilityFact.journey_id == journey.id,
                        MobilityFact.from_key == from_key,
                        MobilityFact.to_key == to_key,
                    )
                ).all()
                chosen = min(rows_available, key=lambda f: (abs(f.duration_min - reserved), f.duration_min))
            rows.append(chosen)
        return rows

    raw = _load(option.leg_modes_json, [])
    requested = {
        f"{item.get('from_key')}->{item.get('to_key')}": str(item.get("mode"))
        for item in raw
        if isinstance(item, dict)
    }
    rows: list[MobilityFact] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        from_key = str(item.get("from_key"))
        to_key = str(item.get("to_key"))
        mode = force_mode or requested.get(f"{from_key}->{to_key}")
        fact = session.scalar(
            select(MobilityFact).where(
                MobilityFact.journey_id == journey.id,
                MobilityFact.from_key == from_key,
                MobilityFact.to_key == to_key,
                MobilityFact.mode == mode,
            )
        )
        if fact is None:
            raise ValueError(f"provider fact unavailable for {from_key}->{to_key}:{mode}")
        rows.append(fact)
    return rows


def materialize_stay_option(
    session: Session,
    journey: Journey,
    option: StayJourneyOption,
    *,
    source: str,
    force_mode: str | None = None,
) -> MaterializationResult:
    offer = session.get(AccommodationOfferSnapshot, option.accommodation_offer_id)
    if offer is None:
        return MaterializationResult(None, "FAILED", ("STAY_EVIDENCE_MISSING",), False)
    offer_view = offer_payload(offer)
    if not offer_view["bookable"]:
        return MaterializationResult(None, "FAILED", ("STAY_QUOTE_EXPIRED",), False)
    nodes = build_candidate_nodes(session, journey, offer)
    if len(nodes) < 2:
        return MaterializationResult(None, "FAILED", ("GENERIC_GRAPH_INFEASIBLE",), False)
    try:
        facts = _selected_facts(
            session, journey, option, force_mode=force_mode, nodes=nodes
        )
    except ValueError as exc:
        return MaterializationResult(None, "FAILED", (str(exc),), False)
    if len(facts) != len(nodes) - 1:
        return MaterializationResult(None, "FAILED", ("INCOMPLETE_MOBILITY_GRAPH",), False)
    if any(item.paid_mode and item.cost_status == "UNKNOWN" for item in facts):
        return MaterializationResult(None, "REQUIRE_CONFIRMATION", ("UNKNOWN_PAID_COST",), True)

    # Freeze the mobility evidence under the *same* canonical semantic identity
    # the materialized Activities carry, so persistence, selection, booking,
    # start and replanning all reload one consistent graph.
    canonical_key_by_node_key = {
        str(node.key): _canonical_node_semantic_key(journey, node) for node in nodes
    }
    canonical_facts = [
        retarget_fact(
            fact,
            from_key=canonical_key_by_node_key.get(str(fact.from_key), fact.from_key),
            to_key=canonical_key_by_node_key.get(str(fact.to_key), fact.to_key),
        )
        for fact in facts
    ]
    mobility_snapshot_id, mobility_evidence = _mobility_evidence_bundle(
        canonical_facts,
        REDACTED_FOR_PUBLIC_RELEASE,
        operation_key=f"stay-option:{option.id}:{source}",
        stay_offer=offer,
    )

    version_no = (
        int(
            session.scalar(
                select(func.max(PlanVersion.version_no)).where(PlanVersion.journey_id == journey.id)
            )
            or 0
        )
        + 1
    )
    plan_id = str(uuid4())
    total_cost = (
        int(offer.total_amount_minor)
        + sum(int(node.estimated_cost_minor) for node in nodes)
        + sum(int(item.cost_amount_minor or 0) for item in facts)
    )
    plan = PlanVersion(
        id=plan_id,
        REDACTED_FOR_PUBLIC_RELEASE,
        version_no=version_no,
        planning_mode=journey.planning_mode,
        status="CANDIDATE",
        source="REPLAN" if source == "AUTOPILOT_RECOVERY" else "MANUAL",
        parent_plan_version_id=journey.active_plan_version_id,
        total_cost_minor=total_cost,
        currency=journey.budget_currency,
        estimated_duration_min=sum(item.duration_min for item in facts),
        utility_score=min(1.0, max(0.0, float(option.score))),
        score_breakdown_json=_json(
            {
                "commitment_preservation": 1.0,
                "stability": min(1.0, max(0.0, float(option.score))),
                "time_reliability": 0.9,
                "preference_fit": min(1.0, max(0.0, float(option.score))),
                "mobility_fit": 0.85,
                "budget_fit": min(
                    1.0,
                    max(
                        0.0,
                        1.0
                        - total_cost
                        / max(1, int(journey.budget_amount_minor)),
                    ),
                ),
                "robustness": 0.85,
                "opportunity_value": 0.8,
                "total": min(1.0, max(0.0, float(option.score))),
                "stay": offer.total_amount_minor,
                "mobility": sum(int(item.cost_amount_minor or 0) for item in facts),
                "walking_min": sum(item.walking_min for item in facts),
            }
        ),
        explanation_json=_json(
            {
                "source_option_id": option.id,
                "operation_source": source,
                "gate": "PENDING",
                "mobility_snapshot_id": mobility_snapshot_id,
            }
        ),
        mobility_snapshot_id=mobility_snapshot_id,
        mobility_evidence_json=_json(mobility_evidence),
        created_at=_now(),
    )
    session.add(plan)
    session.flush()
    activities: list[Activity] = []
    for index, node in enumerate(nodes, start=1):
        place = _place_for_node(session, journey, node)
        # Canonical structural identity, shared with the authoritative planner,
        # the C4 commitment policy and the frozen mobility evidence.
        semantic_identity = _canonical_node_semantic_key(journey, node)
        preceding = facts[index - 2] if index > 1 else None
        activity = Activity(
            id=str(uuid4()),
            plan_version_id=plan.id,
            sequence_no=index,
            activity_type=node.role.lower(),
            title=node.title,
            place_id=place.id,
            city_service_id=None,
            start_at=node.start_at,
            end_at=node.end_at,
            travel_from_prev_min=int(preceding.duration_min if preceding else 0),
            estimated_cost_minor=(
                offer.total_amount_minor if node.role == "CHECK_IN" else node.estimated_cost_minor
            ),
            currency=journey.budget_currency,
            status="PLANNED",
            locked=bool(node.locked),
            metadata_json=_json(
                {
                    "semantic_key": semantic_identity,
                    "role": node.role,
                    "latitude": node.latitude,
                    "longitude": node.longitude,
                    "crs": "WGS84",
                    "stay_offer_id": offer.id
                    if node.role in {"CHECK_IN", "CHECK_OUT", "LUGGAGE_DROP", "STAY"}
                    else None,
                }
            ),
            created_at=_now(),
            updated_at=_now(),
        )
        session.add(activity)
        activities.append(activity)
    session.flush()
    for index, fact in enumerate(facts, start=1):
        from_activity = activities[index - 1]
        to_activity = activities[index]
        session.add(
            JourneyLeg(
                id=str(uuid4()),
                REDACTED_FOR_PUBLIC_RELEASE,
                plan_version_id=plan.id,
                sequence_no=index,
                from_activity_id=from_activity.id,
                to_activity_id=to_activity.id,
                from_key=_canonical_node_semantic_key(journey, nodes[index - 1]),
                to_key=_canonical_node_semantic_key(journey, nodes[index]),
                mode=fact.mode,
                depart_at=from_activity.end_at,
                arrive_at=(
                    datetime.fromisoformat(from_activity.end_at)
                    + timedelta(minutes=fact.duration_min)
                ).isoformat(),
                duration_min=fact.duration_min,
                distance_m=fact.distance_m,
                cost_amount_minor=fact.cost_amount_minor,
                currency=fact.currency,
                cost_status=fact.cost_status,
                walking_min=fact.walking_min,
                transfers=fact.transfers,
                reliability_score=fact.reliability_score,
                weather_exposure=fact.weather_exposure,
                geometry_json=fact.geometry_json,
                crs=fact.crs,
                provider_key=fact.provider_key,
                provenance=fact.provenance,
                provider_snapshot_id=fact.id,
                captured_at=fact.captured_at,
                expires_at=fact.expires_at,
            )
        )
    session.flush()
    # Validate against the exact mobility evidence just materialized; a
    # downstream runtime recapture would create split-brain truth.
    #
    # One canonical identity is used end to end:
    #     Activity.metadata.semantic_key  -> routing / frozen-evidence identity
    #     Activity.place_id               -> database Place identity
    # The stored bundle is already keyed by those semantic node keys, so it is
    # consumed directly.  There is deliberately no identity rebinding here.
    gate_snapshot = MobilitySnapshot.from_evidence(mobility_evidence)
    gate = validate_plan_feasibility(
        session,
        journey,
        plan,
        include_active_c4=True,
        mobility_provider=gate_snapshot,
    )
    failures = list(gate.failures)
    for index, (fact, next_activity) in enumerate(zip(facts, activities[1:])):
        arrive_at = datetime.fromisoformat(activities[index].end_at) + timedelta(
            minutes=fact.duration_min
        )
        if arrive_at > datetime.fromisoformat(next_activity.start_at):
            failures.append(f"LEG_ARRIVES_LATE:{fact.from_key}->{fact.to_key}")
    if total_cost > journey.budget_amount_minor:
        failures.append("BUDGET_MAX_EXCEEDED")
    failures = sorted(set(failures))
    if failures:
        session.execute(delete(JourneyLeg).where(JourneyLeg.plan_version_id == plan.id))
        session.execute(delete(Activity).where(Activity.plan_version_id == plan.id))
        session.delete(plan)
        session.flush()
        return MaterializationResult(None, "FAILED", tuple(failures), False)

    previous_plans = session.scalars(
        select(PlanVersion).where(
            PlanVersion.journey_id == journey.id,
            PlanVersion.id != plan.id,
            PlanVersion.status.in_(("ACTIVE", "SELECTED")),
        )
    ).all()
    for previous in previous_plans:
        previous.status = "SUPERSEDED"
    other_options = session.scalars(
        select(StayJourneyOption).where(StayJourneyOption.journey_id == journey.id)
    ).all()
    for candidate in other_options:
        candidate.status = "SELECTED" if candidate.id == option.id else "CANDIDATE"
    plan.status = "ACTIVE"
    plan.explanation_json = _json(
        {
            "source_option_id": option.id,
            "operation_source": source,
            "deterministic_gate": {
                "status": "PASSED",
                "failures": [],
                "authority": "CityNexus validate_plan_feasibility + concrete-leg checks",
            },
            "mobility_snapshot_id": mobility_snapshot_id,
            "mobility_evidence": {
                "fact_count": len(facts),
                "aggregate_provenance": mobility_evidence["aggregate_provenance"],
                "expires_at": mobility_evidence["expires_at"],
            },
        }
    )
    journey.status = "ACTIVE"
    journey.active_plan_version_id = plan.id
    journey.current_activity_id = activities[0].id
    journey.updated_at = _now()
    activities[0].status = "ACTIVE"
    option.materialized_plan_version_id = plan.id

    active_commitments = session.scalars(
        select(Commitment).where(
            Commitment.journey_id == journey.id,
            Commitment.status == "ACTIVE",
        )
    ).all()
    for commitment in active_commitments:
        policy = _load(commitment.policy_json, {})
        role = str(policy.get("anchor_role") or "").upper()
        anchored = (
            session.get(Activity, commitment.activity_id) if commitment.activity_id else None
        )
        # Reservation anchors resolve to their canonical structural node; a
        # booking/order anchor keeps its own activity-anchored identity.
        if role in {"ARRIVAL", "DEPARTURE"}:
            wanted_key = f"v12:{journey.id}:{role.lower()}-anchor"
        else:
            wanted_key = str(policy.get("semantic_key") or policy.get("semantic_node_key") or "")
        if anchored is not None and anchored.plan_version_id == plan.id and wanted_key in {
            str(policy.get("semantic_key") or ""),
            str(policy.get("semantic_node_key") or ""),
        }:
            continue
        try:
            activity = resolve_commitment_target(
                policy={**policy, "semantic_node_key": wanted_key},
                anchored_activity=anchored,
                candidates=list(activities),
                subject="authoritative plan activation",
            )
        except CommitmentRemapConflict as exc:
            # Previously this branch just skipped the commitment, leaving a live
            # C4 anchored to an activity on the plan this one replaced.
            raise ValueError(f"cannot bind an active commitment: {exc}") from exc
        commitment.activity_id = activity.id
        if role in {"ARRIVAL", "DEPARTURE"}:
            policy["semantic_key"] = wanted_key
        commitment.policy_json = _json(policy)
        commitment.updated_at = _now()

    twin = session.get(JourneyTwin, journey.id)
    if twin is None:
        twin = JourneyTwin(
            REDACTED_FOR_PUBLIC_RELEASE,
            projection_status="READY",
            snapshot_at=_now(),
            context_json="{}",
            upcoming_commitments_json="[]",
            updated_at=_now(),
        )
        session.add(twin)
    twin.active_plan_version_id = plan.id
    twin.current_activity_id = journey.current_activity_id
    twin.projection_status = "READY"
    twin.snapshot_at = _now()
    twin.updated_at = _now()
    twin.context_json = _json(
        {
            "authoritative_plan": plan.id,
            "activity_ids": [item.id for item in activities],
            "leg_ids": [
                item.id
                for item in session.scalars(
                    select(JourneyLeg)
                    .where(JourneyLeg.plan_version_id == plan.id)
                    .order_by(JourneyLeg.sequence_no)
                ).all()
            ],
            "source_option_id": option.id,
            "deterministic_gate": "PASSED",
        }
    )
    twin.upcoming_commitments_json = _json(
        [
            {
                "id": item.id,
                "level": item.level,
                "external_ref": item.external_ref,
                "activity_id": item.activity_id,
            }
            for item in active_commitments
        ]
    )
    record_audit(
        session,
        action="v9.plan.materialized",
        entity_type="PlanVersion",
        entity_id=plan.id,
        REDACTED_FOR_PUBLIC_RELEASE,
        actor_type="AI_AGENT" if source == "AUTOPILOT_RECOVERY" else "USER",
        after={
            "option_id": option.id,
            "source": source,
            "deterministic_gate": "PASSED",
            "leg_count": len(facts),
            "locked_commitments_preserved": True,
        },
    )
    refresh_offline_snapshot(session, journey)
    return MaterializationResult(plan, "PASSED", (), True)


def set_luggage_context(
    session: Session,
    journey: Journey,
    *,
    baggage_present: bool,
    baggage_size_class: str,
    luggage_drop_required: bool,
) -> JourneyOperationsContext:
    row = session.get(JourneyOperationsContext, journey.id)
    now = _now()
    if row is None:
        row = JourneyOperationsContext(
            REDACTED_FOR_PUBLIC_RELEASE,
            baggage_present=baggage_present,
            baggage_size_class=baggage_size_class,
            luggage_drop_required=luggage_drop_required,
            constraints_json="{}",
            created_at=now,
            updated_at=now,
        )
        session.add(row)
    else:
        row.baggage_present = baggage_present
        row.baggage_size_class = baggage_size_class
        row.luggage_drop_required = luggage_drop_required
        row.updated_at = now
    session.flush()
    return row


def import_reservation_artifact(
    session: Session,
    journey: Journey,
    *,
    artifact_type: str,
    payload: dict[str, Any],
) -> ExternalReservationDraft:
    kind = artifact_type.upper()
    extracted: dict[str, Any]
    mode = "DETERMINISTIC"
    if kind == "STRUCTURED_JSON":
        extracted = dict(payload.get("reservation") or payload)
        # A structured payload is authored by the traveller (or by a test
        # fixture standing in for one).  Record that provenance so semantic
        # intake does not treat the booking as an unverified model guess and
        # block it behind an AI journey-match that will never arrive.
        extracted["artifact_source"] = "USER_ENTERED"
        # Normalise free-text endpoints into the canonical location shape.
        for field in ("origin", "destination"):
            value = extracted.get(field)
            if isinstance(value, str) and value.strip():
                extracted[field] = {"name": value.strip()}
    elif kind == "ICS":
        text = str(payload.get("text") or "")
        extracted = parse_reservation_calendar(
            text, default_type=str(payload.get("reservation_type") or "RAIL")
        )
        extracted["origin"] = payload.get("origin")
    else:
        text = str(payload.get("text") or "")
        ref = re.search(
            r"(?i)(?:confirmation|reference|booking)\s*(?:code|no\.?|#)?\s*[:：]?\s*([A-Z0-9-]{5,})",
            text,
        )
        iso_times = re.findall(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:\d{2})", text
        )
        extracted = {
            "reservation_type": next(
                (
                    item
                    for item in ("RAIL", "FLIGHT", "COACH", "FERRY")
                    if item.lower() in text.lower()
                ),
                None,
            ),
            "confirmation_ref": ref.group(1) if ref else None,
            "origin": payload.get("origin"),
            "destination": payload.get("destination"),
            "scheduled_departure_at": iso_times[0] if len(iso_times) > 0 else None,
            "scheduled_arrival_at": iso_times[1] if len(iso_times) > 1 else None,
            "provider_key": "pasted-confirmation",
        }
        mode = "DEEPSEEK_ELIGIBLE_AMBIGUOUS_TEXT"
    unresolved = [field for field in CRITICAL_RESERVATION_FIELDS if not extracted.get(field)]
    draft = ExternalReservationDraft(
        id=str(uuid4()),
        REDACTED_FOR_PUBLIC_RELEASE,
        artifact_type=kind,
        raw_payload_json=_json(payload),
        extracted_json=_json(extracted),
        unresolved_fields_json=_json(unresolved),
        extraction_mode=mode,
        status="READY_FOR_CONFIRMATION" if not unresolved else "NEEDS_REVIEW",
        created_at=_now(),
        confirmed_at=None,
    )
    session.add(draft)
    record_audit(
        session,
        action="journey_inbox.artifact_imported",
        entity_type="ExternalReservationDraft",
        entity_id=draft.id,
        REDACTED_FOR_PUBLIC_RELEASE,
        after={"status": draft.status, "unresolved_fields": unresolved, "extraction_mode": mode},
    )
    session.flush()
    return draft


def confirm_reservation_draft(
    session: Session, journey: Journey, draft: ExternalReservationDraft
) -> TransportReservation:
    if draft.journey_id != journey.id:
        raise ValueError("reservation draft does not belong to Journey")
    unresolved = _load(draft.unresolved_fields_json, [])
    if unresolved:
        missing = ", ".join(reservation_field_label(item) for item in unresolved)
        raise ValueError(
            "This booking still needs a few details before it can be saved: " + missing
        )
    if draft.status == "CONFIRMED":
        existing = session.scalar(
            select(TransportReservation).where(TransportReservation.draft_id == draft.id)
        )
        if existing is None:
            raise ValueError("confirmed reservation is inconsistent")
        return existing
    data = _load(draft.extracted_json, {})
    confirmation_ref = str(data.get("confirmation_ref") or "").strip()
    if not confirmation_ref:
        raise ValueError("confirmation_ref is required")
    duplicate = session.scalar(
        select(TransportReservation).where(
            TransportReservation.journey_id == journey.id,
            TransportReservation.confirmation_ref == confirmation_ref,
            TransportReservation.status == "CONFIRMED",
        )
    )
    if duplicate is not None:
        raise ValueError("duplicate confirmation_ref already confirmed")
    anchor_role = str(data.get("anchor_role") or "ARRIVAL").upper()
    if anchor_role not in {"ARRIVAL", "DEPARTURE", "MID_TRIP"}:
        raise ValueError(f"unsupported reservation anchor_role: {anchor_role}")
    # One stable semantic location identity for every consumer.  ARRIVAL and
    # DEPARTURE resolve to the planner's structural anchor key (a synthetic UUID
    # here would be unsatisfiable by every generator).  MID_TRIP keeps the real
    # user-selected place/service key, or reports the location as unresolved.
    intent_key = str(data.get("semantic_node_key") or data.get("semantic_key") or "").strip()
    semantic_key = resolve_anchor_key(
        role=anchor_role, intent_key=intent_key, REDACTED_FOR_PUBLIC_RELEASE
    )
    if not semantic_key:
        raise ValueError(
            "MID_TRIP reservation requires a non-structural semantic target"
        )
    semantic_node_key = STRUCTURAL_ANCHOR_KEYS.get(anchor_role, semantic_key)
    if anchor_role in {"ARRIVAL", "DEPARTURE"}:
        location_evidence = "STRUCTURAL_ANCHOR"
    else:
        location_evidence = LOCATION_RESOLVED
    # Quarantine any historical duplicate identity *before* relying on the
    # unique index, so the constraint can be enforced without collapsing two
    # genuinely different bookings.
    quarantine_duplicate_confirmation_identities(session, journey.id)
    session.flush()
    now = _now()
    # A node can be claimed by one ACTIVE anchor at a time.  The guard above refuses the same
    # provider reference twice; this one handles the *different* reference for the *same* node -
    # a re-issued ticket - which used to leave two ACTIVE rows and made the planner report the
    # traveller's own duplicate back as an unsatisfiable constraint.
    displaced = retire_superseded_active_anchor(
        session,
        journey,
        identity=semantic_key,
        replaced_by_external_ref=confirmation_ref,
        now=now,
    )
    for previous in displaced:
        record_audit(
            session,
            action="commitment.anchor_superseded",
            entity_type="Commitment",
            entity_id=previous.id,
            REDACTED_FOR_PUBLIC_RELEASE,
            before={"status": "ACTIVE", "external_ref": previous.external_ref},
            after={
                "status": "CANCELLED",
                "retire_reason": ANCHOR_RETIRE_MARKER,
                "superseded_by_confirmation_ref": confirmation_ref,
            },
        )
    # Accept both spellings.  The consumer manual-entry form posts
    # ``amount_minor`` while extracted artifacts carry ``price_amount_minor``;
    # reading only one silently dropped a price the traveller typed in.
    price = data.get("price_amount_minor")
    if price is None:
        price = data.get("amount_minor")
    price_status = str(
        data.get("price_status")
        or ("KNOWN" if price is not None else "UNKNOWN")
    )
    booking = Booking(
        id=str(uuid4()),
        REDACTED_FOR_PUBLIC_RELEASE,
        activity_id=None,
        commitment_id=None,
        provider_key=str(data.get("provider_key") or "external-import"),
        external_ref=confirmation_ref,
        status="CONFIRMED",
        amount_minor=int(price) if price is not None else 0,
        amount_status=price_status,
        currency=str(data.get("currency") or journey.budget_currency),
        cancellation_policy_json=_json(data.get("change_policy") or {}),
        created_at=now,
        updated_at=now,
    )
    session.add(booking)
    session.flush()
    # semantic_key was resolved above; never synthesise a fresh UUID here.
    commitment = Commitment(
        id=str(uuid4()),
        REDACTED_FOR_PUBLIC_RELEASE,
        activity_id=None,
        kind="BOOKING",
        level="C4_Anchored",
        status="ACTIVE",
        monetary_value_minor=int(price) if price is not None else 0,
        monetary_value_status=price_status,
        currency=str(data.get("currency") or journey.budget_currency),
        external_ref=confirmation_ref,
        policy_json=_json(
            {
                "semantic_node_key": semantic_node_key,
                "semantic_key": semantic_key,
                "location_evidence": location_evidence,
                "scheduled_start_at": str(
                    data["scheduled_departure_at"]
                    if anchor_role == "DEPARTURE"
                    else data["scheduled_arrival_at"]
                ),
                "anchor_role": anchor_role,
                "source": "USER_CONFIRMED_EXTERNAL_RESERVATION",
                "price_status": price_status,
            }
        ),
        created_at=now,
        updated_at=now,
    )
    session.add(commitment)
    session.flush()
    booking.commitment_id = commitment.id
    reservation = TransportReservation(
        id=str(uuid4()),
        REDACTED_FOR_PUBLIC_RELEASE,
        draft_id=draft.id,
        reservation_type=str(data["reservation_type"]).upper(),
        provider_key=str(data.get("provider_key") or "external-import"),
        confirmation_ref=confirmation_ref,
        origin_json=_json(data["origin"]),
        destination_json=_json(data["destination"]),
        scheduled_departure_at=str(data["scheduled_departure_at"]),
        scheduled_arrival_at=str(data["scheduled_arrival_at"]),
        estimated_arrival_at=None,
        actual_arrival_at=None,
        status="CONFIRMED",
        realtime_status="UNKNOWN_REALTIME",
        passenger_scope_json=_json(data.get("passengers") or {}),
        change_policy_json=_json(data.get("change_policy") or {}),
        price_amount_minor=int(price) if price is not None else None,
        currency=str(data.get("currency")) if data.get("currency") else None,
        price_status=price_status,
        source_provenance="USER_CONFIRMED_IMPORT",
        captured_at=now,
        booking_id=booking.id,
        commitment_id=commitment.id,
    )
    session.add(reservation)
    context = session.get(JourneyTripContext, journey.id)
    if context is None:
        context = JourneyTripContext(
            REDACTED_FOR_PUBLIC_RELEASE,
            arrival_anchor_json=_json({"at": journey.start_at, "location": "Arrival"}),
            departure_anchor_json=_json({"at": journey.end_at, "location": "Departure"}),
            party_size=1,
            occupancy_json=_json({"adults": 1}),
            stay_budget_amount_minor=0,
            stay_budget_currency=journey.budget_currency,
            # An imported reservation says nothing about a lodging budget, so this row records
            # "never asked" rather than inventing either an answer or a price.
            stay_budget_declared=None,
            created_at=now,
            updated_at=now,
        )
        session.add(context)
        session.flush()
    # Arrival imports update the Journey's arrival anchor as before.  A
    # confirmed mid-trip reservation is a first-class Commitment anchor, not
    # a replacement arrival fact; keeping the context anchor unchanged lets
    # the authoritative planner place flexible Activities around it.
    if context is not None and anchor_role == "ARRIVAL":
        destination = (
            data["destination"]
            if isinstance(data["destination"], dict)
            else {"name": str(data["destination"])}
        )
        context.arrival_anchor_json = _json(
            {
                "at": str(data["scheduled_arrival_at"]),
                "location": str(
                    destination.get("name") or destination.get("location") or "Arrival"
                ),
                "latitude": destination.get("latitude"),
                "longitude": destination.get("longitude"),
                "reservation_id": reservation.id,
                "commitment_id": commitment.id,
            }
        )
        context.updated_at = now
    elif context is not None and anchor_role == "DEPARTURE":
        origin = (
            data["origin"]
            if isinstance(data["origin"], dict)
            else {"name": str(data["origin"])}
        )
        context.departure_anchor_json = _json(
            {
                "at": str(data["scheduled_departure_at"]),
                "location": str(origin.get("name") or origin.get("location") or "Departure"),
                "latitude": origin.get("latitude"),
                "longitude": origin.get("longitude"),
                "reservation_id": reservation.id,
                "commitment_id": commitment.id,
            }
        )
        context.updated_at = now
    draft.status = "CONFIRMED"
    draft.confirmed_at = now
    notify(
        session,
        journey.id,
        dedupe_key=f"reservation-confirmed:{reservation.id}",
        kind="RESERVATION_CONFIRMED",
        priority="HIGH",
        payload={"reservation_id": reservation.id, "commitment_id": commitment.id},
    )
    record_audit(
        session,
        action="journey_inbox.reservation_confirmed",
        entity_type="TransportReservation",
        entity_id=reservation.id,
        REDACTED_FOR_PUBLIC_RELEASE,
        after={"commitment_id": commitment.id, "level": "C4_Anchored"},
    )
    session.flush()
    return reservation


def notify(
    session: Session,
    journey_id: str,
    *,
    dedupe_key: str,
    kind: str,
    priority: str,
    payload: dict[str, Any],
) -> NotificationEvent:
    row = session.scalar(
        select(NotificationEvent).where(NotificationEvent.dedupe_key == dedupe_key)
    )
    now = _now()
    if row is None:
        row = NotificationEvent(
            id=str(uuid4()),
            journey_id=journey_id,
            dedupe_key=dedupe_key,
            kind=kind,
            priority=priority,
            status="RECORDED",
            payload_json=_json(payload),
            delivery_channels_json=_json(["IN_APP", "SSE", "PUSH_RECORD"]),
            occurrence_count=1,
            created_at=now,
            updated_at=now,
        )
        session.add(row)
    else:
        row.occurrence_count += 1
        row.updated_at = now
    session.flush()
    journey = session.get(Journey, journey_id)
    delivered = 0
    attempted = 0
    if journey is not None:
        subscriptions = session.scalars(
            select(PushSubscription).where(
                PushSubscription.traveler_id == journey.traveler_id,
                PushSubscription.status == "ACTIVE",
            )
        ).all()
        for subscription in subscriptions:
            attempted += 1
            result = deliver_web_push(
                _load(subscription.subscription_json, {}),
                {"kind": kind, "priority": priority, "journey_id": journey_id, **payload},
            )
            if result.status == "DELIVERED":
                delivered += 1
    if delivered:
        row.status = "DELIVERED"
        row.delivery_channels_json = _json(["IN_APP", "SSE", "WEB_PUSH"])
    elif attempted:
        row.status = "RECORDED"
        row.delivery_channels_json = _json(["IN_APP", "SSE", "PUSH_RECORD", "WEB_PUSH_UNAVAILABLE"])
    return row


def refresh_offline_snapshot(session: Session, journey: Journey) -> OfflineJourneySnapshot:
    reference = execution_clock.now(journey.timezone)
    window_end = reference + timedelta(hours=24)
    activities = []
    legs = []
    if journey.active_plan_version_id:
        activities = session.scalars(
            select(Activity)
            .where(Activity.plan_version_id == journey.active_plan_version_id)
            .order_by(Activity.sequence_no)
        ).all()
        legs = session.scalars(
            select(JourneyLeg)
            .where(JourneyLeg.plan_version_id == journey.active_plan_version_id)
            .order_by(JourneyLeg.sequence_no)
        ).all()
    next_day = [
        {
            "id": item.id,
            "title": item.title,
            "type": item.activity_type,
            "start_at": item.start_at,
            "end_at": item.end_at,
            "state": item.status,
            "locked": bool(item.locked),
            "metadata": _load(item.metadata_json, {}),
        }
        for item in activities
        if (
            reference <= datetime.fromisoformat(item.start_at) <= window_end
            or (
                item.id == journey.current_activity_id
                and item.status in {"ACTIVE", "EN_ROUTE", "IN_PROGRESS"}
                and datetime.fromisoformat(item.end_at) >= reference
            )
        )
    ]
    reservations = session.scalars(
        select(TransportReservation).where(TransportReservation.journey_id == journey.id)
    ).all()
    selected = session.scalar(
        select(StayJourneyOption).where(
            StayJourneyOption.journey_id == journey.id,
            StayJourneyOption.status == "SELECTED",
        )
    )
    offer = (
        session.get(AccommodationOfferSnapshot, selected.accommodation_offer_id)
        if selected
        else None
    )
    if offer is None and journey.active_plan_version_id:
        active_plan = session.get(PlanVersion, journey.active_plan_version_id)
        plan_evidence = _load(active_plan.mobility_evidence_json, {}) if active_plan else {}
        stay_offer_id = plan_evidence.get("stay_offer_id")
        if stay_offer_id:
            offer = session.get(AccommodationOfferSnapshot, str(stay_offer_id))
    execution = _execution_truth(activities, legs, journey.current_activity_id)
    provider_times = [item.captured_at for item in legs if item.captured_at]
    now = execution_clock.now().isoformat()
    existing = session.get(OfflineJourneySnapshot, journey.id)
    sequence = int(existing.sequence if existing else 0) + 1
    payload = {
        "journey_id": journey.id,
        "active_plan_version_id": journey.active_plan_version_id,
        "current_activity_id": journey.current_activity_id,
        "reference_at": reference.isoformat(),
        "next_24h": next_day,
        "execution": execution,
        "now": execution["now"],
        "next": execution["next"],
        "legs": [journey_leg_payload(item) for item in legs],
        "hotel": {
            "name": offer.property_name,
            "latitude": offer.latitude,
            "longitude": offer.longitude,
            "crs": offer.coordinate_crs,
        }
        if offer
        else None,
        "reservations": [
            {
                "id": item.id,
                "type": item.reservation_type,
                "confirmation_ref": item.confirmation_ref,
                "scheduled_arrival_at": item.scheduled_arrival_at,
                "realtime_status": item.realtime_status,
            }
            for item in reservations
        ],
        "read_only": True,
        "financial_actions_allowed": False,
    }
    freshness = {
        "latest_provider_capture_at": max(provider_times) if provider_times else None,
        "transit_realtime": "UNKNOWN_REALTIME",
    }
    if existing is None:
        existing = OfflineJourneySnapshot(
            REDACTED_FOR_PUBLIC_RELEASE,
            sequence=sequence,
            valid_until=(reference + timedelta(hours=24)).isoformat(),
            payload_json=_json(payload),
            provider_freshness_json=_json(freshness),
            updated_at=now,
        )
        session.add(existing)
    else:
        existing.sequence = sequence
        existing.valid_until = (reference + timedelta(hours=24)).isoformat()
        existing.payload_json = _json(payload)
        existing.provider_freshness_json = _json(freshness)
        existing.updated_at = now
    session.flush()
    return existing


def record_location(
    session: Session,
    journey: Journey,
    *,
    latitude: float,
    longitude: float,
    accuracy_m: float,
    captured_at: str,
) -> LocationObservation:
    captured = datetime.fromisoformat(captured_at)
    if captured.tzinfo is None:
        raise ValueError("captured_at must include timezone")
    row = LocationObservation(
        id=str(uuid4()),
        REDACTED_FOR_PUBLIC_RELEASE,
        latitude=latitude,
        longitude=longitude,
        accuracy_m=accuracy_m,
        captured_at=captured.isoformat(),
        source="FOREGROUND_WEB",
    )
    session.add(row)
    session.flush()
    return row


def register_push_subscription(
    session: Session, traveler_id: str, subscription: dict[str, Any]
) -> PushSubscription:
    endpoint = str(subscription.get("endpoint") or "")
    if not endpoint.startswith("https://"):
        raise ValueError("push endpoint must use HTTPS")
    endpoint_hash = hashlib.sha256(endpoint.encode("utf-8")).hexdigest()
    now = _now()
    row = session.scalar(
        select(PushSubscription).where(PushSubscription.endpoint_hash == endpoint_hash)
    )
    if row is None:
        row = PushSubscription(
            id=str(uuid4()),
            traveler_id=traveler_id,
            endpoint_hash=endpoint_hash,
            subscription_json=_json(subscription),
            status="ACTIVE",
            created_at=now,
            updated_at=now,
        )
        session.add(row)
    else:
        row.status = "ACTIVE"
        row.updated_at = now
    session.flush()
    return row


def register_edge_device(
    session: Session, journey: Journey, *, display_name: str
) -> tuple[EdgeDevice, str]:
    token = f"cnx_{uuid4().hex}{uuid4().hex}"
    now = _now()
    row = EdgeDevice(
        id=str(uuid4()),
        traveler_id=journey.traveler_id,
        REDACTED_FOR_PUBLIC_RELEASE,
        display_name=display_name,
        token_hash=hashlib.sha256(token.encode("utf-8")).hexdigest(),
        scopes_json=_json(sorted(EDGE_ALLOWED_ACTIONS)),
        status="ACTIVE",
        last_sequence=0,
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    session.flush()
    return row, token


def authenticate_edge(device: EdgeDevice, token: str) -> bool:
    return (
        device.status == "ACTIVE"
        and hashlib.sha256(token.encode("utf-8")).hexdigest() == device.token_hash
    )


def edge_snapshot(session: Session, device: EdgeDevice) -> dict[str, Any]:
    if not device.journey_id:
        raise ValueError("edge device is not assigned to a Journey")
    journey = session.get(Journey, device.journey_id)
    if journey is None:
        raise ValueError("assigned Journey was not found")
    activities = (
        session.scalars(
            select(Activity)
            .where(Activity.plan_version_id == journey.active_plan_version_id)
            .order_by(Activity.sequence_no)
        ).all()
        if journey.active_plan_version_id
        else []
    )
    legs = (
        session.scalars(
            select(JourneyLeg)
            .where(JourneyLeg.plan_version_id == journey.active_plan_version_id)
            .order_by(JourneyLeg.sequence_no)
        ).all()
        if journey.active_plan_version_id
        else []
    )
    # One source, one answer.  This used to call the shared projection with an empty
    # leg list and then recompute current_index / Now / Next / leg by hand from its
    # result, so a single Edge response could carry two different stories: the
    # projection naming a next activity while the device-facing fields said there was
    # none, and (before the leg was bound to a trustworthy Now) the other way round.
    # A device cannot be told "is there a next step?" twice with two answers.
    execution = _execution_truth(activities, legs, journey.current_activity_id)
    by_id = {item.id: item for item in activities}
    now_item = (
        by_id.get(str(execution["now_activity_id"])) if execution["now_activity_id"] else None
    )
    next_item = (
        by_id.get(str(execution["next_activity_id"])) if execution["next_activity_id"] else None
    )
    c4 = session.scalar(
        select(Commitment)
        .where(
            Commitment.journey_id == journey.id,
            Commitment.status == "ACTIVE",
            Commitment.level == "C4_Anchored",
        )
        .order_by(Commitment.created_at)
    )
    execution_now = execution_clock.now(journey.timezone)
    c4_countdown = None
    if c4:
        scheduled = _load(c4.policy_json, {}).get("scheduled_start_at")
        if scheduled:
            c4_countdown = int(
                (datetime.fromisoformat(str(scheduled)) - execution_now).total_seconds() // 60
            )
    active_event = session.scalar(
        select(Event)
        .where(Event.journey_id == journey.id, Event.status.in_(("DETECTED", "OPEN", "ACTIVE")))
        .order_by(Event.detected_at.desc())
    )
    latest_guardian = session.scalar(
        select(GuardianRun)
        .where(GuardianRun.journey_id == journey.id)
        .order_by(GuardianRun.created_at.desc())
    )
    enrollment = session.get(AutopilotEnrollment, journey.id)
    if active_event is not None and active_event.severity == "CRITICAL":
        risk = "CRITICAL"
    elif active_event is not None or c4_countdown is not None and c4_countdown < 0:
        risk = "AT_RISK"
    else:
        risk = "SAFE"
    if enrollment and enrollment.enabled:
        autopilot_state = "RECOVERING" if latest_guardian and latest_guardian.action_status == "EXECUTED" else "MONITORING"
    else:
        autopilot_state = "DISABLED"
    device.last_sequence += 1
    device.updated_at = _now()
    stale_projection = execution["cursor_status"] == "STALE"
    return {
        "sequence": device.last_sequence,
        "time": execution_now.isoformat(),
        "journey_state": journey.status,
        # "Nothing is due" and "the record contradicts itself" are different
        # facts; the device must not be told the journey is complete because the
        # cursor points at an activity this plan does not contain.
        "now_title": (
            "Execution position unresolved"
            if stale_projection
            else now_item.title if now_item else "No active step"
        ),
        "now_state": now_item.status if now_item else ("STALE_CURSOR" if stale_projection else "COMPLETE"),
        "next_title": (
            "Next step unavailable"
            if stale_projection
            else next_item.title if next_item else "Journey complete"
        ),
        "next_state": next_item.status if next_item else None,
        "next_at": next_item.start_at if next_item else None,
        "mode": execution["next_mode"],
        "eta_min": execution["next_eta_min"],
        "next_leg_id": execution["next_leg_id"],
        "c4_countdown_min": c4_countdown,
        "risk": risk,
        "autopilot_state": autopilot_state,
        "active_plan_version_id": journey.active_plan_version_id,
        "cursor_status": execution["cursor_status"],
        "execution": execution,
        "execution_truth_id": f"edge:{journey.id}:{journey.active_plan_version_id}:{journey.current_activity_id}",
    }


def record_edge_action(
    session: Session, device: EdgeDevice, *, action: str, payload: dict[str, Any]
) -> EdgeAction:
    normalized = action.upper()
    if normalized not in EDGE_ALLOWED_ACTIONS:
        raise ValueError("edge action exceeds device authority")
    if not device.journey_id:
        raise ValueError("edge device is not assigned to a Journey")
    row = EdgeAction(
        id=str(uuid4()),
        device_id=device.id,
        journey_id=device.journey_id,
        action=normalized,
        status="RECORDED",
        payload_json=_json(payload),
        created_at=_now(),
    )
    session.add(row)
    session.flush()
    return row


def record_edge_observation(
    session: Session, device: EdgeDevice, *, observation_type: str, payload: dict[str, Any]
) -> EdgeObservation:
    if not device.journey_id:
        raise ValueError("edge device is not assigned to a Journey")
    normalized = observation_type.upper()
    if normalized not in {"RAIN", "SOS", "TEMP", "ACK"}:
        raise ValueError("unsupported edge observation")
    row = EdgeObservation(
        id=str(uuid4()),
        device_id=device.id,
        journey_id=device.journey_id,
        observation_type=normalized,
        payload_json=_json(payload),
        created_at=_now(),
    )
    session.add(row)
    if normalized in {"RAIN", "SOS"}:
        notify(
            session,
            device.journey_id,
            dedupe_key=f"edge:{device.id}:{normalized}:{payload.get('incident_id', 'current')}",
            kind=f"EDGE_{normalized}",
            priority="CRITICAL" if normalized == "SOS" else "HIGH",
            payload={"device_id": device.id, **payload},
        )
    session.flush()
    return row
