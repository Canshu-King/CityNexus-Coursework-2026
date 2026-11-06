"""ORD-0033 - a trip over the supported window is refused at the door, by name.

The report this file exists for: on a phone, "确认创建" pressed four times did nothing
visible.  The preview backend logged four pairs of `POST /api/v1/journeys` -> 201 followed
by `POST .../stay-options/generate` -> 500 with
`ValueError: V13_JOURNEY_DAYS_OUT_OF_BOUNDS:9`, and the library ended up holding five
journeys that could never produce a plan.

Four promises, each with its own node:

* the ceiling is enforced where the dates are entered, not at the deepest call, and a
  refused request writes nothing (`test_a_refused_creation_leaves_no_journey_row`);
* the refusal is a named 422 carrying the numbers a traveller can act on, not a traceback;
* the ceiling is still 7 days, watched from both sides - shrinking it to make a test pass
  and loosening it to make a demo easy each turn something red here;
* the deep path also answers a named 422, for rows that predate the door.

The 7-day value itself is deliberately untouched, as ordered: this fixes how the failure is
expressed, not the rule.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.core.temporal import MAX_JOURNEY_DAYS, derive_journey_temporal_shape
from app.db.models import Journey, JourneyTripContext, Traveler
from app.db.session import SessionLocal
from app.main import TEMPORAL_REJECTIONS, app, temporal_rejection
from app.stay_mobility import _anchor, journey_boundaries
from demo_stage import demo_reset_headers  # noqa: E402  (ORD-0389)

client = TestClient(app)
TZ = timezone(timedelta(hours=8))
OVER_WINDOW = MAX_JOURNEY_DAYS + 2  # 9 - the window the user actually typed


def _traveler() -> str:
    return client.post("/api/v1/demo/reset", headers=demo_reset_headers()).json()["traveler_id"]


def _at(day: int, hour: int = 9) -> datetime:
    return datetime(2026, 9, day, hour, 0, tzinfo=TZ)


def _payload(traveler: str, days: int, **extra: object) -> dict:
    """A journey whose own window spans exactly `days` calendar days."""

    start = _at(27)
    return {
        "traveler_id": traveler,
        "title": f"{days}-day window",
        "city_code": "CNX-DEMO",
        "timezone": "Asia/Shanghai",
        "start_at": start.isoformat(),
        "end_at": (start + timedelta(days=days - 1, hours=8)).isoformat(),
        "budget": {"amount_minor": 50000, "currency": "CNY"},
        "planning_mode": "balanced",
        **extra,
    }


def _journey_rows() -> int:
    with SessionLocal() as session:
        return session.scalar(select(func.count(Journey.id))) or 0


def _anchor_payload(at: datetime, location: str) -> dict:
    return {"at": at.isoformat(), "location": location}


def _insert_over_window_journey() -> str:
    """Write an unusable journey straight to the table, bypassing the door.

    Needed to reach the deep path at all: once creation refuses, the only way to arrive at
    `/stay-options/generate` with a nine-day window is a row that predates the rule - which
    is exactly what the preview library had.
    """

    journey_id = str(uuid4())
    now = datetime.now(timezone.utc).isoformat()
    with SessionLocal() as session:
        traveler = session.scalar(select(Traveler.id))
        assert traveler is not None, "the gate needs a seeded traveler (call _traveler() first)"
        session.add(
            Journey(
                id=journey_id,
                traveler_id=str(traveler),
                title="legacy nine days",
                city_code="CNX-DEMO",
                timezone="Asia/Shanghai",
                start_at=_at(27).isoformat(),
                end_at=(_at(27) + timedelta(days=OVER_WINDOW - 1, hours=8)).isoformat(),
                budget_currency="CNY",
                budget_amount_minor=50000,
                planning_mode="balanced",
                status="DRAFT",
                active_plan_version_id=None,
                current_activity_id=None,
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            JourneyTripContext(
                journey_id=journey_id,
                arrival_anchor_json="{}",
                departure_anchor_json="{}",
                party_size=1,
                occupancy_json=json.dumps({"adults": 1, "rooms": 1}),
                stay_budget_amount_minor=0,
                stay_budget_currency="CNY",
                created_at=now,
                updated_at=now,
            )
        )
        session.commit()
    return journey_id


def test_a_trip_over_the_window_is_refused_at_creation_by_name_and_with_numbers() -> None:
    traveler = _traveler()
    response = client.post("/api/v1/journeys", json=_payload(traveler, OVER_WINDOW))

    assert response.status_code == 422, response.text
    body = response.json()["error"]
    assert body["code"] == "V13_JOURNEY_DAYS_OUT_OF_BOUNDS", body
    # The numbers are the point: "invalid dates" is not something a traveller can act on,
    # "you asked for 9, the ceiling is 7" is.
    assert body["details"]["actual_days"] == OVER_WINDOW, body["details"]
    assert body["details"]["max_days"] == MAX_JOURNEY_DAYS, body["details"]
    assert body["details"]["min_days"] == 1, body["details"]
    assert str(MAX_JOURNEY_DAYS) in body["message"], body["message"]
    assert body["trace_id"], "a refusal the logs cannot tie to a request is one nobody can chase"


def test_a_refused_creation_leaves_no_journey_row() -> None:
    """Ruling A's whole reason for existing: five taps must not mean five dead journeys."""

    traveler = _traveler()
    before = _journey_rows()
    for _ in range(4):  # the presses the user actually made
        response = client.post("/api/v1/journeys", json=_payload(traveler, OVER_WINDOW))
        assert response.status_code == 422, response.text
    assert _journey_rows() == before, "a refused create still wrote a Journey row"


