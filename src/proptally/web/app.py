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
import urllib.parse
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from proptally.db.models import Property
from proptally.db.tenant_manager import (
    TenantManager,
    get_system_engine,
    init_system_schema,
)
from proptally.web import navigation
from proptally.web.auth import (
    AdminDependency,
    AuthenticationDependency,
    AuthenticationRequired,
    AuthorizationError,
    LoginRateLimiter,
    NoActivePortfolio,
    current_context,
    retry_incomplete_provisioning,
)
from proptally.web.helpers import (
    asset_version,
    format_bps,
    format_bytes,
    format_cents,
    format_cents_sign,
    format_date,
    initials,
)
from proptally.domain.setup_state import load as load_setup
from proptally.web.property_forms import fresh_coordinates, google_maps_url
from proptally.web.routes.admin import get_admin_router
from proptally.web.routes.auth import get_auth_router
from proptally.web.routes.dashboard import get_dashboard_router
from proptally.web.routes.ingestion import get_ingestion_router
from proptally.web.routes.mortgages import get_mortgages_router
from proptally.web.routes.portfolios import get_portfolios_router
from proptally.web.routes.properties import get_properties_router
from proptally.web.routes.reconciliation import get_reconciliation_router
from proptally.web.routes.settings import get_settings_router
from proptally.web.routes.setup import get_setup_router
from proptally.web.routes.tax import get_tax_router
from proptally.web.routes.transactions import get_transactions_router
from proptally.web.routes.vault import get_vault_router

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
    if system_db_url is None and storage_root is not None:
        system_db_url = f"sqlite:///{Path(storage_root).expanduser() / 'system.db'}"
    control_engine = get_system_engine(system_db_url)
    tenant_manager = TenantManager(db_url=db_url, storage_root_path=storage_root)
    rate_limiter = LoginRateLimiter()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        init_system_schema(control_engine)
        retry_incomplete_provisioning(control_engine, tenant_manager)
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
    templates.env.filters["format_bytes"] = format_bytes
    templates.env.filters["initials"] = initials
    # Stylesheets are fetched with a version query, so a changed file is a changed URL and
    # a stale cached copy cannot hide a fix (see `helpers.asset_version`).
    templates.env.globals["asset_version"] = asset_version
    templates.env.globals["instance_name"] = (
        os.environ.get("PROPTALLY_INSTANCE_NAME", "").strip() or "PropTally"
    )

    # Google Maps is optional infrastructure. An empty browser key disables address
    # search and the map preview; the manual address fields remain the only path.
    # The key is public by design and restricted by HTTP referrer and API, so it is
    # exposed to the page rather than proxied through a server credential.
    google_maps_browser_key = os.environ.get(
        "PROPTALLY_GOOGLE_MAPS_BROWSER_KEY", ""
    ).strip()
    templates.env.globals["google_maps_browser_key"] = google_maps_browser_key
    templates.env.globals["property_coordinates"] = fresh_coordinates
    # Named to avoid colliding with the ``google_maps_url`` context value rendered by
    # the property detail route; templates need both the helper and the saved link.
    templates.env.globals["property_maps_url"] = google_maps_url

    def _google_maps_embed_url(prop) -> str | None:
        """The Maps Embed API URL for a property, or None when it has no location.

        The place ID is used whenever present because Google permits retaining it;
        coordinates are only a fallback and only while their 30-day cache is fresh.
        """
        if not google_maps_browser_key or prop is None:
            return None
        if prop.google_place_id:
            query = "place_id:" + urllib.parse.quote(prop.google_place_id, safe="")
        else:
            coordinates = fresh_coordinates(prop)
            if coordinates is None:
                return None
            query = f"{coordinates[0]},{coordinates[1]}"
        return (
            "https://www.google.com/maps/embed/v1/place?key="
            + urllib.parse.quote(google_maps_browser_key, safe="")
            + "&q=" + query
            + "&zoom=16&maptype=roadmap"
        )

    templates.env.globals["google_maps_embed_url"] = _google_maps_embed_url

    def _portfolio_is_draft() -> bool:
        """Whether the shell should label the portfolio DRAFT.

        A portfolio with a setup row is a draft until it is published. A legacy
        portfolio with no row is already live; an empty one is still a draft.
        """
        context = current_context()
        if context is None:
            return False
        with Session(context.engine) as session:
            setup = load_setup(session)
            if setup is not None:
                return setup.published_at is None
            return session.exec(select(Property).limit(1)).first() is None

    templates.env.globals["portfolio_is_draft"] = _portfolio_is_draft
    # Shell navigation: static structure plus the doors of the portfolio this
    # request is allowed to see. Read from the request context, never from a
    # process-wide engine, so one portfolio's doors can never appear in another's
    # sidebar.
    templates.env.globals["nav"] = navigation
    templates.env.globals["nav_doors"] = lambda: (
        navigation.get_nav_doors(current_context().engine) if current_context() else []
    )

    def _nav_portfolios():
        context = current_context()
        if context is None:
            return []
        return navigation.get_nav_portfolios(
            control_engine, context.user.id, context.tenant.id
        )

    templates.env.globals["nav_portfolios"] = _nav_portfolios
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
    # The installation console: a session belonging to an active instance
    # administrator, or 404. Control-plane only -- no portfolio lease is taken.
    admin_dep = AdminDependency(control_engine)

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

    @app.exception_handler(NoActivePortfolio)
    async def _no_active_portfolio(request, exc):
        from fastapi.responses import PlainTextResponse

        return PlainTextResponse(str(exc), status_code=503)

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
    app.include_router(get_auth_router(templates, control_engine, tenant_manager, rate_limiter, auth_dep, session_dep))

    # The cross-portfolio screen spans portfolios rather than serving the active one,
    # so it takes the authentication dependency directly instead of a tenant session.
    app.include_router(get_portfolios_router(templates, auth_dep, session_dep))

    # The installation console. Control-plane only, gated by its own dependency;
    # it never mounts the tenant authentication dependency and never opens a ledger.
    app.include_router(
        get_admin_router(templates, control_engine, tenant_manager, admin_dep, rate_limiter)
    )

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
        get_setup_router(templates, get_session),
        get_ingestion_router(templates, get_session),
        get_vault_router(templates, get_session),
    ]
    for router in protected:
        app.include_router(router, dependencies=[Depends(auth_dep)])

    # Recorded here so tests and the `/account` screen can inspect the wiring.
    app.state.control_engine = control_engine
    app.state.tenant_manager = tenant_manager
    app.state.auth_dependency = auth_dep
    app.state.session_dependency = session_dep
    app.state.admin_dependency = admin_dep
    # Exposed for inspection: this limiter is deliberately per-process, which is the one
    # behaviour a multi-worker deployment changes (see ADR-010).
    app.state.login_rate_limiter = rate_limiter

    return app


# Default app instance for uvicorn. Real deployments should set PROPTALLY_* env
# vars (see docs) rather than relying on these defaults.
app = create_app()
