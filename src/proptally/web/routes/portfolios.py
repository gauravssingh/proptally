"""Cross-portfolio view and per-portfolio export.

An account can belong to more than one portfolio: an owner with two portfolios, or an
accountant invited to several clients. ``GET /portfolios`` lists those together with
the member's own role, which is the read-only "one reviewer across many portfolios"
view that per-portfolio navigation cannot express.

Every row and every export is resolved through the account's *own* memberships, so
this page can never reach a portfolio the account is not a member of, whatever the
request asks for.
"""

from __future__ import annotations

import urllib.parse

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from sqlmodel import Session, select
from starlette.background import BackgroundTask

from proptally.db.models import Property
from proptally.db.portfolio_lifecycle import (
    PortfolioDeletionError,
    delete_portfolio,
    portfolio_summary,
)
from proptally.db.portfolio_archive import build_portfolio_archive, discard_archive
from proptally.db.system_models import ROLE_CPA_REVIEWER, ROLE_OWNER, ROLE_READ_ONLY, Tenant
from proptally.db.tenant_manager import TenantManager
from proptally.domain.accounting.metrics import calculate_portfolio_equity
from proptally.web.auth import (
    ROLE_DESCRIPTIONS,
    cookie_name,
    memberships_for,
    switch_active_tenant,
)

#: Roles that may take a full copy of a portfolio away with them.
EXPORT_ROLES = (ROLE_OWNER,)

#: Roles that may retire a portfolio.
DELETE_ROLES = (ROLE_OWNER,)


def _membership_for(request: Request, context, tenant_id: str, *, roles):
    """The account's own membership of ``tenant_id``, if it is in ``roles``.

    A portfolio the account is not a member of is reported as not found, so the page
    cannot be used to discover which portfolios exist.
    """
    membership = next(
        (
            m
            for m in memberships_for(request.app.state.control_engine, context.user.id)
            if m.tenant_id == tenant_id
        ),
        None,
    )
    if membership is None:
        raise HTTPException(status_code=404, detail="Portfolio not found")
    if membership.role not in roles:
        raise HTTPException(
            status_code=403,
            detail=f"Role {membership.role} may not do that in this portfolio.",
        )
    return membership


def _portfolio_rows(control_engine, tenant_manager: TenantManager, user_id: str) -> list[dict]:
    """Every portfolio this account is a member of, with its role and headline figures.

    Figures are read from each portfolio's own database. A portfolio that cannot be
    opened is reported as unavailable rather than dropped, so a member never sees a
    shorter list than they belong to.
    """
    rows = []
    for membership in memberships_for(control_engine, user_id):
        with Session(control_engine) as session:
            tenant = session.get(Tenant, membership.tenant_id)
        if tenant is None:
            continue

        row = {
            "tenant_id": tenant.id,
            "name": tenant.name,
            "slug": tenant.slug,
            "role": membership.role,
            "role_description": ROLE_DESCRIPTIONS.get(membership.role, ""),
            "can_export": membership.role in EXPORT_ROLES,
            "available": True,
            "property_count": 0,
            "net_equity_cents": None,
            "total_debt_cents": None,
        }
        try:
            with Session(tenant_manager.engine_for(tenant.id)) as session:
                row["property_count"] = len(session.exec(select(Property)).all())
                equity = calculate_portfolio_equity(session)
                row["net_equity_cents"] = equity["net_equity_cents"]
                row["total_debt_cents"] = equity["total_debt_cents"]
        except Exception:  # noqa: BLE001 - one unopenable portfolio must not blank the page
            row["available"] = False
        rows.append(row)
    return rows


