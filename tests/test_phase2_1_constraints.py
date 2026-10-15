"""Phase 2.1 §6/§4: schema integrity, CHECK enforcement and upgrade parity.

The ledger services validate their inputs before writing, so a removed
``CheckConstraint`` would not surface through them. The first tests bypass the ORM
with raw SQL and assert the constraints exist in the SQLite DDL itself.

The last test proves an upgrade path: a database created before the Phase 2.1
columns and CHECK constraints must reach full parity with a fresh install
without losing rows or foreign-key relationships.
"""

import pytest
from sqlalchemy import CheckConstraint, ForeignKeyConstraint, MetaData, text
from sqlalchemy.schema import CreateIndex, CreateTable
from sqlmodel import SQLModel

from proptally.db.connection import get_engine
from proptally.db.schema import SQL_TRIGGERS, _tables_missing_constraints, init_schema

LEDGER_ACCOUNTS = (
    "INSERT INTO ledger_accounts (code, name, account_type, normal_balance) "
    "VALUES (1010, 'Checking', 'ASSET', 'DEBIT'), (5040, 'Repairs', 'EXPENSE', 'DEBIT')"
)

JOURNAL_ENTRY = (
    "INSERT INTO journal_entries (id, entry_date, entry_type, status, description, created_at) "
    "VALUES (:id, '2026-01-01', :entry_type, :status, 'constraint probe', '2026-01-01 00:00:00')"
)

JOURNAL_LINE = (
    "INSERT INTO journal_entry_lines "
    "(id, journal_entry_id, ledger_account_code, debit_cents, credit_cents) "
    "VALUES (:id, 'je_constraint_probe', 5040, :debit, :credit)"
)

TRANSACTION_SPLIT = (
    "INSERT INTO transaction_splits "
    "(id, transaction_id, category, amount_cents, tax_review_status, created_at) "
    "VALUES (:id, 'txn_constraint_probe', 'REPAIRS', :amount, 'UNCLASSIFIED', '2026-01-01 00:00:00')"
)


@pytest.fixture(name="probe_db")
def probe_db_fixture(engine):
    """Creates the minimum parent rows required to attempt child inserts."""
    with engine.begin() as conn:
        conn.execute(text(LEDGER_ACCOUNTS))
        conn.execute(text(JOURNAL_ENTRY), {"id": "je_constraint_probe", "entry_type": "EXPENSE_POST", "status": "DRAFT"})
    return engine


def _execute(engine, sql, params):
    with engine.begin() as conn:
        conn.execute(text(sql), params)


def test_journal_line_must_be_one_sided_and_non_negative(probe_db):
    for label, debit, credit in [
        ("both sides populated", 100, 100),
        ("neither side populated", 0, 0),
        ("negative debit", -5, 0),
    ]:
        with pytest.raises(Exception, match="check_journal_line_one_sided"):
            _execute(probe_db, JOURNAL_LINE, {"id": f"l_{label}", "debit": debit, "credit": credit})


def test_journal_line_with_one_positive_side_is_accepted(probe_db):
    _execute(probe_db, JOURNAL_LINE, {"id": "l_ok", "debit": 100, "credit": 0})


def test_journal_entry_status_must_be_a_known_state(probe_db):
    with pytest.raises(Exception, match="check_journal_entry_status"):
        _execute(
            probe_db,
            JOURNAL_ENTRY,
            {"id": "je_bad_status", "entry_type": "REVERSAL", "status": "NOT_A_STATUS"},
        )


@pytest.mark.parametrize("amount_cents", [0, -100])
def test_split_amount_must_be_strictly_positive(probe_db, amount_cents):
    with pytest.raises(Exception, match="check_split_amount_positive"):
        _execute(probe_db, TRANSACTION_SPLIT, {"id": f"s_{amount_cents}", "amount": amount_cents})


# --- Upgrade parity: a pre-Phase-2.1 database must end up identical in
# --- integrity guarantees to a freshly created one. ------------------------

