"""ORD-0340 - the agent-command claim gets a lease, and a dead claim stops being permanent.

Seven cells, all reading durability through a second sqlite connection (process memory is not proof):

  L1 a lapsed claim may be taken over, and the new answer lands            (acceptance 1)
  L2 an unexpired claim is refused *by name* and runs no generation        (acceptance 2)
  L3 a resurrected original cannot write over the winner's bytes           (acceptance 3)
  L4 the measured dead-end shape stops being permanent                     (acceptance 4)
  L5 the lease comes from the two budgets, and nobody deletes a receipt    (the never-do rule)
  L6 only the lease holder may fill an open claim                          (the fence, alone)
  L7 the named refusal carries ORD-0332's recitable receipt number         (added by the re-cut)

L2 deliberately keeps the sentence three already-accepted gates pin
(`test_qoder_ord0272_continuation.py`, `test_qoder_ord0287_k8_carrier.py`,
`test_qoder_ord0310_keyless_leg_receipts.py`): what is new is the name and the numbers beside it, not a
reworded failure. Cells L1/L4 shorten the lease through the module constant so the *mechanism* is what
is being judged; the production default is asserted in L5 and is not touched here.
"""
from __future__ import annotations

import contextlib
import json
import sqlite3
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

import app.main as product
from app.commerce.idempotency import canonical_request_signature
from app.core.config import settings
from app.db.models import Journey, Place
from app.db.session import SessionLocal, ensure_schema
from app.error_receipt import RECEIPT_FIELD, is_receipt_shape
from app.main import _execute_plan_generation_claimed, app
from app.schemas import GeneratePlansRequest
from app.services import seed_demo

client = TestClient(app, raise_server_exceptions=False)
DB_PATH = Path(settings.database_url.replace("sqlite:///", "", 1))
DEMO_CITY = "CNX-DEMO"
SENT_INCOMPLETE_RECEIPT = "agent idempotency record is incomplete"
_STAGE: dict = {}


@pytest.fixture(scope="module", autouse=True)
def _demo_stage() -> None:
    """The stage other gates in this family use: the product's own in-process seed, never demo/reset."""

    def _read():
        with SessionLocal() as session:
            places = int(session.scalar(
                select(func.count()).select_from(Place).where(Place.city_code == DEMO_CITY)) or 0)
            journey = session.scalars(
                select(Journey).where(Journey.city_code == DEMO_CITY).order_by(Journey.created_at)
            ).first()
            return places, journey

    ensure_schema()
    places, journey = _read()
    if places == 0 or journey is None:
        with SessionLocal() as session:
            seed_demo(session)
        places, journey = _read()
    assert places, f"SETUP: no {DEMO_CITY} catalog, so every cell would read red for the wrong reason"
    assert journey is not None, "SETUP: no demo journey to copy a window from"
    _STAGE.clear()
    _STAGE.update({
        "city_code": journey.city_code, "timezone": journey.timezone,
        "start_at": journey.start_at, "end_at": journey.end_at,
        "planning_mode": journey.planning_mode,
        "budget": {"amount_minor": journey.budget_amount_minor,
                   "currency": journey.budget_currency},
    })


def _journey() -> str:
    traveler = client.post("/api/v1/travelers",
                           json={"display_name": "ord0340", "timezone": "Asia/Shanghai"})
    assert traveler.status_code == 201, traveler.text
    journey = client.post("/api/v1/journeys", json={
        "traveler_id": traveler.json()["id"], "title": f"ord0340-{uuid.uuid4().hex[:8]}", **_STAGE})
    assert journey.status_code == 201, journey.text
    return str(journey.json()["id"])


def _durable(sql: str, args: tuple = ()) -> list[tuple]:
    assert DB_PATH.is_file(), f"SETUP: cannot open {DB_PATH}, so no durable reading is valid"
    with contextlib.closing(sqlite3.connect(DB_PATH, timeout=30)) as conn:
        return list(conn.execute(sql, args))


def _durable_write(sql: str, args: tuple) -> None:
    with contextlib.closing(sqlite3.connect(DB_PATH, timeout=30)) as conn:
        conn.execute(sql, args)
        conn.commit()


def _plant_claim(key: str, signature: str, *, deadline: datetime | None,
                 token: str, filled: dict | None = None) -> None:
    """Write a claim row exactly as Phase A would, in the state the cell is judging."""

    now = datetime.now(timezone.utc)
    payload = None if filled is None else json.dumps(filled, sort_keys=True)
    status = None if filled is None else 201
    _durable_write(
        "INSERT INTO idempotency_keys (key, operation, request_hash, response_status,"
        " response_json, created_at, expires_at, claim_token, claim_deadline)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (key, "plans.generate", signature, status, payload, now.isoformat(),
         (now + timedelta(days=7)).isoformat(), token,
         None if deadline is None else deadline.isoformat()))


