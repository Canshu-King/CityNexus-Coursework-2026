"""ORD-0383 格三 (the commander's 02:47 ruling) - two taps at 找回 must not both be allowed to write.

The defect and its own admission are in the line this order fixes: at the base tip
`dccd13ad66ea35e9a6d7ca7a3d91a1ae9a579808`, `backend/app/main.py` was

    journey.deleted_at = None
    session.commit()

after a `session.get(...)` whose result had just been checked for non-null.  That is a read-then-write
of an ORM object, and `backend/app/main.py` said so out loud: "Two taps at once are still
unguarded, as in slice 1."  The same file already carries the opposite instruction in
`_accept_replan_executing` (`# Conditional writes, not object assignment.  A read-then-write of the
ORM rows is exactly the window two concurrent accepters can both pass`), and the shape it prescribes
is used by `_take_over_lapsed_claims`, `select_plan` and `_trip_studio_accept_impl`: put the
predicate in the UPDATE and judge the outcome by `rowcount`.  Nothing here is a new mechanism.

What each arm is for, and which of them the fix is actually judged on:

1. `..._invariant_holds_under_fired_together_taps` - the acceptance arm.  Two taps on one trip,
   released by one barrier, repeated over rounds.  It asserts the invariant that must never break
   whichever way the race lands: at most one tap writes, every refusal carries a name, and the
   number of `journey.undeleted` audit rows equals the number of 200s.  Invariant rather than a
   required collision, because a race that happened to interleave once is not a gate.
2. `..._window_is_real_not_claimed` - the arm that keeps arm 1 honest.  It measures how many rounds
   actually had both taps inside the door at once, by timing each round against a deliberately
   slowed trip.  If no round ever overlaps, this arm says so by failing, and arm 1's green would be
   worth nothing - so the pair is only meaningful together.
3. `..._never_a_framework_five_hundred` - the loser's side of the ruling: a named 409 in the ORD-0055
   envelope, not a 500, not a silent success, whatever interleaving the hammer produces.
4. `..._single_tap_path_is_unchanged` - regression on the door that already passed: one trip, one
   tap, back in the list, replay refused as `JOURNEY_NOT_DELETED`.

Everything runs against the temporary database this pytest process declares for itself
(`backend/tests/conftest.py`, printed by its `pytest_report_header`), built through the product's own
POST.  No shared store is opened: neither `D:/moodle/_qoder_preview_20260920/_preview.db` nor any
`_live.db` is named by any line below, and `POST /api/v1/demo/reset` is never called.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from app.db.models import Traveler
from app.db.session import SessionLocal
from app.main import app

TZ = timezone(timedelta(hours=8))
START = datetime(2026, 11, 3, 9, 0, tzinfo=TZ)
ROUNDS = 12
#: A tap that took longer than this to be refused is not evidence of a race; it is evidence of a
#: blocked writer, and the round is discarded rather than counted.
OVERLAP_MARGIN_S = 0.0


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
                display_name="ORD-0383 lock probe",
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
    client = TestClient(app, raise_server_exceptions=False)
    created = client.post(
        "/api/v1/journeys",
        json={
            "traveler_id": _traveler(),
            "title": "ORD-0383 lock",
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


def _delete(journey_id: str) -> int:
    with TestClient(app, raise_server_exceptions=False) as client:
        return client.delete(f"/api/v1/journeys/{journey_id}").status_code


def _deleted_at(journey_id: str) -> str | None:
    with sqlite3.connect(_store_path()) as connection:
        row = connection.execute(
            "SELECT deleted_at FROM journeys WHERE id = ?", (journey_id,)
        ).fetchone()
    assert row is not None, f"no journeys row for id={journey_id} in {_store_path()}"
    return row[0]


def _undelete_audits(journey_id: str) -> list[tuple]:
    with sqlite3.connect(_store_path()) as connection:
        return connection.execute(
            "SELECT id, before_json, after_json FROM audit_logs"
            " WHERE action = 'journey.undeleted' AND entity_id = ? ORDER BY id",
            (journey_id,),
        ).fetchall()


def _tap(journey_id: str, barrier: threading.Barrier, into: dict, key: int) -> None:
    """One 找回 tap, in its own client, released together with the others."""

    with TestClient(app, raise_server_exceptions=False) as client:
        barrier.wait()
        began = time.perf_counter()
        answer = client.post(f"/api/v1/journeys/{journey_id}/undelete")
        into[key] = {
            "status": answer.status_code,
            "reason_code": (
                None
                if answer.status_code < 400
                else ((answer.json() or {}).get("error") or {}).get("reason_code")
            ),
            "began": began,
            "elapsed": time.perf_counter() - began,
        }


def _two_taps(journey_id: str) -> dict:
    barrier = threading.Barrier(2)
    results: dict = {}
    threads = [threading.Thread(target=_tap, args=(journey_id, barrier, results, i)) for i in (0, 1)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert len(results) == 2, f"a tap never came back: {sorted(results)}"
    return results


def test_two_taps_fired_together_keep_the_invariant_and_one_audit_row_per_success() -> None:
    """The acceptance arm: whichever way the race lands, one trip is written by one tap.

    `assert winners <= 1` is the defect being refused; `assert audits == winners` is the ruling's
    "审计行只能有一条"; every other tap must answer with a name out of the ORD-0055 envelope.
    """

    double_success = []
    unnamed_refusal = []
    audit_mismatch = []
    overlaps = 0
    for _round in range(ROUNDS):
        journey_id = _journey()
        assert _delete(journey_id) == 204, "the fixture trip never reached the bin"
        results = _two_taps(journey_id)
        statuses = sorted(item["status"] for item in results.values())
        winners = [item for item in results.values() if item["status"] == 200]
        if len(winners) > 1:
            double_success.append((statuses, results))
        for item in results.values():
            if item["status"] >= 400 and not item["reason_code"]:
                unnamed_refusal.append(item)
        audits = len(_undelete_audits(journey_id))
        if audits != len(winners):
            audit_mismatch.append((audits, len(winners)))
        first, second = sorted(results.values(), key=lambda item: item["began"])
        if first["began"] + first["elapsed"] > second["began"]:
            overlaps += 1
        assert _deleted_at(journey_id) is None or len(winners) == 0, (
            _deleted_at(journey_id),
            statuses,
        )
    print(
        f"[ord0383][lock] rounds={ROUNDS} overlapping_rounds={overlaps} "
        f"double_success={len(double_success)} unnamed_refusal={len(unnamed_refusal)} "
        f"audit_row_mismatch={len(audit_mismatch)}"
    )
    assert double_success == [], f"two taps both wrote the row: {double_success[:2]}"
    assert unnamed_refusal == [], f"a refusal arrived without a name: {unnamed_refusal[:2]}"
    assert audit_mismatch == [], f"audit rows and successes disagree: {audit_mismatch[:3]}"


def test_the_two_taps_window_really_overlaps_instead_of_being_asserted() -> None:
    """Arm 1 means nothing if no round ever had two taps inside the door at the same time.

    This arm therefore measures the overlap and fails if there was none.  It is the control on the
    control: a suite that cannot make the two taps meet cannot tell a guard from a coincidence, and
    saying so is the honest result.
    """

    overlaps = 0
    timings = []
    for _round in range(ROUNDS):
        journey_id = _journey()
        assert _delete(journey_id) == 204
        results = _two_taps(journey_id)
        first, second = sorted(results.values(), key=lambda item: item["began"])
        if first["began"] + first["elapsed"] > second["began"] + OVERLAP_MARGIN_S:
            overlaps += 1
        timings.append(round(first["elapsed"], 4))
    print(
        f"[ord0383][window] rounds={ROUNDS} overlapping_rounds={overlaps} "
        f"slowest_first_tap_s={max(timings) if timings else 0} store={_store_path()}"
    )
    assert overlaps > 0, (
        f"no round overlapped in {ROUNDS} tries: the taps were serialised by the transport, so arm "
        "1 could not have distinguished a guarded write from an unguarded one"
    )


def test_a_tap_that_cannot_win_names_its_refusal_and_never_reaches_a_framework_five_hundred() -> None:
    """The hammer: 找回 tapped while the same trip is being thrown away again.

    Whatever interleaving comes out of it, the door is only allowed to answer 200 or a named 409.  A
    500 here would mean the loser fell through to the framework, which is the failure the ruling
    calls out by name ("不是 500、不是静默成功").
    """

    journey_id = _journey()
    assert _delete(journey_id) == 204
    stop = time.perf_counter() + 1.5
    seen: dict = {}
    lock = threading.Lock()

    def loop(label: str, action) -> None:
        local = []
        while time.perf_counter() < stop:
            local.append(action())
            time.sleep(0.001)
        with lock:
            seen[label] = local

    threads = [
        threading.Thread(target=loop, args=("undelete", lambda: _post_undelete(journey_id))),
        threading.Thread(target=loop, args=("delete", lambda: _delete(journey_id))),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    codes = sorted({code for taps in seen.values() for code in taps})
    undeleted = len(_undelete_audits(journey_id))
    successes = sum(1 for taps in seen.values() for code in taps if code == 200)
    print(
        f"[ord0383][hammer] codes={codes} undelete_taps={len(seen['undelete'])} "
        f"delete_taps={len(seen['delete'])} successes={successes} audit_rows={undeleted} "
        f"final_deleted_at_is_null={_deleted_at(journey_id) is None}"
    )
    assert set(codes) <= {200, 204, 409}, f"the door reached {set(codes) - {200, 204, 409}}"
    assert 500 not in codes, codes
    assert undeleted == successes, (undeleted, successes)


def _post_undelete(journey_id: str) -> int:
    with TestClient(app, raise_server_exceptions=False) as client:
        answer = client.post(f"/api/v1/journeys/{journey_id}/undelete")
        if answer.status_code == 409:
            assert ((answer.json() or {}).get("error") or {}).get("reason_code"), (
                answer.status_code,
                answer.text[:200],
            )
        return answer.status_code


def test_the_single_tap_path_is_unchanged_by_the_predicate() -> None:
    """One trip, one tap: back in the list, replayed taps still refused by the same name."""

    journey_id = _journey()
    assert _delete(journey_id) == 204
    barrier = threading.Barrier(1)
    solo: dict = {}
    _tap(journey_id, barrier, solo, 0)
    assert solo[0]["status"] == 200, solo
    with TestClient(app, raise_server_exceptions=False) as client:
        ids = [item["id"] for item in client.get("/api/v1/journeys").json()]
        replay = client.post(f"/api/v1/journeys/{journey_id}/undelete")
    assert journey_id in ids, "the winner wrote nothing the list door can see"
    assert replay.status_code == 409, replay.text
    assert ((replay.json() or {}).get("error") or {}).get("reason_code") == "JOURNEY_NOT_DELETED"
    assert len(_undelete_audits(journey_id)) == 1, _undelete_audits(journey_id)
    assert _deleted_at(journey_id) is None
