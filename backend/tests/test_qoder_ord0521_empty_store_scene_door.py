"""ORD-0521 - the empty-store scene door, and the counter-examples that keep it honest.

Every arm opens its OWN sqlite file through the product's own `make_engine`, because the claim under
test is "an empty store becomes one walkable trip".  A store emptied through the whole-library reset
door would prove the opposite of what this order delivers, so that door appears nowhere in this
module - not as a fixture, not as a precondition, not as a helper.

The HTTP arms go through `app.main:app` with exactly one thing redirected: which file `get_session`
opens.  The route, its guards, the supply, the plan generator and the selection all run for real, and
every read-back is `sqlite3` against the same file, naming table and rowid.
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import Base
from app.db.session import get_session, make_engine

SCENE_ENV = "CITYNEXUS_DEMO_SCENE_TOKEN"
SCENE_HEADER = "X-Demo-Scene-Token"
ROUTE = "/api/v1/demo/scene/ensure"

FIXTURE_PLACES = 16
FIXTURE_ROWS = 37
SELECTED_ACTIVITY_SQL = (
    "select count(*), sum(case when a.place_id is null and a.city_service_id is null then 1 else 0 end) "
    "from activities a join plan_versions p on p.id = a.plan_version_id where p.status = 'SELECTED'"
)


class Store:
    """One file, opened twice: once as raw sqlite for the read-back, once as the app's session."""

    def __init__(self, path, connection, sessions) -> None:
        self.path = path
        self.connection = connection
        self.sessions = sessions

    def count(self, table: str) -> int:
        return self.connection.execute(f"select count(*) from {table}").fetchone()[0]

    def rows(self, sql: str, params: tuple = ()) -> list[Any]:
        return self.connection.execute(sql, params).fetchall()

    @contextmanager
    def session(self) -> Session:
        with self.sessions() as session:
            yield session


@pytest.fixture()
def store(tmp_path):
    os.environ[SCENE_ENV] = "scene-token-" + uuid.uuid4().hex
    path = tmp_path / "ord0521.db"
    engine = make_engine(f"sqlite:///{path.as_posix()}")
    Base.metadata.create_all(engine)

    from app.main import app

    sessions = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)

    def _override():
        with sessions() as session:
            yield session

    app.dependency_overrides[get_session] = _override
    connection = sqlite3.connect(path)
    try:
        yield Store(str(path), connection, sessions)
    finally:
        connection.close()
        app.dependency_overrides.pop(get_session, None)
        os.environ.pop(SCENE_ENV, None)


@pytest.fixture()
def client(store):
    from app.main import app

    test_client = TestClient(app)
    test_client.store = store
    return test_client


def headers(key: str | None = None) -> dict[str, str]:
    built = {SCENE_HEADER: os.environ[SCENE_ENV]}
    if key is not None:
        built["Idempotency-Key"] = key
    return built


def ensure(client: TestClient, key: str, body: dict | None = None):
    return client.post(ROUTE, headers=headers(key), json=body if body is not None else {})


# --- the door ---------------------------------------------------------------------------------


def test_C1_empty_store_becomes_one_walkable_trip(client):
    assert client.store.count("places") == 0, "the arm must start where no place row exists"
    assert client.store.count("journeys") == 0

    answer = ensure(client, "c1")
    assert answer.status_code == 200, answer.text
    decision = answer.json()

    assert decision["state"] == "READY", decision
    assert decision["plan_version_id"], "a trip with no plan is the defect this order closes"
    assert decision["journey_status"] == "PLANNED", decision["journey_status"]
    assert decision["advance_refused_steps"] == [], decision["advance_refused_steps"]
    assert [step for step in decision["advance"]["steps"] if "refused" in step] == []

    # the supply leg names itself rather than leaving a full catalogue unexplained
    supply = decision["supply"]
    assert supply["supply_leg"].startswith("builtin_fixture:"), supply["supply_leg"]
    assert supply["supported_city_codes"] == ["CN-BEIJING", "CNX-DEMO"], supply
    assert supply["places_held"]["CNX-DEMO"] == 8, supply["places_held"]
    assert supply["rows_installed"] == FIXTURE_ROWS, supply
    assert supply["rows_deleted"] == 0, supply


