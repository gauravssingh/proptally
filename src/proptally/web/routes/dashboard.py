"""Dashboard / Portfolio Overview web router.

The dashboard reports over an explicit window. With no query parameters it covers
all POSTED entries, and the label states the dates the calculation actually used,
so a period chip can never claim a window the figures did not come from.
"""

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlmodel import Session, select

from proptally.db.models import TransactionProcessingState
from proptally.domain.accounting.metrics import (
    calculate_portfolio_equity,
    calculate_property_financials,
)
from proptally.web.helpers import period_label


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

        return templates.TemplateResponse(
            request=request,
            name="dashboard.html",
            context={
                "active_page": "dashboard",
                "equity": equity,
                "financials": financials,
                "review_count": review_count,
                "debt_basis_label": _debt_basis_label(equity),
                "period_label": period_label(financials),
                "period_is_filtered": bool(start_date or end_date),
            },
        )

    return router
