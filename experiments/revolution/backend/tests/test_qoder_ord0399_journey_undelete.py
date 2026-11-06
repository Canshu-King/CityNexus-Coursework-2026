"""ORD-0399 - one door, one verb: a binned trip can be brought back without touching anything else.

Measured at the authoritative tip `dc88df93a35e16dcee824168110d2cf48a592f20`, `/api/v1/journeys`
answered POST and GET, `/api/v1/journeys/{journey_id}` answered GET/PATCH/DELETE, and nothing at
all could put a binned trip back: slice 1 took a trip out of the list and there was no way to undo
it.  This file is the undo.

The order's four requirements, and how each arm below pays for it:

* the door writes one column.  `journeys.deleted_at` goes back to NULL and the row keeps the same
  `rowid`, `status`, `created_at` and `updated_at`; the content tables around the trip are counted
  before the delete, in the bin, and after the 找回, and compared as a dict - so "nothing else
  moved" is a reading rather than an adjective.  The non-zero version of that reading (a private
  copy of a real store that has activities on it) is taken by the probe named in the delivery
  block, because no door in this build can create the first activity row without the seeded
  catalog: measured on a fresh temporary store, `POST /api/v1/journeys/{id}/plans/generate`
  answered 422 with activities=0 and plan_versions=0;
* the refusal is named, and it is not a silent success.  找回 of a trip that was never in the bin
  answers 409 with `error.reason_code = JOURNEY_NOT_DELETED` and leaves no audit row, because a
  screen that said "found it again" about a trip that was never lost would be reporting an event
  that did not happen;
* the word is 找回 / undelete, chosen by measurement: `recover*` and 恢复 are autonomous recovery
  here, 还原 is plan-version history's word for reverting to an older plan, and `restore` already
  means "a reload brings the draft back".  The last arm pins that this door did not smuggle in a
  `journey.restored` / `restoreJourney` name;
* no `Idempotency-Key` (the durable `deleted_at` decides, and a replayed tap is the 409), and no
  hard delete - that verb is slice 3's.
"""

from __future__ import annotations

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
TZ = timezone(timedelta(hours=8))
START = datetime(2026, 11, 3, 9, 0, tzinfo=TZ)

CONTENT_TABLES = ("journeys", "plan_versions", "activities", "commitments", "events")


def _store_path() -> Path:
    """The exact file this pytest process opened - a resolved path, not a name."""

    from conftest import RESOLVED_TEST_DATABASE

    return Path(RESOLVED_TEST_DATABASE.replace("sqlite:///", "", 1)).expanduser().resolve()


def _counts() -> dict[str, int]:
    with sqlite3.connect(_store_path()) as connection:
        return {
            table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in CONTENT_TABLES
        }


def _journey_row(journey_id: str) -> sqlite3.Row:
    with sqlite3.connect(_store_path()) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT rowid, id, traveler_id, status, title, city_code, planning_mode,"
            " budget_amount_minor, created_at, updated_at, deleted_at FROM journeys"
            " WHERE id = ?",
            (journey_id,),
        ).fetchone()
    assert row is not None, f"no journeys row for id={journey_id} in {_store_path()}"
    return row


