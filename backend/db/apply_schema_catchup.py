"""ORD-0488 - bring one named SQLite store up to the schema this tree declares.

Dry-run unless `--apply` is passed, in the shape of `apply_ord0355_stay_budget_declared.py`:
nothing here picks a database for you, and the report path opens the store strictly read-only.

    python backend/db/apply_schema_catchup.py path/to/store.db            # report, mode=ro
    python backend/db/apply_schema_catchup.py path/to/store.db --apply    # heal

`app.db.session.ensure_schema` already runs on every boot (the bare `ensure_schema()` call in the
startup block of `backend/app/main.py`, beside `assert_configuration_reachable()`) and creates
whatever `backend/db/schema.sql` declares that the store is missing, so a new generation deployed
over an old database heals itself on its own way in.  This script exists for the operator who wants
to land that step deliberately, and for the one who has to state WHICH tables were added -- after
`--apply` the answer comes from `ensure_schema`'s own report, not from counting files.

The shared preview database is refused outright: `D:/moodle/_qoder_preview_20260920/_preview.db`
is another process's live store and this order says read-only on it.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

SHARED_PREVIEW_DB = "D:/moodle/_qoder_preview_20260920/_preview.db"


def declared_tables() -> list[str]:
    """The table names this tree's schema script creates, read off the DDL."""
    import re

    root = Path(__file__).resolve().parents[1]
    sql = (root / "db" / "schema.sql").read_text(encoding="utf-8")
    return sorted(set(re.findall(r'CREATE TABLE IF NOT EXISTS\s+"?([a-z_0-9]+)"?', sql, re.I)))


def present_tables(path: str, *, read_only: bool = True) -> set[str]:
    uri = f"file:{Path(path).as_posix()}?mode=ro" if read_only else f"file:{Path(path).as_posix()}"
    conn = sqlite3.connect(uri, uri=True)
    try:
        return {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            if not str(row[0]).startswith("sqlite_")
        }
    finally:
        conn.close()


def refuse_shared(path: Path) -> None:
    resolved = str(path.resolve()).replace("\\", "/").lower()
    if resolved == str(Path(SHARED_PREVIEW_DB).resolve()).replace("\\", "/").lower():
        raise SystemExit(
            f"REFUSED: {SHARED_PREVIEW_DB} is the shared preview store; this order allows reading "
            "it with mode=ro and never healing it."
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("store", help="path to the SQLite store to catch up")
    parser.add_argument("--apply", action="store_true", help="actually run the catch-up")
    args = parser.parse_args()

    store = Path(args.store)
    if args.apply:
        refuse_shared(store)
    if not store.exists():
        # A store that does not exist yet is the from-zero deploy case; `ensure_schema` creates it.
        print(f"store: {store} (does not exist yet -- a from-zero deploy)")
        present: set[str] = set()
    else:
        present = present_tables(str(store))

    declared = declared_tables()
    missing = sorted(set(declared) - present)
    extra = sorted(present - set(declared))
    print(f"declared by backend/db/schema.sql: {len(declared)}")
    print(f"present in store:                  {len(present)}")
    print(f"missing:                           {len(missing)} {missing}")
    print(f"present but not declared:          {len(extra)} {extra}")

    if not args.apply:
        print("VERDICT: REPORT ONLY (pass --apply to heal)")
        return 0

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from app.db.session import ensure_schema, make_engine  # noqa: PLC0415 - after the path is set

    report = ensure_schema(make_engine(f"sqlite:///{store.as_posix()}"))
    created = [str(name) for name in report["created"]]
    print(f"catch-up created:                  {len(created)} {created}")
    remaining = sorted(set(declared) - present_tables(str(store)))
    print(f"still missing after --apply:       {len(remaining)} {remaining}")
    if remaining:
        print("VERDICT: RED (the boot path did not deliver the declared schema)")
        return 1
    print("VERDICT: COMPLETE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
