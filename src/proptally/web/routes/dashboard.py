"""Dashboard / Portfolio Overview web router.

The dashboard reports over an explicit window. With no query parameters it covers
all POSTED entries, and the label states the dates the calculation actually used,
so a period chip can never claim a window the figures did not come from.
"""

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlmodel import Session, select

from proptally.db.models import Property, TransactionProcessingState
from proptally.domain.accounting.metrics import (
    calculate_portfolio_equity,
    calculate_property_financials,
)
from proptally.domain.setup_progress import evaluate as evaluate_setup
from proptally.web.auth import current_context
from proptally.web.helpers import period_label
from proptally.web.onboarding_resume import latest_resume


def _debt_basis_label(equity) -> str:
    """Describes what actually backs the reported debt figure."""
    sources = {m["principal_source"] for m in equity["property_metrics"]}
    if sources == {"SERVICER_REPORTED"}:
        basis = "STATEMENTS"
    elif sources == {"LEDGER_DERIVED"}:
        basis = "POSTED LEDGER"
    elif sources == {"UNAVAILABLE"}:
        basis = "NO EVIDENCE"
    elif "SERVICER_REPORTED" in sources and "LEDGER_DERIVED" in sources:
        basis = "STATEMENTS"
    else:
        basis = "MIXED"
    if not equity["debt_fully_verified"]:
        basis += " + UNVERIFIED"
    return basis


def _period_window(financials: dict) -> tuple[str, str]:
    """The date range the figures cover, plus what that range means."""
    start = financials.get("period_start")
    end = financials.get("period_end")
    coverage_start = financials.get("coverage_start")
    coverage_end = financials.get("coverage_end")

    if start or end:
        if start and end:
            window = f"{start:%d %b %Y} – {end:%d %b %Y}"
        elif start:
            window = f"From {start:%d %b %Y}"
        else:
            window = f"Through {end:%d %b %Y}"
        if coverage_start and coverage_end:
            return window, "Filtered window · figures follow these dates"
        return window, "Filtered window · no posted activity inside it"

    if coverage_start and coverage_end:
        return (
            f"{coverage_start:%d %b %Y} – {coverage_end:%d %b %Y}",
            "All posted activity",
        )
    return "No posted activity yet", "Post a statement or transaction to start the period"


def _parse_day(value: str | None, field: str) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid {field} date: {value!r}")


def get_dashboard_router(templates, get_session_dep):
    router = APIRouter()

    @router.get("/", response_class=HTMLResponse)
    def view_dashboard(
        request: Request,
        start: str | None = None,
        end: str | None = None,
        session: Session = Depends(get_session_dep),
    ):
        start_date = _parse_day(start, "start")
        end_date = _parse_day(end, "end")

        try:
            financials = calculate_property_financials(
                session, start_date=start_date, end_date=end_date
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        equity = calculate_portfolio_equity(session, as_of_date=end_date)

        # Count items pending review
        unallocated_states = session.exec(
            select(TransactionProcessingState).where(
                TransactionProcessingState.allocation_status != "FULLY_ALLOCATED"
            )
        ).all()
        review_count = len(unallocated_states)

        # A portfolio with no doors has nothing to report on. The screen says so and
        # offers the two ways to start instead of rendering empty figures, and the
        # populated rendering below is untouched.
        context = current_context()
        has_properties = session.exec(
            select(Property).where(Property.is_active.is_(True))
        ).first() is not None
        resume = None
        if not has_properties and context is not None:
            resume = latest_resume(
                request.app.state.control_engine, context.user.id, context.tenant.id
            )

        period_window, period_window_note = _period_window(financials)

        return templates.TemplateResponse(
            request=request,
            name="dashboard.html",
            context={
                "active_page": "dashboard" if has_properties else "onboarding",
                "resume": resume,
                "equity": equity,
                "financials": financials,
                "review_count": review_count,
                "has_properties": has_properties,
                "setup_progress": evaluate_setup(session),
                "can_work": bool(context and context.can_work),
                "debt_basis_label": _debt_basis_label(equity),
                "period_label": period_label(financials),
                "period_is_filtered": bool(start_date or end_date),
                "period_window": period_window,
                "period_window_note": period_window_note,
                "rental_count": sum(
                    1
                    for metric in equity["property_metrics"]
                    if metric["property"].property_type == "RENTAL_INVESTMENT"
                ),
                "primary_count": sum(
                    1
                    for metric in equity["property_metrics"]
                    if metric["property"].property_type != "RENTAL_INVESTMENT"
                ),
            },
        )

    return router
