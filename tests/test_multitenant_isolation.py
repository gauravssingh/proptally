"""M2 acceptance: two owners on one instance cannot see or touch each other's data.

The plan's gate for this milestone. Isolation is claimed to be physical (a database
per portfolio), so these tests check the physical facts, not merely that a page
looked empty: raw SQL against the other portfolio's engine must show nothing, and
every request must have been resolved to the right file.

Also covered here, because they are the same trust boundary: unauthenticated access,
CSRF on state-changing requests, session revocation on sign-out, role enforcement,
the invite-only join path, and audit attribution of who did what.
"""

import re

import pytest
from sqlalchemy import text
from sqlmodel import Session, select
from starlette.testclient import TestClient

from proptally.db.models import Document, FinancialAccount, FinancialInstitution, Property
from proptally.web.app import create_app

ALICE = {"email": "alice@example.com", "password": "alice-password-1", "display_name": "Alice", "portfolio_name": "Alice Portfolio"}
BOB = {"email": "bob@example.com", "password": "bob-password-11", "display_name": "Bob", "portfolio_name": "Bob Portfolio"}


@pytest.fixture(name="instance")
def instance_fixture(tmp_path, monkeypatch):
    """One server instance with its own control plane and tenant storage."""
    monkeypatch.delenv("PROPTALLY_ALLOW_REGISTRATION", raising=False)
    monkeypatch.delenv("PROPTALLY_SECURE_COOKIES", raising=False)
    storage = tmp_path / "storage"
    app = create_app(
        system_db_url=f"sqlite:///{storage / 'system.db'}",
        storage_root=str(storage),
    )
    return app