def _receipt(key: str) -> tuple[str, int | None, str | None, str | None, str | None]:
    rows = _durable("SELECT response_status, response_json, claim_token, claim_deadline"
                    " FROM idempotency_keys WHERE key = ?", (key,))
    assert len(rows) <= 1, f"SETUP: one row per key at most, got {len(rows)}"
    if not rows:
        return "absent", None, None, None, None
    status, body, token, deadline = rows[0]
    return ("filled" if body else "unfilled"), status, body, token, deadline


def _generate(jid: str, key: str):
    return client.post(f"/api/v1/journeys/{jid}/plans/generate",
                       json={"candidate_count": 1}, headers={"Idempotency-Key": key})


def _identity(jid: str, transport: str) -> tuple[str, str]:
    key = f"plans.generate:{transport}"
    signature = canonical_request_signature({"journey_id": str(jid), "mission_id": None,
                                            "planning_mode": _STAGE["planning_mode"],
                                            "candidate_count": 1, "place_ids": None})
    return key, signature


def _plan_count(jid: str) -> int:
    return int(_durable("SELECT count(*) FROM plan_versions WHERE journey_id = ?", (jid,))[0][0])


def _envelope(response) -> dict:
    """A success on this route is a bare array, so only a rejection has an envelope."""

    payload = response.json()
    return payload if isinstance(payload, dict) else {}


def _name_of(response) -> str | None:
    return (_envelope(response).get("error") or {}).get("reason_code")


def _details_of(response) -> dict:
    return ((_envelope(response).get("error") or {}).get("details") or {})


def _message_of(response) -> str:
    return ((_envelope(response).get("error") or {}).get("message") or response.text)


def test_L1_a_lapsed_claim_is_taken_over_and_the_new_answer_lands() -> None:
    jid = _journey()
    transport = f"o340L1-{uuid.uuid4().hex[:10]}"
    key, signature = _identity(jid, transport)
    stale_token = f"dead-worker-{uuid.uuid4().hex[:8]}"
    _plant_claim(key, signature, deadline=datetime.now(timezone.utc) - timedelta(seconds=5),
                 token=stale_token)
    before = _receipt(key)
    assert before[0] == "unfilled" and before[3] == stale_token, f"SETUP: {before}"
    plans_before = _plan_count(jid)

    response = _generate(jid, transport)
    assert response.status_code == 201, f"L1: a lapsed claim must be reclaimable: {response.text[:240]}"
    state, status, body, token, _deadline = _receipt(key)
    assert (state, status) == ("filled", 201), f"L1: the new answer did not land: {(state, status)}"
    assert token != stale_token, "L1: the row still names the dead attempt as its owner"
    assert _plan_count(jid) == plans_before + 1, "L1: the take-over did not publish exactly one batch"
    assert json.loads(body)["plans"][0]["journey_id"] == jid, "L1: the receipt is not this journey's"


def test_L2_an_unexpired_lease_refuses_by_name_and_runs_no_generation() -> None:
    jid = _journey()
    transport = f"o340L2-{uuid.uuid4().hex[:10]}"
    key, signature = _identity(jid, transport)
    holder = f"live-worker-{uuid.uuid4().hex[:8]}"
    _plant_claim(key, signature, deadline=datetime.now(timezone.utc) + timedelta(seconds=60),
                 token=holder)
    plans_before = _plan_count(jid)

    response = _generate(jid, transport)
    assert response.status_code == 409, f"L2: an unexpired lease must refuse: {response.text[:240]}"
    assert _message_of(response) == SENT_INCOMPLETE_RECEIPT, (
        f"L2: the accepted sentence must not be reworded: {_message_of(response)!r}")
    assert _name_of(response) == "PLAN_GENERATION_IN_FLIGHT", (
        f"L2: the refusal is not named: {response.text[:240]}")
    details = _details_of(response)
    assert details.get("attempt_id") == holder, f"L2: attempt_id is not the durable holder: {details}"
    assert isinstance(details.get("retry_after_ms"), int) and 0 < details["retry_after_ms"] <= 60000, (
        f"L2: retry_after_ms must be the real remaining lease: {details}")
    assert _plan_count(jid) == plans_before, "L2: a refused attempt produced candidates anyway"
    assert _receipt(key)[0] == "unfilled", "L2: the refuser wrote into the holder's claim"


