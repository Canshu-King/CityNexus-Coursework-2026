"""ORD-0095-1 (§6/§7): the catalog-hydration job, running on the lifecycle that already exists.

Why this file is small on purpose.  `stay_generation_operations` already solved the hard parts
of "an operation the client can poll, abandon and reload into": a guarded status write whose
`rowcount` settles races, a partial unique index that makes duplicate jobs impossible in the
store rather than by convention, a poll ladder that degrades honestly, and a reconciler that
closes jobs whose worker no longer exists.  Inventing a second version of any of that would be
a second place for the same bug, so this module *reuses* the abandon-event registry, the clock
and the ladder semantics from there, and keeps only what genuinely differs: the destination
scope, the arbiter's phase names, and the structural terminal state `UNAVAILABLE`.

The order of operations is the §6 rule, and it is the reason this code opens and closes a
session per stage instead of holding one:

    short read -> release -> (provider work, when a source is configured) -> short write -> release

A SQLite write transaction that spans network I/O is how the stay path once held the only
writer for ~115 seconds; moving the work to a thread does not fix that, it only moves the lock.
"""

from __future__ import annotations

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

from app.anywhere_capability import (
    CATALOG_SCHEMA_VERSION,
    PROVIDER_POLICY_VERSION,
    READINESS_POLICY_VERSION,
    REASON_DESTINATION_AMBIGUOUS,
    REASON_NO_CANONICAL_IDENTITY,
    REASON_RESOLVER_UNAVAILABLE,
    capability_fingerprint,
    evaluate_capability,
    fingerprint_parts,
    audit_evidence,
    identity_fields,
    pending_correlation_key,
    pending_request_fingerprint,
    resolve_destination,
)
from app.db.models import CatalogHydrationOperation, Place, RealPlaceCandidate
from app.db.session import SessionLocal
from app.stay_generation_operations import (  # the lifecycle, not a copy of it
    forget_abandonment,
    now_iso,
    request_abandonment,
    abandonment_requested,
)

QUEUED = "QUEUED"
RUNNING = "RUNNING"
ABANDON_REQUESTED = "ABANDON_REQUESTED"
COMPLETED = "COMPLETED"
FAILED = "FAILED"
UNAVAILABLE = "UNAVAILABLE"
ABANDONED = "ABANDONED"

TERMINAL_STATUSES = frozenset({COMPLETED, FAILED, ABANDONED, UNAVAILABLE})
OPEN_STATUSES = (QUEUED, RUNNING, ABANDON_REQUESTED)

# `UNAVAILABLE` is reachable from both live states and is terminal: it is a statement about the
# destination ("no traceable source"), not about this attempt, so it must never be modelled as a
# FAILED attempt that a client would sensibly retry forever.
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    QUEUED: frozenset({RUNNING, ABANDON_REQUESTED, FAILED, UNAVAILABLE}),
    RUNNING: frozenset({COMPLETED, FAILED, ABANDON_REQUESTED, UNAVAILABLE}),
    ABANDON_REQUESTED: frozenset({ABANDONED, FAILED}),
    COMPLETED: frozenset(),
    FAILED: frozenset(),
    UNAVAILABLE: frozenset(),
    ABANDONED: frozenset(),
}

PHASES = (
    QUEUED,
    "RESOLVING_DESTINATION",
    "DISCOVERING_POIS",
    "NORMALIZING",
    "BUILDING_MOBILITY",
    "PERSISTING",
    "FINALIZING",
)

OUTCOME_PREPARED = "CATALOG_PREPARED"
OUTCOME_ALREADY_CURRENT = "ALREADY_CURRENT"
OUTCOME_NO_REGISTERED_SOURCE = "NO_REGISTERED_SOURCE"
OUTCOME_PENDING_UNAPPLIED = "PENDING_CANDIDATES_UNAPPLIED"
OUTCOME_ABANDONED = "ABANDONED_BY_TRAVELLER"
OUTCOME_WORKER_LOST = "WORKER_LOST"
# ORD-0167: two endings that were previously indistinguishable from "this place has no source".
# An ambiguous query can be fixed by choosing; a resolver that broke can be fixed by retrying.
# Neither may be reported as the other, and neither may be reported as the other one.
OUTCOME_DESTINATION_AMBIGUOUS = "DESTINATION_AMBIGUOUS"
OUTCOME_RESOLVER_RUNTIME_FAILURE = "RESOLVER_RUNTIME_FAILURE"

