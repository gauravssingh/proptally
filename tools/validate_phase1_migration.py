"""Validate a schema migration against a real Phase 1 database.

Copies the supplied database (including its WAL sidecars) so the original is
never touched, migrates the copy, and prints before/after evidence:

  * table list, per-table columns and declared CHECK constraints
  * row counts for every table
  * primary-key / foreign-key integrity
  * transaction, split, journal-entry and reversal records
  * schema parity against a freshly created database
  * an application startup smoke test against the migrated copy

Usage:
    PYTHONPATH=src python tools/validate_phase1_migration.py \
        --source /path/to/phase1.db \
        --out /tmp/migration_validation

The source is expected to be a database created by Phase 1 code, or any real
single-portfolio database from before the multi-tenant change. Reproduce a Phase 1
database with that commit in a worktree, and populate it however you like — the
validator only reads it:

    git worktree add /tmp/p1worktree <phase1-commit>
    cd /tmp/p1worktree
    PROPTALLY_DB_PATH=/tmp/phase1_real.db PYTHONPATH=src \
        /path/to/repo/.venv/bin/python -c "
from proptally.db.connection import get_engine
from proptally.db.schema import init_schema
from proptally.domain.accounting.coa import ensure_entity_ledger_accounts
from sqlmodel import Session
e = get_engine(); init_schema(e)
with Session(e) as s:
    ensure_entity_ledger_accounts(s)"

The application ships no sample portfolio: a portfolio is built by its owner, so
populate a validation database through the application or from your own records.
"""

import argparse
import json
import shutil
import sys
from pathlib import Path

from sqlalchemy import CheckConstraint, text
from sqlmodel import SQLModel

from proptally.db.connection import get_engine
from proptally.db.schema import _tables_missing_constraints, init_schema

WAL_SIDECARS = ("", "-wal", "-shm")

# Columns and tables introduced after Phase 1, used for the parity check.
KEY_TABLES = (
    "properties",
    "financial_accounts",
    "loan_accounts",
    "transactions",
    "transaction_splits",
    "transaction_processing_states",
    "journal_entries",
    "journal_entry_lines",
    "reconciliation_allocations",
    "audit_log",
)


def copy_database(source: Path, destination: Path) -> None:
    for suffix in WAL_SIDECARS:
        src = Path(f"{source}{suffix}")
        dst = Path(f"{destination}{suffix}")
        if dst.exists():
            dst.unlink()
        if src.exists():
            shutil.copy(src, dst)


def _table_names(conn) -> list:
    return [
        row[0]
        for row in conn.execute(text(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ))
    ]


def _row_counts(conn, tables) -> dict:
    counts = {}
    for table in tables:
        try:
            counts[table] = conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar()
        except Exception as exc:  # noqa: BLE001
            counts[table] = f"ERROR: {exc}"
    return counts


def _check_constraints(conn) -> dict:
    found = {}
    for table in _table_names(conn):
        sql = conn.execute(
            text("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = :t"),
            {"t": table},
        ).scalar() or ""
        names = [
            line.split("CONSTRAINT", 1)[1].split()[0]
            for line in sql.splitlines()
            if "CONSTRAINT" in line and "CHECK" in line
        ]
        if names:
            found[table] = sorted(names)
    return found


def _declared_check_names() -> list:
    return sorted(
        constraint.name
        for table in SQLModel.metadata.sorted_tables
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint) and constraint.name
    )


def _columns(conn, table) -> list:
    return sorted(row[1] for row in conn.execute(text(f"PRAGMA table_info({table})")))


def _sample_records(conn) -> dict:
    """A few concrete records, so data survival is visible not just counted."""
    records = {}
    for table, columns, order in [
        ("transactions", "id, transaction_date, amount_cents, raw_description", "id"),
        ("journal_entries", "id, entry_date, entry_type, status, reversal_of_id", "id"),
        ("journal_entry_lines",
         "id, journal_entry_id, ledger_account_code, debit_cents, credit_cents", "id"),
        ("transaction_splits", "id, transaction_id, property_id, category, amount_cents", "id"),
        ("reconciliation_allocations",
         "id, session_id, source_transaction_id, allocated_cents, status", "id"),
    ]:
        try:
            rows = conn.execute(
                text(f"SELECT {columns} FROM {table} ORDER BY {order}")
            ).fetchall()
            records[table] = [list(r) for r in rows]
        except Exception as exc:  # noqa: BLE001
            records[table] = f"ERROR: {exc}"
    return records


def capture(engine) -> dict:
    with engine.connect() as conn:
        tables = _table_names(conn)
        return {
            "tables": tables,
            "row_counts": _row_counts(conn, tables),
            "check_constraints": _check_constraints(conn),
            "columns": {t: _columns(conn, t) for t in KEY_TABLES if t in tables},
            "integrity_check": conn.execute(text("PRAGMA integrity_check")).scalar(),
            "foreign_key_violations": [list(r) for r in conn.execute(text("PRAGMA foreign_key_check"))],
            "reversal_links": conn.execute(
                text("SELECT COUNT(*) FROM journal_entries WHERE reversal_of_id IS NOT NULL")
            ).scalar(),
            "records": _sample_records(conn),
        }


