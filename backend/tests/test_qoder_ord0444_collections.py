"""ORD-0444 -- the collection container: the 「收藏」 exit of the POI capture chain gets a shelf.

Measured at the authoritative tip `7399f59ccdb2dfe3cdf04477d4e4ffcc077e214a`: the product had no
collection object of any kind.  `favorite` and 收藏 hit 0 times across `backend/app`, `frontend/src`
and `contracts`, and every `collection` hit was GeoJSON `FeatureCollection` or prose
(`frontend/src/i18n/mapPanelCopy.ts`, `backend/app/main.py`).  So the three-exit capture
chain (加进计划 / 收藏 / 新建计划) had two landings and no third one, and 「收藏」 could not be
built later on top of anything.

This order cuts the container only: two tables, three doors, one read-only list screen.
What each arm below proves, in the order the file states it:

1. the two tables and the durability index are actually in the store this process writes, read out
   of `sqlite_master` rather than inferred from the ORM having a class;
2. `POST /api/v1/collections` answers a shelf whose `item_count` 0 and `items` [] come from the
   read, and the name comes back byte-for-byte including CJK;
3. adding a place resolves that place from `places` -- name, category, city code, coordinate and
   coordinate system are the place's own values, and `note` passes through with `None` kept as
   `None` rather than folded into `""`;
4. a second read of the list door shows the row on the server, so the screen never has to hold
   client state to know a place is on a shelf; `item_count` equals the length of the `items` array
   it ships beside;
5-7. the counter-cases the order asked for, each named and none a 500: an unknown shelf is
   `404 COLLECTION_FOLDER_NOT_FOUND`, a shelf id that is not a uuid-shaped string is *still* that
   same 404 (the path parameter is a plain string, so a wrong id reaches the door instead of a
   transport-level 422), and an unknown place is `404 PLACE_NOT_FOUND`;
8. a duplicate is `409 PLACE_ALREADY_IN_COLLECTION`, and `details` names the row that already won;
9. absence is a list: `GET /api/v1/collections` answers 200 and never 404, and the contract
   declares no path parameter on it -- 「还没有收藏」 is a state the door can actually express;
10-11. `contracts/openapi.yaml` and `frontend/src/generated/api.ts` move in this same commit, and
   the three doors carry no filter, page or sort parameter, so the copy cannot promise a control
   the door lacks;
12. a tripwire on the shape, not a claim of completion: `places` has no address column and no
    address-shaped metadata key, so `CollectionItem` has no `address`.  If a later order adds the
    column, this arm goes red and tells that order to put the field in the shape as well.

Arms 2, 4 and 10 read the store the `pytest` process declares for itself
(`backend/tests/conftest.py`), and nothing here calls `POST /api/v1/demo/reset`.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.models import Place, Traveler
from app.db.session import SessionLocal
from app.main import app

client = TestClient(app, raise_server_exceptions=False)
ROOT = Path(__file__).resolve().parents[2]
CONTRACT = ROOT / "contracts" / "openapi.yaml"
GENERATED = ROOT / "frontend/src/generated/api.ts"
TZ = timezone(timedelta(hours=8))


def _store_path() -> Path:
    from conftest import RESOLVED_TEST_DATABASE

    return Path(RESOLVED_TEST_DATABASE.replace("sqlite:///", "", 1)).expanduser().resolve()


def _place(label: str) -> str:
    """A real `places` row, written the way the seeder writes one, and its id handed back.

    It goes in through the product's own model rather than through a provider call, because this
    order is about the shelf and not about where a place came from.  `coordinate_system` is left to
    the column's server default (WGS84), which is what `seed_demo` does too.
    """

    identifier = f"ord0444-{label}"
    now = datetime.now(timezone.utc).isoformat()
    with SessionLocal() as session:
        if session.get(Place, identifier) is None:
            session.add(
                Place(
                    id=identifier,
                    provider_key="ord0444-probe",
                    external_id=identifier,
                    city_code="CNX-DEMO",
                    name=f"收藏探针 {label}",
                    category="museum",
                    latitude=24.4792,
                    longitude=118.3973,
                    opening_hours_json=json.dumps({"open": "09:00", "close": "17:00"}),
                    tags_json=json.dumps(["ord0444"]),
                    metadata_json=json.dumps({"cost_minor": 0, "duration_min": 60}),
                    updated_at=now,
                )
            )
            session.commit()
    return identifier


def _traveller(label: str) -> str:
    """A real `travelers` row, because 成员归属 has to point at something the store holds.

    Written through the product's own model, as `_place` does.  `locale` and `timezone` take the
    values `seed_demo` gives a traveller, so nothing here depends on a column this order does not
    own.
    """

    identifier = f"ord0444-traveller-{label}"
    now = datetime.now(timezone.utc).isoformat()
    with SessionLocal() as session:
        if session.get(Traveler, identifier) is None:
            session.add(
                Traveler(
                    id=identifier,
                    display_name=f"收藏探针 {label}",
                    email=None,
                    locale="zh-CN",
                    timezone="Asia/Shanghai",
                    created_at=now,
                    updated_at=now,
                )
            )
            session.commit()
    return identifier


def _owner(label: str = "a") -> str:
    return _traveller(label)


def _shelf(name: str, owner: str | None = None) -> dict:
    owner = owner or _owner()
    created = client.post(
        "/api/v1/collections", json={"name": name, "owner_traveler_id": owner}
    )
    assert created.status_code == 201, created.text
    body = created.json()
    # The answer carries the归属 on the row, not the request's copy of it.
    assert body["owner_traveler_id"] == owner, body
    return body


def _shelves(owner: str | None = None) -> list[dict]:
    answer = client.get(
        "/api/v1/collections", params={"traveler_id": owner or _owner()}
    )
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert isinstance(body, list), type(body)
    return body


def _find_shelf(shelf_id: str) -> dict | None:
    return next((item for item in _shelves() if item["id"] == shelf_id), None)


def _reason(body: object) -> str | None:
    error = (body or {}).get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        code = error.get("reason_code")
        if isinstance(code, str) and code:
            return code
    if isinstance(body, dict):
        code = body.get("reason_code")
        if isinstance(code, str) and code:
            return code
    return None


def _details(body: object) -> dict:
    error = (body or {}).get("error") if isinstance(body, dict) else None
    if isinstance(error, dict) and isinstance(error.get("details"), dict):
        return error["details"]
    if isinstance(body, dict) and isinstance(body.get("details"), dict):
        return body["details"]
    return {}


def _contract() -> dict:
    return yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))


def test_the_two_tables_and_the_durability_index_are_in_the_store():
    """`sqlite_master` says the container exists; an ORM class alone would not have proved it."""

    with sqlite3.connect(_store_path()) as connection:
        names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
                "('collection_folders','collection_items')"
            )
        }
        assert names == {"collection_folders", "collection_items"}, names
        folder_columns = {row[1] for row in connection.execute("PRAGMA table_info(collection_folders)")}
        item_columns = {row[1] for row in connection.execute("PRAGMA table_info(collection_items)")}
        assert folder_columns == {
            "id",
            "name",
            "owner_traveler_id",
            "created_at",
            "updated_at",
        }, sorted(folder_columns)
        assert item_columns == {"id", "folder_id", "place_id", "note", "added_at"}, sorted(item_columns)
        # 成员归属 is not a caption someone hoped would be filled in: `notnull` = 1 is the column
        # flag the store enforces (`PRAGMA table_info`'s fields are cid, name, type, notnull,
        # dflt_value, pk -- the type is NOT where a reference is recorded), and the reference itself
        # is only readable from `foreign_key_list`.
        owner_column = next(r for r in connection.execute("PRAGMA table_info(collection_folders)")
                            if r[1] == "owner_traveler_id")
        assert owner_column[3] == 1, owner_column
        owner_fks = [
            r for r in connection.execute("PRAGMA foreign_key_list(collection_folders)")
            if r[3] == "owner_traveler_id"
        ]
        assert len(owner_fks) == 1, owner_fks
        # (id, seq, table, from, to, on_update, on_delete, match)
        assert owner_fks[0][2] == "travelers" and owner_fks[0][4] == "id", owner_fks
        # NO ACTION, not CASCADE: deleting a traveller must not silently take the shelves -- and the
        # door that writes the row refuses an id nothing carries before one can exist to strand.
        assert owner_fks[0][6] == "NO ACTION", owner_fks
        index_list = list(
            connection.execute("PRAGMA index_list(collection_items)")
        )
        columns_of = {
            row[1]: [r[2] for r in connection.execute(f"PRAGMA index_info('{row[1]}')")]
            for row in index_list
        }
    # The duplicate refusal is a table constraint, and SQLite implements a table-level UNIQUE as
    # an *autoindex* whose `sql` column in `sqlite_master` is NULL -- so reading `sql` for the word
    # "UNIQUE" finds nothing and would have said "no constraint" about a constraint that is there.
    # `PRAGMA index_list` is the instrument that reports it: `origin` = 'u' is a UNIQUE constraint,
    # as distinct from 'pk' and 'c', and `unique` = 1 is the flag the planner enforces on.
    # The generated name's trailing digit is SQLite's numbering of the table's constraints (`_1` is
    # the PRIMARY KEY on `id` here), so the arm pins the origin and the columns it covers, not the
    # digit.
    unique_constraints = [
        (name, columns_of[name])
        for (_seq, name, _u, origin, _partial) in index_list
        if origin == "u"
    ]
    assert len(unique_constraints) == 1, unique_constraints
    constraint_name, constraint_columns = unique_constraints[0]
    assert constraint_name.startswith("sqlite_autoindex_collection_items_"), constraint_name
    assert constraint_columns == ["folder_id", "place_id"], unique_constraints
    assert "idx_collection_items_folder_added" in columns_of, sorted(columns_of)
    assert columns_of["idx_collection_items_folder_added"] == ["folder_id", "added_at"], columns_of
    # The shelf list's only read is by owner, oldest first, so that scan is pinned the same way:
    # an index on a column no read uses would have been the decoration it looks like.
    folder_indexes = list(connection.execute("PRAGMA index_list(collection_folders)"))
    folder_columns_of = {
        row[1]: [r[2] for r in connection.execute(f"PRAGMA index_info('{row[1]}')")]
        for row in folder_indexes
    }
    assert "idx_collection_folders_owner" in folder_columns_of, sorted(folder_columns_of)
    assert folder_columns_of["idx_collection_folders_owner"] == [
        "owner_traveler_id",
        "created_at",
    ], folder_columns_of


def test_create_shelf_reports_zero_read_from_the_store_not_a_typed_zero():
    shelf = _shelf("周末博物馆")
    assert shelf["name"] == "周末博物馆", shelf
    assert shelf["item_count"] == 0, shelf
    assert shelf["items"] == [], shelf
    assert shelf["created_at"] and shelf["updated_at"], shelf
    # `updated_at` is written equal to `created_at` and then left alone -- adding a row writes
    # `collection_items`, not this row.  A door that bumped it would create a second account of
    # the shelf next to `created_at`.
    assert shelf["updated_at"] == shelf["created_at"], shelf
    found = _find_shelf(shelf["id"])
    assert found is not None, "the shelf the create door answered is not in the list door"
    assert found["item_count"] == len(found["items"]), found


def test_adding_a_place_answers_the_place_row_itself():
    shelf = _shelf("厦门收藏")
    place_id = _place("resolve")
    added = client.post(
        f"/api/v1/collections/{shelf['id']}/items",
        json={"place_id": place_id, "note": "闭馆前半小时停止入场"},
    )
    assert added.status_code == 201, added.text
    row = added.json()
    assert row["folder_id"] == shelf["id"], row
    assert row["place_id"] == place_id, row
    assert row["note"] == "闭馆前半小时停止入场", row
    with SessionLocal() as session:
        place = session.get(Place, place_id)
        expected = {
            "name": place.name,
            "category": place.category,
            "city_code": place.city_code,
            "provider_key": place.provider_key,
            "coordinate_system": place.coordinate_system,
            "location": {"latitude": place.latitude, "longitude": place.longitude},
        }
    # Every place-shaped field is the place's own value, resolved at answer time -- the row
    # stores a reference, not a copy, so a shelf cannot show a name the store no longer holds.
    for field, value in expected.items():
        assert row[field] == value, (field, row[field], value)


def test_a_second_read_shows_the_row_on_the_server():
    """客户端本地态不算：the row is visible to a fresh read, in the shelf's own answer."""

    shelf = _shelf("再读一次")
    place_id = _place("reread")
    assert client.post(
        f"/api/v1/collections/{shelf['id']}/items", json={"place_id": place_id}
    ).status_code == 201
    stored = _find_shelf(shelf["id"])
    assert stored is not None, _shelves()
    assert stored["item_count"] == 1, stored
    assert len(stored["items"]) == stored["item_count"], stored
    assert [row["place_id"] for row in stored["items"]] == [place_id], stored
    # `note` was never written here, so the answer is null -- "no note was written" is a fact the
    # screen has to be able to tell apart from "an empty note".
    assert stored["items"][0]["note"] is None, stored["items"][0]