logger = logging.getLogger("citynexus.catalog_hydration")

_MAX_WORKERS = 2
_EXECUTOR: ThreadPoolExecutor | None = None
_EXECUTOR_LOCK = threading.Lock()
_dispatched: set[str] = set()


def get_executor() -> ThreadPoolExecutor:
    """`PROCESS_LOCAL_ONLY`, same disclosure as the stay-generation executor.

    A second uvicorn worker gets its own queue; what is durable across processes is the row and
    the unique index, which is why an id-less poll can still find the open job and why
    `reconcile_interrupted_operations` exists.
    """

    global _EXECUTOR
    with _EXECUTOR_LOCK:
        if _EXECUTOR is None:
            _EXECUTOR = ThreadPoolExecutor(
                max_workers=_MAX_WORKERS, thread_name_prefix="catalog-hydration"
            )
        return _EXECUTOR


def _parse(value: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value)) if value else None
    except ValueError:
        return None


def _ms_since(then: datetime | None, until: datetime | None = None) -> int | None:
    if then is None:
        return None
    return max(0, int(((until or datetime.now(timezone.utc)) - then).total_seconds() * 1000))


def hydration_urls(operation_id: str) -> dict[str, str]:
    base = f"/api/v1/catalog-hydrations/{operation_id}"
    return {"status_url": base, "abandon_url": f"{base}/abandon"}


def retry_after_ms(row: CatalogHydrationOperation) -> int:
    """The same ladder the stay job proved: terminal means stop asking, otherwise back off."""

    if row.status in TERMINAL_STATUSES:
        return 0
    elapsed = _ms_since(_parse(row.queued_at)) or 0
    if elapsed < 10_000:
        return 1000
    if elapsed < 30_000:
        return 2000
    return 5000


def _state_of(row: CatalogHydrationOperation) -> str | None:
    """Which of the four destination-request states this job record has *concluded*.

    Not a column: `canonical_destination_id` being null plus an outcome already says which of
    them it was, and storing a third copy would be a third thing that can disagree.  And not a
    default: a job still in flight, or one that ended because a traveller walked away or a worker
    died, has not concluded anything about the destination, so the honest answer is no answer.
    Reporting `NO_MATCH` for a QUEUED row would be the same mistake as a fabricated `progress`.
    """

    if row.canonical_destination_id:
        return "RESOLVED"
    outcome = str(row.outcome or "")
    if outcome == OUTCOME_DESTINATION_AMBIGUOUS:
        return "AMBIGUOUS"
    if outcome == OUTCOME_RESOLVER_RUNTIME_FAILURE:
        return "RUNTIME_FAILURE"
    if outcome == OUTCOME_NO_REGISTERED_SOURCE or row.status == UNAVAILABLE:
        return "NO_MATCH"
    return None


def operation_payload(row: CatalogHydrationOperation) -> dict[str, Any]:
    """The projection every caller reads.  Note what is *not* in here: `progress`.

    A percentage would be a claim about a queue this process cannot see.  `phase` names the
    stage the worker is genuinely inside, and `elapsed_ms` says how long it has taken - which
    is the honest version of the same information.
    """

    result = json.loads(row.result_json or "{}") or None
    error = json.loads(row.error_json or "{}") or None
    reported_status = row.status
    if reported_status == RUNNING and abandonment_requested(row.id):
        reported_status = ABANDON_REQUESTED
    terminal = row.status in TERMINAL_STATUSES
    return {
        "operation_id": row.id,
        "fingerprint": row.fingerprint,
        "reused": False,
        "status": reported_status,
        "recorded_status": row.status,
        "phase": row.phase,
        "outcome": row.outcome,
        "terminal": terminal,
        "elapsed_ms": _ms_since(_parse(row.queued_at), _parse(row.finished_at) if terminal else None),
        "retry_after_ms": 0 if terminal else retry_after_ms(row),
        "canonical_destination_id": row.canonical_destination_id,
        # The three non-identity columns, named as what they are: the traveller's words, the
        # folded form used to match them, and a token that correlates this job to nothing else.
        "pending_destination_label": row.pending_destination_label,
        "resolver_query_normalized": row.resolver_query_normalized,
        "destination_pending_key": row.destination_pending_key,
        "pending_label_fidelity": row.pending_label_fidelity,
        "destination_state": _state_of(row),
        "retryable": row.status == FAILED or str(row.outcome or "") == OUTCOME_RESOLVER_RUNTIME_FAILURE,
        "catalog_schema_version": row.catalog_schema_version,
        "readiness_policy_version": row.readiness_policy_version,
        "provider_policy_version": row.provider_policy_version,
        "journey_id": row.journey_id,
        "result": result,
        "error": error,
        "submitted_at": row.queued_at,
        "started_at": row.started_at,
        "finished_at": row.finished_at,
        "abandon_requested_at": row.abandon_requested_at,
        **hydration_urls(row.id),
    }


