"""ORD-0520 residual -- 实时行程 must stay decided by the column the abort door owns.

Found by walking the order's own rule on a real screen and then asking what could break it:
`journey_live_view` (`backend/app/services.py`) answered "does this trip belong on 实时行程" from
`journeys.status`, while the abort door's own admission gate was moved onto `journeys.aborted_at`
by the contention follow-up (`backend/app/main.py`, `backend/app/main.py`,
`backend/app/main.py`).  Those two rules agree as long as nothing else writes `status`, and
other doors do: `POST /api/v1/journeys/{id}/plans/{plan_version_id}/select` writes
`journeys.status = "PLANNED"` without reading the journey's status at all
(`backend/app/main.py`), and the two activity-advance doors write `"COMPLETED"` the same way
(`backend/app/main.py`, `backend/app/main.py`).  A trip whose plan is selected again after
being called off would therefore come back onto the live screen while still carrying the abort
moment and the traveller's reason -- the "two answers on one screen" that ORD-0520 forbids, and the
same class of defect the socket race probe found for the write gate.

Why the race here is a committed UPDATE rather than a real second select: this lane's store has an
empty catalogue, so no plan can be produced to select.  Measured on the lane's own stack,
`POST /plans/generate` answered http_status=422 with `reason_code=CATALOG_EMPTY`,
`places_in_catalog` 0 and `error_receipt_id=CNX-5SETGBPA`
(`review/ord0520/ABORT_RESURRECT_PROBE.json`, which also records that `/start` refuses the same row
with http_status=409 and that a second abort answers `JOURNEY_ALREADY_ABORTED`).  The UPDATE below
writes exactly the value the select door writes, from a second connection, committed -- the product
code is not replaced, stubbed, or skipped.

0 product bytes in this file; the rule it asks for lives in `app/services.py`.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from app.db.models import Journey, Traveler
from app.db.session import SessionLocal
from app.main import app
from app.services import journey_live_view

client = TestClient(app, raise_server_exceptions=False)
TZ = timezone(timedelta(hours=8))
START = datetime(2026, 12, 7, 9, 0, tzinfo=TZ)
REASON = "家里临时有事，这趟先不去了"
MOVES = ("PLANNED", "ACTIVE", "COMPLETED")


def _store_path() -> Path:
    from conftest import RESOLVED_TEST_DATABASE

    return Path(RESOLVED_TEST_DATABASE.replace("sqlite:///", "", 1)).expanduser().resolve()


def _traveler(label: str) -> str:
    traveler_id = str(uuid4())
    now = datetime.now(timezone.utc).isoformat()
    with SessionLocal() as session:
        session.add(
            Traveler(
                id=traveler_id,
                display_name=f"ORD-0520 live-view {label}",
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
    traveler_id = _traveler("owner")
    created = client.post(
        "/api/v1/journeys",
        json={
            "traveler_id": traveler_id,
            "title": "ORD-0520 live view",
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


def _abort(journey_id: str, actor: str):
    return client.post(
        f"/api/v1/journeys/{journey_id}/abort",
        json={"reason": REASON, "actor_traveler_id": actor},
    )


def _move_status_committed(journey_id: str, value: str) -> None:
    """The write another door performs on this row, from a second connection, committed.

    `PLANNED` is what `select_plan` writes (`backend/app/main.py`); `ACTIVE` is what `/start`
    writes (`backend/app/main.py`); `COMPLETED` is what the two activity-advance doors write.
    """

    with sqlite3.connect(_store_path()) as connection:
        connection.execute("UPDATE journeys SET status = ? WHERE id = ?", (value, journey_id))
        connection.commit()


def _row(journey_id: str) -> sqlite3.Row:
    with sqlite3.connect(_store_path()) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT rowid, status, aborted_at, abort_reason FROM journeys WHERE id = ?",
            (journey_id,),
        ).fetchone()
    assert row is not None, f"no journeys row for {journey_id} in {_store_path()}"
    return row


def _live_view_from_doors(journey_id: str) -> dict:
    """The same field as each of the three doors that carries it answers it."""

    read: dict[str, dict] = {}
    experience = client.get(f"/api/v1/journeys/{journey_id}/experience")
    assert experience.status_code == 200, experience.text
    read["experience"] = experience.json()["journey"]["live_view"]

    single = client.get(f"/api/v1/journeys/{journey_id}")
    assert single.status_code == 200, single.text
    read["getJourney"] = single.json()["live_view"]

    listed = client.get("/api/v1/journeys")
    assert listed.status_code == 200, listed.text
    item = next((row for row in listed.json() if str(row["id"]) == journey_id), None)
    assert item is not None, "the trip left GET /journeys -- aborting was read as discarding"
    read["listJourneys"] = item["live_view"]
    return read


# ---------------------------------------------------------------- the rule, stated once, on one row


def test_a_trip_called_off_stays_off_the_live_screen_when_another_door_moves_its_status() -> None:
    trip = _journey()
    assert _abort(trip["id"], trip["traveler_id"]).status_code == 200

    moved = _live_view_from_doors(trip["id"])
    for door, view in moved.items():
        assert view["included"] is False, f"{door} still calls an aborted trip live: {view}"

    for value in MOVES:
        _move_status_committed(trip["id"], value)
        answers = _live_view_from_doors(trip["id"])
        for door, view in answers.items():
            assert view["included"] is False, (
                f"status was moved to {value!r} and {door} answered {view}: the live judgement "
                "followed a column another door owns instead of the abort the traveller made"
            )
            assert view.get("reason_code") == "JOURNEY_ABORTED", (
                f"status={value!r} on {door} lost the name of the exclusion: {view}"
            )
        row = _row(trip["id"])
        assert row["status"] == value, "the simulated door did not move the row; this arm measured nothing"
        assert row["aborted_at"], "the abort moment vanished with the status"
        assert row["abort_reason"] == REASON, "the traveller's reason vanished with the status"


def test_a_trip_that_was_never_called_off_still_enters_the_live_screen() -> None:
    """The control the arm above needs: `included` may not simply be false for everybody."""

    trip = _journey()
    answers = _live_view_from_doors(trip["id"])
    for door, view in answers.items():
        assert view["included"] is True, f"{door} excluded a trip nobody aborted: {view}"
        assert view.get("reason_code") is None, f"{door} named a reason for a live trip: {view}"


def test_the_decider_reads_the_abort_column_and_not_the_mutable_status() -> None:
    """The rule itself, off the same model the doors build their payload from."""

    def build(status: str, aborted_at: str | None) -> Journey:
        return Journey(
            id=str(uuid4()),
            traveler_id=str(uuid4()),
            title="rule",
            city_code="CN-XIAMEN",
            timezone="Asia/Shanghai",
            start_at=START.isoformat(),
            end_at=START.isoformat(),
            budget_currency="CNY",
            budget_amount_minor=90000,
            status=status,
            aborted_at=aborted_at,
            abort_reason=REASON if aborted_at else None,
        )

    view = journey_live_view(build("PLANNED", START.isoformat()))
    assert view.included is False, (
        "journey_live_view answered included=True for a row that carries an abort moment -- the "
        "screen would offer live content for a trip the traveller called off"
    )
    assert view.reason_code == "JOURNEY_ABORTED", view

    assert journey_live_view(build("PLANNED", None)).included is True
    assert journey_live_view(build("CANCELLED", None)).included is False
    assert journey_live_view(build("ACTIVE", None)).included is True


def test_the_abort_door_still_refuses_a_second_write_after_the_row_was_moved() -> None:
    """The two halves have to agree: the door that wrote the abort still remembers it."""

    trip = _journey()
    assert _abort(trip["id"], trip["traveler_id"]).status_code == 200
    _move_status_committed(trip["id"], "PLANNED")

    answer = _abort(trip["id"], trip["traveler_id"])
    assert answer.status_code == 409, answer.text
    body = (answer.json().get("error") or {})
    assert body.get("reason_code") == "JOURNEY_ALREADY_ABORTED", body
    details = body.get("details") or {}
    assert details.get("abort_reason") == REASON, (
        f"the refusal did not read the stored reason back: {details}"
    )
    assert details.get("aborted_at"), f"the refusal named no moment: {details}"
    assert _row(trip["id"])["status"] == "PLANNED", "the refused second tap wrote the row after all"


def test_the_live_view_shape_is_unchanged_by_this_rule() -> None:
    """0 contract bytes: the field keeps its two members and its one reason code."""

    trip = _journey()
    view = _live_view_from_doors(trip["id"])["experience"]
    assert set(view) <= {"included", "reason_code"}, sorted(view)
    assert _abort(trip["id"], trip["traveler_id"]).status_code == 200
    after = _live_view_from_doors(trip["id"])["experience"]
    assert set(after) <= {"included", "reason_code"}, sorted(after)
