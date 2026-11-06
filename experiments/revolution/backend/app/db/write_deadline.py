"""ORD-0296 (ORD-0281's backend half): the server's own budget for one write attempt.

Two numbers, one source.  `busy_timeout_ms` is what SQLite is told to wait for a lock; `deadline_ms`
is what this service is willing to spend on one request's writes.  The default deadline must sit
strictly above the lock wait: at or below it, the failure a traveller is told about is *my
configuration* rather than the lock's reality -- a budget that one full SQLite wait already blows
just relabels `busy_timeout` as "deadline exceeded" (ORD-0281 T-b, ratified as Q36=①).

Nothing here lengthens a wait.  A request that used to answer at 30 s still answers at 30 s; what
changes is that a write which loses the lock *after* exhausting its budget now says so under a name
the caller can read (`SERVER_WRITE_DEADLINE_EXCEEDED`, inside the ORD-0055 envelope) instead of
reaching the framework's contentless 500.  Inside budget, the original error propagates untouched.
"""
from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

from sqlalchemy.exc import OperationalError

from app.core.config import settings

SCENARIO_AGENT_COMMANDS = "agent.commands"
SCENARIO_PLANS_GENERATE = "plans.generate"
#: ORD-0327: every other write route.  A request without a named scenario still carries a clock, so
#: a lost lock past its budget names itself instead of reaching the framework's contentless 500.
#: Background work is deliberately excluded -- the clock is armed by the HTTP middleware and
#: `boundary_verdict` only speaks for a request that armed one, so a job's `OperationalError` stays
#: the error its own handlers were written against.
SCENARIO_DEFAULT_WRITE = "default.write"
#: The only place a scenario name exists.  Routes refer to their scenario through these constants, so
#: a second inline copy of either the name or the number cannot be made (ORD-0281 T-c's killer).
SCENARIOS: tuple[str, ...] = (SCENARIO_AGENT_COMMANDS, SCENARIO_PLANS_GENERATE, SCENARIO_DEFAULT_WRITE)

CODE_DEADLINE = "SERVER_WRITE_DEADLINE_EXCEEDED"
CONFIG_OK = "WRITE_DEADLINE_CONFIG_OK"
CONFIG_NOT_ABOVE_BUSY = "DEADLINE_NOT_ABOVE_BUSY_TIMEOUT"
CONFIG_NOT_POSITIVE = "DEADLINE_NOT_POSITIVE"
CONFIG_SCENARIO_UNKNOWN = "DEADLINE_SCENARIO_UNKNOWN"
CONFIG_OVERRIDE_MALFORMED = "DEADLINE_SCENARIO_OVERRIDE_MALFORMED"


class WriteDeadlineConfigurationError(RuntimeError):
    """Raised by the startup door: the shipped numbers make the deadline unable to mean anything."""


def busy_timeout_ms() -> int:
    """The only reader of the lock wait.  `app/db/session.py` asks this for the number."""

    return int(settings.write_busy_timeout_ms)


def busy_timeout_seconds() -> float:
    """The same number in the unit `sqlite3.connect(timeout=...)` wants: one source, two spellings."""

    return busy_timeout_ms() / 1000


def scenario_overrides() -> dict[str, int]:
    """`plans.generate:60000,agent.commands:50000`.  A non-integer value becomes -1 and is named."""

    out: dict[str, int] = {}
    for item in (settings.write_deadline_scenarios or "").split(","):
        item = item.strip()
        if not item:
            continue
        name, _, value = item.partition(":")
        out[name.strip()] = int(value) if value.strip().isdigit() else -1
    return out


def deadline_ms(scenario: str) -> int:
    overrides = scenario_overrides()
    if scenario in overrides:
        declared = overrides[scenario]
        if declared <= 0:
            raise ValueError(f"{CONFIG_OVERRIDE_MALFORMED}: {scenario} override is not an integer")
        return declared
    return int(settings.write_deadline_ms)


def deadline_source(scenario: str) -> str:
    return "scenario_override" if scenario in scenario_overrides() else "default"


def effective_busy_timeout_ms(scenario: str) -> int:
    """A write never waits longer than the budget it declared; by default the budget is the larger."""

    return max(1, min(busy_timeout_ms(), deadline_ms(scenario)))


def deadline_is_reachable(deadline: int, busy_timeout: int) -> bool:
    """Pure, so the two-way gate can drive both sides without sitting through two real waits."""

    return deadline > 0 and busy_timeout > 0 and deadline > busy_timeout


def scenario_verdict(scenario: str) -> dict[str, object]:
    busy = busy_timeout_ms()
    if scenario not in SCENARIOS:
        return {"verdict": CONFIG_SCENARIO_UNKNOWN, "busy_timeout_ms": busy}
    try:
        deadline = deadline_ms(scenario)
    except ValueError:
        # a malformed override is a configuration finding the door can name, not a crash on boot
        return {"verdict": CONFIG_OVERRIDE_MALFORMED, "busy_timeout_ms": busy}
    if deadline <= 0 or busy <= 0:
        verdict = CONFIG_NOT_POSITIVE
    elif not deadline_is_reachable(deadline, busy):
        verdict = CONFIG_NOT_ABOVE_BUSY
    else:
        verdict = CONFIG_OK
    return {"deadline_ms": deadline, "busy_timeout_ms": busy, "delta_ms": deadline - busy,
            "deadline_source": deadline_source(scenario),
            "effective_busy_timeout_ms": effective_busy_timeout_ms(scenario),
            "reachable": deadline_is_reachable(deadline, busy), "verdict": verdict}


