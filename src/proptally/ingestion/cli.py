"""Command-line entry point for private, local document ingestion."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from sqlmodel import Session

from proptally.db.connection import get_engine
from proptally.ingestion.service import (
    approve_extraction,
    import_approved_extraction,
    ingest_folder,
    initialize_database,
    update_extraction,
)


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
    parser.add_argument("--vault-dir", type=Path, default=None, help="Document vault directory (defaults to <db_parent>/vault)")
    parser.add_argument("--db", type=Path, default=None, help="Database path (defaults to PROPTALLY_DB_PATH)")
    parser.add_argument("--commit", action="store_true", help="Persist documents and extraction data, and copy PDFs into the local vault")
    args = parser.parse_args()

    is_watch = args.watch or (args.folder is not None and str(args.folder) == "watch")

    if is_watch:
        if args.db:
            args.db = args.db.resolve()
            args.db.parent.mkdir(parents=True, exist_ok=True)
            engine = get_engine(f"sqlite:///{args.db}")
        else:
            engine = get_engine()
        initialize_database(engine)
        db_file = args.db.resolve() if args.db else Path(engine.url.database or ".")
        vault_dir = (args.vault_dir.resolve() if args.vault_dir else (db_file.parent / "vault"))
        vault_dir.mkdir(parents=True, exist_ok=True)

        target_inbox = args.inbox_dir
        if args.folder and str(args.folder) != "watch":
            target_inbox = args.folder
        inbox_path = target_inbox.resolve()

        from proptally.ingestion.watcher import process_inbox_once, watch_inbox_loop

        if args.once:
            with Session(engine) as session:
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
            print(f"PropTally Hot-Folder Watcher starting on: {inbox_path}")
            print(f"Vault: {vault_dir}")
            print(f"Polling interval: {args.poll_interval}s. Press Ctrl+C to exit.")
            watch_inbox_loop(
                inbox_dir=inbox_path,
                get_session=lambda: Session(engine),
                vault_dir=vault_dir,
                poll_interval_seconds=args.poll_interval,
            )
        return

    if args.approve_extraction or args.import_approved_extraction or args.update_extraction:
        if args.commit:
            parser.error("--commit applies only to folder ingestion; approval/import/update operations persist directly.")
        engine = get_engine(f"sqlite:///{args.db.resolve()}" if args.db else None)
        initialize_database(engine)
        with Session(engine) as session:
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
    if args.commit:
        if args.db:
            args.db = args.db.resolve()
            args.db.parent.mkdir(parents=True, exist_ok=True)
            engine = get_engine(f"sqlite:///{args.db}")
        else:
            engine = get_engine()
    else:
        engine = get_engine("sqlite://")
    initialize_database(engine)
    db_file = args.db.resolve() if args.db else Path(engine.url.database or ".")
    vault_dir = (args.vault_dir.resolve() if args.vault_dir else (db_file.parent / "documents"))
    with Session(engine) as session:
        report = ingest_folder(args.folder, session, vault_dir, commit=args.commit)
    print(json.dumps({"mode": "commit" if args.commit else "dry-run", **report}, indent=2))


if __name__ == "__main__":
    main()
