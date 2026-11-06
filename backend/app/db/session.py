from __future__ import annotations

import json
import logging

from pathlib import Path
from typing import Generator

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import settings
from app.db.write_deadline import busy_timeout_ms as write_busy_timeout_ms
from app.db.write_deadline import busy_timeout_seconds as write_busy_timeout_seconds

ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = ROOT / "db" / "schema.sql"

# ORD-0488: what the most recent `ensure_schema()` call had to create.  Deploying a new generation
# over an old database heals itself in here, and this is the only place that says out loud WHICH
# tables it added -- a boot that silently grew four tables is otherwise indistinguishable from one
# that grew none, and the operator has no way to tell a catch-up that worked from one that was never
# reached.
SCHEMA_CATCHUP: dict[str, object] = {"database": None, "before": 0, "after": 0, "created": []}
schema_log = logging.getLogger("citynexus.schema")


def _refuse_to_reopen_an_existing_e2e_database(url: str) -> None:
    """Fresh database or no run.

    Only an authoritative E2E invocation stamps a process with
    CITYNEXUS_E2E_RUN_ID, so this check cannot disturb normal or test use.  It
    lives here, in the process that is about to open the file, because that is
    the only place where "did this run create it?" is answerable exactly once:
    the frontend's config is re-read by the worker process after the server has
    already created the database, and its globalSetup runs even later.
    """

    if not settings.e2e_run_id or not url.startswith("sqlite:///"):
        return
    path = url[len("sqlite:///") :]
    if not path or path.startswith(":memory:"):
        return
    target = Path(path)
    if not target.is_absolute():
        target = Path.cwd() / target
    if target.exists():
        raise RuntimeError(
            f"E2E run {settings.e2e_run_id!r} refuses to reopen an existing database at "
            f"{target}. An authoritative run must start from an empty file; issue a new "
            "CITYNEXUS_E2E_RUN_ID instead of deleting it here."
        )


def make_engine(database_url: str | None = None) -> Engine:
    url = database_url or settings.database_url
    _refuse_to_reopen_an_existing_e2e_database(url)
    kwargs: dict[str, object] = {"future": True}
    if url.startswith("sqlite"):
        # The local product path intentionally exercises browser requests and
        # the Guardian worker against one SQLite file.  Let the writer wait
        # for the short transaction already in progress instead of surfacing
        # a spurious `database is locked` failure to the Agent run.
        kwargs["connect_args"] = {"check_same_thread": False,
                                  "timeout": write_busy_timeout_seconds()}
        if url in {"sqlite://", "sqlite:///:memory:"}:
            kwargs["poolclass"] = StaticPool
    created = create_engine(url, **kwargs)
    if created.dialect.name == "sqlite":

        def _enable_sqlite_foreign_keys(dbapi_connection, _connection_record) -> None:
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute(f"PRAGMA busy_timeout={write_busy_timeout_ms()}")
                if created.url.database not in {None, ":memory:"}:
                    cursor.execute("PRAGMA journal_mode=WAL")
            finally:
                cursor.close()

        event.listen(created, "connect", _enable_sqlite_foreign_keys)
    return created


engine = make_engine()
SessionLocal = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)




QUARANTINE_STATUS = "QUARANTINED_DUPLICATE_IDENTITY"

# The name the pre-ORD-0167 `catalog_hydration_operations` table is parked under while the schema
# script builds the authoritative new one.  Renaming rather than dropping-and-recreating is what
# keeps this a migration and not a deletion: the old rows stay on disk, untouched, until every one
# of them has been written into the new shape and counted.
PENDING_IDENTITY_PRESERVED = "catalog_hydration_operations__pre_identity_fix"

# What the last boot's identity migration did, so a caller can ask instead of guessing.  Absent
# (`None`) means this process never migrated anything, which is the normal case.
LAST_PENDING_IDENTITY_MIGRATION: dict[str, int] | None = None


def _pending_identity_needs_fix(connection) -> bool:
    """True when `catalog_hydration_operations` still has the pre-ORD-0167 shape.

    Three things moved together and none of them can be altered in place in SQLite: the identity
    column lost its NOT NULL, five columns appeared, and the `outcome` CHECK gained two terminal
    names.  Any one of those is enough to rebuild - and the rebuild is deliberately left to the
    schema script, so the new table comes from `db/schema.sql` instead of a second copy of the
    DDL typed in here where it can drift.
    """

    ddl = connection.exec_driver_sql(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='catalog_hydration_operations'"
    ).scalar()
    if not ddl:
        return False
    columns = {
        row[1]: row
        for row in connection.exec_driver_sql("PRAGMA table_info(catalog_hydration_operations)")
    }
    if "pending_destination_label" not in columns or "legacy_fingerprint" not in columns:
        return True
    canonical = columns.get("canonical_destination_id")
    if canonical is not None and int(canonical[3] or 0) == 1:
        return True  # NOT NULL on an identity column is what forced `pending:<text>` in the first place
    return "DESTINATION_AMBIGUOUS" not in ddl


def _preserve_legacy_hydration_table(connection) -> None:
    """Step the old table aside before the schema script runs, so `IF NOT EXISTS` means it."""

    if not _pending_identity_needs_fix(connection):
        return
    connection.exec_driver_sql(
        f"ALTER TABLE catalog_hydration_operations RENAME TO {PENDING_IDENTITY_PRESERVED}"
    )


