"""ORD-0520 follow-up - the fifth refusal, produced for real instead of declared.

The delivered order named four refusals and gave each of them an arm and a socket-level reading.  The
fifth, `ABORT_WRITE_CONTESTED`, was reported by this lane as unreachable in practice
(`NOT_AVAILABLE_BY_CMDR`), and that was the honest reading at the time -- but the branch is in the
contract, so a name nobody ever sees is a claim nobody ever tested.

These arms take it by scheduling a real, committed UPDATE from a second connection into the window
between this door's own read and its own UPDATE.  Nothing product-side is replaced: no function is
monkeypatched, no clock is frozen, no assertion is loosened.  The door's statements run and lose on
`rowcount`, which is the only thing that branch was written for.

Why the interference is split into two modes, and what each one can prove:

* `mode="status"` -- the other writer moves `status` (DRAFT <-> PLANNED, never CANCELLED, never the
  bin), so the tap loses on the first half of the compare-and-set and must answer 409 with its own
  name, write nothing, and leave no audit row;
* `mode="aborted_at"` -- the other writer leaves `status` exactly as the tap read it and fills
  `aborted_at`, which is the only way to see whether the second half of the predicate is real.
  Deleting that clause from `app.main` keeps every other arm green and turns this one red, measured.

Three mutations were run against these arms, each on its own generation of the tree, and all three
were killed: shortening `_ABORT_WRITE_ATTEMPTS` from 3 to 1, dropping the `status` clause of the
predicate, and dropping the `aborted_at` clause.  The counts are in the delivery block.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event

from app.db.models import Traveler
from app.db.session import SessionLocal, engine
from app.main import _ABORT_WRITE_ATTEMPTS, app

client = TestClient(app, raise_server_exceptions=False)
TZ = timezone(timedelta(hours=8))
START = datetime(2027, 3, 4, 9, 0, tzinfo=TZ)
REASON = "争抢臂写下的原因"


def _store_path() -> Path:
    from conftest import RESOLVED_TEST_DATABASE

    return Path(RESOLVED_TEST_DATABASE.replace("sqlite:///", "", 1)).expanduser().resolve()


def _journey_row(journey_id: str) -> sqlite3.Row:
    with sqlite3.connect(_store_path()) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT rowid, id, status, deleted_at, aborted_at, abort_reason FROM journeys"
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
                display_name="ORD-0520 contention probe",
                email=None,
                locale="zh-CN",
                timezone="Asia/Shanghai",
                created_at=now,
                updated_at=now,
            )
        )
        session.commit()
    return traveler_id


def _journey() -> dict:
    traveler_id = _traveler()
    created = client.post(
        "/api/v1/journeys",
        json={
            "traveler_id": traveler_id,
            "title": "ORD-0520 contention",
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
        f"/api/v1/journeys/{journey_id}/abort",
        json={"reason": reason, "actor_traveler_id": actor},
    )


def _reason_of(answer) -> str | None:
    return (answer.json().get("error") or {}).get("reason_code")


def _details_of(answer) -> dict:
    return (answer.json().get("error") or {}).get("details") or {}


def _receipt_of(answer) -> str | None:
    return (answer.json().get("error") or {}).get("error_receipt_id")


def _audit_rows(action: str, journey_id: str) -> list[sqlite3.Row]:
    with sqlite3.connect(_store_path()) as connection:
        connection.row_factory = sqlite3.Row
        return list(
            connection.execute(
                "SELECT rowid FROM audit_logs WHERE action = ? AND entity_id = ?",
                (action, journey_id),
            ).fetchall()
        )


class Contester:
    """A second connection that commits a real change right before this door's own UPDATE."""

    def __init__(self, journey_id: str, mode: str = "status") -> None:
        self.journey_id = journey_id
        self.mode = mode
        self.store = _store_path()
        self.writes: list[str] = []

    def __enter__(self):
        event.listen(engine, "before_cursor_execute", self._hook)
        return self

    def __exit__(self, *exc):
        # `event.listen` returns nothing; the removable object is the bound hook itself.
        event.remove(engine, "before_cursor_execute", self._hook)
        return False

    def _hook(self, conn, cursor, statement, parameters, context, executemany) -> None:
        if "UPDATE journeys" not in statement or "aborted_at" not in statement:
            return
        with sqlite3.connect(self.store, timeout=30) as other:
            if self.mode == "status":
                current = other.execute(
                    "SELECT status FROM journeys WHERE id = ?", (self.journey_id,)
                ).fetchone()[0]
                # Always a real change: writing back the value the row already holds would let the
                # door's own predicate match, and the tap would simply win.
                nxt = "DRAFT" if current == "PLANNED" else "PLANNED"
                other.execute(
                    "UPDATE journeys SET status = ? WHERE id = ?", (nxt, self.journey_id)
                )
            else:
                # `status` stays exactly as the tap read it; only the second half of the
                # compare-and-set moves.
                # A real instant: `journey_schema` parses this column on the way out, so a
                # placeholder string would break the read path and the arm would stop measuring
                # the write gate it is here for.
                nxt = datetime.now(timezone.utc).isoformat()
                other.execute(
                    "UPDATE journeys SET aborted_at = ? WHERE id = ?",
                    (nxt, self.journey_id),
                )
            other.commit()
        self.writes.append(nxt)


