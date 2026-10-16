"""Self-service portfolio setup, driven through the browser.

An owner must be able to declare their own properties, deposit and card accounts,
mortgage loans, opening balances and vendor rules without a command line and without
a code change. These tests post the real forms (carrying the session's CSRF token)
and then read the portfolio database directly: the point is that the routes made real
rows, with real ledger accounts behind them, in this portfolio only.
"""

import re

from sqlmodel import Session, select
from starlette.testclient import TestClient

from proptally.db.models import (
    EscrowAccount,
    FinancialAccount,
    JournalEntry,
    JournalEntryLine,
    LedgerAccount,
    LoanAccount,
    Property,
    PropertyValuation,
    VendorRule,
)
from proptally.db.system_models import ROLE_READ_ONLY, TenantMembership, User
from proptally.web.auth import cookie_name, hash_password, issue_session

PROPERTY_FORM = {
    "name": "4409 Bluebonnet Ln",
    "address_line1": "4409 Bluebonnet Ln",
    "address_line2": "",
    "city": "Austin",
    "state": "TX",
    "zip_code": "78704",
    "property_type": "RENTAL_INVESTMENT",
    "acquisition_date": "2021-06-15",
    "purchase_price": "400000.00",
    "land_basis": "80000.00",
    "building_basis": "320000.00",
    "current_valuation": "450000.00",
    "valuation_date": "2026-01-01",
    "short_label": "Bluebonnet",
}
CHECKING_FORM = {
    "institution_name": "Lone Star Bank",
    "account_name": "Lone Star Operating Checking",
    "account_type": "CHECKING",
    "account_number_masked": "...4471",
    "mixed_use_status": "DEDICATED_RENTAL",
}
CARD_FORM = {
    "institution_name": "Lone Star Bank",
    "account_name": "Cedar Visa",
    "account_type": "CREDIT_CARD",
    "account_number_masked": "...8820",
    "mixed_use_status": "MIXED_USE",
}
MORTGAGE_FORM = {
    "institution_name": "Bluebonnet Servicing",
    "account_name": "Bluebonnet Servicing Loan",
    "loan_number": "BL-778211",
    "account_number_masked": "",
    "origination_date": "2021-06-15",
    "term_months": "360",
    "interest_rate": "3.125",
    "original_principal": "350000.00",
    "opening_principal": "348250.00",
    "escrow_opening_balance": "2000.00",
}


def _post(signed_in, url: str, data: dict | None = None):
    """Posts a form the way the browser does: CSRF token attached, redirect visible.

    ``follow_redirects=False`` matters: an accepted submission redirects back to the
    setup screen, and a refused one redirects there with the reason attached. Both are
    checked, so the response must not be swallowed by a follow.
    """
    return signed_in.post(url, data or {}, follow_redirects=False)


def _saved(response) -> str:
    """The submission was accepted: the setup screen is re-rendered with a message."""
    assert response.status_code == 303, response.text[:400]
    location = response.headers["location"]
    assert "error=" not in location, location
    assert "message=" in location, location
    return location


def _refused(response) -> str:
    """The submission was rejected: the setup screen carries the reason, not a 500."""
    assert response.status_code == 303, response.text[:400]
    location = response.headers["location"]
    assert "error=" in location, location
    return location


def _assert_portfolio_empty(signed_in) -> None:
    with Session(signed_in.engine) as session:
        assert session.exec(select(Property)).all() == []
        assert session.exec(select(FinancialAccount)).all() == []
        assert session.exec(select(LoanAccount)).all() == []
        assert session.exec(select(JournalEntry)).all() == []
        # A new portfolio still ships the standard chart of accounts and the generic
        # merchant rules: those are portfolio-independent, and are not owner data.
        assert session.exec(select(LedgerAccount)).all() != []
        assert all(
            not rule.id.startswith("vrule_tenant_")
            for rule in session.exec(select(VendorRule)).all()
        )