def test_unknown_shelf_is_named_and_is_not_a_five_hundred():
    missing = "00000000-0000-4000-8000-000000000000"
    answer = client.post(
        f"/api/v1/collections/{missing}/items", json={"place_id": _place("no-shelf")}
    )
    assert answer.status_code == 404, answer.text
    assert answer.status_code != 500
    assert _reason(answer.json()) == "COLLECTION_FOLDER_NOT_FOUND", answer.text
    assert _details(answer.json())["folder_id"] == missing, answer.text


def test_a_shelf_id_that_is_not_uuid_shape_reaches_the_door_and_is_still_named():
    """The path parameter is a plain string, so a wrong id gets a named 404 and not a parse error.

    Declaring `format: uuid` here would have answered this same tap with 422
    `validation.not_an_id`, which names the traveller's typing and not the missing shelf.
    """

    answer = client.post(
        "/api/v1/collections/not-a-uuid-shelf/items", json={"place_id": _place("odd-shelf")}
    )
    assert answer.status_code == 404, answer.text
    assert _reason(answer.json()) == "COLLECTION_FOLDER_NOT_FOUND", answer.text


def test_unknown_place_is_named_apart_from_a_missing_shelf():
    """Two different 404s, because they are two different failures for a traveller to hear about."""

    shelf = _shelf("缺地点")
    answer = client.post(
        f"/api/v1/collections/{shelf['id']}/items", json={"place_id": "ord0444-does-not-exist"}
    )
    assert answer.status_code == 404, answer.text
    assert _reason(answer.json()) == "PLACE_NOT_FOUND", answer.text
    details = _details(answer.json())
    assert details["place_id"] == "ord0444-does-not-exist", details
    assert details["folder_id"] == shelf["id"], details
    # The refusal wrote nothing: the shelf is still as the create door left it.
    unchanged = _find_shelf(shelf["id"])
    assert unchanged["item_count"] == 0, unchanged


