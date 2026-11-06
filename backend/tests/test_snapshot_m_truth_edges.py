"""Snapshot M — permanent regressions for the truth-edge findings.

Scheduler Truth V3:
  * a traveller-selected activity is never silently shortened;
  * pinned truth must be *reachable*, not merely non-overlapping.

Activity Detail truth:
  * real Place coordinates survive into the plan payload;
  * known-free is distinct from unknown price.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta

import pytest
from zoneinfo import ZoneInfo

import app.stay_mobility as sm
from app.db.models import Activity, Journey, Place, PlanVersion
from app.db.session import SessionLocal
from app.stay_mobility import GraphNode, StayItineraryConflict, _reschedule_with_carried

TZ = "+08:00"


def dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def node(key, role, start, end, *, locked=False, lat=24.50, lon=118.10) -> GraphNode:
    return GraphNode(
        key=key,
        title=key,
        role=role,
        start_at=start,
        end_at=end,
        latitude=lat,
        longitude=lon,
        place_key=key,
        locked=locked,
    )


@pytest.fixture()
def fixed_travel(monkeypatch):
    """Deterministic transition cost so the assertions are about the contract."""

    monkeypatch.setattr(sm, "_travel_minutes", lambda a, b, journey=None: 30)
    return 30


# --------------------------------------------------------------------------
# Scheduler Truth V3 — rule 1
# --------------------------------------------------------------------------


def test_original_duration_is_preserved_when_it_fits(fixed_travel):
    arrival = dt(f"2026-11-06T10:00:00{TZ}")
    departure = dt(f"2026-11-06T18:00:00{TZ}")
    anchors = [
        node("arrival", "ARRIVAL", arrival.isoformat(), "2026-11-06T10:20:00+08:00", locked=True),
        node("departure", "DEPARTURE", "2026-11-06T17:50:00+08:00", departure.isoformat(), locked=True),
    ]
    carried = [
        node("museum", "ACTIVITY", "2026-11-06T11:00:00+08:00", "2026-11-06T13:00:00+08:00"),
    ]
    out = _reschedule_with_carried(anchors, carried, arrival, departure, journey=None)
    museum = next(n for n in out if n.key == "museum")
    assert dt(museum.end_at) - dt(museum.start_at) == timedelta(hours=2)


def test_120_minute_activity_with_only_20_minute_window_is_a_conflict(fixed_travel):
    """The packaged probe case: conflict, never silent truncation.

    The visit must stay on its original local date, so the only candidate
    windows are the gaps between the pinned nodes on 2026-11-06.
    """

    arrival = dt(f"2026-11-06T10:00:00{TZ}")
    departure = dt(f"2026-11-06T12:30:00{TZ}")
    anchors = [
        node("arrival", "ARRIVAL", "2026-11-06T10:00:00+08:00", "2026-11-06T10:20:00+08:00", locked=True),
        node("checkin", "CHECK_IN", "2026-11-06T11:30:00+08:00", "2026-11-06T11:50:00+08:00", locked=True),
        node("departure", "DEPARTURE", "2026-11-06T12:20:00+08:00", departure.isoformat(), locked=True),
    ]
    carried = [
        node("museum", "ACTIVITY", "2026-11-06T10:30:00+08:00", "2026-11-06T12:30:00+08:00"),
    ]
    with pytest.raises(StayItineraryConflict) as excinfo:
        _reschedule_with_carried(anchors, carried, arrival, departure, journey=None)
    assert "museum" in str(excinfo.value)


# --------------------------------------------------------------------------
# Scheduler Truth V3 — rule 2
# --------------------------------------------------------------------------


def test_pinned_nodes_ten_minutes_apart_with_thirty_minutes_travel_conflict(fixed_travel):
    """The packaged probe case: pinned truth must be reachable."""

    arrival = dt(f"2026-11-06T10:00:00{TZ}")
    departure = dt(f"2026-11-06T16:00:00{TZ}")
    anchors = [
        node("arrival", "ARRIVAL", "2026-11-06T10:00:00+08:00", "2026-11-06T10:20:00+08:00", locked=True),
        node("checkin", "CHECK_IN", "2026-11-06T12:00:00+08:00", "2026-11-06T12:20:00+08:00", locked=True),
        node("departure", "DEPARTURE", "2026-11-06T15:50:00+08:00", departure.isoformat(), locked=True),
    ]
    carried = [
        node(
            "confirmed",
            "ACTIVITY",
            "2026-11-06T12:30:00+08:00",
            "2026-11-06T13:30:00+08:00",
            locked=True,
            lat=24.70,
            lon=118.30,
        )
    ]
    with pytest.raises(StayItineraryConflict):
        _reschedule_with_carried(anchors, carried, arrival, departure, journey=None)


def test_reachable_pinned_nodes_are_accepted(fixed_travel):
    arrival = dt(f"2026-11-06T10:00:00{TZ}")
    departure = dt(f"2026-11-06T16:00:00{TZ}")
    anchors = [
        node("arrival", "ARRIVAL", "2026-11-06T10:00:00+08:00", "2026-11-06T10:20:00+08:00", locked=True),
        node("checkin", "CHECK_IN", "2026-11-06T12:00:00+08:00", "2026-11-06T12:20:00+08:00", locked=True),
        node("departure", "DEPARTURE", "2026-11-06T15:50:00+08:00", departure.isoformat(), locked=True),
    ]
    out = _reschedule_with_carried(anchors, [], arrival, departure, journey=None)
    assert any(str(n.role).upper() == "CHECK_IN" for n in out)


# --------------------------------------------------------------------------
# Activity Detail truth — coordinates and cost
# --------------------------------------------------------------------------


def _make_journey_with_place(metadata: dict) -> tuple[str, str, str]:
    tag = uuid.uuid4().hex[:8]
    with SessionLocal() as session:
        traveler_id = str(uuid.uuid4())
        from app.db.models import Traveler

        session.add(
            Traveler(
                id=traveler_id,
                display_name=f"M-{tag}",
                email=None,
                locale="zh-CN",
                timezone="Asia/Shanghai",
                created_at="2026-09-17T00:00:00+00:00",
                updated_at="2026-09-17T00:00:00+00:00",
            )
        )
        session.flush()
        journey = Journey(
            id=str(uuid.uuid4()),
            traveler_id=traveler_id,
            title="厦门三天",
            city_code="CN-XIAMEN",
            timezone="Asia/Shanghai",
            start_at="2026-11-06T10:00:00+08:00",
            end_at="2026-11-08T20:00:00+08:00",
            budget_currency="CNY",
            budget_amount_minor=300000,
            planning_mode="balanced",
            status="ACTIVE",
            active_plan_version_id=None,
            current_activity_id=None,
            created_at="2026-09-17T00:00:00+00:00",
            updated_at="2026-09-17T00:00:00+00:00",
        )
        session.add(journey)
        session.flush()

        place = Place(
            id=f"real-place-m-{tag}",
            provider_key="overpass",
            external_id=f"node/{tag}",
            city_code="CN-XIAMEN",
            name="厦门景点",
            category="attraction",
            latitude=24.4798,
            longitude=118.0894,
            opening_hours_json=json.dumps({"status": "UNKNOWN"}),
            tags_json=json.dumps(["real_city"]),
            metadata_json=json.dumps({"cost_minor": 0, "price_known": False}),
            updated_at="2026-09-17T00:00:00+00:00",
        )
        session.add(place)
        session.flush()

        plan = PlanVersion(
            id=str(uuid.uuid4()),
            journey_id=journey.id,
            version_no=1,
            planning_mode="balanced",
            status="ACTIVE",
            source="INITIAL",
            total_cost_minor=0,
            currency="CNY",
            score_breakdown_json="{}",
            utility_score=0.9,
            estimated_duration_min=120,
            created_at="2026-09-17T00:00:00+00:00",
        )
        session.add(plan)
        session.flush()

        activity = Activity(
            id=str(uuid.uuid4()),
            plan_version_id=plan.id,
            sequence_no=1,
            activity_type="visit",
            title="厦门景点",
            place_id=place.id,
            city_service_id=None,
            start_at="2026-11-06T11:00:00+08:00",
            end_at="2026-11-06T13:00:00+08:00",
            travel_from_prev_min=20,
            estimated_cost_minor=0,
            currency="CNY",
            status="PLANNED",
            locked=False,
            metadata_json=json.dumps(metadata),
            created_at="2026-09-17T00:00:00+00:00",
            updated_at="2026-09-17T00:00:00+00:00",
        )
        session.add(activity)
        journey.active_plan_version_id = plan.id
        session.commit()
        return journey.id, plan.id, activity.id


def test_real_place_coordinates_survive_into_the_plan_payload():
    """Even when the activity metadata has no coordinates, the Place row wins."""

    from app.db.models import PlanVersion as PV
    from app.services import plan_schema

    journey_id, plan_id, _activity_id = _make_journey_with_place({"semantic_key": "x"})
    with SessionLocal() as session:
        payload = plan_schema(session, session.get(PV, plan_id))
    first = payload.activities[0]
    assert first.metadata.get("latitude") == pytest.approx(24.4798)
    assert first.metadata.get("longitude") == pytest.approx(118.0894)


def test_known_free_is_not_reported_as_unknown_price():
    """`cost_minor = 0` with KNOWN evidence stays known-free, not unknown.

    This asserts the discovery metadata contract directly: an OSM attraction
    with no fee is genuinely free, and the UI must be able to say 免费 rather
    than 暂无价格.
    """

    from app.integrations.discovery.real_city import _place_metadata

    class _Candidate:
        metadata = {"osm_id": "node/1", "tags": {"tourism": "attraction"}}

    known_free = _place_metadata(
        _Candidate(),
        "live",
        {"status": "KNOWN", "amount_minor": 0, "currency": "CNY", "source": "overpass"},
        {},
    )
    assert known_free["cost_minor"] == 0
    assert known_free["price_known"] is True
    assert known_free["cost_evidence"]["status"] == "KNOWN"

    unknown = _place_metadata(
        _Candidate(),
        "live",
        {"status": "UNKNOWN", "amount_minor": None, "currency": "CNY", "source": "overpass"},
        {},
    )
    assert unknown["cost_minor"] == 0
    assert unknown["price_known"] is False
    assert unknown["cost_evidence"]["status"] == "UNKNOWN"


def test_known_paid_amount_is_preserved():
    from app.integrations.discovery.real_city import _place_metadata

    class _Candidate:
        metadata = {"osm_id": "node/2"}

    paid = _place_metadata(
        _Candidate(),
        "live",
        {"status": "KNOWN", "amount_minor": 5000, "currency": "CNY", "source": "overpass"},
        {},
    )
    assert paid["cost_minor"] == 5000
    assert paid["price_known"] is True


# --------------------------------------------------------------------------
# Hotel local wall-clock truth (found during the continuation run)
# --------------------------------------------------------------------------


def test_hotel_times_use_the_journey_timezone_not_the_anchor_offset():
    """A UTC-stored arrival must not shift local check-in / check-out.

    The continuation run found every Xiamen hotel reporting
    ``STAY_PINNED_CONFLICT:PINNED_AFTER_DEPARTURE`` because a 12:00 local
    check-out was built with the anchor's UTC offset and landed exactly on the
    departure anchor.
    """

    from app.stay_mobility import _at_on_day

    arrival = datetime.fromisoformat("2026-11-06T02:00:00+00:00")  # 10:00 +08:00
    departure = datetime.fromisoformat("2026-11-08T12:00:00+00:00")  # 20:00 +08:00

    check_in = _at_on_day(arrival, 0, "14:00", "Asia/Shanghai")
    check_out = _at_on_day(arrival, 2, "12:00", "Asia/Shanghai")

    # The contract is the local wall-clock *instant*, not the offset the value
    # happens to be expressed in.  Structural times are normalised to the
    # journey's anchor offset so one plan never mixes offsets, so assert the
    # local reading after converting back into the journey timezone.
    zone = ZoneInfo("Asia/Shanghai")
    assert check_in.astimezone(zone).hour == 14, check_in
    assert check_in.astimezone(zone).minute == 0, check_in
    assert check_out.astimezone(zone).hour == 12, check_out
    assert check_out.astimezone(zone).minute == 0, check_out
    assert check_out <= departure, "local check-out must precede the departure anchor"
    # One plan must never mix offsets; every structural time shares the anchor's.
    assert check_in.utcoffset() == arrival.utcoffset(), check_in
    assert check_out.utcoffset() == arrival.utcoffset(), check_out


def test_hotel_times_without_a_timezone_keep_previous_behaviour():
    """No journey timezone supplied -> fall back to the anchor's own offset."""

    from app.stay_mobility import _at_on_day

    arrival = datetime.fromisoformat("2026-11-06T02:00:00+00:00")
    check_in = _at_on_day(arrival, 0, "14:00")
    assert check_in.hour == 14
    assert check_in.utcoffset() == timedelta(0)
