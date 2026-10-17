"""Guided portfolio setup.

The wizard is a view over the same write endpoints the management screen uses:
each step form posts to /properties/create, /accounts/create, /mortgages/create
or /opening-balances with ``return_to=/setup``. Completion is derived from the
portfolio's data, so leaving and returning resumes at the first unfinished step.
"""

from __future__ import annotations

from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlmodel import Session, select

from proptally.db.models import (
    FinancialAccount,
    FinancialInstitution,
    JournalEntry,
    LoanAccount,
    Property,
    Statement,
    Transaction,
)
from proptally.domain.setup_progress import STEP_BY_KEY, evaluate
from proptally.domain.setup_state import ensure, publish, skip_step, unskip_step
from proptally.web.auth import AuthorizationError, current_context
from proptally.web.routes.settings import (
    ACCOUNT_TYPE_OPTIONS,
    LOAN_RATE_TYPE_OPTIONS,
    MIXED_USE_OPTIONS,
    PROPERTY_TYPE_OPTIONS,
    _opening_balance_targets,
)


def _require_mutation() -> None:
    context = current_context()
    if context is None or not context.can_work:
        raise AuthorizationError("This role may read the portfolio but not change it.")


def _institution_names(session: Session) -> list[str]:
    return sorted(
        institution.name
        for institution in session.exec(select(FinancialInstitution)).all()
    )


def get_setup_router(templates, get_session_dep, auth_dep=None):
    router = APIRouter()
    mounted = [Depends(auth_dep)] if auth_dep is not None else []

    def _step_extra(session: Session, step_key: str) -> dict:
        if step_key == "property":
            return {
                "property_type_options": PROPERTY_TYPE_OPTIONS,
                "existing_properties": session.exec(
                    select(Property)
                    .where(Property.is_active == True)
                    .order_by(Property.created_at)
                ).all(),
            }
        if step_key == "accounts":
            return {
                "account_type_options": ACCOUNT_TYPE_OPTIONS,
                "mixed_use_options": MIXED_USE_OPTIONS,
                "institution_names": _institution_names(session),
                "existing_accounts": session.exec(
                    select(FinancialAccount).order_by(FinancialAccount.created_at)
                ).all(),
            }
        if step_key == "mortgage":
            properties = session.exec(
                select(Property)
                .where(Property.is_active == True)
                .order_by(Property.created_at)
            ).all()
            return {
                "properties": properties,
                "property_options": [
                    {"value": prop.id, "label": prop.name} for prop in properties
                ],
                "existing_loans": session.exec(
                    select(LoanAccount).order_by(LoanAccount.created_at)
                ).all(),
                "institution_names": _institution_names(session),
                "loan_rate_type_options": LOAN_RATE_TYPE_OPTIONS,
            }
        if step_key == "opening-balances":
            return {
                "opening_targets": _opening_balance_targets(session),
                "opening_entry": session.exec(
                    select(JournalEntry)
                    .where(JournalEntry.entry_type == "OPENING_BALANCE")
                    .order_by(JournalEntry.entry_date.desc())
                ).first(),
            }
        if step_key == "records":
            return {
                "statement_count": len(session.exec(select(Statement)).all()),
                "transaction_count": len(session.exec(select(Transaction)).all()),
            }
        return {}

    def _render_step(request: Request, session: Session, step_key: str):
        step = STEP_BY_KEY[step_key]
        progress = evaluate(session)
        state = next(item for item in progress.steps if item.step.key == step_key)
        applicable = [item.step.key for item in progress.steps if not item.skipped]
        context = current_context()
        return templates.TemplateResponse(
            request=request,
            name="setup/step.html",
            context={
                "active_page": "setup",
                "step": step,
                "state": state,
                "progress": progress,
                "step_number": (applicable.index(step_key) + 1 if step_key in applicable else None),
                "step_total": len(applicable),
                "can_work": bool(context and context.can_work),
                "message": request.query_params.get("message"),
                "error": request.query_params.get("error"),
                **_step_extra(session, step_key),
            },
        )

    def _render_done(request: Request, progress):
        context = current_context()
        return templates.TemplateResponse(
            request=request,
            name="setup/done.html",
            context={
                "active_page": "setup",
                "progress": progress,
                "can_work": bool(context and context.can_work),
            },
        )

    @router.get("/setup", response_class=HTMLResponse, dependencies=mounted)
    def setup_home(request: Request, session: Session = Depends(get_session_dep)):
        context = current_context()
        progress = evaluate(session)
        if context and context.can_work:
            # Opening the guided flow enrolls an existing portfolio explicitly. A
            # populated portfolio with no setup row predates the draft flow and is
            # already live; it must not be asked to publish retroactively.
            ensure(
                session,
                already_live=progress.has_properties and not progress.enrolled,
            )
            session.commit()
            progress = evaluate(session)

        if progress.publishable:
            return _render_done(request, progress)

        target = progress.next_step or "property"
        query = urlencode({
            key: value
            for key, value in request.query_params.items()
            if key in {"message", "error"}
        })
        url = f"/setup/{target}" + (f"?{query}" if query else "")
        return RedirectResponse(url=url, status_code=303)

    @router.get("/setup/done", response_class=HTMLResponse, dependencies=mounted)
    def setup_done(request: Request, session: Session = Depends(get_session_dep)):
        progress = evaluate(session)
        if not progress.publishable:
            return RedirectResponse(url="/setup", status_code=303)
        return _render_done(request, progress)

    @router.get("/setup/{step_key}", response_class=HTMLResponse, dependencies=mounted)
    def setup_step(
        step_key: str,
        request: Request,
        session: Session = Depends(get_session_dep),
    ):
        if step_key not in STEP_BY_KEY:
            raise HTTPException(status_code=404, detail="Unknown setup step")
        progress = evaluate(session)
        context = current_context()
        if context and context.can_work:
            ensure(
                session,
                already_live=progress.has_properties and not progress.enrolled,
            )
            session.commit()
        return _render_step(request, session, step_key)

    @router.post("/setup/publish", dependencies=mounted)
    def publish_portfolio(session: Session = Depends(get_session_dep)):
        _require_mutation()
        progress = evaluate(session)
        if not progress.publishable:
            query = urlencode({
                "error": "Finish the property, account, mortgage and opening balance steps first."
            })
            return RedirectResponse(url=f"/setup?{query}", status_code=303)
        publish(session)
        session.commit()
        return RedirectResponse(url="/setup/done", status_code=303)

    @router.post("/setup/mortgage/skip", dependencies=mounted)
    def skip_mortgage(session: Session = Depends(get_session_dep)):
        _require_mutation()
        skip_step(session, "mortgage")
        session.commit()
        return RedirectResponse(url="/setup", status_code=303)

    @router.post("/setup/mortgage/unskip", dependencies=mounted)
    def unskip_mortgage(session: Session = Depends(get_session_dep)):
        _require_mutation()
        unskip_step(session, "mortgage")
        session.commit()
        return RedirectResponse(url="/setup/mortgage", status_code=303)

    return router