def test_fresh_portfolio_offers_onboarding_and_the_settings_screen(owner):
    signed_in = owner()
    _assert_portfolio_empty(signed_in)

    dashboard = signed_in.client.get("/")
    assert dashboard.status_code == 200
    # A clear "set up your portfolio" panel: a link to the setup screen, and no
    # synthetic content on offer -- a live portfolio is built by its owner.
    assert "Set up your portfolio" in dashboard.text
    assert 'href="/settings"' in dashboard.text
    assert "sample-portfolio" not in dashboard.text
    # The populated dashboard is not what an empty portfolio sees.
    assert "Portfolio Financial Overview" not in dashboard.text

    settings = signed_in.client.get("/settings")
    assert settings.status_code == 200
    assert "Portfolio Setup" in settings.text
    # The shell's navigation carries the setup entry, marked active on this page.
    assert 'href="/settings"' in settings.text
    assert "is-active" in settings.text
    # Every creation form is on the page, each carrying the CSRF token.
    for action in (
        'action="/properties/create"',
        'action="/accounts/create"',
        'action="/mortgages/create"',
        'action="/vendor-rules/create"',
    ):
        assert action in settings.text, action

    # A minimal portfolio — one door, no valuation, no accounts and no loans — still
    # renders: the empty state gives way to the populated dashboard, and the blank
    # optional money and date fields are accepted.
    _saved(_post(signed_in, "/properties/create", {
        "name": "582 Foxglove Ct",
        "address_line1": "582 Foxglove Ct",
        "address_line2": "",
        "city": "Oakhaven",
        "state": "IL",
        "zip_code": "60199",
        "property_type": "RENTAL_INVESTMENT",
        "acquisition_date": "",
        "purchase_price": "",
        "land_basis": "",
        "building_basis": "",
        "current_valuation": "",
        "valuation_date": "",
        "short_label": "",
    }))
    assert signed_in.client.get("/settings").status_code == 200
    populated = signed_in.client.get("/")
    assert populated.status_code == 200
    assert "Portfolio Financial Overview" in populated.text
    assert "Set up your portfolio" not in populated.text


