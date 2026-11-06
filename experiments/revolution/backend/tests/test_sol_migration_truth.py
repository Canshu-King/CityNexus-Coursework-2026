"""The upgrade path for a database that already contains two ACTIVE plans.

`ux_plan_versions_single_active` protects a healthy database.  The dangerous moment
is the migration: an existing deployment may already hold duplicate ACTIVE rows, and
the cleanup that makes room for the index has to decide which one survives.  Deciding
by timestamp is a guess about which plan the traveller is executing, and it can
retire the very plan `journeys.active_plan_version_id` names - turning "two answers"
into "one answer nothing points at".

These cases build a historical database (schema as it was *before* the index existed),
plant the corruption, and then run the real `ensure_schema`.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from app.db.session import SCHEMA_PATH, ensure_schema

INDEX_STATEMENT = (
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_plan_versions_single_active\n"
    "    ON plan_versions(journey_id) WHERE status = 'ACTIVE';"
)

HISTORICAL_SCHEMA = SCHEMA_PATH.read_text(encoding="utf-8").replace(
    INDEX_STATEMENT, "-- a database from before the invariant existed"
)


def _historical_engine(tmp_path: Path) -> Engine:
    path = tmp_path / "historical.db"
    connection = sqlite3.connect(path)
    connection.executescript(HISTORICAL_SCHEMA)
    connection.executescript(
        """
        INSERT INTO travelers (id, display_name, locale, timezone, created_at, updated_at)
        VALUES ('t1','probe','en-GB','Asia/Shanghai','2026-09-20T00:00:00+00:00','2026-09-20T00:00:00+00:00');
        """
    )
    connection.close()
    return create_engine(f"sqlite:///{path}")


def _plant_two_active_plans(engine: Engine, pointer: str) -> None:
    """Journey points at `pointer`; both p-old and p-new are ACTIVE.

    `p-new` is deliberately the later row, so a timestamp rule and a pointer rule
    disagree and the test can tell which one ran.
    """
    with engine.begin() as connection:
        connection.exec_driver_sql(
            """
            INSERT INTO journeys (id, traveler_id, title, city_code, timezone, status,
                                  start_at, end_at, budget_amount_minor, budget_currency,
                                  planning_mode, active_plan_version_id, created_at, updated_at)
            VALUES ('j1','t1','probe','CN-TIANJIN','Asia/Shanghai','ACTIVE',
                    '2026-10-09T18:00:00+08:00','2026-10-11T15:00:00+08:00',
                    120000,'CNY','balanced', ?,
                    '2026-09-20T00:00:00+00:00','2026-09-20T00:00:00+00:00')
            """,
            (pointer,),
        )
        for plan_id, version_no, created_at in (
            ("p-old", 1, "2026-09-20T00:00:00+00:00"),
            ("p-new", 2, "2026-09-20T09:00:00+00:00"),
        ):
            connection.exec_driver_sql(
                """
                INSERT INTO plan_versions (id, journey_id, version_no, planning_mode, status,
                                           source, total_cost_minor, currency,
                                           estimated_duration_min, created_at)
                VALUES (?, 'j1', ?, 'balanced', 'ACTIVE', 'INITIAL', 0, 'CNY', 60, ?)
                """,
                (plan_id, version_no, created_at),
            )


def _active_plan_ids(engine: Engine) -> list[str]:
    with engine.connect() as connection:
        return [
            row[0]
            for row in connection.exec_driver_sql(
                "SELECT id FROM plan_versions WHERE journey_id='j1' AND status='ACTIVE' ORDER BY id"
            )
        ]


def test_migration_keeps_the_plan_the_journey_points_at_not_the_newest_row(
    tmp_path: Path,
) -> None:
    engine = _historical_engine(tmp_path)
    _plant_two_active_plans(engine, pointer="p-old")

    ensure_schema(engine)

    assert _active_plan_ids(engine) == ["p-old"], (
        "the migration superseded the plan the Journey actually names, leaving the "
        "pointer aimed at a SUPERSEDED row"
    )
    with engine.connect() as connection:
        pointer = connection.exec_driver_sql(
            "SELECT active_plan_version_id FROM journeys WHERE id='j1'"
        ).fetchone()[0]
        superseded = [
            row[0]
            for row in connection.exec_driver_sql(
                "SELECT id FROM plan_versions WHERE journey_id='j1' AND status='SUPERSEDED'"
            )
        ]
    assert pointer == "p-old"
    assert superseded == ["p-new"]
    # And the invariant is now actually enforceable, not just tidy.
    with engine.begin() as connection:
        with pytest.raises(IntegrityError):
            connection.exec_driver_sql(
                """
                INSERT INTO plan_versions (id, journey_id, version_no, planning_mode, status,
                                           source, total_cost_minor, currency,
                                           estimated_duration_min, created_at)
                VALUES ('p-third','j1',3,'balanced','ACTIVE','REPLAN',0,'CNY',60,
                        '2026-09-20T10:00:00+00:00')
                """
            )


def test_migration_refuses_to_guess_when_the_pointer_matches_neither(
    tmp_path: Path,
) -> None:
    engine = _historical_engine(tmp_path)
    # The pointer names a plan that is not among the ACTIVE duplicates - a
    # corrupted row no timestamp rule can resolve honestly.
    _plant_two_active_plans(engine, pointer="does-not-exist")

    with pytest.raises(RuntimeError, match="refusing to migrate duplicate ACTIVE"):
        ensure_schema(engine)

    assert _active_plan_ids(engine) == ["p-new", "p-old"], (
        "a refused migration must not pick a winner on the way out"
    )
