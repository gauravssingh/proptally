"""Retiring a portfolio: what it holds, and how it is removed.

Deleting a portfolio destroys a database and a document store, so the operation is
deliberately narrow:

* it is described before it happens (:func:`portfolio_summary`), so the owner sees
  what would be lost and can export a copy first;
* it is refused when it would leave *any* member with no portfolio at all, because
  an account with no membership cannot get back in through the UI; and
* it removes the control-plane rows and the files together, in an order that
  respects the foreign keys, so no half-deleted portfolio is left behind.

Nothing is retained afterwards: a portfolio is one directory plus its rows, which is
the practical consequence of the isolation model (ADR-009). An operator who wants
retention should keep a backup (``scripts/backup.sh``) or export first.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import Optional

from sqlmodel import Session, select

from proptally.db.models import (
    Document,
    JournalEntry,
    LoanAccount,
    Property,
    Transaction,
)
from proptally.db.system_models import (
    Tenant,
    TenantMembership,
    User,
    UserInvite,
    UserSession,
)
from proptally.db.tenant_manager import TenantManager

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
    control_engine, tenant_manager: TenantManager, tenant_id: str
) -> Optional[PortfolioSummary]:
    """Counts what the portfolio holds. ``None`` when no such portfolio exists.

    A portfolio whose database cannot be opened is still summarised, with
    ``readable=False``: the owner is deciding whether to remove something, and
    "we cannot read it" is itself information.
    """
    with Session(control_engine) as session:
        tenant = session.get(Tenant, tenant_id)
    if tenant is None:
        return None

    summary = PortfolioSummary(tenant_id=tenant.id, name=tenant.name, slug=tenant.slug)
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


def delete_portfolio(
    control_engine,
    tenant_manager: TenantManager,
    *,
    tenant_id: str,
    actor_user_id: str,
) -> DeletedPortfolio:
    """Removes a portfolio: its files and its control-plane rows.

    Raises :class:`PortfolioDeletionError` when the portfolio does not exist or when
    deleting it would leave a member without any portfolio.

    The acting user is recorded by id. Their email address is deliberately not logged:
    application logs carry no personal data (06_SECURITY_AND_DEPLOYMENT.md §4.2).
    """
    summary = portfolio_summary(control_engine, tenant_manager, tenant_id)
    if summary is None:
        raise PortfolioDeletionError("Portfolio not found.")

    with Session(control_engine) as session:
        memberships = session.exec(
            select(TenantMembership).where(TenantMembership.tenant_id == tenant_id)
        ).all()

        # An account with no membership cannot get back into the application, so a
        # deletion that would cause that is refused rather than performed.
        for membership in memberships:
            remaining = session.exec(
                select(TenantMembership).where(
                    TenantMembership.user_id == membership.user_id,
                    TenantMembership.tenant_id != tenant_id,
                )
            ).all()
            if not remaining:
                member = session.get(User, membership.user_id)
                who = member.email if member else membership.user_id
                raise PortfolioDeletionError(
                    f"{who} is a member of no other portfolio, so removing this one would "
                    "leave that account with nothing to sign in to. Delete the account, or "
                    "move the portfolio, as an operator instead."
                )

        # Sessions looking at this portfolio move to one the member still has, so the
        # next page load works rather than failing on a portfolio that no longer is.
        for row in session.exec(
            select(UserSession).where(UserSession.active_tenant_id == tenant_id)
        ).all():
            fallback = session.exec(
                select(TenantMembership)
                .where(
                    TenantMembership.user_id == row.user_id,
                    TenantMembership.tenant_id != tenant_id,
                )
                .order_by(TenantMembership.created_at)
            ).first()
            row.active_tenant_id = fallback.tenant_id if fallback else None
            session.add(row)

        for invite in session.exec(
            select(UserInvite).where(UserInvite.tenant_id == tenant_id)
        ).all():
            session.delete(invite)
        for membership in memberships:
            session.delete(membership)
        session.flush()

        tenant = session.get(Tenant, tenant_id)
        name, slug = (tenant.name, tenant.slug) if tenant else (summary.name, summary.slug)
        if tenant is not None:
            session.delete(tenant)
        session.commit()

    removed_files = tenant_manager.drop_tenant_storage(tenant_id)

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
