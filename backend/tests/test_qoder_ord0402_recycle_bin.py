"""ORD-0402 slice 1 - the recycle bin gets its own door, and it says what it knows.

Measured at the authoritative tip `8f4ddfa9feffe4233b48cbf608caf8e1bbb76291`: the server could
soft-delete a trip (ORD-0383) and bring it back (ORD-0399), but nothing listed what was in the bin.
A screen would have had to conclude "that trip was deleted" from it being missing in
`GET /api/v1/journeys`, which is exactly the inference the order forbids.

What the four hard constraints became in code, and which arm proves each:

1. `GET /api/v1/journeys` is untouched.  One arm compares that function's bytes against the same
   function as recorded at the baseline tip in `review/ord0402-readouts/LIST_DOOR_BASELINE_r2.json`,
   so "not one word changed" is a diff, not a promise.  The record is a committed file rather than
   a `git show` because the suite is also run on `git archive` exports, which have no `.git`;
2. every bin row carries `deleted_at` as stored and `bin_state` = IN_RECYCLE_BIN, both read from
   the column rather than inferred - the arms check the value equals what the DELETE wrote and
   that the state word has exactly one possible value;
3. the contract and the generated client move in the same commit - the last arm parses both and
   compares operation ids, because the checked-in `frontend/src/generated/api.ts` was two slices
   behind and that drift is what the ORD-0387 contract-sync arms were built to catch;
4. no pagination, no per-traveller scope, no filter: an arm reads the contract's parameter list
   for this path and asserts it is empty, so the copy cannot promise a filter the door lacks.

A fifth arm covers the seam between this door and whatever renders it (ORD-0411): it reads the
contract and the client generated from it, and deliberately does not read another lane's screen
source, because that source is build-time syntax and its shape is not this door's business.

The two doors partition the store - live rows plus bin rows equal every row - which is the
reading that makes "it is in the bin" and "it is in the list" one fact instead of two guesses.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import yaml
from fastapi.testclient import TestClient

from app.db.models import Traveler
from app.db.session import SessionLocal
from app.main import app

client = TestClient(app, raise_server_exceptions=False)
ROOT = Path(__file__).resolve().parents[2]
CONTRACT = ROOT / "contracts" / "openapi.yaml"
GENERATED = ROOT / "frontend/src/generated/api.ts"
# ORD-0477 re-recorded this pin: the door it guards gained a named `sort` tier by command, so the
# bytes moved.  r2 stays in the tree untouched -- it is the record of what ORD-0402 was told to
# leave alone -- and r3 carries both halves: r2's segment hash as the "before", this tree's bytes
# as the "after".  BASELINE_TIP is the tip the tier was cut onto.
BASELINE_TIP = "be0ffee216c7d24e48652427d290da848a9195cf"
BASELINE_ARTIFACT = ROOT / "review/ord0402-readouts/LIST_DOOR_BASELINE_r3.json"
# The two decorators the byte pin slices between.  They are literals, not patterns: a door whose
# decorator is reflowed is a door that moved, and this arm is meant to say so loudly.
_LIST_DOOR_MARKER_START = '@app.get(' + chr(10) + '    "/api/v1/journeys",' + chr(10) + '    response_model=list[JourneySchema],'
_LIST_DOOR_MARKER_END = '@app.delete("/api/v1/journeys/{journey_id}", status_code=204)'
TZ = timezone(timedelta(hours=8))
START = datetime(2026, 11, 3, 9, 0, tzinfo=TZ)


def _store_path() -> Path:
    from conftest import RESOLVED_TEST_DATABASE

    return Path(RESOLVED_TEST_DATABASE.replace("sqlite:///", "", 1)).expanduser().resolve()


def _traveler() -> str:
    traveler_id = str(uuid4())
    now = datetime.now(timezone.utc).isoformat()
    with SessionLocal() as session:
        session.add(
            Traveler(
                id=traveler_id,
                display_name="ORD-0402 probe",
                email=None,
                locale="zh-CN",
                timezone="Asia/Shanghai",
                created_at=now,
                updated_at=now,
            )
        )
        session.commit()
    return traveler_id


def _journey() -> str:
    created = client.post(
        "/api/v1/journeys",
        json={
            "traveler_id": _traveler(),
            "title": "ORD-0402",
            "city_code": "CN-XIAMEN",
            "timezone": "Asia/Shanghai",
            "start_at": START.isoformat(),
            "end_at": (START + timedelta(days=2, hours=8)).isoformat(),
            "budget": {"amount_minor": 90000, "currency": "CNY"},
            "planning_mode": "balanced",
        },
    )
    assert created.status_code == 201, created.text
    return created.json()["id"]


def _bin_rows() -> list[dict]:
    answer = client.get("/api/v1/recycle-bin/journeys")
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert isinstance(body, list), type(body)
    return list(body)


def _bin_ids() -> list[str]:
    return [str(row["id"]) for row in _bin_rows()]


def _list_ids() -> list[str]:
    answer = client.get("/api/v1/journeys")
    assert answer.status_code == 200, answer.text
    return [str(item["id"]) for item in answer.json()]


def _stored_deleted_at(journey_id: str) -> tuple[str | None, int]:
    with sqlite3.connect(_store_path()) as connection:
        row = connection.execute(
            "SELECT deleted_at, rowid FROM journeys WHERE id = ?", (journey_id,)
        ).fetchone()
    return (row[0] if row else None), (int(row[1]) if row else -1)


def _journey_function(text: str) -> str:
    # the decorator is multi-line since ORD-0477 put the named refusal on it.  The slice is taken from
    # LF-normalised text: a checkout whose `main.py` carries CRLF must not fail with "substring not
    # found" and report the line-ending convention as if it were a change to the door.
    normalized = text.replace("\r\n", "\n")
    # A missing marker IS the finding -- the door's decorator is not the one the record describes --
    # so it is said in those words instead of letting `str.index` raise a bare ValueError that reads
    # like a broken test.
    for marker, name in ((_LIST_DOOR_MARKER_START, "start"), (_LIST_DOOR_MARKER_END, "end")):
        if marker not in normalized:
            raise AssertionError(
                f"the {name} decorator of the trip-list door is not the text "
                f"{BASELINE_ARTIFACT.name} records; the door moved, and the record has to be "
                "regenerated by review/ord0402-readouts/make_list_door_baseline_artifact_r2.py "
                "rather than hand-patched"
            )
    start = normalized.index(_LIST_DOOR_MARKER_START)
    end = normalized.index(_LIST_DOOR_MARKER_END)
    return normalized[start:end]


# ----------------------------------------------------- constraint 1: the other door is intact


def test_the_existing_list_door_was_not_touched_down_to_its_bytes() -> None:
    """Not one word of this door may move without a named order, so this is a byte diff.

    The word that moved it is ORD-0477 (the trip list takes a named `sort` tier); see the constants
    above for what r2 recorded and what r3 records, and `review/ord0402-readouts/
    make_list_door_baseline_artifact_r2.py` for how that record is rebuilt without touching git.

    The baseline is a committed artifact, not a `git show` (ORD-0402 tail): the suite is also run
    on whole-tree `git archive` exports, and a git call there dies with rc 128 - `fatal: not a git
    repository` - which reports the environment and says nothing about the door.  The artifact
    pins the tip and the `main.py` blob it was cut from, so it cannot be quietly re-pointed at a
    later tree, and its own bytes are checked against the hash recorded inside it.
    """

    assert BASELINE_ARTIFACT.is_file(), (
        f"baseline record missing from this tree: {BASELINE_ARTIFACT} - it ships with the arm, so "
        "a partial export that drops review/ cannot judge this door"
    )
    baseline = json.loads(BASELINE_ARTIFACT.read_text(encoding="utf-8"))
    assert baseline["baseline_tip"] == BASELINE_TIP, baseline["baseline_tip"]
    recorded = baseline["segment_lf"]
    assert hashlib.sha256(recorded.encode("utf-8")).hexdigest() == baseline["segment_sha256"], (
        "the baseline record no longer matches the hash inside it: regenerate it, do not hand-patch it"
    )

    # Newline convention is the store's business; the words are the order's.
    mine = _journey_function((ROOT / "backend/app/main.py").read_text(encoding="utf-8"))
    mine = mine.replace("\r\n", "\n")
    assert recorded == mine, "list_journeys moved - the order forbids changing this door"
    # And it still answers only the not-deleted rows, with the binned trip absent.
    journey_id = _journey()
    assert client.delete(f"/api/v1/journeys/{journey_id}").status_code == 204
    assert journey_id not in _list_ids(), "the old door started answering binned rows"
    live = _journey()
    entry = next(item for item in client.get("/api/v1/journeys").json() if item["id"] == live)
    assert "deleted_at" not in entry and "bin_state" not in entry, sorted(entry)


# ------------------------------------------ constraint 2: the bin says so, in the row itself


def test_the_bin_answers_with_the_stored_moment_and_one_named_state() -> None:
    journey_id = _journey()
    assert journey_id not in _bin_ids(), "a live trip appeared in the bin"

    assert client.delete(f"/api/v1/journeys/{journey_id}").status_code == 204
    stored, rowid = _stored_deleted_at(journey_id)
    assert stored is not None, (stored, rowid)

    rows = _bin_rows()
    entry = next((row for row in rows if str(row["id"]) == journey_id), None)
    assert entry is not None, _bin_ids()
    assert entry["bin_state"] == "IN_RECYCLE_BIN", entry
    # the original value, not a reformatted or invented one
    assert datetime.fromisoformat(str(entry["deleted_at"])) == datetime.fromisoformat(stored), (
        entry["deleted_at"],
        stored,
    )
    # the trip part is the same body getJourney answers for that row
    single = client.get(f"/api/v1/journeys/{journey_id}")
    assert single.status_code == 200, single.text
    for field in ("traveler_id", "title", "city_code", "status", "planning_mode"):
        assert entry[field] == single.json()[field], field
    print(
        f"[ord0402][bin] store={_store_path()} journeys.id={journey_id} rowid={rowid} "
        f"bin_state={entry['bin_state']} deleted_at_returned={entry['deleted_at']} "
        f"deleted_at_stored={stored} rows_in_bin={len(rows)}"
    )


def test_live_rows_and_bin_rows_partition_the_whole_store() -> None:
    kept = _journey()
    binned_a = _journey()
    binned_b = _journey()
    for journey_id in (binned_a, binned_b):
        assert client.delete(f"/api/v1/journeys/{journey_id}").status_code == 204

    listed = set(_list_ids())
    binned = set(_bin_ids())
    with sqlite3.connect(_store_path()) as connection:
        total = int(connection.execute("SELECT COUNT(*) FROM journeys").fetchone()[0])
        stored_binned = int(
            connection.execute(
                "SELECT COUNT(*) FROM journeys WHERE deleted_at IS NOT NULL"
            ).fetchone()[0]
        )
    assert {binned_a, binned_b} <= binned, sorted(binned - {binned_a, binned_b})
    assert len(binned) == stored_binned, (len(binned), stored_binned)
    assert listed.isdisjoint(binned), sorted(listed & binned)
    assert len(listed) + len(binned) == total, (len(listed), len(binned), total)
    assert kept in listed and kept not in binned, (kept, sorted(listed)[:3])
    print(
        f"[ord0402][partition] store={_store_path()} list_rows={len(listed)} "
        f"bin_rows={len(binned)} stored_not_null_rows={stored_binned} journeys_total={total}"
    )


def test_the_bin_orders_newest_deletion_first() -> None:
    first = _journey()
    second = _journey()
    for journey_id in (first, second):
        assert client.delete(f"/api/v1/journeys/{journey_id}").status_code == 204
    ids = _bin_ids()
    moments = [str(row["deleted_at"]) for row in _bin_rows()]
    assert moments == sorted(moments, reverse=True), moments
    assert ids.index(second) < ids.index(first), (ids.index(second), ids.index(first))


def test_undeleting_moves_the_row_out_of_the_bin_and_back_into_the_list() -> None:
    journey_id = _journey()
    assert client.delete(f"/api/v1/journeys/{journey_id}").status_code == 204
    assert journey_id in _bin_ids(), "the trip never reached the bin"

    assert client.post(f"/api/v1/journeys/{journey_id}/undelete").status_code == 200
    assert journey_id not in _bin_ids(), "the bin still holds a trip that was brought back"
    assert journey_id in _list_ids(), "the list did not get it back"
    stored, _rowid = _stored_deleted_at(journey_id)
    assert stored is None, stored


# --------------------------------- constraints 3 and 4: the contract, the client, no filters


def _contract_operations(spec: dict) -> set[str]:
    return {
        str(operation["operationId"])
        for path in spec["paths"].values()
        for operation in path.values()
        if isinstance(operation, dict) and "operationId" in operation
    }


def test_the_contract_declares_the_door_with_both_bin_facts_and_no_filter() -> None:
    spec = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    path = spec["paths"]["/recycle-bin/journeys"]
    operation = path["get"]
    assert operation["operationId"] == "listRecycleBinJourneys", sorted(operation)
    assert "parameters" not in path and "parameters" not in operation, (
        "constraint 4: this version has no pagination, no scope and no filter parameter, so the "
        "contract must not advertise one either"
    )
    item = operation["responses"]["200"]["content"]["application/json"]["schema"]["items"]
    assert item == {"$ref": "#/components/schemas/RecycleBinJourney"}, item
    component = spec["components"]["schemas"]["RecycleBinJourney"]
    assert component["allOf"][0] == {"$ref": "#/components/schemas/Journey"}, component
    added = component["allOf"][1]
    assert sorted(added["required"]) == ["bin_state", "deleted_at"], added
    assert added["properties"]["bin_state"]["enum"] == ["IN_RECYCLE_BIN"], added
    assert added["properties"]["deleted_at"]["format"] == "date-time", added


def test_the_generated_client_carries_every_operation_the_contract_declares() -> None:
    """The drift the ORD-0387 arms were built for, measured as a set on both artifacts.

    The checked-in `frontend/src/generated/api.ts` predated ORD-0383 and ORD-0399, so this arm is
    also the guard that the regeneration in this commit did not drop anything: every operationId
    in the contract has to appear as an `operations["..."]` reference in the generated file.
    """

    spec = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    contract_ops = _contract_operations(spec)
    generated = GENERATED.read_text(encoding="utf-8")
    missing = sorted(op for op in contract_ops if f'operations["{op}"]' not in generated)
    assert missing == [], f"{len(missing)} contract operations are absent from api.ts: {missing}"
    for needed in ("listRecycleBinJourneys", "RecycleBinJourney", "IN_RECYCLE_BIN"):
        assert needed in generated, needed
    print(
        f"[ord0402][codegen] contract_operations={len(contract_ops)} "
        f"missing_from_generated_api_ts={len(missing)} "
        f"api_ts_bytes={len(generated.encode('utf-8'))} "
        f"recycle_bin_reference_present={'listRecycleBinJourneys' in generated}"
    )


# ------------------------- the seam, read from the contract and the client generated from it

RECYCLE_BIN_PATH = "/recycle-bin/journeys"
UNDELETE_PATH = "/journeys/{journey_id}/undelete"


def _contract_fields(spec: dict, component: str) -> set[str]:
    """One schema's field names, with its `allOf` bases resolved."""

    node = spec["components"]["schemas"][component]
    fields: set[str] = set()
    for part in node.get("allOf", [node]):
        if "$ref" in part:
            fields |= _contract_fields(spec, part["$ref"].rsplit("/", 1)[1])
        else:
            fields |= set(part.get("properties", {}))
    return fields


