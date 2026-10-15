"""Schema initialization, versioned migrations and DDL triggers for PropTally."""

from datetime import datetime, timezone

from sqlalchemy import CheckConstraint, MetaData, text
from sqlalchemy.schema import CreateIndex, CreateTable
from sqlmodel import Session, SQLModel

# Import for the registration side effect: create_all() and the migrations below
# read SQLModel.metadata, which is empty unless the models have been imported.
# Relying on another module to import them first silently creates no tables.
from proptally.db import models as _models  # noqa: F401

SQL_TRIGGERS = [
    # 1. Prevent inserting lines into a POSTED journal entry
    """
    CREATE TRIGGER IF NOT EXISTS prevent_posted_lines_insert
    BEFORE INSERT ON journal_entry_lines
    FOR EACH ROW
    WHEN (SELECT status FROM journal_entries WHERE id = NEW.journal_entry_id) = 'POSTED'
    BEGIN
        SELECT RAISE(ABORT, 'ImmutableLedgerError: Cannot insert lines into a POSTED journal entry.');
    END;
    """,

    # 2. Prevent updating lines of a POSTED journal entry
    """
    CREATE TRIGGER IF NOT EXISTS prevent_posted_lines_update
    BEFORE UPDATE ON journal_entry_lines
    FOR EACH ROW
    WHEN (SELECT status FROM journal_entries WHERE id = OLD.journal_entry_id) = 'POSTED'
    BEGIN
        SELECT RAISE(ABORT, 'ImmutableLedgerError: Cannot update lines of a POSTED journal entry.');
    END;
    """,

    # 3. Prevent deleting lines of a POSTED journal entry
    """
    CREATE TRIGGER IF NOT EXISTS prevent_posted_lines_delete
    BEFORE DELETE ON journal_entry_lines
    FOR EACH ROW
    WHEN (SELECT status FROM journal_entries WHERE id = OLD.journal_entry_id) = 'POSTED'
    BEGIN
        SELECT RAISE(ABORT, 'ImmutableLedgerError: Cannot delete lines of a POSTED journal entry.');
    END;
    """,

    # 4. Prevent updating header of a POSTED journal entry
    """
    CREATE TRIGGER IF NOT EXISTS prevent_posted_entry_update
    BEFORE UPDATE ON journal_entries
    FOR EACH ROW
    WHEN OLD.status = 'POSTED'
    BEGIN
        SELECT RAISE(ABORT, 'ImmutableLedgerError: Cannot update a POSTED journal entry.');
    END;
    """,

    # 5. Prevent deleting a POSTED journal entry
    """
    CREATE TRIGGER IF NOT EXISTS prevent_posted_entry_delete
    BEFORE DELETE ON journal_entries
    FOR EACH ROW
    WHEN OLD.status = 'POSTED'
    BEGIN
        SELECT RAISE(ABORT, 'ImmutableLedgerError: Cannot delete a POSTED journal entry.');
    END;
    """,

    # 6. Authorization evidence is append-only: the record of who authorized what
    #    must not be editable or removable after the fact.
    """
    CREATE TRIGGER IF NOT EXISTS prevent_posting_authorization_update
    BEFORE UPDATE ON posting_authorizations
    FOR EACH ROW
    BEGIN
        SELECT RAISE(ABORT, 'ImmutableLedgerError: Posting authorization records are immutable.');
    END;
    """,

    # 7. Prevent deleting an authorization record.
    """
    CREATE TRIGGER IF NOT EXISTS prevent_posting_authorization_delete
    BEFORE DELETE ON posting_authorizations
    FOR EACH ROW
    BEGIN
        SELECT RAISE(ABORT, 'ImmutableLedgerError: Posting authorization records are immutable.');
    END;
    """,
]


def _sql(body: str) -> str:
    """Collapses an indented SQL fragment onto a single line."""
    return " ".join(body.split())


# --- Versioned migrations -------------------------------------------------
#
# SQLModel.metadata.create_all() only creates *missing tables*; it never alters
# an existing one. A database created before the current models therefore lacks
# both new columns and new CHECK constraints. Each migration below is idempotent
# and recorded in `schema_migrations`, so an upgrade is repeatable and a fresh
# database simply records them as already satisfied.

