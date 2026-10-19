"""Tests for deterministic bank and credit card statement ingestion.

Every statement used here is generated as a checked-in synthetic PDF, so the
parser assertions run on a clean checkout instead of silently returning when a
machine-specific sample folder is absent.
"""

from sqlmodel import Session, select
from synthetic_statements import (
    apple_card_installment_pdf,
    apple_card_pdf,
    apple_card_return_discrepancy_pdf,
    bofa_checking_pdf,
    bofa_credit_card_pdf,
)

from proptally.db.connection import get_engine
from proptally.db.models import (
    DocumentExtraction,
    FinancialAccount,
    FinancialInstitution,
    Transaction,
    TransactionProcessingState,
)
from proptally.db.schema import init_schema
from proptally.ingestion.bank_parsers import (
    parse_apple_card,
    parse_bofa_checking,
    parse_bofa_credit_card,
)
from proptally.ingestion.service import (
    approve_extraction,
    import_approved_extraction,
    ingest_folder,
)


def test_bofa_checking_parsing(tmp_path):
    ext = parse_bofa_checking(bofa_checking_pdf(tmp_path / "checking.pdf"))
    assert ext is not None
    assert ext.parser_name == "BofaCheckingParser"
    assert ext.statement_type == "CHECKING"
    # The parser reports the identity the statement prints; which portfolio
    # account that is, is resolved against the portfolio's own records.
    assert ext.institution_name == "Bank of America"
    assert ext.account_last4 == "9456"
    assert ext.opening_balance_cents == 392695  # $3,926.95
    assert ext.closing_balance_cents == 48586   # $485.86
    assert ext.status == "VALIDATED"
    assert len(ext.transactions) == 3

    # Verify balance equation to the single penny
    net_cents = sum(t.amount_cents for t in ext.transactions)
    assert ext.opening_balance_cents + net_cents == ext.closing_balance_cents

    # Verify ATM deposits require client confirmation
    atm_deposits = [t for t in ext.transactions if "CARPENTERSVILLE" in t.raw_description]
    assert len(atm_deposits) == 2
    assert {t.amount_cents for t in atm_deposits} == {85000, 50000}  # $850.00 and $500.00
    assert all(t.direction == "INFLOW" for t in atm_deposits)
    assert all(t.requires_client_confirmation is True for t in atm_deposits)


def test_bofa_credit_card_parsing(tmp_path):
    ext = parse_bofa_credit_card(bofa_credit_card_pdf(tmp_path / "card.pdf"))
    assert ext is not None
    assert ext.parser_name == "BofaCreditCardParser"
    assert ext.statement_type == "CREDIT_CARD"
    assert ext.institution_name == "Bank of America"
    assert ext.account_last4 == "9306"
    # The parser reports observed identity, never a portfolio-specific account id.
    assert ext.financial_account_id is None
    assert ext.institution_id is None
    assert ext.opening_balance_cents == 138531  # $1,385.31
    assert ext.closing_balance_cents == 274012  # $2,740.12
    assert ext.status == "VALIDATED"
    assert len(ext.transactions) == 3

    # Signed cents: payments are inflow-positive, purchases outflow-negative.
    payment_tx = [t for t in ext.transactions if "Online payment from CHK 9456" in t.raw_description]
    assert len(payment_tx) == 1
    assert payment_tx[0].amount_cents == 138531
    assert payment_tx[0].direction == "INFLOW"
    assert all(t.amount_cents < 0 for t in ext.transactions if t.direction == "OUTFLOW")
    assert ext.opening_balance_cents - sum(t.amount_cents for t in ext.transactions) == ext.closing_balance_cents


def test_apple_card_parsing(tmp_path):
    ext = parse_apple_card(apple_card_pdf(tmp_path / "apple.pdf"))
    assert ext is not None
    assert ext.parser_name == "AppleCardParser"
    assert ext.statement_type == "CREDIT_CARD"
    assert ext.institution_name == "Goldman Sachs Bank USA"
    assert ext.opening_balance_cents == 266362  # $2,663.62
    assert ext.closing_balance_cents == 490185  # $4,901.85
    assert ext.status == "VALIDATED"
    assert len(ext.transactions) == 2

    ach_payments = [t for t in ext.transactions if "ACH Deposit" in t.raw_description]
    assert len(ach_payments) == 1
    assert ach_payments[0].amount_cents == 100000
    assert ach_payments[0].direction == "INFLOW"
    purchases = [t for t in ext.transactions if t.direction == "OUTFLOW"]
    assert len(purchases) == 1
    assert purchases[0].amount_cents == -323823
    assert ext.opening_balance_cents - sum(t.amount_cents for t in ext.transactions) == ext.closing_balance_cents


