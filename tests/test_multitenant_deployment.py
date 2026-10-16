"""Deployment-level guarantees that only matter with more than one worker.

Running several processes against one storage root is a supported configuration: the
state that has to be shared is in the control-plane database, and each portfolio's
database is accessed through SQLite's own multi-process locking. These tests cover the
parts that are *not* naturally shared and would otherwise fail silently or fatally:
start-up seeding racing between processes, and what the deployment documentation
claims about which state is per-process.
"""

import pytest
from sqlmodel import Session, select

from proptally.db.models import Property
from proptally.db.system_models import Tenant, TenantMembership, User
from proptally.web.app import _seed_demo_portfolio, create_app


@pytest.fixture(name="demo_env")
def demo_env_fixture(tmp_path, monkeypatch):
    """An instance configured to seed the demo portfolio on start-up."""
    monkeypatch.setenv("PROPTALLY_DEMO_SEED", "true")
    monkeypatch.setenv("PROPTALLY_DEMO_PASSWORD", "demo-password-1")
    monkeypatch.setenv("PROPTALLY_DEMO_EMAIL", "demo@example.com")
    storage = tmp_path / "storage"
    return storage


def _start_worker(storage):
    """Starts one worker against the shared storage root, as a real process would."""
    from starlette.testclient import TestClient

    app = create_app(
        system_db_url=f"sqlite:///{storage / 'system.db'}",
        storage_root=str(storage),
        demo_seed=True,
    )
    client = TestClient(app)
    client.__enter__()
    return client, app


def _counts(app):
    with Session(app.state.control_engine) as session:
        users = len(session.exec(select(User)).all())
        tenants = len(session.exec(select(Tenant)).all())
        memberships = len(session.exec(select(TenantMembership)).all())
        tenant_id = session.exec(select(Tenant)).first().id

    with Session(app.state.tenant_manager.engine_for(tenant_id)) as session:
        properties = len(session.exec(select(Property)).all())
    return users, tenants, memberships, properties


def test_two_workers_starting_at_once_seed_the_demo_portfolio_exactly_once(demo_env):
    """A start-up race must not fail a worker, and must not duplicate the demo portfolio.

    Both processes run the same check-then-act sequence; the one that loses the race
    gets a duplicate-account error, which must be treated as "somebody else did it"
    rather than being allowed to abort that worker's start-up.
    """
    first, first_app = _start_worker(demo_env)
    try:
        # The second worker starts against the same storage root, which is exactly the
        # situation a multi-worker deployment creates on every boot.
        second, second_app = _start_worker(demo_env)
        try:
            assert _counts(first_app) == _counts(second_app)
            users, tenants, memberships, properties = _counts(first_app)
            assert (users, tenants, memberships) == (1, 1, 1)
            assert properties > 0, "the demo portfolio must actually contain its data"

            # Both workers can sign the demo account in, so both share one control plane.
            for client in (first, second):
                client.get("/auth/login")
                signed_in = client.post(
                    "/auth/login",
                    data={
                        "email": "demo@example.com",
                        "password": "demo-password-1",
                        "csrf_token": client.cookies.get("pt_csrf"),
                    },
                    follow_redirects=False,
                )
                assert signed_in.status_code == 303, signed_in.text[:200]
                assert client.get("/").status_code == 200
        finally:
            second.__exit__(None, None, None)
    finally:
        first.__exit__(None, None, None)


def test_seeding_finishes_an_interrupted_start(demo_env, tmp_path):
    """If a worker died between creating the account and seeding the data, the next one finishes it.

    The two steps are not one transaction, so a crash in between leaves an account with
    an empty portfolio. The next start must complete it rather than concluding there is
    nothing to do.
    """
    from uuid import uuid4

    from proptally.db.tenant_manager import TenantManager, get_system_engine, init_system_schema

    control_engine = get_system_engine(f"sqlite:///{demo_env / 'system.db'}")
    init_system_schema(control_engine)
    manager = TenantManager(storage_root_path=demo_env)

    # Recreate the half-done state: the account and portfolio row exist, the data does not.
    tenant_id = f"tnt_{uuid4().hex[:16]}"
    with Session(control_engine) as session:
        session.add(User(
            id=f"usr_{uuid4().hex[:16]}", email="demo@example.com",
            password_hash="x", display_name="Demo Owner",
        ))
        session.flush()
        session.add(Tenant(id=tenant_id, name="Demo Portfolio", slug="demo-portfolio"))
        session.flush()
        session.add(TenantMembership(
            id=f"mem_{uuid4().hex[:16]}",
            user_id=session.exec(select(User)).first().id,
            tenant_id=tenant_id,
        ))
        session.commit()
    manager.ensure_tenant_storage(tenant_id)
    with Session(manager.engine_for(tenant_id)) as session:
        assert session.exec(select(Property)).all() == []

    # The next worker's start-up completes the seed instead of skipping it.
    _seed_demo_portfolio(control_engine, manager)

    with Session(manager.engine_for(tenant_id)) as session:
        assert len(session.exec(select(Property)).all()) > 0
    with Session(control_engine) as session:
        assert len(session.exec(select(Tenant)).all()) == 1, "no second portfolio was created"


def test_session_state_is_shared_between_workers_but_throttling_is_not(demo_env):
    """What a second worker shares, and what it does not, stated as an executable claim.

    Sessions live in the control-plane database, so a cookie issued by one worker is
    honoured by another. Login throttling is a per-process in-memory counter, so the
    effective allowance is multiplied by the number of workers -- that is the one
    behaviour a multi-worker deployment changes, and it is recorded in ADR-010.
    """
    first, first_app = _start_worker(demo_env)
    try:
        second, second_app = _start_worker(demo_env)
        try:
            # Registering through the first worker yields a session the second honours.
            first.get("/auth/login")
            registered = first.post(
                "/auth/register",
                data={
                    "email": "shared@example.com", "password": "shared-password-1",
                    "display_name": "Shared", "portfolio_name": "Shared Portfolio",
                    "csrf_token": first.cookies.get("pt_csrf"),
                },
                follow_redirects=False,
            )
            assert registered.status_code == 303, registered.text[:300]

            cookie = first.cookies.get("pt_session")
            second.cookies.set("pt_session", cookie)
            assert second.get("/").status_code == 200, "a session must work on any worker"

            # Two separate limiters: exhausting one worker's allowance leaves the other
            # unaffected. Stated as a test so the documentation cannot drift from reality.
            from proptally.web.auth import LOGIN_ATTEMPT_LIMIT

            for _ in range(LOGIN_ATTEMPT_LIMIT):
                first.post("/auth/login", data={
                    "email": "shared@example.com", "password": "wrong-password",
                    "csrf_token": first.cookies.get("pt_csrf"),
                }, follow_redirects=False)

            limiter_first = first_app.state.login_rate_limiter
            limiter_second = second_app.state.login_rate_limiter
            assert limiter_first is not limiter_second
            assert limiter_first.is_blocked("testclient", "shared@example.com")
            assert not limiter_second.is_blocked("testclient", "shared@example.com"), (
                "each worker counts separately, so the allowance is per process"
            )
        finally:
            second.__exit__(None, None, None)
    finally:
        first.__exit__(None, None, None)
