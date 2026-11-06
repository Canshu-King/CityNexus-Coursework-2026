"""ORD-0355 - apply the additive `stay_budget_declared` column to one named SQLite store.

Dry-run unless `--apply` is passed.  Nothing here picks a database for you, and nothing here
runs at import time: opening the app already performs the same additive step through
`app.db.session.ensure_schema`, so this script exists for the operator who wants to land the
change deliberately and see the census before and after.

    python backend/db/apply_ord0355_stay_budget_declared.py path/to/store.db           # report
    python backend/db/apply_ord0355_stay_budget_declared.py path/to/store.db --apply    # write

The step is additive: one nullable column with no DEFAULT.  A DEFAULT of 0 would rewrite every
pre-existing row into "the traveller was asked and gave no lodging budget", which is the exact
fabrication ORD-0355 removes; NULL leaves those rows as what they are - never recorded - so the
amounts they already hold keep their old meaning.

The three states this records, and the only two this script can ever write:

    1     a traveller declared a lodging budget (including a declared 0)
    0     a traveller was asked and left it empty
    NULL  a row written before this column existed - unknown, and treated as unknown
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

TABLE = "journey_trip_contexts"
COLUMN = "stay_budget_declared"
# Byte-identical to the guard in `app.db.session.ensure_schema`, so the two routes to a
# migrated store cannot disagree about what the column is.
ADD_COLUMN_SQL = (
    f"ALTER TABLE {TABLE} ADD COLUMN {COLUMN} INTEGER CHECK ({COLUMN} IN (0, 1))"
)
CENSUS_SQL = (
    f"SELECT {COLUMN} IS NULL AS is_unknown, {COLUMN} AS value,"
    f" COUNT(*) AS n_rows FROM {TABLE} GROUP BY is_unknown, value"
)


def _columns(connection: sqlite3.Connection) -> list[str]:
    return [
        str(row[1]) for row in connection.execute(f"PRAGMA table_info({TABLE})").fetchall()
    ]


def _census(connection: sqlite3.Connection) -> dict[str, int]:
    counts = {"declared": 0, "not_declared": 0, "unknown": 0}
    for row in connection.execute(CENSUS_SQL).fetchall():
        is_unknown, value, n_rows = bool(row[0]), row[1], int(row[2])
        if is_unknown:
            counts["unknown"] += n_rows
        elif value == 1:
            counts["declared"] += n_rows
        else:
            counts["not_declared"] += n_rows
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("database", help="path to the SQLite store to migrate (must already exist)")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="actually write the column; without this the script only reports",
    )
    args = parser.parse_args(argv)

    path = Path(args.database).expanduser().resolve()
    if not path.exists():
        print(f"REFUSED: no such database file: {path}", file=sys.stderr)
        return 2
    connection = sqlite3.connect(path)
    tables = {
        str(row[0])
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    if TABLE not in tables:
        print(
            f"REFUSED: {path} has no {TABLE} table; this is not a CityNexus store",
            file=sys.stderr,
        )
        connection.close()
        return 2

    already = COLUMN in _columns(connection)
    print(f"store: {path}")
    print(f"{TABLE}.{COLUMN}: {'present' if already else 'absent'}")
    if already:
        print(f"census: {_census(connection)}")
        print("no-op: the additive step has already been applied")
        connection.close()
        return 0

    if not args.apply:
        print(f"would run: {ADD_COLUMN_SQL}")
        print("dry run: pass --apply to write")
        connection.close()
        return 0

    connection.execute(ADD_COLUMN_SQL)
    connection.commit()
    if COLUMN not in _columns(connection):
        print("FAILED: the column is still not present after the ALTER", file=sys.stderr)
        connection.close()
        return 1
    print(f"applied: {TABLE}.{COLUMN}")
    print(f"census: {_census(connection)}")
    print(
        "every pre-existing row reads unknown; this script wrote no value into any of them"
    )
    connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
