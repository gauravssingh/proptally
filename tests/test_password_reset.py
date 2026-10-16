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
        assert "Ask the installation operator" in page.text
        assert "issue_password_reset.py" in page.text
        assert 'action="/auth/reset"' not in page.text

        # No public endpoint accepts an email and returns its reset token.
        for email in (account.email, "nobody@example.com"):
            response = anonymous.post(
                "/auth/reset",
                data={"email": email},
                follow_redirects=False,
            )
            assert response.status_code == 405


def test_a_pasted_token_reaches_the_change_password_screen(owner):
    """The sign-in box cannot build a path segment, so the token arrives as a
    query parameter and is forwarded to the canonical URL."""
    account = owner(email="token-holder@example.com", password="token-holder-1")

    with TestClient(account.app) as anonymous:
        forwarded = anonymous.get(
            "/auth/reset", params={"token": "tok_example"}, follow_redirects=False
        )
        assert forwarded.status_code == 303
        assert forwarded.headers["location"] == "/auth/reset/tok_example"

        # Nothing usable pasted: the instructions render and nothing redirects.
        blank = anonymous.get(
            "/auth/reset", params={"token": "   "}, follow_redirects=False
        )
        assert blank.status_code == 200
        assert "Ask the installation operator" in blank.text


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
