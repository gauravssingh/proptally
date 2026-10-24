"""Servicer-statement application: funded clearing becomes P&I debt service."""

from datetime import date

import pytest
from sqlmodel import Session

from proptally.db.models import (
    Document,
    EscrowAccount,
    FinancialAccount,
    FinancialInstitution,
    LoanAccount,
    MortgagePeriod,
    Property,
    Statement,
    Transaction,
)
from proptally.domain.accounting.coa import ensure_entity_ledger_accounts
from proptally.domain.accounting.metrics import calculate_property_financials
from proptally.domain.accounting.mortgage_application import apply_mortgage_period
from proptally.domain.accounting.opening_balances import post_loan_opening_balances
from proptally.domain.transactions.splits import (
    SplitItem,
    apply_transaction_splits,
    post_transaction_to_ledger,
)

CHECKING = "acct_app_chk"
MORTGAGE_ACCOUNT = "acct_app_mtg"
PROPERTY = "prop_app"
LOAN = "loan_app"
PERIOD = "period_app"

PRINCIPAL = 30000
INTEREST = 100000
ESCROW = 29000
EXTRA = 48000
FUNDING = PRINCIPAL + INTEREST + ESCROW + EXTRA


@pytest.fixture(name="app_env")
def app_env_fixture(session: Session):
    institution = FinancialInstitution(id="inst_app", name="Test Bank")
    session.add(institution)
    session.flush()

    checking = FinancialAccount(
        id=CHECKING,
        institution_id=institution.id,
        account_name="Checking",
        account_type="CHECKING",
        account_number_masked="...0001",
    )
    mortgage_account = FinancialAccount(
        id=MORTGAGE_ACCOUNT,
        institution_id=institution.id,
        account_name="Test Mortgage",
        account_type="MORTGAGE",
        account_number_masked="...0002",
    )
    prop = Property(
        id=PROPERTY,
        name="Application Test Door",
        address_line1="1 Test St",
        city="Testville",
        zip_code="00000",
        property_type="RENTAL_INVESTMENT",
    )
    session.add(checking)
    session.add(mortgage_account)
    session.add(prop)
    session.flush()

    loan = LoanAccount(
        id=LOAN,
        financial_account_id=mortgage_account.id,
        property_id=prop.id,
        loan_number="APP-1",
        origination_date=date(2026, 1, 1),
        original_principal_cents=3_000_000,
        term_months=360,
        current_interest_rate_bps=5000,
    )
    session.add(loan)
    session.flush()
    ensure_entity_ledger_accounts(session)
    session.refresh(loan)
    session.add(EscrowAccount(
        id="escrow_app",
        loan_account_id=loan.id,
        opening_balance_cents=50000,
    ))
    post_loan_opening_balances(session, loan, 2_500_000, 0)

    document = Document(
        id="doc_app",
        file_sha256="app-hash",
        original_filename="checking.pdf",
        storage_path="/tmp/checking.pdf",
        file_size_bytes=1,
    )
    session.add(document)
    session.flush()
    statement = Statement(
        id="stmt_app",
        document_id=document.id,
        financial_account_id=CHECKING,
        statement_type="CHECKING",
        active_parser_name="test",
        active_parser_version="1",
        statement_date=date(2026, 6, 30),
    )
    session.add(statement)
    session.flush()

    period = MortgagePeriod(
        id=PERIOD,
        statement_id=statement.id,
        loan_account_id=loan.id,
        payment_due_date=date(2026, 6, 1),
        statement_date=date(2026, 5, 1),
        principal_billed_cents=PRINCIPAL,
        interest_billed_cents=INTEREST,
        escrow_billed_cents=ESCROW,
        total_amount_billed_cents=PRINCIPAL + INTEREST + ESCROW,
        outstanding_principal_cents=2_470_000,
        interest_rate_bps=5000,
        status="BILLED",
    )
    session.add(period)
    session.commit()
    return {"loan": loan, "period": period, "statement": statement}


def _fund_clearing(session: Session, statement: Statement) -> Transaction:
    txn = Transaction(
        id="txn_app_funding",
        statement_id=statement.id,
        financial_account_id=CHECKING,
        transaction_date=date(2026, 6, 1),
        amount_cents=-FUNDING,
        direction="OUTFLOW",
        raw_description="TEST MORTGAGE ACH",
    )
    session.add(txn)
    session.flush()
    apply_transaction_splits(session, txn.id, [
        SplitItem(
            property_id=PROPERTY,
            category="MORTGAGE",
            amount_cents=FUNDING,
            tax_review_status="SELF_CLASSIFIED",
        )
    ])
    post_transaction_to_ledger(session, txn.id)
    return txn


def test_statement_application_recognises_principal_and_interest(session: Session, app_env):
    _fund_clearing(session, app_env["statement"])

    apply_mortgage_period(
        session,
        app_env["period"].id,
        extra_principal_cents=EXTRA,
    )

    period = session.get(MortgagePeriod, app_env["period"].id)
    assert period.status == "FULLY_SETTLED"
    assert period.principal_applied_cents == PRINCIPAL + EXTRA
    assert period.interest_applied_cents == INTEREST
    assert period.escrow_applied_cents == ESCROW
    assert period.total_amount_applied_cents == FUNDING

    financials = calculate_property_financials(session, property_id=PROPERTY)
    assert financials["principal_paid_cents"] == PRINCIPAL + EXTRA
    assert financials["mortgage_interest_cents"] == INTEREST
    assert financials["total_debt_service_cents"] == PRINCIPAL + INTEREST + EXTRA
    assert financials["escrow_funded_cents"] == ESCROW
    assert financials["unfunded_applied_debt_service_cents"] == 0
    assert financials["cads_cents"] == -(PRINCIPAL + INTEREST + EXTRA)


def test_statement_application_requires_funded_clearing(session: Session, app_env):
    with pytest.raises(ValueError, match="clearing account is not funded"):
        apply_mortgage_period(
            session,
            app_env["period"].id,
            extra_principal_cents=0,
        )
    assert session.get(MortgagePeriod, app_env["period"].id).status == "BILLED"


def test_statement_application_cannot_run_twice(session: Session, app_env):
    _fund_clearing(session, app_env["statement"])
    apply_mortgage_period(session, app_env["period"].id, extra_principal_cents=EXTRA)

    with pytest.raises(ValueError, match="already been applied"):
        apply_mortgage_period(session, app_env["period"].id, extra_principal_cents=EXTRA)