def schema_signature(engine) -> dict:
    with engine.connect() as conn:
        return {
            table: (
                _columns(conn, table),
                sorted(_check_constraints(conn).get(table, [])),
            )
            for table in _table_names(conn)
            if table != "schema_migrations"
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, help="Phase 1 database to migrate")
    parser.add_argument("--out", required=True, help="directory for the copy and report")
    parser.add_argument("--skip-smoke", action="store_true", help="skip the app startup check")
    args = parser.parse_args()

    source = Path(args.source)
    if not source.exists():
        print(f"source database not found: {source}", file=sys.stderr)
        return 2

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    copy_path = out / "migrated.db"

    print(f"source   : {source} (left untouched)")
    print(f"working  : {copy_path}")

    copy_database(source, copy_path)
    engine = get_engine(f"sqlite:///{copy_path}")

    before = capture(engine)
    with engine.connect() as conn:
        missing_before = [t.name for t in _tables_missing_constraints(conn)]

    print("\n--- BEFORE ---")
    print(f"tables                : {len(before['tables'])}")
    print(f"tables with CHECK     : {len(before['check_constraints'])}")
    print(f"tables needing rebuild: {missing_before}")
    print(f"integrity_check       : {before['integrity_check']}")
    print(f"foreign_key_check     : {before['foreign_key_violations'] or 'clean'}")
    print(f"reversal links        : {before['reversal_links']}")

    init_schema(engine)
    init_schema(engine)  # repeatable

    after = capture(engine)
    with engine.connect() as conn:
        missing_after = [t.name for t in _tables_missing_constraints(conn)]
        migrations = conn.execute(
            text("SELECT version, description FROM schema_migrations ORDER BY version")
        ).fetchall()

    fresh_path = out / "fresh.db"
    if Path(f"{fresh_path}").exists():
        Path(f"{fresh_path}").unlink()
    fresh_engine = get_engine(f"sqlite:///{fresh_path}")
    init_schema(fresh_engine)
    parity = schema_signature(engine) == schema_signature(fresh_engine)

    print("\n--- AFTER ---")
    print(f"tables                : {len(after['tables'])}")
    print(f"tables with CHECK     : {len(after['check_constraints'])}")
    print(f"tables needing rebuild: {missing_after}")
    print(f"integrity_check       : {after['integrity_check']}")
    print(f"foreign_key_check     : {after['foreign_key_violations'] or 'clean'}")
    print(f"reversal links        : {after['reversal_links']}")
    print(f"migrations applied    : {[(v, d) for v, d in migrations]}")
    print(f"schema parity w/ fresh: {parity}")
    print(f"declared CHECK names  : {len(_declared_check_names())}")

    # Tables the migration creates (schema_migrations, posting_authorizations, ...)
    # legitimately have no "before" state. Every table that existed before must
    # keep its exact row count.
    new_tables = sorted(set(after["row_counts"]) - set(before["row_counts"]))
    preserved = {
        t: (before["row_counts"][t], after["row_counts"].get(t))
        for t in before["row_counts"]
        if before["row_counts"][t] != after["row_counts"].get(t)
    }
    print(f"row-count changes     : {preserved or 'none (all pre-existing tables identical)'}")
    print(f"tables added by migration: {new_tables or 'none'} ")
    pre_existing_tables_kept = all(
        before["row_counts"].get(t) == after["row_counts"].get(t)
        for t in before["row_counts"]
    )
    print(f"pre-existing tables   : {len(before['row_counts'])} kept intact = {pre_existing_tables_kept}")

    records_kept = all(after["records"].get(t) == before["records"].get(t) for t in after["records"])
    print(f"sample records intact : {records_kept}")

    report = {
        "source": str(source),
        "copy": str(copy_path),
        "before": before,
        "after": after,
        "tables_needing_rebuild_before": missing_before,
        "tables_needing_rebuild_after": missing_after,
        "migrations": [[v, d] for v, d in migrations],
        "schema_parity_with_fresh": parity,
        "declared_check_constraints": _declared_check_names(),
        "row_count_changes": preserved,
        "tables_added_by_migration": new_tables,
        "sample_records_intact": records_kept,
    }

    if not args.skip_smoke:
        from starlette.testclient import TestClient

        from proptally.web.app import create_app

        app = create_app(db_url=f"sqlite:///{copy_path}")
        with TestClient(app) as client:
            pages = {
                path: client.get(path).status_code
                for path in ("/", "/mortgages", "/transactions", "/reconciliation")
            }
        print(f"\napp startup on migrated DB: {pages}")
        report["app_smoke"] = pages

    report_path = out / "report.json"
    report_path.write_text(json.dumps(report, indent=2, default=str))
    print(f"\nreport: {report_path}")

    ok = (
        not missing_after
        and parity
        and after["integrity_check"] == "ok"
        and not after["foreign_key_violations"]
        and not preserved
        and pre_existing_tables_kept
        and records_kept
    )
    print(f"RESULT: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