# Columns that did not exist when the Phase 1 schema shipped, per table.
PHASE2_COLUMNS = {
    "transaction_processing_states": ("posting_status", "journal_entry_id"),
    "transactions": (
        "requires_client_confirmation",
        "requires_cpa_review",
        "counter_account_id",
    ),
    "journal_entries": ("source_transaction_id",),
    "financial_accounts": ("mixed_use_status",),
}


def _build_phase1_database(db_url):
    """Recreates the schema as it was before the Phase 2.1 columns and constraints.

    Tables are copied from the current models with every CHECK constraint and
    every Phase 2.1 column removed, which is exactly what a pre-Phase-2.1
    database looks like.
    """
    engine = get_engine(db_url)
    staging = MetaData()
    for table in SQLModel.metadata.sorted_tables:
        legacy = table.to_metadata(staging)
        for constraint in list(legacy.constraints):
            if isinstance(constraint, CheckConstraint):
                legacy.constraints.discard(constraint)
        for column_name in PHASE2_COLUMNS.get(legacy.name, ()):
            column = legacy.c[column_name]
            for index in list(legacy.indexes):
                if index.columns.contains_column(column):
                    legacy.indexes.discard(index)
            for constraint in list(legacy.constraints):
                if isinstance(constraint, ForeignKeyConstraint) and column_name in [
                    c.name for c in constraint.columns
                ]:
                    legacy.constraints.discard(constraint)
            legacy._columns.remove(column)

    with engine.begin() as conn:
        for table in staging.sorted_tables:
            conn.exec_driver_sql(str(CreateTable(table).compile(dialect=engine.dialect)))
            for index in table.indexes:
                conn.exec_driver_sql(str(CreateIndex(index).compile(dialect=engine.dialect)))
        # Phase 1 already shipped the immutability triggers, and they reference
        # the tables the migration rebuilds.
        for trigger_sql in SQL_TRIGGERS:
            conn.execute(text(trigger_sql.strip()))
    return engine


def _seed_phase1_rows(engine):
    """A small but foreign-key-consistent snapshot using only Phase 1 columns."""
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "INSERT INTO financial_institutions (id, name, routing_number, website, created_at) "
            "VALUES ('inst_legacy', 'Legacy Bank', NULL, NULL, '2026-01-01')"
        )
        conn.exec_driver_sql(
            "INSERT INTO financial_accounts (id, institution_id, account_name, account_type, "
            "account_number_masked, currency, is_active, created_at) "
            "VALUES ('acct_legacy', 'inst_legacy', 'Legacy Checking', 'CHECKING', '...9999', 'USD', 1, '2026-01-01')"
        )
        conn.exec_driver_sql(
            "INSERT INTO documents (id, file_sha256, original_filename, storage_path, file_size_bytes, "
            "mime_type, uploaded_at) VALUES ('doc_legacy', 'sha_legacy', 'f.pdf', '/p', 1, 'application/pdf', '2026-01-01')"
        )
        conn.exec_driver_sql(
            "INSERT INTO statements (id, document_id, financial_account_id, statement_type, "
            "active_parser_name, active_parser_version, statement_date, ingestion_status, created_at) "
            "VALUES ('stmt_legacy', 'doc_legacy', 'acct_legacy', 'CHECKING', 'P', '1', '2026-03-31', 'COMMITTED', '2026-01-01')"
        )
        conn.exec_driver_sql(LEDGER_ACCOUNTS)
        conn.exec_driver_sql(
            "INSERT INTO transactions (id, statement_id, financial_account_id, transaction_date, "
            "amount_cents, direction, raw_description, created_at) "
            "VALUES ('txn_legacy', 'stmt_legacy', 'acct_legacy', '2026-03-01', -5000, 'OUTFLOW', 'Legacy repair', '2026-01-01')"
        )
        conn.exec_driver_sql(
            "INSERT INTO transaction_splits (id, transaction_id, category, amount_cents, tax_review_status, created_at) "
            "VALUES ('split_legacy', 'txn_legacy', 'REPAIRS', 5000, 'CLIENT_CONFIRMED', '2026-01-01')"
        )
        conn.exec_driver_sql(
            "INSERT INTO transaction_processing_states (transaction_id, allocation_status, allocated_cents, "
            "residual_cents, is_reconciled, updated_at) "
            "VALUES ('txn_legacy', 'FULLY_ALLOCATED', 5000, 0, 1, '2026-01-01')"
        )
        conn.exec_driver_sql(
            "INSERT INTO journal_entries (id, entry_date, entry_type, status, idempotency_key, reversal_of_id, "
            "description, posted_at, created_at) "
            "VALUES ('je_legacy', '2026-03-01', 'EXPENSE_POST', 'DRAFT', 'LEGACY:1', NULL, 'Legacy entry', NULL, '2026-01-01')"
        )
        conn.exec_driver_sql(
            "INSERT INTO journal_entry_lines (id, journal_entry_id, ledger_account_code, property_id, "
            "debit_cents, credit_cents, description) "
            "VALUES ('line_legacy', 'je_legacy', 5040, NULL, 5000, 0, 'Legacy line')"
        )


