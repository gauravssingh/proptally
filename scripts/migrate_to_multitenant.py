#!/usr/bin/env python3
"""Migrate a legacy single-portfolio PropTally instance to the multi-tenant layout.

A pre-multi-tenant deployment keeps one SQLite file (``storage/proptally.db`` or
``storage/client-portfolio.db``) plus its ``vault``/``documents`` and ``photos``
directories directly under ``storage/``. The multi-tenant layout instead holds a
control plane (``storage/system.db``: accounts, portfolios, memberships) and one
directory per portfolio (``storage/tenants/<tenant_id>/`` holding that
portfolio's own ``proptally.db``, ``vault`` and ``photos``).

The apply path is staged and resumable:

1. validate inputs and persist a migration record in ``system.db``;
2. snapshot SQLite through its backup API, then copy the database and file stores into
   a private staging directory without altering the legacy source;
3. migrate schema, verify SQLite/foreign keys and posted-ledger balance invariants;
4. atomically publish the verified tenant directory, then create the account, ACTIVE
   tenant, and membership together in the control plane;
5. leave the legacy source in place by default; deleting it is an explicit operator opt-in.

The dry-run is the default. Failed work remains inactive and records its last completed
stage and error in ``system.db``; rerunning the same command resumes from durable state.
The source remains authoritative until activation. Before activation, rollback means
leaving the source in place and not resuming; staged files are never exposed as a tenant.

Usage
-----
::

    # Show what would happen, changing nothing
    PYTHONPATH=src python scripts/migrate_to_multitenant.py \
        --source storage/client-portfolio.db \
        --email owner@example.com --password 'a-strong-password'

    # Perform the staged migration
    PYTHONPATH=src python scripts/migrate_to_multitenant.py \
        --source storage/client-portfolio.db \
        --email owner@example.com --password 'a-strong-password' \
        --portfolio-name 'My Rentals' --display-name 'Jane Doe' --apply

The vault defaults to the legacy ``storage/vault`` (or ``storage/documents``) and
photos to ``storage/photos`` beside the source database; override with ``--vault``
and ``--photos``. The source database and file stores remain after activation unless
``--remove-source`` is explicitly passed; stop every legacy writer before opting in.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from sqlmodel import Session, select

from proptally.db.connection import get_engine
from proptally.db.schema import SCHEMA_MIGRATIONS, init_schema
from proptally.db.system_models import (
    ROLE_OWNER,
    TENANT_ACTIVE,
    LegacyMigration,
    Tenant,
    TenantMembership,
    User,
)
from proptally.db.tenant_manager import (
    TENANT_SUBDIRS,
    TenantPaths,
    get_system_engine,
    init_system_schema,
)
from proptally.db.tenant_manager import (
    storage_root as default_storage_root,
)
from proptally.web.auth import (
    hash_password,
    normalise_email,
    password_is_acceptable,
    unique_slug,
)

#: Subdirectory of the source that a legacy layout keeps its vault in.
_LEGACY_VAULT_NAMES = ("vault", "documents")


class MigrationError(RuntimeError):
    """Raised when a migration cannot proceed (bad input or already migrated)."""


@dataclass
class MigrationPlan:
    """What a migration would do (and, after ``--apply``, what it did)."""

    source_db: Path
    storage_root: Path
    system_db: Path
    migration_id: str
    tenant_id: str
    tenant_slug: str
    tenant_paths: TenantPaths
    email: str
    display_name: str
    portfolio_name: str
    vault_dir: Path | None
    photos_dir: Path | None
    keep_source: bool
    source_fingerprint: str
    moves: list[tuple[Path, Path]] = field(default_factory=list)


@dataclass
class MigrationResult:
    """The outcome of a migration invocation."""

    applied: bool
    plan: MigrationPlan
    user_id: str | None = None
    applied_migrations: list[tuple[int, str]] = field(default_factory=list)
    moved_paths: list[tuple[str, str]] = field(default_factory=list)


def _fingerprint(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()

def _sqlite_fingerprint(path: Path) -> str:
    """Hash a logical SQLite dump, independent of WAL checkpoint/page layout."""
    digest = hashlib.sha256()
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    try:
        for statement in connection.iterdump():
            digest.update(statement.encode("utf-8"))
            digest.update(b"\n")
    finally:
        connection.close()
    return digest.hexdigest()



def _file_tree_fingerprint(directory: Path | None) -> str:
    """Hash relative names, types and bytes for one legacy file store."""
    digest = hashlib.sha256()
    if directory is None or not directory.exists():
        return digest.hexdigest()
    if directory.is_symlink() or not directory.is_dir():
        raise MigrationError(f"Legacy file store must be a real directory: {directory}")

    for item in sorted(directory.rglob("*"), key=lambda entry: entry.relative_to(directory).as_posix()):
        if item.is_symlink():
            raise MigrationError(f"Legacy file store contains a symbolic link: {item}")
        relative = item.relative_to(directory).as_posix().encode("utf-8")
        if item.is_dir():
            digest.update(b"D\0" + relative + b"\0")
        elif item.is_file():
            digest.update(b"F\0" + relative + b"\0")
            with item.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1 << 20), b""):
                    digest.update(chunk)
            digest.update(b"\0")
        else:
            raise MigrationError(f"Legacy file store contains an unsupported entry: {item}")
    return digest.hexdigest()
def _migration_history(system_db: Path, source_path: Path) -> list[dict]:
    """Read migration state without creating or changing anything (including in dry-run)."""
    if not system_db.is_file():
        return []
    uri = f"{system_db.as_uri()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        tables = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='legacy_migrations'"
        ).fetchone()
        if tables is None:
            return []
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(legacy_migrations)")
        }
        vault_fingerprint = "vault_fingerprint" if "vault_fingerprint" in columns else "''"
        photos_fingerprint = "photos_fingerprint" if "photos_fingerprint" in columns else "''"
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute(
            "SELECT id, tenant_id, source_fingerprint, state, stage, email, display_name, "
            "portfolio_name, vault_path, photos_path, "
            f"{vault_fingerprint} AS vault_fingerprint, "
            f"{photos_fingerprint} AS photos_fingerprint, keep_source "
            "FROM legacy_migrations WHERE source_path=? ORDER BY updated_at DESC",
            (str(source_path),),
        )]
    finally:
        connection.close()


def _save_migration(control_engine, migration_id: str, **changes) -> None:
    with Session(control_engine) as session:
        migration = session.get(LegacyMigration, migration_id)
        if migration is None:
            raise MigrationError(f"Migration record {migration_id!r} disappeared.")
        for name, value in changes.items():
            setattr(migration, name, value)
        migration.updated_at = datetime.now(UTC)
        session.add(migration)
        session.commit()


def _applied_schema_versions(engine) -> set[int]:
    """Versions already recorded in a database's ``schema_migrations`` table."""
    from sqlalchemy import text

    with engine.connect() as conn:
        exists = conn.execute(
            text("SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'")
        ).first()
        if not exists:
            return set()
        return {row[0] for row in conn.execute(text("SELECT version FROM schema_migrations"))}




