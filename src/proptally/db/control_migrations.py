"""Migrations for the control-plane database.

The control plane holds the tables that cannot be recreated cheaply -- accounts,
portfolios, memberships, sessions -- so it needs the same care the portfolio databases
get, and it needs it separately: ``init_schema`` migrates a *portfolio's* database, and
never touches this one.

SQLite cannot alter a CHECK constraint in place, so widening one means rebuilding the
table: create it under a temporary name with the new constraint, copy every row, drop
the original, rename. Foreign keys are switched off for that window, because the
temporary table has no rows the referencing side knows about yet, and
``PRAGMA foreign_key_check`` verifies the result before the transaction commits.

Versions live in ``control_migrations``, deliberately not in ``schema_migrations``: that
table is a shared namespace across code lines, and keeping this one separate means a
portfolio database's version numbers can never be confused with the control plane's.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import text
from sqlmodel import SQLModel

logger = logging.getLogger(__name__)

CONTROL_MIGRATIONS_TABLE = "control_migrations"

#: The role values the current models accept, per table. A table whose live CHECK
#: constraint does not mention them all is rebuilt.
_ROLE_TABLES = {
    "tenant_memberships": ("OWNER", "MANAGER", "CPA_REVIEWER", "READ_ONLY"),
    "user_invites": ("MANAGER", "CPA_REVIEWER", "READ_ONLY"),
}


def _sql(body: str) -> str:
    return " ".join(body.split())


def _table_exists(conn, table_name: str) -> bool:
    return conn.execute(
        text("SELECT 1 FROM sqlite_master WHERE type='table' AND name=:n"), {"n": table_name}
    ).first() is not None


def _live_table_sql(conn, table_name: str) -> str:
    row = conn.execute(
        text("SELECT sql FROM sqlite_master WHERE type='table' AND name=:n"), {"n": table_name}
    ).first()
    return (row[0] or "") if row else ""


def _widen_role_constraints(engine) -> int:
    """Rebuilds any table whose role CHECK does not accept every configured role.

    Without this, adding a role to the model leaves existing databases rejecting it:
    the insert fails at the CHECK, which surfaces as a 500 in the middle of inviting
    somebody rather than as a schema error anybody can act on.
    """
    needing = []
    with engine.begin() as conn:
        for table_name, roles in _ROLE_TABLES.items():
            if not _table_exists(conn, table_name):
                continue
            live = _live_table_sql(conn, table_name)
            if not live:
                continue
            if all(f"'{role}'" in live for role in roles):
                continue
            needing.append(table_name)
    if not needing:
        return 0

    from proptally.db.schema import _rebuild_table_for_constraints

    with engine.begin() as conn:
        for table_name in needing:
            conn.execute(text(f'DROP TABLE IF EXISTS "__new_{table_name}"'))

    # The models the rebuild should produce: the live table is replaced by the shape the
    # models declare, which is how the widened constraint arrives.
    models = {table.name: table for table in SQLModel.metadata.sorted_tables}
    for table_name in needing:
        model_table = models.get(table_name)
        if model_table is None:  # pragma: no cover - a table with no model
            continue
        _rebuild_table_for_constraints(engine, model_table)
        logger.info("Control plane: rebuilt %s to widen its role constraint", table_name)
    return len(needing)


def _control_migration_table(conn) -> None:
    conn.execute(text(_sql(f"""CREATE TABLE IF NOT EXISTS {CONTROL_MIGRATIONS_TABLE} (
        version INTEGER PRIMARY KEY, description TEXT NOT NULL, applied_at TEXT NOT NULL)""")))


def _applied_versions(conn) -> set:
    return {row[0] for row in conn.execute(text(f"SELECT version FROM {CONTROL_MIGRATIONS_TABLE}"))}



def _install_lifecycle_and_membership_constraints(engine) -> int:
    """Add tenant lifecycle state and enforce one membership per user/tenant."""
    from sqlalchemy import inspect
    from sqlmodel import SQLModel

    from proptally.db.schema import _rebuild_table_for_constraints

    with engine.begin() as conn:
        columns = {column["name"] for column in inspect(conn).get_columns("tenants")}
        if "status" not in columns:
            conn.execute(text(
                "ALTER TABLE tenants ADD COLUMN status VARCHAR NOT NULL "
                "DEFAULT 'ACTIVE' CHECK (status IN "
                "('PENDING', 'PROVISIONING', 'ACTIVE', 'FAILED', 'DELETING', 'DELETED'))"
            ))
        if "deletion_token" not in columns:
            conn.execute(text("ALTER TABLE tenants ADD COLUMN deletion_token VARCHAR"))
        if "deletion_started_at" not in columns:
            conn.execute(text("ALTER TABLE tenants ADD COLUMN deletion_started_at TIMESTAMP"))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_tenants_status ON tenants (status)"
        ))
        duplicates = conn.execute(text(
            "SELECT user_id, tenant_id, COUNT(*) AS n FROM tenant_memberships "
            "GROUP BY user_id, tenant_id HAVING COUNT(*) > 1 LIMIT 1"
        )).first()
        if duplicates:
            raise RuntimeError(
                "Cannot enforce unique tenant membership: duplicate user/tenant rows "
                f"exist for {duplicates[0]!r}/{duplicates[1]!r}. Resolve duplicates "
                "before retrying the control-plane migration."
            )

    indexes = inspect(engine).get_unique_constraints("tenant_memberships")
    if not any(
        tuple(index.get("column_names") or ()) == ("user_id", "tenant_id")
        for index in indexes
    ):
        tables = {table.name: table for table in SQLModel.metadata.sorted_tables}
        _rebuild_table_for_constraints(engine, tables["tenant_memberships"])
    return 1



def _add_legacy_store_fingerprints(engine) -> int:
    """Add durable vault/photos fingerprints to resumable migration records."""
    from sqlalchemy import inspect

    with engine.begin() as conn:
        if not _table_exists(conn, "legacy_migrations"):
            return 0
        columns = {
            column["name"] for column in inspect(conn).get_columns("legacy_migrations")
        }
        if "vault_fingerprint" not in columns:
            conn.execute(text(
                "ALTER TABLE legacy_migrations ADD COLUMN vault_fingerprint "
                "VARCHAR NOT NULL DEFAULT ''"
            ))
        if "photos_fingerprint" not in columns:
            conn.execute(text(
                "ALTER TABLE legacy_migrations ADD COLUMN photos_fingerprint "
                "VARCHAR NOT NULL DEFAULT ''"
            ))
    return 1

def _add_staged_database_fingerprint(engine) -> int:
    from sqlalchemy import inspect

    with engine.begin() as conn:
        if not _table_exists(conn, "legacy_migrations"):
            return 0
        columns = {
            column["name"] for column in inspect(conn).get_columns("legacy_migrations")
        }
        if "staged_database_fingerprint" not in columns:
            conn.execute(text(
                "ALTER TABLE legacy_migrations ADD COLUMN staged_database_fingerprint "
                "VARCHAR NOT NULL DEFAULT ''"
            ))
    return 1

def _add_deletion_owner_metadata(engine) -> int:
    from sqlalchemy import inspect

    with engine.begin() as conn:
        columns = {column["name"] for column in inspect(conn).get_columns("tenants")}
        if "deletion_process_id" not in columns:
            conn.execute(text("ALTER TABLE tenants ADD COLUMN deletion_process_id INTEGER"))
        if "deletion_host_name" not in columns:
            conn.execute(text("ALTER TABLE tenants ADD COLUMN deletion_host_name VARCHAR"))
    return 1
CONTROL_MIGRATIONS = (
    (1, "widen role constraints to include MANAGER", _widen_role_constraints),
    (2, "tenant lifecycle states and unique memberships", _install_lifecycle_and_membership_constraints),
    (3, "legacy vault and photo migration fingerprints", _add_legacy_store_fingerprints),
    (4, "staged tenant database fingerprint", _add_staged_database_fingerprint),
    (5, "deletion owner process metadata", _add_deletion_owner_metadata),
)


def run_control_migrations(engine) -> list:
    """Applies every pending control-plane migration, in order."""
    applied_log = []
    with engine.begin() as conn:
        _control_migration_table(conn)

    for version, description, step in CONTROL_MIGRATIONS:
        with engine.begin() as conn:
            if version in _applied_versions(conn):
                continue
        step(engine)
        with engine.begin() as conn:
            conn.execute(
                text(f"INSERT INTO {CONTROL_MIGRATIONS_TABLE} (version, description, applied_at) "
                     "VALUES (:v, :d, :t)"),
                {"v": version, "d": description, "t": datetime.now(timezone.utc).isoformat()},
            )
        applied_log.append((version, description))
    return applied_log
