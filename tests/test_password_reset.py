"""Password recovery is operator-issued, never exposed by a public email form."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

from starlette.testclient import TestClient


def _csrf(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match, "the rendered form must carry its CSRF token"
    return match.group(1)


def test_public_reset_never_issues_a_token(owner):
    account = owner(email="reset-target@example.com", password="before-reset-1")

    with TestClient(account.app) as anonymous:
        page = anonymous.get("/auth/reset")
        assert page.status_code == 200
        assert "Ask the super admin" in page.text
        assert 'action="/auth/reset"' not in page.text
        # Written for the person signing in: host tooling is not advertised here.
        assert "scripts/" not in page.text

        # No public endpoint accepts an email and returns its reset token.
        for email in (account.email, "nobody@example.com"):
            response = anonymous.post(
                "/auth/reset",
                data={"email": email},
                follow_redirects=False,
            )
            assert response.status_code == 405


def test_the_box_checks_a_token_before_the_change_password_screen(owner):
    """The reset step answers about the token itself: a live one sends the reader
    on to set a password, a dead one stays on the step with the reason."""
    from proptally.web.auth import issue_password_reset

    account = owner(email="box-token@example.com", password="box-token-1")

    with TestClient(account.app) as anonymous:
        page = anonymous.get("/auth/login")
        assert 'action="/auth/reset/token"' in page.text, "the box posts the token"

        refused = anonymous.post(
            "/auth/reset/token",
            data={"token": "not-a-real-token", "csrf_token": _csrf(page.text)},
            follow_redirects=False,
        )
        assert refused.status_code == 200
        assert "Token not accepted" in refused.text
        assert 'data-pane="signin" hidden' in refused.text, "the step stays open"

        live = issue_password_reset(account.control_engine, account.email)
        assert live is not None
        accepted = anonymous.post(
            "/auth/reset/token",
            data={"token": live, "csrf_token": _csrf(page.text)},
            follow_redirects=False,
        )
        assert accepted.status_code == 303
        assert accepted.headers["location"] == f"/auth/reset/{live}"


def test_operator_cli_issues_a_link_the_account_holder_can_use(owner):
    account = owner(email="reset-target@example.com", password="before-reset-1")
    script = Path(__file__).resolve().parents[1] / "scripts" / "issue_password_reset.py"
    environment = {
        **os.environ,
        "PROPTALLY_SYSTEM_DB_PATH": account.control_engine.url.database,
    }

    issued = subprocess.run(
        [
            sys.executable,
            str(script),
            "--email",
            account.email,
            "--base-url",
            "https://app.example.test",
        ],
        env=environment,
        capture_output=True,
        text=True,
        check=True,
    )
    match = re.search(r"https://app\.example\.test(/auth/reset/[A-Za-z0-9_-]+)", issued.stdout)
    assert match, issued.stdout

    with TestClient(account.app) as account_holder:
        form = account_holder.get(match.group(1))
        changed = account_holder.post(
            match.group(1),
            data={"password": "after-reset-1", "csrf_token": _csrf(form.text)},
            follow_redirects=False,
        )
        assert changed.status_code == 303

        login = account_holder.get("/auth/login")
        signed_in = account_holder.post(
            "/auth/login",
            data={
                "email": account.email,
                "password": "after-reset-1",
                "next": "/",
                "csrf_token": _csrf(login.text),
            },
            follow_redirects=False,
        )
        assert signed_in.status_code == 303
        assert signed_in.headers["location"] == "/"


def test_sign_in_names_the_installation_and_offers_no_self_registration(owner, monkeypatch):
    """An instance that already has an account names itself and offers no way in
    except signing in."""
    monkeypatch.setenv("PROPTALLY_INSTANCE_NAME", "Nathan Property Office")
    account = owner(email="owner@example.com", password="owner-password-1")

    with TestClient(account.app) as anonymous:
        page = anonymous.get("/auth/login").text

    assert "Nathan Property Office" in page
    assert "Create a portfolio" not in page
