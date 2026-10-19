"""User management: several people, one portfolio, and one way out.

A portfolio can be shared: an owner adds somebody, and that person signs in with their
own account and sees only that portfolio. These tests cover the access list an owner
actually administers — who is in, in what role, and how access ends — plus the two
controls that must always be reachable after signing in: the account screen and sign-out.

The invariant underneath all of it: a portfolio always keeps at least one owner, because
a portfolio nobody owns cannot be administered at all.
"""

import re

from sqlmodel import Session
from starlette.testclient import TestClient

from proptally.db.system_models import (
    INVITABLE_ROLES,
    ROLE_CPA_REVIEWER,
    ROLE_MANAGER,
    ROLE_OWNER,
    ROLE_READ_ONLY,
    ROLES,
)
from proptally.web import navigation


def _csrf(client, path="/account"):
    match = re.search(r'name="csrf_token" value="([^"]+)"', client.get(path).text)
    assert match, f"no csrf token on {path}"
    return match.group(1)


def _invite_and_accept(owner_tenant, invitee, role: str, email: str = "colleague@example.com"):
    """Issues an invitation from an owner's session and accepts it as the invitee."""
    response = owner_tenant.post(
        "/account/invites", {"email": email, "role": role}, follow_redirects=False
    )
    assert response.status_code == 303, response.text[:300]
    token = re.search(r"invite_token=([A-Za-z0-9_\-]+)", response.headers["location"])
    assert token, response.headers["location"]

    invitee.client.get("/auth/login")
    accepted = invitee.client.post(
        f"/auth/invite/{token.group(1)}",
        data={"password": invitee.password, "csrf_token": invitee.client.cookies.get("pt_csrf")},
        follow_redirects=False,
    )
    assert accepted.status_code == 303, accepted.text[:300]
    return invitee


def test_an_owner_can_see_who_has_access_and_invite_somebody(owner, monkeypatch):
    """The access list names each person, their role, and their pending invitations."""
    monkeypatch.setenv("PROPTALLY_ALLOW_REGISTRATION", "true")
    keeper = owner(email="keeper@example.com", portfolio_name="Shared Portfolio")
    colleague = owner(app=keeper.app, email="colleague@example.com",
                      password="colleague-password-1", portfolio_name="Colleague Portfolio")

    page = keeper.client.get("/account")
    assert page.status_code == 200
    assert "People with access to Shared Portfolio" in page.text
    assert "keeper@example.com" in page.text
    assert "OWNER" in page.text
    assert "the portfolio's only owner" in page.text

    # An owner invites, and the invitation is listed until it is accepted.
    invited = keeper.post("/account/invites", {"email": "colleague@example.com",
                                               "role": ROLE_READ_ONLY},
                          follow_redirects=False)
    assert invited.status_code == 303
    assert "colleague@example.com" in keeper.client.get("/account").text
    assert "Invitations not yet accepted" in keeper.client.get("/account").text

    _invite_and_accept(keeper, colleague, ROLE_CPA_REVIEWER)

    # The colleague is now a member, with the role they were invited as, and their own
    # session sees this portfolio.
    roster = keeper.client.get("/account").text
    assert "colleague@example.com" in roster
    assert "CPA_REVIEWER" in roster
    assert "Shared Portfolio" in colleague.client.get("/portfolios").text
    assert colleague.client.get("/").status_code == 200


def test_an_owner_can_change_a_role_and_remove_access(owner, monkeypatch):
    """Roles are editable, and removing access does not delete the person's account."""
    monkeypatch.setenv("PROPTALLY_ALLOW_REGISTRATION", "true")
    keeper = owner(email="keeper@example.com", portfolio_name="Shared Portfolio")
    colleague = owner(app=keeper.app, email="colleague@example.com",
                      password="colleague-password-1", portfolio_name="Colleague Portfolio")
    _invite_and_accept(keeper, colleague, ROLE_READ_ONLY)

    with Session(keeper.control_engine) as session:
        from sqlmodel import select

        from proptally.db.system_models import TenantMembership

        membership = session.exec(
            select(TenantMembership).where(
                TenantMembership.tenant_id == keeper.tenant_id,
                TenantMembership.role == ROLE_READ_ONLY,
            )
        ).one()
        membership_id = membership.id
        colleague_user_id = membership.user_id

    promoted = keeper.post(f"/account/members/{membership_id}/role",
                           {"role": ROLE_CPA_REVIEWER}, follow_redirects=False)
    assert promoted.status_code == 303
    assert "CPA_REVIEWER" in keeper.client.get("/account").text

    # The colleague's own session now carries the new role, since it is read per request.
    assert "CPA_REVIEWER" in colleague.client.get("/portfolios").text

    removed = keeper.post(f"/account/members/{membership_id}/remove", {},
                          follow_redirects=False)
    assert removed.status_code == 303
    assert "colleague@example.com" not in keeper.client.get("/account").text

    # The account survives; only the membership is gone.
    with Session(keeper.control_engine) as session:
        from proptally.db.system_models import TenantMembership, User

        assert session.get(User, colleague_user_id) is not None
        assert session.exec(
            select(TenantMembership).where(TenantMembership.id == membership_id)
        ).first() is None