# 1. Columns added after the initial schema shipped.
COLUMN_MIGRATIONS = [
    (
        "transaction_processing_states",
        "posting_status",
        _sql("ALTER TABLE transaction_processing_states ADD COLUMN "
             "posting_status VARCHAR NOT NULL DEFAULT 'UNPOSTED'"),
    ),
    (
        "transaction_processing_states",
        "journal_entry_id",
        _sql("ALTER TABLE transaction_processing_states ADD COLUMN "
             "journal_entry_id VARCHAR REFERENCES journal_entries (id)"),
    ),
    (
        "transactions",
        "requires_client_confirmation",
        _sql("ALTER TABLE transactions ADD COLUMN "
             "requires_client_confirmation BOOLEAN NOT NULL DEFAULT 0"),
    ),
    (
        "transactions",
        "requires_cpa_review",
        _sql("ALTER TABLE transactions ADD COLUMN "
             "requires_cpa_review BOOLEAN NOT NULL DEFAULT 0"),
    ),
    (
        "journal_entries",
        "source_transaction_id",
        _sql("ALTER TABLE journal_entries ADD COLUMN "
             "source_transaction_id VARCHAR REFERENCES transactions (id)"),
    ),
    (
        "financial_accounts",
        "mixed_use_status",
        _sql("ALTER TABLE financial_accounts ADD COLUMN "
             "mixed_use_status VARCHAR NOT NULL DEFAULT 'UNCLASSIFIED'"),
    ),
    (
        "transactions",
        "counter_account_id",
        _sql("ALTER TABLE transactions ADD COLUMN "
             "counter_account_id VARCHAR REFERENCES financial_accounts (id)"),
    ),
    (
        "properties",
        "description",
        _sql("ALTER TABLE properties ADD COLUMN description TEXT"),
    ),
    (
        "properties",
        "google_maps_url",
        _sql("ALTER TABLE properties ADD COLUMN google_maps_url TEXT"),
    ),
    (
        "properties",
        "cover_photo_id",
        _sql("ALTER TABLE properties ADD COLUMN cover_photo_id VARCHAR"),
    ),
    (
        "documents",
        "document_type",
        _sql("ALTER TABLE documents ADD COLUMN document_type VARCHAR"),
    ),
    (
        "documents",
        "property_id",
        _sql("ALTER TABLE documents ADD COLUMN property_id VARCHAR REFERENCES properties (id)"),
    ),
    (
        "leases",
        "document_id",
        _sql("ALTER TABLE leases ADD COLUMN document_id VARCHAR REFERENCES documents (id)"),
    ),
]


def _table_exists(conn, table_name: str) -> bool:
    row = conn.execute(
        text("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = :name"),
        {"name": table_name},
    ).first()
    return row is not None


def _column_names(conn, table_name: str) -> set:
    return {row[1] for row in conn.execute(text(f"PRAGMA table_info({table_name})"))}


def _live_table_sql(conn, table_name: str) -> str:
    row = conn.execute(
        text("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = :name"),
        {"name": table_name},
    ).first()
    return row[0] or "" if row else ""


def _migrate_columns(conn) -> None:
    """Adds columns that post-date an already-created database."""
    for table_name, column_name, ddl in COLUMN_MIGRATIONS:
        if not _table_exists(conn, table_name):
            continue
        if column_name in _column_names(conn, table_name):
            continue
        conn.execute(text(ddl))


def _migrate_indexes(conn) -> None:
    """Creates any index declared on the models that is missing from the database."""
    for table in SQLModel.metadata.sorted_tables:
        if not _table_exists(conn, table.name):
            continue
        live = {
            row[0]
            for row in conn.execute(
                text("SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = :t"),
                {"t": table.name},
            )
        }
        for index in table.indexes:
            if index.name not in live:
                conn.execute(text(f'CREATE INDEX IF NOT EXISTS "{index.name}" ON "{index.table.name}" '
                                  f'({", ".join(c.name for c in index.columns)})'))


def _tables_missing_constraints(conn) -> list:
    """Model tables whose live DDL is missing a declared CHECK constraint."""
    needing = []
    for table in SQLModel.metadata.sorted_tables:
        if not _table_exists(conn, table.name):
            continue
        declared = [c for c in table.constraints if isinstance(c, CheckConstraint) and c.name]
        if not declared:
            continue
        live_sql = _live_table_sql(conn, table.name)
        if any(c.name not in live_sql for c in declared):
            needing.append(table)
    return needing


def _rebuild_table_for_constraints(engine, table) -> None:
    """Rebuilds a SQLite table so its declared CHECK constraints exist.

    SQLite cannot add a CHECK constraint via ALTER TABLE, so the documented
    rebuild procedure is used: create the constrained table under a temporary
    name, copy every row, drop the original, rename, then recreate indexes.
    Rows and foreign-key relationships survive; ``PRAGMA foreign_key_check``
    verifies this before the transaction is committed.
    """
    temp_name = f"{table.name}__rebuild"
    # The rebuild DDL is compiled from a staged copy of the metadata. Every other
    # table must be present, or foreign keys cannot resolve their targets.
    staging = MetaData()
    for other in SQLModel.metadata.sorted_tables:
        if other.name != table.name:
            other.to_metadata(staging)
    rebuilt = table.to_metadata(staging, name=temp_name)
    create_sql = str(CreateTable(rebuilt).compile(dialect=engine.dialect))
    columns = ", ".join(f'"{c.name}"' for c in table.columns)
    copy_sql = f'INSERT INTO "{temp_name}" ({columns}) SELECT {columns} FROM "{table.name}"'
    # Indexes are created after the rename, so they must be compiled against the
    # original table name (the staged copies are named for the temporary table).
    index_sql = [str(CreateIndex(ix).compile(dialect=engine.dialect)) for ix in table.indexes]

    # AUTOCOMMIT gives explicit control: PRAGMA foreign_keys is a no-op inside a
    # transaction, and it must be toggled outside one.
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.exec_driver_sql("PRAGMA foreign_keys = OFF")
        conn.exec_driver_sql("BEGIN")
        try:
            conn.exec_driver_sql(create_sql)
            conn.exec_driver_sql(copy_sql)
            conn.exec_driver_sql(f'DROP TABLE "{table.name}"')
            conn.exec_driver_sql(f'ALTER TABLE "{temp_name}" RENAME TO "{table.name}"')
            for sql in index_sql:
                conn.exec_driver_sql(sql)
            violations = conn.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise RuntimeError(
                    f"Refusing to commit rebuild of {table.name}: "
                    f"foreign key violations {violations!r}"
                )
            conn.exec_driver_sql("COMMIT")
        except Exception:
            conn.exec_driver_sql("ROLLBACK")
            raise
        finally:
            conn.exec_driver_sql("PRAGMA foreign_keys = ON")


