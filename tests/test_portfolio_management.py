"""Cross-portfolio view and portfolio export.

An account can belong to several portfolios, so the application needs one place that
looks across them without ever showing a portfolio the account is not a member of.
Export is the other half of the same promise: a portfolio is one database plus its
documents, so an owner can be handed everything they have.

These tests check the boundaries rather than the rendering: what a member may see,
what an owner alone may take away, and that an archive of one portfolio contains no
trace of another.
"""

import io
import json
import shutil
import tarfile
from uuid import uuid4

from sqlalchemy import text
from sqlmodel import Session, select
from starlette.testclient import TestClient

from proptally.db.models import FinancialInstitution, Property
from proptally.db.system_models import ROLE_READ_ONLY
from proptally.domain.accounting.coa import ensure_entity_ledger_accounts
from proptally.web import auth as auth_module


def _add_property(owner, name: str) -> str:
    """Creates a door directly in the portfolio, without going through the forms."""
    with Session(owner.engine) as session:
        session.add(FinancialInstitution(id=f"inst_{uuid4().hex[:8]}", name="Bank"))
        session.flush()
        prop = Property(
            id=f"prop_{uuid4().hex[:10]}", name=name, address_line1=f"{name} St",
            city="Austin", state="TX", zip_code="78701", property_type="RENTAL_INVESTMENT",
        )
        session.add(prop)
        session.flush()
        ensure_entity_ledger_accounts(session)
        session.commit()
        return prop.id


def _invite(owner, tenant_id: str, email: str, role: str) -> str:
    """Invites somebody to a portfolio and returns the accept token."""
    with Session(owner.control_engine) as session:
        owner_id = session.execute(
            text("SELECT id FROM users WHERE email = :e"), {"e": owner.email}
        ).scalar()
    _row, token = auth_module.create_invite(
        owner.control_engine, tenant_id=tenant_id, email=email, role=role,
        invited_by_user_id=owner_id,
    )
    return token


def test_the_view_lists_only_portfolios_the_account_belongs_to(owner, monkeypatch):
    """A member sees their own portfolios and nothing else, whatever the request asks."""
    monkeypatch.setenv("PROPTALLY_ALLOW_REGISTRATION", "true")
    alice = owner(portfolio_name="Alice Portfolio")
    _add_property(alice, "Alice Duplex")

    # Bob belongs to his own portfolio only, on the same instance.
    bob = owner(app=alice.app, email="bob@example.com", password="bob-password-11",
                portfolio_name="Bob Portfolio")

    alice_page = alice.client.get("/portfolios").text
    assert "Alice Portfolio" in alice_page
    assert "Bob Portfolio" not in alice_page

    # Bob's page lists his own portfolio and nothing of Alice's — neither her
    # portfolio row nor, because his navigation shows only his active portfolio, her
    # doors.
    bob_page = bob.client.get("/portfolios").text
    assert "Bob Portfolio" in bob_page
    assert "Alice Portfolio" not in bob_page
    assert "Alice Duplex" not in bob_page


def test_a_member_of_two_portfolios_can_see_and_switch_between_them(owner, monkeypatch):
    """An account in two portfolios sees both, with its own role in each, and can switch.

    Accepting an invitation makes the new portfolio active. A read-only member must
    still be able to switch away again, so the switch is permitted on the strength of
    membership rather than of a mutating role -- it changes which portfolio the
    session views, not any portfolio's data.
    """
    monkeypatch.setenv("PROPTALLY_ALLOW_REGISTRATION", "true")
    owner_a = owner(portfolio_name="Owner Portfolio")
    other = owner(app=owner_a.app, email="other@example.com", password="other-password-1",
                  portfolio_name="Other Portfolio")

    token = _invite(other, other.tenant_id, owner_a.email, ROLE_READ_ONLY)
    accepted = owner_a.client.post(
        f"/auth/invite/{token}",
        data={"password": owner_a.password, "csrf_token": owner_a.client.cookies.get("pt_csrf")},
        follow_redirects=False,
    )
    assert accepted.status_code == 303, accepted.text[:300]

    page = owner_a.client.get("/portfolios").text
    assert "Owner Portfolio" in page and "Other Portfolio" in page
    assert "READ_ONLY" in page
    # The portfolio that is not active offers a switch to it, and no export.
    assert f"/portfolios/{owner_a.tenant_id}/open" in page
    assert f"/portfolios/{other.tenant_id}/export" not in page

    # Switching back to their own portfolio works despite the read-only role elsewhere.
    back = owner_a.post(f"/portfolios/{owner_a.tenant_id}/open", {}, follow_redirects=False)
    assert back.status_code == 303 and back.headers["location"] == "/"
    assert owner_a.client.get("/").status_code == 200

    # A portfolio the account is not a member of cannot be opened by asking for it.
    outsider = owner(app=owner_a.app, email="outsider@example.com", password="outsider-pass-1",
                     portfolio_name="Outsider Portfolio")
    refused = outsider.post(f"/portfolios/{other.tenant_id}/open", {}, follow_redirects=False)
    assert refused.status_code == 303
    assert "error=" in refused.headers["location"]
    assert outsider.client.get("/").status_code == 200  # still on their own portfolio


