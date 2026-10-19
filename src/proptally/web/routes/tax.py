"""Web routes for IRS Schedule E tax reports and CPA workpapers."""

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse
from sqlmodel import Session

from proptally.domain.tax.schedule_e import (
    export_schedule_e_csv,
    generate_schedule_e_workpaper,
)

MIN_TAX_YEAR = 1990
MAX_TAX_YEAR = 2100


def _validate_tax_year(year: int) -> int:
    """Reject years outside a sane calendar range instead of erroring at date()."""
    if not MIN_TAX_YEAR <= year <= MAX_TAX_YEAR:
        raise HTTPException(
            status_code=400,
            detail=f"Tax year must be between {MIN_TAX_YEAR} and {MAX_TAX_YEAR}.",
        )
    return year


def _tax_years(current: int) -> list[int]:
    """A small window around the current year for the workpaper switcher."""
    this_year = date.today().year
    years = {current, this_year - 1, this_year, this_year + 1}
    return sorted(year for year in years if MIN_TAX_YEAR <= year <= MAX_TAX_YEAR)


def get_tax_router(templates, get_session_dep):
    router = APIRouter()

    @router.get("/tax/schedule-e", response_class=HTMLResponse)
    def view_schedule_e(
        request: Request,
        year: int = Query(default_factory=lambda: date.today().year),
        session: Session = Depends(get_session_dep),
    ):
        _validate_tax_year(year)
        workpaper = generate_schedule_e_workpaper(session, tax_year=year)
        return templates.TemplateResponse(
            request=request,
            name="tax_schedule_e.html",
            context={
                "active_page": "tax",
                "workpaper": workpaper,
                "tax_year": year,
                "years": _tax_years(year),
            },
        )

    @router.get("/tax/schedule-e/csv")
    def download_schedule_e_csv(
        year: int = Query(default_factory=lambda: date.today().year),
        session: Session = Depends(get_session_dep),
    ):
        _validate_tax_year(year)
        workpaper = generate_schedule_e_workpaper(session, tax_year=year)
        csv_content = export_schedule_e_csv(workpaper)
        return Response(
            content=csv_content,
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename=Schedule_E_{year}_Workpaper.csv"},
        )

    return router
