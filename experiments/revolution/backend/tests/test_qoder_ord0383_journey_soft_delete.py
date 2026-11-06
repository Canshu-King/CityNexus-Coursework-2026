"""ORD-0383 slice 1 - a trip can be deleted without the store throwing it away.

Measured at the authoritative tip `db6a04cb1d734963b673351973329c08bb264ef3`, before this file
existed, `backend/app/main.py` answered `@app.delete` at exactly two places
(`:2657` traveler-twin learned, `:4645` a constraint sub-resource) and the collection path
`/api/v1/journeys` had one method, `POST` (`:2807`).  So there was no door that removed a trip
and no list a removal could take it out of: "deleting no longer destroys" had nothing to point
at, and the commander's `cmdr 04:4x` block re-scoped this order for exactly that reason.

What is pinned here, and what is deliberately *not*:

* the column arrives additively.  R1 migrates a database built with the pre-ORD-0383 DDL and
  checks that the row it already held keeps every value it held and reads `deleted_at` back as
  NULL - not as a default.  A `DEFAULT` would have handed every historical trip a deletion
  moment it never had, which is the same class of fabrication ORD-0355 removed;
* every read is raw SQLite against the file this process actually opened (named by
  `conftest.RESOLVED_TEST_DATABASE`), with table, column and `rowid` in the assertion, because
  a value that only survived inside the request object says nothing about what was stored;
* the two refusals are named, and they are different names: `JOURNEY_NOT_FOUND` on the 404 for
  a row that does not exist, `JOURNEY_ALREADY_DELETED` on the 409 for the second tap on a row
  that does.  Both ride in `error.reason_code` next to the existing `error.code` family
  (`NOT_FOUND` / `CONFLICT`), because `test_qoder_ord0272_continuation.py::test_G6...` keeps
  this server to one failure vocabulary and mints no new `error.code` here - measured: with
  `error("JOURNEY_NOT_FOUND", ...)` written the way the helper is usually called, that gate
  went red on this very commit.  The second tap must not rewrite the first deletion moment;
* `GET /api/v1/journeys` is judged by set equality against the store, not by a count I typed
  in - the suite shares one database across modules, so a hand-written total would be a number
  about whatever ran before this file, not about the filter.

Slices 2 and 3 are out of scope and this file refuses to pre-implement them: no recycle-bin
list, no restore, no retention cap, no idempotency ledger, no screen entry.  R4 pins the one
boundary those slices inherit - a deleted trip is still readable at
`GET /api/v1/journeys/{journey_id}` - so that a later change to it is a decision, not a side
effect.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import yaml
from fastapi.testclient import TestClient

from app.db.models import Traveler
from app.db.session import SessionLocal, ensure_schema, make_engine
from app.main import app

client = TestClient(app)
ROOT = Path(__file__).resolve().parents[2]
CONTRACT = ROOT / "contracts" / "openapi.yaml"
TZ = timezone(timedelta(hours=8))
START = datetime(2026, 11, 3, 9, 0, tzinfo=TZ)


def _store_path() -> Path:
    """The exact database file this pytest process opened - not a name, a resolved path."""

    from conftest import RESOLVED_TEST_DATABASE

    return Path(RESOLVED_TEST_DATABASE.replace("sqlite:///", "", 1)).expanduser().resolve()


def _traveler() -> str:
    traveler_id = str(uuid4())
    now = datetime.now(timezone.utc).isoformat()
    with SessionLocal() as session:
        session.add(
            Traveler(
                id=traveler_id,
                display_name="ORD-0383 probe",
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
    """One trip through the real creation door, so the row is a row the product writes."""

    created = client.post(
        "/api/v1/journeys",
        json={
            "traveler_id": _traveler(),
            "title": "ORD-0383 slice 1",
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


def _journey_row(journey_id: str) -> sqlite3.Row:
    """Read the trip back with plain SQLite, naming the table, the row and its rowid."""

    with sqlite3.connect(_store_path()) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT rowid, id, traveler_id, status, title, city_code, planning_mode,"
            " budget_amount_minor, created_at, updated_at, deleted_at"
            " FROM journeys WHERE id = ?",
            (journey_id,),
        ).fetchone()
    assert row is not None, f"no journeys row for id={journey_id} in {_store_path()}"
    return row


def _live_payload() -> list[dict]:
    answer = client.get("/api/v1/journeys")
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert isinstance(body, list), type(body)
    return list(body)


def _live_ids() -> list[str]:
    return [str(item["id"]) for item in _live_payload()]


def _stored_live_ids() -> list[str]:
    with sqlite3.connect(_store_path()) as connection:
        return [
            str(row[0])
            for row in connection.execute(
                "SELECT id FROM journeys WHERE deleted_at IS NULL ORDER BY created_at, id"
            ).fetchall()
        ]


def _stored_deleted_ids() -> list[str]:
    with sqlite3.connect(_store_path()) as connection:
        return [
            str(row[0])
            for row in connection.execute(
                "SELECT id FROM journeys WHERE deleted_at IS NOT NULL ORDER BY created_at, id"
            ).fetchall()
        ]


# ------------------------------------------------------------------ R1 the additive column


LEGACY_DDL = """
CREATE TABLE travelers (
    id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    email TEXT,
    locale TEXT NOT NULL,
    timezone TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE journeys (
    id TEXT PRIMARY KEY,
    traveler_id TEXT NOT NULL,
    title TEXT NOT NULL,
    city_code TEXT NOT NULL,
    timezone TEXT NOT NULL,
    start_at TEXT NOT NULL,
    end_at TEXT NOT NULL,
    budget_currency TEXT NOT NULL,
    budget_amount_minor INTEGER NOT NULL CHECK (budget_amount_minor >= 0),
    planning_mode TEXT NOT NULL DEFAULT 'balanced' CHECK (planning_mode IN ('balanced','budget','explore','strict_schedule')),
    status TEXT NOT NULL CHECK (status IN ('DRAFT','PLANNED','ACTIVE','COMPLETED','CANCELLED')),
    active_plan_version_id TEXT,
    current_activity_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (end_at > start_at)
);
INSERT INTO travelers (id, display_name, email, locale, timezone, created_at, updated_at)
VALUES ('legacy-traveler', 'before ORD-0383', NULL, 'zh-CN', 'Asia/Shanghai',
        '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00');
INSERT INTO journeys (id, traveler_id, title, city_code, timezone, start_at, end_at,
                      budget_currency, budget_amount_minor, planning_mode, status,
                      created_at, updated_at)
VALUES ('legacy-journey', 'legacy-traveler', 'the trip that predates the column', 'CN-XIAMEN',
        'Asia/Shanghai', '2026-11-03T09:00:00+08:00', '2026-11-05T17:00:00+08:00', 'CNY', 90000,
        'explore', 'COMPLETED', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00');
"""


def test_the_additive_column_arrives_without_rewriting_the_row_that_predates_it(tmp_path) -> None:
    """A pre-ORD-0383 store, migrated, and read back.  This store is a `tmp_path` file created
    and deleted by this test; the shared suite database and every `_live.db` / `_preview.db`
    copy were opened by no code path below.
    """

    database = tmp_path / "_ord0383_legacy.db"
    with sqlite3.connect(database) as connection:
        connection.executescript(LEGACY_DDL)

    before_columns = sqlite3.connect(database).execute(
        "SELECT name FROM pragma_table_info('journeys')"
    ).fetchall()
    assert ("deleted_at",) not in before_columns, before_columns
    before_row = sqlite3.connect(database).execute(
        "SELECT rowid, title, city_code, planning_mode, status, budget_amount_minor,"
        " created_at, updated_at FROM journeys WHERE id = 'legacy-journey'"
    ).fetchone()
    assert before_row is not None, "the legacy fixture wrote no row to migrate"

    engine = make_engine(f"sqlite:///{database.as_posix()}")
    ensure_schema(engine)

    after = sqlite3.connect(database).execute(
        'SELECT name, "notnull", dflt_value FROM pragma_table_info(\'journeys\')'
        " WHERE name IN ('deleted_at','status','planning_mode')"
    ).fetchall()
    columns = {str(row[0]): row for row in after}
    assert "deleted_at" in columns, sorted(columns)
    # Additive: nullable, and no default of any kind.
    assert int(columns["deleted_at"][1]) == 0, columns["deleted_at"]
    assert columns["deleted_at"][2] is None, columns["deleted_at"]
    # The closed CHECK on `status` came out of the migration still closed, untouched.
    assert int(columns["status"][1]) == 1, columns["status"]
    assert columns["status"][2] is None, columns["status"]

    after_row = sqlite3.connect(database).execute(
        "SELECT rowid, title, city_code, planning_mode, status, budget_amount_minor,"
        " created_at, updated_at, deleted_at FROM journeys WHERE id = 'legacy-journey'"
    ).fetchone()
    assert after_row[:8] == before_row, (before_row, after_row)
    # The migrated row reads as what it is: never deleted.  Not "deleted at 1970", not "0".
    assert after_row[8] is None, after_row
    print(
        f"[ord0383][migrate] legacy_store={database.resolve()} journeys.id=legacy-journey "
        f"rowid={after_row[0]} deleted_at={'NULL' if after_row[8] is None else after_row[8]} "
        f"status={after_row[4]} planning_mode={after_row[3]} "
        f"deleted_at_notnull={int(columns['deleted_at'][1])} "
        f"deleted_at_default={columns['deleted_at'][2]} pre_existing_columns_unchanged=True"
    )
    engine.dispose()


# --------------------------------------------------------------------- R2 the DELETE door


def test_delete_records_the_moment_and_leaves_the_row_and_its_status_alone() -> None:
    journey_id = _journey()
    before = _journey_row(journey_id)
    assert before["deleted_at"] is None, dict(before)

    answer = client.delete(f"/api/v1/journeys/{journey_id}")
    assert answer.status_code == 204, answer.text
    assert answer.content == b"", (answer.status_code, answer.content)

    after = _journey_row(journey_id)
    assert after["rowid"] == before["rowid"], (before["rowid"], after["rowid"])
    assert after["status"] == before["status"], (before["status"], after["status"])
    assert after["title"] == before["title"], before["title"]
    assert after["budget_amount_minor"] == before["budget_amount_minor"], before[
        "budget_amount_minor"
    ]
    assert after["deleted_at"] is not None, dict(after)
    # A real timestamp, and the only column this door writes.
    assert datetime.fromisoformat(after["deleted_at"]) is not None, after["deleted_at"]
    assert after["updated_at"] == before["updated_at"], (
        "DELETE must not dress the soft delete up as an edit of the trip: "
        f"{before['updated_at']} != {after['updated_at']}"
    )
    # Readout for the receipt: the store, the row, and the value the door wrote.
    print(
        f"[ord0383][delete] store={_store_path()} journeys.id={journey_id} "
        f"rowid={after['rowid']} deleted_at={after['deleted_at']} status={after['status']} "
        f"updated_at_unchanged=True"
    )

    with sqlite3.connect(_store_path()) as connection:
        audits = connection.execute(
            "SELECT COUNT(*) FROM audit_logs WHERE action = 'journey.deleted' AND entity_id = ?",
            (journey_id,),
        ).fetchone()[0]
    assert int(audits) == 1, f"audit_logs rows for action='journey.deleted': {audits}"


def test_an_unknown_id_is_refused_by_name_and_writes_nothing() -> None:
    missing = str(uuid4())
    answer = client.delete(f"/api/v1/journeys/{missing}")
    assert answer.status_code == 404, answer.text
    body = answer.json()
    # One failure vocabulary (G6): the code is the family this server already answers with,
    # and the specific name rides beside it in `reason_code` - ORD-0101's field for this.
    assert body["error"]["code"] == "NOT_FOUND", body
    assert body["error"]["reason_code"] == "JOURNEY_NOT_FOUND", body
    assert body["error"]["message_key"] == "resource.not_found", body
    assert body["error"]["details"]["journey_id"] == missing, body
    with sqlite3.connect(_store_path()) as connection:
        rows = connection.execute(
            "SELECT COUNT(*) FROM journeys WHERE id = ?", (missing,)
        ).fetchone()[0]
    assert int(rows) == 0, rows
    print(
        f"[ord0383][refusal-404] store={_store_path()} journey_id={missing} "
        f"status={answer.status_code} code={body['error']['code']} "
        f"reason_code={body['error']['reason_code']} journeys_rows_for_id={rows}"
    )


def test_a_second_tap_is_refused_by_another_name_and_keeps_the_first_moment() -> None:
    journey_id = _journey()
    first = client.delete(f"/api/v1/journeys/{journey_id}")
    assert first.status_code == 204, first.text
    original = _journey_row(journey_id)["deleted_at"]
    assert original is not None, dict(_journey_row(journey_id))

    again = client.delete(f"/api/v1/journeys/{journey_id}")
    assert again.status_code == 409, again.text
    body = again.json()
    assert body["error"]["code"] == "CONFLICT", body
    assert body["error"]["reason_code"] == "JOURNEY_ALREADY_DELETED", body
    assert body["error"]["message_key"] == "request.conflict", body
    assert body["error"]["details"]["deleted_at"] == original, body

    after = _journey_row(journey_id)
    assert after["deleted_at"] == original, (original, after["deleted_at"])
    assert after["status"] == "DRAFT", after["status"]
    with sqlite3.connect(_store_path()) as connection:
        audits = int(
            connection.execute(
                "SELECT COUNT(*) FROM audit_logs WHERE action = 'journey.deleted' AND entity_id = ?",
                (journey_id,),
            ).fetchone()[0]
        )
    # The refused tap left no second audit row: a refusal cannot look like a second deletion.
    assert audits == 1, audits
    print(
        f"[ord0383][refusal-409] store={_store_path()} journeys.id={journey_id} "
        f"rowid={after['rowid']} status={again.status_code} code={body['error']['code']} "
        f"reason_code={body['error']['reason_code']} "
        f"deleted_at_before={original} deleted_at_after={after['deleted_at']} "
        f"audit_logs_journey_deleted_rows={audits}"
    )


# ------------------------------------------------------------------------ R3 the list door


def test_the_list_answers_with_exactly_the_rows_the_store_calls_not_deleted() -> None:
    kept = _journey()
    removed = _journey()
    assert client.delete(f"/api/v1/journeys/{removed}").status_code == 204

    payload = _live_payload()
    listed = [str(item["id"]) for item in payload]
    stored_live = _stored_live_ids()
    stored_deleted = _stored_deleted_ids()

    # Set equality against the durable store, in the order the route promises: not a count
    # typed into this file.
    assert listed == stored_live, (len(listed), len(stored_live))
    assert removed in stored_deleted, stored_deleted
    assert removed not in listed, listed.count(removed)
    assert kept in listed, kept
    # No row is lost by the filter, so the bin cannot hide a trip that was never deleted.
    with sqlite3.connect(_store_path()) as connection:
        total = int(connection.execute("SELECT COUNT(*) FROM journeys").fetchone()[0])
    assert len(listed) + len(stored_deleted) == total, (
        len(listed),
        len(stored_deleted),
        total,
    )
    # Readout for the receipt: these are the counts the assertions above just proved equal.
    print(
        f"[ord0383][list] store={_store_path()} GET_/api/v1/journeys_rows={len(listed)} "
        f"deleted_at_IS_NULL_rows={len(stored_live)} deleted_at_IS_NOT_NULL_rows="
        f"{len(stored_deleted)} journeys_total={total}"
    )
    # The entry a screen reads is the same body the single-read door gives for that row.
    single = client.get(f"/api/v1/journeys/{kept}")
    assert single.status_code == 200, single.text
    entry = next(item for item in payload if str(item["id"]) == kept)
    assert single.json() == entry, (single.json(), entry)
    for field in ("traveler_id", "title", "city_code", "status", "planning_mode"):
        assert entry[field] == _journey_row(kept)[field], field


# ------------------------------------------------------------------ R4 the inherited boundary


def test_a_deleted_trip_is_still_readable_because_the_row_is_the_bin_s_content() -> None:
    """Slice 1 removes a trip from the list; it does not remove the row.

    If a later slice decides that `GET /api/v1/journeys/{id}` must refuse a binned trip, that
    decision has to delete this assertion on its way in - it cannot arrive as a side effect.
    """

    journey_id = _journey()
    assert client.delete(f"/api/v1/journeys/{journey_id}").status_code == 204
    answer = client.get(f"/api/v1/journeys/{journey_id}")
    assert answer.status_code == 200, answer.text
    assert str(answer.json()["id"]) == journey_id, answer.json()
    assert journey_id not in _live_ids(), _live_ids().count(journey_id)


# ------------------------------------------------------------- R5 the contract says the same


def test_the_hand_contract_declares_both_new_operations_and_names_both_refusals() -> None:
    """`test_contract.py:82` compares (path, method) sets; this arm compares the words.

    A client generated from the contract has to be able to tell "no such trip" from "already in
    the bin" without parsing English, so the two codes are declared where the operation lives.
    """

    spec = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    journeys = spec["paths"]["/journeys"]
    assert "get" in journeys and journeys["get"]["operationId"] == "listJourneys", sorted(journeys)
    assert journeys["get"]["responses"]["200"]["content"]["application/json"]["schema"] == {
        "type": "array",
        "items": {"$ref": "#/components/schemas/Journey"},
    }, journeys["get"]["responses"]

    delete = spec["paths"]["/journeys/{journey_id}"]["delete"]
    assert delete["operationId"] == "deleteJourney", sorted(delete)
    assert "204" in delete["responses"], sorted(delete["responses"])
    described = {
        status: str(body.get("description", ""))
        for status, body in delete["responses"].items()
    }
    assert "JOURNEY_NOT_FOUND" in described["404"], described
    assert "JOURNEY_ALREADY_DELETED" in described["409"], described
