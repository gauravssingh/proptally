"""What each role may actually do, and who may authorize what.

The roles differ by *authority*, not by whether they can touch the data:

* an owner and a manager both do the bookkeeping, but only the owner may administer the
  portfolio or authorize on the client's own behalf;
* a reviewer does the bookkeeping too -- their attestation is delivered by posting the
  entry that needs it -- and only their review is recorded as a *verified* external one;
* a read-only member changes nothing, but can still manage their own account.

These tests drive the real routes, because the point is that no route is left able to
manufacture an authorization the caller does not hold.
"""

import re
from datetime import date
from uuid import uuid4

from sqlalchemy import text
from sqlmodel import Session, select

from proptally.db.models import (
    Document,
    FinancialAccount,
    FinancialInstitution,
    JournalEntry,
    PostingAuthorization,
    Property,
    Statement,
    Transaction,
)
from proptally.db.system_models import (
    ROLE_CPA_REVIEWER,
    ROLE_MANAGER,
    ROLE_OWNER,
    ROLE_READ_ONLY,
)
from test_user_management import _invite_and_accept


def _csrf(client, path="/account"):
    match = re.search(r'name="csrf_token" value="([^"]+)"', client.get(path).text)
    assert match, f"no csrf token on {path}"
    return match.group(1)


def _seed(tenant, *, kind: str) -> tuple:
    """Seeds a transaction the policy gates, and the door it belongs to.

    ``kind='deposit'``  -- an unallocated inflow only the client may authorize (CBD-1).
    ``kind='card'``     -- a purchase on a mixed-use card, needing external review (CBD-2).
    """
    from proptally.domain.accounting.coa import ensure_entity_ledger_accounts

    with Session(tenant.engine) as session:
        session.add(FinancialInstitution(id=f"inst_{uuid4().hex[:8]}", name="Bank"))
        session.flush()
        prop = Property(
            id=f"prop_{uuid4().hex[:8]}", name="Seeded Door", address_line1="1 Seed St",
            city="Austin", state="TX", zip_code="78701", property_type="RENTAL_INVESTMENT",
        )
        session.add(prop)
        account = FinancialAccount(
            id=f"acct_{uuid4().hex[:8]}",
            institution_id=session.exec(select(FinancialInstitution)).first().id,
            account_name="Seeded Card" if kind == "card" else "Seeded Checking",
            account_type="CREDIT_CARD" if kind == "card" else "CHECKING",
            account_number_masked="...1",
            mixed_use_status="MIXED_USE" if kind == "card" else "UNCLASSIFIED",
        )
        session.add(account)
        session.add(Document(
            id=f"doc_{uuid4().hex[:8]}", file_sha256=uuid4().hex, original_filename="s.pdf",
            storage_path="/vault/s.pdf", file_size_bytes=1,
        ))
        session.flush()
        session.add(Statement(
            id=f"stmt_{uuid4().hex[:8]}", document_id=session.exec(select(Document)).first().id,
            financial_account_id=account.id, statement_type="CHECKING",
            active_parser_name="T", active_parser_version="1", statement_date=date(2026, 3, 31),
        ))
        ensure_entity_ledger_accounts(session)
        session.flush()
        txn = Transaction(
            id=f"txn_{uuid4().hex[:8]}", statement_id=session.exec(select(Statement)).first().id,
            financial_account_id=account.id, transaction_date=date(2026, 3, 20),
            amount_cents=-150_000 if kind == "card" else 150_000,
            direction="OUTFLOW" if kind == "card" else "INFLOW",
            raw_description="SEEDED CARD PURCHASE" if kind == "card" else "ATM CASH DEPOSIT",
            requires_client_confirmation=kind == "deposit",
        )
        session.add(txn)
        session.commit()
        return txn.id, prop.id, account.id


