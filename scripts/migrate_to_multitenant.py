#!/usr/bin/env python3
"""Migrate a legacy single-portfolio PropTally instance to the multi-tenant layout.

A pre-multi-tenant deployment keeps one SQLite file (``storage/proptally.db`` or
``storage/client-portfolio.db``) plus its ``vault``/``documents`` and ``photos``
directories directly under ``storage/``. The multi-tenant layout instead holds a
control plane (``storage/system.db``: accounts, portfolios, memberships) and one
directory per portfolio (``storage/tenants/<tenant_id>/`` holding that
portfolio's own ``proptally.db``, ``vault`` and ``photos``).

This tool performs that conversion once:

1. creates the first account (the owner, who also becomes instance administrator)
   and a portfolio in ``storage/system.db``;
2. relocates the legacy database to ``storage/tenants/<tenant_id>/proptally.db``
   and the vault/photos directories alongside it; and
3. brings the relocated database's schema up to date (adds any columns and
   constraints that post-date it, allocates per-entity ledger accounts).

It is **dry-run by default**: nothing is written until ``--apply`` is passed, and
it refuses to run twice over the same data (detected from the source database's
SHA-256, recorded in ``storage/migration_log.json``).

Usage
-----
::

    # Show what would happen, changing nothing
    PYTHONPATH=src python scripts/migrate_to_multitenant.py \
        --source storage/client-portfolio.db \
        --email owner@example.com --password 'a-strong-password'

    # Perform the migration (moves the database and vault into place)
    PYTHONPATH=src python scripts/migrate_to_multitenant.py \
        --source storage/client-portfolio.db \
        --email owner@example.com --password 'a-strong-password' \
        --portfolio-name 'My Rentals' --display-name 'Jane Doe' --apply

The vault defaults to the legacy ``storage/vault`` (or ``storage/documents``) and
photos to ``storage/photos`` beside the source database; override with ``--vault``
and ``--photos``. Pass ``--keep-source`` to copy rather than move, leaving the
legacy files in place.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from sqlmodel import Session, select  # noqa: E402

from proptally.db.connection import get_engine  # noqa: E402
from proptally.db.schema import SCHEMA_MIGRATIONS, init_schema  # noqa: E402
from proptally.db.system_models import (  # noqa: E402
    ROLE_OWNER,
    Tenant,
    TenantMembership,
    User,
)
from proptally.db.tenant_manager import (  # noqa: E402
    TENANT_SUBDIRS,
    TenantPaths,
    get_system_engine,
    init_system_schema,
    storage_root as default_storage_root,
)
from proptally.web.auth import (  # noqa: E402
    hash_password,
    normalise_email,
    password_is_acceptable,
    unique_slug,
)

#: Where the fingerprint of every migrated source database is recorded, so the
#: migration refuses to run twice over the same data even if the source remains.
MIGRATION_LOG = "migration_log.json"

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


def _log_path(storage_root: Path) -> Path:
    return storage_root / MIGRATION_LOG


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


def _read_log(storage_root: Path) -> list[dict]:
    path = _log_path(storage_root)
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text())
    except (ValueError, OSError):
        return []


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
    keep_source: bool = False,
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

    source_fingerprint = _fingerprint(source_db)

    # The duplicate check keys on the source *path*, not on its bytes. These
    # databases run in WAL mode, so committed pages can sit in the -wal sidecar
    # while the main file is unchanged, and a later checkpoint rewrites the main
    # file. Hashing only the main file therefore yields a different value for the
    # same logical database and would let a second migration through silently.
    # The fingerprint is still recorded, as evidence of what was migrated.
    if not force:
        for entry in _read_log(storage_root):
            if entry.get("source") == str(source_db):
                raise MigrationError(
                    f"{source_db} was already migrated to tenant {entry.get('tenant_id')!r} "
                    f"on {entry.get('migrated_at')} "
                    f"(recorded sha256 {str(entry.get('sha256'))[:12]}…). Refusing to run twice "
                    "over the same legacy database. Pass --force-remigrate only if you have "
                    "deliberately restored an older copy of it."
                )

    tenant_id = f"tnt_{uuid4().hex[:16]}"
    paths = TenantPaths(
        root=storage_root / "tenants" / tenant_id,
        database=storage_root / "tenants" / tenant_id / "proptally.db",
        vault=storage_root / "tenants" / tenant_id / "vault",
        photos=storage_root / "tenants" / tenant_id / "photos",
        inbox=storage_root / "tenants" / tenant_id / "inbox",
        processed=storage_root / "tenants" / tenant_id / "processed",
        quarantine=storage_root / "tenants" / tenant_id / "quarantine",
    )

    resolved_vault = Path(vault_dir).expanduser().resolve() if vault_dir else _legacy_vault(source_db)
    resolved_photos = (
        Path(photos_dir).expanduser().resolve()
        if photos_dir
        else (source_db.parent / "photos")
    )

    plan = MigrationPlan(
        source_db=source_db,
        storage_root=storage_root,
        system_db=Path(system_db).expanduser().resolve() if system_db else storage_root / "system.db",
        tenant_id=tenant_id,
        tenant_slug="",  # resolved against the live control plane at apply time
        tenant_paths=paths,
        email=normalised_email,
        display_name=display_name.strip() or normalised_email,
        portfolio_name=portfolio_name.strip() or "Imported Portfolio",
        vault_dir=resolved_vault if resolved_vault and resolved_vault.is_dir() else None,
        photos_dir=resolved_photos if resolved_photos and resolved_photos.is_dir() else None,
        keep_source=keep_source,
        source_fingerprint=source_fingerprint,
    )

    plan.moves.append((source_db, paths.database))
    if plan.vault_dir:
        plan.moves.append((plan.vault_dir, paths.vault))
    if plan.photos_dir:
        plan.moves.append((plan.photos_dir, paths.photos))
    return plan


def _copy_database(source: Path, destination: Path) -> None:
    """Copy a SQLite database consistently, including any uncheckpointed WAL.

    A live database keeps recently committed pages in its ``-wal`` sidecar until a
    checkpoint, so a raw file copy can silently lose data. The SQLite backup API
    reads through the log and writes a single consistent file.
    """
    import sqlite3

    source_conn = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        destination_conn = sqlite3.connect(str(destination))
        try:
            source_conn.backup(destination_conn)
        finally:
            destination_conn.close()
    finally:
        source_conn.close()


def _relocate(source: Path, destination: Path, *, keep_source: bool) -> None:
    """Moves (or copies) a file or directory into place, creating the parent."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise MigrationError(f"Refusing to overwrite existing path: {destination}")

    if source.is_dir():
        if keep_source:
            shutil.copytree(source, destination)
        else:
            shutil.move(str(source), str(destination))
        return

    # A database file: snapshot it through SQLite so no WAL content is lost.
    _copy_database(source, destination)
    if not keep_source:
        source.unlink(missing_ok=True)
        for suffix in ("-wal", "-shm"):
            Path(f"{source}{suffix}").unlink(missing_ok=True)