def test_a_tap_losing_the_row_three_times_answers_with_its_own_name_and_writes_nothing() -> None:
    trip = _journey()
    with Contester(trip["id"], mode="status") as race:
        answer = _abort(trip["id"], trip["traveler_id"])

    assert answer.status_code == 409, answer.text
    assert _reason_of(answer) == "ABORT_WRITE_CONTESTED", answer.text
    assert _receipt_of(answer), answer.text
    details = _details_of(answer)
    # Three, spelled out on purpose: reading the budget back off `_ABORT_WRITE_ATTEMPTS` would let
    # this arm follow the product into a shortened budget instead of catching it.  The constant is
    # checked separately, so the two cannot silently drift apart.
    assert len(race.writes) == 3, race.writes
    assert details["attempts"] == 3, details
    assert _ABORT_WRITE_ATTEMPTS == 3, _ABORT_WRITE_ATTEMPTS
    assert details["status_seen"] in ("DRAFT", "PLANNED"), details
    assert details["status_now"] in ("DRAFT", "PLANNED"), details
    row = _journey_row(trip["id"])
    assert row["aborted_at"] is None, dict(row)
    assert row["abort_reason"] is None, dict(row)
    assert row["status"] != "CANCELLED", dict(row)
    assert not _audit_rows("journey.aborted", trip["id"]), dict(row)


def test_the_same_row_refuses_the_same_way_twice_and_the_receipt_is_derived_not_random() -> None:
    """Re-runnable, and it corrects one assumption this lane had carried.

    Two identical contested failures answer with the SAME `error_receipt_id`: the receipt is derived
    from the request identity by `app.main`'s per-request middleware, not drawn at random.  That is
    what makes an id quotable from another process, and it is why "two taps, two different ids"
    would have been the wrong assertion to write here.
    """

    trip = _journey()
    receipts = []
    for _ in range(2):
        with Contester(trip["id"], mode="status"):
            answer = _abort(trip["id"], trip["traveler_id"])
        assert answer.status_code == 409, answer.text
        assert _reason_of(answer) == "ABORT_WRITE_CONTESTED", answer.text
        receipts.append(_receipt_of(answer))
    assert all(receipts), receipts
    assert len(set(receipts)) == 1, receipts
    assert not _audit_rows("journey.aborted", trip["id"]), receipts


