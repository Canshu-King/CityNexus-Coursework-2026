"""ORD-0512 续格 - a stop's own name reaches the plan door, so the map's first branch has a value.

Measured at the authoritative tip `b60418ccbda8937029e6b74f8c990ad06a8d62f8` (read from
`git ls-remote origin refs/heads/mirror/integration-frontend-apple-pass1`): the map asks for a
placed stop's name through `place_name`, and its rule module says that word first and the typed
`title` only when there is no name -- but the door that answers the map,
`backend/app/services.py`'s `activity_schema()`, never set the field, and `app.schemas.Activity`
declared no such attribute.  A key the model does not have is dropped when the model is serialised,
so every activity on the wire carried no `place_name` at all: the first branch of the name rule was
structurally unreachable, and the glass only ever painted the fallback.

What this file pins:

* **the name comes from the row, not from the title** - the staged activity is typed with a title
  that is deliberately NOT the place's name, so `place_name == title` would pass this arm for the
  wrong reason; the divergence itself is asserted first (arm
  `test_the_staged_name_and_title_are_actively_different`), because an arm that cannot tell the two
  apart measures nothing;
* **a stop that was never placed answers None, and the dangling shape cannot even be stored** - an
  activity with no `place_id` gets None rather than the typed title copied over (copying would make
  the field a second spelling of `title` and let the screen's fallback look like a confirmed name),
  and a `place_id` naming a row that is not there is refused by the store's own foreign key, judged
  as a raised `IntegrityError` rather than argued;
* **one vocabulary in the two schema halves that exist** - the pydantic model and the hand contract
  both carry `place_name`, and neither puts it in `required`: a name the store does not have must stay
  representable.  This door's route answers a dict, so FastAPI's generated spec has no `Activity`
  component to compare against (measured, and judged in the arm) -- which makes the hand contract the
  only published promise here, the same drift ORD-0387 and ORD-0395 were opened on;
* **nothing is invented when the store is silent** - the empty half is judged on rows this file owns,
  so no shared row is mutated to manufacture a None.

Nothing here calls `POST /api/v1/demo/reset`, mocks a response, or skips an arm.  Rows go in through
the product's own models and are read back from the door's JSON and from the SQLite file the process
opened (`conftest.RESOLVED_TEST_DATABASE`).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from app.db.models import Activity, Journey, Place, PlanVersion, Traveler
from app.db.session import SessionLocal
from app.main import app

client = TestClient(app)
ROOT = Path(__file__).resolve().parents[2]
CONTRACT = ROOT / "contracts" / "openapi.yaml"
MOMENT = datetime(2031, 6, 1, tzinfo=timezone.utc).isoformat()

# The typed title and the store's name are different words on purpose; see the docstring.
TYPED_TITLE = "午后那个点"
STORE_NAME = "鼓浪屿轮渡码头"
DANGLING_PLACE = f"ord0512-missing-place-{uuid4()}"


def _place(identifier: str) -> str:
    with SessionLocal() as session:
        if session.get(Place, identifier) is None:
            session.add(
                Place(
                    id=identifier,
                    provider_key="ord0512-probe",
                    external_id=identifier,
                    city_code="CN-XIAMEN",
                    name=STORE_NAME,
                    category="ferry",
                    latitude=24.45,
                    longitude=118.07,
                    opening_hours_json=json.dumps({"open": "07:00", "close": "19:00"}),
                    tags_json=json.dumps(["ord0512"]),
                    metadata_json=json.dumps({"cost_minor": 0, "duration_min": 30}),
                    updated_at=MOMENT,
                )
            )
            session.commit()
    return identifier


def _stage(tag: str) -> dict:
    """One traveller, one trip, one plan, and the two activities these arms need on it.

    Two rather than one: a stop that has a Place row, and a stop that was never placed.  The second
    is the honest-null half, and it is the shape a projection is most likely to paper over by
    repeating the typed title into the name field.
    """

    place_id = _place(f"ord0512-place-{tag}")
    with SessionLocal() as session:
        traveler = Traveler(
            id=str(uuid4()), display_name=f"ORD-0512 探针 {tag}", email=None,
            locale="zh-CN", timezone="Asia/Shanghai", created_at=MOMENT, updated_at=MOMENT,
        )
        session.add(traveler)
        session.flush()
        journey = Journey(
            id=str(uuid4()), traveler_id=traveler.id, title=f"沿海一周 {tag}",
            city_code="CN-XIAMEN", timezone="Asia/Shanghai",
            start_at="2031-06-01T00:00:00+00:00", end_at="2031-06-02T00:00:00+00:00",
            budget_amount_minor=120000, budget_currency="CNY", status="PLANNED",
            created_at=MOMENT, updated_at=MOMENT,
        )
        session.add(journey)
        session.flush()
        plan = PlanVersion(
            id=str(uuid4()), journey_id=journey.id, version_no=1, planning_mode="balanced",
            status="ACTIVE", source="INITIAL", total_cost_minor=40000, currency="CNY",
            estimated_duration_min=600, created_at=MOMENT,
        )
        session.add(plan)
        session.flush()
        made = {"traveler": traveler.id, "journey": journey.id, "plan": plan.id, "place": place_id}
        made["activities"] = {}
        for sequence, role, given_place in (
            (1, "named", place_id),
            (2, "no_place", None),
        ):
            identifier = str(uuid4())
            made["activities"][role] = identifier
            session.add(
                Activity(
                    id=identifier, plan_version_id=plan.id,
                    sequence_no=sequence, activity_type="visit", title=TYPED_TITLE,
                    place_id=given_place, city_service_id=None,
                    start_at=f"2031-06-01T0{sequence}:00:00+00:00",
                    end_at=f"2031-06-01T0{sequence}:45:00+00:00", travel_from_prev_min=10,
                    estimated_cost_minor=0, currency="CNY", status="PLANNED", locked=False,
                    metadata_json="{}", created_at=MOMENT, updated_at=MOMENT,
                )
            )
        session.commit()
        return made


_STAGED: dict[str, dict] = {}


def stage(tag: str = "a") -> dict:
    if tag not in _STAGED:
        _STAGED[tag] = _stage(tag)
    return _STAGED[tag]


def _door_activities(plan_id: str) -> dict[str, dict]:
    answer = client.get(f"/api/v1/plans/{plan_id}")
    assert answer.status_code == 200, answer.text
    return {row["id"]: row for row in answer.json()["activities"]}


def test_the_staged_name_and_title_are_actively_different():
    """The premise every other arm rests on, judged on the store rather than assumed.

    If a later fixture made the two words equal, `test_the_store_name_reaches_the_plan_door` would
    stay green while proving nothing, so this arm reads the row it is about to compare against.
    """

    own = stage("premise")
    with SessionLocal() as session:
        row = session.get(Place, own["place"])
        assert row is not None, own["place"]
        stored_name = row.name
    assert stored_name == STORE_NAME, stored_name
    assert stored_name != TYPED_TITLE, (stored_name, TYPED_TITLE)


def test_the_store_name_reaches_the_plan_door():
    """A located stop is answered with the place's own name beside the title the planner typed.

    Both halves are asserted, and they are asserted to be different words: this is the field that
    the map's first branch reads, and before this change the door did not send it at all.
    """

    own = stage("named")
    activities = _door_activities(own["plan"])
    located = [row for row in activities.values() if row.get("place_id") == own["place"]]
    assert len(located) == 1, [row["id"] for row in located]
    assert located[0]["place_name"] == STORE_NAME, located[0]
    assert located[0]["title"] == TYPED_TITLE, located[0]
    assert located[0]["place_name"] != located[0]["title"], located[0]


def test_a_stop_that_was_never_placed_answers_null():
    """A stop with no `place_id` is answered None, and its typed title is left where it is.

    Copying the title into the name field would make the screen's fallback look like a confirmed
    name, which is the failure this order is about -- the glass would then be reporting a fact the
    store never held.
    """

    own = stage("nulls")
    activities = _door_activities(own["plan"])
    unplaced = [row for row in activities.values() if row["id"] == own["activities"]["no_place"]]
    assert len(unplaced) == 1, sorted(activities)
    assert unplaced[0]["place_id"] is None, unplaced[0]
    assert unplaced[0]["place_name"] is None, unplaced[0]
    assert unplaced[0]["title"] == TYPED_TITLE, unplaced[0]


def test_a_dangling_place_id_cannot_be_stored_at_all():
    """The other null shape is refused by the store, so the projection never has to guess about it.

    A `place_id` naming a row that is not there would be the case where `place_name` has to be
    invented, and the honest answer would be impossible to tell from a bug.  Measured rather than
    assumed: the insert is attempted through the ORM, and the store's own refusal is the finding.
    """

    own = stage("dangling")
    with SessionLocal() as session:
        plan = session.get(PlanVersion, own["plan"])
        assert plan is not None, own["plan"]
        session.add(
            Activity(
                id=f"ord0512-activity-dangling-{own['plan'][:8]}", plan_version_id=own["plan"],
                sequence_no=9, activity_type="visit", title=TYPED_TITLE, place_id=DANGLING_PLACE,
                city_service_id=None, start_at="2031-06-01T09:00:00+00:00",
                end_at="2031-06-01T09:45:00+00:00", travel_from_prev_min=10,
                estimated_cost_minor=0, currency="CNY", status="PLANNED", locked=False,
                metadata_json="{}", created_at=MOMENT, updated_at=MOMENT,
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()
    assert DANGLING_PLACE not in [row.get("place_id") for row in _door_activities(own["plan"]).values()]
    assert len(_door_activities(own["plan"])) == 2, sorted(_door_activities(own["plan"]))


def test_the_field_is_one_vocabulary_in_the_two_schema_halves_and_the_runtime():
    """The pydantic model and the hand contract declare the same nullable field.

    Measured first, not assumed: this door's route answers a dict, so FastAPI's own `/openapi.json`
    carries no `Activity` component at all -- the hand contract is therefore the only machine-readable
    promise this surface has, which is exactly why the model half and the contract half have to be
    edited in one commit (ORD-0387 was opened on that drift).  The arm is written in both directions:
    if a later generation attaches the model to a route, the runtime spec is then required to agree
    rather than being allowed to differ quietly.
    """

    from app.schemas import Activity as ActivityModel

    fields = ActivityModel.model_fields
    assert "place_name" in fields, sorted(fields)
    assert fields["place_name"].is_required() is False, "a name the store lacks must stay absent-able"

    spec = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    declared = spec["components"]["schemas"]["Activity"]
    assert "place_name" in declared["properties"], sorted(declared["properties"])
    assert "place_name" not in declared["required"], declared["required"]
    assert declared["properties"]["place_name"]["type"] == ["string", "null"], declared["properties"]["place_name"]

    runtime = client.get("/openapi.json")
    assert runtime.status_code == 200, runtime.text
    components = runtime.json().get("components", {}).get("schemas", {})
    if "Activity" in components:
        assert "place_name" in components["Activity"]["properties"], sorted(components["Activity"]["properties"])
        assert "place_name" not in components["Activity"].get("required", []), components["Activity"].get("required")
    else:
        assert not any(
            name.endswith("Activity") and "place_name" in (body.get("properties") or {})
            for name, body in components.items()
            if isinstance(body, dict)
        ), "an activity-shaped component carries the field while the component named Activity is absent"


def test_the_name_is_read_off_the_row_the_door_already_loaded():
    """The claim is about the payload, and its denominator is the store's own rows.

    Reading the SQLite file the process opened guards the case a green assertion about a door cannot
    see: an activity whose `place_id` was written but whose Place row is absent would otherwise look
    like a projection bug in the opposite direction.
    """

    own = stage("store")
    activities = _door_activities(own["plan"])
    from conftest import RESOLVED_TEST_DATABASE

    database = str(Path(RESOLVED_TEST_DATABASE.replace("sqlite:///", "", 1)))
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
        located = connection.execute(
            "SELECT a.id, p.name FROM activities a LEFT JOIN places p ON p.id = a.place_id "
            "WHERE a.plan_version_id = ?",
            (own["plan"],),
        ).fetchall()
    from_store = {row[0]: row[1] for row in located}
    assert sorted(from_store) == sorted(activities), (sorted(from_store), sorted(activities))
    for activity_id, name_from_row in from_store.items():
        assert activities[activity_id]["place_name"] == name_from_row, (activity_id, name_from_row)