def _post_split(member, txn_id: str, property_id: str, *, client_confirmed=False,
                cpa_review=False, amount="1500.00"):
    return member.post("/transactions/split", {
        "transaction_id": txn_id,
        "property_id[]": property_id,
        "category[]": "REPAIRS" if not client_confirmed else "RENT_INCOME",
        "amount_dollars[]": amount,
        "post_to_ledger": "true",
        "client_confirmed": "true" if client_confirmed else "false",
        "user_attested_cpa_review": "true" if cpa_review else "false",
    }, follow_redirects=False)


def test_a_manager_does_the_bookkeeping_but_cannot_administer(owner, monkeypatch):
    """The gap this role fills: real work, no authority over the portfolio itself."""
    monkeypatch.setenv("PROPTALLY_ALLOW_REGISTRATION", "true")
    keeper = owner(email="keeper@example.com", portfolio_name="Shared Portfolio")
    manager = owner(app=keeper.app, email="manager@example.com",
                    password="manager-password-1", portfolio_name="Manager Portfolio")
    _invite_and_accept(keeper, manager, ROLE_MANAGER, email="manager@example.com")

    # Real work: a manager may maintain the portfolio's setup.
    assert manager.client.get("/settings").status_code == 200
    created = manager.post("/properties/create", {
        "name": "Manager Door", "address_line1": "9 Work St", "city": "Austin",
        "state": "TX", "zip_code": "78701", "property_type": "RENTAL_INVESTMENT",
    }, follow_redirects=False)
    assert created.status_code == 303, created.text[:200]
    assert "Manager Door" in manager.client.get("/settings").text

    # No authority: people, invitations, export, removal, role changes.
    with Session(keeper.control_engine) as session:
        from proptally.db.system_models import TenantMembership

        membership_id = session.exec(
            select(TenantMembership).where(TenantMembership.tenant_id == keeper.tenant_id)
        ).first().id

    for path, payload, expected in (
        ("/account/invites", {"email": "x@example.com", "role": ROLE_READ_ONLY}, 403),
        (f"/account/members/{membership_id}/role", {"role": ROLE_READ_ONLY}, 403),
        (f"/account/members/{membership_id}/remove", {}, 403),
    ):
        response = manager.post(path, payload, follow_redirects=False)
        assert response.status_code == expected, (path, response.status_code)

    assert manager.client.get(f"/portfolios/{keeper.tenant_id}/export").status_code == 403
    assert manager.post(f"/portfolios/{keeper.tenant_id}/delete",
                        {"confirm_name": "Shared Portfolio"}, follow_redirects=False).status_code == 403

    # The screens do not offer what the role cannot do.
    account_page = manager.client.get("/account").text
    assert "Only an owner can invite people or change roles" in account_page
    assert "People with access" in account_page, "a manager can still see who is in the portfolio"
    assert "MANAGER" in account_page, "the access list names the member's role"


def test_a_manager_cannot_authorize_on_the_clients_behalf(owner, monkeypatch):
    """CBD-1 is the client's decision, so a manager doing the work cannot supply it."""
    monkeypatch.setenv("PROPTALLY_ALLOW_REGISTRATION", "true")
    keeper = owner(email="keeper@example.com", portfolio_name="Shared Portfolio")
    manager = owner(app=keeper.app, email="manager@example.com",
                    password="manager-password-1", portfolio_name="Manager Portfolio")
    _invite_and_accept(keeper, manager, ROLE_MANAGER, email="manager@example.com")

    txn_id, _prop, _acct = _seed(keeper, kind="deposit")

    claiming = _post_split(manager, txn_id, "", client_confirmed=True)
    assert claiming.status_code == 403, "a manager must not be able to confirm on the client's behalf"
    assert "client" in claiming.text.lower()

    # Without the authorization the domain gate refuses too, so the manager cannot simply
    # omit it to get the posting through. (The allocation covers the whole transaction, so
    # this reaches the posting step where the gate is evaluated.)
    omitting = _post_split(manager, txn_id, "")
    assert omitting.status_code == 403

    # The owner can, and then it posts.
    allowed = _post_split(keeper, txn_id, "", client_confirmed=True)
    assert allowed.status_code == 303
    with Session(keeper.engine) as session:
        assert session.exec(
            select(JournalEntry).where(JournalEntry.status == "POSTED")
        ).all(), "the owner's authorization let it post"