def test_a_trip_of_exactly_the_supported_window_is_still_accepted() -> None:
    """The other direction: a refusal here would mean the ceiling got shrunk."""

    traveler = _traveler()
    accepted = client.post("/api/v1/journeys", json=_payload(traveler, MAX_JOURNEY_DAYS))
    assert accepted.status_code == 201, accepted.text
    with SessionLocal() as session:
        row = session.get(Journey, accepted.json()["id"])
        assert row is not None, "a 201 without a row"
        assert (
            derive_journey_temporal_shape(
                datetime.fromisoformat(row.start_at), datetime.fromisoformat(row.end_at)
            ).day_count
            == MAX_JOURNEY_DAYS
        )


def test_the_supported_ceiling_is_named_and_seven_days() -> None:
    """The order forbids changing 7, so a silent edit has to fail somewhere loud.

    Asserting only "9 days is refused" would stay green if someone shrank the ceiling to 3
    to make a different test pass, so the constant is pinned as well as probed from both
    sides of it.
    """

    assert MAX_JOURNEY_DAYS == 7, MAX_JOURNEY_DAYS
    traveler = _traveler()
    inside = client.post("/api/v1/journeys", json=_payload(traveler, 7))
    outside = client.post("/api/v1/journeys", json=_payload(traveler, 8))
    assert inside.status_code == 201, inside.text
    assert outside.status_code == 422, outside.text
    assert outside.json()["error"]["details"]["actual_days"] == 8