def _traveler() -> str:
    traveler_id = str(uuid4())
    now = datetime.now(timezone.utc).isoformat()
    with SessionLocal() as session:
        session.add(
            Traveler(
                id=traveler_id,
                display_name="ORD-0399 probe",
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
            "title": "ORD-0399",
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


def _list_ids() -> list[str]:
    answer = client.get("/api/v1/journeys")
    assert answer.status_code == 200, answer.text
    return [str(item["id"]) for item in answer.json()]


def _audit_rows(action: str, journey_id: str) -> list[sqlite3.Row]:
    with sqlite3.connect(_store_path()) as connection:
        connection.row_factory = sqlite3.Row
        return list(
            connection.execute(
                "SELECT rowid, before_json, after_json FROM audit_logs"
                " WHERE action = ? AND entity_id = ?",
                (action, journey_id),
            ).fetchall()
        )


def _bring_back(journey_id: str):
    return client.post(f"/api/v1/journeys/{journey_id}/undelete")


# ------------------------------------------------------------- the door and its one column


def test_bringing_a_trip_back_puts_it_in_the_list_without_moving_any_content_row() -> None:
    journey_id = _journey()
    before = _counts()
    baseline_list_len = len(_list_ids())
    assert journey_id in _list_ids(), _list_ids().count(journey_id)

    assert client.delete(f"/api/v1/journeys/{journey_id}").status_code == 204
    binned = _journey_row(journey_id)
    assert binned["deleted_at"] is not None, dict(binned)
    assert journey_id not in _list_ids(), "the deleted trip is still listed"
    binned_list_len = len(_list_ids())
    assert binned_list_len == baseline_list_len - 1, (baseline_list_len, binned_list_len)

    answer = _bring_back(journey_id)
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert str(body["id"]) == journey_id, body
    assert str(body["status"]) == binned["status"], (body, dict(binned))

    after = _journey_row(journey_id)
    assert after["rowid"] == binned["rowid"], (binned["rowid"], after["rowid"])
    assert after["deleted_at"] is None, dict(after)
    assert after["status"] == binned["status"], dict(after)
    assert after["created_at"] == binned["created_at"], dict(after)
    assert after["updated_at"] == binned["updated_at"], (
        "the 找回 door must not dress the trip up as edited: "
        f"{binned['updated_at']} != {after['updated_at']}"
    )
    restored_list_len = len(_list_ids())
    assert restored_list_len == baseline_list_len, (baseline_list_len, restored_list_len)
    # Nothing else in the store moved across the whole delete -> 找回 cycle.
    assert _counts() == before, (before, _counts())
    print(
        f"[ord0399][undelete] store={_store_path()} journeys.id={journey_id} "
        f"rowid={after['rowid']} list_len_before={baseline_list_len} "
        f"list_len_binned={binned_list_len} list_len_restored={restored_list_len} "
        f"deleted_at_restored=NULL content_counts_unchanged=True "
        f"activities_rows={before['activities']} plan_version_rows={before['plan_versions']}"
    )


def test_the_door_writes_the_bin_column_and_exactly_one_audit_row() -> None:
    journey_id = _journey()
    assert client.delete(f"/api/v1/journeys/{journey_id}").status_code == 204
    moment = _journey_row(journey_id)["deleted_at"]
    assert moment is not None, dict(_journey_row(journey_id))

    assert _bring_back(journey_id).status_code == 200, "the 找回 was refused"

    rows = _audit_rows("journey.undeleted", journey_id)
    assert len(rows) == 1, [dict(row) for row in rows]
    assert str(rows[0]["before_json"]) == f'{{"deleted_at": "{moment}"}}', rows[0]["before_json"]
    assert yaml.safe_load(rows[0]["after_json"]) == {"deleted_at": None}, rows[0]["after_json"]
    print(
        f"[ord0399][audit] store={_store_path()} action=journey.undeleted rows={len(rows)} "
        f"audit_rowid={rows[0]['rowid']} deleted_at_before={moment} deleted_at_after=NULL"
    )


# ------------------------------------------------------------------- the two refusals


def test_bringing_back_a_trip_that_was_never_binned_is_refused_and_writes_nothing() -> None:
    journey_id = _journey()
    live = _journey_row(journey_id)
    assert live["deleted_at"] is None, dict(live)
    before = _counts()

    answer = _bring_back(journey_id)
    assert answer.status_code == 409, answer.text
    body = answer.json()
    assert body["error"]["code"] == "CONFLICT", body
    assert body["error"]["reason_code"] == "JOURNEY_NOT_DELETED", body
    assert body["error"]["details"]["deleted_at"] is None, body

    after = _journey_row(journey_id)
    assert after["deleted_at"] is None, dict(after)
    assert after["updated_at"] == live["updated_at"], dict(after)
    assert after["rowid"] == live["rowid"], dict(after)
    assert _counts() == before, (before, _counts())
    # A refused tap is not an event: no 找回 row may exist for this trip.
    assert _audit_rows("journey.undeleted", journey_id) == [], "the refusal left an audit row"
    print(
        f"[ord0399][refusal-409] store={_store_path()} journeys.id={journey_id} "
        f"rowid={after['rowid']} http_status={answer.status_code} code={body['error']['code']} "
        f"reason_code={body['error']['reason_code']} audit_rows_for_journey_undeleted=0"
    )


def test_an_unknown_id_is_the_same_name_slice_1_already_answers_with() -> None:
    missing = str(uuid4())
    answer = _bring_back(missing)
    assert answer.status_code == 404, answer.text
    body = answer.json()
    assert body["error"]["code"] == "NOT_FOUND", body
    assert body["error"]["reason_code"] == "JOURNEY_NOT_FOUND", body
    with sqlite3.connect(_store_path()) as connection:
        rows = int(
            connection.execute("SELECT COUNT(*) FROM journeys WHERE id = ?", (missing,)).fetchone()[0]
        )
    assert rows == 0, rows
    print(
        f"[ord0399][refusal-404] store={_store_path()} journey_id={missing} "
        f"http_status={answer.status_code} reason_code={body['error']['reason_code']} "
        f"journeys_rows_for_id={rows}"
    )


def test_a_replayed_tap_is_the_refusal_and_cannot_undo_the_first_undelete() -> None:
    journey_id = _journey()
    assert client.delete(f"/api/v1/journeys/{journey_id}").status_code == 204
    assert _bring_back(journey_id).status_code == 200
    assert journey_id in _list_ids(), "the first 找回 did not put the trip back"

    again = _bring_back(journey_id)
    assert again.status_code == 409, again.text
    assert again.json()["error"]["reason_code"] == "JOURNEY_NOT_DELETED", again.json()
    assert _journey_row(journey_id)["deleted_at"] is None
    # The refused tap cannot re-bury it: the trip is still where the first one put it.
    assert journey_id in _list_ids(), _list_ids().count(journey_id)


# ------------------------------------------------------- the contract and the word boundary


def test_the_hand_contract_declares_the_door_and_both_refusals() -> None:
    spec = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    operation = spec["paths"]["/journeys/{journey_id}/undelete"]["post"]
    assert operation["operationId"] == "undeleteJourney", sorted(operation)
    assert operation["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/Journey"
    }, operation["responses"]
    described = {
        status: str(body.get("description", "")) for status, body in operation["responses"].items()
    }
    assert "JOURNEY_NOT_FOUND" in described["404"], described
    assert "JOURNEY_NOT_DELETED" in described["409"], described


def test_this_door_did_not_borrow_the_recovery_or_restore_words() -> None:
    """The vocabulary ruling, pinned where it can actually rot: the names in code.

    `恢复` and `recover*` are autonomous recovery in this product, `还原` is plan-version history's
    word, and `restore` means a reload bringing a draft back.  This slice's verb is 找回 / undelete,
    so no *name* introduced here may say restored/recovered about the recycle bin.  Naming literals
    are what is checked - prose that explains the rejected words is allowed, and the docstring of
    this very door uses them to say why they were rejected.
    """

    source = (ROOT / "backend/app/main.py").read_text(encoding="utf-8")
    contract = CONTRACT.read_text(encoding="utf-8")
    door_start = source.index("def undelete_journey")
    door_end = source.index('@app.get("/api/v1/journeys/{journey_id}")', door_start)
    door = source[door_start:door_end]
    assert 'action="journey.undeleted"' in door, "the audit action this door writes changed name"
    # The clearing itself, in whichever shape the product writes it: an ORM assignment up to
    # ORD-0383 格三, and the predicate's `.values(deleted_at=None)` from that order on.  This arm is
    # about words; the behaviour that the trip really comes back is the arm above it, which reads
    # the column out of the store rather than reading the source.
    assert "deleted_at=None" in door or "deleted_at = None" in door, door[:400]
    for borrowed in (
        'action="journey.restored"',
        'action="journey.recovered"',
        "restoreJourney",
        "recoverJourney",
        "undeleteJourneyRestored",
    ):
        assert borrowed not in source, f"{borrowed} would give an occupied word a second meaning"
        assert borrowed not in contract, f"{borrowed} reached the contract"
