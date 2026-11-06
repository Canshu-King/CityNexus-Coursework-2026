"""ORD-0476 slice 1: the trip footprint, tested where it can actually be wrong.

Two places, not one.  A stamp is either durable or it is a caption, so half of this module reads the
store itself (`PRAGMA table_info` / `index_list`) and the other half walks the doors:

1. The duplicate promise has to survive two workers.  The door's own read-then-write catches a second
   tap, but the thing that makes "already stamped" true is the `UNIQUE (journey_id, target_key)`
   index, so one arm drives that index directly, past the door, and one arm checks that the key the
   index sits on cannot be rewritten by a cascade.
2. The target rows are deleted by doors that already exist -- `backend/app/main.py` and
   `backend/app/operations/v9.py` remove activities when a plan version is superseded.  So the
   arms here delete those rows for real and assert what the stamp says afterwards.  This is the
   decision that would otherwise have turned two replan doors into foreign-key failures, which is the
   same class of defect this project already had once with a new table and `demo/reset`.

Nothing here calls `POST /api/v1/demo/reset`, and nothing wipes the shared test store: the reset
ordering is read out of `backend/app/services.py` with `ast`, the way the recycle-bin arms read it.
"""

from __future__ import annotations

import ast
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import yaml
from fastapi.testclient import TestClient
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from app.db.models import Activity, Journey, JourneyCheckin, Place, PlanVersion, Traveler
from app.db.session import SessionLocal
from app.main import app

client = TestClient(app, raise_server_exceptions=False)
ROOT = Path(__file__).resolve().parents[2]
CONTRACT = ROOT / "contracts" / "openapi.yaml"
GENERATED = ROOT / "frontend/src/generated/api.ts"
SERVICES = ROOT / "backend/app/services.py"
MOMENT = datetime.now(timezone.utc).isoformat()


def _store_path() -> Path:
    from conftest import RESOLVED_TEST_DATABASE

    return Path(RESOLVED_TEST_DATABASE.replace("sqlite:///", "", 1)).expanduser().resolve()


def _reason(body: object) -> str | None:
    error = (body or {}).get("error") if isinstance(body, dict) else None
    if isinstance(error, dict) and isinstance(error.get("reason_code"), str):
        return error["reason_code"]
    if isinstance(body, dict) and isinstance(body.get("reason_code"), str):
        return body["reason_code"]
    return None


def _details(body: object) -> dict:
    error = (body or {}).get("error") if isinstance(body, dict) else None
    if isinstance(error, dict) and isinstance(error.get("details"), dict):
        return error["details"]
    return {}


def _place(label: str) -> str:
    identifier = f"ord0476-{label}"
    with SessionLocal() as session:
        if session.get(Place, identifier) is None:
            session.add(
                Place(
                    id=identifier,
                    provider_key="ord0476-probe",
                    external_id=identifier,
                    city_code="CN-XIAMEN",
                    name=f"鼓浪屿轮渡码头 {label}",
                    category="ferry",
                    latitude=24.45,
                    longitude=118.07,
                    opening_hours_json=json.dumps({"open": "07:00", "close": "19:00"}),
                    tags_json=json.dumps(["ord0476"]),
                    metadata_json=json.dumps({"cost_minor": 0, "duration_min": 30}),
                    updated_at=MOMENT,
                )
            )
            session.commit()
    return identifier


