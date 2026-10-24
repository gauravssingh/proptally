"""Pytest configuration and shared fixtures for the PropTally test suite.

Two families of fixture live here:

* ``engine`` / ``session`` -- a bare in-memory ledger database, for tests that
  exercise the accounting engine directly.
* ``owner`` -- a running server instance with one registered, signed-in owner.
  Every web test needs this now that reaching a page requires a session, and the
  helper registers through the real form rather than bypassing it, so the tests
  exercise the same sign-in path a person does.
"""

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Optional

import pytest
from bs4 import BeautifulSoup
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlmodel import Session
from starlette.testclient import TestClient

from proptally.db.connection import configure_sqlite_connection
from proptally.db.schema import init_schema
from proptally.domain.accounting.coa import ensure_entity_ledger_accounts
from proptally.web.app import create_app


def create_test_app(*, db_url=None, system_db_url=None, storage_root=None):
    """Build an app for a test, optionally over a pre-seeded shared database.

    Production ``create_app`` has no shared-database parameter at all. This
    harness substitutes a ``TenantManager`` that points every portfolio at the
    supplied file, and only for the duration of the factory call.
    """
    if db_url is None:
        return create_app(system_db_url=system_db_url, storage_root=storage_root)

    import proptally.web.app as web_app
    from proptally.db.tenant_manager import TenantManager

    shared_db = db_url

    class _SharedTenantManager(TenantManager):
        def __init__(self, *args, **kwargs):
            kwargs["db_url"] = shared_db
            super().__init__(*args, **kwargs)

    original = web_app.TenantManager
    web_app.TenantManager = _SharedTenantManager
    try:
        return create_app(system_db_url=system_db_url, storage_root=storage_root)
    finally:
        web_app.TenantManager = original


def parse_html(content: str | object) -> BeautifulSoup:
    """Parse HTML response or string into a BeautifulSoup DOM tree for semantic querying."""
    if hasattr(content, "text"):
        content = content.text
    return BeautifulSoup(content, "html.parser")


@pytest.fixture(name="parse_html")
def parse_html_fixture():
    """Provides a callable to parse HTTP responses or raw HTML strings into BeautifulSoup."""
    return parse_html


@pytest.fixture(name="engine")
def engine_fixture():
    """Provides an isolated in-memory SQLite database with WAL and triggers configured."""
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    event.listen(engine, "connect", configure_sqlite_connection)
    init_schema(engine)
    return engine


@pytest.fixture(name="session")
def session_fixture(engine):
    """Provides a transactional database session seeded with standard Chart of Accounts."""
    with Session(engine) as session:
        ensure_entity_ledger_accounts(session)
        yield session


@dataclass
class SignedInOwner:
    """A registered owner, signed in, with their portfolio's engine to hand.

    ``csrf()`` returns the session's CSRF token so a test can post the way the
    browser does; ``post()`` is the same call with the token already applied.
    """

    client: TestClient
    app: object
    tenant_id: str
    engine: Engine
    control_engine: Engine
    email: str
    password: str

    def csrf(self) -> str:
        """The session's CSRF token, read from a rendered form.

        Every page carries one: the shell's sign-out form. The administration screen's own
        forms are section-dependent, so scraping a page's *own* form would make this depend
        on which controls the signed-in role happens to have.
        """
        import re

        match = re.search(r'name="csrf_token" value="([^"]+)"', self.client.get("/account").text)
        assert match, "the shell must expose the session's CSRF token"
        return match.group(1)

    def post(self, url: str, data: dict, **kwargs):
        """POSTs with the session's CSRF token attached."""
        return self.client.post(url, data={**data, "csrf_token": self.csrf()}, **kwargs)

    def soup(self, url: str, **kwargs) -> BeautifulSoup:
        """GET a URL and return parsed BeautifulSoup DOM for semantic assertions."""
        res = self.client.get(url, **kwargs)
        return BeautifulSoup(res.text, "html.parser")

    def seed_reference_portfolio(self) -> None:
        """Puts the synthetic reference portfolio into this owner's database.

        Fixture data for tests that need realistic content to render or measure.
        The application ships no sample portfolio, so nothing under ``src/`` can
        populate a tenant.
        """
        from reference_portfolio import seed_reference_portfolio

        with Session(self.engine) as session:
            seed_reference_portfolio(session)


@pytest.fixture(name="owner")
def owner_fixture(tmp_path, monkeypatch) -> Iterator[Callable[..., SignedInOwner]]:
    """Factory for a running instance with one signed-in owner.

    ``db_url`` points the owner's portfolio at a specific database file, which is how
    a test that needs a particular fixture database keeps working now that a session
    is required. Portfolios are otherwise isolated under their own storage root.

    Pass ``app=`` to add a second (or third) person to an *existing* instance, which is
    what a test about more than one account needs: two calls without it are two
    separate servers with separate control planes, and neither can see the other's
    users or invitations.
    """
    monkeypatch.delenv("PROPTALLY_ALLOW_REGISTRATION", raising=False)
    monkeypatch.delenv("PROPTALLY_SECURE_COOKIES", raising=False)

    started: list[TestClient] = []
    created = {"count": 0}

    def _make(
        db_url: Optional[str] = None,
        *,
        app=None,
        reference_portfolio: bool = False,
        email: str = "owner@example.com",
        password: str = "owner-password-1",
        display_name: str = "Owner",
        portfolio_name: str = "Test Portfolio",
    ) -> SignedInOwner:
        if app is None:
            created["count"] += 1
            storage = tmp_path / f"storage-{created['count']}"
            app = create_test_app(
                db_url=db_url,
                system_db_url=f"sqlite:///{storage / 'system.db'}",
                storage_root=str(storage),
            )
        client = TestClient(app)
        client.__enter__()
        started.append(client)

        client.get("/auth/login")
        registered = client.post(
            "/auth/register",
            data={
                "email": email,
                "password": password,
                "display_name": display_name,
                "portfolio_name": portfolio_name,
                "csrf_token": client.cookies.get("pt_csrf"),
            },
            follow_redirects=False,
        )
        assert registered.status_code == 303, registered.text[:400]

        with Session(app.state.control_engine) as session:
            tenant_id = session.execute(
                text("SELECT id FROM tenants WHERE name = :name"),
                {"name": portfolio_name},
            ).scalar()

        signed_in = SignedInOwner(
            client=client,
            app=app,
            tenant_id=tenant_id,
            engine=app.state.tenant_manager.engine_for(tenant_id),
            control_engine=app.state.control_engine,
            email=email,
            password=password,
        )
        if reference_portfolio:
            signed_in.seed_reference_portfolio()
        return signed_in

    yield _make

    for client in started:
        try:
            client.__exit__(None, None, None)
        except Exception:  # noqa: BLE001 - teardown must not mask a test failure
            pass