def test_the_door_measures_the_window_the_pricer_measures_not_the_one_the_form_showed() -> None:
    """An anchor's own moment wins over the raw window, at both places.

    Checked in both directions, because a door that only read `start_at`/`end_at` would be
    wrong twice: it would refuse a journey whose anchors fit, and accept one whose anchors
    do not.  The last block pins `journey_boundaries` against `_anchor` itself, since those
    two restating the same rule is how the doors and the pricer drift apart.
    """

    traveler = _traveler()

    narrow_anchors = _payload(
        traveler,
        MAX_JOURNEY_DAYS,
        arrival_anchor=_anchor_payload(_at(28), "Tianjin West Station"),
        departure_anchor=_anchor_payload(_at(30, 18), "Tianjin South Station"),
    )
    # A journey window of seven days whose anchors span three: accepted, and it is the
    # anchors that decided it - proof the door does not read the raw window on its own.
    accepted = client.post("/api/v1/journeys", json=narrow_anchors)
    assert accepted.status_code == 201, accepted.text
    accepted_id = accepted.json()["id"]

    wide_anchors = _payload(
        traveler,
        3,
        arrival_anchor=_anchor_payload(_at(20), "Tianjin West Station"),
        departure_anchor=_anchor_payload(_at(29, 18), "Tianjin South Station"),
    )
    # Counted here on purpose: the accepted create above legitimately added a row, and a
    # baseline taken before it would turn this into a test of my arithmetic.
    rows_before_refusal = _journey_rows()
    refused = client.post("/api/v1/journeys", json=wide_anchors)
    assert refused.status_code == 422, refused.text
    assert refused.json()["error"]["details"]["actual_days"] == 10, refused.json()["error"]
    assert _journey_rows() == rows_before_refusal, "the anchor-shaped refusal still wrote a row"

    with SessionLocal() as session:
        journey = session.get(Journey, accepted_id)
        assert journey is not None, "a 201 whose row cannot be read back"
        context = session.get(JourneyTripContext, journey.id)
        assert context is not None, "creation wrote a Journey without its trip context"
        assert journey_boundaries(context, journey.start_at, journey.end_at) == (
            datetime.fromisoformat(_anchor(context, journey, "arrival")["at"]),
            datetime.fromisoformat(_anchor(context, journey, "departure")["at"]),
        )


def test_generating_stays_over_the_window_answers_a_named_422_instead_of_a_bare_500() -> None:
    """The reported symptom itself, for a row that predates the door."""

    _traveler()
    journey_id = _insert_over_window_journey()
    response = client.post(f"/api/v1/journeys/{journey_id}/stay-options/generate", json={})

    assert response.status_code == 422, (
        f"a rule reached the client as HTTP {response.status_code}: {response.text[:200]}"
    )
    body = response.json()["error"]
    assert body["code"] == "V13_JOURNEY_DAYS_OUT_OF_BOUNDS", body
    assert body["details"]["actual_days"] == OVER_WINDOW, body


def test_patching_a_saved_trip_past_the_window_is_refused_without_writing() -> None:
    """Ruling A would be half a rule if the other door still let the state in."""

    traveler = _traveler()
    created = client.post("/api/v1/journeys", json=_payload(traveler, 4))
    assert created.status_code == 201, created.text
    journey_id = created.json()["id"]
    original_end = _at(27) + timedelta(days=3, hours=8)

    response = client.patch(
        f"/api/v1/journeys/{journey_id}",
        json={"end_at": (_at(27) + timedelta(days=MAX_JOURNEY_DAYS + 1, hours=8)).isoformat()},
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "V13_JOURNEY_DAYS_OUT_OF_BOUNDS"
    with SessionLocal() as session:
        stored = session.get(Journey, journey_id)
        assert stored is not None and datetime.fromisoformat(stored.end_at) == original_end, (
            "the refused patch still moved the window"
        )


def test_an_unexpected_value_error_stays_a_server_error() -> None:
    """The conversion must not become a launder for genuine bugs.

    Anything without one of the two named prefixes keeps its 500, or a real defect would
    start arriving as "client error" and no one would be told.
    """

    assert set(TEMPORAL_REJECTIONS) == {
        "V13_JOURNEY_DAYS_OUT_OF_BOUNDS",
        "V13_INVALID_ANCHOR_WINDOW",
    }
    for message in (
        "password reset token malformed",
        "V14_SOMETHING_NEW:3",
        "V13_JOURNEY_DAYS_OUT_OF_BOUNDSX:9",
    ):
        escaped = False
        try:
            asyncio.run(temporal_rejection(None, ValueError(message)))
        except ValueError:
            escaped = True
        assert escaped, f"{message!r} was turned into a 4xx instead of staying a server error"