def _stage(tag: str = "a") -> dict:
    """One traveller, two trips, one plan version each, one activity on each, and one shared place.

    Built through the product's own models because this order is about the stamp, not about how a
    plan got its rows.  Two journeys are not decoration: `CHECKIN_ACTIVITY_NOT_IN_JOURNEY` cannot be
    told apart from `CHECKIN_ACTIVITY_UNKNOWN` without an activity that really belongs elsewhere.
    """

    # The place row goes in on its own connection BEFORE this session opens one of its own.  The
    # first cut of this helper called `_place()` inside the `with`, so the outer session was already
    # holding a write transaction and the inner connection waited on it until SQLite answered
    # `database is locked` -- a fixture deadlock, not a product one, and the reason that run took
    # minutes instead of seconds.
    place_id = _place(f"{tag}-place")
    with SessionLocal() as session:
        traveler = Traveler(
            id=str(uuid4()), display_name=f"ORD-0476 探针 {tag}", email=None,
            locale="zh-CN", timezone="Asia/Shanghai", created_at=MOMENT, updated_at=MOMENT,
        )
        session.add(traveler)
        session.flush()
        made = {"traveler": traveler.id, "place": place_id}
        for role in ("own", "other"):
            journey = Journey(
                id=str(uuid4()), traveler_id=traveler.id, title=f"沿海一周 {role} {tag}",
                city_code="CN-XIAMEN", timezone="Asia/Shanghai",
                start_at="2031-05-01T00:00:00+00:00", end_at="2031-05-03T00:00:00+00:00",
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
            activity = Activity(
                id=f"ord0476-activity-{tag}-{role}", plan_version_id=plan.id, sequence_no=1,
                activity_type="visit", title=f"stop-{role}", place_id=made["place"],
                city_service_id=None, start_at="2031-05-01T01:00:00+00:00",
                end_at="2031-05-01T02:00:00+00:00", travel_from_prev_min=10,
                estimated_cost_minor=0, currency="CNY", status="PLANNED", locked=False,
                metadata_json="{}", created_at=MOMENT, updated_at=MOMENT,
            )
            session.add(activity)
            # The pointer is set the way every other plan door reads the active version from.
            journey.active_plan_version_id = plan.id
            made[role] = {"journey": journey.id, "plan": plan.id, "activity": activity.id}
        session.commit()
    return made


def _stamp(journey: str, body: dict):
    return client.post(f"/api/v1/journeys/{journey}/checkins", json=body)


def _list(journey: str):
    return client.get(f"/api/v1/journeys/{journey}/checkins")


# ---------------------------------------------------------------- the store, read from the store


def test_the_columns_the_key_and_the_two_scans_are_in_the_store():
    """`sqlite_master` says the container exists; an ORM class alone would have proved nothing."""

    with sqlite3.connect(_store_path()) as connection:
        tables = {
            r[0] for r in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='journey_checkins'"
            )
        }
        assert tables == {"journey_checkins"}, tables
        info = list(connection.execute("PRAGMA table_info(journey_checkins)"))
        columns = {r[1] for r in info}
        assert columns == {
            "id", "journey_id", "activity_id", "place_id", "target_kind", "target_key",
            "stamped_at", "note", "source",
        }, sorted(columns)
        not_null = {r[1] for r in info if r[3] == 1}
        # `note` and the two target ids are the nullable ones, and that is the shape the door relies
        # on: "no note was written" must stay tellable apart from "an empty note was written".  `id`
        # is asserted through `pk` instead, because SQLite reports a PRIMARY KEY column's notnull
        # flag as 0 unless the column also says NOT NULL -- reading only the flag would have called
        # the key column nullable when it is not.
        assert not_null == {
            "journey_id", "target_kind", "target_key", "stamped_at", "source"
        }, sorted(not_null)
        assert {r[1] for r in info if r[5] == 1} == {"id"}, sorted(info)
        fks = list(connection.execute("PRAGMA foreign_key_list(journey_checkins)"))
        by_table = {r[2]: r for r in fks}
        assert by_table["journeys"][6] == "CASCADE", fks
        # The two decisions this order hangs on, read out of the store rather than out of a comment.
        assert by_table["activities"][6] == "SET NULL", fks
        assert by_table["places"][6] == "SET NULL", fks
        index_list = list(connection.execute("PRAGMA index_list(journey_checkins)"))
        columns_of = {
            r[1]: [x[2] for x in connection.execute(f"PRAGMA index_info('{r[1]}')")]
            for r in index_list
        }
    # A table-level UNIQUE becomes an *autoindex* whose `sql` column in `sqlite_master` is NULL, so
    # looking for the word "UNIQUE" there finds nothing about a constraint that is enforced.
    unique = [(r[1], r[4]) for r in index_list if r[3] == "u"]
    assert len(unique) == 1, unique
    key_name = unique[0][0]
    assert key_name.startswith("sqlite_autoindex_journey_checkins_"), key_name
    assert columns_of[key_name] == ["journey_id", "target_key"], columns_of
    assert "idx_journey_checkins_journey_stamped" in columns_of, sorted(columns_of)
    assert columns_of["idx_journey_checkins_journey_stamped"] == ["journey_id", "stamped_at"]


