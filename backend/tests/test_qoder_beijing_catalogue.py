"""ORD-0089 - a real city in the catalogue has to be plannable by the default path.

Measured before this file existed: choosing 北京 on the phone built the journey, the AI plan
ran 13 seconds, changed nothing, and `plans/generate` answered 422 "No feasible plan
candidates".  Two independent causes, one fix each, and each gets its own gate here:

* the mock catalogue held **one** city (`CNX-DEMO`), so there was nothing to plan with; and
* `_templates()` is a fixture of `CNX-DEMO` place ids and the no-anchor scheduler returns
  `None` at the first id it cannot resolve - so even with a full Beijing set the default path
  could never have drawn a schedule (only the explicit `place_ids` path could).

The fourth gate is the one that keeps the other three honest: a city with no catalogue must
still be refused by name, so "candidates appeared" cannot be the product inventing places.
`CNX-DEMO`'s own fixture is pinned too - extending a catalogue must not silently re-author the
demo stage another lane is demonstrating on.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.models import Activity, CityService, Place
from app.db.session import SessionLocal
from app.main import app
from demo_stage import demo_reset_headers  # noqa: E402  (ORD-0389)

client = TestClient(app)
TZ = timezone(timedelta(hours=8))
MOCK_ROOT = Path(__file__).resolve().parents[2] / "data" / "mock"
BEIJING = "CN-BEIJING"
DEMO = "CNX-DEMO"
# The frozen demo fixture's own first template, verbatim.  Pinned here rather than re-derived
# so a change to the demo stage cannot be waved through by the code that reads it.
DEMO_FIRST_TEMPLATE_IDS = (
    "place-old-town",
    "place-riverside-museum",
    "place-food-hall",
    "place-tech-art",
    "place-river-cruise",
    "place-skyline-dinner",
)


def _minutes(value: str) -> int:
    """Minutes since midnight, from either a stored activity instant or an "HH:MM" opening time."""

    text = str(value)
    if "T" in text:
        moment = datetime.fromisoformat(text)
        return moment.hour * 60 + moment.minute
    hour, minute = (int(part) for part in text.split(":", 1))
    return hour * 60 + minute


@pytest.fixture()
def _seeded(monkeypatch) -> None:
    monkeypatch.setattr("app.main.demo_mode_enabled", lambda: True)
    assert client.post("/api/v1/demo/reset", headers=demo_reset_headers()).status_code == 200


def _journey(city_code: str, *, days: int = 2, budget_minor: int = 150_000) -> str:
    traveler = client.post("/api/v1/demo/reset", headers=demo_reset_headers()).json()["traveler_id"]
    start = datetime(2026, 9, 22, 9, 0, tzinfo=TZ)
    created = client.post(
        "/api/v1/journeys",
        json={
            "traveler_id": traveler,
            "title": f"{city_code} default-path plan",
            "city_code": city_code,
            "timezone": "Asia/Shanghai",
            "start_at": start.isoformat(),
            "end_at": (start + timedelta(days=days - 1, hours=10)).isoformat(),
            "budget": {"amount_minor": budget_minor, "currency": "CNY"},
            "planning_mode": "balanced",
        },
    )
    assert created.status_code == 201, created.text
    return created.json()["id"]


def _generate(journey_id: str):
    return client.post(f"/api/v1/journeys/{journey_id}/plans/generate", json={})


def _places_raw() -> list[dict]:
    return json.loads((MOCK_ROOT / "places.json").read_text(encoding="utf-8"))


def _matrix_raw() -> dict:
    return json.loads((MOCK_ROOT / "mobility_matrix.json").read_text(encoding="utf-8"))


def test_the_beijing_catalogue_is_real_data_and_every_leg_is_priced_explicitly(_seeded) -> None:
    """Item 1 and 2 of the order: real places, and minutes that are not the blanket default."""

    places = [item for item in _places_raw() if item["city_code"] == BEIJING]
    assert len(places) >= 6, len(places)
    names = " ".join(str(item["name"]) for item in places)
    for expected in ("故宫", "天安门", "颐和园", "南锣鼓巷", "大栅栏", "全聚德"):
        assert expected in names, expected
    for place in places:
        assert place["id"].startswith("place-beijing-"), place["id"]
        assert place["provider_key"] == "mock-city", place
        assert 39.4 < place["location"]["latitude"] < 40.7, place
        assert 115.6 < place["location"]["longitude"] < 117.2, place
        assert place["category"], place
        assert datetime.strptime(place["open"], "%H:%M") < datetime.strptime(
            place["close"], "%H:%M"
        ), place
        assert int(place["duration_min"]) > 0 and int(place["cost_minor"]) >= 0, place
        assert place["tags"], place
    # The demo city is still there, unchanged, because another lane demos on it.
    assert [item["id"] for item in _places_raw() if item["city_code"] == DEMO][:8] == [
        "place-old-town",
        "place-riverside-museum",
        "place-food-hall",
        "place-tech-art",
        "place-river-cruise",
        "place-skyline-dinner",
        "place-city-park",
        "place-design-library",
    ]
    services = [
        item for item in json.loads((MOCK_ROOT / "city_services.json").read_text("utf-8"))
    ]
    beijing_services = [item for item in services if item["city_code"] == BEIJING]
    assert {item["service_type"] for item in beijing_services} >= {
        "mobility_hub",
        "visitor_support",
        "bookable_experience",
        "tradable_food",
    }, beijing_services
    for item in beijing_services:
        assert item["provider_key"] in {"mock-city", "mock-booking"}, item

    matrix = _matrix_raw()
    ids = [item["id"] for item in places]
    missing = [
        f"{origin}|{destination}"
        for origin in ids
        for destination in ids
        if origin != destination and f"{origin}|{destination}" not in matrix["minutes"]
    ]
    assert not missing, f"pairs that would fall back to default_minutes: {missing[:6]}"
    explicit = [matrix["minutes"][f"{a}|{b}"] for a in ids for b in ids if a != b]
    assert set(explicit) != {matrix["default_minutes"]}, "every 'explicit' pair equals the default"


def test_a_beijing_journey_gets_at_least_two_candidates_from_the_default_path(_seeded) -> None:
    """The acceptance line: no `place_ids`, no hand-fed places, just the catalogue."""

    with SessionLocal() as session:
        assert session.scalars(
            select(Place).where(Place.city_code == BEIJING)
        ).first() is not None, "the seed must load the catalogue into the database, not just JSON"
    journey_id = _journey(BEIJING)
    response = _generate(journey_id)
    assert response.status_code == 201, response.text
    plans = response.json()
    assert len(plans) >= 2, plans
    with SessionLocal() as session:
        for plan in plans:
            activities = session.scalars(
                select(Activity)
                .where(Activity.plan_version_id == plan["id"])
                .order_by(Activity.sequence_no)
            ).all()
            assert len(activities) >= 2, plan["id"]
            for activity in activities:
                place = session.get(Place, activity.place_id)
                assert place is not None and place.city_code == BEIJING, (
                    activity.place_id,
                    place.city_code if place else None,
                )
                assert place.provider_key == "mock-city", place.provider_key
                # Sequenced, not stacked: the travel minutes were really consumed.
                if activity.sequence_no > 1:
                    assert int(activity.travel_from_prev_min) > 0, plan["id"]
                    previous = activities[activity.sequence_no - 2]
                    assert activity.start_at >= previous.end_at, (activity.start_at, previous.end_at)
                # And inside the venue's own declared hours - the one thing a derived
                # schedule can quietly get wrong by packing one stop too many.
                hours = json.loads(place.opening_hours_json or "{}")
                start_minute = _minutes(activity.start_at)
                end_minute = _minutes(activity.end_at)
                assert start_minute >= _minutes(hours["open"]), (activity.title, hours)
                assert end_minute <= _minutes(hours["close"]), (activity.title, hours)
                assert end_minute > start_minute, activity.title
        assert plans[0]["id"] != plans[1]["id"]
    # Two candidates that are the same schedule three ways would satisfy a count and teach
    # nothing, so the orders must actually differ.
    orders = [[item["title"] for item in plan["activities"]] for plan in plans]
    assert orders[0] != orders[1], orders
    assert len({tuple(order) for order in orders}) == len(orders), orders
    # `total_cost` is the nested amount/currency shape the API actually answers with.
    assert all(plan["total_cost"]["currency"] == "CNY" for plan in plans), plans
    assert all(plan["mobility_snapshot_id"] for plan in plans), plans


def test_the_demo_city_still_composes_from_its_frozen_templates(_seeded) -> None:
    """Regression face: a catalogue city must not become a reason to re-author the demo stage."""

    journey_id = _journey(DEMO)
    response = _generate(journey_id)
    assert response.status_code == 201, response.text
    plans = response.json()
    assert len(plans) == 3, plans
    first = [item["place_id"] for item in plans[0]["activities"]]
    assert first == list(DEMO_FIRST_TEMPLATE_IDS), first
    assert plans[0]["activities"][4]["start_at"].endswith("15:30:00+08:00"), plans[0]["activities"][4]


def test_a_city_with_nothing_in_the_catalogue_is_still_refused_by_name(_seeded) -> None:
    """Without this, the two green gates above could be the planner inventing places."""

    with SessionLocal() as session:
        assert session.scalars(
            select(Place).where(Place.city_code == "CN-SHANGHAI")
        ).first() is None, "this gate means nothing if Shanghai has data"
    journey_id = _journey("CN-SHANGHAI")
    response = _generate(journey_id)
    assert response.status_code == 422, response.text
    body = response.json()["error"]
    assert "No feasible plan candidates" in body["message"], body


def test_the_packer_drops_a_stop_that_cannot_fit_before_its_own_closing_time() -> None:
    """The guard that keeps a derived day honest, exercised where it can actually bite.

    Measured: with the real Beijing set no venue is ever over-run, so a mutant that removes
    the closing-time guard survives the end-to-end gate - a no-op mutant, not a passing test.
    This gate therefore feeds the packer a catalogue that *does* over-run, and asserts the
    over-running stop is the one that disappears.
    """

    from app.services import _catalogue_templates, _place_window

    def place(place_id: str, open_at: str, close_at: str, duration: int) -> Place:
        return Place(
            id=place_id,
            provider_key="mock-city",
            external_id=place_id.upper(),
            city_code=BEIJING,
            name=place_id,
            category="museum",
            latitude=39.92,
            longitude=116.4,
            opening_hours_json=json.dumps({"open": open_at, "close": close_at}),
            tags_json=json.dumps(["culture"]),
            metadata_json=json.dumps({"duration_min": duration, "cost_minor": 0}),
            updated_at="2026-09-22T00:00:00+08:00",
        )

    places = {
        item.id: item
        for item in (
            place("place-open-early", "08:00", "22:00", 60),
            place("place-closes-early", "08:00", "09:30", 120),
            place("place-open-late", "09:00", "22:00", 60),
        )
    }
    travel = {"place-open-early|place-open-late": 25}
    templates = _catalogue_templates(places, lambda a, b: travel.get(f"{a}|{b}", 5))
    assert templates, templates
    for template in templates:
        ids = [item[0] for item in template]
        # A two-hour visit cannot start inside a venue that shuts at 09:30 after a stop that
        # runs to 09:00 plus 25 minutes of travel; it is dropped rather than squeezed.
        if "place-open-early" in ids and "place-open-late" in ids:
            assert "place-closes-early" not in ids, template
        for place_id, _title, start_text, end_text in template:
            window = _place_window(places[place_id])
            assert window[0] <= _minutes(f"2026-09-22T{start_text}:00+08:00"), template
            assert _minutes(f"2026-09-22T{end_text}:00+08:00") <= window[1], template
