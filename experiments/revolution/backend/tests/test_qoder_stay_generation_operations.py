"""ORD-0057 - the generation contract the arbitration approved (`5749459724` §1).

Measured before this file existed: a cold `POST /stay-options/generate` held its HTTP
request for ~115s and came back with `top_k: []`.  A traveller on a phone cannot wait on
that honestly - no way to leave, no way to stop, and a second tap started a second chain.

Each gate below owns one clause of the approved contract, and each is written so the
implementation cannot pass it by moving the same lie somewhere else:

* acceptance answers before any provider is contacted, and the wait is the client's choice;
* one journey plus one identical question is one operation, enforced by a partial unique
  index rather than by politeness;
* the four terminal outcomes stay distinguishable, so `top_k: []` is never "success";
* abandon answers what actually happened - `ABANDON_REQUESTED` while a call is on the wire,
  `ABANDONED` once the worker resolves it, and never `ABANDONED` for work already delivered;
* nothing the abandoned job produced becomes visible;
* `phase` is only ever a stage the code genuinely entered, and no percentage exists;
* the synchronous door is the same operation waited on, not a second implementation.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.db.models import Journey, JourneyTripContext, StayGenerationOperation, Traveler
from app.db.session import SessionLocal
from app.integrations import stay_osm
from app.main import app
from app import stay_mobility
from app.stay_generation_operations import (
    ABANDONED,
    ABANDON_REQUESTED,
    COMPLETED,
    FAILED,
    OPEN_STATUSES,
    PHASES,
    QUEUED,
    RUNNING,
    TransitionRefused,
    abandon_operation,
    generation_fingerprint,
    mark,
    now_iso,
    PhaseReporter,
    reconcile_interrupted_operations,
    retry_after_ms,
    submit_operation,
    wait_for_operation,
)

client = TestClient(app)
TZ = timezone(timedelta(hours=8))
OUTCOMES = {
    "OPTIONS_FOUND",
    "NO_OFFERS_FROM_PROVIDER",
    "ALL_CANDIDATES_REJECTED",
    "NOT_APPLICABLE_ZERO_NIGHTS",
}


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return self._payload


class _SlowHttp:
    """A provider shaped like the real chain, slow enough to abandon while it runs."""

    def __init__(self, *, delay_s: float = 0.4, hotels: bool = False) -> None:
        self.delay_s = delay_s
        self.hotels = hotels
        self.calls: list[str] = []
        self._lock = threading.Lock()

    def _visit(self, label: str) -> None:
        with self._lock:
            self.calls.append(label)
        if self.delay_s:
            time.sleep(self.delay_s)

    def get(self, url, params=None, headers=None):
        if "photon" in url:
            self._visit("photon")
            return _Response({"features": [{"geometry": {"coordinates": [118.1, 24.5]}}]})
        self._visit("nominatim")
        return _Response([])

    def post(self, url, data=None, headers=None):
        self._visit("overpass")
        if not self.hotels:
            raise httpx.TimeoutException("overpass did not answer")
        return _Response(
            {
                "elements": [
                    {
                        "type": "node",
                        "id": 9001,
                        "lat": 24.49,
                        "lon": 118.12,
                        "tags": {"name": "Fake Hotel One", "tourism": "hotel"},
                    }
                ]
            }
        )

    def close(self) -> None:
        return None

    def count(self, label: str) -> int:
        with self._lock:
            return self.calls.count(label)


def _use(monkeypatch, http: _SlowHttp) -> None:
    real = stay_osm.OsmAccommodationProvider
    monkeypatch.setattr(stay_mobility, "OsmAccommodationProvider", lambda: real(client=http))


import pytest  # noqa: E402
from demo_stage import demo_reset_headers  # noqa: E402  (ORD-0389)


@pytest.fixture(autouse=True)
def _no_worker_outlives_its_test(monkeypatch):
    """Wait for the operations *this test* started before the next one begins.

    A worker whose fake provider has already been un-patched would keep running against the
    real network, and a generation holds SQLite's single writer for as long as it runs - so
    an undrained job turns the next test into a lock timeout.  Rows a test inserts by hand
    are deliberately not waited for: they have no worker at all, which is the point of the
    tests that create them.

    The `monkeypatch` parameter **is** the fix, and it is unused on purpose: fixture teardown
    runs in reverse setup order, so an autouse fixture with no dependencies was set up first
    and torn down *last* - after the provider patches had already been reverted. Measured on
    `21a09e8` as a two-file pair (`stay_generation_operations` + `trip_shape_and_budget_reference`,
    identical collection order both times): 31 failed / 8 passed in 62 s with
    `database is locked` everywhere, against 38 passed in 13.77 s on `924461e`. Requesting the
    fixture makes this one drain while its own fakes are still installed.

    Before failing it, the stragglers are abandoned: a job this test started must not outlive
    the test, and an abandoned job stops writing - which keeps the next module measurable
    instead of leaving the suite to inherit the lock. That is a cleanup, not a softening: the
    assertion below still fires.
    """

    _started.clear()
    yield
    deadline = time.monotonic() + 30.0
    pending: list[str] = []
    while time.monotonic() < deadline:
        pending = [item for item in _started if not _settled(item)]
        if not pending:
            return
        for operation_id in pending:
            wait_for_operation(operation_id, deadline_seconds=2.0)
    still_open = [item for item in _started if not _settled(item)]
    for operation_id in still_open:
        with SessionLocal() as session:
            row = session.get(StayGenerationOperation, operation_id)
            if row is not None and row.journey_id:
                abandon_operation(session, operation_id)
    raise AssertionError(
        f"operations still open: {still_open} - a worker outlived its provider patches, so "
        "the next module runs against a database this one is still writing to"
    )


def _started_ids() -> None:
    return None


def _settled(operation_id: str) -> bool:
    with SessionLocal() as session:
        row = session.get(StayGenerationOperation, operation_id)
    return row is None or row.status not in OPEN_STATUSES


def _journey(
    *, days: int = 3, traveler: str | None = None, stay_budget: dict | None = None
) -> str:
    """Create a journey.  Passing `traveler` skips the reset, which would otherwise wipe a
    journey an earlier call in the same test still needs.  Passing `stay_budget` declares one,
    which ORD-0355 needs for the cases that must be read as a money constraint."""

    if traveler is None:
        traveler = client.post("/api/v1/demo/reset", headers=demo_reset_headers()).json()["traveler_id"]
    start = datetime(2026, 11, 3, 9, 0, tzinfo=TZ)
    created = client.post(
        "/api/v1/journeys",
        json={
            "traveler_id": traveler,
            "title": f"{days}d operation",
            "city_code": "CN-XIAMEN",
            "timezone": "Asia/Shanghai",
            "start_at": start.isoformat(),
            "end_at": (start + timedelta(days=days - 1, hours=8)).isoformat(),
            "budget": {"amount_minor": 90000, "currency": "CNY"},
            "planning_mode": "balanced",
            **({"stay_budget": stay_budget} if stay_budget else {}),
        },
    )
    assert created.status_code == 201, created.text
    return created.json()["id"]


def _traveler_id() -> str:
    """The traveler the last reset seeded, without resetting again."""

    with SessionLocal() as session:
        return session.scalar(select(Traveler.id))


_started: list[str] = []


def _submit(journey_id: str, **body) -> dict:
    response = client.post(f"/api/v1/journeys/{journey_id}/stay-generations", json=body)
    assert response.status_code == 202, response.text
    payload = response.json()
    _started.append(payload["operation_id"])
    return payload


def _read(journey_id: str, operation_id: str) -> dict:
    response = client.get(f"/api/v1/journeys/{journey_id}/stay-generations/{operation_id}")
    assert response.status_code == 200, response.text
    return response.json()


def _abandon(journey_id: str, operation_id: str) -> dict:
    response = client.post(
        f"/api/v1/journeys/{journey_id}/stay-generations/{operation_id}/abandon", json={}
    )
    assert response.status_code == 200, response.text
    return response.json()


def _wait(journey_id: str, operation_id: str, *, seconds: float = 30.0) -> dict:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        body = _read(journey_id, operation_id)
        if body["terminal"]:
            return body
        time.sleep(0.05)
    raise AssertionError(f"operation {operation_id} never reached a terminal state")


def _open_job_rows(journey_id: str) -> int:
    with SessionLocal() as session:
        return (
            session.scalar(
                select(func.count(StayGenerationOperation.id)).where(
                    StayGenerationOperation.journey_id == journey_id
                )
            )
            or 0
        )


def _generated_rows(journey_id: str) -> int:
    with SessionLocal() as session:
        return (
            session.scalar(
                select(func.count(stay_mobility.StayJourneyOption.id)).where(
                    stay_mobility.StayJourneyOption.journey_id == journey_id
                )
            )
            or 0
        )


# ------------------------------------------------------------------ acceptance


def test_acceptance_answers_while_the_provider_is_still_being_asked(monkeypatch) -> None:
    """The whole point of the contract: 202 before the chain, not after it."""

    http = _SlowHttp(delay_s=0.6)
    _use(monkeypatch, http)
    journey_id = _journey()

    started = time.perf_counter()
    body = _submit(journey_id)
    accepted_ms = (time.perf_counter() - started) * 1000.0

    assert accepted_ms < 1000.0, f"acceptance took {accepted_ms:.0f}ms with the chain still running"
    assert body["status"] in {QUEUED, RUNNING}, body
    assert body["operation_id"] and body["journey_id"] == journey_id, body
    assert body["status_url"].endswith(f"/stay-generations/{body['operation_id']}"), body
    assert body["abandon_url"] == f"{body['status_url']}/abandon", body
    assert body["retry_after_ms"] == 1000, body
    # Nothing had been asked yet when the answer was written - which is the difference
    # between "off the request path" and a faster-looking lie.
    assert http.count("photon") == 0, http.calls
    finished = _wait(journey_id, body["operation_id"])
    assert finished["status"] == COMPLETED, finished
    assert finished["outcome"] in OUTCOMES, finished


def test_three_taps_of_the_same_question_start_exactly_one_chain(monkeypatch) -> None:
    http = _SlowHttp(delay_s=0.1)
    _use(monkeypatch, http)
    journey_id = _journey()

    first = _submit(journey_id, candidate_count=5)
    second = _submit(journey_id, candidate_count=5)
    third = _submit(journey_id, candidate_count=5)

    assert first["operation_id"] == second["operation_id"] == third["operation_id"], (
        first,
        second,
        third,
    )
    assert second["reused"] is True and third["reused"] is True, (second, third)
    _wait(journey_id, first["operation_id"])
    assert http.count("photon") == 1, http.calls
    assert http.count("overpass") == 3, http.calls  # one chain = three mirrors, once
    assert _open_job_rows(journey_id) == 1, "a duplicate tap must not leave duplicate jobs"


def test_a_different_question_is_a_different_operation(monkeypatch) -> None:
    """Dedupe is per journey *and* per request; a new question may not inherit an answer."""

    http = _SlowHttp(delay_s=0.0)
    _use(monkeypatch, http)
    journey_id = _journey()

    cheapest = _submit(journey_id, sort="CHEAPEST", candidate_count=5)
    balanced = _submit(journey_id, sort="BEST_FOR_JOURNEY", candidate_count=5)
    assert cheapest["operation_id"] != balanced["operation_id"], (cheapest, balanced)
    assert cheapest["reused"] is False and balanced["reused"] is False, (cheapest, balanced)


# ------------------------------------------------------------------- the terminal answer


def test_the_terminal_answer_keeps_the_four_cases_apart(monkeypatch) -> None:
    http = _SlowHttp(delay_s=0.0)
    _use(monkeypatch, http)
    journey_id = _journey()
    finished = _wait(journey_id, _submit(journey_id)["operation_id"])

    assert finished["status"] == COMPLETED, finished
    assert finished["outcome"] == "NO_OFFERS_FROM_PROVIDER", finished
    assert finished["result"]["top_k"] == [], finished
    # `top_k: []` alone used to be readable as "done, nothing to show"; the outcome says
    # which of the four facts the traveller is looking at.
    assert finished["result"]["generation"]["provider_attempts"], finished["result"]["generation"]
    assert finished["phase"] in PHASES, finished


def test_a_trip_that_needs_no_stay_completes_as_not_applicable(monkeypatch) -> None:
    http = _SlowHttp(delay_s=0.0)
    _use(monkeypatch, http)
    journey_id = _journey(days=1)  # same window, no overnight boundary
    finished = _wait(journey_id, _submit(journey_id)["operation_id"])
    assert finished["outcome"] == "NOT_APPLICABLE_ZERO_NIGHTS", finished


def test_every_hotel_conflicting_with_the_itinerary_completes_as_rejected(monkeypatch) -> None:
    http = _SlowHttp(delay_s=0.0, hotels=True)
    _use(monkeypatch, http)

    class _Conflict(stay_mobility.StayItineraryConflict):
        pass

    def _refuse(*_args, **_kwargs):
        raise _Conflict("pinned window")

    monkeypatch.setattr(stay_mobility, "build_candidate_nodes", _refuse)
    # ORD-0355: this answer is the money-naming one, so it now requires a budget the traveller
    # really declared.  Without it the same trip reports STAY_BUDGET_NOT_DECLARED instead.
    journey_id = _journey(stay_budget={"amount_minor": 30000, "currency": "CNY"})
    finished = _wait(journey_id, _submit(journey_id)["operation_id"])
    assert finished["outcome"] == "ALL_CANDIDATES_REJECTED", finished
    assert finished["result"]["top_k"] == [], finished


def test_a_provider_answer_produces_options_and_records_the_stage_reached(monkeypatch) -> None:
    http = _SlowHttp(delay_s=0.0, hotels=True)
    _use(monkeypatch, http)
    journey_id = _journey()
    finished = _wait(journey_id, _submit(journey_id)["operation_id"])
    assert finished["status"] == COMPLETED, finished
    assert finished["outcome"] in OUTCOMES, finished
    assert finished["result"] and "top_k" in finished["result"], finished
    assert finished["error"] is None, finished


# --------------------------------------------------------------------- abandon


def test_abandon_says_requested_while_the_call_is_still_on_the_wire(monkeypatch) -> None:
    """`ABANDONED` before the provider returns would be a claim, not a fact."""

    http = _SlowHttp(delay_s=0.2)
    _use(monkeypatch, http)
    journey_id = _journey()
    body = _submit(journey_id)
    operation_id = body["operation_id"]

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if _read(journey_id, operation_id)["status"] == RUNNING:
            break
        time.sleep(0.02)
    else:
        raise AssertionError(f"worker never entered RUNNING: {http.calls}")

    stopped = _abandon(journey_id, operation_id)
    assert stopped["status"] == ABANDON_REQUESTED, stopped
    assert stopped["terminal"] is False, stopped
    assert stopped["abandoned"] is False, stopped

    finished = _wait(journey_id, operation_id)
    assert finished["status"] == ABANDONED, finished
    assert finished["result"] is None, "an abandoned job may not hand back a result"
    assert finished["outcome"] is None, finished
    # The honest half: nothing it produced became a traveller-visible option row either.
    assert _generated_rows(journey_id) == 0, "abandon rolled back, and must not have"


def test_abandon_is_idempotent_and_never_repeats_a_transition(monkeypatch) -> None:
    http = _SlowHttp(delay_s=0.2)
    _use(monkeypatch, http)
    journey_id = _journey()
    operation_id = _submit(journey_id)["operation_id"]

    seen = [_abandon(journey_id, operation_id) for _ in range(3)]
    for body in seen:
        # Each call answers the state as it is, and none of them may claim a result or an
        # error for work that was stopped.
        assert body["status"] in {ABANDON_REQUESTED, ABANDONED}, body
        assert body["result"] is None, body
        assert body["error"] is None, body
    assert seen[0]["operation_id"] == operation_id, seen[0]

    finished = _wait(journey_id, operation_id)
    assert finished["status"] == ABANDONED, finished
    finished_at = finished["finished_at"]
    again = _abandon(journey_id, operation_id)
    assert again["status"] == ABANDONED, again
    assert again["terminal"] is True, again
    # The single terminal stamp is the proof the transition ran once: a repeated abandon that
    # re-entered the machine would move it.
    assert again["finished_at"] == finished_at, (again["finished_at"], finished_at)


def test_abandon_after_delivery_reports_the_delivery_instead_of_a_fake_cancellation(
    monkeypatch,
) -> None:
    http = _SlowHttp(delay_s=0.0)
    _use(monkeypatch, http)
    journey_id = _journey()
    operation_id = _submit(journey_id)["operation_id"]
    finished = _wait(journey_id, operation_id)
    assert finished["status"] == COMPLETED, finished

    after = _abandon(journey_id, operation_id)
    assert after["status"] == COMPLETED, "a delivered result cannot be un-delivered"
    assert after["abandoned"] is False, after
    assert _read(journey_id, operation_id)["status"] == COMPLETED, after


def test_abandoning_an_unknown_operation_is_a_404_and_not_a_quiet_success() -> None:
    journey_id = _journey()
    response = client.post(
        f"/api/v1/journeys/{journey_id}/stay-generations/{uuid4()}/abandon", json={}
    )
    assert response.status_code == 404, response.text
    assert response.json()["error"]["message_key"] == "resource.not_found", response.json()


def test_the_state_machine_has_no_door_from_abandoned_work_back_to_a_result() -> None:
    """`ABANDON_REQUESTED -> COMPLETED` is the dishonesty the arbitration named.

    The same row is then moved along the transition that *is* legal, so this cannot pass by
    the guard refusing everything.
    """

    journey_id = _journey()
    stamp = datetime.now(timezone.utc).isoformat()
    with SessionLocal() as session:
        operation_id = str(uuid4())
        session.add(
            StayGenerationOperation(
                id=operation_id,
                journey_id=journey_id,
                fingerprint="state-machine",
                status=ABANDON_REQUESTED,
                queued_at=stamp,
                started_at=stamp,
                abandon_requested_at=stamp,
                updated_at=stamp,
            )
        )
        session.commit()

        try:
            mark(session, operation_id, COMPLETED, from_statuses=(ABANDON_REQUESTED,))
        except TransitionRefused as exc:
            assert ABANDON_REQUESTED in str(exc), exc
        else:
            raise AssertionError("an abandoned operation was allowed to complete")
        assert (
            mark(
                session,
                operation_id,
                ABANDONED,
                from_statuses=(ABANDON_REQUESTED,),
                finished_at=now_iso(),
            )
            is True
        )
        row = session.get(StayGenerationOperation, operation_id)
        assert row.status == ABANDONED, row.status
        assert row.result_json == "{}", row.result_json
        # And the write is *guarded*, not merely legal-looking: a job that is no longer
        # RUNNING cannot be completed by anything, which is what resolves a race with an
        # abandon into exactly one answer.
        assert mark(session, operation_id, COMPLETED, from_statuses=(RUNNING,)) is False
        assert session.get(StayGenerationOperation, operation_id).status == ABANDONED


def test_a_phase_is_only_ever_a_stage_the_code_actually_entered() -> None:
    """No percentage, and no stage nobody visited (arbitration's wording, literally).

    A progress bar with an unprovable denominator is worse than none, so the reporter
    accepts exactly the four named stages and refuses anything else without writing it.
    """

    with SessionLocal() as session:
        reporter = PhaseReporter("operation-that-does-not-exist", session)
        reporter("PERCENT_50")
        reporter("ALMOST_DONE")
        assert reporter.last is None and reporter.writes == 0, (reporter.last, reporter.writes)
        # A real stage is accepted even for a row that is not there - the write matches no
        # row, and the recorded stage is still one the code can claim to have entered.
        reporter("DISCOVERY")
        assert reporter.last == "DISCOVERY", reporter.last
        session.rollback()


def test_only_one_open_job_per_journey_and_fingerprint_survives_a_race() -> None:
    """The database, not an in-process set, is what refuses the second chain."""

    from sqlalchemy.exc import IntegrityError

    journey_id = _journey()
    with SessionLocal() as session:
        journey = session.get(Journey, journey_id)
        context = session.get(JourneyTripContext, journey_id)
        assert journey is not None and context is not None
        fingerprint = generation_fingerprint(journey, {"sort": "BEST_FOR_JOURNEY"})
        stamp = datetime.now(timezone.utc).isoformat()
        session.add(
            StayGenerationOperation(
                id=str(uuid4()),
                journey_id=journey_id,
                fingerprint=fingerprint,
                status=QUEUED,
                queued_at=stamp,
                updated_at=stamp,
            )
        )
        session.commit()
        try:
            session.add(
                StayGenerationOperation(
                    id=str(uuid4()),
                    journey_id=journey_id,
                    fingerprint=fingerprint,
                    status=QUEUED,
                    queued_at=stamp,
                    updated_at=stamp,
                )
            )
            session.commit()
        except IntegrityError:
            session.rollback()
        else:
            raise AssertionError("two open jobs for one identical request were allowed")

        # A different question is allowed to run beside it, and a finished one frees the slot.
        session.add(
            StayGenerationOperation(
                id=str(uuid4()),
                journey_id=journey_id,
                fingerprint=fingerprint + "-other",
                status=QUEUED,
                queued_at=stamp,
                updated_at=stamp,
            )
        )
        session.commit()
        session.add(
            StayGenerationOperation(
                id=str(uuid4()),
                journey_id=journey_id,
                fingerprint=fingerprint,
                status=COMPLETED,
                queued_at=stamp,
                updated_at=stamp,
                finished_at=stamp,
            )
        )
        session.commit()
        open_count = (
            session.scalar(
                select(func.count(StayGenerationOperation.id)).where(
                    StayGenerationOperation.journey_id == journey_id,
                    StayGenerationOperation.status.in_(OPEN_STATUSES),
                )
            )
            or 0
        )
        assert open_count == 2, open_count  # the two distinct fingerprints, not three


# ------------------------------------------------------------- poll rules and reload


def test_retry_after_grows_with_the_wait_and_stops_at_a_terminal_state() -> None:
    class _Row:
        def __init__(self, status: str, elapsed_seconds: float) -> None:
            self.status = status
            self.queued_at = (
                datetime.now(timezone.utc) - timedelta(seconds=elapsed_seconds)
            ).isoformat()

    assert retry_after_ms(_Row(RUNNING, 2)) == 1000
    assert retry_after_ms(_Row(RUNNING, 15)) == 2000
    assert retry_after_ms(_Row(ABANDON_REQUESTED, 45)) == 5000
    assert retry_after_ms(_Row(COMPLETED, 45)) == 0
    assert retry_after_ms(_Row(FAILED, 45)) == 0
    assert retry_after_ms(_Row(ABANDONED, 45)) == 0


def test_a_reload_reads_the_stored_operation_and_a_repeat_post_reuses_it(monkeypatch) -> None:
    """While a job is open, a client recovers by *reading* it - not by asking twice.

    Once the job is terminal the dedupe releases on purpose: "generate again" after an
    answer is a new question, and a contract that refused it would strand a traveller who
    genuinely wants a fresh chain.
    """

    http = _SlowHttp(delay_s=0.2)
    _use(monkeypatch, http)
    journey_id = _journey()
    operation_id = _submit(journey_id)["operation_id"]

    # What a reload does: it has the id it stored, and it reads.  No second POST involved.
    assert _read(journey_id, operation_id)["operation_id"] == operation_id
    # And if a client did POST the same question again while it is still open, it is told
    # about the same job rather than being handed a second one.
    again = _submit(journey_id)
    assert again["operation_id"] == operation_id, again
    assert again["reused"] is True, again
    finished = _wait(journey_id, operation_id)
    assert http.count("photon") == 1, http.calls

    after = _submit(journey_id)
    assert after["operation_id"] != operation_id, after
    assert after["reused"] is False, after
    _wait(journey_id, after["operation_id"])
    assert finished["status"] == COMPLETED, finished


def test_a_busy_store_answers_a_named_retry_on_both_doors(monkeypatch) -> None:
    """The database can be held by a generation in flight; that is not an internal error.

    A raw 500 would tell the traveller nothing and would look like the app broke.  A named 503
    with a retry hint says what happened, on the door that accepts and the door that waits.
    """

    from app import main as main_module

    def _busy(*_args, **_kwargs):
        raise main_module.GenerationStoreBusy("the store is busy")

    monkeypatch.setattr(main_module, "submit_operation", _busy)
    journey_id = _journey()
    for url in (
        f"/api/v1/journeys/{journey_id}/stay-generations",
        f"/api/v1/journeys/{journey_id}/stay-options/generate",
    ):
        response = client.post(url, json={})
        assert response.status_code == 503, (url, response.status_code, response.text[:200])
        body = response.json()["error"]
        assert body["code"] == "GENERATION_STORE_BUSY", body
        assert body["message_key"] == "generation.store_busy", body
        assert body["details"]["retry_after_ms"] == 500, body


def test_the_acceptance_retry_is_bounded_and_then_names_the_condition() -> None:
    """Four short waits, then a refusal - never the engine's 30-second stall.

    Without the bound, "accept fast" quietly becomes "the request hangs and then 500s", which is
    the failure mode this whole contract was written to remove.
    """

    from sqlalchemy.exc import OperationalError

    from app import stay_generation_operations as ops

    attempts = []

    class _StubbornSession:
        def commit(self) -> None:
            attempts.append(1)
            raise OperationalError("INSERT INTO stay_generation_operations", {}, Exception("locked"))

        def rollback(self) -> None:
            return None

    started = time.monotonic()
    try:
        ops._commit_with_bounded_retries(_StubbornSession(), attempts=4)
    except ops.GenerationStoreBusy as exc:
        assert "4 attempts" in str(exc), exc
    else:
        raise AssertionError("a permanently busy store was reported as success")
    assert len(attempts) == 4, attempts
    assert time.monotonic() - started < 1.0, time.monotonic() - started


def test_a_returning_client_can_name_its_open_job_without_the_id(monkeypatch) -> None:
    """The read side of "reload restores the same operation".

    A phone that lost its stored id still deserves to find the job that is already running -
    otherwise the only way back is another POST, which is how one 115s chain becomes two.
    """

    http = _SlowHttp(delay_s=0.2)
    _use(monkeypatch, http)
    journey_id = _journey()
    operation_id = _submit(journey_id)["operation_id"]

    listing = client.get(f"/api/v1/journeys/{journey_id}/stay-generations")
    assert listing.status_code == 200, listing.text
    body = listing.json()
    assert body["journey_id"] == journey_id, body
    assert [item["operation_id"] for item in body["open"]] == [operation_id], body["open"]
    assert body["latest"]["operation_id"] == operation_id, body["latest"]
    assert body["count"] == 1, body

    finished = _wait(journey_id, operation_id)
    settled = client.get(f"/api/v1/journeys/{journey_id}/stay-generations").json()
    assert settled["open"] == [], settled["open"]
    assert settled["latest"]["status"] == finished["status"], settled["latest"]
    assert settled["latest"]["outcome"] in OUTCOMES, settled["latest"]


def test_a_journey_that_never_asked_reports_nothing_rather_than_a_stale_job() -> None:
    """`latest: null` is the honest answer; an inherited job from another trip is not."""

    journey_id = _journey()
    body = client.get(f"/api/v1/journeys/{journey_id}/stay-generations").json()
    assert body["open"] == [] and body["latest"] is None and body["count"] == 0, body


def test_the_status_route_is_scoped_to_its_journey(monkeypatch) -> None:
    http = _SlowHttp(delay_s=0.0)
    _use(monkeypatch, http)
    journey_id = _journey()
    other_journey = _journey(traveler=_traveler_id())
    operation_id = _submit(journey_id)["operation_id"]
    response = client.get(f"/api/v1/journeys/{other_journey}/stay-generations/{operation_id}")
    assert response.status_code == 404, response.text
    assert response.json()["error"]["message_key"] == "resource.not_found", response.json()
    assert _read(journey_id, operation_id)["operation_id"] == operation_id
    _wait(journey_id, operation_id)


# ----------------------------------------------------------- one path, no second truth


def test_the_synchronous_door_answers_with_what_the_operation_stored(monkeypatch) -> None:
    """Compatibility is a wrapper, not a fork: the two doors cannot drift.

    A second implementation of "generate stays" is how the create endpoint and the pricer
    once disagreed about the window (ORD-0033).  This pins them to one answer.
    """

    http = _SlowHttp(delay_s=0.0)
    _use(monkeypatch, http)
    journey_id = _journey()
    operation_id = _submit(journey_id, sort="BEST_FOR_JOURNEY", candidate_count=5)["operation_id"]
    stored = _wait(journey_id, operation_id)["result"]
    stored.pop("phase_ms_header", None)

    response = client.post(
        f"/api/v1/journeys/{journey_id}/stay-options/generate",
        json={"sort": "BEST_FOR_JOURNEY", "candidate_count": 5},
    )
    assert response.status_code == 200, response.text
    assert response.json() == stored, "the sync door and the operation disagree"


def test_a_named_window_rejection_still_answers_422_from_inside_a_worker(monkeypatch) -> None:
    """Moving the work off the request thread must not move the refusal with it.

    The exception now surfaces in another thread, so the wrapper re-raises the stored text
    through the one HTTP conversion that already exists - rather than the operation gaining
    its own idea of what a 422 is.
    """

    http = _SlowHttp(delay_s=0.0)
    _use(monkeypatch, http)
    journey_id = _journey()
    with SessionLocal() as session:
        row = session.get(Journey, journey_id)
        context = session.get(JourneyTripContext, journey_id)
        assert row is not None and context is not None
        start = datetime.fromisoformat(row.start_at)
        row.end_at = (start + timedelta(days=9)).isoformat()
        context.updated_at = datetime.now(timezone.utc).isoformat()
        session.commit()

    response = client.post(f"/api/v1/journeys/{journey_id}/stay-options/generate", json={})
    assert response.status_code == 422, response.text
    body = response.json()["error"]
    assert body["code"] == "V13_JOURNEY_DAYS_OUT_OF_BOUNDS", body
    assert body["message_key"] == "journey.days_out_of_bounds", body
    assert body["details"]["actual_days"] == 10, body["details"]
    failed = _wait(journey_id, _submit(journey_id)["operation_id"], seconds=5)
    assert failed["status"] == FAILED, failed
    assert failed["error"]["reason"].startswith("V13_JOURNEY_DAYS_OUT_OF_BOUNDS"), failed["error"]
    assert failed["outcome"] is None, failed


def test_an_operation_left_open_by_a_dead_process_is_closed_as_worker_lost() -> None:
    """A restart cannot finish a job it never had a thread for.

    Left `QUEUED`, this is a spinner with no end and a reload that restores a lie.
    """

    journey_id = _journey()
    stamp = datetime.now(timezone.utc).isoformat()
    with SessionLocal() as session:
        orphan = StayGenerationOperation(
            id=str(uuid4()),
            journey_id=journey_id,
            fingerprint="orphan",
            status=RUNNING,
            phase="DISCOVERY",
            queued_at=stamp,
            started_at=stamp,
            updated_at=stamp,
        )
        session.add(orphan)
        session.commit()
        orphan_id = orphan.id

    closed = reconcile_interrupted_operations()
    assert closed >= 1, closed
    with SessionLocal() as session:
        row = session.get(StayGenerationOperation, orphan_id)
        assert row.status == FAILED, row.status
        payload = json.loads(row.error_json)
        assert payload["code"] == "WORKER_LOST", payload
        assert payload["message_key"] == "error.worker_lost", payload
        # What the traveller was doing when the process died stays in the record.
        assert payload["details"]["phase_at_restart"] == "DISCOVERY", payload
        assert row.result_json == "{}", "a lost worker may not have produced a result"
    # Re-running the reconciliation changes nothing: nothing is open, so nothing is claimed.
    assert reconcile_interrupted_operations() >= 0
    with SessionLocal() as session:
        assert session.get(StayGenerationOperation, orphan_id).status == FAILED


def test_the_provider_timeout_is_the_configured_value_not_a_hidden_floor(monkeypatch) -> None:
    """`max(30.0, configured)` meant every setting at or below 30s was silently discarded."""

    # `settings` is a frozen dataclass, so the seam is the module attribute the provider
    # actually reads at construction time.
    class _Timeout:
        real_city_timeout_seconds = 7.0

    monkeypatch.setattr(stay_osm, "settings", _Timeout())
    provider = stay_osm.OsmAccommodationProvider()
    try:
        timeout = provider._client.timeout
    finally:
        provider.close()
    assert timeout.connect == 7.0, timeout
    assert timeout.read == 7.0, timeout
    assert timeout.read != 30.0, timeout

    class _Wider:
        real_city_timeout_seconds = 45.0

    monkeypatch.setattr(stay_osm, "settings", _Wider())
    widened = stay_osm.OsmAccommodationProvider()
    try:
        assert widened._client.timeout.read == 45.0, widened._client.timeout
    finally:
        widened.close()


def test_a_refused_commit_is_re_staged_and_only_then_called_a_202(monkeypatch) -> None:
    """Tonight's defect, measured at the seam where it happened.

    The ORD-0057 acceptance probe (eight submits behind a job owning SQLite's single writer)
    came back with eight 202s and **one** row in the table: the refused attempt rolled back, the
    retry's `session.commit()` then succeeded while writing nothing - a rollback de-registers
    the already-flushed new object, so the retried transaction had no row left to insert - and
    the caller's first poll was a 404 for a job the door had just accepted.

    The busy writer is simulated here rather than held for real.  Measured, not theorised: an
    earlier version of this gate took the write lock from a second connection, and because the
    suite shares one database file it left the *next* run's tests timing out in a cascade -
    a gate that destabilises the thing it is meant to protect is not evidence.  Failing the
    first commit at the seam is the same state, without borrowing the lock.
    """

    from sqlalchemy.exc import OperationalError

    import app.stay_generation_operations as ops

    journey_id = _journey()
    request = {"candidate_count": 4, "query": "the first commit is refused"}
    with SessionLocal() as session:
        journey = session.get(Journey, journey_id)
        assert journey is not None
        real_commit = session.commit
        attempts: list[str] = []

        def refusing_first_commit() -> None:
            attempts.append("commit")
            if len(attempts) == 1:
                session.rollback()  # what the store does to a refused write
                raise OperationalError(
                    "INSERT INTO stay_generation_operations (id) VALUES (?)",
                    {"id": "first attempt"},
                    Exception("database is locked"),
                )
            real_commit()

        # The retry path waits between attempts; the wait itself is not what is under test.
        monkeypatch.setattr(ops.time, "sleep", lambda _seconds: None)
        session.commit = refusing_first_commit
        row, reused = ops.submit_operation(session, journey, request, start_worker=False)
        assert reused is False, row
        stored_id = row.id

    # Two commits happened: the refused one and the one that had to carry the row.
    assert len(attempts) == 2, attempts
    # And the second one actually wrote something, which is the whole claim.
    with SessionLocal() as verify:
        found = verify.scalar(
            select(StayGenerationOperation.id).where(
                StayGenerationOperation.id == stored_id
            )
        )
    assert found == stored_id, (
        "the door handed out an operation id the store never received - the retried commit "
        "wrote an empty transaction, which is the pre-fix behaviour"
    )