def _register(client: TestClient, payload: dict) -> None:
    """Registers through the real form, starting from the sign-in page."""
    client.get("/auth/login")
    response = client.post(
        "/auth/register",
        data={**payload, "csrf_token": client.cookies.get("pt_csrf")},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text[:500]


def _csrf(client: TestClient) -> str:
    """The signed-in session's CSRF token, read from a rendered form."""
    html = client.get("/account").text
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match, "the shell must expose the session's CSRF token to its forms"
    return match.group(1)


def _tenant_id_for(app, name: str) -> str:
    with Session(app.state.control_engine) as session:
        rows = session.execute(text("SELECT id FROM tenants WHERE name = :n"), {"n": name}).all()
    assert rows, f"no portfolio named {name}"
    return rows[0][0]


def _seed_alice_data(app, tenant_id: str) -> str:
    """Writes a property, account, transaction, photo and document for one portfolio."""
    engine = app.state.tenant_manager.engine_for(tenant_id)
    from proptally.domain.accounting.coa import ensure_entity_ledger_accounts

    # A real file: a path that exists for nobody would make the owner's 404
    # indistinguishable from a cross-tenant denial.
    vault_path = app.state.tenant_manager.paths(tenant_id).vault / "alice-secret.pdf"
    vault_path.parent.mkdir(parents=True, exist_ok=True)
    vault_path.write_bytes(b"%PDF-1.4 alice private statement")

    with Session(engine) as session:
        session.add(FinancialInstitution(id="inst_alice", name="Alice Bank"))
        session.flush()
        session.add(Property(
            id="prop_alice_beach_house", name="Alice Beach House", address_line1="1 Shore Rd",
            city="Galveston", zip_code="77550", property_type="RENTAL_INVESTMENT",
        ))
        session.add(FinancialAccount(
            id="acct_alice_chk", institution_id="inst_alice", account_name="Alice Checking",
            account_type="CHECKING", account_number_masked="...1111",
        ))
        ensure_entity_ledger_accounts(session)
        session.add(Document(
            id="doc_alice_secret", file_sha256="sha_alice_secret",
            original_filename="alice-secret-statement.pdf",
            storage_path=str(vault_path), file_size_bytes=vault_path.stat().st_size,
        ))
        session.commit()

    photos = app.state.tenant_manager.paths(tenant_id).photos / "prop_alice_beach_house"
    photos.mkdir(parents=True, exist_ok=True)
    (photos / "cover.jpg").write_bytes(b"\xff\xd8\xff\xe0alice-private-photo")
    return "prop_alice_beach_house"


def test_each_portfolio_gets_its_own_database_file(instance, monkeypatch):
    """The isolation model is physical: one database per portfolio, per owner."""
    monkeypatch.setenv("PROPTALLY_ALLOW_REGISTRATION", "true")
    with TestClient(instance) as client:
        _register(client, ALICE)
        _register(client, BOB)

    alice_tenant = _tenant_id_for(instance, ALICE["portfolio_name"])
    bob_tenant = _tenant_id_for(instance, BOB["portfolio_name"])
    assert alice_tenant != bob_tenant

    alice_db = instance.state.tenant_manager.paths(alice_tenant).database
    bob_db = instance.state.tenant_manager.paths(bob_tenant).database
    assert alice_db.exists() and bob_db.exists()
    assert alice_db != bob_db

    # Each database carries the standard chart of accounts and its own triggers.
    for path in (alice_db, bob_db):
        with Session(instance.state.tenant_manager.engine_for(path.parent.name)) as session:
            assert session.execute(text("SELECT COUNT(*) FROM ledger_accounts")).scalar() > 0
            triggers = session.execute(
                text("SELECT name FROM sqlite_master WHERE type = 'trigger'")
            ).all()
            assert len(triggers) == 9, "every portfolio database gets the immutability triggers"


def test_endpoints_show_nothing_of_another_portfolio(instance, monkeypatch):
    """Bob sees none of Alice's portfolio, through any route."""
    monkeypatch.setenv("PROPTALLY_ALLOW_REGISTRATION", "true")
    with TestClient(instance) as alice, TestClient(instance) as bob:
        _register(alice, ALICE)
        _register(bob, BOB)
        alice_tenant = _tenant_id_for(instance, ALICE["portfolio_name"])
        bob_tenant = _tenant_id_for(instance, BOB["portfolio_name"])
        alice_property = _seed_alice_data(instance, alice_tenant)

        # Alice sees her own property; Bob does not.
        assert alice.get(f"/properties/{alice_property}").status_code == 200
        assert bob.get(f"/properties/{alice_property}").status_code == 404
        # Alice can fetch her own document, so Bob's 404 below is about ownership.
        assert alice.get("/documents/doc_alice_secret/view").status_code == 200
        assert bob.get("/documents/doc_alice_secret/view").status_code == 404
        assert bob.get(f"/photos/{alice_property}/cover.jpg").status_code == 404
        # Alice can fetch her own photo, so the 404 above is about ownership.
        assert alice.get(f"/photos/{alice_property}/cover.jpg").status_code == 200

        # Bob's dashboard reports an empty portfolio rather than Alice's balances.
        dashboard = bob.get("/")
        assert dashboard.status_code == 200
        assert "Alice" not in dashboard.text

    # The physical check: not one row of Alice's data exists in Bob's database.
    with Session(instance.state.tenant_manager.engine_for(bob_tenant)) as session:
        for table in ("properties", "financial_accounts", "documents", "journal_entries", "transactions"):
            count = session.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar()
            assert count == 0, f"{table} in Bob's database should be empty, found {count}"

    # And Alice's data really is there, so the assertion above is meaningful.
    with Session(instance.state.tenant_manager.engine_for(alice_tenant)) as session:
        assert session.execute(text("SELECT COUNT(*) FROM properties")).scalar() == 1


def test_photo_route_cannot_read_outside_the_portfolios_photo_directory(instance):
    """A crafted filename cannot walk out of the portfolio's own photo store."""
    with TestClient(instance) as alice:
        _register(alice, ALICE)
        tenant = _tenant_id_for(instance, ALICE["portfolio_name"])
        _seed_alice_data(instance, tenant)

        for attempt in (
            "/photos/prop_alice_beach_house/../../system.db",
            "/photos/prop_alice_beach_house/..%2f..%2fsystem.db",
            "/photos/prop_alice_beach_house/%2e%2e%2f%2e%2e%2fsystem.db",
        ):
            assert alice.get(attempt).status_code in (404, 400), attempt


def test_unauthenticated_requests_are_redirected_and_cannot_mutate(instance):
    with TestClient(instance) as anonymous:
        for path in ("/", "/transactions", "/mortgages", "/reconciliation", "/tax/schedule-e", "/account"):
            response = anonymous.get(path, follow_redirects=False)
            assert response.status_code == 303, path
            assert response.headers["location"].startswith("/auth/login"), path

        pre = anonymous.post(
            "/transactions/manual",
            data={"account_id": "x", "transaction_date": "2026-01-01",
                  "description": "unauthenticated", "amount_dollars": "1", "direction": "INFLOW"},
            follow_redirects=False,
        )
        assert pre.status_code == 303  # sent to sign in, nothing written


def test_csrf_is_required_on_state_changing_requests(instance):
    """A signed-in POST without the session's token is refused."""
    with TestClient(instance) as client:
        _register(client, ALICE)

        without_token = client.post(
            "/transactions/manual",
            data={"account_id": "acct_missing", "transaction_date": "2026-01-01",
                  "description": "no csrf", "amount_dollars": "5", "direction": "INFLOW"},
            follow_redirects=False,
        )
        assert without_token.status_code == 403

        stale_token = client.post(
            "/transactions/manual",
            data={"account_id": "acct_missing", "transaction_date": "2026-01-01",
                  "description": "stale csrf", "amount_dollars": "5", "direction": "INFLOW",
                  "csrf_token": "not-the-session-token"},
            follow_redirects=False,
        )
        assert stale_token.status_code == 403


def test_sign_out_revokes_the_session(instance):
    with TestClient(instance) as client:
        _register(client, ALICE)
        token = _csrf(client)
        assert client.get("/").status_code == 200

        client.post("/auth/logout", data={"csrf_token": token}, follow_redirects=False)

        after = client.get("/", follow_redirects=False)
        assert after.status_code == 303
        assert after.headers["location"].startswith("/auth/login")


def test_audit_records_who_posted(instance):
    """The audit trail names the acting user, and no route can invent that."""
    from proptally.db.models import AuditLog, Statement, Transaction
    from proptally.domain.accounting.coa import ensure_entity_ledger_accounts
    from proptally.domain.transactions.splits import (
        SplitItem,
        apply_transaction_splits,
        post_transaction_to_ledger,
    )

    with TestClient(instance) as client:
        _register(client, ALICE)
        tenant = _tenant_id_for(instance, ALICE["portfolio_name"])
        engine = instance.state.tenant_manager.engine_for(tenant)
        with Session(instance.state.control_engine) as control:
            user_id = control.execute(text("SELECT id FROM users WHERE email = :e"), {"e": ALICE["email"]}).scalar()

    with Session(engine) as session:
        session.add(FinancialInstitution(id="inst_a", name="Bank"))
        session.flush()
        session.add(FinancialAccount(id="acct_a_chk", institution_id="inst_a", account_name="Chk",
                                     account_type="CHECKING", account_number_masked="...1"))
        session.add(Document(id="doc_a", file_sha256="sha_a", original_filename="s.pdf",
                             storage_path="/vault/a.pdf", file_size_bytes=1))
        session.flush()
        session.add(Statement(id="stmt_a", document_id="doc_a", financial_account_id="acct_a_chk",
                              statement_type="CHECKING", active_parser_name="T", active_parser_version="1",
                              statement_date=__import__("datetime").date(2026, 1, 31)))
        ensure_entity_ledger_accounts(session)
        session.flush()

        session.add(Transaction(id="txn_a", statement_id="stmt_a", financial_account_id="acct_a_chk",
                                transaction_date=__import__("datetime").date(2026, 1, 5),
                                amount_cents=100_000, direction="INFLOW", raw_description="RENT"))
        session.flush()
        apply_transaction_splits(session, "txn_a", [
            SplitItem(property_id=None, category="RENT_INCOME", amount_cents=100_000,
                      tax_review_status="SELF_CLASSIFIED"),
        ], actor_id=user_id)
        post_transaction_to_ledger(session, "txn_a", actor_id=user_id)
        session.commit()

        entries = session.exec(select(AuditLog).where(AuditLog.entity_name == "journal_entries")).all()
        assert entries, "posting must leave an audit record"
        assert all(row.changed_by == user_id for row in entries)
        assert all(row.changed_by != "system" for row in entries)


def test_registration_closes_once_the_instance_has_an_account(instance):
    """Self-registration is a bootstrap affordance, not an open door by default."""
    with TestClient(instance) as client:
        _register(client, ALICE)
        closed = client.get("/auth/register", follow_redirects=False)
        assert closed.status_code == 303
        assert closed.headers["location"] == "/auth/login"


def test_a_read_only_invitee_can_read_but_not_mutate(instance):
    """Invitation is the only way to join somebody else's portfolio, and the
    invited role bounds what that person can do."""
    from proptally.db.system_models import ROLE_READ_ONLY
    from proptally.web import auth as auth_module

    with TestClient(instance) as alice:
        _register(alice, ALICE)
        tenant = _tenant_id_for(instance, ALICE["portfolio_name"])
        with Session(instance.state.control_engine) as control:
            owner_id = control.execute(text("SELECT id FROM users WHERE email = :e"), {"e": ALICE["email"]}).scalar()

        _invite, token = auth_module.create_invite(
            instance.state.control_engine, tenant_id=tenant,
            email="carol@example.com", role=ROLE_READ_ONLY, invited_by_user_id=owner_id,
        )

    with TestClient(instance) as carol:
        page = carol.get(f"/auth/invite/{token}")
        assert page.status_code == 200
        accepted = carol.post(
            f"/auth/invite/{token}",
            data={"password": "carol-password-1", "display_name": "Carol",
                  "csrf_token": carol.cookies.get("pt_csrf")},
            follow_redirects=False,
        )
        assert accepted.status_code == 303, accepted.text[:400]

        # Read paths work; the mutation does not.
        assert carol.get("/").status_code == 200
        assert carol.get("/transactions").status_code == 200
        blocked = carol.post(
            "/transactions/manual",
            data={"account_id": "acct_missing", "transaction_date": "2026-01-01",
                  "description": "read-only attempt", "amount_dollars": "1", "direction": "INFLOW",
                  "csrf_token": _csrf(carol)},
            follow_redirects=False,
        )
        assert blocked.status_code == 403
