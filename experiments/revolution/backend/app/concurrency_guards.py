"""Journey-scoped serialization for authoritative state transitions.

Why this module exists
----------------------
Three product operations each read the same authoritative facts, validate them
against what they read, and then write:

  1. accepting a replan proposal   (base plan -> SUPERSEDED, child -> ACTIVE)
  2. capturing a booking payment   (Commitment promoted to a protected level)
  3. the commitment re-pointing that an accepted replan performs immediately after

Two concurrent callers can therefore both pass validation against the same
pre-transition state and both write.  A unique constraint cannot express this
("at most one ACTIVE PlanVersion per Journey" is a conditional property, and
`replan_proposals` carries no such index), so the guard has to be at the
transaction boundary rather than at the final UPDATE.

Scope of the lock
-----------------
It is taken *before* the read that begins validation and released *after* the
commit — not around the write.  Releasing between validation and commit is the
bug this exists to prevent: the second caller would then validate against state
that the first caller had already invalidated.

PROCESS_LOCAL_ONLY = True
-------------------------
This is a process-local `threading.RLock` registry.  It serializes workers
inside one API process only.  It is **not** multi-worker safe and **not**
distributed safe: two uvicorn processes, two gunicorn workers, or two replicas
behind a load balancer each own a private registry and will happily validate
against the same stale row at the same time.  The reference deployment this was
written against is a single process on SQLite, where SQLite also does not honour
`SELECT ... FOR UPDATE`, so there is no database-level backstop to lean on.

Any move to multi-process serving must replace or supplement this with a
database-backed or advisory lock (for example `SELECT ... FOR UPDATE` on the
Journey row under postgres, or a real lock service).  The call sites are written
so that swapping the mechanism does not change any business invariant: they hold
the lock across read/validate/mutate/commit and assert final truth afterwards,
never "my status code was 200".

The registry uses a weak-valued map so a Journey id that no longer has a waiting
thread does not pin its lock forever.
"""

from __future__ import annotations

from contextlib import contextmanager
from threading import RLock
from typing import Iterator
from weakref import WeakValueDictionary

#: Disclosed deliberately: see the module docstring.  Anything that reads this
#: module, or any report that cites it, must not describe this as distributed
#: safety.
PROCESS_LOCAL_ONLY = True

_registry_guard = RLock()
_journey_locks: WeakValueDictionary[str, RLock] = WeakValueDictionary()


def journey_lock_registry() -> dict[str, RLock]:
    """Read-only view of the live registry, for tests and diagnostics."""

    with _registry_guard:
        return dict(_journey_locks)


@contextmanager
def journey_write_lock(journey_id: str) -> Iterator[None]:
    """Serialize the complete read/validate/mutate/commit transition for one Journey.

    Re-entrant on purpose: an accepted replan re-enters the commitment and audit
    helpers that take the same Journey-scoped lock, and a non-reentrant lock
    would deadlock the very path it guards.
    """

    key = str(journey_id)
    with _registry_guard:
        lock = _journey_locks.get(key)
        if lock is None:
            lock = RLock()
            _journey_locks[key] = lock
    with lock:
        yield