def test_the_last_owner_cannot_be_demoted_or_removed(owner):
    """A portfolio nobody owns cannot be administered, so it keeps its owner."""
    keeper = owner(email="keeper@example.com", portfolio_name="Solo Portfolio")
    page = keeper.client.get("/account").text
    # The screen does not even offer the controls, and explains itself.
    assert "the portfolio's only owner" in page
    assert "Remove</button>" not in page
    assert "Change</button>" not in page

    with Session(keeper.control_engine) as session:
        from sqlmodel import select

        from proptally.db.system_models import TenantMembership

        membership_id = session.exec(
            select(TenantMembership).where(TenantMembership.tenant_id == keeper.tenant_id)
        ).one().id

    demoted = keeper.post(f"/account/members/{membership_id}/role", {"role": ROLE_READ_ONLY},
                          follow_redirects=False)
    assert demoted.status_code == 303
    assert "error=" in demoted.headers["location"]
    assert "only" in demoted.headers["location"].lower()

    removed = keeper.post(f"/account/members/{membership_id}/remove", {},
                          follow_redirects=False)
    assert removed.status_code == 303 and "error=" in removed.headers["location"]

    # Still an owner, still signed in and working.
    assert "OWNER" in keeper.client.get("/account").text
    assert keeper.client.get("/").status_code == 200


def test_a_non_owner_cannot_change_the_access_list(owner, monkeypatch):
    """Reading the roster is fine; changing it is not, and the ids cannot be borrowed."""
    monkeypatch.setenv("PROPTALLY_ALLOW_REGISTRATION", "true")
    keeper = owner(email="keeper@example.com", portfolio_name="Shared Portfolio")
    reviewer = owner(app=keeper.app, email="reviewer@example.com",
                     password="reviewer-password-1", portfolio_name="Reviewer Portfolio")
    _invite_and_accept(keeper, reviewer, ROLE_CPA_REVIEWER)

    # A reviewer sees who is in the portfolio but no controls.
    page = reviewer.client.get("/account").text
    assert "People with access" in page
    assert "keeper@example.com" in page
    assert "Only an owner can invite people or change roles" in page

    with Session(keeper.control_engine) as session:
        from sqlmodel import select

        from proptally.db.system_models import TenantMembership

        owner_membership = session.exec(
            select(TenantMembership).where(
                TenantMembership.tenant_id == keeper.tenant_id,
                TenantMembership.role == ROLE_OWNER,
            )
        ).one().id

    # Even knowing the membership id, a reviewer cannot use it.
    for path, payload in (
        (f"/account/members/{owner_membership}/role", {"role": ROLE_READ_ONLY}),
        (f"/account/members/{owner_membership}/remove", {}),
        ("/account/invites", {"email": "someone@example.com", "role": ROLE_READ_ONLY}),
    ):
        response = reviewer.post(path, payload, follow_redirects=False)
        assert response.status_code == 403, (path, response.status_code)

    # Nothing changed.
    assert "OWNER" in keeper.client.get("/account").text


def test_a_membership_from_another_portfolio_cannot_be_reached(owner, monkeypatch):
    """The id alone must never be enough: access lists are portfolio-scoped."""
    monkeypatch.setenv("PROPTALLY_ALLOW_REGISTRATION", "true")
    first = owner(email="first@example.com", portfolio_name="First Portfolio")
    second = owner(app=first.app, email="second@example.com",
                   password="second-password-1", portfolio_name="Second Portfolio")

    with Session(second.control_engine) as session:
        from sqlmodel import select

        from proptally.db.system_models import TenantMembership

        other_id = session.exec(
            select(TenantMembership).where(TenantMembership.tenant_id == second.tenant_id)
        ).one().id

    for path, payload in (
        (f"/account/members/{other_id}/role", {"role": ROLE_READ_ONLY}),
        (f"/account/members/{other_id}/remove", {}),
    ):
        response = first.post(path, payload, follow_redirects=False)
        assert response.status_code == 303, path
        assert "error=" in response.headers["location"]

    # Second's owner still owns their portfolio.
    assert "OWNER" in second.client.get("/account").text