def _columns(conn, table):
    return {row[1] for row in conn.execute(text(f"PRAGMA table_info({table})"))}


def test_fresh_database_has_every_declared_check_constraint(engine):
    with engine.connect() as conn:
        assert _tables_missing_constraints(conn) == []
        triggers = conn.execute(
            text("SELECT name FROM sqlite_master WHERE type = 'trigger'")
        ).fetchall()
    assert len(triggers) == len(SQL_TRIGGERS)


def test_upgrade_from_phase1_reaches_parity_without_data_loss(tmp_path):
    """Columns, constraints and indexes all arrive; rows and FKs survive."""
    engine = _build_phase1_database(f"sqlite:///{tmp_path / 'phase1.db'}")
    _seed_phase1_rows(engine)

    # Pre-conditions: this really is a Phase 1 database.
    with engine.connect() as conn:
        assert "posting_status" not in _columns(conn, "transaction_processing_states")
        assert "requires_client_confirmation" not in _columns(conn, "transactions")
        assert "source_transaction_id" not in _columns(conn, "journal_entries")
        assert _tables_missing_constraints(conn), "expected constraints to be missing before upgrade"
        assert conn.execute(
            text("SELECT COUNT(*) FROM sqlite_master WHERE type = 'trigger'")
        ).scalar() == len(SQL_TRIGGERS)  # the immutability triggers are reinstalled on rebuild

    init_schema(engine)
    init_schema(engine)  # repeatable / idempotent

    with engine.begin() as conn:
        # Columns installed (migration 1)
        assert {"posting_status", "journal_entry_id"} <= _columns(conn, "transaction_processing_states")
        assert {"requires_client_confirmation", "requires_cpa_review"} <= _columns(conn, "transactions")
        assert "source_transaction_id" in _columns(conn, "journal_entries")

        # Constraints installed (migration 2)
        assert _tables_missing_constraints(conn) == []

        # Indexes installed (migration 3)
        indexes = {
            row[0] for row in conn.execute(text("SELECT name FROM sqlite_master WHERE type = 'index'"))
        }
        assert "ix_journal_entries_source_transaction_id" in indexes
        assert "ix_journal_entries_idempotency_key" in indexes

        # Triggers reinstalled
        triggers = {row[0] for row in conn.execute(text("SELECT name FROM sqlite_master WHERE type = 'trigger'"))}
        assert "prevent_posted_entry_update" in triggers
        assert len(triggers) == len(SQL_TRIGGERS)

        # Rows preserved, with sane backfill defaults
        assert conn.execute(text("SELECT COUNT(*) FROM transactions")).scalar() == 1
        assert conn.execute(text("SELECT COUNT(*) FROM journal_entry_lines")).scalar() == 1
        assert conn.execute(text("SELECT COUNT(*) FROM transaction_splits")).scalar() == 1
        state = conn.execute(
            text("SELECT allocation_status, allocated_cents, posting_status FROM transaction_processing_states")
        ).one()
        assert state[0] == "FULLY_ALLOCATED"
        assert state[1] == 5000
        assert state[2] == "UNPOSTED"  # backfilled default for a pre-existing row
        flags = conn.execute(
            text("SELECT requires_client_confirmation, requires_cpa_review FROM transactions")
        ).one()
        assert (flags[0], flags[1]) == (0, 0)
        assert conn.execute(text("SELECT description FROM journal_entries")).scalar() == "Legacy entry"

        # Foreign keys intact
        conn.exec_driver_sql(
            "INSERT INTO financial_accounts (id, institution_id, account_name, account_type, "
            "account_number_masked, currency, is_active, mixed_use_status, created_at) "
            "VALUES ('acct_after', 'inst_legacy', 'After', 'CHECKING', '...1', 'USD', 1, "
            "'UNCLASSIFIED', '2026-01-01')"
        )
        with pytest.raises(Exception):
            conn.exec_driver_sql(
                "INSERT INTO financial_accounts (id, institution_id, account_name, account_type, "
                "account_number_masked, currency, is_active, mixed_use_status, created_at) "
                "VALUES ('acct_orphan', 'inst_missing', 'Orphan', 'CHECKING', '...2', 'USD', 1, "
                "'UNCLASSIFIED', '2026-01-01')"
            )

    # Enforcement parity: a fresh database and the upgraded one now behave alike.
    with engine.connect() as conn:
        assert conn.execute(text("PRAGMA foreign_key_check")).fetchall() == []

    # Schema parity: the upgraded database must expose exactly the columns, CHECK
    # constraints and indexes a freshly created one has.
    fresh = get_engine(f"sqlite:///{tmp_path / 'fresh.db'}")
    init_schema(fresh)
    assert _schema_signature(engine) == _schema_signature(fresh)

    with pytest.raises(Exception, match="check_journal_line_one_sided"):
        with engine.begin() as conn:
            conn.exec_driver_sql(
                "INSERT INTO journal_entry_lines (id, journal_entry_id, ledger_account_code, "
                "debit_cents, credit_cents) VALUES ('bad_line', 'je_legacy', 5040, 10, 10)"
            )
    with pytest.raises(Exception, match="check_transaction_direction"):
        with engine.begin() as conn:
            conn.exec_driver_sql(
                "INSERT INTO transactions (id, statement_id, financial_account_id, transaction_date, "
                "amount_cents, direction, raw_description, requires_client_confirmation, "
                "requires_cpa_review, created_at) "
                "VALUES ('txn_bad_dir', 'stmt_legacy', 'acct_legacy', '2026-03-01', 1, 'SIDEWAYS', "
                "'bad', 0, 0, '2026-01-01')"
            )


def _schema_signature(engine):
    """Per-table columns, declared CHECK constraints and indexes."""
    with engine.connect() as conn:
        tables = [
            row[0]
            for row in conn.execute(text(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
                "AND name != 'schema_migrations' ORDER BY name"
            ))
        ]
        signature = {}
        for table in tables:
            columns = tuple(
                sorted(row[1] for row in conn.execute(text(f"PRAGMA table_info({table})")))
            )
            live_sql = conn.execute(
                text("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = :t"),
                {"t": table},
            ).scalar() or ""
            checks = tuple(sorted(
                name for name in _CHECK_NAMES if name in live_sql
            ))
            indexes = tuple(sorted(
                row[0]
                for row in conn.execute(text(
                    "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = :t "
                    "AND name NOT LIKE 'sqlite_%'"
                ), {"t": table})
            ))
            signature[table] = (columns, checks, indexes)
    return signature


_CHECK_NAMES = tuple(sorted(
    constraint.name
    for table in SQLModel.metadata.sorted_tables
    for constraint in table.constraints
    if isinstance(constraint, CheckConstraint) and constraint.name
))
