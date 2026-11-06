"""ORD-0488: the schema a new generation declares must actually arrive in the store.

The store the shared preview runs on was measured missing four tables, and the reading that
matters is not "does the boot path try" but "does a store that predates a table end up with the
same table the from-zero path builds".  So these cells compare structure, not existence: column
declarations and index sets, taken off the healed store and off a from-zero store built by the
same code.

Everything here runs against databases created under `tmp_path`.  Nothing here opens a socket,
calls `POST /api/v1/demo/reset`, or touches `D:/moodle/_qoder_preview_20260920/_preview.db`.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from app.db.models import Base
from app.db.session import SCHEMA_CATCHUP, ensure_schema, make_engine

SCHEMA_SQL = Path(__file__).resolve().parents[1] / "db" / "schema.sql"

# The four cells the shared preview store was measured short of (ORD-0488).  Named per table so a
# heal that fixes three of four cannot pass.
PREVIEW_MISSING = (
    "journey_checkins",
    "journey_members",
    "journey_role_capabilities",
    "journey_share_grants",
)


def declared_tables() -> set[str]:
    return set(
        re.findall(r'CREATE TABLE IF NOT EXISTS\s+"?([a-z_0-9]+)"?',
                   SCHEMA_SQL.read_text(encoding="utf-8"), re.I)
    )


def _engine(tmp_path: Path, name: str):
    return make_engine(f"sqlite:///{(tmp_path / name).as_posix()}")


def _names(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        if not str(row[0]).startswith("sqlite_")
    }


def _shape(db: Path, table: str) -> tuple[tuple, ...]:
    """Column declarations and index names for one table -- what a heal must get right."""
    conn = sqlite3.connect(db)
    try:
        columns = tuple(
            (str(row[1]), str(row[2]).upper(), int(row[3]), row[4], int(row[5]))
            for row in conn.execute(f"PRAGMA table_info({table})")
        )
        indexes = tuple(
            sorted(
                str(row[0])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=?", (table,)
                )
            )
        )
        return columns + (("INDEXES", indexes),)
    finally:
        conn.close()


def test_ord0488_from_zero_boot_delivers_every_declared_table(tmp_path: Path) -> None:
    """A new environment starting at zero gets the whole schema -- the order's first question."""
    engine = _engine(tmp_path, "from_zero.db")
    ensure_schema(engine)
    engine.dispose()
    conn = sqlite3.connect(tmp_path / "from_zero.db")
    try:
        present = _names(conn)
    finally:
        conn.close()
    absent = sorted(declared_tables() - present)
    assert not absent, (
        f"expected the boot path to create all {len(declared_tables())} declared tables, missing: "
        f"{absent}"
    )


def test_ord0488_every_model_table_is_declared_in_the_ddl() -> None:
    """`ensure_schema` applies only `db/schema.sql`, so an undeclared model is a table no boot
    can ever create -- which is how a new environment ends up short without anyone renaming a
    door."""
    undeclared = sorted(set(Base.metadata.tables) - declared_tables())
    assert not undeclared, (
        f"expected every ORM model ({len(Base.metadata.tables)} mapped) to have its table in "
        f"backend/db/schema.sql; these are declared nowhere: {undeclared}"
    )


def test_ord0488_dropping_a_preview_missing_table_then_booting_heals_it(
    tmp_path: Path,
) -> None:
    """The order's counter-example, one cell per table: delete it, boot again, it must be back
    and be structurally the table a from-zero boot builds.  If the heal is removed this is red."""
    from_zero_db = tmp_path / "from_zero.db"
    from_zero = _engine(tmp_path, "from_zero.db")
    ensure_schema(from_zero)
    from_zero.dispose()

    stale = _engine(tmp_path, "stale.db")
    ensure_schema(stale)
    stale.dispose()

    for table in PREVIEW_MISSING:
        target = tmp_path / "stale.db"
        conn = sqlite3.connect(target)
        conn.execute(f"DROP TABLE IF EXISTS {table}")
        conn.commit()
        conn.close()
        gone = _engine(tmp_path, "stale.db")
        report = ensure_schema(gone)
        gone.dispose()

        conn = sqlite3.connect(target)
        try:
            back = table in _names(conn)
        finally:
            conn.close()
        assert back, f"expected {table} to be recreated by the boot path after it was dropped"
        assert table in list(report["created"]), (
            f"expected the catch-up report to name {table} among what it created; it reported "
            f"{list(report['created'])}"
        )
        assert _shape(target, table) == _shape(from_zero_db, table), (
            f"{table} came back, but not as the from-zero schema builds it -- a healed store "
            f"whose columns or indexes differ from a fresh one is the drift this order is about"
        )


def test_ord0488_the_catch_up_report_is_named_and_complete(tmp_path: Path) -> None:
    """A deploy must be able to state what it added.  An invisible heal fails this cell even
    though the database itself ends up correct."""
    report = ensure_schema(_engine(tmp_path, "report.db"))
    assert set(report) == {"database", "before", "after", "created"}, (
        f"expected the four declared fields of SCHEMA_CATCHUP, got {sorted(report)}"
    )
    assert report["before"] == 0 and report["after"] == len(declared_tables()), (
        f"expected a from-zero boot to read 0 tables before and {len(declared_tables())} after, "
        f"got before={report['before']} after={report['after']}"
    )
    assert report["created"] == sorted(declared_tables()), (
        "the report must name every table it created, not just count them"
    )
    assert SCHEMA_CATCHUP == report, (
        "expected the module-level SCHEMA_CATCHUP to carry the same report the call returned"
    )