def test_C2_the_supply_lands_in_the_store_named_by_rowid(client):
    ensure(client, "c2")

    assert client.store.count("places") == FIXTURE_PLACES
    assert client.store.count("journeys") == 1
    place = client.store.rows(
        "select rowid,provider_key,city_code,name from places where city_code='CNX-DEMO' "
        "order by rowid limit 1"
    )
    assert place and place[0][1] == "mock-city", place
    assert place[0][2] == "CNX-DEMO" and place[0][3], place[0]

    journey = client.store.rows(
        "select rowid,city_code,status,active_plan_version_id from journeys"
    )[0]
    assert journey[1] == "CNX-DEMO" and journey[2] == "PLANNED" and journey[3], journey

    # walkable means stops on a schedule, each anchored to a row the store actually holds
    stops, unanchored = client.store.rows(SELECTED_ACTIVITY_SQL)[0]
    assert stops > 0, "the selected plan produced no stop at all"
    assert unanchored == 0, "a stop with neither a place nor a service is the unresolved case"
    dangling = client.store.rows(
        "select count(*) from activities a left join places p on p.id = a.place_id "
        "where a.place_id is not null and p.id is null"
    )[0][0]
    assert dangling == 0, "a stop pointing at a place that is not in the catalogue"


def test_C3_prepare_scenario_after_the_door_has_no_refused_hop(client, monkeypatch):
    """`prepare-scenario` opens `SessionLocal` itself, so this arm redirects that to the same file.

    Not a mock and not an in-process call: both routes are walked over HTTP against the store this
    arm created, which is what "the door's supply carries a later route" means when there is no
    reset in between.
    """

    import app.main as module

    monkeypatch.setattr(module, "SessionLocal", client.store.sessions)
    assert ensure(client, "c3").status_code == 200

    answer = client.post("/api/v1/demo/prepare-scenario")
    assert answer.status_code == 200, answer.text
    prepared = answer.json()
    assert prepared["plan_version_id"], prepared
    assert [step for step in prepared["advance"]["steps"] if "refused" in step] == [], prepared


# --- what the door must not do ----------------------------------------------------------------


def test_C4_no_token_is_a_named_refusal_and_writes_nothing(client):
    answer = client.post(ROUTE, headers={"Idempotency-Key": "c4"}, json={})

    assert answer.status_code == 403, answer.text
    error = answer.json()["error"]
    assert error["reason_code"] == "SCENE_TOKEN_REQUIRED", error
    assert client.store.count("places") == 0
    assert client.store.count("journeys") == 0


def test_C5_a_wrong_token_refuses_in_the_same_shape_and_writes_nothing(client):
    answer = client.post(
        ROUTE, headers={SCENE_HEADER: "not-the-operators-token", "Idempotency-Key": "c5"}, json={}
    )

    assert answer.status_code == 403, answer.text
    error = answer.json()["error"]
    assert error["reason_code"] == "SCENE_TOKEN_INVALID", error
    assert os.environ[SCENE_ENV] not in answer.text, "a refusal must not echo the token it compared"
    assert client.store.count("places") == 0


def test_C6_a_missing_idempotency_key_is_refused_before_any_write(client):
    answer = client.post(ROUTE, headers=headers(), json={})

    assert answer.status_code == 422, answer.text
    assert client.store.count("places") == 0, "an unclaimed request may not fill the catalogue"


def test_C7_demo_mode_off_refuses_by_name_and_touches_nothing(client, monkeypatch):
    import app.main as module

    monkeypatch.setattr(module, "demo_mode_enabled", lambda: False)
    answer = client.post(ROUTE, headers=headers("c7"), json={})

    assert answer.status_code == 403, answer.text
    error = answer.json()["error"]
    assert error["reason_code"] == "DEMO_MODE_REQUIRED", error
    assert client.store.count("places") == 0