def apply_migration(plan: MigrationPlan, *, password: str) -> MigrationResult:
    """Perform the migration described by ``plan``. Requires an explicit password."""
    ok, reason = password_is_acceptable(password)
    if not ok:
        raise MigrationError(reason)

    plan.storage_root.mkdir(parents=True, exist_ok=True)
    control_engine = get_system_engine(f"sqlite:///{plan.system_db}")
    init_system_schema(control_engine)

    with Session(control_engine) as session:
        slug = unique_slug(control_engine, plan.portfolio_name)
        is_first_user = session.exec(select(User)).first() is None
        existing_user = session.exec(select(User).where(User.email == plan.email)).first()
        if existing_user is not None:
            raise MigrationError(f"An account with email {plan.email!r} already exists.")

        user = User(
            id=f"usr_{uuid4().hex[:16]}",
            email=plan.email,
            password_hash=hash_password(password),
            display_name=plan.display_name,
            is_instance_admin=is_first_user,
        )
        tenant = Tenant(id=plan.tenant_id, name=plan.portfolio_name, slug=slug)
        session.add(user)
        session.add(tenant)
        session.flush()
        session.add(
            TenantMembership(
                id=f"mem_{uuid4().hex[:16]}",
                user_id=user.id,
                tenant_id=tenant.id,
                role=ROLE_OWNER,
            )
        )
        session.commit()
        session.refresh(user)
        plan.tenant_slug = slug

    # Relocate the database and files first, then fill in any portfolio
    # subdirectories the legacy layout did not have.
    moved: list[tuple[str, str]] = []
    for source, destination in plan.moves:
        _relocate(source, destination, keep_source=plan.keep_source)
        moved.append((str(source), str(destination)))

    for subdir in TENANT_SUBDIRS:
        (plan.tenant_paths.root / subdir).mkdir(parents=True, exist_ok=True)

    # Bring the relocated database's schema up to date (adds post-dating columns,
    # constraints and the per-entity ledger accounts). Capture the versions that
    # land so the operator sees exactly what changed.
    tenant_engine = get_engine(f"sqlite:///{plan.tenant_paths.database}")
    before = _applied_schema_versions(tenant_engine)
    init_schema(tenant_engine)
    after = _applied_schema_versions(tenant_engine)
    descriptions = {version: description for version, description, _ in SCHEMA_MIGRATIONS}
    applied_log = [(v, descriptions.get(v, "")) for v in sorted(after - before)]
    tenant_engine.dispose()

    # Record the source fingerprint so a second run over the same data refuses.
    log = _read_log(plan.storage_root)
    log.append(
        {
            "source": str(plan.source_db),
            "sha256": plan.source_fingerprint,
            "tenant_id": plan.tenant_id,
            "tenant_slug": plan.tenant_slug,
            "migrated_at": datetime.now(timezone.utc).isoformat(),
            "kept_source": plan.keep_source,
        }
    )
    _log_path(plan.storage_root).write_text(json.dumps(log, indent=2))

    return MigrationResult(
        applied=True,
        plan=plan,
        user_id=user.id,
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
    keep_source: bool = False,
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
        f"Tenant id:        {plan.tenant_id}",
        f"Mode:             {'APPLY' if applied else 'DRY RUN (pass --apply to write)'}"
        + (" [keep source]" if plan.keep_source else " [move source]"),
        "Relocations:",
    ]
    for source, destination in plan.moves:
        lines.append(f"  {source}")
        lines.append(f"    → {destination}")
    if plan.vault_dir is None:
        lines.append("  (no legacy vault directory found)")
    if plan.photos_dir is None:
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
    parser.add_argument("--keep-source", action="store_true",
                        help="Copy the database and files instead of moving them.")
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
            keep_source=args.keep_source,
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
