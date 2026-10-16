"""Retiring a portfolio: what it holds, and how it is removed.

Deleting a portfolio destroys a database and a document store, so the operation is
deliberately narrow:

* it is described before it happens (:func:`portfolio_summary`), so the owner sees
  what would be lost and can export a copy first;
* it is refused when it would leave any member without another active, usable portfolio;
  an account with no usable membership cannot reach the portfolio chooser; and
* it removes the control-plane rows and the files together, in an order that
  respects the foreign keys, so no half-deleted portfolio is left behind.

Nothing is retained afterwards: a portfolio is one directory plus its rows, which is
the practical consequence of the isolation model (ADR-009). An operator who wants
retention should keep a backup (``scripts/backup.sh``) or export first.
"""

from __future__ import annotations

import logging
import os
import socket
import time
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Optional
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session, select

from proptally.db.models import (
    Document,
    JournalEntry,
    LoanAccount,
    Property,
    Transaction,
)
from proptally.db.system_models import (
    TENANT_ACTIVE,
    TENANT_DELETED,
    TENANT_DELETING,
    TENANT_PROVISIONING,
    Tenant,
    TenantMembership,
    TenantOperation,
    UserInvite,
    UserSession,
    utc_now,
)
from proptally.db.tenant_manager import (
    TenantManager,
    TenantStorageError,
    system_db_path,
)

logger = logging.getLogger(__name__)


class PortfolioDeletionError(RuntimeError):
    """Raised when a portfolio may not be deleted, with a reason to show the owner."""


@dataclass(frozen=True)
class PortfolioSummary:
    """What a portfolio holds, for the confirmation page."""

    tenant_id: str
    name: str
    slug: str
    properties: int = 0
    loans: int = 0
    documents: int = 0
    transactions: int = 0
    journal_entries: int = 0
    stored_files: int = 0
    readable: bool = True


@dataclass(frozen=True)
class DeletedPortfolio:
    tenant_id: str
    name: str
    slug: str
    properties: int
    documents: int
    removed_files: bool


def portfolio_summary(
    control_engine,
    tenant_manager: TenantManager,
    tenant_id: str,
    *,
    allow_deleting: bool = False,
) -> Optional[PortfolioSummary]:
    """Count one portfolio, or report it unavailable without creating storage."""
    with Session(control_engine) as session:
        tenant = session.get(Tenant, tenant_id)
    if tenant is None or tenant.status == TENANT_DELETED:
        return None

    summary = PortfolioSummary(tenant_id=tenant.id, name=tenant.name, slug=tenant.slug)
    if tenant.status == TENANT_DELETING and not allow_deleting:
        return replace(summary, readable=False)
    try:
        with Session(tenant_manager.engine_for(tenant.id)) as session:
            def count(model):
                return len(session.exec(select(model)).all())

            summary = PortfolioSummary(
                tenant_id=tenant.id,
                name=tenant.name,
                slug=tenant.slug,
                properties=count(Property),
                loans=count(LoanAccount),
                documents=count(Document),
                transactions=count(Transaction),
                journal_entries=count(JournalEntry),
            )
    except Exception:  # noqa: BLE001 - an unreadable portfolio is reported, not hidden
        summary = PortfolioSummary(
            tenant_id=tenant.id, name=tenant.name, slug=tenant.slug, readable=False
        )

    paths = tenant_manager.paths(tenant.id)
    if paths.root.is_dir():
        stored = sum(1 for item in paths.root.rglob("*") if item.is_file())
        summary = replace(summary, stored_files=stored)
    return summary


@dataclass(frozen=True)
class PortfolioStatus:
    """Where one portfolio lives, how much it occupies, and what it holds.

    Read-only. It opens the portfolio's database to count rows and measures its files; an
    unreadable database is reported through ``summary.readable`` rather than raised, so the
    administration screen can state what is wrong instead of failing.
    """

    summary: PortfolioSummary
    created_at: Optional[datetime]
    root: Path
    database: Path
    vault: Path
    photos: Path
    database_bytes: int
    wal_bytes: int
    vault_bytes: int
    photos_bytes: int
    control_plane: Path
    control_plane_bytes: int

    @property
    def portfolio_bytes(self) -> int:
        """Everything an export of this portfolio would contain, in bytes."""
        return self.database_bytes + self.wal_bytes + self.vault_bytes + self.photos_bytes


def _file_bytes(path: Path) -> int:
    return path.stat().st_size if path.is_file() else 0


def _tree_bytes(directory: Path) -> int:
    """Bytes on disk under ``directory``, or 0 when it does not exist."""
    if not directory.is_dir():
        return 0
    return sum(item.stat().st_size for item in directory.rglob("*") if item.is_file())