def test_C8_an_unsupplied_destination_says_so_readably_instead_of_inventing(client):
    answer = ensure(client, "c8", {"city_code": "CN-TIDY"})

    assert answer.status_code == 409, answer.text
    error = answer.json()["error"]
    assert error["reason_code"] == "DEMO_STATE_SUPPLY_UNAVAILABLE", error
    assert "cannot obtain places" in error["message"], error["message"]
    details = error["details"]
    assert details["failed_hop"] == "places_catalog", details
    assert details["places_held"] == 0, details
    assert "catalog-hydrations" in details["next_hop"], details

    # a named refusal is atomic: the store is left exactly as it was found, and no place was
    # invented for the city this server cannot supply
    assert client.store.count("places") == 0, "the refusal rolled back, it did not half-install"
    assert client.store.count("journeys") == 0
    city_codes = {row[0] for row in client.store.rows("select distinct city_code from places")}
    assert "CN-TIDY" not in city_codes, city_codes


# --- idempotence and non-destruction -----------------------------------------------------------


def test_C9_a_replayed_key_returns_the_first_receipt_and_adds_no_rows(client):
    first = ensure(client, "c9").json()
    before = {t: client.store.count(t) for t in ("journeys", "places", "plan_versions")}

    again = ensure(client, "c9").json()
    after = {t: client.store.count(t) for t in ("journeys", "places", "plan_versions")}

    assert again["idempotency_replay"] is True, again
    assert again["journey_id"] == first["journey_id"], (again, first)
    assert again["plan_version_id"] == first["plan_version_id"], again["plan_version_id"]
    assert after == before, (before, after)


def test_C10_a_second_door_on_a_filled_store_installs_nothing_twice(client):
    ensure(client, "c10")
    second = ensure(client, "c10-second").json()

    assert second["supply"]["rows_installed"] == 0, second["supply"]
    assert second["supply"]["rows_already_present"] == FIXTURE_ROWS, second["supply"]
    assert client.store.count("places") == FIXTURE_PLACES, "a second door must not double the rows"
    assert client.store.count("journeys") == 1


def test_C11_the_door_leaves_rows_it_did_not_create_alone(client):
    """Non-destructive is the claim, so a foreign row is planted first and has to survive."""

    from app.db.models import Journey, Place, Traveler

    with client.store.session() as session:
        # a journey is a child row, so the foreign board brings its own traveler
        session.add(
            Traveler(
                id="00000000-0000-4000-8000-000000000001",
                display_name="Foreign board",
                email="foreign@example.invalid",
                locale="en",
                timezone="Asia/Shanghai",
                created_at="2026-10-01T00:00:00+00:00",
                updated_at="2026-10-01T00:00:00+00:00",
            )
        )
        session.flush()
        foreign = Journey(
            id=str(uuid.uuid4()),
            traveler_id="00000000-0000-4000-8000-000000000001",
            title="Someone else's board",
            city_code="CN-BEIJING",
            timezone="Asia/Shanghai",
            start_at="2026-10-05T00:00:00+00:00",
            end_at="2026-10-06T00:00:00+00:00",
            budget_currency="CNY",
            budget_amount_minor=30000,
            planning_mode="balanced",
            status="DRAFT",
            created_at="2026-10-01T00:00:00+00:00",
            updated_at="2026-10-01T00:00:00+00:00",
        )
        session.add(foreign)
        session.add(
            Place(
                id="place-someone-elses",
                provider_key="other-provider",
                external_id="other-1",
                city_code="CN-BEIJING",
                name="A row this door did not ship",
                category="sight",
                latitude=39.0,
                longitude=116.0,
                opening_hours_json="{}",
                tags_json="[]",
                metadata_json="{}",
                updated_at="2026-10-01T00:00:00+00:00",
            )
        )
        session.commit()

    answer = ensure(client, "c11")
    assert answer.status_code == 200, answer.text

    survivor = client.store.rows(
        "select title,status from journeys where id=?", (str(foreign.id),)
    )
    assert survivor == [("Someone else's board", "DRAFT")], survivor
    kept = client.store.rows("select name from places where id='place-someone-elses'")
    assert kept == [("A row this door did not ship",)], kept
    assert answer.json()["supply"]["rows_deleted"] == 0


# --- the contract and the receipt --------------------------------------------------------------


