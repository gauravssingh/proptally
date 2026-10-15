"""Web routes for IRS Schedule E tax reports and CPA workpapers."""

from datetime import date
from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import HTMLResponse
from sqlmodel import Session

from proptally.domain.tax.schedule_e import (
    export_schedule_e_csv,
    generate_schedule_e_workpaper,
)


def get_tax_router(templates, get_session_dep):
    router = APIRouter()

    @router.get("/tax/schedule-e", response_class=HTMLResponse)
    def view_schedule_e(
        request: Request,
        year: int = Query(default=2026),
        session: Session = Depends(get_session_dep),
    ):
        workpaper = generate_schedule_e_workpaper(session, tax_year=year)
        return templates.TemplateResponse(
            request=request,
            name="tax_schedule_e.html",
            context={
                "active_page": "tax",
                "workpaper": workpaper,
                "tax_year": year,
            },
        )

    @router.get("/tax/schedule-e/csv")
    def download_schedule_e_csv(
        year: int = Query(default=2026),
        session: Session = Depends(get_session_dep),
    ):
        workpaper = generate_schedule_e_workpaper(session, tax_year=year)
        csv_content = export_schedule_e_csv(workpaper)
        return Response(
            content=csv_content,
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename=Schedule_E_{year}_Workpaper.csv"},
        )

    return router
