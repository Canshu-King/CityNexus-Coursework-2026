"""ORD-0057 - a stay generation is an operation the traveller can leave and come back to.

Why this exists: a cold accommodation chain (Photon -> three Overpass mirrors -> Nominatim)
was measured at ~115s *inside* `POST /stay-options/generate` (`e981b46` phase timings,
provider 100% of 120.86s).  A held-open request cannot be waited on politely: the phone
shows a spinner with no way out, a reload loses the work, and a second tap starts a second
chain.  The arbitration (`5749459724` §1) approved a contract change rather than a longer
timeout, and this module is that contract.

Rules this file is written to, each because a failure mode exists:

* **The state machine is the only writer of status.** Transitions are checked against
  `ALLOWED_TRANSITIONS`, and every write is a guarded `UPDATE ... WHERE status IN (from)` so
  two threads cannot both land a terminal state.
* **Abandon is honest.** `ABANDON_REQUESTED` is what the endpoint can truthfully promise;
  the worker reaches `ABANDONED` once the in-flight provider call returns.  A result is
  never persisted after an abandon, because the terminal write and the generated rows are the
  *same transaction*, guarded by the same status test.
* **No fake progress.** `phase` is emitted from the stages the code actually enters
  (`DISCOVERY` / `NORMALIZATION` / `MOBILITY_CHECK` / `RANKING`) and is `NULL` before the
  first one; there is no percentage anywhere, because there is no provable denominator.
* **One open job per journey and request.** Enforced by a partial unique index, not by an
  in-process set, so a racing duplicate is resolved by the database and read back.
* **A dead process says so.** Rows left `QUEUED`/`RUNNING` by an earlier process are moved
  to `FAILED` at boot with reason `WORKER_LOST` - see the `PROCESS_LOCAL_ONLY` note below.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import Journey, StayGenerationOperation
from app.db.session import SessionLocal

QUEUED = "QUEUED"
RUNNING = "RUNNING"
ABANDON_REQUESTED = "ABANDON_REQUESTED"
COMPLETED = "COMPLETED"
FAILED = "FAILED"
ABANDONED = "ABANDONED"

TERMINAL_STATUSES = frozenset({COMPLETED, FAILED, ABANDONED})
OPEN_STATUSES = (QUEUED, RUNNING, ABANDON_REQUESTED)

ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    QUEUED: frozenset({RUNNING, ABANDON_REQUESTED, FAILED}),
    RUNNING: frozenset({COMPLETED, FAILED, ABANDON_REQUESTED}),
    # Once abandonment is asked for, `COMPLETED` is no longer reachable: the traveller
    # stopped using this job, and writing a result anyway is the exact dishonesty the
    # arbitration named.  A crash on the way out still has an honest exit.
    ABANDON_REQUESTED: frozenset({ABANDONED, FAILED}),
    COMPLETED: frozenset(),
    FAILED: frozenset(),
    ABANDONED: frozenset(),
}

PHASES = ("DISCOVERY", "NORMALIZATION", "MOBILITY_CHECK", "RANKING")

logger = logging.getLogger("citynexus.stay_generation")

# The executor lives in this process, like the journey mutation lock does.  A second
# worker process would take its own queue, so an operation created here is only ever run
# here; `reconcile_interrupted_operations` is what keeps a restart from leaving a job
# spinning forever.  Disclosed rather than hidden, because `PROCESS_LOCAL_ONLY` is already
# the standing caveat for cross-process work in this backend.
_MAX_WORKERS = 4
_EXECUTOR: ThreadPoolExecutor | None = None
_EXECUTOR_LOCK = threading.Lock()
_dispatched: set[str] = set()

# `PROCESS_LOCAL_ONLY`, same disclosure as the journey mutation lock.  Generation holds
# SQLite's single writer for as long as it runs, so an endpoint that insisted on recording
# "the traveller asked to stop" durably would block on that lock and answer nothing - the
# one thing an abandon button must not do.  The event is therefore the authoritative "this
# result will not be used" signal inside this process, and the row is what the worker
# writes on its way out.  A restart cannot lose a *result* this way, only a request whose
# worker no longer exists, which `reconcile_interrupted_operations` closes as WORKER_LOST.
_abandon_requests: dict[str, threading.Event] = {}
_ABANDON_LOCK = threading.Lock()


def request_abandonment(operation_id: str) -> None:
    with _ABANDON_LOCK:
        _abandon_requests.setdefault(operation_id, threading.Event()).set()


def abandonment_requested(operation_id: str) -> bool:
    with _ABANDON_LOCK:
        event = _abandon_requests.get(operation_id)
    return bool(event is not None and event.is_set())


def forget_abandonment(operation_id: str) -> None:
    with _ABANDON_LOCK:
        _abandon_requests.pop(operation_id, None)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _ms_since(then: datetime | None, until: datetime | None) -> int | None:
    if then is None:
        return None
    return max(0, int(((until or datetime.now(timezone.utc)) - then).total_seconds() * 1000))


def get_executor() -> ThreadPoolExecutor:
    global _EXECUTOR
    with _EXECUTOR_LOCK:
        if _EXECUTOR is None:
            _EXECUTOR = ThreadPoolExecutor(
                max_workers=_MAX_WORKERS, thread_name_prefix="stay-generation"
            )
        return _EXECUTOR


def generation_fingerprint(journey: Journey, request_payload: dict[str, Any]) -> str:
    """The inputs that can change the answer, and nothing that cannot.

    Deliberately *not* included: the idempotency key, the trace id, the submission time.  A
    traveller tapping "生成" four times sends four identical bodies and must get one job.
    The journey's own window and budget are included because they genuinely change which
    stays are feasible - editing the trip is a different question, not a repeat of it.
    """

    material = {
        "journey_id": journey.id,
        "city_code": journey.city_code,
        "timezone": journey.timezone,
        "start_at": journey.start_at,
        "end_at": journey.end_at,
        "budget_minor": journey.budget_amount_minor,
        "currency": journey.budget_currency,
        "request": {
            key: request_payload.get(key)
            for key in sorted(request_payload)
            if key in {"query", "sort", "weather_state", "candidate_count"}
        },
    }
    blob = json.dumps(material, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


def status_urls(journey_id: str, operation_id: str) -> dict[str, str]:
    base = f"/api/v1/journeys/{journey_id}/stay-generations/{operation_id}"
    return {"status_url": base, "abandon_url": f"{base}/abandon"}


def retry_after_ms(row: StayGenerationOperation) -> int:
    """A poll hint the client may follow blindly, so it has to degrade honestly.

    Terminal means stop asking.  Otherwise the wait grows: a job that has already run 30s
    is not going to finish because it was polled twice as often, and every extra poll is a
    read against the same SQLite file that the worker is writing to.
    """

    if row.status in TERMINAL_STATUSES:
        return 0
    elapsed = _ms_since(_parse(row.queued_at), None) or 0
    if elapsed < 10_000:
        return 1000
    if elapsed < 30_000:
        return 2000
    return 5000


def operation_payload(row: StayGenerationOperation) -> dict[str, Any]:
    result = json.loads(row.result_json or "{}") or None
    error = json.loads(row.error_json or "{}") or None
    # An abandon this process accepted but could not yet write is still an abandon the
    # traveller asked for; reporting the row's `RUNNING` alone would read as the button
    # having been ignored.
    reported_status = row.status
    if (
        reported_status == RUNNING
        and not result
        and abandonment_requested(row.id)
    ):
        reported_status = ABANDON_REQUESTED
    elapsed = _ms_since(
        _parse(row.queued_at), _parse(row.finished_at) if row.status in TERMINAL_STATUSES else None
    )
    return {
        "operation_id": row.id,
        "journey_id": row.journey_id,
        "status": reported_status,
        "recorded_status": row.status,
        "phase": row.phase,
        "outcome": row.outcome,
        "elapsed_ms": elapsed,
        "retry_after_ms": 0 if reported_status in TERMINAL_STATUSES else retry_after_ms(row),
        "terminal": row.status in TERMINAL_STATUSES,
        "result": result,
        "error": error,
        "submitted_at": row.queued_at,
        "started_at": row.started_at,
        "finished_at": row.finished_at,
        "abandon_requested_at": row.abandon_requested_at,
        **status_urls(row.journey_id, row.id),
    }


def find_open_operation(
    session: Session, journey_id: str, fingerprint: str
) -> StayGenerationOperation | None:
    return session.scalar(
        select(StayGenerationOperation)
        .where(
            StayGenerationOperation.journey_id == journey_id,
            StayGenerationOperation.fingerprint == fingerprint,
            StayGenerationOperation.status.in_(OPEN_STATUSES),
        )
        .order_by(StayGenerationOperation.queued_at.desc())
    )


class GenerationStoreBusy(RuntimeError):
    """The database could not take the new job within the acceptance budget.

    Distinct from a bug on purpose: it is a real, tellable condition ("another generation owns
    the writer right now"), and the door answers 503 + `retry_after_ms` rather than the raw 500
    an unhandled `OperationalError` would produce.
    """


def _commit_with_bounded_retries(
    session: Session,
    *,
    attempts: int = 4,
    restage: Callable[[], None] | None = None,
) -> None:
    """Try to commit, giving up inside the acceptance budget instead of at the 30s engine timeout.

    The wait is per attempt and deliberately short: four tries cost well under a second, which
    keeps the "accept fast" promise even while another job holds the writer, and a caller that
    still gets refused learns it immediately rather than after a stalled request.

    ``restage`` exists because of a measurement, not a theory.  Reproduced with the acceptance
    probe (eight submits, first job's worker holding the writer): the attempts that were refused
    printed `INSERT … ROLLBACK`, and the attempt after them printed a **clean COMMIT that wrote
    nothing** - because the rollback de-registered the already-flushed new ORM object, so the
    retried transaction had no pending row.  The door answered 202 with an id that was never in
    the store, and the caller's first poll came back 404.  A retry that cannot re-queue its work
    is not a retry, so the caller passes the staging step in and every attempt submits the row
    again.
    """

    from sqlalchemy.exc import OperationalError

    for attempt in range(attempts):
        try:
            session.commit()
            return
        except OperationalError:
            session.rollback()
            if restage is not None:
                restage()
            if attempt == attempts - 1:
                raise GenerationStoreBusy(
                    f"the stay-generation store stayed busy over {attempts} attempts"
                )
            time.sleep(0.05)


def submit_operation(
    session: Session,
    journey: Journey,
    request_payload: dict[str, Any],
    *,
    start_worker: bool = True,
) -> tuple[StayGenerationOperation, bool]:
    """Return (operation, reused).  A duplicate tap reuses; it never queues twice.

    The partial unique index is the arbiter, so two requests that race each other produce
    one row and both callers are shown that row's id - which is what makes "reload restores
    the same operation" true rather than lucky.
    """

    fingerprint = generation_fingerprint(journey, request_payload)
    existing = find_open_operation(session, journey.id, fingerprint)
    if existing is not None:
        return existing, True
    stamp = now_iso()
    # A generation in flight owns the database's only writer, so this INSERT can be refused.
    # It must never turn into a 500 at the door that is supposed to be fast: wait briefly,
    # retry a bounded number of times, and let the caller decide what to say.
    session.execute(text("PRAGMA busy_timeout = 200"))
    operation_id = str(uuid.uuid4())
    request_json = json.dumps(request_payload, ensure_ascii=False, sort_keys=True)

    staged: list[StayGenerationOperation] = []

    def stage() -> StayGenerationOperation:
        """Put the row into the session, ready for this attempt's transaction.

        Re-runnable by design: after a refused commit the rollback de-registers the flushed
        object, so the next attempt needs its own instance.  The id is minted once, outside,
        because the caller was already handed a status url that names it - and the live
        instance is remembered, so what this function returns is the row the session actually
        owns, not the detached first attempt.
        """

        row = StayGenerationOperation(
            id=operation_id,
            journey_id=journey.id,
            fingerprint=fingerprint,
            status=QUEUED,
            phase=None,
            request_json=request_json,
            result_json="{}",
            error_json="{}",
            outcome=None,
            queued_at=stamp,
            updated_at=stamp,
        )
        session.add(row)
        staged.append(row)
        return row

    stage()
    try:
        _commit_with_bounded_retries(session, attempts=4, restage=stage)
    except IntegrityError:
        # Someone else's identical job won the index between the read and the insert.
        session.rollback()
        winner = find_open_operation(session, journey.id, fingerprint)
        if winner is None:
            raise
        return winner, True
    # The promise of a 202 is "this job exists and you can ask it about later", so check it
    # against the store rather than against the object in memory: with `expire_on_commit=False`
    # the session would happily hand back a row that a rolled-back retry never wrote.
    durable = session.scalar(
        select(func.count())
        .select_from(StayGenerationOperation)
        .where(StayGenerationOperation.id == operation_id)
    )
    if not durable:
        raise GenerationStoreBusy(
            f"operation {operation_id} reported a commit the store does not hold"
        )
    if start_worker:
        dispatch(operation_id)
    return staged[-1], False


class TransitionRefused(RuntimeError):
    """A status write that did not match its guard."""


def mark(
    session: Session,
    operation_id: str,
    to_status: str,
    *,
    from_statuses: tuple[str, ...] | None = None,
    commit: bool = True,
    **fields: Any,
) -> bool:
    """Move one operation along its state machine, and only from a state that allows it.

    `rowcount` is the whole point: the write either matches the state the caller believed in
    or it does nothing, so a worker finishing next to an abandon resolves into exactly one
    answer instead of a last-write-wins race.
    """

    if from_statuses is None:
        from_statuses = tuple(
            source
            for source, targets in ALLOWED_TRANSITIONS.items()
            if to_status in targets and targets
        )
    if not from_statuses:
        raise TransitionRefused(f"no state allows {to_status}")
    for source in from_statuses:
        if to_status not in ALLOWED_TRANSITIONS.get(source, frozenset()):
            raise TransitionRefused(f"{source} -> {to_status} is not a legal transition")
    values = {"status": to_status, "updated_at": now_iso(), **fields}
    result = session.execute(
        update(StayGenerationOperation)
        .where(
            StayGenerationOperation.id == operation_id,
            StayGenerationOperation.status.in_(from_statuses),
        )
        .values(**values)
    )
    if commit:
        session.commit()
    return result.rowcount == 1


@dataclass
class PhaseReporter:
    """Records the stage the worker has actually entered, at most once per change.

    `phase` is the field the arbitration allowed in place of a percentage, so it has one
    rule: it is only ever set to a stage this code is genuinely inside.  It starts as NULL,
    and a stage that never happens is never reported.

    It writes through the *worker's own* session, on purpose.  Generation is one write
    transaction from the first captured offer to the last ranked option (SQLite has a single
    writer), so a second connection updating this same row does not interleave - it waits
    out the busy timeout and fails with `database is locked`, which is what the first
    version of this did.  The honest consequence is stated where a client can read it:
    the stage becomes visible when the transaction commits, and the live signal during a
    running job is `status` plus `elapsed_ms`, not a progress claim.
    """

    operation_id: str
    session: Session
    last: str | None = None
    writes: int = 0
    skipped: int = 0

    def __call__(self, phase_name: str) -> None:
        if phase_name not in PHASES or phase_name == self.last:
            return
        try:
            self.session.execute(
                update(StayGenerationOperation)
                .where(
                    StayGenerationOperation.id == self.operation_id,
                    StayGenerationOperation.status.in_((RUNNING,)),
                )
                .values(phase=phase_name, updated_at=now_iso())
            )
            self.last = phase_name
            self.writes += 1
        except Exception:  # pragma: no cover - a lost stage must not lose the job
            # A stage that could not be recorded is missing information, not a failed
            # generation: the work carries on and the result stays authoritative.
            self.skipped += 1


def read_operation(session: Session, operation_id: str) -> StayGenerationOperation | None:
    return session.get(StayGenerationOperation, operation_id)


def operations_for_journey(session: Session, journey_id: str) -> dict[str, Any]:
    """What a returning client can ask instead of POSTing again and hoping to dedupe.

    Restoring the *same* job after a reload is only honest if the job can be found without
    knowing its id: the fingerprint match happens to cover the common case (same question),
    but a client that came back later - or that asks a differently-worded question - needs
    the open job named, not silently re-created.
    """

    rows = session.scalars(
        select(StayGenerationOperation)
        .where(StayGenerationOperation.journey_id == journey_id)
        .order_by(StayGenerationOperation.queued_at.desc())
    ).all()
    open_rows = [row for row in rows if row.status in OPEN_STATUSES]
    return {
        "journey_id": journey_id,
        "open": [operation_payload(row) for row in open_rows],
        "latest": operation_payload(rows[0]) if rows else None,
        "count": len(rows),
    }


def abandon_operation(session: Session, operation_id: str) -> StayGenerationOperation:
    """Ask a job to stop.  Idempotent, and honest about what just happened.

    The request is registered in this process first, because the generation transaction can
    hold SQLite's only writer for minutes and an endpoint that waited on it would answer
    nothing at all.  The row write is then attempted with a short per-connection busy
    timeout; whether or not it lands, the worker resolves the job to `ABANDONED` on its way
    out and writes no result, which is the promise that actually matters.  A job that has
    already produced a result cannot be un-finished, and this says so instead of reporting a
    cancellation that did not happen.
    """

    row = read_operation(session, operation_id)
    if row is None:
        raise LookupError(operation_id)
    if row.status in TERMINAL_STATUSES:
        return row
    request_abandonment(operation_id)
    if row.status == ABANDON_REQUESTED:
        return row
    session.execute(text("PRAGMA busy_timeout = 1500"))
    try:
        moved = mark(
            session,
            operation_id,
            ABANDON_REQUESTED,
            from_statuses=(QUEUED, RUNNING),
            abandon_requested_at=now_iso(),
        )
    except Exception:  # pragma: no cover - the event above is the live signal
        session.rollback()
        moved = False
    if not moved:
        session.rollback()
    fresh = read_operation(session, operation_id)
    assert fresh is not None, operation_id
    return fresh


def dispatch(operation_id: str) -> None:
    """Hand one queued operation to the executor, once."""

    with _EXECUTOR_LOCK:
        if operation_id in _dispatched:
            return
        _dispatched.add(operation_id)
    get_executor().submit(_run_operation, operation_id)


def _fail(
    session: Session,
    operation_id: str,
    code: str,
    message: str,
    *,
    reason: str = "",
    exception: str = "",
    **details: Any,
) -> None:
    payload: dict[str, Any] = {
        "code": code,
        "message": message,
        "message_key": f"error.{code.lower()}",
    }
    if reason:
        # The untouched exception text, so the synchronous door can hand it to
        # `temporal_rejection` and get the *same* 422 a caller got before this moved
        # off the request thread.  A second place that converts domain errors into HTTP
        # would be a second truth about which refusals are refusals.
        payload["reason"] = reason
    if exception:
        payload["exception"] = exception
    if details:
        payload["details"] = details
    mark(
        session,
        operation_id,
        FAILED,
        from_statuses=(QUEUED, RUNNING, ABANDON_REQUESTED),
        error_json=json.dumps(payload, ensure_ascii=False),
        finished_at=now_iso(),
    )
    session.commit()


def _run_operation(operation_id: str) -> None:
    """The body that used to be a request.  Nothing in here may raise into a caller."""

    from app.core.phase_timing import begin_trace, end_trace, phase
    from app.stay_mobility import generate_stay_options  # late: circular at import

    session = SessionLocal()
    try:
        row = read_operation(session, operation_id)
        if row is None:
            return
        if row.status in TERMINAL_STATUSES:
            return
        if row.status == ABANDON_REQUESTED:
            mark(session, operation_id, ABANDONED, from_statuses=(ABANDON_REQUESTED,), finished_at=now_iso())
            session.commit()
            return
        if not mark(session, operation_id, RUNNING, from_statuses=(QUEUED,), started_at=now_iso()):
            session.commit()
            return
        journey = session.get(Journey, row.journey_id)
        if journey is None:
            _fail(
                session,
                operation_id,
                "GENERATION_JOURNEY_GONE",
                "The journey was deleted while generating.",
            )
            with _EXECUTOR_LOCK:
                _dispatched.discard(operation_id)
            return
        request = json.loads(row.request_json or "{}")
        reporter = PhaseReporter(operation_id, session)
        generation: dict[str, Any] = {}
        # The same root span the synchronous endpoint used to own, opened here now that the
        # work lives here: `X-CityNexus-Phase-Ms` and the log line keep naming
        # `stay.generate`, so ORD-0049's measurement contract did not quietly change shape.
        begin_trace()
        try:
            with phase(
                "stay.generate", detail=f"journey={journey.id} city={journey.city_code}"
            ):
                options = generate_stay_options(
                    session,
                    journey,
                    weather=str(request.get("weather_state") or "NORMAL"),
                    generation_out=generation,
                    stage_out=reporter,
                )
                body = _render_result(session, journey, options, request, generation)
        except Exception as exc:  # noqa: BLE001 - a worker must not die quietly
            session.rollback()
            logger.exception("stay generation %s failed", operation_id)
            _fail(
                session,
                operation_id,
                "GENERATION_FAILED",
                f"{type(exc).__name__}: {exc}",
                reason=str(exc),
                exception=type(exc).__name__,
            )
            return
        finally:
            finished_trace = end_trace()
        if finished_trace is not None:
            logger.info(finished_trace.render())
            body["phase_ms_header"] = finished_trace.render_header()
        # The terminal write and the generated rows commit together, guarded by the same
        # status test.  A stop this process accepted cannot be un-asked, so the result is
        # dropped here and the row is resolved on the session that already owns the write -
        # which is also the only session that can write while the transaction is open.
        if abandonment_requested(operation_id):
            session.rollback()
            mark(
                session,
                operation_id,
                ABANDON_REQUESTED,
                from_statuses=(RUNNING,),
                commit=False,
                abandon_requested_at=now_iso(),
            )
            mark(
                session,
                operation_id,
                ABANDONED,
                from_statuses=(ABANDON_REQUESTED,),
                commit=False,
                finished_at=now_iso(),
            )
            session.commit()
            return
        landed = mark(
            session,
            operation_id,
            COMPLETED,
            from_statuses=(RUNNING,),
            commit=False,
            outcome=generation.get("outcome"),
            phase=reporter.last,
            result_json=json.dumps(body, ensure_ascii=False, default=str),
            finished_at=now_iso(),
        )
        if not landed:
            # The row was moved under us - in practice by an abandon that got its own
            # durable write in - so nothing this job produced may become visible.
            session.rollback()
            mark(
                session,
                operation_id,
                ABANDONED,
                from_statuses=(ABANDON_REQUESTED,),
                commit=False,
                finished_at=now_iso(),
            )
        # The commit that makes the guarded write real: `landed` only proved the row was
        # still in `RUNNING` when the UPDATE ran, and a pending terminal state that nobody
        # commits leaves every caller polling a job that is already over.
        session.commit()
    except BaseException as exc:  # noqa: BLE001 - an operation must never outlive its worker
        # Anything that escapes the handling above - a bug in this file, a database that
        # refused the write, even a `SystemExit` - has one acceptable outcome: the operation
        # is terminal and says why, rather than sitting in `RUNNING` forever.
        logger.exception("stay generation worker died for %s", operation_id)
        try:
            session.rollback()
        except Exception:  # pragma: no cover - best effort
            pass
        _force_fail(operation_id, exc)
    finally:
        session.close()
        with _EXECUTOR_LOCK:
            _dispatched.discard(operation_id)
        forget_abandonment(operation_id)


def _force_fail(operation_id: str, exc: BaseException) -> None:
    session = SessionLocal()
    try:
        mark(
            session,
            operation_id,
            FAILED,
            from_statuses=OPEN_STATUSES,
            error_json=json.dumps(
                {
                    "code": "GENERATION_WORKER_DIED",
                    "message": f"{type(exc).__name__}: {exc}",
                    "message_key": "error.generation_worker_died",
                    "exception": type(exc).__name__,
                    "reason": str(exc),
                },
                ensure_ascii=False,
            ),
            finished_at=now_iso(),
        )
        session.commit()
    except Exception:  # pragma: no cover - nothing left to try
        logger.exception("could not close generation operation %s", operation_id)
    finally:
        session.close()


def _render_result(
    session: Session,
    journey: Journey,
    options: list[Any],
    request: dict[str, Any],
    generation: dict[str, Any],
) -> dict[str, Any]:
    """The exact body the synchronous endpoint answers with, stored on the operation.

    Both entry points read this, so the asynchronous contract and the compatibility wrapper
    cannot drift into two different shapes of "done".
    """

    from app.db.models import AccommodationOfferSnapshot
    from app.stay_mobility import option_payload

    offers = session.scalars(
        select(AccommodationOfferSnapshot)
        .where(AccommodationOfferSnapshot.journey_id == journey.id)
        .order_by(AccommodationOfferSnapshot.total_amount_minor)
    ).all()
    candidate_count = int(request.get("candidate_count") or 5)
    ordered = list(options)
    if request.get("sort") == "CHEAPEST":
        ordered = sorted(ordered, key=lambda item: (item.stay_total_minor, item.rank))
    elif request.get("sort") == "LEAST_TRAVEL":
        ordered = sorted(
            ordered, key=lambda item: (item.daily_travel_minor, item.walking_min, item.rank)
        )
    elif request.get("sort") == "MOST_FLEXIBLE":
        ordered = sorted(ordered, key=lambda item: (-item.cancellation_score, item.rank))
    return {
        "optimizer": ordered[0].optimizer_kind if ordered else "BOUNDED_DETERMINISTIC",
        "query": request.get("query") or f"{journey.city_code} stay-aware Journey",
        "sort": request.get("sort") or "BALANCED",
        "top_k": [option_payload(item, offers) for item in ordered[:candidate_count]],
        "generation": generation,
        "hard_constraints": {
            "protected_commitments": "PRESERVED",
            "deterministic_feasibility": "PASSED",
        },
    }


def wait_for_operation(
    operation_id: str,
    *,
    poll_seconds: float = 0.05,
    deadline_seconds: float | None = None,
    on_tick: Callable[[], None] | None = None,
) -> StayGenerationOperation | None:
    """Block until the operation is terminal, reading it as any client would.

    The synchronous endpoint is this loop and nothing else - there is no second code path
    that does the work, which is what keeps "async" from becoming a fork in the product.
    """

    started = time.monotonic()
    while True:
        session = SessionLocal()
        try:
            row = read_operation(session, operation_id)
        finally:
            session.close()
        if row is None or row.status in TERMINAL_STATUSES:
            return row
        if deadline_seconds is not None and (time.monotonic() - started) > deadline_seconds:
            return row
        if on_tick is not None:
            on_tick()
        time.sleep(poll_seconds)


def reconcile_interrupted_operations() -> int:
    """Close jobs no thread in this process can finish any more.

    `QUEUED`/`RUNNING` survive a restart as facts about a worker that no longer exists.
    Leaving them there makes a client poll forever and makes "reload restores the same
    operation" a trap; `FAILED` + `WORKER_LOST` is the honest answer, and it says retry.
    """

    session = SessionLocal()
    try:
        stale = session.scalars(
            select(StayGenerationOperation).where(
                StayGenerationOperation.status.in_((QUEUED, RUNNING, ABANDON_REQUESTED))
            )
        ).all()
        count = 0
        for row in stale:
            payload = {
                "code": "WORKER_LOST",
                "message": (
                    "This generation was running in a server process that has since "
                    "restarted, so its result was never written. Ask for it again."
                ),
                "message_key": "error.worker_lost",
                "details": {"status_at_restart": row.status, "phase_at_restart": row.phase},
            }
            landed = mark(
                session,
                row.id,
                FAILED,
                from_statuses=(QUEUED, RUNNING, ABANDON_REQUESTED),
                commit=False,
                error_json=json.dumps(payload, ensure_ascii=False),
                finished_at=now_iso(),
            )
            count += 1 if landed else 0
        session.commit()
        return count
    finally:
        session.close()