def find_open_operation(
    session: Session, fingerprint: str
) -> CatalogHydrationOperation | None:
    return session.scalar(
        select(CatalogHydrationOperation)
        .where(
            CatalogHydrationOperation.fingerprint == fingerprint,
            CatalogHydrationOperation.status.in_(OPEN_STATUSES),
        )
        .order_by(CatalogHydrationOperation.queued_at.desc())
    )


class TransitionRefused(RuntimeError):
    """A status write that did not match its guard."""


class HydrationStoreBusy(RuntimeError):
    """The store could not take the job inside the acceptance budget (answer 503, not 500)."""


def mark(
    session: Session,
    operation_id: str,
    to_status: str,
    *,
    from_statuses: tuple[str, ...] | None = None,
    commit: bool = True,
    **fields: Any,
) -> bool:
    """Move one job along the state machine, and only from a state that allows it.

    `rowcount` is the arbiter: a worker that finishes next to an abandon resolves into exactly
    one answer instead of a last-write-wins race.
    """

    if to_status not in ALLOWED_TRANSITIONS:
        raise TransitionRefused(f"unknown status {to_status}")
    if from_statuses is None:
        from_statuses = tuple(
            source for source, targets in ALLOWED_TRANSITIONS.items() if to_status in targets
        )
    if not from_statuses:
        raise TransitionRefused(f"no state allows {to_status}")
    for source in from_statuses:
        if to_status not in ALLOWED_TRANSITIONS.get(source, frozenset()):
            raise TransitionRefused(f"{source} -> {to_status} is not a legal transition")
    values = {"status": to_status, "updated_at": now_iso(), **fields}
    result = session.execute(
        update(CatalogHydrationOperation)
        .where(
            CatalogHydrationOperation.id == operation_id,
            CatalogHydrationOperation.status.in_(from_statuses),
        )
        .values(**values)
    )
    if commit:
        session.commit()
    return result.rowcount == 1


def _terminate(
    session: Session,
    operation_id: str,
    to_status: str,
    *,
    phase: str,
    **fields: Any,
) -> str | None:
    """Close a finished job in the state its stage earned, or in the named abandoned branch.

    Why this exists rather than one wide `from_statuses`: `mark` checks every source a call names
    against the table before it touches the store, and the table allows
    `ABANDON_REQUESTED -> {ABANDONED, FAILED}` only.  Naming `ABANDON_REQUESTED` as a source for a
    `COMPLETED` or `UNAVAILABLE` write therefore did not express a race at all - it raised on every
    resolved job whatever the row happened to be, the worker died on its own bookkeeping, and the
    traveller was left with a job that never reports a result.

    The honest reading of "the row is no longer RUNNING" is that somebody asked this job to stop, so
    the abandon wins and the terminal state is `ABANDONED`: publishing a verdict about a destination
    to a request nobody is waiting for would be the laundering the rest of this file refuses.  The
    abandoned branch deliberately carries no `result_json` and no `error_json` - a job that was
    stopped has no result and did not fail.

    Returns the status that landed, or `None` when the row is already terminal or gone.  Never
    raises: the alternative is a dead worker and a job left open, which also keeps owning its
    fingerprint through the partial unique index and hands every later request to a thread that no
    longer exists.
    """

    finished = str(fields.pop("finished_at", None) or now_iso())
    outcome = fields.pop("outcome", None)
    if mark(
        session,
        operation_id,
        to_status,
        from_statuses=(RUNNING,),
        commit=False,
        outcome=outcome,
        phase=phase,
        finished_at=finished,
        **fields,
    ):
        session.commit()
        return to_status
    session.rollback()
    if _close_abandoned(session, operation_id, phase=phase, finished_at=finished):
        return ABANDONED
    return None


