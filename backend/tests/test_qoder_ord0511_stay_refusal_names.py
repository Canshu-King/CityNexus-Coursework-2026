"""ORD-0511 -- the stay selection door names each of its refusals, and leaks no identifier.

Why the shapes are injected here: two of the three refusals are properties of the planner behind the
door, and nothing in this repo's data set produces a check-in collision or a failed hard gate for a
CN-TIANJIN candidate on demand.  Injecting them at the seam is the honest way to hold the DOOR to its
own contract -- which is what this file judges -- and every injected cell says so in `how`.  The first
cell is not injected: an unknown option id is a thing a traveller's link can actually do to this door.

What each arm holds the code to:

* R1 a request for a candidate this trip does not have answers 404 with its own name, and the body
  carries no identifier a screen could be forced to print;
* R2 a planner conflict reaches the traveller as `STAY_ITINERARY_CONFLICT`, not as the shared
  VALIDATION_ERROR class every bad request on the product already is;
* R3 a candidate that could not become authoritative reaches the traveller as `STAY_NOT_AUTHORITATIVE`
  with the count of failures, not their prose;
* R4 the three names are three names -- a refusal that collapsed back onto one shared class would let a
  screen print "失败" over three different facts, which is the defect this order exists to close.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app import main as app_main
from app.main import app
from app.stay_mobility import StayItineraryConflict

client = TestClient(app, raise_server_exceptions=False)

UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
TZ = timezone(timedelta(hours=8))
START = datetime(2026, 12, 4, 15, 0, tzinfo=TZ)


def _journey(city_code: str = "CN-TIANJIN") -> str:
    traveler = client.post(
        "/api/v1/travelers",
        json={"display_name": "ORD-0511 probe", "locale": "zh-CN", "timezone": "Asia/Shanghai"},
    )
    assert traveler.status_code in (200, 201), traveler.text
    created = client.post(
        "/api/v1/journeys",
        json={
            "traveler_id": traveler.json()["id"],
            "title": "ORD-0511",
            "city_code": city_code,
            "timezone": "Asia/Shanghai",
            "start_at": START.isoformat(),
            "end_at": (START + timedelta(days=2, hours=6)).isoformat(),
            "budget": {"amount_minor": 200000, "currency": "CNY"},
            "planning_mode": "balanced",
        },
    )
    assert created.status_code == 201, created.text
    return created.json()["id"]


def _reason(payload: dict) -> str | None:
    return (payload.get("error") or {}).get("reason_code")


def _identifier_free(error: dict) -> None:
    """Nothing a screen would print may carry an identifier.

    `trace_id` is excluded on purpose: it is this product's own envelope field (the ORD-0332 receipt
    family) and the traveller never reads it -- the arms in the e2e file are what prove no uuid reaches
    rendered text.  `message` and `details` are what a screen is built from, so those two are judged.
    """
    assert not UUID_RE.search(str(error.get("message") or "")), error
    assert not UUID_RE.search(str(error.get("details") or "")), error


def _select(journey_id: str, option_id: str):
    return client.post(f"/api/v1/journeys/{journey_id}/stay-options/{option_id}/select")


def _one_real_option(journey_id: str) -> str:
    """Ask this trip for stays and hand back a candidate it actually holds.

    Needed by the two injected cells: with an id the trip does not have, the door answers "no such
    candidate" and never reaches the planner, which is R1's fact rather than theirs.  Measured the hard
    way -- that is what the first take of this file did and why it read 404 where it meant 422.
    """
    generated = client.post(f"/api/v1/journeys/{journey_id}/stay-options/generate")
    assert generated.status_code == 200, generated.text
    listed = client.get(f"/api/v1/journeys/{journey_id}/stay-options")
    assert listed.status_code == 200, listed.text
    options = listed.json().get("options") or []
    assert options, generated.text
    return str(options[0]["id"])


def test_r1_an_unknown_candidate_is_named_and_carries_no_identifier() -> None:
    journey_id = _journey()
    response = _select(journey_id, str(uuid4()))
    assert response.status_code == 404, response.text
    assert _reason(response.json()) == "STAY_OPTION_UNKNOWN", response.text
    _identifier_free(response.json()["error"])


def test_r2_a_plan_conflict_refuses_under_its_own_name(monkeypatch: pytest.MonkeyPatch) -> None:
    journey_id = _journey()

    def refuse(*_args, **_kwargs):
        raise StayItineraryConflict("STAY_PINNED_CONFLICT:check-in")

    # how: injected at the planner seam; this cell judges the door's answer, not the planner's cause.
    monkeypatch.setattr(app_main, "materialize_stay_option", refuse)
    response = _select(journey_id, _one_real_option(journey_id))
    assert response.status_code == 422, response.text
    assert _reason(response.json()) == "STAY_ITINERARY_CONFLICT", response.text
    _identifier_free(response.json()["error"])


def test_r3_a_stay_that_cannot_become_truth_is_named(monkeypatch: pytest.MonkeyPatch) -> None:
    journey_id = _journey()

    class Gate:
        gate_status = "FAILED"
        plan = None
        failures = ["hard constraint", "another"]

    # how: injected; the door's contract is that a failed gate answers with a name and a count.
    monkeypatch.setattr(app_main, "materialize_stay_option", lambda *_a, **_k: Gate())
    response = _select(journey_id, _one_real_option(journey_id))
    assert response.status_code == 422, response.text
    body = response.json()
    assert _reason(body) == "STAY_NOT_AUTHORITATIVE", body
    assert (body.get("error") or {}).get("details") == {"failure_count": 2}, body


def test_r4_the_three_refusals_are_three_names(monkeypatch: pytest.MonkeyPatch) -> None:
    journey_id = _journey()
    names = set()

    names.add(_reason(_select(journey_id, str(uuid4())).json()))

    held = _one_real_option(journey_id)
    monkeypatch.setattr(
        app_main,
        "materialize_stay_option",
        lambda *_a, **_k: (_ for _ in ()).throw(StayItineraryConflict("x")),
    )
    names.add(_reason(_select(journey_id, held).json()))

    class Gate:
        gate_status = "FAILED"
        plan = None
        failures = ["one"]

    monkeypatch.setattr(app_main, "materialize_stay_option", lambda *_a, **_k: Gate())
    names.add(_reason(_select(journey_id, held).json()))

    assert names == {"STAY_OPTION_UNKNOWN", "STAY_ITINERARY_CONFLICT", "STAY_NOT_AUTHORITATIVE"}, names
