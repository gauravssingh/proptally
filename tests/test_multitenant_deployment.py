"""Deployment-level guarantees that only matter with more than one worker.

Running several processes against one storage root is a supported configuration: the
state that has to be shared is in the control-plane database, and each portfolio's
database is accessed through SQLite's own multi-process locking. What is *not*
naturally shared is the login throttle, and this suite states that as an executable
claim so the deployment documentation cannot drift away from the code.
"""

import re

from sqlmodel import Session, select
from starlette.testclient import TestClient

from proptally.db.system_models import Tenant
from proptally.web.app import create_app
from proptally.web.auth import LOGIN_ATTEMPT_LIMIT


def _start_worker(storage):
    """Starts one worker against the shared storage root, as a real process would."""
    app = create_app(
        system_db_url=f"sqlite:///{storage / 'system.db'}",
        storage_root=str(storage),
    )
    client = TestClient(app)
    client.__enter__()
    return client, app


def test_explicit_storage_root_places_control_and_tenant_paths_together(tmp_path, monkeypatch):
    monkeypatch.delenv("PROPTALLY_SYSTEM_DB_PATH", raising=False)
    storage = tmp_path / "explicit-storage"

    app = create_app(storage_root=str(storage))
    try:
        assert app.state.control_engine.url.database == str(storage / "system.db")
        assert app.state.tenant_manager.paths("tnt_probe").root == storage / "tenants" / "tnt_probe"
    finally:
        app.state.control_engine.dispose()


def _csrf(client, path="/settings"):
    match = re.search(r'name="csrf_token" value="([^"]+)"', client.get(path).text)
    assert match, f"no csrf token on {path}"
    return match.group(1)


def test_session_state_is_shared_between_workers_but_throttling_is_not(tmp_path, monkeypatch):
    """What a second worker shares, and what it does not, stated as an executable claim.

    Sessions live in the control-plane database, so a cookie issued by one worker is
    honoured by another -- registration, sign-in, sign-out and password changes all
    apply across processes. Login throttling is a per-process in-memory counter, so the
    effective allowance is multiplied by the number of workers; that is the one
    behaviour a multi-worker deployment changes, and it is recorded in ADR-010.
    """
    monkeypatch.setenv("PROPTALLY_ALLOW_REGISTRATION", "true")
    storage = tmp_path / "storage"

    first, first_app = _start_worker(storage)
    try:
        second, second_app = _start_worker(storage)
        try:
            # Register on the first worker and carry the cookie to the second.
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

            second.cookies.set("pt_session", first.cookies.get("pt_session"))
            assert second.get("/").status_code == 200, "a session must work on any worker"

            # A sign-out on one worker invalidates the session for every other.
            assert first.post("/auth/logout", data={"csrf_token": _csrf(first)},
                              follow_redirects=False).status_code == 303
            assert second.get("/", follow_redirects=False).status_code == 303

            # Two separate limiters: exhausting one worker's allowance leaves the other
            # unaffected, which is the documented caveat for a multi-worker deployment.
            for _ in range(LOGIN_ATTEMPT_LIMIT):
                first.post(
                    "/auth/login",
                    data={"email": "shared@example.com", "password": "definitely-wrong",
                          "csrf_token": first.cookies.get("pt_csrf") or "x"},
                    follow_redirects=False,
                )

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


def test_two_workers_share_one_control_plane_and_one_set_of_portfolios(tmp_path, monkeypatch):
    """Both processes see the same accounts, portfolios and membership, not two islands."""
    monkeypatch.setenv("PROPTALLY_ALLOW_REGISTRATION", "true")
    storage = tmp_path / "storage"

    first, first_app = _start_worker(storage)
    try:
        second, _second_app = _start_worker(storage)
        try:
            first.get("/auth/login")
            first.post("/auth/register", data={
                "email": "owner@example.com", "password": "owner-password-1",
                "display_name": "Owner", "portfolio_name": "Shared Portfolio",
                "csrf_token": first.cookies.get("pt_csrf"),
            }, follow_redirects=False)
            first.post("/properties/create", data={
                "csrf_token": _csrf(first), "name": "Written By Worker 1",
                "address_line1": "1 Split St", "city": "Austin", "state": "TX",
                "zip_code": "78701", "property_type": "RENTAL_INVESTMENT",
            }, follow_redirects=False)

            # Worker 2 sees the portfolio and the door the other process created.
            second.cookies.set("pt_session", first.cookies.get("pt_session"))
            assert "Shared Portfolio" in second.get("/portfolios").text
            assert "Written By Worker 1" in second.get("/settings").text

            # And a write through worker 2 is visible to worker 1.
            second.post("/properties/create", data={
                "csrf_token": _csrf(second), "name": "Written By Worker 2",
                "address_line1": "2 Split St", "city": "Austin", "state": "TX",
                "zip_code": "78701", "property_type": "RENTAL_INVESTMENT",
            }, follow_redirects=False)
            assert "Written By Worker 2" in first.get("/settings").text

            with Session(first_app.state.control_engine) as session:
                assert len(session.exec(select(Tenant)).all()) == 1, "one control plane"
        finally:
            second.__exit__(None, None, None)
    finally:
        first.__exit__(None, None, None)