def test_L7_the_in_flight_refusal_carries_a_number_the_traveller_can_read_back() -> None:
    """The one cell this file could not have carried at it was written: ORD-0332 landed after it.

    My first candidate reported the lease refusal with `retry_after_ms`/`attempt_id` and, for a repeatable
    id, the honest answer was NOT-EMITTED -- the envelope had none. F's receipt is now emitted by `error()`
    itself, so the pair has to be tested together rather than assumed compatible: the same call, refused
    twice with the same transport key, must hand back the *same* recitable number, and that number must
    not be `trace_id` wearing a different name (trace_id is minted per envelope and is exactly the thing a
    traveller cannot read back).
    """

    jid = _journey()
    transport = f"o340L7-{uuid.uuid4().hex[:10]}"
    key, signature = _identity(jid, transport)
    _plant_claim(key, signature, deadline=datetime.now(timezone.utc) + timedelta(seconds=60),
                 token=f"live-worker-{uuid.uuid4().hex[:8]}")

    envelopes = {}
    for label in ("first", "second"):
        response = _generate(jid, transport)
        assert response.status_code == 409, (
            f"L7: the {label} attempt is not the named refusal: {response.text[:200]}")
        assert _name_of(response) == "PLAN_GENERATION_IN_FLIGHT", (
            f"L7: the {label} refusal is not named: {response.text[:200]}")
        envelopes[label] = _envelope(response)["error"]

    for label, body in envelopes.items():
        receipt = body.get(RECEIPT_FIELD)
        assert receipt is not None, f"L7: the {label} envelope carries no receipt field at all: {sorted(body)}"
        assert is_receipt_shape(receipt), (
            f"L7: {receipt!r} is not the shape contracts/ERROR_RECEIPT_ID_V1.md fixes: {sorted(body)}")

    first, second = envelopes["first"], envelopes["second"]
    assert first[RECEIPT_FIELD] == second[RECEIPT_FIELD], (
        f"L7: one call refused twice gave two numbers ({first[RECEIPT_FIELD]} vs {second[RECEIPT_FIELD]}) "
        "-- a traveller cannot read back whichever one they happened to be told")
    assert first["trace_id"] != second["trace_id"], (
        "L7: both envelopes share a trace_id, so the equality above would be about the wrong field")
    assert first[RECEIPT_FIELD] != first["trace_id"], "L7: the receipt is just trace_id renamed"


def test_L3_a_resurrected_original_cannot_write_over_the_winners_bytes() -> None:
    jid = _journey()
    transport = f"o340L3-{uuid.uuid4().hex[:10]}"
    key, signature = _identity(jid, transport)
    winner = {"plans": [{"id": f"winner-{uuid.uuid4().hex[:8]}", "journey_id": jid}]}
    _plant_claim(key, signature, deadline=datetime.now(timezone.utc) + timedelta(seconds=60),
                 token="winner-token", filled=winner)
    before = _receipt(key)
    assert before[0] == "filled", f"SETUP: the winner must already own the receipt: {before}"

    with SessionLocal() as session:
        journey = session.get(Journey, jid)
        request = GeneratePlansRequest(candidate_count=1)
        with pytest.raises(Exception) as caught:
            _execute_plan_generation_claimed(journey, request, [key], None,
                                            datetime.now(timezone.utc), f"ghost-{transport}")
    assert getattr(caught.value, "status_code", None) == 409, (
        f"L3: a superseded attempt must refuse, got {caught.value!r}")
    assert getattr(caught.value, "reason_code", None) == "PLAN_GENERATION_ATTEMPT_SUPERSEDED", (
        f"L3: the refusal must name what happened: {getattr(caught.value, 'reason_code', None)}")
    after = _receipt(key)
    assert after == before, f"L3: the winner's bytes moved: {before} -> {after}"