# ---------------------------------------------------------------------- the write door, for real


def test_a_stamp_on_an_activity_comes_back_with_the_targets_own_words():
    stage = _stage("words")
    own = stage["own"]
    answer = _stamp(own["journey"], {"activity_id": own["activity"], "note": "傍晚到的"})
    assert answer.status_code == 201, answer.text
    body = answer.json()
    assert body["target_kind"] == "ACTIVITY", body
    assert body["target_key"] == f"activity:{own['activity']}", body
    assert body["activity_id"] == own["activity"], body
    # The title is the activity's own (`stop-own`), not the place's, and the place leg is not filled
    # in just because the activity happens to point at a row: the stamp named one target.
    assert body["activity_title"] == "stop-own", body
    assert body["activity_type"] == "visit", body
    assert body["place_id"] is None and body["place_name"] is None, body
    assert body["note"] == "傍晚到的", body
    assert body["source"] == "FOREGROUND_WEB", body
    # The door takes no time.  A caller that sends one must not have it stored as the stamp's moment.
    forged = _stamp(
        own["journey"], {"place_id": stage["place"], "stamped_at": "1999-01-01T00:00:00+00:00"}
    )
    assert forged.status_code == 201, forged.text
    assert forged.json()["stamped_at"] != "1999-01-01T00:00:00+00:00", forged.json()
    assert forged.json()["target_key"] == f"place:{stage['place']}", forged.json()

    read = _list(own["journey"])
    assert read.status_code == 200, read.text
    stamps = read.json()
    assert len(stamps) == 2, stamps
    assert [s["target_key"] for s in stamps] == sorted(s["target_key"] for s in stamps), stamps


def test_a_second_tap_names_the_row_that_already_won():
    stage = _stage("dupe")
    own = stage["own"]
    first = _stamp(own["journey"], {"activity_id": own["activity"]})
    assert first.status_code == 201, first.text
    again = _stamp(own["journey"], {"activity_id": own["activity"]})
    assert again.status_code == 409, (again.status_code, again.text)
    assert again.status_code != 500
    assert _reason(again.json()) == "CHECKIN_DUPLICATE_STAMP", again.text
    details = _details(again.json())
    assert details["existing_checkin_id"] == first.json()["id"], details
    assert details["target_key"] == f"activity:{own['activity']}", details
    # A tap that is refused must not have written anything either.
    with SessionLocal() as session:
        rows = session.scalars(
            select(JourneyCheckin).where(JourneyCheckin.journey_id == own["journey"])
        ).all()
    assert len(rows) == 1, [r.target_key for r in rows]
    # The same activity under the *other* trip is a different target row, not a duplicate.
    other = _stamp(stage["other"]["journey"], {"activity_id": stage["other"]["activity"]})
    assert other.status_code == 201, other.text


def test_the_index_refuses_a_second_key_even_when_no_door_looked():
    """The durable half, driven past the door: two rows for one target cannot both be written.

    The door's read catches a replayed tap; this arm is about the case that read cannot see -- a row
    that appears between the check and the insert.  It goes in through the ORM, because the store is
    what has to refuse, and a promise that only holds inside one process is not a promise.
    """

    stage = _stage("race")
    own = stage["own"]
    key = f"activity:{own['activity']}"
    with SessionLocal() as session:
        session.add(
            JourneyCheckin(
                id=str(uuid4()), journey_id=own["journey"], activity_id=own["activity"],
                place_id=None, target_kind="ACTIVITY", target_key=key, stamped_at=MOMENT,
                note=None, source="FOREGROUND_WEB",
            )
        )
        session.commit()
    with SessionLocal() as session:
        try:
            session.add(
                JourneyCheckin(
                    id=str(uuid4()), journey_id=own["journey"], activity_id=own["activity"],
                    place_id=None, target_kind="ACTIVITY", target_key=key,
                    stamped_at=datetime.now(timezone.utc).isoformat(),
                    note="第二次", source="FOREGROUND_WEB",
                )
            )
            session.commit()
            raise AssertionError("the UNIQUE (journey_id, target_key) index did not refuse a duplicate")
        except IntegrityError:
            session.rollback()
        rows = session.scalars(
            select(JourneyCheckin).where(JourneyCheckin.target_key == key)
        ).all()
    assert len(rows) == 1, [r.id for r in rows]
    # And the door says the same named thing about a row it did not write itself.
    answer = _stamp(own["journey"], {"activity_id": own["activity"]})
    assert answer.status_code == 409, (answer.status_code, answer.text)
    assert _reason(answer.json()) == "CHECKIN_DUPLICATE_STAMP", answer.text