def test_apple_card_current_layout_foots_monthly_balance(tmp_path):
    """Monthly balance excludes installment debt; Daily Cash reversals are charges."""
    ext = parse_apple_card(apple_card_installment_pdf(tmp_path / "apple-installment.pdf"))
    assert ext is not None
    assert ext.status == "VALIDATED"
    assert ext.opening_balance_cents == 100000
    assert ext.closing_balance_cents == 113020

    # Payment, purchase, return, Daily Cash reversal and this month's installment.
    assert len(ext.transactions) == 5
    adjustments = [t for t in ext.transactions if "Daily Cash" in t.raw_description]
    assert len(adjustments) == 1
    assert adjustments[0].amount_cents == -20
    assert adjustments[0].direction == "OUTFLOW"
    installments = [
        t for t in ext.transactions
        if t.amount_cents == -5000 and t.direction == "OUTFLOW"
    ]
    assert len(installments) == 1
    assert ext.opening_balance_cents - sum(t.amount_cents for t in ext.transactions) == ext.closing_balance_cents


def test_apple_card_return_never_fabricates_an_adjustment(tmp_path):
    """A return that does not foot must be retained and flagged, not plugged."""
    ext = parse_apple_card(apple_card_return_discrepancy_pdf(tmp_path / "apple-return.pdf"))
    assert ext is not None
    assert ext.status == "NEEDS_REVIEW"
    assert ext.review_reasons

    # Only the two printed rows exist; no synthetic "daily cash adjustment".
    assert len(ext.transactions) == 2
    assert not any("Daily Cash Adjustment" in t.raw_description for t in ext.transactions)
    returns = [t for t in ext.transactions if "(return)" in t.raw_description.lower()]
    assert len(returns) == 1
    assert returns[0].amount_cents == 5000
    assert returns[0].direction == "INFLOW"

    net_cents = sum(t.amount_cents for t in ext.transactions)
    assert ext.opening_balance_cents - net_cents != ext.closing_balance_cents


def test_bank_statement_vault_and_database_persistence(tmp_path):
    db_file = tmp_path / "test_bank.db"
    vault_dir = tmp_path / "vault"
    statements_dir = tmp_path / "statements"
    statements_dir.mkdir()
    engine = get_engine(f"sqlite:///{db_file}")
    init_schema(engine)

    # A statement is only booked against an account the portfolio holds, so the
    # checking account is registered first -- the same thing an operator does
    # before pointing the ingester at a portfolio.
    with Session(engine) as session:
        session.add(FinancialInstitution(id="inst_bofa", name="Bank of America"))
        session.flush()
        session.add(FinancialAccount(
            id="acct_bofa_chk_9456",
            institution_id="inst_bofa",
            account_name="Bank of America Advantage Plus Checking",
            account_type="CHECKING",
            account_number_masked="...9456",
        ))
        session.commit()

    # Three distinct statements so each one is imported rather than deduped.
    for day in (15, 16, 17):
        bofa_checking_pdf(
            statements_dir / f"eStmt_2026-01-{day}.pdf",
            period_start="December 16, 2025",
            period_end=f"January {day}, 2026",
        )

    with Session(engine) as session:
        report = ingest_folder(statements_dir, session, vault_dir, commit=True)
        assert report["imported"] == 3
        assert report["errors"] == 0

        # Verify Document and DocumentExtraction created
        extractions = session.exec(select(DocumentExtraction)).all()
        assert len(extractions) == 3
        for ext in extractions:
            assert ext.validation_status == "VALIDATED"
            assert ext.approval_status == "PENDING"

            # Approve and import
            approve_extraction(session, ext.id, commit=True)
            stmt = import_approved_extraction(session, ext.id, commit=True)
            assert stmt is not None
            assert stmt.statement_type == "CHECKING"

        # Verify transactions created with unallocated processing states
        txs = session.exec(select(Transaction)).all()
        assert len(txs) == 9
        states = session.exec(select(TransactionProcessingState)).all()
        assert len(states) == len(txs)
        for s in states:
            assert s.allocation_status == "UNALLOCATED"
            assert s.posting_status == "UNPOSTED"

        # Verify idempotency on second scan
        report2 = ingest_folder(statements_dir, session, vault_dir, commit=True)
        assert report2["duplicates"] == 3
        assert report2["imported"] == 0
