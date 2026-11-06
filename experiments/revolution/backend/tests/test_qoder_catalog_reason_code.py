"""ORD-0101 - "the catalog is empty" and "the catalog was not enough" are different sentences.

`plans/generate` answers 422 with "No feasible plan candidates satisfy the journey
constraints" for both of them. That is literally true in both cases and useless to a
traveller: one means "this destination has no data yet" (nothing they can do), the other means
"there were places and none of the combinations survived" (budget, dates, or picking places -
things they *can* do). The user on the phone saw the first case described as the second.

The fix is additive by design, and the gates below hold both halves:

* the **message is byte-identical**, so the existing assertion in
  `test_qoder_beijing_catalogue.py` that quotes it stays green and nobody has to widen it;
* a new `reason_code` names the condition at the point where the counts are known, with the
  counts beside it;
* an error that is *not* a catalog shortfall must not grow the key - otherwise "the field is
  always there" would be the new kind of noise, and a caller would learn nothing from it.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.db.models import Place
from app.db.session import SessionLocal
from app.main import app
from demo_stage import demo_reset_headers  # noqa: E402  (ORD-0389)

client = TestClient(app)
TZ = timezone(timedelta(hours=8))
EMPTY_CITY = "CN-SHANGHAI"
ONE_STOP_CITY = "CN-ONESTOP"
MESSAGE = "No feasible plan candidates satisfy the journey constraints"


@pytest.fixture()
def _clean_cities(monkeypatch):
    """No rows for either city, and a demo traveler to own the journeys."""

    monkeypatch.setattr("app.main.demo_mode_enabled", lambda: True)
    assert client.post("/api/v1/demo/reset", headers=demo_reset_headers()).status_code == 200
    with SessionLocal() as session:
        session.execute(delete(Place).where(Place.city_code.in_([EMPTY_CITY, ONE_STOP_CITY])))
        session.commit()
    return client.post("/api/v1/demo/reset", headers=demo_reset_headers()).json()["traveler_id"]


def _journey(traveler: str, city_code: str, *, days: int = 2, budget: int = 300_000) -> str:
    start = datetime(2026, 11, 10, 9, 0, tzinfo=TZ)
    created = client.post(
        "/api/v1/journeys",
        json={
            "traveler_id": traveler,
            "title": f"{city_code} shortfall",
            "city_code": city_code,
            "timezone": "Asia/Shanghai",
            "start_at": start.isoformat(),
            "end_at": (start + timedelta(days=days - 1, hours=9)).isoformat(),
            "budget": {"amount_minor": budget, "currency": "CNY"},
            "planning_mode": "balanced",
        },
    )
    assert created.status_code == 201, created.text
    return created.json()["id"]


def _generate(journey_id: str):
    return client.post(f"/api/v1/journeys/{journey_id}/plans/generate", json={})


def _add_place(place_id: str, city_code: str) -> None:
    with SessionLocal() as session:
        session.add(
            Place(
                id=place_id,
                provider_key="mock-city",
                external_id=place_id.upper(),
                city_code=city_code,
                name="One Harbour Museum",
                category="museum",
                latitude=31.24,
                longitude=121.5,
                opening_hours_json=json.dumps({"open": "09:00", "close": "17:00"}),
                tags_json=json.dumps(["culture"]),
                metadata_json=json.dumps({"duration_min": 60, "cost_minor": 3000}),
                updated_at="2026-11-01T00:00:00+08:00",
            )
        )
        session.commit()


def test_an_empty_catalog_says_so_instead_of_borrowing_the_other_refusal(_clean_cities) -> None:
    """`CATALOG_EMPTY`, with the message left exactly as it was."""

    with SessionLocal() as session:
        assert session.query(Place).filter(Place.city_code == EMPTY_CITY).first() is None
    journey_id = _journey(_clean_cities, EMPTY_CITY)
    response = _generate(journey_id)
    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["message"].startswith(MESSAGE), error
    assert error["reason_code"] == "CATALOG_EMPTY", error
    assert error["details"]["places_in_catalog"] == 0, error
    assert error["details"]["city_code"] == EMPTY_CITY, error
    # The name the order forbids as a stand-in for "we have no data here".
    assert error["reason_code"] != "NO_FEASIBLE_PLAN", error


def test_a_catalog_that_is_present_but_too_thin_says_that_instead(_clean_cities) -> None:
    """The distinction has to be reachable in both directions, not one of them by default.

    One museum in the city is a real catalog and cannot fill a day: the same sentence comes
    back, and only the name tells a caller that the destination is *known* but insufficient -
    which is the case a traveller can act on.
    """

    _add_place("place-onestop-museum", ONE_STOP_CITY)
    journey_id = _journey(_clean_cities, ONE_STOP_CITY)
    response = _generate(journey_id)
    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["message"].startswith(MESSAGE), error
    assert error["reason_code"] == "CATALOG_INSUFFICIENT", error
    assert error["details"]["places_in_catalog"] == 1, error
    assert error["reason_code"] != "CATALOG_EMPTY", error


def test_a_refusal_that_is_not_about_the_catalog_grows_no_name(_clean_cities) -> None:
    """`reason_code` is emitted where a raise site knows one, not everywhere.

    Measured on the journey-window rejection, which is a different rule with a different
    sentence: a key present on every error would teach a caller nothing and would let the
    screens treat unrelated refusals as one bucket.
    """

    start = datetime(2026, 11, 10, 9, 0, tzinfo=TZ)
    created = client.post(
        "/api/v1/journeys",
        json={
            "traveler_id": _clean_cities,
            "title": "nine days",
            "city_code": "CNX-DEMO",
            "timezone": "Asia/Shanghai",
            "start_at": start.isoformat(),
            "end_at": (start + timedelta(days=8, hours=9)).isoformat(),
            "budget": {"amount_minor": 300000, "currency": "CNY"},
            "planning_mode": "balanced",
        },
    )
    assert created.status_code == 422, created.text
    error = created.json()["error"]
    assert "reason_code" not in error, error
    assert error["code"] == "V13_JOURNEY_DAYS_OUT_OF_BOUNDS", error


def test_the_message_still_reads_the_same_for_a_city_with_data(_clean_cities) -> None:
    """The refusal that has a name must not have acquired a new sentence.

    This is the half the order insisted on: the existing gate in
    `test_qoder_beijing_catalogue.py` quotes this message, and adding a field must never be an
    excuse to reword it.
    """

    journey_id = _journey(_clean_cities, EMPTY_CITY, budget=300_000)
    body = _generate(journey_id).json()["error"]
    # Byte-identical to what this door answered before the name existed - verified against the
    # live response rather than against this assertion, so rewording cannot slide in sideways.
    assert body["message"] == MESSAGE, body
    assert body["message_key"] == "request.invalid", body
    assert body["code"] == "VALIDATION_ERROR", body