def test_creating_entities_through_the_browser_allocates_ledger_accounts(owner):
    signed_in = owner()
    _saved(_post(signed_in, "/properties/create", PROPERTY_FORM))
    _saved(_post(signed_in, "/accounts/create", CHECKING_FORM))
    _saved(_post(signed_in, "/accounts/create", CARD_FORM))

    with Session(signed_in.engine) as session:
        property_id = session.exec(select(Property)).one().id

    _saved(_post(signed_in, "/mortgages/create", {**MORTGAGE_FORM, "property_id": property_id}))

    with Session(signed_in.engine) as session:
        prop = session.exec(select(Property)).one()
        assert prop.short_label == "Bluebonnet"
        assert prop.asset_ledger_code is not None
        assert session.get(LedgerAccount, prop.asset_ledger_code) is not None

        valuation = session.exec(select(PropertyValuation)).one()
        assert valuation.property_id == prop.id
        assert valuation.estimated_value_cents == 45_000_000
        assert valuation.valuation_source == "CLIENT_MANUAL"

        accounts = {a.account_type: a for a in session.exec(select(FinancialAccount)).all()}
        checking = accounts["CHECKING"]
        checking_id = checking.id
        card = accounts["CREDIT_CARD"]
        assert checking.ledger_account_code is not None
        assert card.ledger_account_code is not None
        # A deposit account's code is an asset and a card's is a liability: they come
        # from different allocation classes, so they can never be the same account.
        assert checking.ledger_account_code != card.ledger_account_code
        # A mortgage servicing account holds no code of its own; the loan does.
        assert accounts["MORTGAGE"].ledger_account_code is None

        loan = session.exec(select(LoanAccount)).one()
        assert loan.property_id == prop.id
        assert loan.loan_number == "BL-778211"
        assert loan.term_months == 360
        assert loan.current_interest_rate_bps == 3125
        assert loan.original_principal_cents == 35_000_000
        assert loan.principal_ledger_code is not None
        assert loan.clearing_ledger_code is not None
        assert loan.escrow_ledger_code is not None

        escrow = session.exec(select(EscrowAccount)).one()
        assert escrow.loan_account_id == loan.id
        assert escrow.opening_balance_cents == 200_000

        # Every allocated code has a real, distinct ledger account behind it.
        allocated = (
            checking.ledger_account_code,
            card.ledger_account_code,
            loan.principal_ledger_code,
            loan.clearing_ledger_code,
            loan.escrow_ledger_code,
        )
        assert len(set(allocated)) == len(allocated)
        for code in allocated:
            assert session.get(LedgerAccount, code) is not None

        # The declared opening principal is in the ledger, keyed to this loan.
        entry = session.exec(
            select(JournalEntry).where(
                JournalEntry.idempotency_key == f"MORTGAGE_OPENING:{loan.id}"
            )
        ).one()
        assert entry.status == "POSTED"
        assert entry.entry_type == "OPENING_BALANCE"

        asset_code = prop.asset_ledger_code
        principal_code = loan.principal_ledger_code
        loan_id = loan.id

    # The populated screen renders every declared entity, with its allocation.
    populated = signed_in.client.get("/settings")
    assert populated.status_code == 200
    for expected in (
        "4409 Bluebonnet Ln",
        "Lone Star Operating Checking",
        "Cedar Visa",
        "BL-778211",
        str(asset_code),
        str(principal_code),
    ):
        assert expected in populated.text, expected

    # The dated baseline offers the portfolio's own balances, and deliberately not the
    # loan's: a loan states its principal and escrow on the loan, so neither figure has
    # two homes and neither can be counted twice.
    assert f'name="asset_{property_id}"' in populated.text
    assert f'name="amount_{checking_id}"' in populated.text
    assert f'name="principal_{loan_id}"' not in populated.text
    assert f'name="escrow_{loan_id}"' not in populated.text


def test_editing_a_declared_property_and_managing_a_vendor_rule(owner):
    signed_in = owner()
    _saved(_post(signed_in, "/properties/create", PROPERTY_FORM))

    with Session(signed_in.engine) as session:
        property_id = session.exec(select(Property)).one().id

    _saved(_post(signed_in, f"/properties/{property_id}/edit", {
        **PROPERTY_FORM,
        "name": "4409 Bluebonnet Lane",
        "property_type": "PRIMARY_RESIDENCE",
        "short_label": "",
        "current_valuation": "460000.00",
    }))

    with Session(signed_in.engine) as session:
        prop = session.get(Property, property_id)
        assert prop.name == "4409 Bluebonnet Lane"
        assert prop.property_type == "PRIMARY_RESIDENCE"
        assert prop.short_label is None
        # The same valuation date is updated in place rather than duplicated.
        valuations = session.exec(
            select(PropertyValuation).where(PropertyValuation.property_id == property_id)
        ).all()
        assert [v.estimated_value_cents for v in valuations] == [46_000_000]

    _saved(_post(signed_in, "/vendor-rules/create", {
        "pattern": "jnl climate",
        "vendor_name": "JNL Climate Control",
        "service_type": "HVAC servicing and maintenance",
        "default_category": "REPAIRS",
        "default_property_id": property_id,
        "requires_cpa_review": "true",
    }))

    with Session(signed_in.engine) as session:
        rule = session.exec(select(VendorRule).where(VendorRule.pattern == "jnl climate")).one()
        assert rule.default_category == "REPAIRS"
        assert rule.default_property_id == property_id
        assert rule.requires_cpa_review is True
        assert rule.requires_client_confirmation is False
        assert rule.is_active is True
        rule_id = rule.id

    _saved(_post(signed_in, f"/vendor-rules/{rule_id}/toggle", {}))

    with Session(signed_in.engine) as session:
        assert session.get(VendorRule, rule_id).is_active is False

    listed = signed_in.client.get("/settings")
    assert listed.status_code == 200
    assert "JNL Climate Control" in listed.text
    assert f'action="/vendor-rules/{rule_id}/toggle"' in listed.text
    assert "PAUSED" in listed.text