def _contract_word(spec: dict, component: str) -> str:
    """The single value `bin_state` may have, read out of the contract."""

    node = spec["components"]["schemas"][component]
    for part in node.get("allOf", [{}]):
        enum = part.get("properties", {}).get("bin_state", {}).get("enum")
        if enum:
            assert len(enum) == 1, enum
            return str(enum[0])
    raise AssertionError(f"{component} declares no bin_state enum: {node}")


def _generated_fields(text: str, component: str, seen: tuple[str, ...] = ()) -> set[str]:
    """The field names openapi-typescript gives a component, intersections followed."""

    assert component not in seen, f"cyclic component reference: {(*seen, component)}"
    head = re.search(rf"^        {component}: ", text, re.MULTILINE)
    assert head, f"{component} is not declared in frontend/src/generated/api.ts"
    brace = text.index("{", head.end())
    depth, index, start = 0, brace, brace + 1
    while True:
        char = text[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                break
        index += 1
    own = set(re.findall(r"^ {12}(\w+)\??:", text[start:index], re.MULTILINE))
    for base in set(re.findall(r'components\["schemas"\]\["(\w+)"\]', text[head.end() : brace])):
        own |= _generated_fields(text, base, (*seen, component))
    return own


def test_the_panels_one_constant_and_field_names_match_the_route_that_was_landed() -> None:
    """ORD-0411 - the seam is judged from the contract and the generation, not from a screen's source.

    What this arm used to do was regex two literals out of
    `frontend/src/features/trash/journeyTrash.ts`: a hand-written row type and a sentence naming the
    state word.  That file then typed its rows from the generated component instead, both literals
    went away, and the arm died on `'NoneType' object has no attribute 'group'` - a message that
    named neither file and read as though the door had broken.

    Reading that file was the wrong instrument in two ways.  A screen's TypeScript is erased at
    build time, so a rename there cannot reach the bundle and cannot be caught from the server side
    at all; and the agreement the arm exists to protect has three copies that do ship -
    `contracts/openapi.yaml`, the `frontend/src/generated/api.ts` generated from it, and the door
    that answers.  This arm now holds those three against each other: both doors declared with the
    method the screen uses, the field names taken from the contract schema and compared with what
    the generated client declares, and a real row checked against both.  What giving up the screen
    file costs is stated plainly: nothing here notices someone editing `journeyTrash.ts`.  The
    commander's own screen walk covers that half, and its readings are cited in the delivery.
    """

    methods: dict[str, set[str]] = {}
    for route in app.routes:
        path = getattr(route, "path", "")
        for method in getattr(route, "methods", None) or ():
            methods.setdefault(path, set()).add(method)
    assert "GET" in methods.get(f"/api/v1{RECYCLE_BIN_PATH}", set()), sorted(methods)
    assert "POST" in methods.get(f"/api/v1{UNDELETE_PATH}", set()), sorted(methods)

    spec = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    assert "get" in spec["paths"][RECYCLE_BIN_PATH], sorted(spec["paths"][RECYCLE_BIN_PATH])
    assert "post" in spec["paths"][UNDELETE_PATH], sorted(spec["paths"][UNDELETE_PATH])
    answered = spec["paths"][RECYCLE_BIN_PATH]["get"]["responses"]["200"]["content"]["application/json"]
    assert answered["schema"]["items"]["$ref"].endswith("/RecycleBinJourney"), answered["schema"]

    declared = _contract_fields(spec, "RecycleBinJourney")
    assert {"id", "deleted_at", "bin_state"} <= declared, sorted(declared)
    generated = GENERATED.read_text(encoding="utf-8")
    in_the_client = _generated_fields(generated, "RecycleBinJourney")
    assert declared == in_the_client, sorted(declared ^ in_the_client)

    journey_id = _journey()
    assert client.delete(f"/api/v1/journeys/{journey_id}").status_code == 204
    row = next(item for item in _bin_rows() if str(item["id"]) == journey_id)
    absent = sorted(declared - set(row))
    assert absent == [], f"the contract promises fields this door does not send: {absent}"
    expected_word = _contract_word(spec, "RecycleBinJourney")
    assert str(row["bin_state"]) == expected_word, (row["bin_state"], expected_word)
    # The raw column value, because a screen treats a null there as "the server did not answer".
    stored, _rowid = _stored_deleted_at(journey_id)
    assert datetime.fromisoformat(str(row["deleted_at"])) == datetime.fromisoformat(stored), (
        row["deleted_at"],
        stored,
    )
    # The other door the contract promises the screen: bringing it back really does answer.
    back = client.post(f"/api/v1/journeys/{journey_id}/undelete")
    assert back.status_code == 200, back.text
    print(
        f"[ord0402][seam] declared_by_contract={len(declared)} declared_in_generated_api_ts="
        f"{len(in_the_client)} missing_from_the_row={len(absent)} expected_bin_state={expected_word} "
        f"undelete_answered={back.status_code} panel_source_file_read=0"
    )