def test_the_second_half_of_the_predicate_moves_the_row_on_its_own() -> None:
    """Interference on `aborted_at` alone still has to cost the tap the row.

    A contester that leaves `status` exactly as the tap read it is the only way to test whether the
    compare-and-set really carries the abort column: with that clause deleted from the UPDATE, the
    tap below writes and this arm goes red -- measured, not assumed.
    """

    trip = _journey()
    with Contester(trip["id"], mode="aborted_at") as race:
        answer = _abort(trip["id"], trip["traveler_id"])

    assert answer.status_code == 409, answer.text
    # The name here is the durable one, not the contested one: once `aborted_at` carries a value the
    # door has a fact to report, and "already aborted" is the truth even when this tap lost the row
    # three times over.  `abort_reason` staying NULL in the store is what proves this tap wrote
    # nothing -- the contester's placeholder value is not a reason this door would ever store.
    # Which name appears is genuinely a matter of timing: if the second read sees the value the
    # contester committed, the durable fact answers and the tap stops spending attempts; if it never
    # sees one before the budget runs out, the contest is named.  What may not vary is everything
    # else -- a 409, a name, and no write by this tap.
    assert _reason_of(answer) in ("JOURNEY_ALREADY_ABORTED", "ABORT_WRITE_CONTESTED"), answer.text
    assert race.writes, answer.text
    details = _details_of(answer)
    if _reason_of(answer) == "JOURNEY_ALREADY_ABORTED":
        assert details["aborted_at"] in race.writes, (details, race.writes)
    assert _journey_row(trip["id"])["abort_reason"] is None, details
    assert not _audit_rows("journey.aborted", trip["id"]), details


@pytest.mark.parametrize("reason", ["第一次争抢败因", "第二次争抢败因"])
def test_a_contested_tap_never_reports_a_success_it_did_not_win(reason: str) -> None:
    """The invariant, independent of which of the two halves moved: a lost tap is never a 200."""

    trip = _journey()
    mode = "status" if reason.startswith("第一") else "aborted_at"
    with Contester(trip["id"], mode=mode):
        answer = _abort(trip["id"], trip["traveler_id"], reason=reason)
    assert answer.status_code == 409, answer.text
    body = answer.json()
    assert (body.get("error") or {}).get("code") == "CONFLICT", body
    # Either interference answers with a name -- never a 200 the tap did not win, and never the
    # framework's unnamed 409.  Which name depends on what the row ended up holding.
    assert _reason_of(answer) in ("ABORT_WRITE_CONTESTED", "JOURNEY_ALREADY_ABORTED"), body
    row = _journey_row(trip["id"])
    assert row["abort_reason"] != reason, dict(row)
    assert len(_audit_rows("journey.aborted", trip["id"])) == 0, row


def test_a_trip_moved_off_cancelled_elsewhere_still_refuses_a_second_abort() -> None:
    """The gate is the stored abort fact, not the mutable status word.

    Found by running the door under real concurrent writers rather than reading it: with the first
    version of this code a tap that saw `status` moved off CANCELLED re-aborted the trip, so one
    journey accumulated several `journey.aborted` audit rows and the traveller's stored reason was
    replaced by a tap that changed nothing.  This arm is red on that version and green here.
    """

    trip = _journey()
    first = _abort(trip["id"], trip["traveler_id"], reason="真正中止的那一句")
    assert first.status_code == 200, first.text
    stored = _journey_row(trip["id"])
    assert stored["status"] == "CANCELLED", dict(stored)
    assert stored["abort_reason"] == "真正中止的那一句", dict(stored)

    # An ordinary writer of that column -- the same statement the plan doors use -- moves the trip
    # off CANCELLED.  Nothing here reaches into the door or replaces one of its functions.
    with sqlite3.connect(_store_path(), timeout=30) as other:
        other.execute("UPDATE journeys SET status = ? WHERE id = ?", ("PLANNED", trip["id"]))
        other.commit()
    assert _journey_row(trip["id"])["status"] == "PLANNED"

    second = _abort(trip["id"], trip["traveler_id"], reason="第二拍想改写成这句")
    assert second.status_code == 409, second.text
    assert _reason_of(second) == "JOURNEY_ALREADY_ABORTED", second.text
    details = _details_of(second)
    assert details["abort_reason"] == "真正中止的那一句", details
    assert details["aborted_at"] == stored["aborted_at"], details

    after = _journey_row(trip["id"])
    assert after["abort_reason"] == "真正中止的那一句", dict(after)
    assert after["aborted_at"] == stored["aborted_at"], dict(after)
    rows = _audit_rows("journey.aborted", trip["id"])
    assert len(rows) == 1, [dict(r) for r in rows]