def _legacy_vault(source_db: Path) -> Path | None:
    for name in _LEGACY_VAULT_NAMES:
        candidate = source_db.parent / name
        if candidate.is_dir():
            return candidate
    return None


def build_plan(
    source_db: Path,
    *,
    storage_root: Path,
    email: str,
    display_name: str = "",
    portfolio_name: str = "",
    vault_dir: Path | None = None,
    photos_dir: Path | None = None,
    keep_source: bool = True,
    system_db: Path | None = None,
    force: bool = False,
) -> MigrationPlan:
    """Validate the inputs and describe the migration without touching anything.

    ``force`` skips the already-migrated refusal, for the deliberate case of
    restoring an older copy of a legacy database at the same path.
    """
    source_db = Path(source_db).expanduser().resolve()
    storage_root = Path(storage_root).expanduser().resolve()

    if not source_db.is_file():
        raise MigrationError(
            f"Source database not found: {source_db}. If it was already migrated, "
            "the legacy file has been moved into storage/tenants/."
        )
    if "tenants" in source_db.parts:
        raise MigrationError(
            f"{source_db} already lives in a tenants/ directory: it is not a legacy "
            "single-portfolio database."
        )

    normalised_email = normalise_email(email)
    if not normalised_email or "@" not in normalised_email:
        raise MigrationError("A valid --email is required.")

    system_database = (
        Path(system_db).expanduser().resolve() if system_db else storage_root / "system.db"
    )
    history = _migration_history(system_database, source_db)
    unfinished = next((record for record in history if record["state"] != "ACTIVE"), None)
    if unfinished is not None:
        migration_id = unfinished["id"]
        tenant_id = unfinished["tenant_id"]
        if normalised_email != unfinished["email"]:
            raise MigrationError(
                f"Migration {migration_id} is resumable only with owner email "
                f"{unfinished['email']!r}."
            )
        if display_name.strip() and display_name.strip() != unfinished["display_name"]:
            raise MigrationError("A resumed migration must use its recorded display name.")
        if portfolio_name.strip() and portfolio_name.strip() != unfinished["portfolio_name"]:
            raise MigrationError("A resumed migration must use its recorded portfolio name.")
        if keep_source != bool(unfinished["keep_source"]):
            keep_source = bool(unfinished["keep_source"])
        display_name = unfinished["display_name"]
        portfolio_name = unfinished["portfolio_name"]
        saved_vault = (
            Path(unfinished["vault_path"])
            if unfinished["vault_path"]
            else (_legacy_vault(source_db) or source_db.parent / "vault")
        )
        saved_photos = (
            Path(unfinished["photos_path"])
            if unfinished["photos_path"]
            else source_db.parent / "photos"
        )
        if vault_dir is not None and saved_vault != Path(vault_dir).expanduser().resolve():
            raise MigrationError("A resumed migration must use its recorded vault directory.")
        if photos_dir is not None and saved_photos != Path(photos_dir).expanduser().resolve():
            raise MigrationError("A resumed migration must use its recorded photos directory.")
        resolved_vault, resolved_photos = saved_vault, saved_photos
        source_fingerprint = unfinished["source_fingerprint"] or _fingerprint(source_db)
    else:
        completed = next((record for record in history if record["state"] == "ACTIVE"), None)
        if completed is not None and not force:
            raise MigrationError(
                f"{source_db} was already migrated to tenant {completed['tenant_id']!r}. "
                "Refusing to run twice over the same legacy database. Pass --force-remigrate "
                "only after deliberately restoring an older copy."
            )
        migration_id = f"mig_{uuid4().hex}"
        tenant_id = f"tnt_{uuid4().hex[:16]}"
        source_fingerprint = _fingerprint(source_db)
        resolved_vault = (
            Path(vault_dir).expanduser().resolve()
            if vault_dir
            else (_legacy_vault(source_db) or source_db.parent / "vault")
        )
        resolved_photos = (
            Path(photos_dir).expanduser().resolve() if photos_dir else source_db.parent / "photos"
        )

    paths = TenantPaths(
        root=storage_root / "tenants" / tenant_id,
        database=storage_root / "tenants" / tenant_id / "proptally.db",
        vault=storage_root / "tenants" / tenant_id / "vault",
        photos=storage_root / "tenants" / tenant_id / "photos",
        inbox=storage_root / "tenants" / tenant_id / "inbox",
        processed=storage_root / "tenants" / tenant_id / "processed",
        quarantine=storage_root / "tenants" / tenant_id / "quarantine",
    )

    plan = MigrationPlan(
        source_db=source_db,
        storage_root=storage_root,
        system_db=system_database,
        migration_id=migration_id,
        tenant_id=tenant_id,
        tenant_slug="",  # resolved against the live control plane at apply time
        tenant_paths=paths,
        email=normalised_email,
        display_name=display_name.strip() or normalised_email,
        portfolio_name=portfolio_name.strip() or "Imported Portfolio",
        vault_dir=resolved_vault,
        photos_dir=resolved_photos,
        keep_source=keep_source,
        source_fingerprint=source_fingerprint,
    )
    plan.moves.append((source_db, paths.database))
    if plan.vault_dir.is_dir():
        plan.moves.append((plan.vault_dir, paths.vault))
    if plan.photos_dir.is_dir():
        plan.moves.append((plan.photos_dir, paths.photos))
    return plan


