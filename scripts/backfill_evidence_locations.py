#!/usr/bin/env python3
"""Resolve PDF coordinates for evidence rows created before the locator existed.

Idempotent: rows already marked AVAILABLE are left alone. Documents that are no
longer present, are not native PDFs, or cannot be matched are marked UNAVAILABLE
and do not fail the run.
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

import pymupdf
from sqlmodel import Session, select

from proptally.db.onboarding_models import OnboardingDocument, OnboardingFieldEvidence
from proptally.db.tenant_manager import init_system_schema
from proptally.onboarding.extraction import _page_geometry
from proptally.onboarding.source_location import locate_source
from proptally.onboarding.store import OnboardingStore
from proptally.web.app import create_app


def _geometry(path: Path) -> list[dict]:
    document = pymupdf.open(path)
    try:
        return [_page_geometry(page) for page in document]
    finally:
        document.close()


def main() -> int:
    app = create_app()
    init_system_schema(app.state.control_engine)
    store = OnboardingStore(app.state.tenant_manager.root)

    with Session(app.state.control_engine) as session:
        documents = {
            document.id: document
            for document in session.exec(select(OnboardingDocument)).all()
        }
        rows = session.exec(select(OnboardingFieldEvidence)).all()
        grouped: dict[str, list[OnboardingFieldEvidence]] = defaultdict(list)
        for row in rows:
            current = json.loads(row.source_location_json or "{}")
            if current.get("status") == "AVAILABLE":
                continue
            grouped[row.document_id].append(row)

        updated = 0
        skipped = 0
        for document_id, evidence_rows in grouped.items():
            document = documents.get(document_id)
            if document is None or document.format not in {"PDF", "NATIVE_PDF", "SCANNED_PDF"}:
                skipped += len(evidence_rows)
                continue
            try:
                path = store.resolve(document.storage_path)
                geometry = _geometry(path) if path.is_file() else []
            except (ValueError, OSError, pymupdf.FileDataError):
                geometry = []
            for row in evidence_rows:
                location = locate_source(
                    field_name=row.field_name,
                    value=json.loads(row.value_json),
                    evidence=row.evidence_text,
                    page=row.page_number,
                    geometry=geometry,
                )
                row.source_location_json = json.dumps(location)
                session.add(row)
                if location["status"] == "AVAILABLE":
                    updated += 1
                else:
                    skipped += 1
        session.commit()

    print(f"evidence locations: {updated} resolved, {skipped} unresolved")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