def get_portfolios_router(templates, auth_dep, session_dep=None):
    """Routes for the cross-portfolio screen.

    Unlike the other routers this one does not take the active portfolio's session:
    it works across every portfolio the account belongs to, opening each one itself.
    """
    router = APIRouter()
    mounted = [Depends(auth_dep)]
    # Choosing which portfolio the session looks at changes only the session, so a
    # read-only member must be able to do it (otherwise accepting an invitation would
    # strand them in a portfolio they cannot act in and cannot leave).
    session_scope = session_dep or auth_dep

    @router.get("/portfolios", response_class=HTMLResponse, dependencies=mounted)
    def list_portfolios(request: Request, context=Depends(auth_dep)):
        return templates.TemplateResponse(
            request=request,
            name="portfolios.html",
            context={
                "active_page": "portfolios",
                "context": context,
                "portfolios": _portfolio_rows(
                    request.app.state.control_engine,
                    request.app.state.tenant_manager,
                    context.user.id,
                ),
                "error": request.query_params.get("error"),
                "csrf_token": context.csrf_token,
            },
        )

    @router.get("/portfolios/{tenant_id}/export", dependencies=mounted)
    def export_portfolio(tenant_id: str, request: Request, context=Depends(auth_dep)):
        """Downloads one portfolio: its ledger database and its documents.

        Owner-only, because the archive is the whole portfolio. Membership is settled
        by looking the portfolio up among this account's own memberships, so an id
        from anywhere else is simply not found.
        """
        _membership_for(request, context, tenant_id, roles=EXPORT_ROLES)

        tenant_manager = request.app.state.tenant_manager
        if not tenant_manager.tenant_exists(tenant_id):
            raise HTTPException(status_code=404, detail="Portfolio data not found")

        with Session(request.app.state.control_engine) as session:
            tenant = session.get(Tenant, tenant_id)

        archive = build_portfolio_archive(
            tenant_manager,
            tenant_id,
            portfolio_name=tenant.name if tenant else "",
            tenant_slug=tenant.slug if tenant else "",
            exported_for=context.user.email,
        )
        return FileResponse(
            path=archive.path,
            media_type="application/gzip",
            filename=archive.filename,
            background=BackgroundTask(discard_archive, archive),
        )

    # The two removal routes take the session-scoped dependency rather than the
    # shared one. The shared dependency gates an unsafe method on the role held in the
    # *active* portfolio, but this operation concerns a portfolio the account selects,
    # and the route checks the role held in that portfolio below. Gating on the active
    # one instead would refuse an owner who happens to be viewing a different portfolio
    # -- for example a read-only invitation they accepted.
    @router.get("/portfolios/{tenant_id}/delete", response_class=HTMLResponse, dependencies=[Depends(session_scope)])
    def confirm_delete_portfolio(tenant_id: str, request: Request, context=Depends(session_scope)):
        """Shows what removing this portfolio would destroy, and how to keep a copy."""
        _membership_for(request, context, tenant_id, roles=DELETE_ROLES)

        tenant_manager = request.app.state.tenant_manager
        summary = portfolio_summary(request.app.state.control_engine, tenant_manager, tenant_id)
        if summary is None:
            raise HTTPException(status_code=404, detail="Portfolio not found")

        return templates.TemplateResponse(
            request=request,
            name="portfolio_delete.html",
            context={
                "active_page": "portfolios",
                "context": context,
                "portfolio": summary,
                "error": request.query_params.get("error"),
                "csrf_token": context.csrf_token,
            },
        )

    @router.post("/portfolios/{tenant_id}/delete", dependencies=[Depends(session_scope)])
    def delete_portfolio_route(
        tenant_id: str,
        request: Request,
        confirm_name: str = Form(""),
        context=Depends(session_scope),
    ):
        """Removes the portfolio, once the owner has typed its name to confirm.

        The typed name is the confirmation: it cannot be satisfied by a stray click,
        and it forces the page that lists what will be lost to be read.
        """
        _membership_for(request, context, tenant_id, roles=DELETE_ROLES)

        tenant_manager = request.app.state.tenant_manager
        summary = portfolio_summary(request.app.state.control_engine, tenant_manager, tenant_id)
        if summary is None:
            raise HTTPException(status_code=404, detail="Portfolio not found")

        if confirm_name.strip() != summary.name:
            return RedirectResponse(
                url=f"/portfolios/{tenant_id}/delete"
                    f"?error={urllib.parse.quote('Type the portfolio name exactly to confirm.')}",
                status_code=303,
            )

        try:
            delete_portfolio(
                request.app.state.control_engine,
                tenant_manager,
                tenant_id=tenant_id,
                actor_user_id=context.user.id,
            )
        except PortfolioDeletionError as exc:
            return RedirectResponse(
                url=f"/portfolios/{tenant_id}/delete?error={urllib.parse.quote(str(exc))}",
                status_code=303,
            )

        # The session may have been looking at the portfolio that just went away; the
        # deletion repointed it, so the list is the safe place to land.
        return RedirectResponse(url="/portfolios", status_code=303)

    @router.post("/portfolios/{tenant_id}/open", dependencies=[Depends(session_scope)])
    def open_portfolio(tenant_id: str, request: Request, context=Depends(session_scope)):
        """Switches the session's active portfolio to one this account belongs to."""
        allowed = {
            m.tenant_id
            for m in memberships_for(request.app.state.control_engine, context.user.id)
        }
        if tenant_id not in allowed:
            # The posted value never chooses which database is opened; the membership
            # list does.
            return RedirectResponse(
                url=f"/portfolios?error={urllib.parse.quote('Not a member of that portfolio.')}",
                status_code=303,
            )
        switch_active_tenant(
            request.app.state.control_engine, request.cookies.get(cookie_name()), tenant_id
        )
        return RedirectResponse(url="/", status_code=303)

    return router
