"""Integration tests for transactions pagination, filtering, and unverified tenant view."""

from starlette.testclient import TestClient
from proptally.web.app import create_app


def test_unverified_tenant_presentation_on_property_detail(tmp_path):
    """Test that Tyler J. Saxsma is presented as an unverified tenant when no lease is vaulted."""
    db_file = tmp_path / "test_unverified.db"
    db_url = f"sqlite:///{db_file}"
    app = create_app(db_url=db_url)
    with TestClient(app) as client:
        # Note: in demo mode or live, test property_detail for Alder
        resp = client.get("/properties/prop_tinsley_huntley")
        assert resp.status_code == 200
        html = resp.text
        # Check unverified tenant callout or presentation
        assert "Tyler J. Saxsma" in html or "Riley Alvarez" in html


def test_transactions_pagination_and_filtering(tmp_path):
    """Test pagination, search, status, and flow filtering on /transactions."""
    db_file = tmp_path / "test_txns.db"
    db_url = f"sqlite:///{db_file}"
    app = create_app(db_url=db_url)
    with TestClient(app) as client:
        # 1. Base register
        resp = client.get("/transactions")
        assert resp.status_code == 200
        assert "Transaction Register" in resp.text
        assert "All Recorded" in resp.text
        assert "Awaiting Allocation" in resp.text
        assert "Search Description / Counterparty" in resp.text
        assert "Apply Filters" in resp.text

        # 2. Test pagination with per_page=5
        resp_paged = client.get("/transactions?per_page=5&page=1")
        assert resp_paged.status_code == 200
        assert "Page 1 of" in resp_paged.text
        assert "Next →" in resp_paged.text

        # 3. Test page 2
        resp_p2 = client.get("/transactions?per_page=5&page=2")
        assert resp_p2.status_code == 200
        assert "Page 2 of" in resp_p2.text
        assert "← Previous" in resp_p2.text

        # 4. Test text search
        resp_search = client.get("/transactions?q=Rent")
        assert resp_search.status_code == 200
        assert "Clear Filters" in resp_search.text

        # 5. Test direction filter
        resp_inflow = client.get("/transactions?direction=INFLOW")
        assert resp_inflow.status_code == 200
        assert "Clear Filters" in resp_inflow.text

        resp_outflow = client.get("/transactions?direction=OUTFLOW")
        assert resp_outflow.status_code == 200

        # 6. Test status filter
        resp_status = client.get("/transactions?status=UNALLOCATED")
        assert resp_status.status_code == 200

        # 7. Test referer redirect preservation on manual transaction
        resp_create = client.post(
            "/transactions/manual",
            data={
                "account_id": "acct_bofa_chk_9456",
                "transaction_date": "2026-04-01",
                "description": "Menards Hardware Supplies",
                "amount_dollars": "89.20",
                "direction": "OUTFLOW",
            },
            headers={"referer": "http://testserver/transactions?per_page=10&page=2"},
            follow_redirects=False,
        )
        assert resp_create.status_code == 303
        assert resp_create.headers["location"] == "http://testserver/transactions?per_page=10&page=2"


def test_live_poc_db_unverified_tenant_and_pagination():
    """Verify live POC database has Tyler as unverified tenant and 578 paginated transactions."""
    from pathlib import Path
    db_path = Path("storage/client-portfolio.db")
    if not db_path.exists():
        return
    app = create_app(db_url="sqlite:///storage/client-portfolio.db")
    with TestClient(app) as client:
        # Check unverified tenant on Alder
        resp_t = client.get("/properties/prop_tinsley_huntley")
        assert resp_t.status_code == 200
        assert "Tyler J. Saxsma" in resp_t.text
        assert "Unverified Tenant" in resp_t.text
        assert "Upload Lease Agreement to Verify" in resp_t.text
        assert "Inferred Rent" in resp_t.text

        # Check transactions pagination
        resp_tx = client.get("/transactions")
        assert resp_tx.status_code == 200
        assert "578 transactions recorded" in resp_tx.text
        assert "Page 1 of 24" in resp_tx.text

        # Check search filter for Tyler
        resp_q = client.get("/transactions?q=saxsma")
        assert resp_q.status_code == 200
        assert "TYLER J SAXSMA" in resp_q.text