def test_the_second_add_of_one_place_names_the_row_that_already_won():
    shelf = _shelf("重复一次")
    place_id = _place("twice")
    first = client.post(
        f"/api/v1/collections/{shelf['id']}/items",
        json={"place_id": place_id, "note": "第一次"},
    )
    assert first.status_code == 201, first.text
    second = client.post(
        f"/api/v1/collections/{shelf['id']}/items",
        json={"place_id": place_id, "note": "第二次"},
    )
    assert second.status_code == 409, second.text
    assert _reason(second.json()) == "PLACE_ALREADY_IN_COLLECTION", second.text
    details = _details(second.json())
    assert details["existing_item_id"] == first.json()["id"], details
    assert details["existing_added_at"] == first.json()["added_at"], details
    # And the shelf still holds exactly one row -- the loser wrote nothing.
    stored = _find_shelf(shelf["id"])
    assert stored["item_count"] == 1, stored
    assert [row["note"] for row in stored["items"]] == ["第一次"], stored


def test_absence_is_a_list_and_the_list_door_has_no_way_to_404():
    """「还没有收藏」 has to be expressible, so the door cannot answer it with a 404.

    A traveller who owns nothing is a real state, and it reads as the empty list.  The door does
    require an owner to scope by, and omitting it is a validation answer (422) -- never a 404 and
    never a silent read of everyone's shelves.
    """

    answer = client.get(
        "/api/v1/collections", params={"traveler_id": _owner("nobody-here")}
    )
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert isinstance(body, list), type(body)
    assert body == [], "a traveller that never created a shelf must read no rows"
    for shelf in _shelves():
        assert shelf["item_count"] == len(shelf["items"]), shelf
    unscoped = client.get("/api/v1/collections")
    assert unscoped.status_code == 422, unscoped.status_code
    assert unscoped.status_code != 404
    paths = _contract()["paths"]
    assert paths["/collections"].get("parameters") is None, paths["/collections"].keys()
    assert "get" in paths["/collections"] and "post" in paths["/collections"], paths["/collections"].keys()