def _close_abandoned(session: Session, operation_id: str, **fields: Any) -> bool:
    """Land `ABANDONED` on a job the traveller stopped, however the row currently reads.

    Two shapes, both reached through states the table already allows:

    * the normal one - the durable request landed, so the row is `ABANDON_REQUESTED` and
      `ABANDONED` is a legal next state from there;
    * the one this helper exists for - this process recorded the request but the write that was
      supposed to move the row did not land (another writer held the SQLite lock, which this
      project's own shared-file setup makes routine), so the row still reads `RUNNING`.  Walking
      `RUNNING -> ABANDON_REQUESTED -> ABANDONED` in one transaction keeps the state machine
      honest; adding `RUNNING -> ABANDONED` to the table would not, and would let a job that never
      saw a request close as abandoned.

    Whether it landed is returned rather than swallowed: an ignored `False` is a job left open with
    a flag nobody will honour, and the partial unique index then hands every later request for that
    destination to a thread that has already gone.
    """

    stamp = str(fields.pop("finished_at", None) or now_iso())
    fields.setdefault("outcome", OUTCOME_ABANDONED)
    if mark(
        session,
        operation_id,
        ABANDONED,
        from_statuses=(ABANDON_REQUESTED,),
        commit=False,
        finished_at=stamp,
        **fields,
    ):
        session.commit()
        return True
    session.rollback()
    fresh = read_operation(session, operation_id)
    if fresh is not None and fresh.status == RUNNING:
        requested = mark(
            session,
            operation_id,
            ABANDON_REQUESTED,
            from_statuses=(RUNNING,),
            commit=False,
            abandon_requested_at=stamp,
        )
        if requested and mark(
            session,
            operation_id,
            ABANDONED,
            from_statuses=(ABANDON_REQUESTED,),
            commit=False,
            finished_at=stamp,
            **fields,
        ):
            session.commit()
            return True
        session.rollback()
    return False


def set_phase(session: Session, operation_id: str, phase: str) -> None:
    """Record the stage the worker has actually entered, in its own short transaction.

    Only ever a phase this code is genuinely inside; a stage that does not happen is never
    reported, and the write commits immediately so a concurrent reader is not queued behind it.
    """

    if phase not in PHASES:
        return
    # A phase write must never queue behind somebody else's long transaction waiting for the
    # engine's 30s timeout: this is information, and stale information is better than a stalled
    # job.  The write is then its own transaction, so a reader sees it immediately.
    session.execute(text("PRAGMA busy_timeout = 500"))
    session.execute(
        update(CatalogHydrationOperation)
        .where(
            CatalogHydrationOperation.id == operation_id,
            CatalogHydrationOperation.status.in_((RUNNING, ABANDON_REQUESTED)),
        )
        .values(phase=phase, updated_at=now_iso())
    )
    session.commit()


