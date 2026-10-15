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

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from sqlmodel import Session, select
from starlette.background import BackgroundTask

from proptally.db.models import Property
from proptally.db.portfolio_archive import build_portfolio_archive, discard_archive
from proptally.db.system_models import ROLE_CPA_REVIEWER, ROLE_OWNER, ROLE_READ_ONLY, Tenant
from proptally.db.tenant_manager import TenantManager
from proptally.domain.accounting.metrics import calculate_portfolio_equity
from proptally.web.auth import cookie_name, memberships_for, switch_active_tenant

ROLE_DESCRIPTIONS = {
    ROLE_OWNER: "Full access: setup, import, posting, invitations and export.",
    ROLE_CPA_REVIEWER: "Read, plus recording the external review a deduction requires.",
    ROLE_READ_ONLY: "Read only.",
}

#: Roles that may take a full copy of a portfolio away with them.
EXPORT_ROLES = (ROLE_OWNER,)


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
        if membership.role not in EXPORT_ROLES:
            raise HTTPException(
                status_code=403,
                detail=f"Role {membership.role} may read this portfolio but not export it.",
            )

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
                url="/portfolios?error=Not+a+member+of+that+portfolio", status_code=303
            )
        switch_active_tenant(
            request.app.state.control_engine, request.cookies.get(cookie_name()), tenant_id
        )
        return RedirectResponse(url="/", status_code=303)

    return router
