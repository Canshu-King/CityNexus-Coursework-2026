"""One cross-process caller used by the `G3_XPROC_LOSER_READBACK` door. NOT a test module.

Deliberately a separate file that the door spawns as its own OS process: the invariant being tested
(idempotency survives two workers that do not share a process) cannot be measured from inside one
interpreter, where a session, an identity map and a module-level dispatch set are all shared. So this
script is run N times by the door, each child gets the app-resolved test database URL through
`CITYNEXUS_DATABASE_URL`, and the door synchronises them on a socket - announce ready, block, get
released together. No sleep is used as a synchroniser anywhere.

Modes
  blind   the first two `find_open_operation` calls of each submit answer "nothing is open". That is
          the earliest possible interleaving from the caller's point of view, and it leaves exactly one
          thing able to deliver convergence: the partial unique index plus the loser's read-back of
          the winner's id inside the `IntegrityError` handler.
  honest  no instrument at all - what the plain race looks like across processes.

Emits exactly one JSON line on stdout so the parent can read it back.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import time


def wait_for_release(port: int, label: str) -> float:
    sock = socket.create_connection(("127.0.0.1", int(port)), timeout=60)
    sock.sendall(f"ready {label}\n".encode())
    sock.settimeout(60)
    started = time.perf_counter()
    sock.recv(1)
    return round((time.perf_counter() - started) * 1000, 2)


def main() -> int:
    mode, db_url, port, label, question = sys.argv[1:6]
    os.environ["CITYNEXUS_DATABASE_URL"] = db_url
    record: dict = {"label": label, "mode": mode}
    try:
        record["pid"] = os.getpid()
        import threading

        import app.catalog_hydration_operations as operations
        from app.db.session import SessionLocal

        if mode == "blind":
            original_find = operations.find_open_operation
            per_caller = threading.local()

            def blind_first_two(session, key):
                per_caller.n = getattr(per_caller, "n", 0) + 1
                return None if per_caller.n <= 2 else original_find(session, key)

            operations.find_open_operation = blind_first_two
            record["instrument"] = "first two open-operation lookups answered None"
        elif mode == "honest":
            record["instrument"] = "none"
        else:
            raise SystemExit(f"unknown mode {mode}")

        record["waited_for_release_ms"] = wait_for_release(int(port), label)
        started = time.perf_counter()
        with SessionLocal() as session:
            row, reused = operations.submit_operation(session, question, start_worker=False)
            record.update({"operation_id": str(row.id), "fingerprint": str(row.fingerprint),
                           "reused": bool(reused), "status": str(row.status)})
        record["submit_ms"] = round((time.perf_counter() - started) * 1000, 1)
        print(json.dumps(record, ensure_ascii=False), flush=True)
        return 0
    except Exception as exc:  # noqa: BLE001
        record["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
        print(json.dumps(record, ensure_ascii=False), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
