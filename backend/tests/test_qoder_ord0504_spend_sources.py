"""ORD-0504 -- the spend face reads two doors and nothing else, so those two answers are pinned here.

The face's arithmetic lives in `frontend/src/features/trip/spendFacts.ts` and the order asks for
numbers that come from the server. That combination has a specific failure mode this file exists to
catch: a screen that keeps rendering while the field it was reading stops arriving. The total would
not go blank -- it would go quietly smaller, which is worse, because a smaller sum looks like a
cheaper trip rather than like a broken read.

What is pinned, and why each one is the thing that could silently break:

  * `active_plan.legs[]` carries `cost_amount_minor`, `cost_status`, `currency` and `depart_at`. The
    server has been answering these for as long as the leg table existed; the client type did not name
    them until this order (`frontend/src/app/v8Types.ts`), which is how a page titled "这趟旅行花多
    少" came to say nothing about transport cost.
  * a leg the server cannot price comes back as `cost_amount_minor: null` with `cost_status: "UNKNOWN"`
    -- not as `0`. The difference between "free" and "nobody has costed this" is the whole content of
    the order's no-invented-numbers clause, and it is decided here, on the server side.
  * `/wallet` carries a reservation's `price` triple and a commitment's `monetary_value` plus its
    status word, and a reservation names the commitment it became (`commitment_id`) so the same promise
    can be recognised twice and counted once.
  * the five spend classes the store holds and NO journey-scoped read answers. Their status codes are
    pinned so that the day somebody opens one of those doors, this file says which named gap on the
    screen has to be revisited -- rather than the screen going on listing a class it could now show.
  * and one negative on purpose: nothing in either answer carries a converted amount. There is no
    exchange rate on this server, so a screen cannot be reading one.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.models import (
    Activity, Commitment, Journey, JourneyLeg, Place, PlanVersion, Traveler, TransportReservation,
)
from app.db.session import SessionLocal
from app.main import app

client = TestClient(app, raise_server_exceptions=False)

CONVERTED_WORDS = ("usd", "eur", "converted", "exchange", "rate", "normalised", "in_cny")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _trip_with_a_priced_leg_and_a_priced_reservation() -> str:
    """One trip holding every class the face reads: a priced leg, an unpriced leg, a reservation, a commitment."""
    moment = _now()
    batch = str(uuid4())
    start = datetime.now(timezone.utc) + timedelta(days=20)
    with SessionLocal() as session:
        traveler = Traveler(
            id=str(uuid4()), display_name="ORD-0504 probe", email=None, locale="zh-CN",
            timezone="Asia/Shanghai", created_at=moment, updated_at=moment,
        )
        session.add(traveler)
        session.flush()
        journey = Journey(
            id=str(uuid4()), traveler_id=traveler.id, title="花费这一屏的探针趟",
            city_code="CN-XIAMEN", timezone="Asia/Shanghai",
            start_at=start.isoformat(), end_at=(start + timedelta(days=2)).isoformat(),
            budget_amount_minor=300000, budget_currency="CNY", status="PLANNED",
            created_at=moment, updated_at=moment,
        )
        session.add(journey)
        plan = PlanVersion(
            id=str(uuid4()), journey_id=journey.id, version_no=1, planning_mode="balanced",
            status="ACTIVE", source="INITIAL", total_cost_minor=20000, currency="CNY",
            estimated_duration_min=600, created_at=moment,
        )
        session.add(plan)
        activity_ids = []
        for index in (1, 2):
            place = Place(
                id=str(uuid4()), provider_key="ord0504-probe",
                external_id=f"ord0504-{batch}-{index}", city_code="CN-XIAMEN",
                name=f"花费探针地点 {index}", category="attraction",
                latitude=24.44 + index / 100, longitude=118.06 + index / 100,
                opening_hours_json="{}", tags_json="[]", metadata_json="{}", updated_at=moment,
            )
            session.add(place)
            session.flush()
            activity = Activity(
                id=str(uuid4()), plan_version_id=plan.id, sequence_no=index, activity_type="visit",
                title=f"stop-{index}", place_id=place.id, city_service_id=None,
                start_at=(start + timedelta(hours=9 + index)).isoformat(),
                end_at=(start + timedelta(hours=10 + index)).isoformat(),
                travel_from_prev_min=15, estimated_cost_minor=12000 * index, currency="CNY",
                status="PLANNED", locked=False, metadata_json="{}",
                created_at=moment, updated_at=moment,
            )
            session.add(activity)
            activity_ids.append(activity.id)
        for index, (cost, currency, status_word) in enumerate(
            [(4500, "CNY", "KNOWN"), (None, None, "UNKNOWN")], start=1
        ):
            session.add(JourneyLeg(
                id=str(uuid4()), journey_id=journey.id, plan_version_id=plan.id, sequence_no=index,
                from_activity_id=activity_ids[0], to_activity_id=activity_ids[1],
                from_key=f"stop-{index}", to_key="next", mode="TRANSIT",
                depart_at=(start + timedelta(hours=11 + index)).isoformat(),
                arrive_at=(start + timedelta(hours=12 + index)).isoformat(),
                duration_min=30, distance_m=5000, cost_amount_minor=cost, currency=currency,
                cost_status=status_word, walking_min=8, transfers=0, reliability_score=0.8,
                weather_exposure="LOW", geometry_json="{}", crs="WGS84",
                provider_key="ord0504-probe", provenance="TEST_SEED", captured_at=moment,
            ))
        reservation = TransportReservation(
            id=str(uuid4()), journey_id=journey.id, draft_id=None, reservation_type="TRAIN",
            provider_key="ord0504-probe", confirmation_ref=f"ORD0504-{batch[:8]}",
            origin_json="{}", destination_json="{}",
            scheduled_departure_at=(start + timedelta(hours=8)).isoformat(),
            scheduled_arrival_at=(start + timedelta(hours=10)).isoformat(),
            status="CONFIRMED", realtime_status="SCHEDULED", passenger_scope_json="{}",
            change_policy_json="{}", price_amount_minor=23000, currency="CNY", price_status="KNOWN",
            source_provenance="TEST_SEED", captured_at=moment,
        )
        session.add(reservation)
        session.add(Commitment(
            id=str(uuid4()), journey_id=journey.id, activity_id=activity_ids[0], kind="BOOKING",
            level="C3_Guaranteed", status="ACTIVE", monetary_value_minor=9900,
            monetary_value_status="KNOWN", currency="CNY", external_ref=f"ORD0504-{batch[:8]}",
            policy_json="{}", created_at=moment, updated_at=moment,
        ))
        journey.active_plan_version_id = plan.id
        session.commit()
        return journey.id


def _experience(journey_id: str) -> dict:
    answer = client.get(f"/api/v1/journeys/{journey_id}/experience")
    assert answer.status_code == 200, answer.text[:400]
    return answer.json()


def _wallet(journey_id: str) -> dict:
    answer = client.get(f"/api/v1/journeys/{journey_id}/wallet")
    assert answer.status_code == 200, answer.text[:400]
    return answer.json()


def test_ord0504_the_experience_answer_carries_a_legs_price_and_the_word_that_says_how_well_known_it_is():
    """The four fields the spend face reads off a leg, named and present.

    This is the pair to `frontend/src/app/v8Types.ts`: the server answered them all along and the
    client declared none of them, so no screen could ask. If one of these ever stops arriving, the
    transport total on that page gets quietly smaller instead of visibly wrong -- which is why it is
    pinned on the door rather than in the component.
    """
    journey_id = _trip_with_a_priced_leg_and_a_priced_reservation()
    legs = _experience(journey_id)["active_plan"]["legs"]
    assert len(legs) == 2, [leg.get("cost_status") for leg in legs]
    priced = legs[0]
    for field in ("cost_amount_minor", "cost_status", "currency", "depart_at"):
        assert field in priced, (sorted(priced), field)
    assert priced["cost_amount_minor"] == 4500
    assert priced["cost_status"] == "KNOWN"
    assert priced["currency"] == "CNY"


def test_ord0504_an_unpriced_leg_stays_absent_rather_than_becoming_zero():
    """`null` plus `UNKNOWN`, on the wire. The screen's rule that an absent price is not a zero can
    only hold if the server does not turn it into one first."""
    journey_id = _trip_with_a_priced_leg_and_a_priced_reservation()
    legs = _experience(journey_id)["active_plan"]["legs"]
    unpriced = legs[1]
    assert unpriced["cost_amount_minor"] is None, unpriced
    assert unpriced["cost_status"] == "UNKNOWN"


def test_ord0504_the_wallet_answer_carries_the_two_confirmed_money_classes():
    """A reservation's price triple, a commitment's value with its status word, and the link between
    them -- the last one is what lets a screen recognise the same promise twice and count it once."""
    journey_id = _trip_with_a_priced_leg_and_a_priced_reservation()
    wallet = _wallet(journey_id)
    reservation = wallet["reservations"][0]
    assert reservation["price"]["amount_minor"] == 23000
    assert reservation["price"]["currency"] == "CNY"
    assert reservation["price"]["status"] == "KNOWN"
    assert reservation["status"] == "CONFIRMED"
    commitment = wallet["commitments"][0]
    assert commitment["monetary_value"]["amount_minor"] == 9900
    assert commitment["monetary_value_status"] == "KNOWN"
    assert commitment["level"] == "C3_Guaranteed"
    assert "commitment_id" in reservation, sorted(reservation)


def test_ord0504_five_spend_classes_have_no_journey_scoped_read_and_that_is_said_on_screen():
    """The named gap, pinned by status code.

    These are the classes the store holds (`backend/app/db/models.py`: `activities.estimated_cost_minor`,
    `plan_versions.total_cost_minor`, `bookings.amount_minor`, `orders.amount_minor`,
    `transactions.amount_minor`) and no door answers for a trip. The face lists them as missing rather
    than estimating them; when somebody opens one of these doors this test goes red on purpose, so the
    missing list has to be re-read rather than left standing.
    """
    journey_id = _trip_with_a_priced_leg_and_a_priced_reservation()
    for suffix, expected in (
        ("spend", 404), ("costs", 404), ("expenses", 404), ("legs", 404),
        ("reservations", 404), ("commitments", 404),
    ):
        answer = client.get(f"/api/v1/journeys/{journey_id}/{suffix}")
        assert answer.status_code == expected, (suffix, answer.status_code, answer.text[:160])
    # Bookings and transactions answer a POST or nothing: the path exists, a journey-scoped GET does not.
    for suffix in ("bookings", "transactions"):
        answer = client.get(f"/api/v1/journeys/{journey_id}/{suffix}")
        assert answer.status_code == 405, (suffix, answer.status_code)


def test_ord0504_the_stop_estimate_and_the_plan_total_are_stored_but_not_answered():
    """The two absences that matter most on a per-day face, checked against the same row.

    The store has both numbers for this very trip -- a plan total and two stop estimates -- and the
    answer carries neither. That is the difference between "this trip has no per-stop prices" and
    "this screen was never given them", and the face says the second one.
    """
    journey_id = _trip_with_a_priced_leg_and_a_priced_reservation()
    with SessionLocal() as session:
        journey = session.get(Journey, journey_id)
        plan = session.get(PlanVersion, journey.active_plan_version_id)
        assert plan.total_cost_minor == 20000
        estimates = [
            activity.estimated_cost_minor
            for activity in session.scalars(
                select(Activity).where(Activity.plan_version_id == plan.id).order_by(Activity.sequence_no)
            ).all()
        ]
        assert estimates == [12000, 24000], estimates

    body = json.dumps(_experience(journey_id))
    assert "20000" not in body, "the plan's own total must not leak onto this answer unannounced"
    assert "estimated_cost" not in body


def test_ord0504_nothing_on_either_answer_is_a_converted_amount():
    """There is no exchange rate on this server, so a screen cannot be reading one.

    Checked as an absence over both answers' key names rather than as a value comparison, because the
    defect this guards against is the quiet one: somebody adds a `*_usd` or `converted_*` field to make
    a total look tidier, and every currency on the page stops meaning anything.
    """
    journey_id = _trip_with_a_priced_leg_and_a_priced_reservation()
    keys = []

    def walk(node, prefix="$"):
        if isinstance(node, dict):
            for key, value in node.items():
                keys.append(f"{prefix}.{key}")
                walk(value, f"{prefix}.{key}")
        elif isinstance(node, list):
            for index, item in enumerate(node):
                walk(item, f"{prefix}[{index}]")

    walk(_experience(journey_id))
    walk(_wallet(journey_id))
    offenders = [path for path in keys if any(word in path.lower() for word in CONVERTED_WORDS)]
    assert offenders == [], offenders
    # And the two answers keep each amount in the currency the store holds it in.
    assert _experience(journey_id)["journey"]["budget"]["currency"] == "CNY"
    assert _wallet(journey_id)["reservations"][0]["price"]["currency"] == "CNY"