def test_a_shelf_is_answered_to_its_owner_and_to_nobody_else():
    """成员归属 is a scope, not a caption: the row must not surface under another id."""

    mine = _shelf("只属于甲", owner=_owner("a"))
    answer_to_owner = client.get("/api/v1/collections", params={"traveler_id": _owner("a")})
    assert answer_to_owner.status_code == 200, answer_to_owner.text
    ids_owner = [item["id"] for item in answer_to_owner.json()]
    assert mine["id"] in ids_owner, ids_owner
    answer_to_other = client.get("/api/v1/collections", params={"traveler_id": _owner("b")})
    assert answer_to_other.status_code == 200, answer_to_other.text
    ids_other = [item["id"] for item in answer_to_other.json()]
    assert mine["id"] not in ids_other, ids_other
    # And the rows are scoped with the shelf: the other traveller reads no count and no item either.
    for item in answer_to_other.json():
        assert item["owner_traveler_id"] == _owner("b"), item
    store_owner = sqlite3.connect(_store_path()).execute(
        "SELECT owner_traveler_id FROM collection_folders WHERE id = ?", (mine["id"],)
    ).fetchone()
    assert store_owner == (_owner("a"),), store_owner


def test_a_shelf_for_an_id_no_traveller_carries_is_named_and_is_not_a_five_hundred():
    """The归属 column is validated, so no orphan shelf can exist for a scoped read to miss."""

    answer = client.post(
        "/api/v1/collections",
        json={"name": "无人认领", "owner_traveler_id": "ord0444-no-such-traveller"},
    )
    assert answer.status_code == 404, (answer.status_code, answer.text)
    assert answer.status_code != 500
    assert _reason(answer.json()) == "MEMBER_TRAVELER_UNKNOWN", answer.text
    assert _details(answer.json()).get("field") == "traveler_id", answer.text
    ids = [item["id"] for item in _shelves()]
    assert all(item["owner_traveler_id"] == _owner() for item in _shelves()), ids