def portfolio_status(
    control_engine, tenant_manager: TenantManager, tenant_id: str
) -> Optional[PortfolioStatus]:
    """Everything the administration screen reports about one portfolio.

    ``None`` when the portfolio does not exist. The control-plane figures are included
    because the whole-instance backup covers both, and an operator deciding whether to take
    one wants to see the size of what it would contain.

    The control-plane path is read from the engine that is actually open, not from the
    environment: an engine handed a different URL (a test's temporary database, say) must
    not be described by the path the environment would have produced.
    """
    summary = portfolio_summary(control_engine, tenant_manager, tenant_id)
    if summary is None:
        return None

    with Session(control_engine) as session:
        tenant = session.get(Tenant, tenant_id)
    created_at = tenant.created_at if tenant is not None else None

    paths = tenant_manager.paths(tenant_id)
    database_url_path = control_engine.url.database
    control_plane = Path(database_url_path) if database_url_path else system_db_path()

    return PortfolioStatus(
        summary=summary,
        created_at=created_at,
        root=paths.root,
        database=paths.database,
        vault=paths.vault,
        photos=paths.photos,
        database_bytes=_file_bytes(paths.database),
        wal_bytes=_file_bytes(paths.database.with_name(paths.database.name + "-wal")),
        vault_bytes=_tree_bytes(paths.vault),
        photos_bytes=_tree_bytes(paths.photos),
        control_plane=control_plane,
        control_plane_bytes=_file_bytes(control_plane),
    )