def test_opening_balances_are_recorded_once_per_date(owner):
    signed_in = owner()
    _saved(_post(signed_in, "/properties/create", PROPERTY_FORM))
    _saved(_post(signed_in, "/accounts/create", CHECKING_FORM))

    with Session(signed_in.engine) as session:
        prop = session.exec(select(Property)).one()
        property_id, asset_code = prop.id, prop.asset_ledger_code
        account = session.exec(select(FinancialAccount)).one()
        account_id, cash_code = account.id, account.ledger_account_code
    assert asset_code is not None and cash_code is not None

    _saved(_post(signed_in, "/opening-balances", {
        "as_of": "2026-01-01",
        f"asset_{property_id}": "400000.00",
        f"amount_{account_id}": "5,000.00",
    }))

    with Session(signed_in.engine) as session:
        entry = session.exec(
            select(JournalEntry).where(
                JournalEntry.idempotency_key == "OPENING_BALANCE:2026-01-01"
            )
        ).one()
        assert entry.status == "POSTED"
        # The door's cost basis and the cash balance both landed in the one entry.
        codes = {
            line.ledger_account_code
            for line in session.exec(
                select(JournalEntryLine).where(JournalEntryLine.journal_entry_id == entry.id)
            ).all()
        }
        assert {asset_code, cash_code} <= codes

    # The ledger refuses a second opening balance for the same date. That is a fact
    # about the ledger rather than a server error, so the screen says so.
    rejected = _refused(_post(signed_in, "/opening-balances", {
        "as_of": "2026-01-01",
        f"amount_{account_id}": "9,999.00",
    }))
    assert "already" in rejected.lower()

    with Session(signed_in.engine) as session:
        entries = session.exec(
            select(JournalEntry).where(
                JournalEntry.idempotency_key == "OPENING_BALANCE:2026-01-01"
            )
        ).all()
        assert len(entries) == 1


def test_read_only_member_is_refused_and_anonymous_post_is_redirected(owner):
    signed_in = owner()
    read_only = _read_only_client(signed_in)

    token = re.search(r'name="csrf_token" value="([^"]+)"', read_only.get("/account").text)
    assert token, "the account screen exposes the session's CSRF token"
    refused = read_only.post(
        "/properties/create",
        data={**PROPERTY_FORM, "csrf_token": token.group(1)},
        follow_redirects=False,
    )
    assert refused.status_code == 403

    with TestClient(signed_in.app) as anonymous:
        redirected = anonymous.post(
            "/properties/create", data=PROPERTY_FORM, follow_redirects=False
        )
    assert redirected.status_code == 303
    assert redirected.headers["location"].startswith("/auth/login")

    with Session(signed_in.engine) as session:
        assert session.exec(select(Property)).all() == []


def _read_only_client(signed_in) -> TestClient:
    """A second live session for the same portfolio, in the READ_ONLY role."""
    with Session(signed_in.control_engine) as session:
        user = User(
            id="usr_readonly_member",
            email="readonly@example.com",
            password_hash=hash_password("read-only-password-1"),
            display_name="Read Only",
        )
        session.add(user)
        session.flush()
        session.add(TenantMembership(
            id="mem_readonly_member",
            user_id=user.id,
            tenant_id=signed_in.tenant_id,
            role=ROLE_READ_ONLY,
        ))
        session.commit()
        # Issued inside the session that owns the row: an expired instance cannot be
        # read once it is detached.
        token, _row = issue_session(signed_in.control_engine, user, signed_in.tenant_id)
    client = TestClient(signed_in.app)
    client.__enter__()
    client.cookies.set(cookie_name(), token)
    return client
