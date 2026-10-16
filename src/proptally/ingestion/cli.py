"""Command-line entry point for private, local document ingestion."""

from __future__ import annotations

import argparse
import json
import os
import socket
from contextlib import contextmanager, nullcontext
from pathlib import Path
from uuid import uuid4

from sqlalchemy import Engine, text
from sqlmodel import Session, select
from datetime import datetime, timezone

from proptally.db.connection import get_engine
from proptally.db.system_models import TENANT_ACTIVE, Tenant
from proptally.db.tenant_manager import TenantManager, get_system_engine, init_system_schema
from proptally.ingestion.service import (
    approve_extraction,
    import_approved_extraction,
    ingest_folder,
    initialize_database,
    update_extraction,
)


def _resolve_tenant(
    parser: argparse.ArgumentParser, reference: str
) -> tuple[str, TenantManager, Engine]:
    """Resolve one active tenant from the control plane, never from a global default."""
    control_engine = get_system_engine()
    init_system_schema(control_engine)
    with Session(control_engine) as session:
        tenant = session.get(Tenant, reference)
        if tenant is None:
            tenant = session.exec(select(Tenant).where(Tenant.slug == reference)).first()
    if tenant is None:
        parser.error(f"No portfolio matches --tenant {reference!r}; pass a tenant id or slug.")
    if tenant.status != TENANT_ACTIVE:
        parser.error(f"Portfolio {reference!r} is not active and cannot run ingestion.")
    return tenant.id, TenantManager(), control_engine


@contextmanager
def _tenant_job_scope(control_engine: Engine, tenant_manager: TenantManager, tenant_id: str):
    """Lease a named tenant for one CLI operation or one watcher scan."""
    operation_id = f"job_{uuid4().hex}"
    now = datetime.now(timezone.utc).isoformat()
    with Session(control_engine) as session:
        claimed = session.execute(text(
            "INSERT INTO tenant_operations "
            "(id, tenant_id, actor_id, user_id, session_id, process_id, host_name, started_at) "
            "SELECT :id, tenants.id, :actor_id, NULL, NULL, :process_id, :host_name, :started_at "
            "FROM tenants WHERE tenants.id=:tenant_id AND tenants.status=:active"
        ), {"id": operation_id, "tenant_id": tenant_id, "active": TENANT_ACTIVE,
            "actor_id": "local-operator", "process_id": os.getpid(),
            "host_name": socket.gethostname(), "started_at": now})
        if claimed.rowcount != 1:
            session.rollback()
            raise RuntimeError(f"Tenant {tenant_id!r} is no longer active; job was not started.")
        session.commit()
    tenant_manager.begin_operation(tenant_id, operation_id)
    try:
        tenant_manager.ensure_tenant_storage(tenant_id)
        yield
    finally:
        try:
            with Session(control_engine) as session:
                session.execute(text("DELETE FROM tenant_operations WHERE id=:id"), {"id": operation_id})
                session.commit()
        finally:
            tenant_manager.end_operation(tenant_id, operation_id)


@contextmanager
def _tenant_session(engine: Engine, job_scope, *, initialize: bool = True):
    """Use the explicitly selected engine under its tenant-job lease."""
    scope = job_scope() if job_scope else nullcontext()
    with scope:
        if initialize:
            initialize_database(engine)
        with Session(engine) as session:
            yield session


def _tenant_is_active(control_engine: Engine, tenant_id: str) -> bool:
    with Session(control_engine) as session:
        tenant = session.get(Tenant, tenant_id)
        return tenant is not None and tenant.status == TENANT_ACTIVE


def _resolve_target(
    parser: argparse.ArgumentParser, args
) -> tuple[Engine, Path | None, Path | None, object | None, object | None]:
    """Return explicit database/files plus a short-lived tenant-job lease factory."""
    if args.tenant is not None:
        if args.db is not None:
            parser.error("--tenant and --db are mutually exclusive.")
        if args.vault_dir is not None:
            parser.error("--vault-dir cannot override a named tenant's private vault.")
        tenant_id, tenant_manager, control_engine = _resolve_tenant(parser, args.tenant)
        paths = tenant_manager.ensure_tenant_storage(tenant_id)
        return (
            tenant_manager.engine_for(tenant_id), paths.vault, paths.inbox,
            lambda: _tenant_job_scope(control_engine, tenant_manager, tenant_id),
            lambda: _tenant_is_active(control_engine, tenant_id),
        )
    if args.db is not None:
        args.db = args.db.resolve()
        args.db.parent.mkdir(parents=True, exist_ok=True)
        return get_engine(f"sqlite:///{args.db}"), None, None, None, None
    parser.error("Select an explicit --tenant or --db target.")




