"""Tenant storage: one SQLite database, vault and photo store per portfolio.

Isolation is physical. A portfolio's ledger lives in its own database file under
``storage/tenants/<tenant_id>/``, so no query anywhere in the application can read
another portfolio's rows by forgetting a filter, and one owner's import cannot hold
the write lock another owner's request needs.

The control plane (``storage/system.db``) holds the accounts and memberships that
decide which of those files a request may open. It never holds ledger data.
"""

from __future__ import annotations

import os
import re
import shutil
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlmodel import SQLModel

from proptally.db.connection import configure_sqlite_connection
from proptally.db.schema import init_schema
from proptally.db.system_models import SYSTEM_TABLES

#: Default storage root: the repository's ``storage/`` directory.
DEFAULT_STORAGE_ROOT = Path(__file__).resolve().parents[3] / "storage"

#: Per-portfolio directories created for every tenant.
TENANT_SUBDIRS = ("vault", "photos", "inbox", "processed", "quarantine")

#: How many portfolio engines to keep open per process.
ENGINE_CACHE_SIZE = 64

_SLUG_UNSAFE = re.compile(r"[^a-z0-9]+")


def storage_root() -> Path:
    """The root under which the control plane and every portfolio live."""
    return Path(os.environ.get("PROPTALLY_STORAGE_ROOT", str(DEFAULT_STORAGE_ROOT)))


def system_db_path() -> Path:
    """Path of the control-plane database."""
    configured = os.environ.get("PROPTALLY_SYSTEM_DB_PATH")
    return Path(configured) if configured else storage_root() / "system.db"


def tenant_directory(tenant_id: str) -> Path:
    """The directory holding one portfolio's database and files."""
    return storage_root() / "tenants" / tenant_id


def tenant_db_path(tenant_id: str) -> Path:
    return tenant_directory(tenant_id) / "proptally.db"


def slugify(value: str) -> str:
    """A filesystem- and URL-safe slug for a portfolio name."""
    slug = _SLUG_UNSAFE.sub("-", value.strip().lower()).strip("-")
    return slug or "portfolio"


def init_system_schema(engine: Engine) -> None:
    """Creates only the control-plane tables.

    Explicitly listed rather than a blanket ``create_all``: SQLModel shares one
    metadata across the control plane and the ledger, so a plain ``create_all``
    would put empty copies of every ledger table in the control-plane database.
    """
    SQLModel.metadata.create_all(engine, tables=list(SYSTEM_TABLES))


def get_system_engine(db_url: Optional[str] = None) -> Engine:
    """Engine for the control-plane database, creating its directory if needed."""
    if db_url is None:
        path = system_db_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        db_url = f"sqlite:///{path}"
    elif db_url.startswith("sqlite:///"):
        Path(db_url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)

    connect_args = {"check_same_thread": False} if db_url.startswith("sqlite") else {}
    engine = create_engine(db_url, connect_args=connect_args)
    if db_url.startswith("sqlite"):
        event.listen(engine, "connect", configure_sqlite_connection)
    return engine


@dataclass
class TenantPaths:
    """Every filesystem location belonging to one portfolio."""

    root: Path
    database: Path
    vault: Path
    photos: Path
    inbox: Path
    processed: Path
    quarantine: Path


def tenant_paths(tenant_id: str) -> TenantPaths:
    directory = tenant_directory(tenant_id)
    return TenantPaths(
        root=directory,
        database=directory / "proptally.db",
        vault=directory / "vault",
        photos=directory / "photos",
        inbox=directory / "inbox",
        processed=directory / "processed",
        quarantine=directory / "quarantine",
    )


class TenantManager:
    """Resolves, creates and caches per-portfolio database engines.

    Engines are cached because building one per request would re-issue the WAL
    pragmas on every page load; the cache is bounded so a long-lived process with
    many portfolios cannot leak file handles without limit.
    """

    def __init__(self, db_url: Optional[str] = None, storage_root_path: Optional[Path] = None):
        # ``db_url`` is a test/harness override that points every portfolio at one
        # database file; ``storage_root_path`` lets an app instance place the
        # control plane and tenant directories somewhere other than the default.
        self._explicit_db_url = db_url
        self.root = Path(storage_root_path) if storage_root_path else storage_root()
        self._engines: dict[str, Engine] = {}
        self._lock = threading.Lock()

    def paths(self, tenant_id: str) -> TenantPaths:
        """Filesystem locations for a portfolio under this manager's root."""
        directory = self.root / "tenants" / tenant_id
        return TenantPaths(
            root=directory,
            database=directory / "proptally.db",
            vault=directory / "vault",
            photos=directory / "photos",
            inbox=directory / "inbox",
            processed=directory / "processed",
            quarantine=directory / "quarantine",
        )

    # --- engine resolution -------------------------------------------------

    def engine_for(self, tenant_id: str) -> Engine:
        """The portfolio's engine, created on first use."""
        with self._lock:
            engine = self._engines.get(tenant_id)
            if engine is not None:
                return engine
            engine = self._build_engine(tenant_id)
            if len(self._engines) >= ENGINE_CACHE_SIZE:
                # Drop the oldest handle; its database stays on disk untouched.
                self._engines.pop(next(iter(self._engines)))
            self._engines[tenant_id] = engine
            return engine

    def _build_engine(self, tenant_id: str) -> Engine:
        if self._explicit_db_url:
            # Test/harness override: every portfolio shares one database. Only the
            # test suite uses this, and it never runs a two-tenant isolation check.
            db_url = self._explicit_db_url
            if db_url.startswith("sqlite:///"):
                Path(db_url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
        else:
            database = self.paths(tenant_id).database
            database.parent.mkdir(parents=True, exist_ok=True)
            db_url = f"sqlite:///{database}"

        connect_args = {"check_same_thread": False} if db_url.startswith("sqlite") else {}
        engine = create_engine(db_url, connect_args=connect_args)
        if db_url.startswith("sqlite"):
            event.listen(engine, "connect", configure_sqlite_connection)
        return engine

    # --- provisioning ------------------------------------------------------

    def ensure_tenant_storage(self, tenant_id: str) -> TenantPaths:
        """Creates the portfolio's directories and brings its schema up to date.

        ``init_schema`` seeds the standard chart of accounts and the generic vendor
        rules, so a new portfolio starts usable and portfolio-independent. It is
        idempotent, so this is safe on every request that needs the directories.
        """
        paths = self.paths(tenant_id)
        for subdir in TENANT_SUBDIRS:
            (paths.root / subdir).mkdir(parents=True, exist_ok=True)
        init_schema(self.engine_for(tenant_id))
        return paths

    def drop_tenant_storage(self, tenant_id: str) -> bool:
        """Disposes of one portfolio's engine handle and its files.

        Returns whether anything was removed. Used by tests and by an operator
        decommissioning a portfolio; the application has no route that calls it.
        """
        with self._lock:
            engine = self._engines.pop(tenant_id, None)
        if engine is not None:
            engine.dispose()
        directory = self.paths(tenant_id).root
        if not directory.exists():
            return False
        shutil.rmtree(directory)
        return True

    # --- helpers -----------------------------------------------------------

    def tenant_exists(self, tenant_id: str) -> bool:
        """Whether this manager's root holds the portfolio's database.

        Resolved through the manager rather than the module-level default, so a
        manager pointed at a different storage root reports on the portfolios it
        actually owns.
        """
        return self.paths(tenant_id).database.exists()