def test_only_a_reviewers_review_is_recorded_as_a_verified_external_review(owner, monkeypatch):
    """The point of the reviewer role: their sign-off is distinguishable from the owner's.

    A manager may do the bookkeeping but cannot attest external review at all. An owner
    may tick the same control, and the record shows it was the owner attesting their own
    deduction rather than an accountant who saw it.
    """
    monkeypatch.setenv("PROPTALLY_ALLOW_REGISTRATION", "true")
    keeper = owner(email="keeper@example.com", portfolio_name="Shared Portfolio")
    reviewer = owner(app=keeper.app, email="cpa@example.com",
                     password="cpa-password-11", portfolio_name="Cpa Portfolio")
    manager = owner(app=keeper.app, email="manager@example.com",
                    password="manager-password-1", portfolio_name="Manager Portfolio")
    _invite_and_accept(keeper, reviewer, ROLE_CPA_REVIEWER, email="cpa@example.com")
    _invite_and_accept(keeper, manager, ROLE_MANAGER, email="manager@example.com")

    # A manager cannot attest at all.
    txn_for_manager, _p, _a = _seed(keeper, kind="card")
    refused = _post_split(manager, txn_for_manager, "", cpa_review=True)
    assert refused.status_code == 403
    assert "review" in refused.text.lower()

    # A reviewer's attestation posts, and is recorded as verified.
    txn_for_reviewer, prop_id, _a = _seed(keeper, kind="card")
    by_reviewer = _post_split(reviewer, txn_for_reviewer, prop_id, cpa_review=True)
    assert by_reviewer.status_code == 303, by_reviewer.text[:300]

    # The owner's own tick posts too, and is recorded as an owner attestation only.
    txn_for_owner, prop_id_2, _a2 = _seed(keeper, kind="card")
    by_owner = _post_split(keeper, txn_for_owner, prop_id_2, cpa_review=True)
    assert by_owner.status_code == 303, by_owner.text[:300]

    with Session(keeper.engine) as session:
        rows = {row.transaction_id: row for row in session.exec(select(PostingAuthorization)).all()}

    reviewer_row = rows[txn_for_reviewer]
    owner_row = rows[txn_for_owner]
    assert reviewer_row.verified_cpa_reviewer is True
    assert reviewer_row.authorized_by_role == ROLE_CPA_REVIEWER
    assert owner_row.verified_cpa_reviewer is False, "an owner's tick is not a CPA's review"
    assert owner_row.authorized_by_role == ROLE_OWNER


def test_a_read_only_member_can_still_manage_their_own_account(owner, monkeypatch):
    """Changing your own password is about you, not about the portfolio."""
    monkeypatch.setenv("PROPTALLY_ALLOW_REGISTRATION", "true")
    keeper = owner(email="keeper@example.com", portfolio_name="Shared Portfolio")
    viewer = owner(app=keeper.app, email="viewer@example.com",
                   password="viewer-password-1", portfolio_name="Viewer Portfolio")
    _invite_and_accept(keeper, viewer, ROLE_READ_ONLY, email="viewer@example.com")
    signin_page = viewer.client.get("/account?section=signin").text
    assert "Changing it signs out every session, including this one" in signin_page

    changed = viewer.post("/account/password", {
        "current_password": "viewer-password-1", "new_password": "a-new-password-22",
    }, follow_redirects=False)
    assert changed.status_code == 303, "a read-only member must be able to change their password"
    assert "error=" not in changed.headers["location"], changed.headers["location"]

    # Changing the password signed them out, and signing in with the new one works.
    viewer.client.cookies.clear()
    viewer.client.get("/auth/login")
    signed_in = viewer.client.post("/auth/login", data={
        "email": "viewer@example.com", "password": "a-new-password-22",
        "csrf_token": viewer.client.cookies.get("pt_csrf"),
    }, follow_redirects=False)
    assert signed_in.status_code == 303, "the new password must work"

    # They are back on their own portfolio; switch to the shared one, where they are
    # read-only, and confirm that nothing can be changed there.
    opened = viewer.post(f"/portfolios/{keeper.tenant_id}/open", {}, follow_redirects=False)
    assert opened.status_code == 303
    assert viewer.client.get("/").status_code == 200

    txn_id, _p, _a = _seed(keeper, kind="deposit")
    assert _post_split(viewer, txn_id, "").status_code == 403