def test_the_contract_declares_three_doors_and_no_control_they_lack():
    paths = _contract()["paths"]
    listed = paths["/collections"]["get"]
    created = paths["/collections"]["post"]
    added = paths["/collections/{folder_id}/items"]["post"]
    assert listed["operationId"] == "listCollections", listed
    assert created["operationId"] == "createCollection", created
    assert added["operationId"] == "addCollectionItem", added
    # One control the list door does have, declared: the owner it scopes by.  Nothing else -- no
    # pagination and no sort -- may ride in on that parameter (the same arm shape ORD-0402 grew).
    listed_params = listed.get("parameters") or []
    assert [p["name"] for p in listed_params if isinstance(p, dict) and "name" in p] == [
        "traveler_id"
    ], listed_params
    scope = listed_params[0]
    assert scope["in"] == "query" and scope["required"] is True, scope
    # The create door takes its归属 in the body and no query parameter at all.
    assert created.get("parameters") is None, ("createCollection", sorted(created))
    for name, door in (("listCollections", listed), ("createCollection", created)):
        assert "query" not in json.dumps(door.get("requestBody", {})), name
    for forbidden in ("page", "limit", "offset", "sort", "order"):
        assert forbidden not in json.dumps(listed_params), forbidden
    assert "owner_traveler_id" in json.dumps(
        _contract()["components"]["schemas"]["CollectionFolderCreate"]
    ), "the create shape must carry the归属 or the column has no way in"
    added_item = paths["/collections/{folder_id}/items"]
    assert [p["$ref"].rsplit("/", 1)[-1] for p in added_item["parameters"]] == ["CollectionFolderId"], added_item["parameters"]
    folder_id = _contract()["components"]["parameters"]["CollectionFolderId"]
    assert folder_id["name"] == "folder_id" and folder_id["in"] == "path", folder_id
    # A uuid-typed parameter would turn a missing shelf into a 422 and take the named refusal away.
    assert "format" not in folder_id["schema"], folder_id["schema"]
    assert sorted(added["responses"]) == ["201", "404", "409", "422"], sorted(added["responses"])