def test_L4_a_claim_that_died_mid_attempt_stops_being_permanent() -> None:
    jid = _journey()
    transport = f"o340L4-{uuid.uuid4().hex[:10]}"
    key, signature = _identity(jid, transport)
    fired: list[str] = []
    original = product._execute_plan_generation_claimed

    def die_once(*args, **kwargs):
        if not fired:
            fired.append("died")
            raise RuntimeError("injected: the worker was killed after claiming")
        return original(*args, **kwargs)

    monkey_lease = 300
    real_lease = product.PLAN_CLAIM_LEASE_MS
    product.PLAN_CLAIM_LEASE_MS = monkey_lease
    product._execute_plan_generation_claimed = die_once
    try:
        first = _generate(jid, transport)
        assert first.status_code == 500, f"L4 SETUP: the injected death must surface: {first.text[:180]}"
        died = _receipt(key)
        assert died[0] == "unfilled", f"L4 SETUP: the claim must be left open: {died}"
        plans_after_death = _plan_count(jid)

        second_while_held = _generate(jid, transport)
        assert second_while_held.status_code == 409, (
            "L4: inside the lease a second attempt must refuse, not double-run")
        assert _name_of(second_while_held) == "PLAN_GENERATION_IN_FLIGHT", (
            f"L4: held refusal must be named: {second_while_held.text[:200]}")
        assert _plan_count(jid) == plans_after_death, "L4: a refused attempt published candidates"

        time.sleep((monkey_lease + 120) / 1000.0)
        third = _generate(jid, transport)
        assert third.status_code == 201, (
            f"L4: the dead key must stop being permanent: {third.text[:240]}")
        assert _name_of(third) is None, f"L4: a success must not carry a refusal name: {third.text[:120]}"
        state, status, _body, token, _d = _receipt(key)
        assert (state, status) == ("filled", 201), f"L4: the reclaim did not fill the receipt: {state}"
        assert token != died[3], "L4: the row still names the worker that died"
        assert _plan_count(jid) == plans_after_death + 1, "L4: exactly one batch must land after the reclaim"
    finally:
        product._execute_plan_generation_claimed = original
        product.PLAN_CLAIM_LEASE_MS = real_lease


def test_L6_only_the_lease_holder_may_fill_an_open_claim() -> None:
    """The discriminating cell for the fence: the row is still open, so the NULL guard alone
    would let a stranger fill it. Only `claim_token` tells these two attempts apart."""

    jid = _journey()
    transport = f"o340L6-{uuid.uuid4().hex[:10]}"
    key, signature = _identity(jid, transport)
    holder = f"holder-{uuid.uuid4().hex[:8]}"
    _plant_claim(key, signature, deadline=datetime.now(timezone.utc) + timedelta(seconds=60),
                 token=holder)
    before = _receipt(key)
    assert before[0] == "unfilled" and before[3] == holder, f"SETUP: {before}"
    plans_before = _plan_count(jid)

    from app.db.models import Journey as _J
    with SessionLocal() as session:
        journey = session.get(_J, jid)
        request = GeneratePlansRequest(candidate_count=1)
        with pytest.raises(Exception) as caught:
            _execute_plan_generation_claimed(journey, request, [key], None,
                                            datetime.now(timezone.utc), f"stranger-{transport}")
    assert getattr(caught.value, "reason_code", None) == "PLAN_GENERATION_ATTEMPT_SUPERSEDED", (
        f"L6: a stranger filled an open claim it does not hold: {caught.value!r}")
    after = _receipt(key)
    assert after[0] == "unfilled" and after[3] == holder, (
        f"L6: the stranger wrote into the holder's claim: {before} -> {after}")
    assert _plan_count(jid) == plans_before, "L6: a refused attempt must not publish candidates"


def test_L5_the_lease_comes_from_the_two_budgets_and_no_receipt_is_ever_deleted() -> None:
    assert product.PLAN_CLAIM_LEASE_MS == int(settings.write_deadline_ms) + int(
        settings.write_busy_timeout_ms), (
        "L5: the lease must stay derived from the budgets this route already obeys, not a new magic number")
    assert product.PLAN_CLAIM_LEASE_MS > 0

    jid = _journey()
    transport = f"o340L5-{uuid.uuid4().hex[:10]}"
    key, signature = _identity(jid, transport)
    _plant_claim(key, signature, deadline=datetime.now(timezone.utc) - timedelta(seconds=5),
                 token="whatever")
    rows_before = int(_durable("SELECT count(*) FROM idempotency_keys")[0][0])
    response = _generate(jid, transport)
    assert response.status_code == 201, f"L5: reclaim path must answer: {response.text[:180]}"
    rows_after = int(_durable("SELECT count(*) FROM idempotency_keys")[0][0])
    assert rows_after == rows_before, (
        f"L5: recovery must not forget receipts: {rows_before} -> {rows_after}")
    assert _receipt(key)[0] == "filled"
    _state, _status, _body, token, deadline = _receipt(key)
    assert token and deadline, "L5: the filled receipt lost its attempt identity or its clock"
