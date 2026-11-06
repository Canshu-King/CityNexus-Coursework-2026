"""One HTTP caller's server for G3's cross-process door: a real uvicorn process, no test client.

Usage:  python _qoder_g3_http_worker.py <port> <database-url>

The door (``test_qoder_anywhere_gates.py``) spawns two of these on two ports against ONE fresh
temporary database, which is the shape arbiter A-2 asked for after ruling the store-level probe
``ACCEPTED_STORE_LEVEL_SUPPORTING_EVIDENCE``: it must cross the HTTP / app-process boundary, because
that is where a traveller actually is.

The only thing this file changes about the product is that the *background worker* is suppressed,
so the generation the door is testing stays OPEN instead of being answered and terminalised by the
very process that accepted it.  It is done by replacing ``dispatch`` with a logger, which touches
nothing upstream of the decision under test: the pre-check reads, the provider/canonical step, the
partial unique index and the loser's read-back all run as they do in production.  Each suppression
is printed as a stdout line, so "which process actually created the row" is readable from the logs
and not only from the HTTP body.
"""
from __future__ import annotations

import json
import os
import sys


def main() -> int:
    port = int(sys.argv[1])
    database_url = sys.argv[2]
    os.environ["CITYNEXUS_DATABASE_URL"] = database_url
    os.environ.setdefault("CITYNEXUS_DEMO_MODE", "true")

    import uvicorn  # noqa: PLC0415
    from app import catalog_hydration_operations as operations  # noqa: PLC0415
    from app.db.session import engine  # noqa: PLC0415
    from app.main import app  # noqa: PLC0415 - imports ensure_schema and reconciles at boot

    def _suppress(operation_id: str) -> None:
        print(json.dumps({"kind": "dispatch_suppressed", "pid": os.getpid(), "port": port,
                          "operation_id": str(operation_id)}, ensure_ascii=False), flush=True)

    operations.dispatch = _suppress

    print(json.dumps({"kind": "serving", "pid": os.getpid(), "ppid": os.getppid(), "port": port,
                      "database": database_url, "resolved_db_file": str(engine.url.database)},
                     ensure_ascii=False), flush=True)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