def test_the_generated_client_is_the_same_generation_as_the_contract():
    """`frontend/src/generated/api.ts` was re-taken from this commit's contract, not hand-edited."""

    text = GENERATED.read_text(encoding="utf-8")
    schemas = _contract()["components"]["schemas"]
    for component in ("CollectionFolder", "CollectionItem", "CollectionFolderCreate", "CollectionItemCreate"):
        assert f"        {component}: {{" in text, f"{component} is not declared in generated/api.ts"
        assert component in schemas, component
    for operation in ("listCollections", "createCollection", "addCollectionItem"):
        assert f"    {operation}: {{" in text, operation
    assert '"/collections": {' in text and '"/collections/{folder_id}/items": {' in text
    # The generated shape carries no `address`, because the contract declares none.
    item_fields = set(schemas["CollectionItem"]["properties"])
    assert "address" not in item_fields, sorted(item_fields)
    generated_item = text[text.index("        CollectionItem: {") :]
    assert "address" not in generated_item.split("        }", 1)[0], "api.ts grew an address cell"


def test_the_face_fetches_the_runtime_prefix_not_the_contract_path():
    """The contract's paths omit `/api/v1`; the server's do not.  Fetching the former is a silent miss.

    This arm exists because the first cut of the face did exactly that: `COLLECTIONS_LIST_PATH` was
    the contract's `/collections`, so the browser asked the static server for a route it answers with
    `index.html`, `JSON.parse` threw, and the screen showed its own failure line while the door
    answered 200 to the same query.  The contract file and the runtime disagree by design
    (`tools/verify_openapi_parity.py` maps one onto the other), so the agreement has to be pinned in
    a test rather than trusted from a comment in the module.
    """

    text = (ROOT / "frontend/src/features/collections/collections.ts").read_text(encoding="utf-8")
    declared = [l.split("=", 1)[1].split('"')[1] for l in text.splitlines()
                if l.startswith("export const COLLECTIONS_LIST_PATH")]
    assert len(declared) == 1, declared
    assert declared[0] == "/api/v1/collections", declared
    contract_paths = _contract()["paths"]
    assert declared[0].startswith("/api/v1"), "the runtime prefix is what the server mounts"
    assert declared[0] not in contract_paths, (
        "the contract's own path is not a fetchable route on this server", declared[0])


