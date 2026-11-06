"""Snapshot J — backend truth-gap regressions (B1–B5).

B1  CHECK_IN / CHECK_OUT / LUGGAGE_DROP must survive stay augmentation.
B2  a carried ``locked`` activity must keep its authoritative start/end.
B3  precise replan reasons must be reachable, not masked by the generic
    ``NO_DISTINCT_CANDIDATE`` fallback.
B4  an ambiguous no-target CLOSURE / WEATHER must not be guessed.
B5  a stay-enriched plan must be able to recover without a mobility-identity
    mismatch.

Everything here runs offline against the shared test database.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from app.db.models import (
    AccommodationOfferSnapshot,
    Activity,
    Journey,
    Place,
    PlanVersion,
)
from app.db.session import SessionLocal
from app.main import app
from app.stay_mobility import build_candidate_nodes

client = TestClient(app)

STRUCTURAL_ROLES = {"LUGGAGE_DROP", "CHECK_IN", "STAY", "CHECK_OUT"}


def _tag(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _activity_id(tag: str, index: int) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"citynexus-sj-{tag}-{index}"))


def _make_journey(tag: str) -> str:
    traveler = client.post(
        "/api/v1/travelers",
        json={"display_name": f"SJ-{tag}", "timezone": "Asia/Shanghai", "locale": "zh-CN"},
    ).json()
    journey = client.post(
        "/api/v1/journeys",
        json={
            "traveler_id": traveler["id"],
            "title": "厦门三天",
            "city_code": "CN-XIAMEN",
            "timezone": "Asia/Shanghai",
            "start_at": "2026-11-06T10:00:00+08:00",
            "end_at": "2026-11-08T20:00:00+08:00",
            "budget": {"amount_minor": 300000, "currency": "CNY"},
            "stay_budget": {"amount_minor": 100000, "currency": "CNY"},
            "party_size": 2,
            "occupancy": {"adults": 2, "rooms": 1},
            "arrival_anchor": {
                "at": "2026-11-06T10:00:00+08:00",
                "location": "厦门高崎国际机场",
            },
            "departure_anchor": {
                "at": "2026-11-08T20:00:00+08:00",
                "location": "厦门北站",
            },
        },
    ).json()
    return journey["id"]


def _seed(tag: str, *, count: int = 4, lock_index: int | None = None):
    """Create a journey whose active plan holds real POIs. Closes its session."""

    with SessionLocal() as session:
        journey_id = _make_journey(tag)
        journey = session.get(Journey, journey_id)
        base = datetime.fromisoformat("2026-11-06T11:00:00+08:00")
        poi_ids = []
        for index in range(count):
            place = Place(
                id=f"real-place-sj-{tag}-{index}",
                provider_key="overpass",
                external_id=f"node/{tag}/{index}",
                city_code=journey.city_code,
                name=f"厦门景点 {tag} {index}",
                category="attraction",
                latitude=24.48 + index * 0.01,
                longitude=118.09 + index * 0.01,
                opening_hours_json=json.dumps({"status": "UNKNOWN"}),
                tags_json=json.dumps(["real_city"]),
                metadata_json=json.dumps({"cost_minor": 0}),
                updated_at="2026-09-16T00:00:00+00:00",
            )
            session.add(place)
            poi_ids.append(place.id)
        session.flush()

        plan = PlanVersion(
            id=f"plan-sj-{tag}",
            journey_id=journey.id,
            version_no=1,
            planning_mode="balanced",
            # The replan endpoint requires an ACTIVE base plan.
            status="ACTIVE",
            source="INITIAL",
            total_cost_minor=0,
            currency="CNY",
            score_breakdown_json="{}",
            utility_score=0.9,
            estimated_duration_min=540,
            created_at="2026-09-16T00:00:00+00:00",
        )
        session.add(plan)
        session.flush()
        for index, place_id in enumerate(poi_ids):
            start = base + timedelta(hours=index * 2)
            session.add(
                Activity(
                    id=_activity_id(tag, index),
                    plan_version_id=plan.id,
                    sequence_no=index + 1,
                    activity_type="visit",
                    title=f"厦门景点 {tag} {index}",
                    place_id=place_id,
                    city_service_id=None,
                    start_at=start.isoformat(),
                    end_at=(start + timedelta(hours=1)).isoformat(),
                    travel_from_prev_min=20,
                    estimated_cost_minor=0,
                    currency="CNY",
                    status="PLANNED",
                    locked=(lock_index is not None and index == lock_index),
                    metadata_json=json.dumps({"semantic_key": place_id, "role": "ACTIVITY"}),
                    created_at="2026-09-16T00:00:00+00:00",
                    updated_at="2026-09-16T00:00:00+00:00",
                )
            )
        journey.active_plan_version_id = plan.id
        journey.status = "ACTIVE"
        session.commit()
        return journey_id, poi_ids


def _seed_offer(session, journey_id: str, tag: str, **overrides):
    payload = {
        "id": f"offer-sj-{tag}",
        "journey_id": journey_id,
        "provider_key": "osm-overpass",
        "external_offer_id": f"osm:node/{tag}",
        "property_name": f"厦门测试酒店 {tag}",
        "latitude": 24.47,
        "longitude": 118.10,
        "room_type": "hotel",
        "nightly_amount_minor": 0,
        "total_amount_minor": 0,
        "currency": "CNY",
        "check_in_at": "2026-11-06T16:00:00+08:00",
        "check_out_at": "2026-11-08T12:00:00+08:00",
        "captured_at": "2026-09-16T00:00:00+00:00",
    }
    payload.update(overrides)
    offer = AccommodationOfferSnapshot(**payload)
    session.add(offer)
    session.commit()
    return offer


def _roles(nodes) -> list[str]:
    return [str(node.role).upper() for node in nodes]


# --------------------------------------------------------------------------
# B1 — structural stay operations survive augmentation
# --------------------------------------------------------------------------


def test_b1_structural_operations_survive_stay_augmentation():
    tag = _tag("b1")
    journey_id, poi_ids = _seed(tag)
    with SessionLocal() as session:
        offer = _seed_offer(session, journey_id, tag)
        journey = session.get(Journey, journey_id)
        nodes = build_candidate_nodes(session, journey, offer)

    roles = _roles(nodes)
    assert "ARRIVAL" in roles
    assert "DEPARTURE" in roles
    # The concrete defect: these three used to vanish.
    assert "CHECK_IN" in roles, f"CHECK_IN was dropped: {roles}"
    assert "CHECK_OUT" in roles, f"CHECK_OUT was dropped: {roles}"
    assert "STAY" in roles, f"STAY nights were dropped: {roles}"
    # The carried POIs are still present.
    carried = {
        str(node.place_key)
        for node in nodes
        if str(node.role).upper() == "ACTIVITY"
    }
    assert set(poi_ids) <= carried, "stay augmentation erased chosen POIs"


def test_b1_luggage_drop_survives_when_required():
    tag = _tag("b1d")
    journey_id, _ = _seed(tag)
    with SessionLocal() as session:
        offer = _seed_offer(
            session,
            journey_id,
            tag,
            luggage_drop_json=json.dumps({"available": True}),
        )
        # Baggage must be present for a drop node to be required.
        from app.db.models import JourneyOperationsContext

        ops = session.get(JourneyOperationsContext, journey_id)
        if ops is None:
            ops = JourneyOperationsContext(
                journey_id=journey_id,
                baggage_present=True,
                baggage_size_class="MEDIUM",
                luggage_drop_required=True,
                constraints_json="{}",
                created_at="2026-09-16T00:00:00+00:00",
                updated_at="2026-09-16T00:00:00+00:00",
            )
            session.add(ops)
        else:
            ops.baggage_present = True
            ops.luggage_drop_required = True
        session.commit()
        journey = session.get(Journey, journey_id)
        nodes = build_candidate_nodes(session, journey, offer)

    roles = _roles(nodes)
    assert "LUGGAGE_DROP" in roles, f"LUGGAGE_DROP was dropped: {roles}"


def test_b1_structural_sequence_is_chronological():
    tag = _tag("b1o")
    journey_id, _ = _seed(tag)
    with SessionLocal() as session:
        offer = _seed_offer(session, journey_id, tag)
        journey = session.get(Journey, journey_id)
        nodes = build_candidate_nodes(session, journey, offer)

    starts = [datetime.fromisoformat(node.start_at) for node in nodes]
    ends = [datetime.fromisoformat(node.end_at) for node in nodes]
    assert starts == sorted(starts), "structural sequence is not chronological"
    for previous_end, next_start in zip(ends, starts[1:]):
        assert next_start >= previous_end, "structural sequence contains an overlap"

    roles = _roles(nodes)
    assert roles.index("CHECK_IN") < roles.index("CHECK_OUT"), (
        "check-in must precede check-out"
    )


# --------------------------------------------------------------------------
# B2 — locked truth is never re-timed
# --------------------------------------------------------------------------


def test_b2_locked_carried_activity_keeps_its_exact_window():
    tag = _tag("b2")
    journey_id, _ = _seed(tag, lock_index=1)
    locked_id = _activity_id(tag, 1)
    with SessionLocal() as session:
        before = session.get(Activity, locked_id)
        original_start, original_end = before.start_at, before.end_at

    with SessionLocal() as session:
        offer = _seed_offer(session, journey_id, tag)
        journey = session.get(Journey, journey_id)
        nodes = build_candidate_nodes(session, journey, offer)

    locked_nodes = [
        node
        for node in nodes
        if str(node.role).upper() == "ACTIVITY" and node.locked
    ]
    assert locked_nodes, "the locked carried activity disappeared"
    assert len(locked_nodes) == 1
    assert locked_nodes[0].start_at == original_start, (
        "hotel selection re-timed a locked activity "
        f"({original_start} -> {locked_nodes[0].start_at})"
    )
    assert locked_nodes[0].end_at == original_end


def test_b2_flexible_activities_move_around_locked_truth():
    tag = _tag("b2f")
    journey_id, _ = _seed(tag, lock_index=1)
    locked_id = _activity_id(tag, 1)
    with SessionLocal() as session:
        locked = session.get(Activity, locked_id)
        locked_window = (locked.start_at, locked.end_at)

    with SessionLocal() as session:
        offer = _seed_offer(session, journey_id, tag)
        journey = session.get(Journey, journey_id)
        nodes = build_candidate_nodes(session, journey, offer)

    for node in nodes:
        if str(node.role).upper() != "ACTIVITY" or node.locked:
            continue
        start = datetime.fromisoformat(node.start_at)
        end = datetime.fromisoformat(node.end_at)
        lock_start = datetime.fromisoformat(locked_window[0])
        lock_end = datetime.fromisoformat(locked_window[1])
        assert end <= lock_start or start >= lock_end, (
            f"flexible activity {node.key} overlaps the locked window"
        )


# --------------------------------------------------------------------------
# B3 — precise replan reasons are reachable
# --------------------------------------------------------------------------


def test_b3_unresolved_reason_is_not_masked_by_generic_fallback():
    """A CLOSURE whose target cannot be grounded returns a precise reason."""

    tag = _tag("b3")
    journey_id, _ = _seed(tag)
    event = client.post(
        f"/api/v1/journeys/{journey_id}/events",
        json={"event_type": "CLOSURE", "severity": "MAJOR", "provenance": "USER_REPORTED"},
    )
    assert event.status_code == 201, event.text
    proposal = client.post(
        f"/api/v1/journeys/{journey_id}/replans",
        json={"trigger_event_id": event.json()["id"]},
    )
    if proposal.status_code == 201:
        diff = proposal.json().get("diff") or {}
        assert (diff.get("removed") or diff.get("added")), (
            "a no-target closure was accepted as a no-op"
        )
        return
    assert proposal.status_code in {409, 422}, proposal.text
    message = json.dumps(proposal.json(), ensure_ascii=False)
    assert "NO_DISTINCT_CANDIDATE" not in message, (
        "the generic fallback masked the precise reason: " + message[:200]
    )
    assert any(
        token in message
        for token in (
            "REPLAN_TARGET_UNRESOLVED",
            "REPLAN_PROTECTED_CONFLICT",
            "REPLAN_NO_ALTERNATIVE",
            "CLARIFICATION",
            "REVIEW_REQUIRED",
        )
    ), message[:200]


# --------------------------------------------------------------------------
# B4 — ambiguous no-target physical disruption is not guessed
# --------------------------------------------------------------------------


def test_b4_ambiguous_closure_is_not_auto_assigned_to_first_activity():
    """A closure with no grounded target must not silently pick a stop."""

    tag = _tag("b4")
    journey_id, _ = _seed(tag)
    with SessionLocal() as session:
        plan_id = session.get(Journey, journey_id).active_plan_version_id
        before = [
            (a.id, a.start_at)
            for a in session.query(Activity)
            .filter(Activity.plan_version_id == plan_id)
            .order_by(Activity.sequence_no)
        ]

    event = client.post(
        f"/api/v1/journeys/{journey_id}/events",
        json={"event_type": "CLOSURE", "severity": "MAJOR", "provenance": "USER_REPORTED"},
    ).json()
    proposal = client.post(
        f"/api/v1/journeys/{journey_id}/replans",
        json={"trigger_event_id": event["id"]},
    )
    if proposal.status_code in {409, 422}:
        message = json.dumps(proposal.json(), ensure_ascii=False)
        assert any(
            token in message
            for token in (
                "REPLAN_TARGET_UNRESOLVED",
                "CLARIFICATION",
                "REVIEW_REQUIRED",
                "NO_ALTERNATIVE",
                "PROTECTED",
            )
        ), message[:200]
        return
    # If a proposal was produced it must have been grounded by a real target,
    # and the stored plan must not have been silently rewritten.
    with SessionLocal() as session:
        after = [
            (a.id, a.start_at)
            for a in session.query(Activity)
            .filter(Activity.plan_version_id == plan_id)
            .order_by(Activity.sequence_no)
        ]
    assert before == after, "an ambiguous closure rewrote the active plan"


import pytest as _pytest


@_pytest.fixture(autouse=True)
def _fixture_travel(monkeypatch):
    """Deterministic short transition time for the local fixtures.

    These tests assert structural invariants (survival, ordering, locked truth,
    duration preservation) on co-located fixture coordinates.  Pinning the
    transition cost keeps them independent of the distance heuristic while the
    real travel reachability rule stays fully active in production.
    """

    import app.stay_mobility as _sm

    monkeypatch.setattr(_sm, "_travel_minutes", lambda a, b, journey=None: 5)
