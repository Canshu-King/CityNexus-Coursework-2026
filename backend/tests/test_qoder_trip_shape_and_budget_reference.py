"""ORD-0063 / ORD-0064(a) - one trip shape and one allowance, computed by the backend.

What a phone actually showed, measured on the authoritative build: a Singapore trip
"08:00 - 20:00 on the same date" carried the headline "2 天", and the same screen divided
the budget by that invented second day, and printed a ¥ sign on a JPY budget.  Three
symptoms, one cause: the screen kept its own arithmetic (`Math.round(hours/24)+1`) beside
the backend's, so two answers existed and the false one was the visible one.

These tests pin the contract that replaces it.  The projection now carries
`journey.temporal_shape` and `trip.budget_reference`, and every case below is written so a
lane cannot pass it by moving the wrong formula to a different file:

* a same-date trip is one day and zero nights, and the allowance equals budget / party;
* the projected count is *the same object* the pricing path derives, not an equal number;
* cross-midnight and multi-day agree with the documented "Oct 1 -> Oct 4 = 4 days / 3 nights";
* the division rounds half-up in integers, so an exact half cannot print two ways;
* a non-CNY budget keeps its own currency and loses the CNY-denominated "tight" floor;
* a stored row the doors would reject is still readable - disclosed, not 500, not rounded
  into the window;
* a window that cannot be ordered yields no count at all rather than a plausible "1".
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.temporal import (
    BUDGET_DAILY_FLOOR_CURRENCY,
    BUDGET_DAILY_FLOOR_MINOR,
    FLOOR_NOT_COMPARABLE,
    MAX_JOURNEY_DAYS,
    MIN_JOURNEY_DAYS,
    UNAVAILABLE_NO_BUDGET,
    UNAVAILABLE_SHAPE_UNKNOWN,
    WINDOW_IN,
    WINDOW_INVALID,
    WINDOW_OUT,
    WINDOW_UNCOMPARABLE,
    derive_budget_reference,
    derive_journey_temporal_shape,
)
from app.db.models import Journey, JourneyTripContext, Traveler
from app.db.session import SessionLocal
from app.main import app
from app.services import journey_schema
from app.stay_mobility import journey_projection_window, journey_temporal_shape
from demo_stage import demo_reset_headers  # noqa: E402  (ORD-0389)

client = TestClient(app)
TZ = timezone(timedelta(hours=8))


def _traveler() -> str:
    return client.post("/api/v1/demo/reset", headers=demo_reset_headers()).json()["traveler_id"]


def _at(day: int, hour: int = 8) -> datetime:
    return datetime(2026, 9, day, hour, 0, tzinfo=TZ)


def _create(
    traveler: str,
    start: datetime,
    end: datetime,
    *,
    amount_minor: int = 20000,
    currency: str = "CNY",
    party_size: int = 1,
) -> str:
    response = client.post(
        "/api/v1/journeys",
        json={
            "traveler_id": traveler,
            "title": f"{currency} {amount_minor} / {party_size}",
            "city_code": "CNX-DEMO",
            "timezone": "Asia/Shanghai",
            "start_at": start.isoformat(),
            "end_at": end.isoformat(),
            "budget": {"amount_minor": amount_minor, "currency": currency},
            "party_size": party_size,
            "planning_mode": "balanced",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _projection(journey_id: str) -> dict:
    response = client.get(f"/api/v1/journeys/{journey_id}/experience")
    assert response.status_code == 200, response.text
    return response.json()


def _shape_of(journey_id: str) -> dict:
    return _projection(journey_id)["journey"]["temporal_shape"]


def _allowance_of(journey_id: str) -> dict:
    return _projection(journey_id)["trip"]["budget_reference"]


def _insert_with_window(
    start_raw: str,
    end_raw: str,
    *,
    amount_minor: int = 20000,
    currency: str = "CNY",
    party_size: int = 1,
    status: str = "DRAFT",
    arrival_anchor: dict | None = None,
    departure_anchor: dict | None = None,
) -> str:
    """Write a journey straight to the table, so a window the API would refuse is reachable.

    Rows like this exist for real: the preview library held nine-day journeys created before
    ORD-0033's door, and a `GET /experience` that 500s on them takes the traveller's whole
    screen away for a condition they cannot fix from the front door.

    `arrival_anchor` / `departure_anchor` are how an *inverted* window is reached at all:
    `journeys` carries `CHECK (end_at > start_at)`, measured below, while the anchor JSON on
    the trip context has no such guard and is what actually bounds a priced trip.
    """

    journey_id = str(uuid4())
    now = datetime.now(timezone.utc).isoformat()
    with SessionLocal() as session:
        traveler = session.scalar(select(Traveler.id))
        assert traveler is not None, "needs a seeded traveler (call _traveler() first)"
        session.add(
            Journey(
                id=journey_id,
                traveler_id=str(traveler),
                title="stored window",
                city_code="CNX-DEMO",
                timezone="Asia/Shanghai",
                start_at=start_raw,
                end_at=end_raw,
                budget_currency=currency,
                budget_amount_minor=amount_minor,
                planning_mode="balanced",
                status=status,
                active_plan_version_id=None,
                current_activity_id=None,
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            JourneyTripContext(
                journey_id=journey_id,
                arrival_anchor_json=json.dumps(arrival_anchor or {}),
                departure_anchor_json=json.dumps(departure_anchor or {}),
                party_size=party_size,
                occupancy_json=json.dumps({"adults": party_size, "rooms": 1}),
                stay_budget_amount_minor=0,
                stay_budget_currency=currency,
                created_at=now,
                updated_at=now,
            )
        )
        session.commit()
    return journey_id


# ---------------------------------------------------------------- the reported number


def test_a_same_day_trip_is_one_day_and_the_allowance_is_budget_over_party() -> None:
    """08:00 -> 20:00 on one date is a twelve-hour trip, not a two-day one.

    The old screen formula returned 2 here, which made the headline contradict the time
    range printed two lines below it on the same screen.
    """

    traveler = _traveler()
    journey_id = _create(traveler, _at(26, 8), _at(26, 20), amount_minor=60000, party_size=3)

    shape = _shape_of(journey_id)
    assert shape["day_count"] == 1, shape
    assert shape["night_count"] == 0, shape
    assert shape["window_status"] == WINDOW_IN, shape
    # Stated in the payload rather than inferred from a filename: the count is calendar days
    # with both ends included, and a client that needs "nights" must read `night_count`.
    assert shape["semantics"] == "CALENDAR_DAYS_INCLUSIVE", shape
    assert shape["min_days"] == MIN_JOURNEY_DAYS, shape
    assert shape["max_days"] == MAX_JOURNEY_DAYS, shape

    allowance = _allowance_of(journey_id)
    assert allowance["available"] is True, allowance
    assert allowance["day_count"] == 1, allowance
    assert allowance["party_size"] == 3, allowance
    # ORD-0063's acceptance, computed by the backend: one day means per-day == the whole
    # budget, and per person per day is exactly budget / party.
    assert allowance["per_day_minor"] == 60000, allowance
    assert allowance["per_person_per_day_minor"] == 20000, allowance


def test_cross_midnight_and_multi_day_windows_use_the_documented_rule() -> None:
    """A night out is 2 days / 1 night; Oct 1 -> Oct 4 is 4 days / 3 nights."""

    traveler = _traveler()
    overnight = _create(traveler, _at(26, 22), _at(27, 6))
    multi_day = _create(traveler, _at(1, 9), _at(4, 18))

    assert (_shape_of(overnight)["day_count"], _shape_of(overnight)["night_count"]) == (2, 1)
    assert (_shape_of(multi_day)["day_count"], _shape_of(multi_day)["night_count"]) == (4, 3)
    # The 7-day ceiling is unchanged by this order; a 4-day trip must not read as out of window.
    assert _shape_of(multi_day)["window_status"] == WINDOW_IN


def test_the_projection_and_the_pricing_path_return_the_same_shape_object() -> None:
    """One formula, two judgements: the read count *is* the pricing count, not equal by luck.

    The defect was two implementations disagreeing, so the gate compares the objects from
    the write path's validator and the read path's projection over the same rows.
    """

    traveler = _traveler()
    cases = [(_at(26, 8), _at(26, 20)), (_at(26, 22), _at(27, 6)), (_at(1, 9), _at(4, 18))]
    with SessionLocal() as session:
        for start, end in cases:
            journey_id = _create(traveler, start, end)
            row = session.get(Journey, journey_id)
            context = session.get(JourneyTripContext, journey_id)
            priced = journey_temporal_shape(context, row)
            projected, status = journey_projection_window(context, row)
            assert projected == priced, (projected, priced)
            assert status == WINDOW_IN, status
            assert _shape_of(journey_id)["day_count"] == priced.day_count


def test_the_allowance_divides_half_up_so_an_exact_half_cannot_print_two_ways() -> None:
    """`round()` is half-to-even in Python; a money figure must not depend on that."""

    assert derive_budget_reference(
        day_count=2, budget_amount_minor=5, budget_currency="CNY", party_size=1
    ).per_day_minor == 3
    assert derive_budget_reference(
        day_count=2, budget_amount_minor=7, budget_currency="CNY", party_size=1
    ).per_day_minor == 4
    # Two steps on purpose: per day first, then per person, which is how the number is read.
    assert derive_budget_reference(
        day_count=3, budget_amount_minor=10000, budget_currency="CNY", party_size=2
    ).per_person_per_day_minor == 1667


def test_a_party_of_zero_never_reaches_the_division() -> None:
    """Two guards, and only one of them owns the invariant.

    `journey_trip_contexts.party_size >= 1` is a SQL CHECK, so a stored zero is not a state
    the projection can be reached in - pinning that here is what keeps the next reader from
    "fixing" a case that cannot exist.  The clamp inside `derive_budget_reference` is
    therefore belt-and-braces for a caller that hands it a bad number, not the guarantee.
    """

    from sqlalchemy.exc import IntegrityError

    _traveler()
    try:
        _insert_with_window(
            _at(26, 8).isoformat(), _at(26, 20).isoformat(), party_size=0
        )
    except IntegrityError as exc:
        assert "party_size" in str(exc), exc
    else:
        raise AssertionError("the party_size CHECK stopped enforcing; the clamp is now load-bearing")

    clamped = derive_budget_reference(
        day_count=1, budget_amount_minor=20000, budget_currency="CNY", party_size=0
    )
    assert clamped.party_size == 1, clamped
    assert clamped.per_person_per_day_minor == 20000, clamped


# ------------------------------------------------------------------ the currency half


def test_a_non_cny_budget_keeps_its_own_currency_and_loses_the_cny_floor() -> None:
    """A JPY budget must carry "JPY", and may not be judged by a Chinese price level.

    ¥ is also JPY's sign, which is exactly why a sign hard-coded to CNY is unreadable here:
    the code travels with the amount and the client renders it.
    """

    traveler = _traveler()
    journey_id = _create(traveler, _at(26, 8), _at(26, 20), amount_minor=150000, currency="JPY")

    payload = _projection(journey_id)
    assert payload["journey"]["budget"]["currency"] == "JPY", payload["journey"]["budget"]
    allowance = payload["trip"]["budget_reference"]
    assert allowance["currency"] == "JPY", allowance
    assert allowance["available"] is True, allowance
    assert allowance["per_day_minor"] == 150000, allowance
    # The floor is CNY minor units.  Reporting it as not comparable is the honest answer;
    # silently applying it rated a Tokyo trip against Shanghai prices.
    assert allowance["floor_currency"] == BUDGET_DAILY_FLOOR_CURRENCY, allowance
    assert allowance["floor_minor"] == BUDGET_DAILY_FLOOR_MINOR, allowance
    assert allowance["floor_comparable"] is False, allowance
    assert allowance["reason"] == FLOOR_NOT_COMPARABLE, allowance


def test_a_cny_budget_is_still_compared_against_the_floor() -> None:
    traveler = _traveler()
    journey_id = _create(traveler, _at(26, 8), _at(26, 20), amount_minor=20000, currency="CNY")
    allowance = _allowance_of(journey_id)
    assert allowance["floor_comparable"] is True, allowance
    assert allowance["reason"] is None, allowance


def test_the_floor_threshold_itself_is_unchanged() -> None:
    """CNY 150 per person per day, from the frontend constant, moved not invented."""

    assert (BUDGET_DAILY_FLOOR_MINOR, BUDGET_DAILY_FLOOR_CURRENCY) == (15_000, "CNY")


# --------------------------------------------------------- unreadable windows stay open


def test_a_stored_trip_over_the_window_is_disclosed_instead_of_500_or_truncated() -> None:
    """Nine days stays nine days on screen, with the judgement named beside it."""

    _traveler()
    start = _at(27)
    journey_id = _insert_with_window(
        start.isoformat(), (start + timedelta(days=MAX_JOURNEY_DAYS + 1, hours=8)).isoformat()
    )
    shape = _shape_of(journey_id)
    assert shape["day_count"] == MAX_JOURNEY_DAYS + 2, shape
    assert shape["window_status"] == WINDOW_OUT, shape
    # Refusing to price is the write path's job; a read still tells the truth about the money.
    allowance = _allowance_of(journey_id)
    assert allowance["available"] is True, allowance
    assert allowance["day_count"] == MAX_JOURNEY_DAYS + 2, allowance

    with SessionLocal() as session:
        row = session.get(Journey, journey_id)
        context = session.get(JourneyTripContext, journey_id)
        try:
            journey_temporal_shape(context, row)
        except ValueError as exc:
            assert str(exc).startswith("V13_JOURNEY_DAYS_OUT_OF_BOUNDS"), exc
        else:
            raise AssertionError("the pricing door stopped judging the window")


def test_naive_against_offset_anchors_reports_no_count_rather_than_one_day() -> None:
    """Cannot order the two instants at all is not the same as a one-day trip."""

    _traveler()
    journey_id = _insert_with_window("2026-09-26T08:00:00", "2026-09-26T20:00:00+08:00")
    shape = _shape_of(journey_id)
    assert shape["window_status"] == WINDOW_UNCOMPARABLE, shape
    assert shape["day_count"] is None, shape
    assert shape["night_count"] is None, shape

    allowance = _allowance_of(journey_id)
    assert allowance["available"] is False, allowance
    assert allowance["reason"] == UNAVAILABLE_SHAPE_UNKNOWN, allowance
    assert allowance["per_day_minor"] is None, allowance
    assert allowance["per_person_per_day_minor"] is None, allowance
    # The raw window is still on the payload: hiding it would leave the traveller with
    # nothing to check the "unknown" against.
    assert _projection(journey_id)["journey"]["start_at"].endswith("08:00:00")


def test_an_inverted_window_is_named_as_invalid_rather_than_one_day() -> None:
    """Departure before arrival is a broken record, not a one-day trip.

    Reached through the anchors, which is the only way it can exist: `journeys` has
    `CHECK (end_at > start_at)`, but an anchor's own moment is what bounds the priced trip
    and nothing constrains the pair inside the JSON.
    """

    from sqlalchemy.exc import IntegrityError

    _traveler()
    journey_id = _insert_with_window(
        _at(26, 8).isoformat(),
        _at(27, 20).isoformat(),
        arrival_anchor={"at": _at(27, 20).isoformat(), "location": "late arrival"},
        departure_anchor={"at": _at(26, 8).isoformat(), "location": "early departure"},
    )
    shape = _shape_of(journey_id)
    assert shape["window_status"] == WINDOW_INVALID, shape
    assert shape["day_count"] is None, shape
    assert _allowance_of(journey_id)["reason"] == UNAVAILABLE_SHAPE_UNKNOWN

    try:
        _insert_with_window(_at(27, 20).isoformat(), _at(26, 8).isoformat())
    except IntegrityError as exc:
        assert "end_at" in str(exc), exc
    else:
        raise AssertionError("the journeys CHECK stopped enforcing, so inverted rows are now possible")


def test_a_journey_with_no_budget_says_so_instead_of_sharing_zero() -> None:
    """A zero amount must never render as "每人每天约 ¥0", which reads as a free trip."""

    _traveler()
    journey_id = _insert_with_window(
        _at(26, 8).isoformat(), _at(26, 20).isoformat(), amount_minor=0
    )
    allowance = _allowance_of(journey_id)
    assert allowance["available"] is False, allowance
    assert allowance["reason"] == UNAVAILABLE_NO_BUDGET, allowance
    assert allowance["per_person_per_day_minor"] is None, allowance
    # The trip shape is still knowable and still reported.
    assert _shape_of(journey_id)["day_count"] == 1


def test_the_journey_schema_still_carries_the_raw_window_it_always_carried() -> None:
    """Additive contract: the new blocks do not replace `start_at` / `end_at` / `budget`.

    Other consumers (copilot context, replanning) read those fields, and a "single source"
    change that deletes them would move the ambiguity rather than remove it.
    """

    traveler = _traveler()
    journey_id = _create(traveler, _at(26, 8), _at(27, 20))
    payload = _projection(journey_id)
    journey = payload["journey"]
    assert journey["start_at"] == _at(26, 8).isoformat(), journey
    assert journey["end_at"] == _at(27, 20).isoformat(), journey
    assert journey["budget"] == {"amount_minor": 20000, "currency": "CNY"}, journey
    with SessionLocal() as session:
        row = session.get(Journey, journey_id)
        assert journey_schema(row).model_dump(mode="json")["id"] == journey_id