def test_a_stamp_has_to_name_exactly_one_target():
    stage = _stage("target")
    own = stage["own"]
    neither = _stamp(own["journey"], {})
    assert neither.status_code == 422, (neither.status_code, neither.text)
    assert _reason(neither.json()) == "CHECKIN_TARGET_REQUIRED", neither.text
    both = _stamp(own["journey"], {"activity_id": own["activity"], "place_id": stage["place"]})
    assert both.status_code == 422, (both.status_code, both.text)
    assert _reason(both.json()) == "CHECKIN_TARGET_AMBIGUOUS", both.text
    assert _list(own["journey"]).json() == [], "a refused tap must leave nothing behind"

    # An id that is only whitespace names nothing, so it is the same fact as sending no field at all.
    # Answering `CHECKIN_ACTIVITY_UNKNOWN` here would describe a target the traveller never chose.
    blank = _stamp(own["journey"], {"activity_id": "   "})
    assert blank.status_code == 422, (blank.status_code, blank.text)
    assert _reason(blank.json()) == "CHECKIN_TARGET_REQUIRED", blank.text
    assert _list(own["journey"]).json() == [], "a refused tap must leave nothing behind"


def test_the_duplicate_is_answered_before_the_target_row_is_asked_about():
    """The order of the two checks is a claim about which row is being spoken of.

    Whether this trip already holds a footprint for a key is a fact about `journey_checkins`, and it
    stays true after the target row is deleted (`ON DELETE SET NULL`).  So a re-tap on a superseded
    stop is told "already stamped here", naming the stamp on file -- not "no such activity", which
    would be a statement about a different table that the traveller's own tap did not raise.
    """

    stage = _stage("order")
    own = stage["own"]
    first = _stamp(own["journey"], {"activity_id": own["activity"]})
    assert first.status_code == 201, first.text
    with SessionLocal() as session:
        session.execute(delete(Activity).where(Activity.id == own["activity"]))
        session.commit()

    again = _stamp(own["journey"], {"activity_id": own["activity"]})
    assert again.status_code == 409, (again.status_code, again.text)
    assert _reason(again.json()) == "CHECKIN_DUPLICATE_STAMP", again.text
    details = _details(again.json())
    assert details["existing_checkin_id"] == first.json()["id"], details
    assert details["target_key"] == f"activity:{own['activity']}", details
    assert len(_list(own["journey"]).json()) == 1, "the refused tap wrote nothing"


def test_an_activity_elsewhere_is_named_apart_from_a_row_that_is_not_there():
    stage = _stage("foreign")
    own, other = stage["own"], stage["other"]

    elsewhere = _stamp(own["journey"], {"activity_id": other["activity"]})
    assert elsewhere.status_code == 409, (elsewhere.status_code, elsewhere.text)
    assert _reason(elsewhere.json()) == "CHECKIN_ACTIVITY_NOT_IN_JOURNEY", elsewhere.text
    details = _details(elsewhere.json())
    assert details["plan_journey_id"] == other["journey"], details
    assert details["asked_journey_id"] == own["journey"], details

    ghost_activity = _stamp(own["journey"], {"activity_id": "ord0476-no-such-activity"})
    assert ghost_activity.status_code == 404, (ghost_activity.status_code, ghost_activity.text)
    assert _reason(ghost_activity.json()) == "CHECKIN_ACTIVITY_UNKNOWN", ghost_activity.text

    ghost_place = _stamp(own["journey"], {"place_id": "ord0476-no-such-place"})
    assert ghost_place.status_code == 404, ghost_place.text
    assert _reason(ghost_place.json()) == "CHECKIN_PLACE_UNKNOWN", ghost_place.text

    for answer in (elsewhere, ghost_activity, ghost_place):
        assert answer.status_code != 500
    assert _list(own["journey"]).json() == [], "a refused tap must leave nothing behind"