def _backfill_legacy_hydration_rows(connection) -> None:
    """Move every legacy row into the new shape, in place, losing none (arbiter ④ / R10).

    The semantic decision - what the identity, label, fidelity and fingerprint become - belongs to
    the module that owns that vocabulary, so it is asked, not reimplemented here.  What this
    function owns is the physical part: no row leaves the old table without arriving in the new
    one, the operation keeps its id and its history, the row count is compared before anything is
    dropped, and the whole exchange is in the schema transaction so a failure rolls it back.
    """

    global LAST_PENDING_IDENTITY_MIGRATION

    if not _table_exists(connection, PENDING_IDENTITY_PRESERVED):
        return
    from app.anywhere_capability import backfill_pending_identity_row  # vocabulary owner

    legacy = connection.exec_driver_sql(f"SELECT * FROM {PENDING_IDENTITY_PRESERVED}")
    legacy_keys = list(legacy.keys())
    old_rows = [dict(zip(legacy_keys, row)) for row in legacy]
    new_columns = [
        row[1] for row in connection.exec_driver_sql("PRAGMA table_info(catalog_hydration_operations)")
    ]
    counts = {
        "rows_before": len(old_rows),
        "rows_after": 0,
        "identities_nulled": 0,
        "identities_untouched": 0,
        "labels_exact_raw": 0,
        "labels_legacy_normalized": 0,
    }
    placeholders = ", ".join("?" * len(new_columns))
    columns_sql = ", ".join(new_columns)
    # Upsert, not a bare INSERT: `executescript` commits, so a boot that died between parking the
    # old table and dropping it leaves the rows behind.  The next boot must *finish* that job, and
    # a primary-key collision on an id it already migrated is that case, not a corruption.
    updates = ", ".join(
        f"{name} = excluded.{name}" for name in new_columns if name != "id"
    )
    statement = (
        f"INSERT INTO catalog_hydration_operations ({columns_sql}) VALUES ({placeholders})"
        f" ON CONFLICT(id) DO UPDATE SET {updates}"
    )
    for old in old_rows:
        fields = backfill_pending_identity_row(
            old.get("canonical_destination_id"),
            old.get("request_json"),
            old.get("fingerprint"),
            old.get("id"),
        )
        if fields["canonical_destination_id"] is None:
            counts["identities_nulled"] += 1
        else:
            counts["identities_untouched"] += 1
        if fields["pending_label_fidelity"] == "EXACT_RAW":
            counts["labels_exact_raw"] += 1
        elif fields["pending_label_fidelity"] == "LEGACY_NORMALIZED":
            counts["labels_legacy_normalized"] += 1
        values = dict(old)
        values.update({name: value for name, value in fields.items() if name in new_columns})
        connection.exec_driver_sql(
            statement, tuple(values.get(name) for name in new_columns)
        )
    counts["rows_after"] = int(
        connection.exec_driver_sql("SELECT COUNT(*) FROM catalog_hydration_operations").scalar()
        or 0
    )
    if counts["rows_after"] < counts["rows_before"]:
        raise RuntimeError(
            "catalog_hydration_operations identity migration refused to continue: "
            f"{counts['rows_before']} rows went in, {counts['rows_after']} came out. The schema "
            "transaction is rolling back; no legacy job has been dropped."
        )
    connection.exec_driver_sql(f"DROP TABLE {PENDING_IDENTITY_PRESERVED}")

    # One named line about what changed, on a database that has somewhere to put it.  A migration
    # that cannot be described afterwards is indistinguishable from data loss.
    if _table_exists(connection, "audit_logs") and counts["identities_nulled"]:
        from datetime import datetime, timezone

        stamp = datetime.now(timezone.utc).isoformat()
        connection.exec_driver_sql(
            "INSERT INTO audit_logs (id, actor_type, actor_id, journey_id, action, entity_type,"
            " entity_id, before_json, after_json, trace_id, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                f"pending-identity-migration-{stamp}",
                "SYSTEM",
                "db/session.py:_backfill_legacy_hydration_rows",
                None,
                "CATALOG_HYDRATION_IDENTITY_MIGRATED",
                "catalog_hydration_operations",
                None,
                json.dumps({"pending_identity_rows": counts["identities_nulled"]}),
                json.dumps(counts),
                f"pending-identity-migration@{stamp}",
                stamp,
            ),
        )
    LAST_PENDING_IDENTITY_MIGRATION = counts





# ORD-0434 step 2: the ACL table's one home.  A role is a set of capabilities plus the message key
# that states its scope in one sentence (RELATION/relation_and_copy_findings.md); the sentence
# itself is not stored here, so no server-authored prose can ever reach a screen from this table.
JOURNEY_ROLE_CAPABILITIES = (
    ("OWNER", "JOURNEY_READ", "member.scope.owner"),
    ("OWNER", "JOURNEY_UPDATE", "member.scope.owner"),
    ("OWNER", "PLACE_EDIT", "member.scope.owner"),
    ("OWNER", "MEMBER_MANAGE", "member.scope.owner"),
    ("OWNER", "SHARE_GRANT_MANAGE", "member.scope.owner"),
    ("ADMIN", "JOURNEY_READ", "member.scope.admin"),
    ("ADMIN", "PLACE_EDIT", "member.scope.admin"),
    ("ADMIN", "MEMBER_MANAGE", "member.scope.admin"),
    ("COLLABORATOR", "JOURNEY_READ", "member.scope.collaborator"),
    ("COLLABORATOR", "PLACE_EDIT", "member.scope.collaborator"),
)


def _seed_journey_acl(connection) -> int:
    """Insert the role/capability rows when the table is still empty.  Returns rows added."""
    if connection.exec_driver_sql("SELECT COUNT(*) FROM journey_role_capabilities").scalar():
        return 0
    for role, capability, message_key in JOURNEY_ROLE_CAPABILITIES:
        connection.exec_driver_sql(
            "INSERT INTO journey_role_capabilities"
            " (role, capability, scope_message_key, created_at) VALUES (?, ?, ?, datetime('now'))",
            (role, capability, message_key),
        )
    return len(JOURNEY_ROLE_CAPABILITIES)