def test_export_returns_one_portfolio_and_nothing_else(owner, monkeypatch):
    """The archive is that portfolio: its database and documents, no other portfolio."""
    monkeypatch.setenv("PROPTALLY_ALLOW_REGISTRATION", "true")
    alice = owner(portfolio_name="Alice Portfolio")
    _add_property(alice, "Alice Duplex")
    bob = owner(app=alice.app, email="bob@example.com", password="bob-password-11",
                portfolio_name="Bob Portfolio")
    _add_property(bob, "Bob Barn")

    # Put a document in Alice's vault so the archive has something to carry.
    vault = alice.app.state.tenant_manager.paths(alice.tenant_id).vault
    vault.mkdir(parents=True, exist_ok=True)
    (vault / "statement.pdf").write_bytes(b"%PDF-1.4 alice private statement")

    response = alice.client.get(f"/portfolios/{alice.tenant_id}/export")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/gzip"

    with tarfile.open(fileobj=io.BytesIO(response.content), mode="r:gz") as archive:
        names = archive.getnames()
        assert "proptally.db" in names
        assert any(name.startswith("vault/") for name in names)
        assert "manifest.json" in names

        manifest_member = archive.extractfile("manifest.json")
        assert manifest_member is not None
        manifest = json.loads(manifest_member.read())
        assert manifest["tenant_id"] == alice.tenant_id
        assert manifest["portfolio_name"] == "Alice Portfolio"

        database_member = archive.extractfile("proptally.db")
        assert database_member is not None
        database_bytes = database_member.read()

    # The exported database holds Alice's portfolio and no trace of Bob's.
    assert b"Alice Duplex" in database_bytes
    assert b"Bob Barn" not in database_bytes


def test_export_is_owner_only_and_hidden_from_other_roles(owner, monkeypatch):
    """A read-only member can read the portfolio but cannot take it away."""
    monkeypatch.setenv("PROPTALLY_ALLOW_REGISTRATION", "true")
    alice = owner(portfolio_name="Alice Portfolio")
    _add_property(alice, "Alice Duplex")

    token = _invite(alice, alice.tenant_id, "carol@example.com", ROLE_READ_ONLY)
    carol = owner(
        app=alice.app, email="carol@example.com", password="carol-password-1",
        portfolio_name="Carol Portfolio",
    )
    accepted = carol.client.post(
        f"/auth/invite/{token}",
        data={"password": "carol-password-1", "csrf_token": carol.client.cookies.get("pt_csrf")},
        follow_redirects=False,
    )
    assert accepted.status_code == 303, accepted.text[:300]

    page = carol.client.get("/portfolios").text
    assert "Alice Portfolio" in page
    assert f"/portfolios/{alice.tenant_id}/export" not in page, "no export control for a read-only member"

    refused = carol.client.get(f"/portfolios/{alice.tenant_id}/export")
    assert refused.status_code == 403


def test_export_of_a_portfolio_you_do_not_belong_to_is_not_found(owner, monkeypatch):
    """A portfolio id from elsewhere is simply not found, not forbidden by name."""
    monkeypatch.setenv("PROPTALLY_ALLOW_REGISTRATION", "true")
    alice = owner(portfolio_name="Alice Portfolio")
    bob = owner(app=alice.app, email="bob@example.com", password="bob-password-11",
                portfolio_name="Bob Portfolio")

    response = bob.client.get(f"/portfolios/{alice.tenant_id}/export")
    assert response.status_code == 404

    # An id that is not a portfolio at all behaves the same way.
    assert bob.client.get("/portfolios/tnt_nonexistent/export").status_code == 404