def _local_process_is_alive(process_id: int) -> bool:
    try:
        os.kill(process_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
def _reap_dead_local_operations(control_engine, tenant_id: str) -> None:
    """Remove lease rows only when their recorded local worker process is gone."""
    with Session(control_engine) as session:
        operations = session.exec(
            select(TenantOperation).where(TenantOperation.tenant_id == tenant_id)
        ).all()
        stale = []
        for operation in operations:
            if operation.host_name != socket.gethostname():
                continue
            if not _local_process_is_alive(operation.process_id):
                stale.append(operation)
        for operation in stale:
            session.delete(operation)
        if stale:
            session.commit()

def delete_portfolio(
    control_engine,
    tenant_manager: TenantManager,
    *,
    tenant_id: str,
    actor_user_id: str,
    operation_id: Optional[str] = None,
) -> DeletedPortfolio:
    """Retire storage first, then finalize its control-plane tombstone."""
    deletion_token = uuid4().hex
    now = utc_now()
    connection = control_engine.connect().execution_options(isolation_level="AUTOCOMMIT")
    transaction_active = False
    try:
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        transaction_active = True
        tenant = connection.execute(
            text(
                "SELECT name, slug, status, deletion_token, deletion_process_id, "
                "deletion_host_name FROM tenants WHERE id=:tenant_id"
            ),
            {"tenant_id": tenant_id},
        ).first()
        if tenant is None or tenant.status == TENANT_DELETED:
            raise PortfolioDeletionError("Portfolio not found.")
        if tenant.status == TENANT_PROVISIONING:
            raise PortfolioDeletionError(
                "Portfolio provisioning is in progress; retry deletion after it finishes."
            )
        name, slug = tenant.name, tenant.slug
        if tenant.status == TENANT_DELETING and tenant.deletion_token is not None:
            if (
                tenant.deletion_host_name != socket.gethostname()
                or tenant.deletion_process_id is None
                or _local_process_is_alive(tenant.deletion_process_id)
            ):
                raise PortfolioDeletionError(
                    "Portfolio deletion is still owned by a live or unverifiable worker. "
                    "Do not retry until that worker has stopped."
                )
            cleared = connection.execute(text(
                "UPDATE tenants SET deletion_token=NULL, deletion_started_at=NULL, "
                "deletion_process_id=NULL, deletion_host_name=NULL "
                "WHERE id=:tenant_id AND status=:deleting AND deletion_token=:token"
            ), {"tenant_id": tenant_id, "deleting": TENANT_DELETING,
                "token": tenant.deletion_token})
            if cleared.rowcount != 1:
                raise PortfolioDeletionError("Stale deletion claim could not be recovered safely.")

        if tenant.status != TENANT_DELETING:
            memberships = connection.execute(text(
                "SELECT membership.user_id, users.email FROM tenant_memberships AS membership "
                "LEFT JOIN users ON users.id=membership.user_id "
                "WHERE membership.tenant_id=:tenant_id"
            ), {"tenant_id": tenant_id}).all()
            for membership in memberships:
                candidates = connection.execute(text(
                    "SELECT candidate.id FROM tenant_memberships AS membership "
                    "JOIN tenants AS candidate ON candidate.id=membership.tenant_id "
                    "WHERE membership.user_id=:user_id "
                    "AND membership.tenant_id!=:tenant_id AND candidate.status=:active"
                ), {"user_id": membership.user_id, "tenant_id": tenant_id,
                    "active": TENANT_ACTIVE}).scalars().all()
                has_usable_fallback = False
                for candidate_id in candidates:
                    try:
                        tenant_manager.ensure_tenant_storage(candidate_id)
                        with Session(tenant_manager.engine_for(candidate_id)) as candidate_session:
                            candidate_session.exec(select(Property.id).limit(1)).first()
                    except (TenantStorageError, SQLAlchemyError, OSError):
                        continue
                    has_usable_fallback = True
                    break
                if not has_usable_fallback:
                    who = membership.email or membership.user_id
                    raise PortfolioDeletionError(
                        f"{who} has no other active portfolio with usable storage, so removing "
                        "this one would leave that account without a portfolio to open. "
                        "Restore another portfolio or transfer membership before retrying."
                    )

        claimed = connection.execute(text(
            "UPDATE tenants SET status=:deleting, deletion_token=:token, "
            "deletion_started_at=:started, deletion_process_id=:process_id, "
            "deletion_host_name=:host_name WHERE id=:tenant_id "
            "AND status IN (:active, :pending, :failed, :deleting) "
            "AND deletion_token IS NULL"
        ), {"deleting": TENANT_DELETING, "token": deletion_token,
            "started": now.isoformat(), "process_id": os.getpid(),
            "host_name": socket.gethostname(), "tenant_id": tenant_id,
            "active": TENANT_ACTIVE, "pending": "PENDING", "failed": "FAILED"})
        if claimed.rowcount != 1:
            raise PortfolioDeletionError(
                "Portfolio deletion is already in progress. Retry after it finishes."
            )
        connection.execute(text(
            "UPDATE user_sessions SET revoked_at=:now "
            "WHERE active_tenant_id=:tenant_id AND revoked_at IS NULL"
        ), {"now": now.isoformat(), "tenant_id": tenant_id})
        connection.exec_driver_sql("COMMIT")
        transaction_active = False
    except Exception:
        if transaction_active:
            connection.exec_driver_sql("ROLLBACK")
        raise
    finally:
        connection.close()

    completed = False
    try:
        if operation_id:
            with Session(control_engine) as session:
                released = session.execute(text(
                    "DELETE FROM tenant_operations WHERE id=:id AND tenant_id=:tenant_id"
                ), {"id": operation_id, "tenant_id": tenant_id})
                if released.rowcount != 1:
                    raise PortfolioDeletionError(
                        "The deletion request lease could not be released safely. Retry deletion."
                    )
                session.commit()
            tenant_manager.end_operation(tenant_id, operation_id)
            operation_id = None

        deadline = time.monotonic() + 30.0
        while True:
            _reap_dead_local_operations(control_engine, tenant_id)
            with Session(control_engine) as session:
                operations = session.exec(
                    select(TenantOperation).where(TenantOperation.tenant_id == tenant_id)
                ).all()
            if not operations:
                break
            if time.monotonic() >= deadline:
                raise PortfolioDeletionError(
                    "Portfolio is marked for deletion but active requests have not drained. "
                    "Retry the deletion; no files or memberships were removed."
                )
            time.sleep(0.01)
        summary = portfolio_summary(
            control_engine, tenant_manager, tenant_id, allow_deleting=True
        )
        if summary is None:
            raise PortfolioDeletionError("Portfolio not found.")

        try:
            removed_files = tenant_manager.drop_tenant_storage(tenant_id)
        except (OSError, TenantStorageError) as exc:
            raise PortfolioDeletionError(
                f"Portfolio is marked for deletion but storage cleanup failed: {exc}. Retry deletion."
            ) from exc

        with Session(control_engine) as session:
            tenant = session.get(Tenant, tenant_id)
            if tenant is None or tenant.status != TENANT_DELETING or tenant.deletion_token != deletion_token:
                raise PortfolioDeletionError(
                    "Portfolio storage was removed, but its lifecycle state changed unexpectedly."
                )
            for invite in session.exec(
                select(UserInvite).where(UserInvite.tenant_id == tenant_id)
            ).all():
                session.delete(invite)
            for membership in session.exec(
                select(TenantMembership).where(TenantMembership.tenant_id == tenant_id)
            ).all():
                session.delete(membership)
            for row in session.exec(
                select(UserSession).where(UserSession.active_tenant_id == tenant_id)
            ).all():
                row.active_tenant_id = None
                row.revoked_at = row.revoked_at or now
                session.add(row)
            tenant.status = TENANT_DELETED
            tenant.deletion_token = None
            tenant.deletion_started_at = None
            tenant.deletion_process_id = None
            tenant.deletion_host_name = None
            session.add(tenant)
            session.commit()
        completed = True
    finally:
        if not completed:
            with Session(control_engine) as session:
                session.execute(text(
                    "UPDATE tenants SET deletion_token=NULL, deletion_started_at=NULL, "
                    "deletion_process_id=NULL, deletion_host_name=NULL "
                    "WHERE id=:tenant_id AND status=:deleting AND deletion_token=:token"
                ), {"tenant_id": tenant_id, "deleting": TENANT_DELETING,
                    "token": deletion_token})
                session.commit()

    logger.warning(
        "Portfolio deleted: tenant=%s slug=%s by_user=%s files_removed=%s "
        "properties=%s documents=%s",
        tenant_id,
        slug,
        actor_user_id,
        removed_files,
        summary.properties,
        summary.documents,
    )
    return DeletedPortfolio(
        tenant_id=tenant_id,
        name=name,
        slug=slug,
        properties=summary.properties,
        documents=summary.documents,
        removed_files=removed_files,
    )
