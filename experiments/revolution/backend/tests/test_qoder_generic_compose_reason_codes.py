"""ORD-0108 - the explicit-POI path has to say *which* of its refusals happened.

Measured shape before this file (lane A's ORD-0102 finding, re-verified by the Commander at
`351dd31`): the AI sheet ends on `POST /journeys/{id}/plans/generate` **with `place_ids`**, and
that branch is a different planner family from the catalogue path.  It refuses for at least
three genuinely different reasons -

* an id the caller invented / that lives in another city (`…PLACE_NOT_FOUND`),
* a place with no price we can vouch for (`…UNKNOWN_COST`),
* a stop that would end after the traveller leaves (`…JOURNEY_WINDOW_EXCEEDED`),

and all three reached the phone as one `VALIDATION_ERROR` whose message was the raw internal
string, folded further by `_generic_compose_variants` into `GENERIC_COMPOSE_INFEASIBLE:<blob>`.
So "北京 0 方案" could not be told apart from "你的预算太紧" - which is the sentence the user
actually saw.

Both halves are gated here, because the order is *additive*:

* the refusal carries a `reason_code` naming the condition plus `details.place_ids` naming the
  places it blames, attached where those ids are actually in hand;
* `message`, `code`, `message_key` and the 422 stay **byte-identical** (the doubled
  `INFEASIBLE:PLACE_NOT_FOUND:…` shape is pinned verbatim, not paraphrased), and a refusal with
  no single cause still grows **no** name - a field present on every error teaches nothing.

The blamed id is deliberately placed **first** in the two catalog-shaped cases: scopes are
prefixes of the requested list, so a tail-only offender would let a narrower scope succeed and
the request would return 201 instead of refusing at all.  That is the real code path, not a
convenience for these gates.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

import app.services as services
from app.db.models import Journey, Place
from app.db.session import SessionLocal
from app.main import app
from demo_stage import demo_reset_headers  # noqa: E402  (ORD-0389)

client = TestClient(app)
TZ = timezone(timedelta(hours=8))
BEIJING = "CN-BEIJING"
# A city this file owns outright: every stop below is one this file inserted, so a green gate
# cannot be the product recognising a hand-authored row somewhere else.
STAGE = "CN-QINGPU"
REAL = "place-qingpu-museum"
SECOND = "place-qingpu-canal"
THIRD = "place-qingpu-teahouse"
INVENTED = "place-definitely-not-in-any-catalog"


@pytest.fixture()
def traveler(monkeypatch) -> str:
    """Simulated stage, one reset, and no rows at all for the city these gates author."""

    monkeypatch.setattr("app.main.demo_mode_enabled", lambda: True)
    reset = client.post("/api/v1/demo/reset", headers=demo_reset_headers())
    assert reset.status_code == 200, reset.text
    with SessionLocal() as session:
        session.execute(delete(Place).where(Place.city_code == STAGE))
        session.commit()
        assert session.scalars(
            select(Place).where(Place.city_code == STAGE)
        ).first() is None, "SETUP 失败：这扇门的舞台城市本来就有数据，那它什么都没测"
    return str(reset.json()["traveler_id"])


def _journey(traveler_id: str, city_code: str, *, hours: int = 30) -> str:
    start = datetime(2026, 11, 10, 9, 0, tzinfo=TZ)
    created = client.post(
        "/api/v1/journeys",
        json={
            "traveler_id": traveler_id,
            "title": f"{city_code} generic compose",
            "city_code": city_code,
            "timezone": "Asia/Shanghai",
            "start_at": start.isoformat(),
            "end_at": (start + timedelta(hours=hours)).isoformat(),
            "budget": {"amount_minor": 150000, "currency": "CNY"},
            "planning_mode": "balanced",
        },
    )
    assert created.status_code == 201, created.text
    return created.json()["id"]


def _generate(journey_id: str, place_ids: list[str]):
    return client.post(
        f"/api/v1/journeys/{journey_id}/plans/generate", json={"place_ids": place_ids}
    )


def _error(response) -> dict:
    assert response.status_code == 422, response.text
    body = response.json()["error"]
    assert body, response.text
    return body


def _place(place_id: str, *, cost: int | None = 3000, duration: int = 60, name: str = "Museum") -> None:
    metadata: dict[str, object] = {"duration_min": duration}
    if cost is not None:
        metadata["cost_minor"] = cost
    with SessionLocal() as session:
        session.add(
            Place(
                id=place_id,
                provider_key="mock-city",
                external_id=place_id.upper(),
                city_code=STAGE,
                name=name,
                category="museum",
                latitude=31.11,
                longitude=121.05,
                opening_hours_json=json.dumps({"open": "09:00", "close": "17:00"}),
                tags_json=json.dumps(["culture"]),
                metadata_json=json.dumps(metadata),
                updated_at="2026-11-01T00:00:00+08:00",
            )
        )
        session.commit()


def _seed_stage() -> None:
    _place(REAL, name="Qingpu Museum")
    _place(SECOND, name="Canal Walk", cost=4000)


# --------------------------------------------------------------------------------------
# the three names, each at the HTTP door a phone actually calls
# --------------------------------------------------------------------------------------
def test_an_invented_place_id_is_named_with_the_id_it_blames(traveler) -> None:
    """`…PLACE_NOT_FOUND` is a fact about *which* id, not a verdict on the traveller."""

    _seed_stage()
    journey_id = _journey(traveler, STAGE)
    error = _error(_generate(journey_id, [INVENTED, REAL, SECOND]))
    assert error["reason_code"] == "PLACES_NOT_IN_CATALOG", error
    assert error["details"]["place_ids"] == [INVENTED], error
    assert error["details"]["city_code"] == STAGE, error
    # An id nobody authored must never be reported as "the catalog has nothing".
    assert error["reason_code"] not in {"CATALOG_EMPTY", "CATALOG_INSUFFICIENT"}, error


def test_a_place_without_a_verifiable_price_is_not_reported_as_a_tight_budget(traveler) -> None:
    """`…UNKNOWN_COST` used to read like "your constraints failed"; nothing was constrained."""

    _seed_stage()
    _place(THIRD, cost=None, name="Teahouse")
    journey_id = _journey(traveler, STAGE)
    error = _error(_generate(journey_id, [THIRD, REAL, SECOND]))
    assert error["reason_code"] == "PLACE_COST_UNVERIFIED", error
    assert error["details"]["place_ids"] == [THIRD], error
    assert error["reason_code"] != "PLACE_OUTSIDE_JOURNEY_WINDOW", error


def test_a_stop_that_would_end_after_the_trip_is_named_with_its_dates(traveler) -> None:
    """`…JOURNEY_WINDOW_EXCEEDED`: too few days is something a caller can act on."""

    _place(REAL, duration=60)
    _place(SECOND, duration=600, cost=4000)
    journey_id = _journey(traveler, STAGE, hours=3)  # 09:00 -> 12:00, one usable window
    error = _error(_generate(journey_id, [REAL, SECOND]))
    assert error["reason_code"] == "PLACE_OUTSIDE_JOURNEY_WINDOW", error
    assert error["details"]["place_ids"] == [SECOND], error
    assert str(error["details"]["journey_end_at"]).startswith("2026-11-10T12:00"), error
    assert str(error["details"]["would_end_at"]) > str(error["details"]["journey_end_at"]), error


# --------------------------------------------------------------------------------------
# the additive half: nothing that already read this way may have changed
# --------------------------------------------------------------------------------------
def test_the_sentence_the_code_and_the_key_are_still_exactly_what_they_were(traveler) -> None:
    """Naming a refusal must not become an excuse to reword it.

    The doubled prefix is the pre-existing shape: the scope loop joins the inner refusals with
    `|` under one `GENERIC_COMPOSE_INFEASIBLE:`.  Pinned verbatim against the live response so
    "we added a field" cannot quietly also become "we rewrote the sentence".
    """

    _seed_stage()
    journey_id = _journey(traveler, STAGE)
    error = _error(_generate(journey_id, [INVENTED, REAL, SECOND]))
    inner = f"GENERIC_COMPOSE_PLACE_NOT_FOUND:{INVENTED}"
    assert error["message"] == f"GENERIC_COMPOSE_INFEASIBLE:{inner}|{inner}", error
    assert error["code"] == "VALIDATION_ERROR", error
    assert error["message_key"] == "request.invalid", error
    assert {"code", "message", "message_key", "trace_id", "details"} <= set(error), error


def test_a_refusal_with_no_single_cause_still_grows_no_name(traveler) -> None:
    """The regression face: the key appears where a raise site knows one, not everywhere.

    Two real stops and a budget of one fen: the deterministic gate refuses, and that genuinely
    *is* "your constraints cannot be satisfied".  Borrowing a catalog name here would be the
    same lie pointing the other way, which is what the third assertion forbids.
    """

    _seed_stage()
    journey_id = _journey(traveler, STAGE)
    with SessionLocal() as session:
        journey = session.get(Journey, journey_id)
        assert journey is not None, "SETUP 失败：行程没落库"
        journey.budget_amount_minor = 1
        session.commit()
    response = _generate(journey_id, [REAL, SECOND])
    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert "reason_code" not in error, error
    assert error["message"].startswith("GENERIC_COMPOSE_INFEASIBLE:"), error
    assert "PLACE" not in error["message"], error


# --------------------------------------------------------------------------------------
# the merge rule inside `_generic_compose_variants`, exercised where it is decided
# --------------------------------------------------------------------------------------
def _refuse_scopes(monkeypatch, traveler_id: str, names: list[str]) -> ValueError:
    """Drive the scope loop with named fakes so the *merge rule* is what is under test.

    Reaching this through HTTP cannot express disagreement (scopes are prefixes, so they share
    their head), and pretending otherwise would have this gate test my fixture instead of the
    code.  Everything else - the message shape, the merged ids - is the real function's.
    """

    _seed_stage()
    journey_id = _journey(traveler_id, STAGE)
    queue = list(names)

    def fake(_session, _journey, _mode, scope):
        raise services.CompositionRefusal(
            f"FAKE:{scope[0]}", reason_code=queue.pop(0), details={"place_ids": list(scope)}
        )

    monkeypatch.setattr(services, "_generic_compose_plans", fake)
    with SessionLocal() as session:
        journey = session.get(Journey, journey_id)
        assert journey is not None, "SETUP 失败：行程没落库"
        with pytest.raises(ValueError) as caught:
            services._generic_compose_variants(
                session, journey, "balanced", [REAL, SECOND, THIRD], 3
            )
        return caught.value


def test_the_aggregate_publishes_a_name_only_when_every_scope_blames_the_same_condition(
    traveler, monkeypatch
) -> None:
    """One name for two different conditions would be a guess dressed up as a fact."""

    mixed = _refuse_scopes(
        monkeypatch,
        traveler,
        [services.PLACE_COST_UNVERIFIED, services.PLACE_OUTSIDE_JOURNEY_WINDOW],
    )
    assert type(mixed) is ValueError, repr(mixed)
    assert not getattr(mixed, "reason_code", None), mixed
    assert str(mixed) == (
        f"GENERIC_COMPOSE_INFEASIBLE:FAKE:{REAL}|FAKE:{REAL}"
    ), mixed


def test_the_aggregate_keeps_the_shared_cause_and_merges_the_blamed_ids(traveler, monkeypatch) -> None:
    """Agreement must survive the loop, or the three names never reach a caller at all."""

    agreed = _refuse_scopes(monkeypatch, traveler, [services.PLACES_NOT_IN_CATALOG] * 2)
    assert isinstance(agreed, services.CompositionRefusal), repr(agreed)
    assert agreed.reason_code == services.PLACES_NOT_IN_CATALOG, agreed
    assert agreed.details["scopes_tried"] == 2, agreed.details
    # Union in first-seen order: the wide scope blames all three, the narrow one adds nothing.
    assert agreed.details["place_ids"] == [REAL, SECOND, THIRD], agreed.details
    assert str(agreed).startswith("GENERIC_COMPOSE_INFEASIBLE:FAKE:"), agreed


def test_the_three_names_are_distinct_values(traveler) -> None:
    """Three names for three conditions: collapsing two into one is the defect, renamed.

    Cheap to check and it is the whole point of the order - and a mutant that reuses one
    constant for two raise sites would otherwise pass every gate above, because each of them
    looks at one condition at a time.
    """

    assert len({services.PLACES_NOT_IN_CATALOG, services.PLACE_COST_UNVERIFIED,
                services.PLACE_OUTSIDE_JOURNEY_WINDOW}) == 3