def test_an_owner_can_withdraw_an_invitation(owner):
    """A withdrawn invitation stops working."""
    keeper = owner(email="keeper@example.com", portfolio_name="Shared Portfolio")
    keeper.post("/account/invites", {"email": "gone@example.com", "role": ROLE_READ_ONLY},
                follow_redirects=False)

    with Session(keeper.control_engine) as session:
        from sqlmodel import select

        from proptally.db.system_models import UserInvite

        invite_id = session.exec(select(UserInvite)).one().id

    withdrawn = keeper.post(f"/account/invites/{invite_id}/revoke", {},
                            follow_redirects=False)
    assert withdrawn.status_code == 303
    assert "gone@example.com" not in keeper.client.get("/account").text

    with TestClient(keeper.app) as anonymous:
        page = anonymous.get("/auth/invite/does-not-matter")
        assert page.status_code == 200  # renders the "invalid invitation" path, not a crash


def test_the_account_screen_and_sign_out_are_reachable_from_the_shell(owner):
    """After signing in, both controls must be one click away, on every page."""
    keeper = owner(email="keeper@example.com", portfolio_name="Solo Portfolio")

    dashboard = keeper.client.get("/").text
    assert 'href="/account"' in dashboard or "/account" in dashboard, "account must be linked"
    assert 'href="/account?section=signin"' in dashboard, "the identity row must lead to personal sign-in settings"
    assert 'action="/auth/logout"' in dashboard, "sign out must be reachable without a URL"
    assert "Sign out" in dashboard

    # The navigation names it too: whatever the destination is called, the shell renders
    # the label the navigation module declares for it.
    administration = [
        item
        for section in navigation.primary_sections() + navigation.financial_sections()
        for item in section.items
        if item.href == "/account"
    ]
    assert administration, "the administration screen must be a named navigation destination"
    assert administration[0].label in dashboard, (
        f"the shell must render the navigation label {administration[0].label!r}"
    )

    # And signing out from there really signs out.
    response = keeper.client.post(
        "/auth/logout", data={"csrf_token": _csrf(keeper.client)}, follow_redirects=False
    )
    assert response.status_code == 303
    after = keeper.client.get("/", follow_redirects=False)
    assert after.status_code == 303 and after.headers["location"].startswith("/auth/login")


def test_the_role_controls_offer_exactly_the_roles_they_may_hand_out(owner, monkeypatch):
    """The role dropdowns are the only way a person chooses a role, so they must be filled.

    Regression this pins: the invite form's options were rendered from a name the template no
    longer defined, so the select came out empty while the POST routes kept working — every
    other test posts a role value directly and so never saw the empty dropdown.
    """
    monkeypatch.setenv("PROPTALLY_ALLOW_REGISTRATION", "true")
    keeper = owner(email="keeper@example.com", portfolio_name="Solo Portfolio")
    page = keeper.client.get("/account").text

    invite_form = page.split('id="invite-member-modal"', 1)[1].split("</form>", 1)[0]
    invite_options = dict(re.findall(r'<option value="([A-Z_]+)">(.*?)</option>', invite_form))
    assert tuple(invite_options) == INVITABLE_ROLES, (
        "the invitation form must offer exactly the roles an invitation may carry"
    )
    assert "Bookkeeping + setup" in invite_options[ROLE_MANAGER]
    assert "verified review" in invite_options[ROLE_CPA_REVIEWER]
    assert "View only" in invite_options[ROLE_READ_ONLY]

    manager_invite = keeper.post(
        "/account/invites", {"email": "manager@example.com", "role": ROLE_MANAGER},
        follow_redirects=False,
    )
    manager_token = re.search(r"invite_token=([A-Za-z0-9_-]+)", manager_invite.headers["location"]).group(1)
    with TestClient(keeper.app) as invitee:
        manager_page = invitee.get(f"/auth/invite/{manager_token}").text
        assert "Do bookkeeping and portfolio setup" in manager_page
        assert "record a verified external review" not in manager_page

        reader_invite = keeper.post(
            "/account/invites", {"email": "reader@example.com", "role": ROLE_READ_ONLY},
            follow_redirects=False,
        )
        reader_token = re.search(r"invite_token=([A-Za-z0-9_-]+)", reader_invite.headers["location"]).group(1)
        reader_page = invitee.get(f"/auth/invite/{reader_token}").text
        assert "cannot change it or export the portfolio" in reader_page
    # A member who is not the portfolio's only owner gets a role control, and that control is
    # how ownership is granted, so it offers every role.
    _invite_and_accept(keeper, owner(app=keeper.app, email="member@example.com",
                                     password="member-password-1", portfolio_name="Member"),
                       ROLE_CPA_REVIEWER, email="member@example.com")
    roster = keeper.client.get("/account").text
    roster = roster.split("People with access", 1)[1].split("What each role can do", 1)[0]
    assert tuple(re.findall(r'<option value="([A-Z_]+)"', roster)) == ROLES, (
        "the access list must be able to promote a member to any role, including owner"
    )
