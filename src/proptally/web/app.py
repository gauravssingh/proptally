"""FastAPI application factory for PropTally Client Demo MVP."""

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlmodel import Session

from proptally.db.connection import get_engine
from proptally.db.schema import init_schema
from proptally.domain.fixtures.demo_seed import seed_demo_data
from proptally.web import navigation
from proptally.web.helpers import (
    format_bps,
    format_cents,
    format_cents_sign,
    format_date,
)
from proptally.web.routes.dashboard import get_dashboard_router
from proptally.web.routes.mortgages import get_mortgages_router
from proptally.web.routes.properties import get_properties_router
from proptally.web.routes.reconciliation import get_reconciliation_router
from proptally.web.routes.tax import get_tax_router
from proptally.web.routes.transactions import get_transactions_router


def create_app(db_url: str = None) -> FastAPI:
    """Creates and configures PropTally web application."""
    engine = get_engine(db_url)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # 1. Initialize schema and DDL triggers
        init_schema(engine)
        # 2. Seed synthetic portfolio fixtures idempotently
        if os.environ.get("PROPTALLY_DEMO_SEED", "true").lower() not in {"0", "false", "no"}:
            with Session(engine) as session:
                seed_demo_data(session)
        yield

    app = FastAPI(
        title="PropTally",
        description="Self-hosted property, mortgage, and personal real estate finance engine.",
        version="0.2.0",
        lifespan=lifespan,
    )

    # Mount local static files (zero-egress assets)
    static_dir = Path(__file__).resolve().parent / "static"
    static_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # Mount local photos storage for offline property visuals
    repo_root = Path(__file__).resolve().parents[3]
    photos_dir = Path(os.environ.get("PROPTALLY_PHOTOS_DIR", repo_root / "storage" / "photos"))
    photos_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/photos", StaticFiles(directory=str(photos_dir)), name="photos")

    @app.get("/manifest.json")
    async def get_manifest():
        from fastapi.responses import FileResponse
        return FileResponse(static_dir / "manifest.json", media_type="application/manifest+json")

    # Configure Jinja2 templates
    templates_dir = Path(__file__).resolve().parent / "templates"
    templates = Jinja2Templates(directory=str(templates_dir))
    templates.env.filters["format_cents"] = format_cents
    templates.env.filters["format_cents_sign"] = format_cents_sign
    templates.env.filters["format_bps"] = format_bps
    templates.env.filters["format_date"] = format_date
    templates.env.globals["is_demo_mode"] = (
        os.environ.get("PROPTALLY_DEMO_SEED", "true").lower() not in {"0", "false", "no"}
    )
    # Shell navigation: static structure plus a door list read from the database,
    # so the sidebar and drawer are built once (docs/ui-ux/01_APPLICATION_AUDIT.md M3).
    templates.env.globals["nav"] = navigation
    templates.env.globals["nav_doors"] = lambda: navigation.get_nav_doors(engine)

    # Database session dependency
    def get_session():
        with Session(engine) as session:
            yield session

    # Mount routers
    app.include_router(get_dashboard_router(templates, get_session))
    app.include_router(get_properties_router(templates, get_session))
    app.include_router(get_mortgages_router(templates, get_session))
    app.include_router(get_transactions_router(templates, get_session))
    app.include_router(get_reconciliation_router(templates, get_session))
    app.include_router(get_tax_router(templates, get_session))

    return app


# Default app instance for uvicorn
app = create_app()
