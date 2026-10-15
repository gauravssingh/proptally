"""Phase 2.1 review remediation: dashboard labels must match the evidence.

A label that overstates what backs a balance is a financial-reporting defect, so
these tests tie each claim on the page to the data underneath it.
"""

import pytest
from sqlalchemy import text
from starlette.testclient import TestClient

from proptally.db.connection import get_engine
from proptally.domain.accounting.metrics import calculate_portfolio_equity
from proptally.web.app import create_app
from proptally.web.helpers import format_cents


@pytest.fixture(name="client")
def client_fixture(tmp_path):
    db_path = tmp_path / "labels.db"
    app = create_app(db_url=f"sqlite:///{db_path}")
    with TestClient(app) as test_client:
        test_client.db_url = f"sqlite:///{db_path}"
        yield test_client


def _engine(client):
    return get_engine(client.db_url)


def test_no_unsupported_verified_claim_is_shown(client):
    """The pages must not claim verification the data does not support."""
    dashboard = client.get("/").text
    mortgages = client.get("/mortgages").text

    assert "[VERIFIED]" not in dashboard
    assert "[VERIFIED LEDGER]" not in dashboard
    assert "[VERIFIED]" not in mortgages
    assert "[VERIFIED LEG" not in mortgages


def test_dashboard_debt_label_describes_the_actual_evidence(client):
    """The demo has no servicer statements, so the label must say so."""
    html = client.get("/").text
    assert "POSTED LEDGER" in html
    assert "SERVICER STATEMENTS" not in html

    with _engine(client).connect() as conn:
        statements = conn.execute(text("SELECT COUNT(*) FROM mortgage_periods")).scalar()
    assert statements == 0, "fixture assumption: demo ships no servicer statements"
    assert "NO STATEMENT" in client.get("/mortgages").text


def test_dashboard_debt_equals_the_sum_of_the_loan_ledgers(client):
    """The headline debt figure reconciles with the per-loan ledger principals."""
    with _engine(client).connect() as conn:
        ledger_debt = conn.execute(text(
            "SELECT COALESCE(SUM(l.credit_cents - l.debit_cents), 0) "
            "FROM journal_entry_lines l JOIN journal_entries e ON e.id = l.journal_entry_id "
            "WHERE e.status = 'POSTED' AND l.ledger_account_code IN (2110, 2120, 2130)"
        )).scalar()
        per_loan = [
            row[1] for row in conn.execute(text(
                "SELECT l.ledger_account_code, SUM(l.credit_cents - l.debit_cents) "
                "FROM journal_entry_lines l JOIN journal_entries e ON e.id = l.journal_entry_id "
                "WHERE e.status = 'POSTED' AND l.ledger_account_code IN (2110, 2120, 2130) "
                "GROUP BY l.ledger_account_code"
            ))
        ]

    equity = calculate_portfolio_equity(_session(client))
    assert equity["total_debt_cents"] == ledger_debt
    assert equity["debt_fully_verified"] is True
    assert sum(per_loan) == ledger_debt

    # The rendered figures must equal the ledger's own totals, whatever the
    # fixture amounts happen to be — expectations are derived, never transcribed.
    html = client.get("/").text
    assert format_cents(ledger_debt) in html
    # The same components are visible per loan on the mortgages page.
    loans_html = client.get("/mortgages").text
    for component_cents in per_loan:
        assert format_cents(component_cents) in loans_html


def _session(client):
    from sqlmodel import Session

    return Session(_engine(client))


def test_no_statement_is_fabricated_formatted_as_a_servicer_figure(client):
    """Without a statement, the servicer column must show nothing, not a ledger echo."""
    html = client.get("/mortgages").text
    # Three loans and two escrow reserves, none with a statement behind them.
    assert html.count("NO STATEMENT") >= 5
    assert "FROM STATEMENT" not in html

    with _engine(client).connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM mortgage_periods")).scalar() == 0
