from __future__ import annotations

import json
from datetime import datetime, timezone

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
# that states its scope in one sentence (RELATION/relation_and_copy_findings.md:94); the sentence
# itself is not stored here, so no server-authored prose can ever reach a screen from this table.
JOURNEY_ROLE_CAPABILITIES = (
    ("OWNER", "JOURNEY_READ", "member.scope.owner"),
    ("OWNER", "JOURNEY_UPDATE", "member.scope.owner"),
    ("OWNER", "JOURNEY_EXECUTION_UPDATE", "member.scope.owner"),
    ("OWNER", "PLACE_EDIT", "member.scope.owner"),
    ("OWNER", "MEMBER_MANAGE", "member.scope.owner"),
    ("OWNER", "SHARE_GRANT_MANAGE", "member.scope.owner"),
    ("ADMIN", "JOURNEY_READ", "member.scope.admin"),
    ("ADMIN", "JOURNEY_EXECUTION_UPDATE", "member.scope.admin"),
    ("ADMIN", "PLACE_EDIT", "member.scope.admin"),
    ("ADMIN", "MEMBER_MANAGE", "member.scope.admin"),
    ("COLLABORATOR", "JOURNEY_READ", "member.scope.collaborator"),
    ("COLLABORATOR", "JOURNEY_EXECUTION_UPDATE", "member.scope.collaborator"),
    ("COLLABORATOR", "PLACE_EDIT", "member.scope.collaborator"),
)


def _seed_journey_acl(connection) -> int:
    """Insert the role/capability rows when the table is still empty.  Returns rows added."""
    existing = int(connection.exec_driver_sql("SELECT COUNT(*) FROM journey_role_capabilities").scalar() or 0)
    if existing:
        # R62 adds only self-reported execution progress authority.  Backfill
        # this narrowly on existing databases without rewriting other ACL rows.
        added = 0
        for role in ("OWNER", "ADMIN", "COLLABORATOR"):
            result = connection.exec_driver_sql(
                "INSERT OR IGNORE INTO journey_role_capabilities"
                " (role, capability, scope_message_key, created_at)"
                " VALUES (?, 'JOURNEY_EXECUTION_UPDATE', ?, datetime('now'))",
                (role, f"member.scope.{role.lower()}"),
            )
            added += int(result.rowcount or 0)
        return added
    for role, capability, message_key in JOURNEY_ROLE_CAPABILITIES:
        connection.exec_driver_sql(
            "INSERT INTO journey_role_capabilities"
            " (role, capability, scope_message_key, created_at) VALUES (?, ?, ?, datetime('now'))",
            (role, capability, message_key),
        )
    return len(JOURNEY_ROLE_CAPABILITIES)


def _migrate_journey_execution_capability(connection) -> None:
    """Extend the SQLite ACL vocabulary while preserving every existing grant."""
    row = connection.exec_driver_sql(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='journey_role_capabilities'"
    ).first()
    schema = str(row[0] or "") if row else ""
    if not schema or "JOURNEY_EXECUTION_UPDATE" in schema:
        return
    connection.exec_driver_sql(
        "ALTER TABLE journey_role_capabilities RENAME TO journey_role_capabilities_pre_r62"
    )
    connection.exec_driver_sql(
        "CREATE TABLE journey_role_capabilities ("
        "role TEXT NOT NULL CHECK (role IN ('OWNER','ADMIN','COLLABORATOR')),"
        "capability TEXT NOT NULL CHECK (capability IN "
        "('JOURNEY_READ','JOURNEY_UPDATE','JOURNEY_EXECUTION_UPDATE','PLACE_EDIT','MEMBER_MANAGE','SHARE_GRANT_MANAGE')) ,"
        "scope_message_key TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY (role, capability))"
    )
    connection.exec_driver_sql(
        "INSERT INTO journey_role_capabilities (role, capability, scope_message_key, created_at) "
        "SELECT role, capability, scope_message_key, created_at "
        "FROM journey_role_capabilities_pre_r62"
    )
    connection.exec_driver_sql("DROP TABLE journey_role_capabilities_pre_r62")