def submit_operation(
    session: Session,
    query: str,
    *,
    journey_id: str | None = None,
    requested_categories: list[str] | None = None,
    start_worker: bool = True,
) -> tuple[CatalogHydrationOperation, bool]:
    """Return (operation, reused).  Same fingerprint means the same job, never two.

    The read that decides reuse and the write that claims the row are separate short
    transactions: under two concurrent taps the read can miss, and then the partial unique
    index - not a process-local lock - is what makes one of the two the winner, whose id the
    loser reads back and reports.
    """

    # ORD-0213 weak fix (commander released 00:5x): ask the store before asking the world.
    #
    # What this buys: a repeat of a question that is *already being answered* costs one
    # resolution, not two.  Before this line the key existed only after `resolve_destination`
    # returned, so every duplicate request paid the provider/catalog pass and then arrived at a
    # reuse check that could only see work already committed - which is §7's "provider 调用不得因
    # 并发而翻倍" left unguarded.
    #
    # What it deliberately does NOT buy: a job that has already reached a terminal status is still
    # answered with a **new** job (the signed tail-A rule), so this does not close ORD-0213/g3 - the
    # door's own preceding single request can still finish before its five arrive.  Nobody may read
    # this line as that closure.
    #
    # Why it cannot merge two different places: this key is what an *unnamed* destination is filed
    # under; a job that resolved is keyed by its canonical id, which `pending_request_fingerprint`
    # cannot produce.  And `ABANDON_REQUESTED` is excluded on purpose - that job is heading to
    # ABANDONED because a traveller walked away, and joining it would strand the newcomer behind a
    # job nobody is waiting for any more.
    in_flight = find_open_operation(session, pending_request_fingerprint(query))
    if in_flight is not None and str(in_flight.status) in (QUEUED, RUNNING):
        return in_flight, True

    resolution = resolve_destination(session, query)
    fingerprint = capability_fingerprint(
        resolution.canonical_destination_id,
        resolver_query_normalized=resolution.resolver_query_normalized,
    )
    # Kept by the commander's disposition (ORD-0224, 2026-09-23 03:0x) and by measurement, not by
    # habit: mutant **Y1b** at `f92f9e9` switched this read off and all twelve doors stayed green, so
    # **no gate depends on it today** - it is a second chance, behind the pre-resolution read and in
    # front of the durable index + loser read-back that actually carry convergence (`Y3`).  Deleting it
    # now would reopen the concurrency path for no evidence, and that is the strong fix's work surface;
    # if it ever goes, it goes in that order, in writing.
    existing = find_open_operation(session, fingerprint)
    if existing is not None:
        return existing, True
    stamp = now_iso()
    request_json = json.dumps(
        {"query": str(query), "requested_categories": sorted(requested_categories or [])},
        ensure_ascii=False,
        sort_keys=True,
    )
    operation_id = str(uuid.uuid4())
    # The token belongs to the operation, so it is named here and not by the resolver: a read that
    # never opened a job has no correlation material to hand out.  Still never derived from the
    # query text, and still never an identity - `places` must not become joinable on it.
    fields = identity_fields(resolution)
    if fields["canonical_destination_id"] is None:
        fields["destination_pending_key"] = pending_correlation_key(operation_id)
    staged: list[CatalogHydrationOperation] = []

    def stage() -> CatalogHydrationOperation:
        row = CatalogHydrationOperation(
            id=operation_id,
            fingerprint=fingerprint,
            **fields,
            catalog_schema_version=CATALOG_SCHEMA_VERSION,
            readiness_policy_version=READINESS_POLICY_VERSION,
            provider_policy_version=PROVIDER_POLICY_VERSION,
            journey_id=journey_id,
            status=QUEUED,
            phase=QUEUED,
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
        session.commit()
    except IntegrityError:
        session.rollback()
        winner = find_open_operation(session, fingerprint)
        if winner is None:
            raise
        return winner, True
    except Exception:
        session.rollback()
        raise HydrationStoreBusy("the catalog store refused the new job")
    # A 202 promises "this job exists, ask it later", so the promise is checked against the
    # store rather than against the object in memory: `expire_on_commit=False` would happily
    # hand back a row a rolled-back retry never wrote.
    durable = session.scalar(
        select(func.count())
        .select_from(CatalogHydrationOperation)
        .where(CatalogHydrationOperation.id == operation_id)
    )
    if not durable:
        raise HydrationStoreBusy(f"operation {operation_id} reported a commit the store does not hold")
    if start_worker:
        dispatch(operation_id)
    return staged[-1], False


def read_operation(
    session: Session, operation_id: str
) -> CatalogHydrationOperation | None:
    return session.get(CatalogHydrationOperation, operation_id)


def abandon_operation(session: Session, operation_id: str) -> CatalogHydrationOperation:
    """Ask a job to stop.  Idempotent, and honest about what just happened."""

    row = read_operation(session, operation_id)
    if row is None:
        raise LookupError(operation_id)
    if row.status in TERMINAL_STATUSES:
        return row
    request_abandonment(operation_id)
    if row.status == ABANDON_REQUESTED:
        return row
    was_queued_without_worker = row.status == QUEUED and not has_worker(operation_id)
    try:
        moved = mark(
            session,
            operation_id,
            ABANDON_REQUESTED,
            from_statuses=(QUEUED, RUNNING),
            abandon_requested_at=now_iso(),
        )
    except Exception:  # noqa: BLE001 - the in-process event above is the live signal
        session.rollback()
        moved = False
    if not moved:
        session.rollback()
    if was_queued_without_worker and moved:
        # The traveller stopped a job no thread here can ever run, so "we asked it to stop" would
        # be a promise with nobody left to keep it: close it now. Left open, it would also keep
        # owning the fingerprint through the partial unique index, and every later request for
        # that destination would be handed this dead job instead of starting its own.
        mark(
            session,
            operation_id,
            ABANDONED,
            from_statuses=(ABANDON_REQUESTED,),
            outcome=OUTCOME_ABANDONED,
            finished_at=now_iso(),
        )
    fresh = read_operation(session, operation_id)
    assert fresh is not None, operation_id
    return fresh


def has_worker(operation_id: str) -> bool:
    """Whether this process still has a thread that can finish this job.

    `PROCESS_LOCAL_ONLY` again, stated rather than hidden: the durable row cannot tell "a worker
    is about to pick me up" from "the worker died with its process", and an abandon endpoint that
    assumed the former would leave the row open forever.
    """

    with _EXECUTOR_LOCK:
        return operation_id in _dispatched


def dispatch(operation_id: str) -> None:
    with _EXECUTOR_LOCK:
        if operation_id in _dispatched:
            return
        _dispatched.add(operation_id)
    get_executor().submit(_run_operation, operation_id)


def _failure_payload(code: str, message: str, **details: Any) -> str:
    return json.dumps(
        {
            "code": code,
            "message": message,
            "message_key": f"error.{code.lower()}",
            "reason_code": details.pop("reason_code", code),
            "details": details,
        },
        ensure_ascii=False,
    )


def _survey(session: Session, canonical_destination_id: str | None) -> dict[str, Any]:
    """What the registered deterministic sources hold for this destination.  Read-only.

    `canonical_destination_id` is the *identity or null* - the caller passes what the resolver
    concluded, and null is answered by counting nothing.  It used to be answered by testing for a
    `pending:` prefix, which meant the meaning of a row depended on a string convention instead of
    on the database's own distinction between "this place" and "no place yet".

    `pending_materialisations` counts captured provider candidates that have not become
    catalogue rows yet - a real number from the store, not a promise that this build will act
    on them (turning them into `places` rows is a write path of its own, and ORD-0095-1 was
    scoped to existing sources; see the delivery note).
    """

    city_code = canonical_destination_id
    held = (
        int(
            session.scalar(select(func.count(Place.id)).where(Place.city_code == city_code))
            or 0
        )
        if city_code
        else 0
    )
    pending = (
        int(
            session.scalar(
                select(func.count(RealPlaceCandidate.id)).where(
                    RealPlaceCandidate.city_code == city_code,
                    RealPlaceCandidate.materialized_place_id.is_(None),
                )
            )
            or 0
        )
        if city_code
        else 0
    )
    return {"city_code": city_code, "places_held": held, "pending_materialisations": pending}


def _run_operation(operation_id: str) -> None:  # noqa: PLR0912 - one stage per block, in order
    """The §6 order of operations, in code.

    Every stage opens its own short session and closes it before the next one starts, so no
    write transaction is ever held across the survey or across provider work.
    """

    session = SessionLocal()
    try:
        row = read_operation(session, operation_id)
        if row is None:
            return
        if row.status == ABANDON_REQUESTED:
            # The traveller stopped using this job *before* this thread ever picked it up.
            # Returning here would leave it open forever: `ABANDON_REQUESTED` counts as an open
            # status, so the partial unique index would keep handing every later request with the
            # same fingerprint to a job that has no worker - and none of them would ever finish.
            if not _close_abandoned(session, operation_id):
                logger.error("could not close abandoned hydration %s", operation_id)
            return
        if row.status != QUEUED:
            return
        if not mark(
            session,
            operation_id,
            RUNNING,
            from_statuses=(QUEUED,),
            phase=QUEUED,
            started_at=now_iso(),
        ):
            # Somebody moved the row between the read above and this guarded write - in practice
            # an abandon that landed in that window.  Re-read and close honestly instead of
            # running a stage machine on a job that is no longer ours to run.
            fresh = read_operation(session, operation_id)
            if fresh is not None and fresh.status == ABANDON_REQUESTED:
                if not _close_abandoned(session, operation_id):
                    logger.error("could not close abandoned hydration %s", operation_id)
            return

        set_phase(session, operation_id, "RESOLVING_DESTINATION")
        # short read, then release: the resolution is a lookup, not a transaction to hold open.
        resolution = resolve_destination(session, json.loads(row.request_json or "{}").get("query") or "")
        survey = _survey(session, resolution.canonical_destination_id)

        set_phase(session, operation_id, "DISCOVERING_POIS")
        if resolution.state == "RUNTIME_FAILURE":
            # Retryable, and named as such (R1): one of our own sources failed, which says nothing
            # about the destination.  Ending the job UNAVAILABLE here would be the same laundering
            # this branch replaces, just written to a row instead of to a response.
            mark(
                session,
                operation_id,
                FAILED,
                from_statuses=(RUNNING, ABANDON_REQUESTED),
                outcome=OUTCOME_RESOLVER_RUNTIME_FAILURE,
                phase="DISCOVERING_POIS",
                error_json=_failure_payload(
                    "DESTINATION_RESOLVER_UNAVAILABLE",
                    "A destination source this build depends on could not be read; the request is "
                    "unchanged and may be submitted again.",
                    reason_code=REASON_RESOLVER_UNAVAILABLE,
                    retryable=True,
                    **identity_fields(resolution),
                    # Every attempt, not just the one that produced the conclusion: the row outlives
                    # the request, and an outage has to stay visible after the job says NO_MATCH.
                    **audit_evidence(resolution),
                ),
                finished_at=now_iso(),
            )
            return
        if resolution.state == "AMBIGUOUS":
            _terminate(
                session,
                operation_id,
                UNAVAILABLE,
                phase="DISCOVERING_POIS",
                outcome=OUTCOME_DESTINATION_AMBIGUOUS,
                error_json=_failure_payload(
                    "DESTINATION_AMBIGUOUS",
                    f"'{resolution.query}' names more than one destination this product can see; "
                    "no winner was guessed.",
                    reason_code=REASON_DESTINATION_AMBIGUOUS,
                    candidates=resolution.candidates,
                    **identity_fields(resolution),
                    **audit_evidence(resolution),
                ),
                finished_at=now_iso(),
            )
            return
        if not resolution.resolved:
            # Structural: nothing in this build can name the destination.  This is *not* the
            # branch a failing resolver takes - a provider that could not be reached stays
            # retryable (R1), and there is no such failure here because no provider is consulted.
            _terminate(
                session,
                operation_id,
                UNAVAILABLE,
                phase="DISCOVERING_POIS",
                outcome=OUTCOME_NO_REGISTERED_SOURCE,
                error_json=_failure_payload(
                    "DESTINATION_IDENTITY_UNRESOLVED",
                    f"'{resolution.query}' cannot be resolved to a destination this product can "
                    "name from a traceable source.",
                    reason_code=REASON_NO_CANONICAL_IDENTITY,
                    # The claim is counted, not asserted: these are the rows the survey actually
                    # found for *this* request.  A non-zero number here while the identity is null
                    # would mean something was matched on text or on the correlation token.
                    survey=survey,
                    **identity_fields(resolution),
                    # The transport contract this emptiness depends on is part of the record: an
                    # answer that says "nothing" and a mirror that never replied are different facts,
                    # and a reader must be able to tell them apart from the row alone.
                    **audit_evidence(resolution),
                ),
                finished_at=now_iso(),
            )
            return

        set_phase(session, operation_id, "NORMALIZING")
        capability = evaluate_capability(session, resolution.query, resolution=resolution)

        set_phase(session, operation_id, "BUILDING_MOBILITY")
        mobility = capability["coverage_detail"]["mobility"]

        set_phase(session, operation_id, "PERSISTING")
        # One short transaction, and it writes the job's own result - no catalogue row is
        # invented here, because in this build the registered sources are already applied.
        if survey["places_held"] == 0 and survey["pending_materialisations"] == 0:
            outcome = OUTCOME_NO_REGISTERED_SOURCE
        elif survey["pending_materialisations"] == 0:
            outcome = OUTCOME_ALREADY_CURRENT
        else:
            # Candidates exist that this build does not promote into the catalogue.  Named
            # rather than rounded up to "prepared": a caller that reads COMPLETED must still be
            # able to see that the gaps it asked about are the gaps that remain.
            outcome = OUTCOME_PENDING_UNAPPLIED
        body = {
            **identity_fields(resolution),
            "matched_label": resolution.matched_label,
            "capability_after": capability["capability"],
            "coverage_after": capability["coverage"],
            "missing_categories": capability["missing_categories"],
            "survey": survey,
            "mobility_evidence": {
                "explicit_pairs": mobility["candidate_count"],
                "state": mobility["state"],
                # Repeated here on purpose: the job is the record an auditor reads a week later,
                # and "there were N legs" without "none of them was hard-eligible" is the half that
                # made a prepared destination look stronger than it was.
                "source_classes": mobility.get("source_classes") or [],
                "hard_eligible": bool(mobility.get("hard_eligible")),
                "hard_gaps": capability.get("hard_gaps") or [],
            },
            "source_kinds": capability["provenance"]["source_kinds"],
            "outcome": outcome,
            # A COMPLETED/UNAVAILABLE ending that named a destination carries the provider's answer
            # with it: entity reference, WGS84 point, extent, timestamps, and every attempt that led
            # here.  Without this the identity in the column has nothing behind it to chase.
            **audit_evidence(resolution),
        }

        set_phase(session, operation_id, "FINALIZING")
        if abandonment_requested(operation_id):
            # The request is recorded in this process; whether the durable move landed is a separate
            # question, and the row may still read `RUNNING` because another writer held the lock.
            # `_close_abandoned` answers both shapes through states the table allows.
            if not _close_abandoned(session, operation_id, phase="FINALIZING"):
                logger.error("could not close abandoned hydration %s", operation_id)
            return
        # One call, two honest endings: the state this run earned if the row is still ours to
        # finish, the named abandoned branch if an abandon moved it in between.  This line used to
        # name `ABANDON_REQUESTED` as a source for a `COMPLETED`/`UNAVAILABLE` write, which the
        # transition table forbids, so `mark` raised on every resolved job before it reached the
        # store - see `_terminate` for why widening the table was the wrong repair.
        _terminate(
            session,
            operation_id,
            COMPLETED if outcome != OUTCOME_NO_REGISTERED_SOURCE else UNAVAILABLE,
            phase="FINALIZING",
            outcome=outcome,
            result_json=json.dumps(body, ensure_ascii=False, default=str),
            finished_at=now_iso(),
        )
    except BaseException as exc:  # noqa: BLE001 - an operation must never outlive its worker
        logger.exception("catalog hydration worker died for %s", operation_id)
        try:
            session.rollback()
        except Exception:  # noqa: BLE001 - best effort
            pass
        _force_fail(operation_id, exc)
    finally:
        session.close()
        forget_abandonment(operation_id)
        with _EXECUTOR_LOCK:
            _dispatched.discard(operation_id)


def _force_fail(operation_id: str, exc: BaseException) -> None:
    session = SessionLocal()
    try:
        mark(
            session,
            operation_id,
            FAILED,
            from_statuses=OPEN_STATUSES,
            error_json=_failure_payload(
                "HYDRATION_WORKER_DIED",
                f"{type(exc).__name__}: {exc}",
                reason_code="HYDRATION_WORKER_DIED",
                exception=type(exc).__name__,
            ),
            finished_at=now_iso(),
        )
    except Exception:  # noqa: BLE001 - nothing left to try
        logger.exception("could not close hydration operation %s", operation_id)
    finally:
        session.close()


def reconcile_interrupted_operations() -> int:
    """Close jobs no thread in this process can finish any more (restart hygiene)."""

    session = SessionLocal()
    try:
        stale = session.scalars(
            select(CatalogHydrationOperation).where(
                CatalogHydrationOperation.status.in_(OPEN_STATUSES)
            )
        ).all()
        count = 0
        for row in stale:
            landed = mark(
                session,
                row.id,
                FAILED,
                from_statuses=OPEN_STATUSES,
                commit=False,
                outcome=OUTCOME_WORKER_LOST,
                error_json=_failure_payload(
                    "WORKER_LOST",
                    "This hydration was running in a server process that has since restarted, "
                    "so its result was never written. Ask for it again.",
                    reason_code="WORKER_LOST",
                    status_at_restart=row.status,
                    phase_at_restart=row.phase,
                ),
                finished_at=now_iso(),
            )
            count += 1 if landed else 0
        session.commit()
        return count
    except Exception:  # noqa: BLE001 - startup must not be held hostage by a cleanup pass
        session.rollback()
        logger.exception("could not reconcile interrupted catalog hydrations")
        return 0
    finally:
        session.close()


@dataclass
class SubmittedJob:
    operation_id: str
    reused: bool
    payload: dict[str, Any]


def accept_hydration(
    session: Session,
    query: str,
    *,
    journey_id: str | None = None,
    requested_categories: list[str] | None = None,
) -> SubmittedJob:
    """The door's own words: accept, and say whether this call started the work or joined it."""

    row, reused = submit_operation(
        session, query, journey_id=journey_id, requested_categories=requested_categories
    )
    payload = operation_payload(row)
    payload["reused"] = reused
    payload["fingerprint_parts"] = fingerprint_parts(
        row.canonical_destination_id,
        resolver_query_normalized=row.resolver_query_normalized,
    )
    return SubmittedJob(operation_id=row.id, reused=reused, payload=payload)


def wait_for_operation(
    operation_id: str,
    *,
    poll_seconds: float = 0.05,
    deadline_seconds: float | None = None,
    on_tick: Callable[[], None] | None = None,
) -> CatalogHydrationOperation | None:
    """Block until terminal, reading the row exactly as a client would - no second code path."""

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
