"""Portfolio export: a self-contained archive of one portfolio.

A portfolio is one database plus its document vault, so exporting it is a matter of
taking a consistent snapshot of the database and bundling the files. This is the
practical payoff of the database-per-portfolio model (ADR-009): an owner can be handed
everything they have, and an operator can archive or retire one portfolio without
touching anybody else's.

The snapshot uses the SQLite online backup API rather than copying the file. These
databases run in WAL mode, so committed pages can live in the ``-wal`` sidecar; a byte
copy would silently omit them.
"""

from __future__ import annotations

import json
import sqlite3
import tarfile
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from proptally.db.tenant_manager import TenantManager

#: Files that belong to a portfolio's archive, besides its database.
PORTFOLIO_FILE_DIRS = ("vault", "photos")


@dataclass(frozen=True)
class PortfolioArchive:
    """A built archive on disk, with what it contains."""

    path: Path
    filename: str
    database_bytes: int
    document_files: int
    manifest: dict


def _snapshot_database(source: Path, destination: Path) -> None:
    """A consistent copy of a live database, including nothing uncommitted."""
    if not source.exists():
        raise FileNotFoundError(f"Portfolio database not found: {source}")
    connection = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        connection.execute("VACUUM INTO ?", (str(destination),))
    finally:
        connection.close()


def build_portfolio_archive(
    tenant_manager: TenantManager,
    tenant_id: str,
    *,
    portfolio_name: str = "",
    tenant_slug: str = "",
    exported_for: str = "",
) -> PortfolioArchive:
    """Writes a ``.tar.gz`` of one portfolio and returns where it landed.

    The caller owns the returned file and is responsible for deleting it.
    """
    paths = tenant_manager.paths(tenant_id)
    staging = Path(tempfile.mkdtemp(prefix=f"proptally-export-{tenant_id}-"))
    database_copy = staging / "proptally.db"
    _snapshot_database(paths.database, database_copy)

    document_files = 0
    for directory in PORTFOLIO_FILE_DIRS:
        source = paths.root / directory
        if source.is_dir():
            document_files += sum(1 for item in source.rglob("*") if item.is_file())

    stamp = datetime.now(timezone.utc)
    manifest = {
        "kind": "proptally-portfolio-archive",
        "format_version": 1,
        "tenant_id": tenant_id,
        "portfolio_name": portfolio_name,
        "slug": tenant_slug,
        "exported_at": stamp.isoformat(),
        "exported_for": exported_for,
        "database_file": "proptally.db",
        "directories": [d for d in PORTFOLIO_FILE_DIRS if (paths.root / d).is_dir()],
        "document_files": document_files,
        "note": (
            "This archive contains one portfolio: its ledger database and its document "
            "and photo stores. It excludes the control plane (accounts and sessions), "
            "which is not portfolio data."
        ),
    }

    filename = f"proptally-{tenant_slug or tenant_id}-{stamp.strftime('%Y%m%d-%H%M%S')}.tar.gz"
    archive_path = staging / filename
    with tarfile.open(archive_path, "w:gz") as archive:
        archive.add(database_copy, arcname="proptally.db")
        for directory in PORTFOLIO_FILE_DIRS:
            source = paths.root / directory
            if source.is_dir():
                archive.add(source, arcname=directory)
        manifest_path = staging / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2))
        archive.add(manifest_path, arcname="manifest.json")

    return PortfolioArchive(
        path=archive_path,
        filename=filename,
        database_bytes=database_copy.stat().st_size,
        document_files=document_files,
        manifest=manifest,
    )


def discard_archive(archive: PortfolioArchive) -> None:
    """Removes a built archive and its staging directory."""
    staging = archive.path.parent
    archive.path.unlink(missing_ok=True)
    for leftover in staging.glob("*"):
        leftover.unlink(missing_ok=True)
    staging.rmdir()