def _table_exists(connection, name: str) -> bool:
    row = connection.exec_driver_sql(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _table_names(connection) -> set[str]:
    """Every table really in this database, `sqlite_*` bookkeeping excluded."""
    return {
        row[0]
        for row in connection.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'")
        if not str(row[0]).startswith("sqlite_")
    }


def _quarantine_existing_duplicates(connection) -> None:
    """Best-effort duplicate cleanup that runs *before* the schema script.

    On a fresh database the table does not exist yet, so this returns
    immediately.  On a legacy database it moves duplicate losers out of the
    authoritative identity so the unique index can be created afterwards.
    """

    if not _table_exists(connection, "transport_reservations"):
        return
    columns = {
        row[1]
        for row in connection.exec_driver_sql("PRAGMA table_info(transport_reservations)")
    }
    if "confirmation_ref" not in columns or "status" not in columns:
        return
    _quarantine_duplicates(connection)


def _collapse_duplicate_active_plans(connection) -> None:
    """Resolve historical duplicate ACTIVE plans using the Journey's own pointer.

    The invariant itself is enforced by `ux_plan_versions_single_active`; this only
    clears violations an existing database may already contain, so the index can be
    created at all.

    The survivor is *not* chosen by timestamp.  `journeys.active_plan_version_id` is
    the product's authoritative statement of which plan a Journey is executing, and
    picking "the newest row" can retire exactly that plan - trading two answers for
    one answer nothing points at.  Where the pointer cannot settle the question, this
    refuses to migrate rather than guess.
    """

    if not _table_exists(connection, "plan_versions"):
        return
    if not _table_exists(connection, "journeys"):
        return
    duplicates = connection.exec_driver_sql(
        """
        SELECT journey_id, group_concat(id) AS ids, COUNT(*) AS total
        FROM plan_versions
        WHERE status = 'ACTIVE'
        GROUP BY journey_id
        HAVING COUNT(*) > 1
        """
    ).fetchall()
    for journey_id, ids, total in duplicates:
        active_ids = [item for item in str(ids or "").split(",") if item]
        pointer_row = connection.exec_driver_sql(
            "SELECT active_plan_version_id FROM journeys WHERE id = ?", (journey_id,)
        ).fetchone()
        pointer_id = pointer_row[0] if pointer_row is not None else None
        if pointer_id not in active_ids:
            raise RuntimeError(
                "refusing to migrate duplicate ACTIVE PlanVersions: Journey "
                f"{journey_id!r} has {total} ACTIVE plans {active_ids} but its "
                f"active_plan_version_id is {pointer_id!r}, which names none of them. "
                "Choosing a winner by created_at/version_no would silently decide "
                "which plan the traveller is executing; this is a data-integrity "
                "conflict and must be repaired explicitly."
            )
        connection.exec_driver_sql(
            "UPDATE plan_versions SET status = 'SUPERSEDED' "
            "WHERE journey_id = ? AND status = 'ACTIVE' AND id <> ?",
            (journey_id, pointer_id),
        )


def _anchor_identity_groups(connection) -> list[tuple[str, str, list[str]]]:
    """``(journey_id, identity, ids survivor-first)`` for each duplicated ACTIVE C4 anchor.

    The identity expression is the same node split ``anchor_claim_identity`` and
    ``resolve_plan_anchors`` make: ARRIVAL and DEPARTURE collapse onto the Journey's structural
    node (so the role alone identifies them), MID_TRIP carries its stored place/service key.  A
    MID_TRIP row with no key anchors nothing - it is reported unresolved - and the unique indexes
    count its NULL key as distinct, so it is left out here too rather than being retired by a
    rule the schema deliberately does not enforce.

    The ``ORDER BY`` is the survivor rule, applied in Python instead of through ``group_concat``
    because the concatenation order of that aggregate is not defined: a rule that only holds when
    the scan happens to come out in the right order is not a rule.  Bound to the Journey's
    executing plan first, then the most recent confirmation, then the lowest rowid so the same
    database repairs the same way twice.
    """

    rows = connection.exec_driver_sql(
        """
        SELECT journey_id, identity, id
          FROM (
            SELECT c.id AS id,
                   c.journey_id AS journey_id,
                   c.created_at AS created_at,
                   c.rowid AS rowid,
                   CASE upper(json_extract(c.policy_json, '$.anchor_role'))
                     WHEN 'ARRIVAL'   THEN '#arrival'
                     WHEN 'DEPARTURE' THEN '#departure'
                     ELSE json_extract(c.policy_json, '$.semantic_key')
                   END AS identity,
                   (SELECT COUNT(*) FROM activities a
                     JOIN plan_versions p ON p.id = a.plan_version_id
                    WHERE a.id = c.activity_id
                      AND p.journey_id = c.journey_id
                      AND p.status IN ('ACTIVE', 'SELECTED')) AS bound
              FROM commitments c
             WHERE c.status = 'ACTIVE'
               AND c.level = 'C4_Anchored'
               AND json_valid(c.policy_json)
               AND upper(json_extract(c.policy_json, '$.anchor_role'))
                   IN ('ARRIVAL', 'DEPARTURE', 'MID_TRIP')
          )
         WHERE identity IS NOT NULL
         ORDER BY journey_id, identity, bound DESC, created_at DESC, rowid ASC
        """
    ).fetchall()
    grouped: dict[tuple[str, str], list[str]] = {}
    for journey_id, identity, row_id in rows:
        grouped.setdefault((str(journey_id), str(identity)), []).append(str(row_id))
    return [
        (journey_id, identity, ids)
        for (journey_id, identity), ids in grouped.items()
        if len(ids) > 1
    ]


def _retire_duplicate_active_anchors(connection) -> int:
    """Clear duplicate ACTIVE anchors so ORD-0360's unique indexes can be created.

    Runs before the schema script, the way ``_collapse_duplicate_active_plans`` does for plans.
    Unlike that repair this one never refuses to migrate: the survivor rule is total (bound to
    the executing plan, then newest, then lowest rowid), so a legacy database boots instead of
    deadlocking on an unfixable row - and unlike a duplicate ACTIVE PlanVersion, retiring an
    anchor does not decide which plan the traveller executes, it only decides which of two
    tickets for one node the planner constrains the itinerary with.  Nothing is deleted: the
    loser keeps its row, its ``external_ref`` and its Booking, and each group gets one durable
    Event naming who was retired and who stayed, as ``_quarantine_duplicates`` does.
    """

    if not _table_exists(connection, "commitments"):
        return 0
    columns = {
        row[1] for row in connection.exec_driver_sql("PRAGMA table_info(commitments)")
    }
    if not {"policy_json", "status", "level", "journey_id"} <= columns:
        return 0
    repaired = 0
    for journey_id, identity, ids in _anchor_identity_groups(connection):
        keep, *losers = ids
        for row_id in losers:
            connection.exec_driver_sql(
                "UPDATE commitments SET status = 'CANCELLED', updated_at = datetime('now'),"
                " policy_json = json_set("
                "   policy_json,"
                "   '$.retire_reason', 'SUPERSEDED_BY_NEWER_CONFIRMATION',"
                "   '$.superseded_by_commitment_id', ?,"
                "   '$.retired_at', datetime('now')"
                " ) WHERE id = ?",
                (keep, row_id),
            )
            repaired += 1
        connection.exec_driver_sql(
            """
            INSERT INTO events (
                id, journey_id, provider_key, provenance, event_type, severity,
                status, affected_activity_id, occurred_at, detected_at,
                resolved_at, payload_json, created_at
            ) VALUES (
                lower(hex(randomblob(16))), ?, 'citynexus-reservation',
                'SYSTEM_DERIVED', 'USER_CHANGE', 'MAJOR', 'RESOLVED', NULL,
                datetime('now'), datetime('now'), datetime('now'), ?, datetime('now')
            )
            """,
            (
                journey_id,
                json.dumps(
                    {
                        "kind": "C4_ANCHOR_SUPERSEDED_BY_NEWER_CONFIRMATION",
                        "identity": identity,
                        "kept_commitment_id": keep,
                        "retired_commitment_ids": losers,
                        "found_active_rows": len(ids),
                        "policy": "SUPERSEDED_BY_NEWER_CONFIRMATION",
                        "repair": "ensure_schema",
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            ),
        )
        repaired += len(losers)
    return repaired


def _migrate_reservation_identity(connection) -> None:
    """Make the reservation uniqueness rule actually enforceable.

    Runs *after* the schema script.  Duplicates are already quarantined by
    ``_quarantine_existing_duplicates``; this function creates the real partial
    unique index and raises on any genuine failure instead of swallowing it.
    """

    _quarantine_duplicates(connection)
    connection.exec_driver_sql(
        "DROP INDEX IF EXISTS uq_transport_reservations_journey_confirmation"
    )
    connection.exec_driver_sql(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_transport_reservations_journey_confirmation "
        "ON transport_reservations(journey_id, confirmation_ref) "
        "WHERE confirmation_ref IS NOT NULL AND status <> '"
        + QUARANTINE_STATUS
        + "'"
    )


def _quarantine_duplicates(connection) -> None:
    """Move duplicate losers out of the authoritative reservation identity."""
    """Make the reservation uniqueness rule actually enforceable.

    A plain unique index over ``(journey_id, confirmation_ref)`` cannot be
    created while historical duplicates exist.  Instead of silently swallowing
    the failure, duplicates are *migrated*:

    * the earliest reservation for a reference stays authoritative;
    * later duplicates are marked ``QUARANTINED_DUPLICATE_IDENTITY`` and have
      their draft pointer released, so they no longer claim the identity but
      their evidence and audit trail are preserved;
    * a partial unique index then genuinely constrains every remaining claim.

    A failure here is a real schema error and is raised, never swallowed.
    """

    duplicates = list(
        connection.exec_driver_sql(
            """
            SELECT journey_id, confirmation_ref, COUNT(*) AS n
            FROM transport_reservations
            WHERE confirmation_ref IS NOT NULL
              AND status <> ?
            GROUP BY journey_id, confirmation_ref
            HAVING COUNT(*) > 1
            """,
            (QUARANTINE_STATUS,),
        )
    )
    for journey_id, confirmation_ref, _count in duplicates:
        rows = list(
            connection.exec_driver_sql(
                """
                SELECT id FROM transport_reservations
                WHERE journey_id = ? AND confirmation_ref = ? AND status <> ?
                ORDER BY captured_at ASC, id ASC
                """,
                (journey_id, confirmation_ref, QUARANTINE_STATUS),
            )
        )
        loser_ids = [row[0] for row in rows[1:]]
        if not loser_ids:
            continue
        placeholders = ",".join("?" for _ in loser_ids)
        connection.exec_driver_sql(
            f"""
            UPDATE transport_reservations
               SET status = ?, draft_id = NULL
             WHERE id IN ({placeholders})
            """,
            (QUARANTINE_STATUS, *loser_ids),
        )
        connection.exec_driver_sql(
            """
            INSERT INTO events (
                id, journey_id, provider_key, provenance, event_type, severity,
                status, affected_activity_id, occurred_at, detected_at,
                resolved_at, payload_json, created_at
            ) VALUES (
                lower(hex(randomblob(16))), ?, 'citynexus-reservation',
                'SYSTEM_DERIVED', 'USER_CHANGE', 'MAJOR', 'RESOLVED', NULL,
                datetime('now'), datetime('now'), datetime('now'), ?, datetime('now')
            )
            """,
            (
                journey_id,
                json.dumps(
                    {
                        "kind": "RESERVATION_IDENTITY_DUPLICATE",
                        "confirmation_ref": confirmation_ref,
                        "winner_id": rows[0][0],
                        "quarantined_ids": loser_ids,
                        "policy": QUARANTINE_STATUS,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            ),
        )



def ensure_schema(target_engine: Engine = engine) -> dict[str, object]:
    """Apply the package SQL schema to an empty or existing SQLite database.

    Returns the ORD-0488 catch-up report -- the table names this call had to add -- and records the
    same in `SCHEMA_CATCHUP`.  Deploying a new generation over an old database is meant to heal
    itself here; the report is what makes that a stated fact in the deploy log rather than an
    inference from the absence of a crash.
    """
    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    with target_engine.begin() as connection:
        before = _table_names(connection)
        raw = getattr(connection.connection, "driver_connection", connection.connection)
        # Quarantine historical duplicates *before* the schema script runs, so
        # the unique index in the script can actually be created on a database
        # that already contains them.  A fresh database has no such table yet,
        # so this is a no-op there.
        if target_engine.dialect.name == "sqlite":
            _quarantine_existing_duplicates(connection)
            _collapse_duplicate_active_plans(connection)
            _retire_duplicate_active_anchors(connection)
            # ORD-0167: parked before the script so the new table is the one in `db/schema.sql`.
            _preserve_legacy_hydration_table(connection)
        if hasattr(raw, "executescript"):
            raw.executescript(sql)
        else:  # pragma: no cover - retained for non-SQLite development engines
            for statement in sql.split(";"):
                if statement.strip():
                    connection.exec_driver_sql(statement)
        if target_engine.dialect.name == "sqlite":
            _reconcile_real_place_candidate_scope(connection)
            _backfill_legacy_hydration_rows(connection)
            journey_columns = {
                row[1] for row in connection.exec_driver_sql("PRAGMA table_info(journeys)")
            }
            if "planning_mode" not in journey_columns:
                connection.exec_driver_sql(
                    "ALTER TABLE journeys ADD COLUMN planning_mode TEXT NOT NULL DEFAULT 'balanced'"
                )
            # ORD-0383 slice 1: additive, and no DEFAULT.  A row written before this column
            # existed was never deleted, and NULL is the only value that says so without
            # inventing a deletion moment for it.
            if "deleted_at" not in journey_columns:
                connection.exec_driver_sql("ALTER TABLE journeys ADD COLUMN deleted_at TEXT")
            # ORD-0520 中止: same additive rule as `deleted_at` above.  A trip that was never
            # aborted has no moment and gave no reason, so both stay NULL instead of taking a
            # DEFAULT that would put words in a traveller's mouth.
            if "aborted_at" not in journey_columns:
                connection.exec_driver_sql("ALTER TABLE journeys ADD COLUMN aborted_at TEXT")
            if "abort_reason" not in journey_columns:
                connection.exec_driver_sql("ALTER TABLE journeys ADD COLUMN abort_reason TEXT")
            _migrate_reservation_identity(connection)
            # ORD-0434: the ACL table ships seeded, so a client can read the product's own answer
            # to what a role may do instead of inventing one per screen.
            _seed_journey_acl(connection)
            event_columns = {
                row[1] for row in connection.exec_driver_sql("PRAGMA table_info(events)")
            }
            if event_columns and "provenance" not in event_columns:
                connection.exec_driver_sql(
                    "ALTER TABLE events ADD COLUMN provenance TEXT NOT NULL DEFAULT 'SIMULATED' "
                    "CHECK (provenance IN ('SIMULATED','PROVIDER_VERIFIED','USER_REPORTED','SYSTEM_DERIVED'))"
                )
            policy_columns = {
                row[1]
                for row in connection.exec_driver_sql("PRAGMA table_info(traveler_agent_policies)")
            }
            if policy_columns and "max_auto_loss_minor" not in policy_columns:
                connection.exec_driver_sql(
                    "ALTER TABLE traveler_agent_policies ADD COLUMN max_auto_loss_minor INTEGER NOT NULL DEFAULT 0"
                )
            if policy_columns and "max_auto_schedule_drift_min" not in policy_columns:
                connection.exec_driver_sql(
                    "ALTER TABLE traveler_agent_policies ADD COLUMN max_auto_schedule_drift_min INTEGER NOT NULL DEFAULT 0"
                )
            if policy_columns and "allowed_activity_types_json" not in policy_columns:
                connection.exec_driver_sql(
                    "ALTER TABLE traveler_agent_policies ADD COLUMN allowed_activity_types_json TEXT NOT NULL DEFAULT '[]'"
                )
            if policy_columns and "allowed_booking_types_json" not in policy_columns:
                connection.exec_driver_sql(
                    "ALTER TABLE traveler_agent_policies ADD COLUMN allowed_booking_types_json TEXT NOT NULL DEFAULT '[]'"
                )
            if policy_columns and "protected_commitment_levels_json" not in policy_columns:
                connection.exec_driver_sql(
                    'ALTER TABLE traveler_agent_policies ADD COLUMN protected_commitment_levels_json TEXT NOT NULL DEFAULT \'["C3_Guaranteed","C4_Anchored"]\''
                )
            if policy_columns and "allow_auto_commitment_management" not in policy_columns:
                connection.exec_driver_sql(
                    "ALTER TABLE traveler_agent_policies ADD COLUMN allow_auto_commitment_management INTEGER NOT NULL DEFAULT 0"
                )
            mission_columns = {
                row[1] for row in connection.exec_driver_sql("PRAGMA table_info(journey_missions)")
            }
            capability_columns = {
                row[1]
                for row in connection.exec_driver_sql("PRAGMA table_info(traveler_capabilities)")
            }
            if not capability_columns:
                connection.exec_driver_sql(
                    """CREATE TABLE IF NOT EXISTS traveler_capabilities (
                    id TEXT PRIMARY KEY,
                    traveler_id TEXT NOT NULL REFERENCES travelers(id) ON DELETE CASCADE,
                    journey_id TEXT REFERENCES journeys(id) ON DELETE CASCADE,
                    code TEXT NOT NULL,
                    capability_class TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    params_json TEXT NOT NULL DEFAULT '{}',
                    scope TEXT NOT NULL DEFAULT 'TRAVELER',
                    source TEXT NOT NULL DEFAULT 'USER_CONFIRMED',
                    provenance TEXT NOT NULL DEFAULT 'USER_STATED',
                    explicit INTEGER NOT NULL DEFAULT 1,
                    confirmed INTEGER NOT NULL DEFAULT 0,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    confirmed_at TEXT,
                    UNIQUE(traveler_id, journey_id, scope, code)
                    )"""
                )
            twin_columns = {
                row[1]
                for row in connection.exec_driver_sql("PRAGMA table_info(traveler_twin_snapshots)")
            }
            if twin_columns and "explicit_capabilities_json" not in twin_columns:
                connection.exec_driver_sql(
                    "ALTER TABLE traveler_twin_snapshots ADD COLUMN explicit_capabilities_json TEXT NOT NULL DEFAULT '[]'"
                )
            mission_v2_columns = {
                "mutable_activity_ids_json": "TEXT NOT NULL DEFAULT '[]'",
                "intent_json": "TEXT NOT NULL DEFAULT '{}'",
                "resolved_intent_json": "TEXT NOT NULL DEFAULT '{}'",
                "allowed_action_classes_json": "TEXT NOT NULL DEFAULT '[]'",
                "unresolved_references_json": "TEXT NOT NULL DEFAULT '[]'",
                "compiler_provider_key": "TEXT NOT NULL DEFAULT 'deterministic'",
                "compiler_model_key": "TEXT NOT NULL DEFAULT 'mission-compiler-v2-fallback'",
                "compiler_confidence": "REAL NOT NULL DEFAULT 1.0",
                "reasoning_summary": "TEXT NOT NULL DEFAULT ''",
                "is_standing": "INTEGER NOT NULL DEFAULT 0",
                "parent_mission_id": "TEXT",
            }
            for column, declaration in mission_v2_columns.items():
                if mission_columns and column not in mission_columns:
                    connection.exec_driver_sql(
                        f"ALTER TABLE journey_missions ADD COLUMN {column} {declaration}"
                    )
            receipt_columns = {
                row[1]
                for row in connection.exec_driver_sql("PRAGMA table_info(idempotency_keys)")
            }
            for column, declaration in (("claim_token", "TEXT"), ("claim_deadline", "TEXT")):
                if receipt_columns and column not in receipt_columns:
                    connection.exec_driver_sql(
                        f"ALTER TABLE idempotency_keys ADD COLUMN {column} {declaration}"
                    )
            impact_columns = {
                row[1]
                for row in connection.exec_driver_sql("PRAGMA table_info(impact_assessments)")
            }
            if impact_columns and "maturity_stage" not in impact_columns:
                connection.exec_driver_sql(
                    "ALTER TABLE impact_assessments ADD COLUMN maturity_stage TEXT NOT NULL DEFAULT 'AI_ONLY'"
                )
            if impact_columns and "selected_corroboration_refs_json" not in impact_columns:
                connection.exec_driver_sql(
                    "ALTER TABLE impact_assessments ADD COLUMN selected_corroboration_refs_json TEXT NOT NULL DEFAULT '[]'"
                )
            if impact_columns and "independent_evidence_count" not in impact_columns:
                connection.exec_driver_sql(
                    "ALTER TABLE impact_assessments ADD COLUMN independent_evidence_count INTEGER NOT NULL DEFAULT 0"
                )
            if impact_columns and "primary_recovery_target_id" not in impact_columns:
                connection.exec_driver_sql(
                    "ALTER TABLE impact_assessments ADD COLUMN primary_recovery_target_id TEXT"
                )
            if impact_columns and "secondary_risk_activity_ids_json" not in impact_columns:
                connection.exec_driver_sql(
                    "ALTER TABLE impact_assessments ADD COLUMN secondary_risk_activity_ids_json TEXT NOT NULL DEFAULT '[]'"
                )
            approval_columns = {
                row[1] for row in connection.exec_driver_sql("PRAGMA table_info(action_approvals)")
            }
            if approval_columns and "required_decision" not in approval_columns:
                connection.exec_driver_sql(
                    "ALTER TABLE action_approvals ADD COLUMN required_decision TEXT NOT NULL DEFAULT 'REQUIRE_CONFIRMATION'"
                )
            provider_columns = {
                row[1]
                for row in connection.exec_driver_sql("PRAGMA table_info(provider_observations)")
            }
            if provider_columns and "provenance" not in provider_columns:
                connection.exec_driver_sql(
                    "ALTER TABLE provider_observations ADD COLUMN provenance TEXT NOT NULL DEFAULT 'REPLAY'"
                )
            if provider_columns and "expires_at" not in provider_columns:
                connection.exec_driver_sql(
                    "ALTER TABLE provider_observations ADD COLUMN expires_at TEXT"
                )
            guardian_columns = {
                row[1] for row in connection.exec_driver_sql("PRAGMA table_info(guardian_runs)")
            }
            if guardian_columns and "trigger_kind" not in guardian_columns:
                connection.exec_driver_sql(
                    "ALTER TABLE guardian_runs ADD COLUMN trigger_kind TEXT NOT NULL DEFAULT 'REACTIVE'"
                )
            if guardian_columns and "resilience_snapshot_id" not in guardian_columns:
                connection.exec_driver_sql(
                    "ALTER TABLE guardian_runs ADD COLUMN resilience_snapshot_id TEXT"
                )
            offer_columns = {
                row[1]
                for row in connection.exec_driver_sql(
                    "PRAGMA table_info(accommodation_offer_snapshots)"
                )
            }
            plan_columns = {
                row[1] for row in connection.exec_driver_sql("PRAGMA table_info(plan_versions)")
            }
            for column, declaration in {
                "mobility_snapshot_id": "TEXT",
                "mobility_evidence_json": "TEXT",
            }.items():
                if plan_columns and column not in plan_columns:
                    connection.exec_driver_sql(
                        f"ALTER TABLE plan_versions ADD COLUMN {column} {declaration}"
                    )
            place_columns = {
                row[1] for row in connection.exec_driver_sql("PRAGMA table_info(places)")
            }
            # R4 is *additive*: an existing database keeps every row and column it had and gains
            # the two coordinate facts, with WGS-84 as the recorded system of the geometry it
            # already holds (measured: the fixtures and provider rows in this build are WGS-84).
            for column, declaration in {
                "coordinate_system": "TEXT NOT NULL DEFAULT 'WGS84'",
                "source_coordinate_system": "TEXT",
            }.items():
                if place_columns and column not in place_columns:
                    connection.exec_driver_sql(
                        f"ALTER TABLE places ADD COLUMN {column} {declaration}"
                    )
            commitment_columns = {
                row[1] for row in connection.exec_driver_sql("PRAGMA table_info(commitments)")
            }
            if commitment_columns and "monetary_value_status" not in commitment_columns:
                connection.exec_driver_sql(
                    "ALTER TABLE commitments ADD COLUMN monetary_value_status TEXT NOT NULL DEFAULT 'KNOWN'"
                )
            booking_columns = {
                row[1] for row in connection.exec_driver_sql("PRAGMA table_info(bookings)")
            }
            if booking_columns and "amount_status" not in booking_columns:
                connection.exec_driver_sql(
                    "ALTER TABLE bookings ADD COLUMN amount_status TEXT NOT NULL DEFAULT 'KNOWN'"
                )
            for column, declaration in {
                "coordinate_crs": "TEXT NOT NULL DEFAULT 'WGS84'",
                "luggage_drop_json": "TEXT NOT NULL DEFAULT '{}'",
            }.items():
                if offer_columns and column not in offer_columns:
                    connection.exec_driver_sql(
                        f"ALTER TABLE accommodation_offer_snapshots ADD COLUMN {column} {declaration}"
                    )
            option_columns = {
                row[1]
                for row in connection.exec_driver_sql("PRAGMA table_info(stay_journey_options)")
            }
            if option_columns and "materialized_plan_version_id" not in option_columns:
                connection.exec_driver_sql(
                    "ALTER TABLE stay_journey_options ADD COLUMN materialized_plan_version_id TEXT"
                )
            # ORD-0355: an *additive* column, and deliberately without a DEFAULT.  A default of
            # 0 would rewrite every pre-existing row into "the traveller was asked and said no",
            # which is the very fabrication this order removes; NULL leaves those rows as what
            # they are - never recorded - and the code that reads them keeps its old answer.
            trip_context_columns = {
                row[1]
                for row in connection.exec_driver_sql(
                    "PRAGMA table_info(journey_trip_contexts)"
                )
            }
            if trip_context_columns and "stay_budget_declared" not in trip_context_columns:
                connection.exec_driver_sql(
                    "ALTER TABLE journey_trip_contexts ADD COLUMN stay_budget_declared INTEGER"
                    " CHECK (stay_budget_declared IN (0, 1))"
                )
            mobility_columns = {
                row[1] for row in connection.exec_driver_sql("PRAGMA table_info(mobility_facts)")
            }
            for column, declaration in {
                "geometry_json": "TEXT NOT NULL DEFAULT '{}'",
                "crs": "TEXT NOT NULL DEFAULT 'WGS84'",
                "paid_mode": "INTEGER NOT NULL DEFAULT 0",
                "expires_at": "TEXT",
            }.items():
                if mobility_columns and column not in mobility_columns:
                    connection.exec_driver_sql(
                        f"ALTER TABLE mobility_facts ADD COLUMN {column} {declaration}"
                    )
            # ORD-0466: version-scoped share grants. A nullable column with no DEFAULT, because the
            # rows this table already holds are journey-level grants, and "no version" is their real
            # shape -- a DEFAULT of '' or a back-filled id would invent a scope nobody granted.  No
            # foreign key either, and `models.py` says why: every ON DELETE option there would
            # either delete the grant with its version (collapsing the 409 into a 404) or widen the
            # scope to the whole journey.
            share_grant_columns = {
                row[1]
                for row in connection.exec_driver_sql("PRAGMA table_info(journey_share_grants)")
            }
            if share_grant_columns and "plan_version_id" not in share_grant_columns:
                connection.exec_driver_sql(
                    "ALTER TABLE journey_share_grants ADD COLUMN plan_version_id TEXT"
                )
            _reconcile_commitment_strength_check(connection)
            _reconcile_activity_status_check(connection)
        after = _table_names(connection)
        created = sorted(after - before)
        report: dict[str, object] = {
            "database": str(target_engine.url.database),
            "before": len(before),
            "after": len(after),
            "created": created,
        }

    # SQLite does not allow toggling this pragma while a transaction is open.
    # The activity-table reconciliation temporarily disables it so the table
    # can be rebuilt without touching the dependent tables' foreign-key SQL.
    # Re-enable it on the pooled connection after the schema transaction has
    # committed.
    if target_engine.dialect.name == "sqlite":
        with target_engine.connect() as connection:
            connection.execute(text("PRAGMA foreign_keys = ON"))

    # Published only after the transaction committed: an uncommitted catch-up would name tables a
    # rolled-back boot never actually created.
    SCHEMA_CATCHUP.clear()
    SCHEMA_CATCHUP.update(report)
    if created:
        schema_log.info(
            "ORD-0488 schema catch-up on %s: %d tables were missing and are now created: %s",
            target_engine.url.database,
            len(created),
            ", ".join(created),
        )
    return dict(SCHEMA_CATCHUP)


def _reconcile_commitment_strength_check(connection) -> None:
    """Upgrade a pre-WP04 SQLite commitments CHECK without guessing labels.

    The workspace database may have been created before the scaffold and still
    contain the old four-label CHECK.  SQLite cannot alter a CHECK constraint
    in place, so rebuild only that table while preserving every row.  Legacy
    labels have no lossless source-domain meaning; they are conservatively
    represented as the exact lowest source level rather than being promoted to
    a guessed C1/C2/C3/C4.
    """

    row = connection.exec_driver_sql(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='commitments'"
    ).scalar()
    if not row or "C4_Anchored" in row:
        return
    old_rows = connection.exec_driver_sql("SELECT COUNT(*) FROM commitments").scalar() or 0
    connection.exec_driver_sql("PRAGMA foreign_keys = OFF")
    if old_rows:
        connection.exec_driver_sql(
            "CREATE TABLE commitments_wp04_backup AS SELECT * FROM commitments"
        )
    connection.exec_driver_sql("DROP TABLE commitments")
    connection.exec_driver_sql(
        """
        CREATE TABLE commitments_wp04_reconciled (
            id TEXT PRIMARY KEY,
            journey_id TEXT NOT NULL REFERENCES journeys(id) ON DELETE CASCADE,
            activity_id TEXT REFERENCES activities(id) ON DELETE SET NULL,
            kind TEXT NOT NULL CHECK (kind IN ('BOOKING','ORDER','PAYMENT','APPOINTMENT','OTHER')),
            level TEXT NOT NULL CHECK (level IN ('C0_Flexible','C1_Preferred','C2_Reserved','C3_Guaranteed','C4_Anchored')),
            status TEXT NOT NULL CHECK (status IN ('PROPOSED','ACTIVE','FULFILLED','CANCELLED','BREACHED')),
            monetary_value_minor INTEGER NOT NULL DEFAULT 0 CHECK (monetary_value_minor >= 0),
            currency TEXT NOT NULL,
            external_ref TEXT,
            policy_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    # The old table has already been dropped.  There are no rows in the normal
    # pre-WP04 checkpoint, but keep the branch explicit for diagnostic clarity.
    if old_rows:
        # A pre-WP04 row can only carry the lossy legacy labels.  C0 is the
        # conservative exact source value; WP04 writes all future levels from
        # the frozen generated tuple.
        connection.exec_driver_sql(
            """
            INSERT INTO commitments_wp04_reconciled
                (id, journey_id, activity_id, kind, level, status,
                 monetary_value_minor, currency, external_ref, policy_json,
                 created_at, updated_at)
            SELECT id, journey_id, activity_id, kind, 'C0_Flexible', status,
                   monetary_value_minor, currency, external_ref,
                   policy_json, created_at, updated_at
            FROM commitments_wp04_backup
            """
        )
        connection.exec_driver_sql("DROP TABLE commitments_wp04_backup")
    connection.exec_driver_sql("ALTER TABLE commitments_wp04_reconciled RENAME TO commitments")
    connection.exec_driver_sql("PRAGMA foreign_keys = ON")


def _reconcile_activity_status_check(connection) -> None:
    """Upgrade the pre-V12 activity status CHECK in an existing SQLite DB.

    V12 uses a real execution state machine (NOT_STARTED -> EN_ROUTE ->
    ARRIVED -> IN_PROGRESS -> COMPLETED/SKIPPED).  SQLite cannot alter a CHECK
    constraint in place, so rebuild only this table and preserve all rows.
    The table name is kept stable during the rebuild so existing dependent
    foreign-key definitions continue to refer to ``activities``.
    """

    row = connection.exec_driver_sql(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='activities'"
    ).scalar()
    if not row or "NOT_STARTED" in row:
        return

    connection.exec_driver_sql("PRAGMA foreign_keys = OFF")
    connection.exec_driver_sql(
        """
        CREATE TABLE activities_v12_reconciled (
            id TEXT PRIMARY KEY,
            plan_version_id TEXT NOT NULL REFERENCES plan_versions(id) ON DELETE CASCADE,
            sequence_no INTEGER NOT NULL CHECK (sequence_no >= 1),
            activity_type TEXT NOT NULL,
            title TEXT NOT NULL,
            place_id TEXT REFERENCES places(id) ON DELETE SET NULL,
            city_service_id TEXT REFERENCES city_services(id) ON DELETE SET NULL,
            start_at TEXT NOT NULL,
            end_at TEXT NOT NULL,
            travel_from_prev_min INTEGER NOT NULL DEFAULT 0 CHECK (travel_from_prev_min >= 0),
            estimated_cost_minor INTEGER NOT NULL DEFAULT 0 CHECK (estimated_cost_minor >= 0),
            currency TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('PLANNED','READY','NOT_STARTED','EN_ROUTE','ARRIVED','IN_PROGRESS','ACTIVE','COMPLETED','SKIPPED','CANCELLED')),
            locked INTEGER NOT NULL DEFAULT 0 CHECK (locked IN (0,1)),
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(plan_version_id, sequence_no),
            CHECK (end_at > start_at)
        )
        """
    )
    connection.exec_driver_sql(
        """
        INSERT INTO activities_v12_reconciled
            (id, plan_version_id, sequence_no, activity_type, title, place_id,
             city_service_id, start_at, end_at, travel_from_prev_min,
             estimated_cost_minor, currency, status, locked, metadata_json,
             created_at, updated_at)
        SELECT id, plan_version_id, sequence_no, activity_type, title, place_id,
               city_service_id, start_at, end_at, travel_from_prev_min,
               estimated_cost_minor, currency, status, locked, metadata_json,
               created_at, updated_at
        FROM activities
        """
    )
    connection.exec_driver_sql("DROP TABLE activities")
    connection.exec_driver_sql("ALTER TABLE activities_v12_reconciled RENAME TO activities")
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_activities_plan ON activities(plan_version_id, sequence_no)"
    )


def _reconcile_real_place_candidate_scope(connection) -> None:
    """Keep provider observations scoped to the Journey that captured them."""

    row = connection.exec_driver_sql(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='real_place_candidates'"
    ).scalar()
    normalized = "".join(str(row or "").split())
    if not row or "UNIQUE(journey_id,provider_key,external_id)" in normalized:
        return
    connection.exec_driver_sql("PRAGMA foreign_keys = OFF")
    connection.exec_driver_sql(
        """
        CREATE TABLE real_place_candidates_v134 (
            id TEXT PRIMARY KEY,
            journey_id TEXT REFERENCES journeys(id) ON DELETE CASCADE,
            provider_key TEXT NOT NULL,
            external_id TEXT NOT NULL,
            city_code TEXT NOT NULL,
            name TEXT NOT NULL,
            category TEXT NOT NULL,
            latitude REAL NOT NULL,
            longitude REAL NOT NULL,
            tags_json TEXT NOT NULL DEFAULT '[]',
            opening_hours_json TEXT NOT NULL DEFAULT '{}',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            captured_at TEXT NOT NULL,
            expires_at TEXT,
            distance_m REAL,
            evidence_json TEXT NOT NULL DEFAULT '{}',
            materialized_place_id TEXT,
            status TEXT NOT NULL DEFAULT 'DISCOVERED',
            UNIQUE(journey_id, provider_key, external_id)
        )
        """
    )
    connection.exec_driver_sql(
        """
        INSERT INTO real_place_candidates_v134
            (id, journey_id, provider_key, external_id, city_code, name, category,
             latitude, longitude, tags_json, opening_hours_json, metadata_json,
             captured_at, expires_at, distance_m, evidence_json, materialized_place_id, status)
        SELECT id, journey_id, provider_key, external_id, city_code, name, category,
               latitude, longitude, tags_json, opening_hours_json, metadata_json,
               captured_at, expires_at, distance_m, evidence_json, materialized_place_id, status
        FROM real_place_candidates
        """
    )
    connection.exec_driver_sql("DROP TABLE real_place_candidates")
    connection.exec_driver_sql(
        "ALTER TABLE real_place_candidates_v134 RENAME TO real_place_candidates"
    )
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_real_place_candidates_journey "
        "ON real_place_candidates(journey_id, captured_at)"
    )
    connection.exec_driver_sql("PRAGMA foreign_keys = ON")


def get_session() -> Generator[Session, None, None]:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
