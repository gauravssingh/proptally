"""Automated hot-folder watcher for zero-touch intake into PropTally vault."""

from __future__ import annotations

import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional
from sqlmodel import Session, select

from proptally.db.models import Document
from proptally.ingestion.service import (
    _persist_document,
    _sha256,
    approve_extraction,
    extract_and_resolve,
    import_approved_extraction,
)


@dataclass
class WatcherFileResult:
    filename: str
    status: str  # 'PROCESSED', 'DUPLICATE', 'NEEDS_REVIEW', 'UNSUPPORTED', 'ERROR'
    document_id: Optional[str] = None
    statement_id: Optional[str] = None
    details: Optional[str] = None


def process_inbox_file(
    file_path: Path,
    session: Session,
    vault_dir: Path,
    processed_dir: Optional[Path] = None,
    review_dir: Optional[Path] = None,
    quarantine_dir: Optional[Path] = None,
    auto_import: bool = True,
) -> WatcherFileResult:
    """Processes a single incoming statement file from inbox."""
    if not file_path.is_file() or file_path.suffix.lower() != ".pdf":
        if quarantine_dir and file_path.is_file():
            quarantine_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(file_path), str(quarantine_dir / file_path.name))
        return WatcherFileResult(filename=file_path.name, status="UNSUPPORTED", details="Not a PDF file")

    try:
        digest = _sha256(file_path)
        existing_doc = session.exec(select(Document).where(Document.file_sha256 == digest)).first()
        if existing_doc:
            if processed_dir:
                processed_dir.mkdir(parents=True, exist_ok=True)
                shutil.move(str(file_path), str(processed_dir / file_path.name))
            return WatcherFileResult(
                filename=file_path.name,
                status="DUPLICATE",
                document_id=existing_doc.id,
                details="File already exists in vault with matching SHA-256 digest.",
            )

        # 1. Classify, extract and match against this portfolio's own accounts
        extraction = extract_and_resolve(file_path, session)

        if extraction is None:
            if quarantine_dir:
                quarantine_dir.mkdir(parents=True, exist_ok=True)
                shutil.move(str(file_path), str(quarantine_dir / file_path.name))
            return WatcherFileResult(
                filename=file_path.name,
                status="UNSUPPORTED",
                details="Document does not match any recognized servicer or banking template.",
            )

        # 2. Persist to vault and database
        document, extraction_id = _persist_document(file_path, digest, vault_dir, session, extraction)

        # 3. If Validated and auto_import is enabled: approve and import
        statement_id = None
        if extraction.status == "VALIDATED" and auto_import and extraction_id:
            approve_extraction(session, extraction_id, commit=True)
            statement = import_approved_extraction(session, extraction_id, commit=True)
            statement_id = statement.id
            final_status = "PROCESSED"
        else:
            final_status = "NEEDS_REVIEW"

        # 4. Move file to target directory
        if final_status == "PROCESSED" and processed_dir:
            processed_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(file_path), str(processed_dir / file_path.name))
        elif final_status == "NEEDS_REVIEW" and (review_dir or quarantine_dir):
            target = review_dir or quarantine_dir
            target.mkdir(parents=True, exist_ok=True)
            shutil.move(str(file_path), str(target / file_path.name))
        elif quarantine_dir:
            quarantine_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(file_path), str(quarantine_dir / file_path.name))

        return WatcherFileResult(
            filename=file_path.name,
            status=final_status,
            document_id=document.id,
            statement_id=statement_id,
            details=f"Extracted by {extraction.parser_name} (Status: {extraction.status})",
        )

    except Exception as exc:
        session.rollback()
        if quarantine_dir and file_path.exists():
            quarantine_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(file_path), str(quarantine_dir / file_path.name))
        return WatcherFileResult(
            filename=file_path.name,
            status="ERROR",
            details=str(exc),
        )


def process_inbox_once(
    inbox_dir: Path,
    session: Session,
    vault_dir: Path,
    archive_subfolders: bool = True,
    auto_import: bool = True,
) -> List[WatcherFileResult]:
    """Scans inbox folder once and processes all pending statements."""
    inbox_dir.mkdir(parents=True, exist_ok=True)
    processed_dir = inbox_dir / "processed" if archive_subfolders else None
    review_dir = inbox_dir / "review" if archive_subfolders else None
    quarantine_dir = inbox_dir / "quarantine" if archive_subfolders else None

    # Get all candidate files directly in inbox (exclude processed/quarantine/review subdirs)
    candidates = [
        f for f in inbox_dir.iterdir()
        if f.is_file() and not f.name.startswith(".")
    ]

    results = []
    for cand in sorted(candidates):
        res = process_inbox_file(
            file_path=cand,
            session=session,
            vault_dir=vault_dir,
            processed_dir=processed_dir,
            review_dir=review_dir,
            quarantine_dir=quarantine_dir,
            auto_import=auto_import,
        )
        results.append(res)
    return results


def watch_inbox_loop(
    inbox_dir: Path,
    get_session: Callable[[], Session],
    vault_dir: Path,
    poll_interval_seconds: float = 3.0,
    stop_event: Optional[Callable[[], bool]] = None,
) -> None:
    """Continuously watches inbox folder until stopped."""
    while True:
        if stop_event and stop_event():
            break
        try:
            with get_session() as session:
                results = process_inbox_once(inbox_dir, session, vault_dir)
                for r in results:
                    print(f"[{r.status}] {r.filename} -> Doc: {r.document_id} Stmt: {r.statement_id} ({r.details})")
        except Exception as exc:
            print(f"Inbox watcher error: {exc}")
        time.sleep(poll_interval_seconds)