def configuration_verdict(scenario: str | None = None) -> dict[str, object]:
    """Which of the two numbers wins -- for one scenario, or for every declared scenario."""

    targets = [scenario] if scenario is not None else list(SCENARIOS)
    per_scenario = {name: scenario_verdict(name) for name in targets}
    verdicts = {str(v["verdict"]) for v in per_scenario.values()}
    return {"scenario": scenario, "busy_timeout_ms": busy_timeout_ms(),
            "default_deadline_ms": int(settings.write_deadline_ms),
            "scenario_overrides": scenario_overrides(), "per_scenario": per_scenario,
            "verdict": CONFIG_OK if verdicts == {CONFIG_OK} else sorted(verdicts)[0]}


def assert_configuration_reachable() -> dict[str, object]:
    """The startup door.  Ships nothing that cannot name itself truthfully later."""

    report = configuration_verdict()
    if report["verdict"] != CONFIG_OK:
        raise WriteDeadlineConfigurationError(
            f"{report['verdict']}: write_deadline_ms must be strictly above "
            f"write_busy_timeout_ms={busy_timeout_ms()} for every scenario in {list(SCENARIOS)}; "
            f"got {report['per_scenario']}"
        )
    return report


class WriteDeadlineExceeded(RuntimeError):
    """This request's write budget is gone and a lost lock is why it did not finish."""

    code = CODE_DEADLINE

    def __init__(self, scenario: str, elapsed_ms: int, deadline: int, cause: str) -> None:
        super().__init__(
            f"{CODE_DEADLINE}: scenario={scenario} spent {elapsed_ms} ms of a {deadline} ms write "
            f"budget (sqlite busy_timeout={busy_timeout_ms()} ms): {cause}"
        )
        self.scenario = scenario
        self.elapsed_ms = elapsed_ms
        self.deadline_ms = deadline
        self.busy_timeout_ms = busy_timeout_ms()
        self.cause = cause

    def details(self) -> dict[str, object]:
        return {"scenario": self.scenario, "elapsed_ms": self.elapsed_ms,
                "deadline_ms": self.deadline_ms, "busy_timeout_ms": self.busy_timeout_ms,
                "server_sentence": self.cause}


@dataclass
class WriteBudget:
    """One request's write clock.  Every commit in that request shares it, so waits add up."""

    scenario: str
    deadline_ms: int
    started_monotonic: float = field(default_factory=time.monotonic)
    clean_attempts: int = 0
    waits_lost: int = 0

    @property
    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self.started_monotonic) * 1000)

    def expired(self) -> bool:
        return self.elapsed_ms >= self.deadline_ms


_CURRENT: ContextVar[WriteBudget | None] = ContextVar("citynexus_write_budget", default=None)


def start_request_budget(scenario: str) -> WriteBudget:
    budget = WriteBudget(scenario=scenario, deadline_ms=deadline_ms(scenario))
    _CURRENT.set(budget)
    return budget


def current_budget(scenario: str) -> WriteBudget:
    budget = _CURRENT.get()
    if budget is None or budget.scenario != scenario:
        return start_request_budget(scenario)
    return budget


def reset_request_budget() -> None:
    _CURRENT.set(None)


def is_lock_contention(exc: BaseException) -> bool:
    detail = str(exc).casefold()
    return "database is locked" in detail or "database is busy" in detail


@contextmanager
def write_attempt(scenario: str) -> Iterator[WriteBudget]:
    """Bound one write transaction of a request.  A successful write behaves exactly as before.

    Attempts nest (a route wraps its whole write section and calls `commit_within_budget` inside
    it).  Both levels share this request's clock, so one lost lock can be *counted* twice in
    `waits_lost`; no verdict rides on that counter -- the inner level names the answer first.
    """

    budget = current_budget(scenario)
    try:
        yield budget
    except OperationalError as exc:
        if not is_lock_contention(exc):
            raise
        budget.waits_lost += 1
        if not budget.expired():
            raise
        raise WriteDeadlineExceeded(scenario, budget.elapsed_ms, budget.deadline_ms,
                                    str(exc)) from exc
    budget.clean_attempts += 1


def commit_within_budget(session, scenario: str) -> int:
    """`session.commit()` for a caller that named its scenario; returns the spend so far."""

    with write_attempt(scenario) as budget:
        session.commit()
    return budget.elapsed_ms


def active_budget() -> WriteBudget | None:
    """The clock this request armed, or None for a write outside any request (a job, a script)."""

    return _CURRENT.get()


def arm_request_budget(scenario: str = SCENARIO_DEFAULT_WRITE) -> WriteBudget:
    """Start the clock for one request.  Called by the HTTP middleware, not by a route."""

    return start_request_budget(scenario)


def boundary_verdict(exc: BaseException) -> WriteDeadlineExceeded | None:
    """What this request's clock says about an escaping database error -- or None to leave it alone.

    This is the one judge at the boundary, and it is deliberately narrow.  A write that lost the lock
    *before* the budget ran out is today's error, unchanged: returning None means the caller re-raises
    the same object and the traveller's answer stays exactly what it was.  An error that is not lock
    contention is a bug, not a wait, and renaming it would launder it into a 503.  And with no clock
    armed -- a job, a script -- nothing here applies.

    The judge sits at the boundary rather than around `Session.commit()` because a request's writes do
    not all leave at commit: `app/main.py:718` flushes mid-route (674 in the generation this branch is
    cut from), so a commit-only wrapper would bound part of the same request and leave the rest
    reaching the framework's contentless 500.
    """

    budget = _CURRENT.get()
    if budget is None or not is_lock_contention(exc) or not budget.expired():
        return None
    return WriteDeadlineExceeded(budget.scenario, budget.elapsed_ms, budget.deadline_ms, str(exc))