def test_the_view_and_export_require_a_session(owner, monkeypatch):
    """Neither page is reachable without signing in, and the export is not a POST."""
    monkeypatch.setenv("PROPTALLY_ALLOW_REGISTRATION", "true")
    alice = owner(portfolio_name="Alice Portfolio")

    with TestClient(alice.app) as anon:
        listed = anon.get("/portfolios", follow_redirects=False)
        assert listed.status_code == 303
        assert listed.headers["location"].startswith("/auth/login")
        exported = anon.get(f"/portfolios/{alice.tenant_id}/export", follow_redirects=False)
        assert exported.status_code == 303


def test_an_unopenable_portfolio_is_reported_not_omitted(owner, monkeypatch):
    """A member is never shown a shorter list than they belong to.

    A portfolio whose storage is missing (moved, half-restored, or on a volume that
    did not mount) must appear as unavailable. Silently omitting it would tell the
    member it does not exist.
    """
    monkeypatch.setenv("PROPTALLY_ALLOW_REGISTRATION", "true")
    alice = owner(portfolio_name="Alice Portfolio")

    # A membership for a portfolio that was never provisioned on this root.
    from uuid import uuid4

    from proptally.db.system_models import ROLE_OWNER, Tenant, TenantMembership

    with Session(alice.control_engine) as session:
        session.add(Tenant(id=f"tnt_{uuid4().hex[:16]}", name="Ghost Portfolio", slug="ghost"))
        session.flush()
        user_id = session.execute(
            text("SELECT id FROM users WHERE email = :e"), {"e": alice.email}
        ).scalar()
        session.add(TenantMembership(
            id=f"mem_{uuid4().hex[:16]}", user_id=user_id,
            tenant_id="ghost" and [t.id for t in session.exec(select(Tenant)).all() if t.slug == "ghost"][0],
            role=ROLE_OWNER,
        ))
        session.commit()

    response = alice.client.get("/portfolios")
    assert response.status_code == 200
    assert "Alice Portfolio" in response.text
    assert "Ghost Portfolio" in response.text
    assert "could not be opened" in response.text


def _owner_who_can_remove_their_portfolio(owner, monkeypatch, *, keep_name="Keep Portfolio",
                                          doomed_name="Doomed Portfolio"):
    """A keeper, and a second owner who may legitimately remove their own portfolio.

    An account is refused removal when it would be left with nothing to sign in to, so
    the second owner is also made a member of the keeper's portfolio first.
    """
    monkeypatch.setenv("PROPTALLY_ALLOW_REGISTRATION", "true")
    keeper = owner(portfolio_name=keep_name)
    doomed = owner(app=keeper.app, email="doomed@example.com", password="doomed-pass-1",
                   portfolio_name=doomed_name)
    token = _invite(keeper, keeper.tenant_id, doomed.email, ROLE_READ_ONLY)
    accepted = doomed.client.post(
        f"/auth/invite/{token}",
        data={"password": "doomed-pass-1", "csrf_token": doomed.client.cookies.get("pt_csrf")},
        follow_redirects=False,
    )
    assert accepted.status_code == 303, accepted.text[:300]
    return keeper, doomed


def test_deleting_a_portfolio_removes_its_files_and_its_rows(owner, monkeypatch):
    """Removal takes the portfolio's database and its control-plane rows with it."""
    alice, doomed = _owner_who_can_remove_their_portfolio(owner, monkeypatch)
    _add_property(doomed, "Doomed Duplex")

    tenant_manager = alice.app.state.tenant_manager
    doomed_root = tenant_manager.paths(doomed.tenant_id).root
    assert doomed_root.is_dir()

    # Alice is an owner of her own portfolio and not a member of the doomed one, so she
    # cannot even see its removal page.
    assert alice.client.get(f"/portfolios/{doomed.tenant_id}/delete").status_code == 404

    page = doomed.client.get(f"/portfolios/{doomed.tenant_id}/delete")
    assert page.status_code == 200
    assert "Doomed Portfolio" in page.text
    assert "What would be destroyed" in page.text
    assert "Download an export now" in page.text, "an export must be offered before removal"

    # A wrong confirmation is refused and nothing is removed.
    refused = doomed.post(
        f"/portfolios/{doomed.tenant_id}/delete", {"confirm_name": "Not The Name"},
        follow_redirects=False,
    )
    assert refused.status_code == 303
    assert "error=" in refused.headers["location"]
    assert doomed_root.is_dir(), "a refused confirmation must not remove anything"
    assert doomed.client.get("/portfolios").status_code == 200

    # With the name typed exactly, the portfolio goes.
    removed = doomed.post(
        f"/portfolios/{doomed.tenant_id}/delete", {"confirm_name": "Doomed Portfolio"},
        follow_redirects=False,
    )
    assert removed.status_code == 303 and removed.headers["location"] == "/portfolios"
    assert not doomed_root.exists(), "the portfolio's files must be gone"

    with Session(doomed.control_engine) as session:
        from proptally.db.system_models import Tenant, TenantMembership

        assert session.get(Tenant, doomed.tenant_id) is None
        assert not session.exec(
            select(TenantMembership).where(TenantMembership.tenant_id == doomed.tenant_id)
        ).all()
    assert "Doomed Portfolio" not in doomed.client.get("/portfolios").text