def _copy_database(source: Path, destination: Path) -> None:
    """Create an atomic consistent SQLite snapshot through SQLite's backup API."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    temporary.unlink(missing_ok=True)
    source_conn = sqlite3.connect(f"{source.as_uri()}?mode=ro", uri=True)
    try:
        destination_conn = sqlite3.connect(str(temporary))
        try:
            source_conn.backup(destination_conn)
            integrity = destination_conn.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise MigrationError(f"SQLite snapshot failed integrity check: {integrity}")
        finally:
            destination_conn.close()
    finally:
        source_conn.close()
    os.replace(temporary, destination)


def _verify_financial_database(engine) -> None:
    """Verify SQLite, foreign-key and posted-ledger balance invariants."""
    from sqlalchemy import text

    with engine.connect() as connection:
        integrity = connection.exec_driver_sql("PRAGMA integrity_check").scalar_one()
        if integrity != "ok":
            raise MigrationError(f"SQLite integrity check failed: {integrity}")
        foreign_keys = connection.exec_driver_sql("PRAGMA foreign_key_check").all()
        if foreign_keys:
            raise MigrationError(f"SQLite foreign-key check failed: {foreign_keys!r}")
        unbalanced = connection.execute(text(
            "SELECT entry.id FROM journal_entries AS entry "
            "LEFT JOIN (SELECT journal_entry_id, COUNT(*) AS line_count, "
            "SUM(debit_cents) AS debits, SUM(credit_cents) AS credits "
            "FROM journal_entry_lines GROUP BY journal_entry_id) AS lines "
            "ON lines.journal_entry_id=entry.id "
            "WHERE entry.status='POSTED' AND "
            "(COALESCE(lines.line_count, 0)=0 OR COALESCE(lines.debits, 0)<=0 "
            "OR COALESCE(lines.debits, 0)!=COALESCE(lines.credits, 0)) LIMIT 1"
        )).first()
        if unbalanced:
            raise MigrationError(
                f"Posted journal entry {unbalanced[0]!r} violates the balance invariant."
            )


def _stage_portfolio(
    snapshot: Path, stage_root: Path, plan: MigrationPlan
) -> Path:
    """Build a complete private tenant tree and verify its copied file stores."""
    artifact = stage_root / "portfolio"
    if artifact.exists():
        return artifact
    temporary = stage_root / "portfolio.tmp"
    if temporary.exists():
        shutil.rmtree(temporary)
    temporary.mkdir(parents=True)
    vault_before = _file_tree_fingerprint(plan.vault_dir)
    photos_before = _file_tree_fingerprint(plan.photos_dir)
    _copy_database(snapshot, temporary / "proptally.db")
    if plan.vault_dir.is_dir():
        shutil.copytree(plan.vault_dir, temporary / "vault")
    if plan.photos_dir.is_dir():
        shutil.copytree(plan.photos_dir, temporary / "photos")
    for subdir in TENANT_SUBDIRS:
        (temporary / subdir).mkdir(parents=True, exist_ok=True)
    if (
        _file_tree_fingerprint(plan.vault_dir) != vault_before
        or _file_tree_fingerprint(plan.photos_dir) != photos_before
        or _file_tree_fingerprint(temporary / "vault") != vault_before
        or _file_tree_fingerprint(temporary / "photos") != photos_before
    ):
        shutil.rmtree(temporary)
        raise MigrationError(
            "A legacy vault or photos directory changed while it was being staged; "
            "source files were preserved and the migration can be retried."
        )
    os.replace(temporary, artifact)
    return artifact


def _staged_artifact_matches(
    artifact: Path,
    expected_database_fingerprint: str,
    expected_vault_fingerprint: str,
    expected_photos_fingerprint: str,
) -> bool:
    """Reject partial or modified staged databases and file stores on resume."""
    database = artifact / "proptally.db"
    if not expected_database_fingerprint or not database.is_file():
        return False
    if any(not (artifact / subdir).is_dir() for subdir in TENANT_SUBDIRS):
        return False
    try:
        connection = sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)
        try:
            if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                return False
        finally:
            connection.close()
        return (
            _sqlite_fingerprint(database) == expected_database_fingerprint
            and _file_tree_fingerprint(artifact / "vault") == expected_vault_fingerprint
            and _file_tree_fingerprint(artifact / "photos") == expected_photos_fingerprint
        )
    except (OSError, sqlite3.Error, MigrationError):
        return False
def _refresh_snapshot_if_source_changed(
    plan: MigrationPlan,
    control_engine,
    stage_root: Path,
    snapshot: Path,
    artifact: Path,
) -> tuple[str, str, str]:
    """Refresh an inactive retry when any legacy database or file-store input changed."""
    current_vault_fingerprint = _file_tree_fingerprint(plan.vault_dir)
    current_photos_fingerprint = _file_tree_fingerprint(plan.photos_dir)
    if not snapshot.is_file():
        _copy_database(plan.source_db, snapshot)
        return (
            _sqlite_fingerprint(snapshot),
            current_vault_fingerprint,
            current_photos_fingerprint,
        )

    candidate = stage_root / "source.refresh.sqlite"
    _copy_database(plan.source_db, candidate)
    candidate_fingerprint = _sqlite_fingerprint(candidate)
    with Session(control_engine) as session:
        migration = session.get(LegacyMigration, plan.migration_id)
        tenant = session.get(Tenant, plan.tenant_id)
        if migration is None or migration.state == "ACTIVE" or tenant is not None:
            candidate.unlink(missing_ok=True)
            raise MigrationError("Refusing to refresh a migration that has already activated.")
        database_changed = candidate_fingerprint != _sqlite_fingerprint(snapshot)
        vault_changed = current_vault_fingerprint != migration.vault_fingerprint
        photos_changed = current_photos_fingerprint != migration.photos_fingerprint
        if not (database_changed or vault_changed or photos_changed):
            candidate.unlink(missing_ok=True)
            return candidate_fingerprint, current_vault_fingerprint, current_photos_fingerprint
        previously_published = migration.stage in {"VERIFIED", "PUBLISHED"}

    destination = plan.tenant_paths.root
    if destination.exists():
        if not previously_published:
            candidate.unlink(missing_ok=True)
            raise MigrationError(f"Refusing to replace an unrecognized tenant path: {destination}")
        shutil.rmtree(destination)
    for staged in (artifact, stage_root / "portfolio.tmp"):
        if staged.exists():
            shutil.rmtree(staged)
    if database_changed:
        os.replace(candidate, snapshot)
    else:
        candidate.unlink(missing_ok=True)
    return (
        _sqlite_fingerprint(snapshot),
        current_vault_fingerprint,
        current_photos_fingerprint,
    )


def _lock_and_verify_source_snapshot(plan: MigrationPlan, stage_root: Path):
    """Block new legacy writes until control-plane activation commits."""
    source_guard = sqlite3.connect(str(plan.source_db), timeout=30)
    verification_copy = stage_root / "source.activation.sqlite"
    try:
        source_guard.execute("BEGIN IMMEDIATE")
        _copy_database(plan.source_db, verification_copy)
        current_fingerprint = _sqlite_fingerprint(verification_copy)
        verification_copy.unlink(missing_ok=True)
        if current_fingerprint != plan.source_fingerprint:
            raise MigrationError(
                "Legacy source changed after its staged snapshot. No tenant was activated and "
                "the source was retained; stop legacy writers and rerun to refresh the snapshot."
            )
        return source_guard
    except Exception:
        verification_copy.unlink(missing_ok=True)
        source_guard.rollback()
        source_guard.close()
        raise
def apply_migration(plan: MigrationPlan, *, password: str) -> MigrationResult:
    """Run validated, resumable staging and publish only a verified tenant."""
    ok, reason = password_is_acceptable(password)
    if not ok:
        raise MigrationError(reason)

    plan.storage_root.mkdir(parents=True, exist_ok=True)
    control_engine = get_system_engine(f"sqlite:///{plan.system_db}")
    init_system_schema(control_engine)
    with Session(control_engine) as session:
        if session.exec(select(User).where(User.email == plan.email)).first() is not None:
            raise MigrationError(f"An account with email {plan.email!r} already exists.")
    now = datetime.now(UTC)
    with Session(control_engine) as session:
        migration = session.get(LegacyMigration, plan.migration_id)
        if migration is None:
            migration = LegacyMigration(
                id=plan.migration_id,
                source_path=str(plan.source_db),
                tenant_id=plan.tenant_id,
                state="IN_PROGRESS",
                stage="VALIDATED",
                email=plan.email,
                display_name=plan.display_name,
                portfolio_name=plan.portfolio_name,
                vault_path=str(plan.vault_dir) if plan.vault_dir else None,
                photos_path=str(plan.photos_dir) if plan.photos_dir else None,
                vault_fingerprint=_file_tree_fingerprint(plan.vault_dir),
                photos_fingerprint=_file_tree_fingerprint(plan.photos_dir),
                keep_source=plan.keep_source,
                created_at=now,
                updated_at=now,
            )
            session.add(migration)
        elif migration.state == "ACTIVE":
            raise MigrationError(
                f"Migration {plan.migration_id} is already active for tenant {plan.tenant_id}."
            )
        else:
            migration.state = "IN_PROGRESS"
            migration.last_error = None
            migration.updated_at = now
            session.add(migration)
        session.commit()

    stage_root = plan.storage_root / ".migration-staging" / plan.migration_id
    snapshot = stage_root / "source.sqlite"
    artifact = stage_root / "portfolio"
    moved: list[tuple[str, str]] = []
    tenant_engine = None
    source_guard = None
    activation_committed = False
    try:
        stage_root.mkdir(parents=True, exist_ok=True)
        (
            plan.source_fingerprint,
            vault_fingerprint,
            photos_fingerprint,
        ) = _refresh_snapshot_if_source_changed(
            plan, control_engine, stage_root, snapshot, artifact
        )
        destination = plan.tenant_paths.root
        with Session(control_engine) as session:
            migration = session.get(LegacyMigration, plan.migration_id)
            tenant = session.get(Tenant, plan.tenant_id)
            recognized_published = (
                destination.exists()
                and migration is not None
                and migration.state != "ACTIVE"
                and tenant is None
                and (
                    migration.stage == "PUBLISHED"
                    or (migration.stage == "VERIFIED" and not artifact.exists())
                )
            )
            prior_database_fingerprint = (
                migration.staged_database_fingerprint if migration is not None else ""
            )

        already_published = recognized_published and _staged_artifact_matches(
            destination,
            prior_database_fingerprint,
            vault_fingerprint,
            photos_fingerprint,
        )
        if recognized_published and not already_published:
            shutil.rmtree(destination)
        if already_published:
            if artifact.exists():
                shutil.rmtree(artifact)
            artifact = destination
            _save_migration(
                control_engine,
                plan.migration_id,
                source_fingerprint=plan.source_fingerprint,
                vault_fingerprint=vault_fingerprint,
                photos_fingerprint=photos_fingerprint,
                state="IN_PROGRESS",
                last_error=None,
            )
        else:
            if artifact.exists():
                shutil.rmtree(artifact)
            _save_migration(
                control_engine,
                plan.migration_id,
                source_fingerprint=plan.source_fingerprint,
                vault_fingerprint=vault_fingerprint,
                photos_fingerprint=photos_fingerprint,
                staged_database_fingerprint="",
                stage="SNAPSHOTTED",
                state="IN_PROGRESS",
                last_error=None,
            )
            artifact = _stage_portfolio(snapshot, stage_root, plan)
            _save_migration(
                control_engine, plan.migration_id, stage="STAGED", state="IN_PROGRESS"
            )

        snapshot_engine = get_engine(f"sqlite:///{snapshot}")
        before = _applied_schema_versions(snapshot_engine)
        snapshot_engine.dispose()
        staged_database = artifact / "proptally.db"
        tenant_engine = get_engine(f"sqlite:///{staged_database}")
        init_schema(tenant_engine)
        after = _applied_schema_versions(tenant_engine)
        descriptions = {version: description for version, description, _ in SCHEMA_MIGRATIONS}
        applied_log = [(v, descriptions.get(v, "")) for v in sorted(after - before)]
        _save_migration(
            control_engine,
            plan.migration_id,
            stage="SCHEMA_MIGRATED",
            staged_database_fingerprint=_sqlite_fingerprint(staged_database),
            state="IN_PROGRESS",
        )
        _verify_financial_database(tenant_engine)
        tenant_engine.dispose()
        tenant_engine = None
        _save_migration(
            control_engine, plan.migration_id, stage="VERIFIED", state="IN_PROGRESS"
        )
        if (
            _file_tree_fingerprint(plan.vault_dir) != vault_fingerprint
            or _file_tree_fingerprint(plan.photos_dir) != photos_fingerprint
        ):
            raise MigrationError(
                "A legacy vault or photos directory changed after staging. No tenant was "
                "published; the source is retained and the same command can restage it."
            )
        source_guard = _lock_and_verify_source_snapshot(plan, stage_root)
        if (
            _file_tree_fingerprint(plan.vault_dir) != vault_fingerprint
            or _file_tree_fingerprint(plan.photos_dir) != photos_fingerprint
        ):
            raise MigrationError(
                "A legacy vault or photos directory changed after staging. No tenant was "
                "published; the source is retained and the same command can restage it."
            )

        destination = plan.tenant_paths.root
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            with Session(control_engine) as session:
                migration = session.get(LegacyMigration, plan.migration_id)
                published = (
                    already_published
                    or (
                        migration is not None
                        and (
                            migration.stage in {"PUBLISHED", "ACTIVE"}
                            or (migration.stage == "VERIFIED" and not artifact.exists())
                        )
                    )
                )
            if not published:
                raise MigrationError(f"Refusing to overwrite existing path: {destination}")
        else:
            os.replace(artifact, destination)
        published_engine = get_engine(f"sqlite:///{plan.tenant_paths.database}")
        try:
            _verify_financial_database(published_engine)
        finally:
            published_engine.dispose()
        _save_migration(
            control_engine, plan.migration_id, stage="PUBLISHED", state="IN_PROGRESS"
        )

        with Session(control_engine) as session:
            migration = session.get(LegacyMigration, plan.migration_id)
            if migration is None or migration.state == "ACTIVE":
                raise MigrationError("Migration state changed before activation.")
            existing_user = session.exec(
                select(User).where(User.email == plan.email)
            ).first()
            if existing_user is not None:
                raise MigrationError(f"An account with email {plan.email!r} already exists.")
            slug = unique_slug(control_engine, plan.portfolio_name)
            user = User(
                id=f"usr_{uuid4().hex[:16]}",
                email=plan.email,
                password_hash=hash_password(password),
                display_name=plan.display_name,
                # A migrated owner is a tenant, not the platform's operator. Super-admin
                # authority is created on the host by ``scripts/create_super_admin.py``.
                is_super_admin=False,
            )
            tenant = Tenant(
                id=plan.tenant_id,
                name=plan.portfolio_name,
                slug=slug,
                status=TENANT_ACTIVE,
            )
            session.add(user)
            session.add(tenant)
            session.flush()
            session.add(TenantMembership(
                id=f"mem_{uuid4().hex[:16]}",
                user_id=user.id,
                tenant_id=tenant.id,
                role=ROLE_OWNER,
            ))
            migration.state = "ACTIVE"
            migration.stage = "ACTIVE"
            migration.last_error = None
            migration.updated_at = datetime.now(UTC)
            session.add(migration)
            session.commit()
            plan.tenant_slug = slug
            user_id = user.id
            activation_committed = True
        shutil.rmtree(stage_root, ignore_errors=True)
        if not plan.keep_source:
            source_paths = [plan.source_db]
            source_paths.extend(Path(f"{plan.source_db}{suffix}") for suffix in ("-wal", "-shm"))
            source_paths.extend(path for path in (plan.vault_dir, plan.photos_dir) if path)
            for source in source_paths:
                try:
                    if source.is_dir():
                        shutil.rmtree(source)
                    else:
                        source.unlink(missing_ok=True)
                except OSError:
                    # Publication and control-plane activation are already complete;
                    # retaining a legacy copy is safer than reporting a false rollback.
                    pass
        if not plan.keep_source:
            moved.extend(
                (str(source), str(destination))
                for source, destination in plan.moves
                if not source.exists()
            )
        if source_guard is not None:
            source_guard.rollback()
            source_guard.close()
            source_guard = None
    except Exception as exc:
        if tenant_engine is not None:
            tenant_engine.dispose()
        if source_guard is not None:
            try:
                source_guard.rollback()
            finally:
                source_guard.close()
        if not activation_committed:
            try:
                _save_migration(
                    control_engine,
                    plan.migration_id,
                    state="FAILED",
                    last_error=f"{type(exc).__name__}: {exc}",
                )
            except Exception:
                pass
        if isinstance(exc, MigrationError):
            raise
        if activation_committed:
            raise MigrationError(
                f"Migration {plan.migration_id} activated successfully, but source cleanup "
                f"did not finish: {exc}. The active tenant was not rolled back."
            ) from exc
        raise MigrationError(
            f"Migration {plan.migration_id} failed safely: {exc}. Source data is preserved; "
            "rerun the same command to resume from its durable SQLite stage."
        ) from exc

    return MigrationResult(
        applied=True,
        plan=plan,
        user_id=user_id,
        applied_migrations=applied_log,
        moved_paths=moved,
    )


def migrate_to_multitenant(
    source_db: Path,
    *,
    storage_root: Path,
    email: str,
    password: str,
    display_name: str = "",
    portfolio_name: str = "",
    vault_dir: Path | None = None,
    photos_dir: Path | None = None,
    keep_source: bool = True,
    apply: bool = False,
    system_db: Path | None = None,
    force: bool = False,
) -> MigrationResult:
    """Plan, and (when ``apply``) perform, a single-portfolio → multi-tenant migration."""
    plan = build_plan(
        source_db,
        storage_root=storage_root,
        email=email,
        display_name=display_name,
        portfolio_name=portfolio_name,
        vault_dir=vault_dir,
        photos_dir=photos_dir,
        keep_source=keep_source,
        system_db=system_db,
        force=force,
    )
    if not apply:
        return MigrationResult(applied=False, plan=plan)
    return apply_migration(plan, password=password)


def _describe(plan: MigrationPlan, applied: bool) -> str:
    lines = [
        "=== PropTally single-portfolio → multi-tenant migration ===",
        f"Source database:  {plan.source_db}",
        f"Fingerprint:      sha256 {plan.source_fingerprint}",
        f"Storage root:     {plan.storage_root}",
        f"Control plane:    {plan.system_db}",
        f"Owner:            {plan.email} ({plan.display_name})",
        f"Portfolio:        {plan.portfolio_name} (slug {plan.tenant_slug or '<new>'})",
        f"Migration id:    {plan.migration_id}",
        f"Tenant id:        {plan.tenant_id}",
        f"Mode:             {'APPLY' if applied else 'DRY RUN (pass --apply to write)'}"
        + (" [preserve source]" if plan.keep_source else " [remove source after activation]"),
        "Relocations:",
    ]
    for source, destination in plan.moves:
        lines.append(f"  {source}")
        lines.append(f"    → {destination}")
    if not plan.vault_dir.is_dir():
        lines.append("  (no legacy vault directory found)")
    if not plan.photos_dir.is_dir():
        lines.append("  (no legacy photos directory found)")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Migrate a legacy single-portfolio PropTally database and its vault/photos "
            "into the multi-tenant storage layout. Dry-run by default: nothing is written "
            "until --apply is given. Refuses to run twice over the same data."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  # Inspect what would happen (writes nothing)\n"
            "  python scripts/migrate_to_multitenant.py --source storage/client-portfolio.db \\\n"
            "      --email owner@example.com --password 'a-strong-password'\n\n"
            "  # Perform the migration\n"
            "  python scripts/migrate_to_multitenant.py --source storage/client-portfolio.db \\\n"
            "      --email owner@example.com --password 'a-strong-password' --apply\n"
        ),
    )
    parser.add_argument("--source", required=True, type=Path,
                        help="Legacy single-portfolio SQLite database (e.g. storage/client-portfolio.db).")
    parser.add_argument("--email", required=True, help="Email for the initial owner account.")
    parser.add_argument(
        "--password",
        default=None,
        help="Password for the initial owner account (required with --apply).",
    )
    parser.add_argument("--display-name", default="", help="Owner display name.")
    parser.add_argument("--portfolio-name", default="", help="Portfolio name (defaults to 'Imported Portfolio').")
    parser.add_argument("--storage", type=Path, default=None,
                        help="Storage root (defaults to PROPTALLY_STORAGE_ROOT or storage/).")
    parser.add_argument("--vault", type=Path, default=None, help="Legacy vault directory.")
    parser.add_argument("--photos", type=Path, default=None, help="Legacy photos directory.")
    parser.add_argument(
        "--remove-source",
        action="store_true",
        help="Delete legacy files only after activation; first stop every legacy writer.",
    )
    parser.add_argument(
        "--force-remigrate",
        action="store_true",
        help=(
            "Proceed even though this source database has already been migrated. "
            "Only for a deliberately restored older copy; a normal re-run must keep refusing."
        ),
    )
    parser.add_argument("--apply", action="store_true",
                        help="Perform the migration. Without this flag the run is a dry run.")
    args = parser.parse_args(argv)

    if args.apply and not args.password:
        parser.error("--password is required with --apply")

    storage = args.storage if args.storage else default_storage_root()
    try:
        result = migrate_to_multitenant(
            args.source,
            storage_root=storage,
            email=args.email,
            password=args.password or "",
            display_name=args.display_name,
            portfolio_name=args.portfolio_name,
            vault_dir=args.vault,
            photos_dir=args.photos,
            keep_source=not args.remove_source,
            apply=args.apply,
            force=args.force_remigrate,
        )
    except MigrationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    print(_describe(result.plan, result.applied))
    if not result.applied:
        print("\nDry run complete. Re-run with --apply to migrate.")
        return 0

    print(f"\nOwner user id:   {result.user_id}")
    print(f"Portfolio db:    {result.plan.tenant_paths.database}")
    if result.applied_migrations:
        print("Schema migrations applied: " + ", ".join(f"{v}={d}" for v, d in result.applied_migrations))
    else:
        print("Schema already up to date.")
    print("=== Migration completed successfully ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
