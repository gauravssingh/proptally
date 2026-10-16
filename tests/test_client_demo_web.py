"""Integration tests for Phase 2 Client Demo MVP web interface and endpoints."""

import pytest
from starlette.testclient import TestClient


@pytest.fixture(name="signed_in")
def signed_in_fixture(owner):
    """A signed-in owner whose portfolio holds the synthetic demo data."""
    return owner(reference_portfolio=True)


@pytest.fixture(name="client")
def client_fixture(signed_in):
    """The signed-in owner's client, so page GETs reach the portfolio."""
    return signed_in.client


@pytest.fixture(name="csrf")
def csrf_fixture(signed_in):
    """The session's CSRF token, required on every state-changing request."""
    return signed_in.csrf()


def test_dashboard_overview_renders_successfully(client: TestClient):
    """GET / renders portfolio overview with equity, debt, NOI, CADS, and property cards."""
    response = client.get("/")
    assert response.status_code == 200
    html = response.text

    assert "Portfolio Financial Overview" in html
    assert "Portfolio Equity" in html
    assert "Total Debt Outstanding" in html
    assert "Rental Portfolio NOI" in html
    assert "Net Cash Flow (CADS)" in html
    assert "12307 Larkspur St" in html
    assert "582 Foxglove Ct" in html
    assert "1017 Elderberry Dr" in html
    assert "PRIMARY RESIDENCE" in html
    assert "CBD REVIEW" in html  # Review queue alert


def test_property_detail_views(client: TestClient):
    """GET /properties/:id renders property P&L, debt terms, and lease ledger."""
    # 0. /properties redirects to /
    resp_redirect = client.get("/properties", follow_redirects=False)
    assert resp_redirect.status_code == 303
    assert resp_redirect.headers["location"] == "/"

    # 1. Larkspur St (Rental Door)
    resp_alder = client.get("/properties/prop_tinsley_huntley")
    assert resp_alder.status_code == 200
    assert "12307 Larkspur St" in resp_alder.text
    assert "RENTAL DOOR" in resp_alder.text
    assert "Riley Alvarez" in resp_alder.text
    assert "Rocket Mortgage" in resp_alder.text or "Account 0700000001" in resp_alder.text
    assert "Net Operating Income (NOI)" in resp_alder.text

    # 2. Maple Cir (Rental Door)
    resp_cim = client.get("/properties/prop_cimmaron_pingree")
    assert resp_cim.status_code == 200
    assert "582 Foxglove Ct" in resp_cim.text
    assert "Account 0700000002" in resp_cim.text

    # 3. Elderberry Dr (Primary Residence)
    resp_rowan = client.get("/properties/prop_burkey_algonquin")
    assert resp_rowan.status_code == 200
    assert "1017 Elderberry Dr" in resp_rowan.text
    assert "PRIMARY RESIDENCE" in resp_rowan.text


def test_mortgages_and_escrow_ledger(client: TestClient):
    """GET /mortgages renders consolidated loan table and escrow reserve health."""
    response = client.get("/mortgages")
    assert response.status_code == 200
    html = response.text

    assert "Consolidated Mortgage Ledger" in html
    assert "Rocket Mortgage" in html
    assert "PNC Bank" in html
    assert "CrossCountry Mortgage" in html
    assert "Servicer Escrow Reserve Accounts" in html
    # V2.2 §3: the six balances must be visibly distinguished, not conflated.
    assert "Ledger Principal" in html
    assert "Servicer Reported" in html
    assert "Clearing Balance" in html
    assert "Calculated Balance" in html
    assert "Reported Balance" in html


def test_transactions_register_and_manual_entry(client: TestClient, csrf: str):
    """GET /transactions lists register; POST /transactions/manual creates a new transaction."""
    # Register view
    resp_list = client.get("/transactions")
    assert resp_list.status_code == 200
    assert "Transaction Register" in resp_list.text
    assert "Riley Alvarez - March Rent" in resp_list.text

    resp_create = client.post(
        "/transactions/manual",
        data={
            "account_id": "acct_bofa_chk_9456",
            "transaction_date": "2026-03-29",
            "description": "Ace Hardware Paint & Caulk",
            "amount_dollars": "45.50",
            "direction": "OUTFLOW",
            "csrf_token": csrf,
        },
        follow_redirects=True,
    )
    assert resp_create.status_code == 200
    assert "Ace Hardware Paint" in resp_create.text
    assert "$45.50" in resp_create.text


def test_transaction_split_flow(client: TestClient, csrf: str):
    """POST /transactions/split updates split allocations for a transaction."""
    # Create an unallocated transaction
    client.post(
        "/transactions/manual",
        data={
            "account_id": "acct_bofa_chk_9456",
            "transaction_date": "2026-03-29",
            "description": "Lowe's Multi-Property Repair",
            "amount_dollars": "100.00",
            "direction": "OUTFLOW",
            "csrf_token": csrf,
        },
        follow_redirects=True,
    )

    # Split: $60 Alder Repairs + $40 Maple Supplies
    resp_split = client.post(
        "/transactions/split",
        data={
            "transaction_id": "txn_menards_partial_20260318",
            "property_id[]": ["prop_tinsley_huntley", "prop_cimmaron_pingree"],
            "category[]": ["REPAIRS", "SUPPLIES"],
            "amount_dollars[]": ["140.00", "80.00"],
            "csrf_token": csrf,
        },
        follow_redirects=True,
    )
    assert resp_split.status_code == 200
    assert "Allocated" in resp_split.text


def test_reconciliation_review_queue_and_assignment(client: TestClient, csrf: str):
    """GET /reconciliation displays CBD-1 and CBD-2 items; POST /reconciliation/assign resolves an item."""
    # 1. Review queue contains CBD-1 and CBD-2
    resp_queue = client.get("/reconciliation")
    assert resp_queue.status_code == 200
    html = resp_queue.text

    assert "Reconciliation & Review Queue" in html
    assert "CBD-1 CASH RENT" in html
    assert "CBD-2 MIXED CARD" in html
    assert "ATM CASH DEPOSIT" in html
    assert "APPLE.COM/BILL" in html

    # 2. Assign CBD-1 ATM Deposit. The backend gate rejects the request without
    #    explicit client confirmation, regardless of what the UI sends.
    resp_unconfirmed = client.post(
        "/reconciliation/assign",
        data={
            "transaction_id": "txn_atm_deposit_cbd1_20260320",
            "property_id": "prop_cimmaron_pingree",
            "category": "RENT_INCOME",
            "csrf_token": csrf,
        },
    )
    assert resp_unconfirmed.status_code == 403

    # 3. With client confirmation the assignment and posting succeeds.
    resp_assign = client.post(
        "/reconciliation/assign",
        data={
            "transaction_id": "txn_atm_deposit_cbd1_20260320",
            "property_id": "prop_cimmaron_pingree",
            "category": "RENT_INCOME",
            "client_confirmed": "true",
            "csrf_token": csrf,
        },
        follow_redirects=True,
    )
    assert resp_assign.status_code == 200

    # CBD-1 is now resolved; remaining pending review count decreased
    resp_after = client.get("/reconciliation")
    assert "ATM CASH DEPOSIT" not in resp_after.text