def _table_exists(connection, name: str) -> bool:
    row = connection.exec_driver_sql(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _migrate_private_buddy_route_location_bindings(connection) -> None:
    """Install the R81 additive binding index and write-once guard.

    The canonical table is introduced by ``db/schema.sql``. Existing R80
    observations are intentionally left unbound: they have no durable record of
    which accepted handoff authorized their capture and therefore fail closed.
    Reasserting the index and trigger here makes this an idempotent ensure_schema
    migration as well as a fresh-database schema change.
    """

    table = "private_buddy_route_location_bindings"
    if not _table_exists(connection, table):
        return
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_private_buddy_route_bindings_context_expiry "
        "ON private_buddy_route_location_bindings "
        "(journey_id, handoff_id, journey_member_id, traveler_id, expires_at)"
    )
    connection.exec_driver_sql(
        "CREATE TRIGGER IF NOT EXISTS trg_private_buddy_route_binding_no_update "
        "BEFORE UPDATE ON private_buddy_route_location_bindings "
        "BEGIN SELECT RAISE(ABORT, 'private buddy route location binding is immutable'); END"
    )


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



def _reconcile_agent_pending_action_status_check(connection) -> None:
    """Upgrade pre-R32 pending-action CHECK while preserving durable history."""

    row = connection.exec_driver_sql(
        "SELECT sql FROM sqlite_master "
        "WHERE type='table' AND name='agent_pending_actions'"
    ).scalar()
    if not row or "SUPERSEDED" in str(row):
        return

    columns = {
        item[1]
        for item in connection.exec_driver_sql(
            "PRAGMA table_info(agent_pending_actions)"
        )
    }
    required = {
        "id",
        "approval_id",
        "run_id",
        "mission_id",
        "journey_id",
        "originating_step_id",
        "tool_name",
        "arguments_json",
        "status",
        "result_json",
        "error_json",
        "created_at",
        "claimed_at",
        "resumed_at",
        "updated_at",
        "authority_receipt_id",
        "recovery_epoch_id",
    }
    if not required.issubset(columns):
        raise RuntimeError(
            "agent_pending_actions cannot be upgraded to R32 safely"
        )

    connection.exec_driver_sql(
        """
        CREATE TABLE agent_pending_actions_r32_reconciled (
            id TEXT PRIMARY KEY,
            approval_id TEXT NOT NULL UNIQUE
                REFERENCES action_approvals(id) ON DELETE CASCADE,
            run_id TEXT NOT NULL
                REFERENCES agent_runs(id) ON DELETE CASCADE,
            mission_id TEXT NOT NULL
                REFERENCES journey_missions(id) ON DELETE CASCADE,
            journey_id TEXT NOT NULL
                REFERENCES journeys(id) ON DELETE CASCADE,
            originating_step_id TEXT
                REFERENCES agent_run_steps(id) ON DELETE SET NULL,
            tool_name TEXT NOT NULL,
            arguments_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'PENDING'
                CHECK (status IN (
                    'PENDING','RESUMING','RESUMED','FAILED',
                    'EXPIRED','SUPERSEDED'
                )),
            result_json TEXT NOT NULL DEFAULT '{}',
            error_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            claimed_at TEXT,
            resumed_at TEXT,
            updated_at TEXT NOT NULL,
            authority_receipt_id TEXT,
            recovery_epoch_id TEXT
                REFERENCES recovery_epochs(id) ON DELETE SET NULL
        )
        """
    )
    connection.exec_driver_sql(
        """
        INSERT INTO agent_pending_actions_r32_reconciled (
            id, approval_id, run_id, mission_id, journey_id,
            originating_step_id, tool_name, arguments_json, status,
            result_json, error_json, created_at, claimed_at, resumed_at,
            updated_at, authority_receipt_id, recovery_epoch_id
        )
        SELECT
            id, approval_id, run_id, mission_id, journey_id,
            originating_step_id, tool_name, arguments_json, status,
            result_json, error_json, created_at, claimed_at, resumed_at,
            updated_at, authority_receipt_id, recovery_epoch_id
        FROM agent_pending_actions
        """
    )
    connection.exec_driver_sql("DROP TABLE agent_pending_actions")
    connection.exec_driver_sql(
        "ALTER TABLE agent_pending_actions_r32_reconciled "
        "RENAME TO agent_pending_actions"
    )
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_agent_pending_actions_run "
        "ON agent_pending_actions(run_id, status, created_at)"
    )
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_agent_pending_actions_journey "
        "ON agent_pending_actions(journey_id, status, created_at)"
    )