def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest mortgage & bank PDFs locally; dry-run unless --commit is set.")
    operations = parser.add_mutually_exclusive_group()
    operations.add_argument("--approve-extraction", metavar="ID", help="Explicitly approve a VALIDATED extraction")
    operations.add_argument("--import-approved-extraction", metavar="ID", help="Import an already-approved extraction")
    operations.add_argument("--update-extraction", metavar="ID", help="Update fields of an extraction and revalidate")
    operations.add_argument("--watch", action="store_true", help="Watch or scan an inbox folder for new incoming statements")
    parser.add_argument("--set", nargs="+", metavar="KEY=VALUE", help="Key-value pairs to update (e.g. principal_cents=52179)")
    parser.add_argument("folder", type=Path, nargs="?", help="Folder containing PDF statements or inbox path")
    parser.add_argument("--inbox-dir", type=Path, default=Path("inbox"), help="Inbox directory for watcher (default: inbox)")
    parser.add_argument("--poll-interval", type=float, default=3.0, help="Polling interval in seconds (default: 3.0)")
    parser.add_argument("--once", action="store_true", help="Process inbox once and exit without running continuous loop")
    parser.add_argument("--vault-dir", type=Path, default=None, help="Standalone vault path; --tenant always uses its own vault and rejects this override")
    parser.add_argument("--db", type=Path, default=None, help="Explicit standalone database path; never inferred from PROPTALLY_DB_PATH and not allowed with --watch")
    parser.add_argument("--tenant", metavar="ID_OR_SLUG", default=None, help="Target one portfolio by tenant id or slug, using its own database and vault")
    parser.add_argument("--commit", action="store_true", help="Persist documents and extraction data, and copy PDFs into the local vault")
    args = parser.parse_args()

    is_watch = args.watch or (args.folder is not None and str(args.folder) == "watch")

    if is_watch:
        if args.tenant is None:
            parser.error("--watch requires an explicit --tenant target, including with --once.")
        engine, portfolio_vault, portfolio_inbox, job_scope, tenant_is_active = _resolve_target(parser, args)
        vault_dir = portfolio_vault

        target_inbox = (
            portfolio_inbox
            if args.tenant is not None and args.inbox_dir == Path("inbox")
            else args.inbox_dir
        )
        if args.folder and str(args.folder) != "watch":
            target_inbox = args.folder
        inbox_path = target_inbox.resolve()

        from proptally.ingestion.watcher import process_inbox_once, watch_inbox_loop

        if args.once:
            with _tenant_session(engine, job_scope) as session:
                vault_dir.mkdir(parents=True, exist_ok=True)
                results = process_inbox_once(inbox_path, session, vault_dir)
                out = [
                    {
                        "filename": r.filename,
                        "status": r.status,
                        "document_id": r.document_id,
                        "statement_id": r.statement_id,
                        "details": r.details,
                    }
                    for r in results
                ]
                print(json.dumps({"processed_count": len(out), "results": out}, indent=2))
        else:
            with _tenant_session(engine, job_scope) as _session:
                vault_dir.mkdir(parents=True, exist_ok=True)
            print(f"PropTally Hot-Folder Watcher starting on: {inbox_path}")
            print(f"Vault: {vault_dir}")
            print(f"Polling interval: {args.poll_interval}s. Press Ctrl+C to exit.")
            watch_inbox_loop(
                inbox_dir=inbox_path,
                get_session=lambda: _tenant_session(engine, job_scope, initialize=False),
                vault_dir=vault_dir,
                poll_interval_seconds=args.poll_interval,
                stop_event=(lambda: not tenant_is_active()) if tenant_is_active else None,
            )
        return

    if args.approve_extraction or args.import_approved_extraction or args.update_extraction:
        if args.commit:
            parser.error("--commit applies only to folder ingestion; approval/import/update operations persist directly.")
        engine, _portfolio_vault, _portfolio_inbox, job_scope, _tenant_is_active = _resolve_target(parser, args)
        with _tenant_session(engine, job_scope) as session:
            if args.update_extraction:
                if not args.set:
                    parser.error("--set KEY=VALUE [KEY=VALUE ...] is required with --update-extraction")
                updates = {}
                for pair in args.set:
                    if "=" not in pair:
                        parser.error(f"Invalid format for --set, expected KEY=VALUE: {pair}")
                    key, val = pair.split("=", 1)
                    if key.endswith(("_cents", "_bps", "_milli_percent")):
                        try:
                            updates[key] = int(val)
                        except ValueError:
                            parser.error(f"Value for {key} must be an integer: {val}")
                    else:
                        updates[key] = val
                extraction = update_extraction(session, args.update_extraction, updates, commit=True)
                print(json.dumps({
                    "extraction_id": extraction.id,
                    "validation_status": extraction.validation_status,
                    "approval_status": extraction.approval_status,
                    "data": json.loads(extraction.extracted_data_json),
                }, indent=2))
            elif args.approve_extraction:
                extraction = approve_extraction(session, args.approve_extraction, commit=True)
                print(json.dumps({"status": extraction.approval_status, "extraction_id": extraction.id}))
            else:
                statement = import_approved_extraction(session, args.import_approved_extraction, commit=True)
                print(json.dumps({"status": "COMMITTED", "statement_id": statement.id}))
        return

    if args.folder is None:
        parser.error("folder is required for ingestion (or specify --watch)")
    if not args.folder.is_dir():
        parser.error(f"Folder does not exist: {args.folder}")
    if args.commit or args.tenant is not None:
        # A dry run against a named portfolio is still matched against that
        # portfolio's own accounts, which is the whole point of naming it.
        engine, portfolio_vault, _portfolio_inbox, job_scope, _tenant_is_active = _resolve_target(parser, args)
    else:
        engine = get_engine("sqlite://")
        portfolio_vault = None
        job_scope = None
    db_file = args.db.resolve() if args.db else Path(engine.url.database or ".")
    vault_dir = args.vault_dir.resolve() if args.vault_dir else (portfolio_vault or db_file.parent / "documents")
    with _tenant_session(engine, job_scope) as session:
        report = ingest_folder(args.folder, session, vault_dir, commit=args.commit)
    print(json.dumps({"mode": "commit" if args.commit else "dry-run", **report}, indent=2))


if __name__ == "__main__":
    main()
