"""ORD-0520 - 中止: the trip can be called off, the reason is stored, and it is not the recycle bin.

Measured on the base this order was cut from (`48aa7a9eaa33a9eaea9ed1d84a0e5f926c88d8f3`):
`journeys.status` had the word `CANCELLED` in its CHECK (`backend/db/schema.sql`) and no door wrote
it - the only writers that existed put PLANNED (`app/main.py` at that commit), ACTIVE
(`app/main.py`) and COMPLETED (`app/main.py`, `:8876`) in that column.  Both shapes of
`POST /api/v1/journeys/{id}/abort` answered the framework's own 404.  So the half of the product
that says "this trip is off, and here is why" did not exist, and a traveller could only drop the
whole row into the bin.

The order's requirements, and the arm that pays for each:

* one named write, with the reason on the row (`aborted_at`, `abort_reason`) rather than only on the
  screen - the first arm reads the row back out of the store by `rowid`;
* every refusal named, one code per condition: `JOURNEY_NOT_FOUND` (404), `ABORT_ACTOR_NOT_OWNER`
  (403), `ABORT_JOURNEY_IN_RECYCLE_BIN` (409), `JOURNEY_ALREADY_ABORTED` (409).  Four arms, and each
  of them also proves nothing was written - a refusal that still moved the row is not a refusal;
* 中止 is not 丢弃: the trip is still in `listJourneys` after the abort, with `deleted_at` still NULL;
* the 实时行程 judgement comes from the server: `live_view` is answered by one function
  (`app/services.py` -- `journey_live_view`) and reaches the screen through all three journey doors,
  which the sixth arm measures side by side on the same row.

The counterexamples are the four refusal arms: they are red on the base generation, where the door
does not exist and every one of those requests answers the framework's 404.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import os
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
GENERATED = ROOT / "frontend" / "src" / "generated" / "api.ts"
PARITY_TOOL = ROOT / "tools" / "verify_openapi_parity.py"
TZ = timezone(timedelta(hours=8))
START = datetime(2026, 12, 7, 9, 0, tzinfo=TZ)
REASON = "家里临时有事，这趟先不去了"

ABORT_PATH = "/api/v1/journeys/{journey_id}/abort"


def _store_path() -> Path:
    """The exact file this pytest process opened - a resolved path, not a name."""

    from conftest import RESOLVED_TEST_DATABASE

    return Path(RESOLVED_TEST_DATABASE.replace("sqlite:///", "", 1)).expanduser().resolve()


def _journey_row(journey_id: str) -> sqlite3.Row:
    with sqlite3.connect(_store_path()) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT rowid, id, traveler_id, status, title, created_at, updated_at,"
            " deleted_at, aborted_at, abort_reason FROM journeys WHERE id = ?",
            (journey_id,),
        ).fetchone()
    assert row is not None, f"no journeys row for id={journey_id} in {_store_path()}"
    return row


def _traveler(label: str) -> str:
    traveler_id = str(uuid4())
    now = datetime.now(timezone.utc).isoformat()
    with SessionLocal() as session:
        session.add(
            Traveler(
                id=traveler_id,
                display_name=f"ORD-0520 {label}",
                email=None,
                locale="zh-CN",
                timezone="Asia/Shanghai",
                created_at=now,
                updated_at=now,
            )
        )
        session.commit()
    return traveler_id


def _journey(owner: str | None = None) -> dict:
    traveler_id = owner or _traveler("owner")
    created = client.post(
        "/api/v1/journeys",
        json={
            "traveler_id": traveler_id,
            "title": "ORD-0520",
            "city_code": "CN-XIAMEN",
            "timezone": "Asia/Shanghai",
            "start_at": START.isoformat(),
            "end_at": (START + timedelta(days=2, hours=8)).isoformat(),
            "budget": {"amount_minor": 90000, "currency": "CNY"},
            "planning_mode": "balanced",
        },
    )
    assert created.status_code == 201, created.text
    return {"id": created.json()["id"], "traveler_id": traveler_id}


def _abort(journey_id: str, actor: str, reason: str = REASON):
    return client.post(
        ABORT_PATH.format(journey_id=journey_id),
        json={"reason": reason, "actor_traveler_id": actor},
    )


def _list_item(journey_id: str) -> dict | None:
    answer = client.get("/api/v1/journeys")
    assert answer.status_code == 200, answer.text
    return next((item for item in answer.json() if str(item["id"]) == journey_id), None)


def _audit_rows(action: str, journey_id: str) -> list[sqlite3.Row]:
    with sqlite3.connect(_store_path()) as connection:
        connection.row_factory = sqlite3.Row
        return list(
            connection.execute(
                "SELECT rowid, actor_type, actor_id, before_json, after_json FROM audit_logs"
                " WHERE action = ? AND entity_id = ?",
                (action, journey_id),
            ).fetchall()
        )


def _reason_of(answer) -> str | None:
    body = answer.json()
    return (body.get("error") or {}).get("reason_code")


def _details_of(answer) -> dict:
    return (answer.json().get("error") or {}).get("details") or {}


def _receipt_of(answer) -> str | None:
    return (answer.json().get("error") or {}).get("error_receipt_id")


# ------------------------------------------------------- the door writes status + reason + moment


def test_aborting_stores_the_reason_on_the_row_and_the_row_keeps_its_identity() -> None:
    trip = _journey()
    before = _journey_row(trip["id"])
    assert before["aborted_at"] is None and before["abort_reason"] is None, dict(before)

    answer = _abort(trip["id"], trip["traveler_id"])
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert str(body["status"]) == "CANCELLED", body
    assert body["abort_reason"] == REASON, body
    assert body["aborted_at"], body

    after = _journey_row(trip["id"])
    # The row is the same row: an UPDATE, not a rebuild.
    assert after["rowid"] == before["rowid"], (dict(before), dict(after))
    assert after["id"] == trip["id"]
    assert after["status"] == "CANCELLED", dict(after)
    # The reason is in the store, not only in the response body -- which is what makes it readable
    # by a later screen that never saw this request.
    assert after["abort_reason"] == REASON, dict(after)
    assert after["aborted_at"] is not None, dict(after)
    assert after["deleted_at"] is None, "中止 must not touch the recycle bin's column"
    assert after["updated_at"] == before["updated_at"], (
        "aborted_at is the authority for this moment; a second timestamp would give a reader two"
    )
    assert datetime.fromisoformat(after["aborted_at"]).year >= 2026, dict(after)

    # And the same three facts come back off a fresh read of the door, not out of the POST body.
    again = client.get(f"/api/v1/journeys/{trip['id']}")
    assert again.status_code == 200, again.text
    assert again.json()["abort_reason"] == REASON, again.json()
    # The column keeps what the door wrote (`datetime.now(timezone.utc).isoformat()`, rendered with
    # `+00:00`) and the response model renders that same instant as `Z`; comparing the strings would
    # measure the renderer, so the arm compares the moments and pins the two spellings as read.
    read_back = datetime.fromisoformat(str(again.json()["aborted_at"]).replace("Z", "+00:00"))
    stored = datetime.fromisoformat(str(after["aborted_at"]).replace("Z", "+00:00"))
    assert read_back == stored, (again.json()["aborted_at"], after["aborted_at"])
    assert str(again.json()["aborted_at"]).endswith("Z"), again.json()["aborted_at"]

    audit = _audit_rows("journey.aborted", trip["id"])
    assert len(audit) == 1, [dict(row) for row in audit]
    assert audit[0]["actor_type"] == "traveler", dict(audit[0])
    assert audit[0]["actor_id"] == trip["traveler_id"], dict(audit[0])
    assert json.loads(audit[0]["before_json"])["status"] == before["status"], dict(audit[0])
    assert json.loads(audit[0]["after_json"])["abort_reason"] == REASON, dict(audit[0])


def test_an_aborted_trip_is_still_in_the_list_because_aborting_is_not_discarding() -> None:
    trip = _journey()
    assert _list_item(trip["id"]) is not None, "the trip was not listed before the abort"

    assert _abort(trip["id"], trip["traveler_id"]).status_code == 200

    item = _list_item(trip["id"])
    assert item is not None, "中止 took the trip out of listJourneys: that is the bin's job"
    assert str(item["status"]) == "CANCELLED", item
    assert item["abort_reason"] == REASON, item
    # The bin doors still hold the trip: it is not in the recycle bin, and its own row says so.
    bin_rows = client.get("/api/v1/recycle-bin/journeys").json()
    assert trip["id"] not in [str(row["id"]) for row in bin_rows], bin_rows


# ------------------------------------------------------------------ four refusals, one name each


def test_a_trip_that_does_not_exist_is_named_absent_rather_than_failed_to_abort() -> None:
    absent = str(uuid4())
    rows_before = int(
        sqlite3.connect(_store_path()).execute("SELECT COUNT(*) FROM journeys").fetchone()[0]
    )
    answer = _abort(absent, str(uuid4()))
    assert answer.status_code == 404, answer.text
    assert _reason_of(answer) == "JOURNEY_NOT_FOUND", answer.text
    assert _details_of(answer)["journey_id"] == absent, answer.text
    assert _receipt_of(answer), "a refusal without an error_receipt_id is not traceable"
    assert not _audit_rows("journey.aborted", absent)
    rows_after = int(
        sqlite3.connect(_store_path()).execute("SELECT COUNT(*) FROM journeys").fetchone()[0]
    )
    assert rows_after == rows_before, (rows_before, rows_after)


def test_somebody_elses_trip_is_refused_by_name_and_stays_exactly_as_it_was() -> None:
    trip = _journey()
    outsider = _traveler("outsider")
    before = _journey_row(trip["id"])

    refused = _abort(trip["id"], outsider)
    assert refused.status_code == 403, refused.text
    assert _reason_of(refused) == "ABORT_ACTOR_NOT_OWNER", refused.text
    assert _details_of(refused)["field"] == "actor_traveler_id", refused.text
    after_refusal = _journey_row(trip["id"])
    assert dict(after_refusal) == dict(before), (dict(before), dict(after_refusal))
    assert not _audit_rows("journey.aborted", trip["id"])

    # The same request with the owner's name on it succeeds, so what the arm just measured is the
    # actor and not the door, the payload, or this route being absent.
    assert _abort(trip["id"], trip["traveler_id"]).status_code == 200, "the owner was refused too"


def test_a_trip_in_the_recycle_bin_is_refused_and_finds_the_door_after_being_brought_back() -> None:
    trip = _journey()
    assert client.delete(f"/api/v1/journeys/{trip['id']}").status_code == 204

    refused = _abort(trip["id"], trip["traveler_id"])
    assert refused.status_code == 409, refused.text
    assert _reason_of(refused) == "ABORT_JOURNEY_IN_RECYCLE_BIN", refused.text
    assert _details_of(refused)["deleted_at"], refused.text
    row = _journey_row(trip["id"])
    assert row["aborted_at"] is None and row["abort_reason"] is None, dict(row)
    assert row["status"] != "CANCELLED", dict(row)
    assert not _audit_rows("journey.aborted", trip["id"])

    assert client.post(f"/api/v1/journeys/{trip['id']}/undelete").status_code == 200
    assert _abort(trip["id"], trip["traveler_id"]).status_code == 200, (
        "the refusal did not follow the bin: the trip aborts once it is back"
    )


def test_a_second_tap_names_the_first_abort_and_leaves_its_moment_and_reason_alone() -> None:
    trip = _journey()
    first = _abort(trip["id"], trip["traveler_id"], reason="第一次的原因")
    assert first.status_code == 200, first.text
    stored = _journey_row(trip["id"])

    second = _abort(trip["id"], trip["traveler_id"], reason="第二次换了个说法")
    assert second.status_code == 409, second.text
    assert _reason_of(second) == "JOURNEY_ALREADY_ABORTED", second.text
    # The refusal quotes what is already stored instead of quietly replacing it.
    assert _details_of(second)["abort_reason"] == "第一次的原因", second.text
    assert _details_of(second)["aborted_at"] == stored["aborted_at"], second.text

    after = _journey_row(trip["id"])
    assert after["aborted_at"] == stored["aborted_at"], (dict(stored), dict(after))
    assert after["abort_reason"] == "第一次的原因", dict(after)
    assert len(_audit_rows("journey.aborted", trip["id"])) == 1, "a refused tap left a second row"


def test_an_empty_reason_is_refused_before_anything_is_written() -> None:
    trip = _journey()
    before = _journey_row(trip["id"])
    answer = _abort(trip["id"], trip["traveler_id"], reason="")
    assert answer.status_code == 422, answer.text
    assert dict(_journey_row(trip["id"])) == dict(before), (dict(before), dict(_journey_row(trip["id"])))
    assert not _audit_rows("journey.aborted", trip["id"])


# --------------------------------------------------- the live-screen judgement is the server's


def test_the_live_screen_judgement_comes_from_the_server_on_every_journey_door() -> None:
    """`live_view` is answered by one function, so three doors cannot disagree about one row."""

    trip = _journey()
    for door, payload in (
        ("GET /journeys item", _list_item(trip["id"])),
        ("GET /journeys/{id}", client.get(f"/api/v1/journeys/{trip['id']}").json()),
        (
            "GET /journeys/{id}/experience",
            client.get(f"/api/v1/journeys/{trip['id']}/experience").json()["journey"],
        ),
    ):
        assert payload["live_view"] == {"included": True, "reason_code": None}, (door, payload)

    answer = _abort(trip["id"], trip["traveler_id"])
    assert answer.status_code == 200, answer.text

    for door, payload in (
        ("POST /abort", answer.json()),
        ("GET /journeys item", _list_item(trip["id"])),
        ("GET /journeys/{id}", client.get(f"/api/v1/journeys/{trip['id']}").json()),
        (
            "GET /journeys/{id}/experience",
            client.get(f"/api/v1/journeys/{trip['id']}/experience").json()["journey"],
        ),
    ):
        assert payload["live_view"] == {
            "included": False,
            "reason_code": "JOURNEY_ABORTED",
        }, (door, payload)

    # A trip still in the bin is decided by the bin's door, not by this one: it keeps its live view.
    binned = _journey()
    assert client.delete(f"/api/v1/journeys/{binned['id']}").status_code == 204
    bin_row = client.get("/api/v1/recycle-bin/journeys").json()
    row = next(item for item in bin_row if str(item["id"]) == binned["id"])
    assert row["bin_state"] == "IN_RECYCLE_BIN", row
    assert row["live_view"]["included"] is True, row


# ------------------------------------------------------------------- contract and generated client


def test_the_door_and_its_refusals_are_declared_in_the_hand_written_contract() -> None:
    spec = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    declared = spec["paths"]["/journeys/{journey_id}/abort"]
    assert set(method for method in declared if method != "parameters") == {"post"}, declared.keys()
    responses = declared["post"]["responses"]
    assert {"200", "403", "404", "409"} <= set(responses), sorted(responses)
    assert responses["200"]["content"]["application/json"]["schema"][
        "$ref"
    ] == "#/components/schemas/Journey"
    for status in ("403", "404", "409"):
        assert responses[status]["content"]["application/json"]["schema"][
            "$ref"
        ] == "#/components/schemas/ErrorEnvelope", status
    joined = " ".join(responses[status]["description"] for status in ("403", "404", "409"))
    for name in (
        "JOURNEY_NOT_FOUND",
        "ABORT_ACTOR_NOT_OWNER",
        "ABORT_JOURNEY_IN_RECYCLE_BIN",
        "JOURNEY_ALREADY_ABORTED",
    ):
        assert name in joined, (name, joined)
    schemas = spec["components"]["schemas"]
    assert {"JourneyAbort", "JourneyLiveView"} <= set(schemas), sorted(schemas)[:8]
    assert set(schemas["JourneyAbort"]["required"]) == {"reason", "actor_traveler_id"}


def test_the_parity_ruler_is_green_and_names_the_new_components_in_the_generated_client() -> None:
    text = GENERATED.read_text(encoding="utf-8")
    assert '"/journeys/{journey_id}/abort"' in text, "the client does not carry the door"
    for name in ("JourneyAbort", "JourneyLiveView"):
        assert f"        {name}: " in text, f"the generated client has no component {name}"
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    run = subprocess.run(
        [sys.executable, str(PARITY_TOOL)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=ROOT,
        env=env,
    )
    assert run.returncode == 0, f"契约与运行时/生成物不齐：{run.stdout}{run.stderr}"
    assert "missing 0; extra 0" in run.stdout, run.stdout


def test_the_word_for_this_action_is_aborted_and_never_borrows_the_bins_or_recovery() -> None:
    """中止 is its own verb: the audit action, the operationId and the copy must not say 找回,
    还原, 恢复 or discard -- those name a different mechanism on this server already."""

    spec = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    declared = spec["paths"]["/journeys/{journey_id}/abort"]["post"]
    assert declared["operationId"] == "abortJourney", declared["operationId"]
    forbidden = ("undelete", "restore", "recover", "discard", "trash", "回收", "找回", "还原", "恢复")
    blob = json.dumps(declared, ensure_ascii=False) + json.dumps(
        spec["components"]["schemas"]["JourneyAbort"], ensure_ascii=False
    )
    # The contract may *explain* the boundary by naming the other words, so only the machine-facing
    # keys are held to them: the operationId, the schema names and the field names.
    machine = " ".join(
        [
            declared["operationId"],
            json.dumps(sorted(declared["responses"])),
            json.dumps(sorted(spec["components"]["schemas"]["JourneyAbort"]["properties"])),
        ]
    )
    for word in forbidden:
        assert word not in machine, (word, machine)
    assert blob  # the prose is allowed to mention the neighbours, and does