def ensure_schema(target_engine: Engine = engine) -> None:
    """Apply the package SQL schema to an empty or existing SQLite database."""
    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    with target_engine.begin() as connection:
        raw = getattr(connection.connection, "driver_connection", connection.connection)
        # Quarantine historical duplicates *before* the schema script runs, so
        # the unique index in the script can actually be created on a database
        # that already contains them.  A fresh database has no such table yet,
        # so this is a no-op there.
        if target_engine.dialect.name == "sqlite":
            _quarantine_existing_duplicates(connection)
            _collapse_duplicate_active_plans(connection)
            _retire_duplicate_active_anchors(connection)
            _prepare_shared_stop_grant_slot(connection)
            # ORD-0167: parked before the script so the new table is the one in `db/schema.sql`.
            _preserve_legacy_hydration_table(connection)
        if hasattr(raw, "executescript"):
            raw.executescript(sql)
        else:  # pragma: no cover - retained for non-SQLite development engines
            for statement in sql.split(";"):
                if statement.strip():
                    connection.exec_driver_sql(statement)
        if target_engine.dialect.name == "sqlite":
            _migrate_private_buddy_route_location_bindings(connection)
            _reconcile_real_place_candidate_scope(connection)
            _backfill_legacy_hydration_rows(connection)
            # R100: a member-private, ordered Pocket Walk intent stores only
            # shortlist identities and an optional human walking budget.  The
            # public coordinates and source facts remain on the signed
            # shortlist rows and are never duplicated here.
            if not _table_exists(connection, "member_draft_pocket_walks"):
                connection.exec_driver_sql(
                    """CREATE TABLE member_draft_pocket_walks (
                    id TEXT PRIMARY KEY,
                    journey_id TEXT NOT NULL REFERENCES journeys(id) ON DELETE CASCADE,
                    member_id TEXT NOT NULL REFERENCES journey_members(id) ON DELETE CASCADE,
                    traveler_id TEXT NOT NULL REFERENCES travelers(id) ON DELETE CASCADE,
                    first_shortlist_id TEXT NOT NULL REFERENCES member_draft_shortlist_items(id) ON DELETE CASCADE,
                    second_shortlist_id TEXT NOT NULL REFERENCES member_draft_shortlist_items(id) ON DELETE CASCADE,
                    max_walk_minutes INTEGER CHECK (max_walk_minutes IS NULL OR (max_walk_minutes >= 1 AND max_walk_minutes <= 180)),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(journey_id, member_id, traveler_id),
                    CHECK(first_shortlist_id <> second_shortlist_id)
                    )"""
                )
            pocket_walk_columns = {
                row[1]
                for row in connection.exec_driver_sql(
                    "PRAGMA table_info(member_draft_pocket_walks)"
                )
            }
            if pocket_walk_columns and "revision" not in pocket_walk_columns:
                connection.exec_driver_sql(
                    "ALTER TABLE member_draft_pocket_walks "
                    "ADD COLUMN revision INTEGER NOT NULL DEFAULT 1"
                )
            connection.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS idx_member_draft_pocket_walk_owner "
                "ON member_draft_pocket_walks(journey_id, member_id, traveler_id, updated_at)"
            )
            if not _table_exists(connection, "member_draft_pocket_walk_progress"):
                connection.exec_driver_sql(
                    """CREATE TABLE member_draft_pocket_walk_progress (
                    id TEXT PRIMARY KEY,
                    journey_id TEXT NOT NULL REFERENCES journeys(id) ON DELETE CASCADE,
                    member_id TEXT NOT NULL REFERENCES journey_members(id) ON DELETE CASCADE,
                    traveler_id TEXT NOT NULL REFERENCES travelers(id) ON DELETE CASCADE,
                    pocket_walk_id TEXT NOT NULL REFERENCES member_draft_pocket_walks(id) ON DELETE CASCADE,
                    first_shortlist_id TEXT NOT NULL REFERENCES member_draft_shortlist_items(id) ON DELETE CASCADE,
                    second_shortlist_id TEXT NOT NULL REFERENCES member_draft_shortlist_items(id) ON DELETE CASCADE,
                    pocket_walk_revision INTEGER NOT NULL DEFAULT 1,
                    state TEXT NOT NULL DEFAULT 'NOT_STARTED' CHECK (state IN (
                        'NOT_STARTED', 'HEADING_TO_FIRST', 'FIRST_STOP_DONE_SELF_REPORTED',
                        'HEADING_TO_SECOND', 'WALK_DONE_SELF_REPORTED', 'PAUSED'
                    )),
                    paused_from_state TEXT,
                    progress_revision INTEGER NOT NULL DEFAULT 0,
                    progress_class TEXT NOT NULL DEFAULT 'USER_REPORTED_PROGRESS'
                        CHECK (progress_class = 'USER_REPORTED_PROGRESS'),
                    last_operation_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(journey_id, member_id, traveler_id)
                    )"""
                )
            connection.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS idx_member_draft_pocket_walk_progress_owner "
                "ON member_draft_pocket_walk_progress(journey_id, member_id, traveler_id, updated_at)"
            )
            invitation_columns = {
                row[1] for row in connection.exec_driver_sql(
                    "PRAGMA table_info(member_place_invitations)"
                )
            }
            if invitation_columns and "disclosure_snapshot_json" not in invitation_columns:
                # Keep the exact public Place and rendezvous values the inviter
                # reviewed. Legacy rows without a snapshot fail closed as stale.
                connection.exec_driver_sql(
                    "ALTER TABLE member_place_invitations "
                    "ADD COLUMN disclosure_snapshot_json TEXT NOT NULL DEFAULT '{}'"
                )
            journey_columns = {
                row[1] for row in connection.exec_driver_sql("PRAGMA table_info(journeys)")
            }
            member_columns = {
                row[1] for row in connection.exec_driver_sql("PRAGMA table_info(journey_members)")
            }
            if member_columns and "preferences_json" not in member_columns:
                connection.exec_driver_sql(
                    "ALTER TABLE journey_members ADD COLUMN preferences_json TEXT NOT NULL DEFAULT '{}'"
                )
            location_columns = {
                row[1] for row in connection.exec_driver_sql("PRAGMA table_info(location_observations)")
            }
            if location_columns and "traveler_id" not in location_columns:
                connection.exec_driver_sql(
                    "ALTER TABLE location_observations ADD COLUMN traveler_id TEXT REFERENCES travelers(id) ON DELETE SET NULL"
                )
            if location_columns and "journey_member_id" not in location_columns:
                connection.exec_driver_sql(
                    "ALTER TABLE location_observations ADD COLUMN journey_member_id TEXT REFERENCES journey_members(id) ON DELETE SET NULL"
                )
            inbox_columns = {
                row[1] for row in connection.exec_driver_sql("PRAGMA table_info(journey_inbox_items)")
            }
            if inbox_columns and "reality_claim_id" not in inbox_columns:
                connection.exec_driver_sql("ALTER TABLE journey_inbox_items ADD COLUMN reality_claim_id TEXT")
            if "planning_mode" not in journey_columns:
                connection.exec_driver_sql(
                    "ALTER TABLE journeys ADD COLUMN planning_mode TEXT NOT NULL DEFAULT 'balanced'"
                )
            # ORD-0383 slice 1: additive, and no DEFAULT.  A row written before this column
            # existed was never deleted, and NULL is the only value that says so without
            # inventing a deletion moment for it.
            if "deleted_at" not in journey_columns:
                connection.exec_driver_sql("ALTER TABLE journeys ADD COLUMN deleted_at TEXT")
            plan_columns = {
                row[1] for row in connection.exec_driver_sql("PRAGMA table_info(plan_versions)")
            }
            if plan_columns and "lifecycle" not in plan_columns:
                connection.exec_driver_sql(
                    "ALTER TABLE plan_versions ADD COLUMN lifecycle TEXT NOT NULL DEFAULT 'ACTIVE'"
                )
            plan_columns = {
                row[1] for row in connection.exec_driver_sql("PRAGMA table_info(plan_versions)")
            }
            if plan_columns and "trigger_contract_json" not in plan_columns:
                connection.exec_driver_sql(
                    "ALTER TABLE plan_versions ADD COLUMN trigger_contract_json TEXT NOT NULL DEFAULT '{}'"
                )
            # R18 wrote its standby marker into explanation only.  Promote that
            # persisted intent into an explicit lifecycle without changing the
            # Journey's one active pointer.
            connection.exec_driver_sql(
                "UPDATE plan_versions SET lifecycle='STANDBY' "
                "WHERE status='CANDIDATE' AND explanation_json LIKE '%NON_ACTIVE_STANDBY%'"
            )
            _migrate_reservation_identity(connection)
            # ORD-0434: the ACL table ships seeded, so a client can read the product's own answer
            # to what a role may do instead of inventing one per screen.
            _migrate_journey_execution_capability(connection)
            _seed_journey_acl(connection)
            event_columns = {
                row[1] for row in connection.exec_driver_sql("PRAGMA table_info(events)")
            }
            if event_columns and "provenance" not in event_columns:
                connection.exec_driver_sql(
                    "ALTER TABLE events ADD COLUMN provenance TEXT NOT NULL DEFAULT 'SIMULATED' "
                    "CHECK (provenance IN ('SIMULATED','PROVIDER_VERIFIED','USER_REPORTED','SYSTEM_DERIVED'))"
                )
            # R22 adds a server-derived opportunity signal.  Existing SQLite
            # databases carry the older event_type CHECK constraint, so rebuild
            # only this table while preserving every historical event row.
            event_schema_row = connection.exec_driver_sql(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='events'"
            ).first()
            event_schema = str(event_schema_row[0] or "") if event_schema_row else ""
            # SQLite may rewrite dependent foreign-key SQL during the rename
            # even with foreign_keys disabled.  Repair that textual reference
            # on a retry-safe boot before any dependent insert is attempted.
            legacy_event_refs = connection.exec_driver_sql(
                "SELECT COUNT(*) FROM sqlite_master "
                "WHERE sql IS NOT NULL AND sql LIKE '%events_r22_legacy%'"
            ).scalar_one()
            if legacy_event_refs:
                connection.exec_driver_sql("PRAGMA writable_schema=ON")
                connection.exec_driver_sql(
                    "UPDATE sqlite_master SET sql=replace(sql, 'events_r22_legacy', 'events') "
                    "WHERE sql IS NOT NULL AND sql LIKE '%events_r22_legacy%'"
                )
                connection.exec_driver_sql("PRAGMA writable_schema=OFF")
                schema_version = connection.exec_driver_sql("PRAGMA schema_version").scalar_one()
                connection.exec_driver_sql(f"PRAGMA schema_version = {int(schema_version) + 1}")
            if event_schema and "OPPORTUNITY_WINDOW" not in event_schema:
                connection.exec_driver_sql("PRAGMA legacy_alter_table=ON")
                connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
                connection.exec_driver_sql("ALTER TABLE events RENAME TO events_r22_legacy")
                connection.exec_driver_sql(
                    """
                    CREATE TABLE events (
                        id TEXT PRIMARY KEY,
                        journey_id TEXT NOT NULL REFERENCES journeys(id) ON DELETE CASCADE,
                        provider_key TEXT NOT NULL,
                        provenance TEXT NOT NULL DEFAULT 'SIMULATED'
                            CHECK (provenance IN ('SIMULATED','PROVIDER_VERIFIED','USER_REPORTED','SYSTEM_DERIVED')),
                        event_type TEXT NOT NULL CHECK (event_type IN (
                            'TRAFFIC_DELAY','CLOSURE','WEATHER','SCHEDULE_DELAY',
                            'SERVICE_UNAVAILABLE','USER_CHANGE','OPPORTUNITY_WINDOW','OTHER'
                        )),
                        severity TEXT NOT NULL CHECK (severity IN ('INFO','MINOR','MAJOR','CRITICAL')),
                        status TEXT NOT NULL CHECK (status IN ('DETECTED','ACKNOWLEDGED','RESOLVED','IGNORED')),
                        affected_activity_id TEXT REFERENCES activities(id) ON DELETE SET NULL,
                        occurred_at TEXT NOT NULL,
                        detected_at TEXT NOT NULL,
                        resolved_at TEXT,
                        payload_json TEXT NOT NULL DEFAULT '{}',
                        created_at TEXT NOT NULL
                    )
                    """
                )
                connection.exec_driver_sql(
                    """
                    INSERT INTO events (
                        id, journey_id, provider_key, provenance, event_type, severity,
                        status, affected_activity_id, occurred_at, detected_at,
                        resolved_at, payload_json, created_at
                    )
                    SELECT id, journey_id, provider_key, provenance, event_type, severity,
                        status, affected_activity_id, occurred_at, detected_at,
                        resolved_at, payload_json, created_at
                    FROM events_r22_legacy
                    """
                )
                connection.exec_driver_sql("DROP TABLE events_r22_legacy")
                connection.exec_driver_sql("PRAGMA foreign_keys=ON")
                connection.exec_driver_sql("PRAGMA legacy_alter_table=OFF")
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
                "clarification_json": "TEXT NOT NULL DEFAULT '{}'",
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
            if approval_columns and "authority_receipt_id" not in approval_columns:
                connection.exec_driver_sql(
                    "ALTER TABLE action_approvals ADD COLUMN authority_receipt_id TEXT"
                )
            pending_columns = {
                row[1] for row in connection.exec_driver_sql("PRAGMA table_info(agent_pending_actions)")
            }
            if pending_columns and "authority_receipt_id" not in pending_columns:
                connection.exec_driver_sql(
                    "ALTER TABLE agent_pending_actions ADD COLUMN authority_receipt_id TEXT"
                )
            # R32: durable server-owned recovery reality epochs.  All bindings
            # are nullable for legacy rows; new authority paths fail closed
            # when they have no epoch.
            if not _table_exists(connection, "recovery_epochs"):
                connection.exec_driver_sql(
                    """CREATE TABLE recovery_epochs (
                    id TEXT PRIMARY KEY,
                    journey_id TEXT NOT NULL REFERENCES journeys(id) ON DELETE CASCADE,
                    generation INTEGER NOT NULL,
                    active_plan_version_id_at_open TEXT,
                    material_world_epoch_at_open TEXT,
                    recovery_reality_fingerprint TEXT NOT NULL,
                    triggering_event_ids_json TEXT NOT NULL DEFAULT '[]',
                    status TEXT NOT NULL CHECK (status IN ('OPEN','SUPERSEDED','COMMITTED','ABANDONED')),
                    opened_at TEXT NOT NULL,
                    closed_at TEXT,
                    close_reason TEXT,
                    superseded_by_recovery_epoch_id TEXT REFERENCES recovery_epochs(id) ON DELETE SET NULL,
                    UNIQUE(journey_id, generation)
                    )"""
                )
            if not _table_exists(connection, "operator_recovery_resumes"):
                connection.exec_driver_sql(
                    """CREATE TABLE operator_recovery_resumes (
                    id TEXT PRIMARY KEY,
                    journey_id TEXT NOT NULL REFERENCES journeys(id) ON DELETE CASCADE,
                    recovery_epoch_id TEXT NOT NULL REFERENCES recovery_epochs(id) ON DELETE CASCADE,
                    event_id TEXT REFERENCES events(id) ON DELETE SET NULL,
                    incident_fingerprint TEXT NOT NULL,
                    standing_mission_id TEXT REFERENCES journey_missions(id) ON DELETE SET NULL,
                    recovery_mission_id TEXT REFERENCES journey_missions(id) ON DELETE SET NULL,
                    guardian_run_id TEXT REFERENCES guardian_runs(id) ON DELETE SET NULL,
                    active_plan_version_id_at_block TEXT,
                    material_world_epoch_at_block TEXT,
                    blocked_reason TEXT NOT NULL,
                    failure_classification TEXT NOT NULL,
                    provider_key TEXT,
                    model_key TEXT,
                    status TEXT NOT NULL DEFAULT 'PENDING',
                    next_retry_at TEXT,
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    safe_to_resume INTEGER NOT NULL DEFAULT 1,
                    owner_runtime_key TEXT,
                    owner_claim_token TEXT,
                    owner_claimed_at TEXT,
                    last_attempt_at TEXT,
                    resumed_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(journey_id, recovery_epoch_id)
                    )"""
                )
            if not _table_exists(connection, "recovery_discovery_attempts"):
                connection.exec_driver_sql(
                    """CREATE TABLE recovery_discovery_attempts (
                    id TEXT PRIMARY KEY,
                    journey_id TEXT NOT NULL REFERENCES journeys(id) ON DELETE CASCADE,
                    recovery_epoch_id TEXT NOT NULL REFERENCES recovery_epochs(id) ON DELETE CASCADE,
                    mission_id TEXT,
                    guardian_run_id TEXT,
                    query_hash TEXT NOT NULL,
                    query TEXT NOT NULL,
                    category TEXT,
                    radius_m INTEGER NOT NULL DEFAULT 2500,
                    candidate_limit INTEGER NOT NULL DEFAULT 8,
                    status TEXT NOT NULL DEFAULT 'COMPLETED',
                    provider_mode TEXT NOT NULL,
                    runtime_provenance TEXT NOT NULL,
                    raw_candidate_count INTEGER NOT NULL DEFAULT 0,
                    materialized_place_ids_json TEXT NOT NULL DEFAULT '[]',
                    deep_candidate_ids_json TEXT NOT NULL DEFAULT '[]',
                    provider_status_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(recovery_epoch_id, query_hash)
                    )"""
                )
            if not _table_exists(connection, "recovery_execution_plans"):
                connection.exec_driver_sql(
                    """CREATE TABLE recovery_execution_plans (
                    id TEXT PRIMARY KEY,
                    journey_id TEXT NOT NULL REFERENCES journeys(id) ON DELETE CASCADE,
                    recovery_epoch_id TEXT NOT NULL REFERENCES recovery_epochs(id) ON DELETE CASCADE,
                    proposal_id TEXT NOT NULL REFERENCES replan_proposals(id) ON DELETE CASCADE,
                    agent_run_id TEXT REFERENCES agent_runs(id) ON DELETE SET NULL,
                    status TEXT NOT NULL DEFAULT 'PLANNED',
                    semantic_fingerprint TEXT NOT NULL,
                    approval_id TEXT REFERENCES action_approvals(id) ON DELETE SET NULL,
                    error_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT,
                    UNIQUE(recovery_epoch_id, proposal_id)
                    )"""
                )
            if not _table_exists(connection, "recovery_execution_actions"):
                connection.exec_driver_sql(
                    """CREATE TABLE recovery_execution_actions (
                    id TEXT PRIMARY KEY,
                    execution_plan_id TEXT NOT NULL REFERENCES recovery_execution_plans(id) ON DELETE CASCADE,
                    sequence_no INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    target_type TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    provider_key TEXT,
                    semantic_fingerprint TEXT NOT NULL,
                    authority_decision TEXT NOT NULL DEFAULT 'REQUIRE_CONFIRMATION',
                    approval_id TEXT,
                    status TEXT NOT NULL DEFAULT 'PENDING',
                    result_json TEXT NOT NULL DEFAULT '{}',
                    error_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT,
                    UNIQUE(execution_plan_id, semantic_fingerprint)
                    )"""
                )
            connection.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS idx_operator_recovery_resumes_due "
                "ON operator_recovery_resumes(journey_id, status, next_retry_at)"
            )
            connection.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS idx_recovery_discovery_epoch "
                "ON recovery_discovery_attempts(journey_id, recovery_epoch_id, created_at)"
            )
            connection.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS idx_recovery_execution_plans_journey "
                "ON recovery_execution_plans(journey_id, recovery_epoch_id, status, updated_at)"
            )
            connection.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS idx_recovery_execution_actions_plan "
                "ON recovery_execution_actions(execution_plan_id, sequence_no)"
            )
            connection.exec_driver_sql(
                "CREATE UNIQUE INDEX IF NOT EXISTS ux_recovery_epochs_one_open "
                "ON recovery_epochs(journey_id) WHERE status = 'OPEN'"
            )
            connection.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS idx_recovery_epochs_journey_status "
                "ON recovery_epochs(journey_id, status, generation)"
            )
            for table, columns in {
                "agent_runs": {"recovery_epoch_id": "TEXT"},
                "agent_pending_actions": {"recovery_epoch_id": "TEXT"},
                "action_approvals": {"recovery_epoch_id": "TEXT"},
                "replan_proposals": {
                    "recovery_epoch_id": "TEXT",
                    "source_agent_run_id": "TEXT",
                    "semantic_identity": "TEXT",
                    "canonical_proposal_id": "TEXT",
                },
            }.items():
                existing = {row[1] for row in connection.exec_driver_sql(f"PRAGMA table_info({table})")}
                if not existing:
                    continue
                for column, declaration in columns.items():
                    if column not in existing:
                        connection.exec_driver_sql(
                            f"ALTER TABLE {table} ADD COLUMN {column} {declaration}"
                        )
            _reconcile_agent_pending_action_status_check(connection)
            collective_columns = {
                row[1]
                for row in connection.exec_driver_sql(
                    "PRAGMA table_info(collective_authority_receipts)"
                )
            }
            if collective_columns and "recovery_epoch_id" not in collective_columns:
                connection.exec_driver_sql(
                    "ALTER TABLE collective_authority_receipts ADD COLUMN recovery_epoch_id TEXT"
                )
            if not _table_exists(connection, "collective_authority_receipts"):
                connection.exec_driver_sql(
                    """CREATE TABLE collective_authority_receipts (
                    id TEXT PRIMARY KEY,
                    journey_id TEXT NOT NULL REFERENCES journeys(id) ON DELETE CASCADE,
                    proposal_id TEXT NOT NULL REFERENCES replan_proposals(id) ON DELETE CASCADE,
                    plan_version_id TEXT NOT NULL REFERENCES plan_versions(id) ON DELETE CASCADE,
                    authority_mode TEXT NOT NULL,
                    decision_epoch TEXT NOT NULL,
                    material_world_epoch TEXT NOT NULL,
                    candidate_surface_fingerprint TEXT NOT NULL,
                    source_mission_id TEXT REFERENCES journey_missions(id) ON DELETE SET NULL,
                    source_agent_run_id TEXT REFERENCES agent_runs(id) ON DELETE SET NULL,
                    required_member_ids_json TEXT NOT NULL DEFAULT '[]',
                    response_audit_ids_json TEXT NOT NULL DEFAULT '[]',
                    principal_actor_ids_json TEXT NOT NULL DEFAULT '[]',
                    classification TEXT NOT NULL,
                    issued_at TEXT NOT NULL,
                    invalidated_at TEXT,
                    invalidation_code TEXT
                    )"""
                )
                connection.exec_driver_sql(
                    "CREATE INDEX IF NOT EXISTS idx_collective_authority_receipts_target "
                    "ON collective_authority_receipts(journey_id, proposal_id, plan_version_id)"
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
            if guardian_columns and "recovery_epoch_id" not in guardian_columns:
                connection.exec_driver_sql(
                    "ALTER TABLE guardian_runs ADD COLUMN recovery_epoch_id TEXT"
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
            _reconcile_commitment_strength_check(connection)
            _reconcile_activity_status_check(connection)

    # SQLite does not allow toggling this pragma while a transaction is open.
    # The activity-table reconciliation temporarily disables it so the table
    # can be rebuilt without touching the dependent tables' foreign-key SQL.
    # Re-enable it on the pooled connection after the schema transaction has
    # committed.
    if target_engine.dialect.name == "sqlite":
        with target_engine.connect() as connection:
            connection.execute(text("PRAGMA foreign_keys = ON"))


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


def _prepare_shared_stop_grant_slot(connection) -> None:
    """Add and backfill the cross-process uniqueness slot before schema indexes run."""

    table = connection.exec_driver_sql(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='shared_stop_coplanning_grants'"
    ).first()
    if table is None:
        return
    columns = {
        row[1]
        for row in connection.exec_driver_sql("PRAGMA table_info(shared_stop_coplanning_grants)")
    }
    added_column = "active_slot" not in columns
    if added_column:
        # Historical rows default inactive. Below, retain at most the newest
        # live grant in each natural invitation/plan/member scope.
        connection.exec_driver_sql(
            "ALTER TABLE shared_stop_coplanning_grants "
            "ADD COLUMN active_slot INTEGER NOT NULL DEFAULT 0"
        )
    index_exists = connection.exec_driver_sql(
        "SELECT 1 FROM sqlite_master WHERE type='index' "
        "AND name='ux_shared_stop_coplanning_one_unbound_active_grant'"
    ).first() is not None
    if not added_column and index_exists:
        return

    connection.exec_driver_sql("UPDATE shared_stop_coplanning_grants SET active_slot=0")
    rows = connection.exec_driver_sql(
        "SELECT id, journey_id, invitation_id, base_plan_version_id, grantee_member_id, "
        "expires_at FROM shared_stop_coplanning_grants "
        "WHERE promotion_id IS NULL AND status IN ('OFFERED', 'ACCEPTED') "
        "ORDER BY updated_at DESC, id DESC"
    ).all()
    now = datetime.now(timezone.utc)
    retained: set[tuple[str, str, str, str]] = set()
    for row in rows:
        grant_id, journey_id, invitation_id, plan_id, grantee_id, expires_at = row
        try:
            expiry = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            continue
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        if expiry <= now:
            continue
        scope = (str(journey_id), str(invitation_id), str(plan_id), str(grantee_id))
        if scope in retained:
            continue
        retained.add(scope)
        connection.exec_driver_sql(
            "UPDATE shared_stop_coplanning_grants SET active_slot=1 WHERE id=?",
            (str(grant_id),),
        )


def get_session() -> Generator[Session, None, None]:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
