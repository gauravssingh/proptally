"""Workspace identity and switching stay explicit across sign-in and navigation."""

from __future__ import annotations

import re

from sqlmodel import Session, select

from proptally.db.system_models import ROLE_READ_ONLY, Tenant


def _csrf(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match, "a rendered form must expose the CSRF token"
    return match.group(1)

def _invite_token(location: str) -> str:
    match = re.search(r"invite_token=([A-Za-z0-9_-]+)", location)
    assert match, location
    return match.group(1)


def test_multi_membership_sign_in_prompts_for_workspace_and_switches_as_read_only(
    owner, monkeypatch
):
    monkeypatch.setenv("PROPTALLY_ALLOW_REGISTRATION", "true")
    first = owner(email="first@example.com", password="first-password-1", portfolio_name="First")
    second = owner(
        app=first.app,
        email="second@example.com",
        password="second-password-1",
        portfolio_name="Second",
    )

    # Add the first account to the second portfolio through the real invitation flow.
    response = second.post(
        "/account/invites",
        {"email": first.email, "role": ROLE_READ_ONLY},
        follow_redirects=False,
    )
    assert response.status_code == 303
    token = _invite_token(response.headers["location"])
    invitation = first.client.get(f"/auth/invite/{token}")
    accepted = first.client.post(
        f"/auth/invite/{token}",
        data={
            "password": first.password,
            "display_name": "First Account",
            "csrf_token": _csrf(invitation.text),
        },
        follow_redirects=False,
    )
    assert accepted.status_code == 303

    with Session(first.control_engine) as session:
        tenants = {tenant.name: tenant.id for tenant in session.exec(select(Tenant)).all()}
    assert set(tenants) == {"First", "Second"}

    # A fresh sign-in must not silently land in the oldest membership.
    first.client.post("/auth/logout", data={"csrf_token": _csrf(first.client.get("/account").text)})
    login = first.client.get("/auth/login")
    response = first.client.post(
        "/auth/login",
        data={
            "email": first.email,
            "password": first.password,
            "next": "/transactions",
            "csrf_token": _csrf(login.text),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/portfolios?choose=1&")

    chooser = first.client.get(response.headers["location"])
    assert chooser.status_code == 200
    assert "Choose a portfolio" in chooser.text
    assert "First" in chooser.text and "Second" in chooser.text
    assert "Portfolios" in chooser.text and "Current portfolio" in chooser.text
    assert 'class="pt-brand__meta">First</span>' in chooser.text
    assert chooser.text.count('class="pt-workspace-switcher__select"') >= 1
    assert 'href="/static/css/components.css?v=' in chooser.text
    assert 'value="/transactions"' in chooser.text

    # The chooser offers a continuation action for the active portfolio and restores the
    # requested local destination after switching to the other one.
    opened = first.client.post(
        f"/portfolios/{tenants['Second']}/open",
        data={"csrf_token": _csrf(chooser.text), "return_to": "/transactions"},
        follow_redirects=False,
    )
    assert opened.status_code == 303
    assert opened.headers["location"] == "/transactions"
    status = first.client.get("/account?section=portfolio")
    assert status.status_code == 200
    assert "Portfolio status" in status.text
    assert 'value="' + tenants["Second"] + '" selected' in status.text
    assert 'class="pt-brand__meta">Second</span>' in status.text


    # The backup section never hands a read-only member owner-only export rights.
    # Platform-level backup instructions belong to the super admin, and a tenant -- even
    # the first account ever registered -- is not one: they see only that it exists.
    backup = first.client.get("/account?section=backup").text
    assert "Super admin only" in backup
    assert "Download export now" not in backup
    returned = first.client.post(
        "/auth/switch-tenant",
        data={"tenant_id": tenants["First"], "return_to": "/account?section=people",
              "csrf_token": _csrf(first.client.get("/account").text)},
        follow_redirects=False,
    )
    assert returned.status_code == 303
    invitation = first.post(
        "/account/invites", {"email": second.email, "role": ROLE_READ_ONLY},
        follow_redirects=False,
    )
    token = _invite_token(invitation.headers["location"])
    invite_page = second.client.get(f"/auth/invite/{token}")
    accepted = second.client.post(
        f"/auth/invite/{token}",
        data={"password": second.password, "display_name": "Second Account",
              "csrf_token": _csrf(invite_page.text)},
        follow_redirects=False,
    )
    assert accepted.status_code == 303
    backup = second.client.get("/account?section=backup").text
    status = second.client.get("/account?section=portfolio").text
    first_root = str(first.app.state.tenant_manager.paths(tenants["First"]).root)
    assert "Super admin only" in backup
    assert "Who you are" not in backup
    assert "scripts/backup.sh" not in backup
    assert "system.db" not in backup
    assert first_root not in status
    assert "Exact host paths are visible only to the super admin" in status
    signin = second.client.get("/account?section=signin").text
    assert "Who you are" in signin
    assert "Super admin" in signin and "No" in signin

def test_read_only_member_can_switch_workspaces_from_the_shell(owner, monkeypatch):
    monkeypatch.setenv("PROPTALLY_ALLOW_REGISTRATION", "true")
    first = owner(email="reader@example.com", password="reader-password-1", portfolio_name="Read-only home")
    second = owner(
        app=first.app,
        email="other-owner@example.com",
        password="other-password-1",
        portfolio_name="Other workspace",
    )
    invitation = second.post(
        "/account/invites",
        {"email": first.email, "role": ROLE_READ_ONLY},
        follow_redirects=False,
    )
    token = _invite_token(invitation.headers["location"])
    invite_page = first.client.get(f"/auth/invite/{token}")
    accepted = first.client.post(
        f"/auth/invite/{token}",
        data={"password": first.password, "display_name": "Reader",
              "csrf_token": _csrf(invite_page.text)},
        follow_redirects=False,
    )
    assert accepted.status_code == 303

    with Session(first.control_engine) as session:
        other_id = session.exec(select(Tenant).where(Tenant.name == "Other workspace")).one().id
    switched = first.client.post(
        "/auth/switch-tenant",
        data={"tenant_id": other_id, "return_to": "/account?section=portfolio",
              "csrf_token": _csrf(first.client.get("/account").text)},
        follow_redirects=False,
    )
    assert switched.status_code == 303
    assert switched.headers["location"] == "/account?section=portfolio"
    page = first.client.get(switched.headers["location"]).text
    assert "Other workspace" in page
    assert "READ_ONLY" in page