def test_demo_reset_can_empty_the_container_and_does_it_in_the_right_order():
    """`services.reset_all` names the two tables, and names them before `places`.

    Measured before that line existed: a shelf holding one place made
    `POST /api/v1/demo/reset` answer 500 with `sqlite3.IntegrityError: FOREIGN KEY constraint
    failed` (`scratch/reset_counter_before_r1.json`, keys
    `4_demo_reset_with_a_non_empty_shelf` and `6_server_log_attribution`).  `places` is deleted by
    that door and `collection_items.place_id` references it with no `ON DELETE` clause, with
    `PRAGMA foreign_keys=ON` on every connection (`backend/app/db/session.py`), so a container
    that is not emptied first makes the fixture reset for roughly 140 modules fail the moment a
    traveller favourites anything.

    This arm reads the declared list, because a behavioural run of it here would empty the store
    this pytest process shares with every module that runs after it -- the walk is in the receipt,
    where it owns a private database.  The number of tables is printed rather than asserted: it is
    read out of the artifact each run, so a later order that adds one cannot leave a stale literal
    behind, and the six prose claims of "52 tables" in other modules' docstrings are named in this
    order's delivery block instead of being rewritten here.
    """

    import ast

    from app import services

    tree = ast.parse(Path(services.__file__).read_text(encoding="utf-8"))
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "reset_all"
    )
    loop = next(node for node in function.body if isinstance(node, ast.For))
    tables = [element.value for element in loop.iter.elts]
    assert len(tables) == len(set(tables)), "the reset list names a table twice"
    for name in ("collection_items", "collection_folders"):
        assert name in tables, name
    # Children before parents, the rule the function's own comment states.
    assert tables.index("collection_items") < tables.index("collection_folders"), tables
    assert tables.index("collection_items") < tables.index("places"), tables
    assert tables.index("collection_folders") < tables.index("places"), tables
    print(f"reset_all declares {len(tables)} tables")


def test_the_served_document_declares_the_refusals_the_contract_declares():
    """`/openapi.json` is a second artifact, and a code unnamed in the decorator never reaches it.

    Found on this order's own round trip: the contract declares a 404 for creating a shelf (the
    asserted traveller does not exist), while the served document answered `201, 422` for that door
    because the decorator had named no 404 -- and `tools/verify_openapi_parity.py` compares path and
    method sets, so it stayed green across exactly that gap.  ORD-0395's rule is written here as an
    arm rather than as a sentence: for every refusal name the contract carries on these three doors,
    the served document must carry the same status code.
    """

    served = app.openapi()
    contract = _contract()["paths"]
    doors = {
        ("/collections", "post"): {"404"},
        ("/collections/{folder_id}/items", "post"): {"404", "409"},
        ("/collections", "get"): {"422"},
    }
    for (path, verb), expected in doors.items():
        assert path in contract, path
        # the runtime mounts everything under /api/v1, which the contract path omits by design
        node = served["paths"]["/api/v1" + path][verb]
        got = set(node.get("responses", {}))
        assert expected <= got, (path, verb, sorted(got))
        for code in sorted(expected):
            assert code in contract[path][verb].get("responses", {}), (path, verb, code)


def test_the_shape_claims_no_address_because_places_carries_none():
    """A tripwire for the next order, not a claim that the competitor's row is matched.

    The competitor's shelf row shows a cover, a name, an address and a note
    (`D:/moodle/_apk_analysis_pi_travel/MAP/map_findings.md`, F23).  Three of those four exist
    here; `address` and the cover do not, and the reason is a column that is not there rather than
    a field nobody thought to add.  When `places` grows one, this arm goes red so the field has to
    be put into `CollectionItem` and the contract in the same cut.
    """

    with sqlite3.connect(_store_path()) as connection:
        columns = [row[1] for row in connection.execute("PRAGMA table_info(places)")]
    assert not [name for name in columns if "addr" in name.lower()], columns
    with SessionLocal() as session:
        rows = session.scalars(select(Place)).all()
        address_bearing = [
            row.id
            for row in rows
            if isinstance(json.loads(row.metadata_json), dict)
            and any("addr" in key.lower() or "street" in key.lower() for key in json.loads(row.metadata_json))
        ]
    assert address_bearing == [], address_bearing
    assert "address" not in _contract()["components"]["schemas"]["CollectionItem"]["properties"]