# --------------------------------------------------------------------------------- bin, cascade


def test_a_trip_in_the_bin_is_neither_stampable_nor_stamp_readable():
    """The bin state is decided by the column, and one rule holds at both doors.

    The trip is put in the bin through the product's own `DELETE /api/v1/journeys/{id}`, and taken
    out again through `/undelete`, so what is asserted is the state a traveller can actually reach --
    not a column this module poked.
    """

    stage = _stage("bin")
    own = stage["own"]
    stamped = _stamp(own["journey"], {"activity_id": own["activity"]})
    assert stamped.status_code == 201, stamped.text

    dropped = client.delete(f"/api/v1/journeys/{own['journey']}")
    assert dropped.status_code == 204, dropped.text

    write = _stamp(own["journey"], {"place_id": stage["place"]})
    assert write.status_code == 409, (write.status_code, write.text)
    assert _reason(write.json()) == "CHECKIN_JOURNEY_IN_RECYCLE_BIN", write.text
    assert _details(write.json())["deleted_at"], write.text

    read = _list(own["journey"])
    assert read.status_code == 409, (read.status_code, read.text)
    assert _reason(read.json()) == "CHECKIN_JOURNEY_IN_RECYCLE_BIN", read.text
    assert read.status_code != 500

    back = client.post(f"/api/v1/journeys/{own['journey']}/undelete")
    assert back.status_code == 200, back.text
    after = _list(own["journey"])
    assert after.status_code == 200, after.text
    # Being in the bin is not being deleted: the stamp was there before and is still there after.
    assert [s["target_key"] for s in after.json()] == [f"activity:{own['activity']}"], after.json()


def test_a_footprint_outlives_the_rows_it_pointed_at():
    """The measured reason for `ON DELETE SET NULL`, replayed the way the replan path does it.

    `backend/app/operations/v9.py` deletes the activities of a superseded plan version, so this
    arm deletes that plan version and asserts three things: the stamp survives, its link goes to NULL
    through the store rather than through application code, and the door still says *what* was
    stamped from the key it wrote at the time.  A RESTRICT here would have broken that replan door --
    which is the same class of defect this project met once with `demo/reset` and a new table.
    """

    stage = _stage("cascade")
    own = stage["own"]
    stamped = _stamp(own["journey"], {"activity_id": own["activity"]})
    assert stamped.status_code == 201, stamped.text
    stamp_id = stamped.json()["id"]

    with SessionLocal() as session:
        plan = session.get(PlanVersion, own["plan"])
        session.execute(delete(Activity).where(Activity.plan_version_id == plan.id))
        session.delete(plan)
        session.commit()

    with SessionLocal() as session:
        row = session.get(JourneyCheckin, stamp_id)
        assert row is not None, "the stamp was deleted along with its activity"
        assert row.activity_id is None, row.activity_id
        assert row.target_key == f"activity:{own['activity']}", row.target_key
        assert row.target_kind == "ACTIVITY", row.target_kind

    read = _list(own["journey"])
    assert read.status_code == 200, read.text
    bodies = read.json()
    assert len(bodies) == 1, bodies
    assert bodies[0]["activity_id"] is None, bodies[0]
    # The resolved title is null because the row is gone, and it is not filled in with anything.
    assert bodies[0]["activity_title"] is None and bodies[0]["activity_type"] is None, bodies[0]
    # And the freed key is not reusable: the stamp is still holding that target.
    again = _stamp(own["journey"], {"activity_id": own["activity"]})
    assert again.status_code == 409, (again.status_code, again.text)
    assert _reason(again.json()) == "CHECKIN_DUPLICATE_STAMP", again.text


