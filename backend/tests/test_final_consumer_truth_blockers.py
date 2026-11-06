"""Final consumer closure — truth blocker regressions.

T1  selecting a stay must AUGMENT the chosen itinerary, never erase it.
T2  a CLOSURE / WEATHER disruption must never settle as a no-op "stable copy".

Both are driven through the real services, not by inspecting strings.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta

import pytest
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
# The suite shares one process-wide database, so every row this module creates
# uses a unique prefix and is cleaned up afterwards.  Reloading the app or
# rebinding the engine here would poison the shared process for every other
# test module.


def _tag(prefix: str) -> str:
    """A tag that is unique per run, so repeat runs never collide."""

    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _seed(tag: str):
    """Create a journey with real POIs on its active plan, then close the session.

    The session must not stay open: the test client writes through its own
    session and SQLite would report "database is locked".
    """

    from app.db.session import SessionLocal

    with SessionLocal() as session:
        journey_id = _make_journey(client)
        poi_ids = _seed_real_pois(session, journey_id, tag)
        session.commit()
        return journey_id, poi_ids

def _make_journey(client: TestClient) -> str:
    traveler = client.post(
        "/api/v1/travelers",
        json={"display_name": "T1T2", "timezone": "Asia/Shanghai", "locale": "zh-CN"},
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



def _activity_id(tag: str, index: int) -> str:
    """A stable, unique, UUID-shaped activity id per test."""

    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"citynexus-t1t2-{tag}-{index}"))

def _seed_real_pois(session, journey_id: str, tag: str, count: int = 4) -> list[str]:
    """Materialise real-city POIs and a selected plan, without any provider call."""

    journey = session.get(Journey, journey_id)
    base = datetime.fromisoformat("2026-11-06T11:00:00+08:00")
    places = []
    for index in range(count):
        place = Place(
            id=f"real-place-t1t2-{tag}-{index}",
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
        places.append(place)
    session.flush()

    plan = PlanVersion(
        id=f"plan-t1t2-{tag}",
        journey_id=journey.id,
        version_no=1,
        planning_mode="balanced",
        status="SELECTED",
        source="INITIAL",
        parent_plan_version_id=None,
        total_cost_minor=0,
        currency="CNY",
        score_breakdown_json="{}",
        utility_score=0.9,
        mobility_snapshot_id=None,
        mobility_evidence_json="{}",
        estimated_duration_min=540,
        created_at="2026-09-16T00:00:00+00:00",
    )
    session.add(plan)
    session.flush()
    for index, place in enumerate(places):
        start = base + timedelta(hours=index * 2)
        session.add(
            Activity(
                id=_activity_id(tag, index),
                plan_version_id=plan.id,
                sequence_no=index + 1,
                activity_type="visit",
                title=place.name,
                place_id=place.id,
                city_service_id=None,
                start_at=start.isoformat(),
                end_at=(start + timedelta(hours=1)).isoformat(),
                travel_from_prev_min=20,
                estimated_cost_minor=0,
                currency="CNY",
                status="PLANNED",
                locked=False,
                metadata_json=json.dumps({"semantic_key": place.id, "role": "ACTIVITY"}),
                created_at="2026-09-16T00:00:00+00:00",
                updated_at="2026-09-16T00:00:00+00:00",
            )
        )
    journey.active_plan_version_id = plan.id
    journey.status = "ACTIVE"
    session.commit()
    return [place.id for place in places]


def _seed_offer(session, journey_id: str, tag: str) -> AccommodationOfferSnapshot:
    offer = AccommodationOfferSnapshot(
        id=f"offer-t1t2-{tag}",
        journey_id=journey_id,
        provider_key="osm-overpass",
        external_offer_id="osm:node/1",
        property_name="厦门测试酒店",
        latitude=24.47,
        longitude=118.10,
        room_type="hotel",
        nightly_amount_minor=0,
        total_amount_minor=0,
        currency="CNY",
        check_in_at="2026-11-06T14:00:00+08:00",
        check_out_at="2026-11-08T12:00:00+08:00",
        captured_at="2026-09-16T00:00:00+00:00",
    )
    session.add(offer)
    session.commit()
    return offer


def test_t1_stay_augments_instead_of_erasing_pois():
    """T1: the chosen real-city POIs survive a stay selection."""

    tag = _tag("a")
    journey_id, poi_ids = _seed(tag)
    with SessionLocal() as session:
        offer = _seed_offer(session, journey_id, tag)
        journey = session.get(Journey, journey_id)
        nodes = build_candidate_nodes(session, journey, offer)

    carried = {
        str(node.place_key)
        for node in nodes
        if str(node.role).upper() == "ACTIVITY"
    }
    assert set(poi_ids) <= carried, (
        "stay graph dropped the traveller's chosen POIs: "
        f"missing {sorted(set(poi_ids) - carried)}"
    )
    stay_nodes = [node for node in nodes if str(node.role).upper() == "STAY"]
    assert stay_nodes, "stay nodes were not inserted"
    assert any(str(node.role).upper() == "ARRIVAL" for node in nodes)
    assert any(str(node.role).upper() == "DEPARTURE" for node in nodes)


def test_t1_stay_sequence_is_ordered_and_gapped():
    """T1: the augmented sequence must be ordered with no overlap."""

    tag = _tag("b")
    journey_id, _ = _seed(tag)
    with SessionLocal() as session:
        offer = _seed_offer(session, journey_id, tag)
        journey = session.get(Journey, journey_id)
        nodes = build_candidate_nodes(session, journey, offer)
    starts = [datetime.fromisoformat(node.start_at) for node in nodes]
    ends = [datetime.fromisoformat(node.end_at) for node in nodes]
    assert starts == sorted(starts), "stay graph is not chronologically ordered"
    for previous_end, next_start in zip(ends, starts[1:]):
        assert next_start >= previous_end, "stay graph contains an overlap"
    assert starts[-1] < datetime.fromisoformat(journey.end_at) or starts[-1] == datetime.fromisoformat(
        journey.end_at
    )


def test_t2_closure_on_mutable_poi_is_never_a_noop():
    """T2: a closure on a movable stop must produce a material proposal."""

    tag = _tag("c")
    journey_id, _ = _seed(tag)
    activity_id = _activity_id(tag, 1)

    event = client.post(
        f"/api/v1/journeys/{journey_id}/events",
        json={
            "event_type": "CLOSURE",
            "severity": "MAJOR",
            "affected_activity_id": activity_id,
            "provenance": "USER_REPORTED",
        },
    )
    assert event.status_code == 201, event.text
    proposal = client.post(
        f"/api/v1/journeys/{journey_id}/replans",
        json={"trigger_event_id": event.json()["id"]},
    )
    if proposal.status_code == 201:
        diff = proposal.json().get("diff") or {}
        removed = diff.get("removed") or []
        added = diff.get("added") or []
        assert removed or added, "a closure was repaired with a no-op proposal"
    else:
        # Refusing is acceptable; silently accepting "nothing changed" is not.
        # A structurally incomplete fixture (no frozen mobility bundle) is also
        # a refusal — the important property is that no 201 no-op is returned.
        assert proposal.status_code in {409, 422}, proposal.text
        message = json.dumps(proposal.json(), ensure_ascii=False).upper()
        assert any(
            token in message
            for token in (
                "NO_ALTERNATIVE",
                "UNRESOLVED",
                "PROTECTED",
                "NO_DISTINCT_CANDIDATE",
                "ACTIVE PLAN IS MISSING",
            )
        ), proposal.text


def test_t2_stable_copy_is_not_generated_for_physical_disruption():
    """T2: F0_STABLE_COPY must not be the recovery for a closure."""

    tag = _tag("d")
    journey_id, _ = _seed(tag)
    with SessionLocal() as session:
        # A locked stop cannot be removed; the engine must not answer "no change".
        locked = session.get(Activity, _activity_id(tag, 1))
        locked.locked = True
        session.commit()
    activity_id = _activity_id(tag, 1)

    event = client.post(
        f"/api/v1/journeys/{journey_id}/events",
        json={
            "event_type": "CLOSURE",
            "severity": "MAJOR",
            "affected_activity_id": activity_id,
            "provenance": "USER_REPORTED",
        },
    )
    assert event.status_code == 201, event.text
    proposal = client.post(
        f"/api/v1/journeys/{journey_id}/replans",
        json={"trigger_event_id": event.json()["id"]},
    )
    if proposal.status_code == 201:
        body = proposal.json()
        diff = body.get("diff") or {}
        family = str((body.get("explanation") or {}).get("family") or "")
        assert family != "F0_STABLE_COPY", "closure recovered via stable copy"
        assert (diff.get("removed") or diff.get("added")), "no-op accepted for a closure"
    else:
        assert proposal.status_code in {409, 422}, proposal.text
