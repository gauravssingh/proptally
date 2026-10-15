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
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlmodel import Session
from starlette.testclient import TestClient

from proptally.db.connection import configure_sqlite_connection
from proptally.db.schema import init_schema
from proptally.domain.accounting.coa import ensure_entity_ledger_accounts
from proptally.web.app import create_app


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
        """The session's CSRF token, read from a rendered form."""
        import re

        match = re.search(r'name="csrf_token" value="([^"]+)"', self.client.get("/account").text)
        assert match, "the account screen must expose the session's CSRF token"
        return match.group(1)

    def post(self, url: str, data: dict, **kwargs):
        """POSTs with the session's CSRF token attached."""
        return self.client.post(url, data={**data, "csrf_token": self.csrf()}, **kwargs)

    def seed_demo_portfolio(self) -> None:
        """Puts the synthetic demo portfolio into this owner's database."""
        from proptally.domain.fixtures.demo_seed import seed_demo_data

        with Session(self.engine) as session:
            seed_demo_data(session)


@pytest.fixture(name="owner")
def owner_fixture(tmp_path, monkeypatch) -> Iterator[Callable[..., SignedInOwner]]:
    """Factory for a running instance with one signed-in owner.

    ``db_url`` points the owner's portfolio at a specific database file, which is how
    a test that needs a particular fixture database keeps working now that a session
    is required. Portfolios are otherwise isolated under their own storage root.
    """
    monkeypatch.delenv("PROPTALLY_ALLOW_REGISTRATION", raising=False)
    monkeypatch.delenv("PROPTALLY_SECURE_COOKIES", raising=False)

    started: list[TestClient] = []
    created = {"count": 0}

    def _make(
        db_url: Optional[str] = None,
        *,
        demo_seed: bool = False,
        email: str = "owner@example.com",
        password: str = "owner-password-1",
        display_name: str = "Owner",
        portfolio_name: str = "Test Portfolio",
    ) -> SignedInOwner:
        created["count"] += 1
        storage = tmp_path / f"storage-{created['count']}"
        app = create_app(
            db_url=db_url,
            system_db_url=f"sqlite:///{storage / 'system.db'}",
            storage_root=str(storage),
            demo_seed=False,
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
        if demo_seed:
            signed_in.seed_demo_portfolio()
        return signed_in

    yield _make

    for client in started:
        try:
            client.__exit__(None, None, None)
        except Exception:  # noqa: BLE001 - teardown must not mask a test failure
            pass