def test_reset_all_empties_the_footprints_before_it_empties_what_they_point_at():
    """Read out of `backend/app/services.py` rather than restated: a list order is a real ordering.

    The list is what `reset_all` walks, and it deletes children first because the schema turns
    foreign keys on.  A stamp references three tables, so its name has to come before all three or
    the reset answers a foreign-key failure having deleted half the store -- the exact shape this
    project already hit once when a new table was left out of that list.
    """

    source = SERVICES.read_text(encoding="utf-8")
    tree = ast.parse(source)
    order = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "reset_all":
            for inner in ast.walk(node):
                if isinstance(inner, ast.Tuple):
                    names = [e.value for e in inner.elts if isinstance(e, ast.Constant)]
                    if "journey_checkins" in names:
                        order = names
                        break
    assert order is not None, "reset_all does not name journey_checkins at all"
    index = order.index("journey_checkins")
    for parent in ("journeys", "activities", "places"):
        assert parent in order, parent
        assert index < order.index(parent), (
            "journey_checkins would be deleted after", parent)


def test_the_contract_declares_both_doors_and_no_control_they_lack():
    paths = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))["paths"]
    node = paths["/journeys/{journey_id}/checkins"]
    listed, created = node["get"], node["post"]
    assert [p["$ref"].rsplit("/", 1)[-1] for p in node["parameters"]] == ["JourneyId"], node
    assert listed["operationId"] == "listJourneyCheckins", listed
    assert created["operationId"] == "createJourneyCheckin", created
    # Every named refusal the doors raise is in the contract, by name, so a screen may say it.
    raised = {
        "CHECKIN_JOURNEY_IN_RECYCLE_BIN", "CHECKIN_ACTIVITY_NOT_IN_JOURNEY",
        "CHECKIN_DUPLICATE_STAMP", "CHECKIN_ACTIVITY_UNKNOWN", "CHECKIN_PLACE_UNKNOWN",
        "CHECKIN_TARGET_REQUIRED", "CHECKIN_TARGET_AMBIGUOUS",
    }
    written = json.dumps(node, ensure_ascii=False)
    for name in sorted(raised):
        assert name in written, name
    assert sorted(created["responses"]) == ["201", "404", "409", "422"], sorted(created["responses"])
    # CMDR 01:24 裁①, applied to this one gate: the read door owes `200` and `409` by name, and any other
    # code documented here must be named in `named_expansion_get` by the very order that documents it. An
    # unnamed add is still red, a promised code that disappeared is red on its own line, and a code that is
    # named but never documented is red too, so the whitelist cannot rot into a list of intentions.
    promised_get = {"200", "409"}
    named_expansion_get: set[str] = set()  # empty as shipped: nothing else is promised on this door
    seen_get = set(listed["responses"])
    assert promised_get <= seen_get, ("a response ORD-0476 promised is gone from the read door",
                                      sorted(promised_get - seen_get), listed["responses"])
    assert seen_get - promised_get <= named_expansion_get, (
        "the read door documents a code this arm was never told about - name it in "
        "named_expansion_get in the same order that documents it",
        sorted(seen_get - promised_get - named_expansion_get))
    assert named_expansion_get <= seen_get, ("a code is whitelisted but not documented",
                                             sorted(named_expansion_get - seen_get))

    schemas = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))["components"]["schemas"]
    create = schemas["JourneyCheckinCreate"]
    # The door takes no time and no target-kind from a caller: those are the server's to decide.
    assert sorted(create["properties"]) == ["activity_id", "note", "place_id"], create
    read_shape = schemas["JourneyCheckin"]
    assert read_shape["properties"]["target_kind"]["enum"] == ["ACTIVITY", "PLACE"], read_shape
    assert read_shape["properties"]["source"]["enum"] == ["FOREGROUND_WEB"], read_shape
    for nullable in ("activity_id", "place_id", "note", "activity_title", "place_name"):
        assert read_shape["properties"][nullable]["type"] == ["string", "null"], nullable

    # Same generation, and the generated client is read as text because it is a type file.
    generated = GENERATED.read_text(encoding="utf-8")
    assert '"/journeys/{journey_id}/checkins": {' in generated, "generated/api.ts is stale"
    for component in ("JourneyCheckin", "JourneyCheckinCreate"):
        assert f"        {component}: {{" in generated, component
    for operation in ("listJourneyCheckins", "createJourneyCheckin"):
        assert f"    {operation}: {{" in generated, operation