def test_the_last_portfolio_cannot_be_removed(owner, monkeypatch):
    """An account with nothing left to sign in to is refused, not stranded."""
    monkeypatch.setenv("PROPTALLY_ALLOW_REGISTRATION", "true")
    alice = owner(portfolio_name="Only Portfolio")

    page = alice.client.get(f"/portfolios/{alice.tenant_id}/delete")
    assert page.status_code == 200

    refused = alice.post(
        f"/portfolios/{alice.tenant_id}/delete", {"confirm_name": "Only Portfolio"},
        follow_redirects=False,
    )
    assert refused.status_code == 303
    assert "error=" in refused.headers["location"]
    assert alice.app.state.tenant_manager.paths(alice.tenant_id).root.is_dir()
    assert "Only Portfolio" in alice.client.get("/portfolios").text


def test_a_read_only_member_cannot_remove_a_portfolio(owner, monkeypatch):
    """Neither the page nor the action is available without ownership."""
    monkeypatch.setenv("PROPTALLY_ALLOW_REGISTRATION", "true")
    alice = owner(portfolio_name="Alice Portfolio")
    token = _invite(alice, alice.tenant_id, "viewer@example.com", ROLE_READ_ONLY)
    viewer = owner(app=alice.app, email="viewer@example.com", password="viewer-pass-1",
                   portfolio_name="Viewer Portfolio")
    accepted = viewer.client.post(
        f"/auth/invite/{token}",
        data={"password": "viewer-pass-1", "csrf_token": viewer.client.cookies.get("pt_csrf")},
        follow_redirects=False,
    )
    assert accepted.status_code == 303, accepted.text[:300]

    page = viewer.client.get("/portfolios").text
    assert f"/portfolios/{alice.tenant_id}/delete" not in page, "no removal control for a read-only member"
    assert viewer.client.get(f"/portfolios/{alice.tenant_id}/delete").status_code == 403
    # The viewer owns their own portfolio, so a read-only membership elsewhere is the
    # only thing stopping this; the role check is what refuses it.
    refused = viewer.post(
        f"/portfolios/{alice.tenant_id}/delete", {"confirm_name": "Alice Portfolio"},
        follow_redirects=False,
    )
    assert refused.status_code == 403
    assert alice.app.state.tenant_manager.paths(alice.tenant_id).root.is_dir()


def test_deleting_a_portfolio_repaints_the_sessions_looking_at_it(owner, monkeypatch):
    """The member's session moves to a portfolio they still have, not into a broken state."""
    keeper, doomed = _owner_who_can_remove_their_portfolio(owner, monkeypatch)

    # Their session is now looking at the keeper's portfolio; switch back to their own
    # and delete it while it is the active one.
    back = doomed.post(f"/portfolios/{doomed.tenant_id}/open", {}, follow_redirects=False)
    assert back.status_code == 303
    assert doomed.client.get("/").status_code == 200

    removed = doomed.post(
        f"/portfolios/{doomed.tenant_id}/delete", {"confirm_name": "Doomed Portfolio"},
        follow_redirects=False,
    )
    assert removed.status_code == 303 and removed.headers["location"] == "/portfolios"

    # The active portfolio was the one removed, so the session was repointed and the
    # next page load works rather than failing.
    assert doomed.client.get("/").status_code == 200
    assert doomed.client.get("/portfolios").status_code == 200