def _migrate_constraints(engine) -> int:
    """Installs missing CHECK constraints by rebuilding affected tables.

    Triggers are removed first: the immutability triggers on journal_entry_lines
    reference journal_entries, and SQLite refuses the rename while any surviving
    trigger points at a table that is momentarily absent. They are reinstalled
    immediately afterwards.
    """
    with engine.connect() as conn:
        needing = _tables_missing_constraints(conn)
    if not needing:
        return 0

    with engine.begin() as conn:
        for row in conn.execute(text("SELECT name FROM sqlite_master WHERE type = 'trigger'")).all():
            conn.execute(text(f'DROP TRIGGER IF EXISTS "{row[0]}"'))

    for table in needing:
        _rebuild_table_for_constraints(engine, table)

    with engine.begin() as conn:
        for trigger_sql in SQL_TRIGGERS:
            conn.execute(text(trigger_sql.strip()))
    return len(needing)


def _migrate_vendor_rules(engine) -> int:
    """Creates vendor_rules table if not present and seeds standard rules."""
    from proptally.domain.transactions.suggest import seed_vendor_rules
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS vendor_rules (
                id VARCHAR PRIMARY KEY,
                pattern VARCHAR NOT NULL,
                vendor_name VARCHAR NOT NULL,
                service_type VARCHAR NOT NULL,
                default_category VARCHAR NOT NULL DEFAULT 'PERSONAL',
                default_property_id VARCHAR,
                confidence FLOAT NOT NULL DEFAULT 0.90,
                requires_client_confirmation BOOLEAN NOT NULL DEFAULT 0,
                requires_cpa_review BOOLEAN NOT NULL DEFAULT 0,
                is_active BOOLEAN NOT NULL DEFAULT 1,
                notes TEXT,
                created_at TIMESTAMP NOT NULL
            )
        """))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_vendor_rules_pattern ON vendor_rules (pattern)"))
    with Session(engine) as session:
        seed_vendor_rules(session)
    return 1


SCHEMA_MIGRATIONS = (
    (1, "add post-initial-schema columns", lambda engine: _apply(engine, _migrate_columns)),
    (2, "install missing CHECK constraints", _migrate_constraints),
    (3, "create missing indexes", lambda engine: _apply(engine, _migrate_indexes)),
    (4, "add property description, maps url, and photo columns", lambda engine: _apply(engine, _migrate_columns)),
    (5, "create vendor_rules table and seed standard rules", _migrate_vendor_rules),
)


def _apply(engine, step) -> int:
    with engine.begin() as conn:
        step(conn)
    return 0


def _ensure_migration_table(conn) -> None:
    conn.execute(text(
        _sql("""CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            description TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )""")
    ))


def _applied_versions(conn) -> set:
    return {row[0] for row in conn.execute(text("SELECT version FROM schema_migrations"))}


def run_migrations(engine) -> list:
    """Applies every pending migration in order, recording each as it lands."""
    applied_log = []
    with engine.begin() as conn:
        _ensure_migration_table(conn)

    for version, description, step in SCHEMA_MIGRATIONS:
        with engine.begin() as conn:
            already = _applied_versions(conn)
        if version in already:
            continue
        step(engine)
        with engine.begin() as conn:
            conn.execute(
                text("INSERT INTO schema_migrations (version, description, applied_at) "
                     "VALUES (:v, :d, :t)"),
                {"v": version, "d": description, "t": datetime.now(timezone.utc).isoformat()},
            )
        applied_log.append((version, description))
    return applied_log


def init_schema(engine) -> None:
    """Creates all relational tables, migrates, and installs immutability triggers."""
    # Create tables that do not exist yet. This never alters existing tables.
    SQLModel.metadata.create_all(engine)

    # Bring a pre-existing database up to the current columns, constraints and
    # indexes before the triggers are (re)installed: rebuilding a table drops any
    # triggers attached to it.
    run_migrations(engine)

    with engine.begin() as conn:
        for trigger_sql in SQL_TRIGGERS:
            conn.execute(text(trigger_sql.strip()))