def test_C15_a_supplied_city_without_a_satisfiable_constraint_refuses_by_name(client):
    """CN-BEIJING is supplied but the shipped constraints are written for CNX-DEMO.

    The honest answer is a named refusal with the hop that broke, not a trip silently built on the
    other city - so this arm pins the state the second half of the order has to move.
    """

    decision = ensure(client, "c15", {"city_code": "CN-BEIJING"}).json()

    assert decision["state"] == "SCENE_INSTALLED_BUT_PLAN_REFUSED", decision["state"]
    assert decision["plan_version_id"] is None, decision["plan_version_id"]
    assert decision["city_code"] == "CN-BEIJING", decision["city_code"]
    refused = decision["advance_refused_steps"]
    assert refused[0]["step"] == "plans/generate", refused
    assert refused[0]["refused"] == "CatalogShortfall", refused
    assert decision["journey_created_by_this_call"] is True, "the residue must name its own row"

    # the supply itself did land for that city - the refusal is about the plan, not the catalogue
    assert decision["supply"]["places_held"]["CN-BEIJING"] == 8, decision["supply"]["places_held"]
    assert client.store.count("places") == FIXTURE_PLACES
    anchored = client.store.rows(
        "select count(*) from journeys where city_code = 'CN-BEIJING' and status = 'DRAFT'"
    )[0][0]
    assert anchored == 1, anchored


def test_C16_the_door_advances_its_own_trip_not_the_newest_row(client):
    """On a board that already holds somebody else's journey the door must not move theirs."""

    from app.db.models import Journey, Traveler

    with client.store.session() as session:
        session.add(
            Traveler(
                id="00000000-0000-4000-8000-000000000009",
                display_name="Another board",
                email="another@example.invalid",
                locale="en",
                timezone="Asia/Shanghai",
                created_at="2099-01-01T00:00:00+00:00",
                updated_at="2099-01-01T00:00:00+00:00",
            )
        )
        session.flush()
        other = Journey(
            id=str(uuid.uuid4()),
            traveler_id="00000000-0000-4000-8000-000000000009",
            title="Newest draft on the board",
            city_code="CNX-DEMO",
            timezone="Asia/Shanghai",
            start_at="2026-11-01T00:00:00+00:00",
            end_at="2026-11-02T00:00:00+00:00",
            budget_currency="CNY",
            budget_amount_minor=10000,
            planning_mode="balanced",
            status="DRAFT",
            created_at="2099-01-01T00:00:00+00:00",
            updated_at="2099-01-01T00:00:00+00:00",
        )
        session.add(other)
        session.commit()
        other_id = str(other.id)

    decision = ensure(client, "c16").json()

    assert decision["state"] == "READY", decision
    assert decision["journey_created_by_this_call"] is False, decision
    assert decision["journey_id"] != other_id, "the door advanced a board it does not own"
    untouched = client.store.rows("select status,active_plan_version_id from journeys where id=?", (other_id,))
    assert untouched == [("DRAFT", None)], untouched


def test_C12_the_spec_carries_the_route_and_both_refusal_shapes(client):
    spec = client.get("/openapi.json").json()
    operation = spec["paths"]["/api/v1/demo/scene/ensure"]["post"]

    assert {"403", "409", "422"} <= set(operation["responses"]), sorted(operation["responses"])
    assert [p for p in operation["parameters"] if p.get("in") == "header"], operation["parameters"]


def test_C13_the_same_key_against_another_body_is_a_conflict_not_a_second_board(client):
    ensure(client, "c13")

    answer = ensure(client, "c13", {"city_code": "CN-BEIJING"})

    assert answer.status_code == 422, answer.text
    error = answer.json()["error"]
    assert error["reason_code"] == "IDEMPOTENCY_KEY_REUSE_CONFLICT", error
    assert client.store.count("journeys") == 1, "a conflicted replay must not create a trip"


def test_C14_the_receipt_is_persisted_under_this_operations_name(client):
    decision = ensure(client, "c14").json()

    stored = client.store.rows(
        "select operation,request_hash,response_status from idempotency_keys where key=?",
        (decision["idempotency_key"],),
    )
    assert stored, decision["idempotency_key"]
    assert stored[0][0] == "demo.scene.ensure", stored[0]
    assert json.loads(stored[0][1])["city_code"] is None, stored[0]
    assert stored[0][2] == 200, stored[0]