def test_the_control_plane_accepts_the_new_role_on_an_existing_database(tmp_path):
    """A database built before MANAGER existed must accept it after the migration.

    The role is guarded by a CHECK constraint, which SQLite cannot alter in place, so the
    table is rebuilt. Without that, inviting a manager fails at the constraint -- in the
    middle of the flow, as a 500, rather than as anything an operator can act on.
    """
    from proptally.db.system_models import TenantMembership
    from proptally.db.tenant_manager import get_system_engine, init_system_schema

    engine = get_system_engine(f"sqlite:///{tmp_path / 'system.db'}")
    with engine.begin() as conn:
        # The pre-MANAGER shape, as an existing control plane has it.
        conn.exec_driver_sql("""CREATE TABLE users (id VARCHAR PRIMARY KEY, email VARCHAR NOT NULL UNIQUE,
            password_hash VARCHAR NOT NULL, display_name VARCHAR NOT NULL,
            is_instance_admin BOOLEAN NOT NULL, is_active BOOLEAN NOT NULL,
            password_changed_at TIMESTAMP, created_at TIMESTAMP NOT NULL)""")
        conn.exec_driver_sql("""CREATE TABLE tenants (id VARCHAR PRIMARY KEY, name VARCHAR NOT NULL,
            slug VARCHAR NOT NULL UNIQUE, created_at TIMESTAMP NOT NULL)""")
        conn.exec_driver_sql("""CREATE TABLE tenant_memberships (id VARCHAR PRIMARY KEY,
            user_id VARCHAR NOT NULL, tenant_id VARCHAR NOT NULL, role VARCHAR NOT NULL DEFAULT 'OWNER',
            created_at TIMESTAMP NOT NULL,
            CONSTRAINT check_membership_role CHECK (role IN ('OWNER', 'CPA_REVIEWER', 'READ_ONLY')))""")
        conn.exec_driver_sql("INSERT INTO users VALUES ('u1','a@b.c','x','A',1,1,NULL,'2026-01-01')")
        conn.exec_driver_sql("INSERT INTO users VALUES ('u2','c@d.e','x','C',0,1,NULL,'2026-01-01')")
        conn.exec_driver_sql("INSERT INTO tenants VALUES ('t1','Existing Portfolio','existing','2026-01-01')")
        conn.exec_driver_sql("INSERT INTO tenant_memberships VALUES ('m1','u1','t1','OWNER','2026-01-01')")

    init_system_schema(engine)

    from sqlalchemy import inspect as _inspect

    # ...and the platform-authority column arrives under its new name, carrying the
    # existing rows: the rename is what keeps `u1` (a super admin in the old schema)
    # a super admin rather than dropping the flag on the floor.
    columns = {column["name"] for column in _inspect(engine).get_columns("users")}
    assert "is_super_admin" in columns
    assert "is_instance_admin" not in columns
    with Session(engine) as session:
        preserved = session.execute(
            text("SELECT is_super_admin FROM users WHERE id = 'u1'")
        ).scalar()
    assert preserved == 1

    with Session(engine) as session:
        # The pre-existing membership survived the rebuild...
        assert len(session.exec(select(TenantMembership)).all()) == 1
        # ...and the new role is accepted.
        session.add(TenantMembership(id="m2", user_id="u2", tenant_id="t1", role=ROLE_MANAGER))
        session.commit()
        roles = sorted(m.role for m in session.exec(select(TenantMembership)).all())
    assert roles == [ROLE_MANAGER, ROLE_OWNER]
