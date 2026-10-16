"""FastAPI application factory for PropTally.

Two databases are involved and the difference matters:

* the **control plane** (``storage/system.db``) holds accounts, portfolios,
  memberships and sessions, and
* each **portfolio** has its own database under ``storage/tenants/<id>/``.

Every request resolves a session cookie to exactly one of those portfolios, and the
database session handed to a route can only reach that one. There is no request
path that opens a portfolio chosen by the client.
"""

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlmodel import Session

from proptally.db.tenant_manager import TenantManager, get_system_engine, init_system_schema
from proptally.web import navigation
from proptally.web.auth import (
    AuthenticationDependency,
    AuthenticationRequired,
    AuthorizationError,
    LoginRateLimiter,
    current_context,
)
from proptally.web.helpers import (
    format_bps,
    format_cents,
    format_cents_sign,
    format_date,
)
from proptally.web.routes.auth import get_auth_router
from proptally.web.routes.dashboard import get_dashboard_router
from proptally.web.routes.ingestion import get_ingestion_router
from proptally.web.routes.mortgages import get_mortgages_router
from proptally.web.routes.portfolios import get_portfolios_router
from proptally.web.routes.properties import get_properties_router
from proptally.web.routes.reconciliation import get_reconciliation_router
from proptally.web.routes.settings import get_settings_router
from proptally.web.routes.tax import get_tax_router
from proptally.web.routes.transactions import get_transactions_router


logger = logging.getLogger(__name__)


def create_app(
    db_url: str | None = None,
    *,
    system_db_url: str | None = None,
    storage_root: str | None = None,
) -> FastAPI:
    """Creates and configures the PropTally web application.

    ``db_url`` points every portfolio at one database file and exists for the test
    harness; production leaves it unset so each portfolio gets its own file.
    """
    control_engine = get_system_engine(system_db_url)
    tenant_manager = TenantManager(db_url=db_url, storage_root_path=storage_root)
    rate_limiter = LoginRateLimiter()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        init_system_schema(control_engine)
        yield

    app = FastAPI(
        title="PropTally",
        description="Self-hosted, multi-portfolio property, mortgage and cash flow engine.",
        version="0.3.0",
        lifespan=lifespan,
    )

    # --- Templates --------------------------------------------------------

    templates_dir = Path(__file__).resolve().parent / "templates"
    templates = Jinja2Templates(directory=str(templates_dir))
    templates.env.filters["format_cents"] = format_cents
    templates.env.filters["format_cents_sign"] = format_cents_sign
    templates.env.filters["format_bps"] = format_bps
    templates.env.filters["format_date"] = format_date
    # Shell navigation: static structure plus the doors of the portfolio this
    # request is allowed to see. Read from the request context, never from a
    # process-wide engine, so one portfolio's doors can never appear in another's
    # sidebar.
    templates.env.globals["nav"] = navigation
    templates.env.globals["nav_doors"] = lambda: (
        navigation.get_nav_doors(current_context().engine) if current_context() else []
    )
    templates.env.globals["current_context"] = current_context
    templates.env.globals["csrf_token_value"] = lambda: (
        current_context().csrf_token if current_context() else ""
    )

    def _csrf_field() -> str:
        """Hidden input carrying the request's CSRF token.

        Exposed to templates so every state-changing form can include it without
        the route having to pass the token into each render call.
        """
        from markupsafe import Markup

        return Markup(f'<input type="hidden" name="csrf_token" value="{_csrf_field_token()}">')

    def _csrf_field_token() -> str:
        context = current_context()
        return context.csrf_token if context else ""

    templates.env.globals["csrf_field"] = _csrf_field

    # --- Authentication ---------------------------------------------------

    auth_dep = AuthenticationDependency(control_engine, tenant_manager)
    # Same authentication, without the mutating-role requirement: used only by the
    # operation that picks which portfolio the session is viewing.
    session_dep = AuthenticationDependency(
        control_engine, tenant_manager, require_mutating_role=False
    )

    @app.exception_handler(AuthenticationRequired)
    async def _unauthenticated(request, exc):
        """Browser requests are sent to sign in; API callers get a 401."""
        accepts_html = "text/html" in request.headers.get("accept", "")
        if accepts_html or request.method == "GET":
            target = request.url.path
            if request.url.query:
                target = f"{target}?{request.url.query}"
            return RedirectResponse(url=f"/auth/login?next={target}", status_code=303)
        return RedirectResponse(url="/auth/login", status_code=303)

    @app.exception_handler(AuthorizationError)
    async def _forbidden(request, exc):
        from fastapi.responses import PlainTextResponse

        return PlainTextResponse(str(exc), status_code=403)

    # --- Static assets and per-portfolio files ----------------------------

    static_dir = Path(__file__).resolve().parent / "static"
    static_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @app.get("/manifest.json")
    async def get_manifest():
        return FileResponse(static_dir / "manifest.json", media_type="application/manifest+json")

    @app.get("/photos/{property_id}/{filename}")
    def get_property_photo(property_id: str, filename: str, context=Depends(auth_dep)):
        """Serves a property photo from the signed-in portfolio's own store.

        Ownership is established by the lookup itself: the property must exist in
        this portfolio's database, which is the only one the request can open. The
        filename is resolved inside the portfolio's photo directory so a crafted
        name cannot climb out of it.
        """
        from proptally.db.models import Property

        photo_root = (context.paths.photos / property_id).resolve()
        candidate = (photo_root / filename).resolve()
        if photo_root not in candidate.parents and candidate != photo_root:
            raise HTTPException(status_code=404, detail="Photo not found")
        with Session(context.engine) as session:
            if session.get(Property, property_id) is None:
                raise HTTPException(status_code=404, detail="Photo not found")
        if not candidate.is_file():
            raise HTTPException(status_code=404, detail="Photo not found")
        return FileResponse(candidate)

    # --- Database session for routes --------------------------------------

    def get_session(context=Depends(auth_dep)):
        """A session bound to the portfolio the request authenticated for."""
        with Session(context.engine) as session:
            yield session

    # --- Routers ----------------------------------------------------------

    # Unauthenticated: sign-in, registration, invitations, password reset.
    app.include_router(get_auth_router(templates, control_engine, tenant_manager, rate_limiter, auth_dep))

    # The cross-portfolio screen spans portfolios rather than serving the active one,
    # so it takes the authentication dependency directly instead of a tenant session.
    app.include_router(get_portfolios_router(templates, auth_dep, session_dep))

    # Everything else requires an authenticated portfolio context. Declaring it here
    # means a route added later cannot be reached without one.
    protected = [
        get_dashboard_router(templates, get_session),
        get_properties_router(templates, get_session),
        get_mortgages_router(templates, get_session),
        get_transactions_router(templates, get_session),
        get_reconciliation_router(templates, get_session),
        get_tax_router(templates, get_session),
        get_settings_router(templates, get_session),
        get_ingestion_router(templates, get_session),
    ]
    for router in protected:
        app.include_router(router, dependencies=[Depends(auth_dep)])

    # Recorded here so tests and the `/account` screen can inspect the wiring.
    app.state.control_engine = control_engine
    app.state.tenant_manager = tenant_manager
    app.state.auth_dependency = auth_dep
    app.state.session_dependency = session_dep
    # Exposed for inspection: this limiter is deliberately per-process, which is the one
    # behaviour a multi-worker deployment changes (see ADR-010).
    app.state.login_rate_limiter = rate_limiter

    return app


# Default app instance for uvicorn. Real deployments should set PROPTALLY_* env
# vars (see docs) rather than relying on these defaults.
app = create_app()
