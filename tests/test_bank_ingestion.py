"""Tests for deterministic bank and credit card statement ingestion."""

import os
from pathlib import Path

from sqlmodel import Session, select

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

DOCS_BASE = Path(
    os.environ.get(
        "PROPTALLY_TEST_DOCS_DIR",
        str(Path.home() / "Documents" / "client-statements"),
    )
)


def test_bofa_checking_parsing():
    pdf_path = DOCS_BASE / "BOFA Checking" / "eStmt_2026-01-15.pdf"
    if not pdf_path.exists():
        return

    ext = parse_bofa_checking(pdf_path)
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
    assert len(ext.transactions) == 52

    # Verify balance equation to the single penny
    net_cents = sum(t.amount_cents for t in ext.transactions)
    assert ext.opening_balance_cents + net_cents == ext.closing_balance_cents

    # Verify Millbrook ATM deposits require client confirmation
    atm_deposits = [t for t in ext.transactions if "CARPENTERSVILLE" in t.raw_description and t.direction == "INFLOW"]
    assert len(atm_deposits) == 2
    assert {t.amount_cents for t in atm_deposits} == {85000, 50000}  # $850.00 and $500.00
    assert all(t.requires_client_confirmation is True for t in atm_deposits)


def test_bofa_credit_card_parsing():
    pdf_path = DOCS_BASE / "BOFA Credit card" / "eStmt_2026-01-08.pdf"
    if not pdf_path.exists():
        return

    ext = parse_bofa_credit_card(pdf_path)
    assert ext is not None
    assert ext.parser_name == "BofaCreditCardParser"
    assert ext.statement_type == "CREDIT_CARD"
    assert ext.institution_name == "Bank of America"
    assert ext.account_last4 == "9306"
    assert ext.opening_balance_cents == 138531  # $1,385.31
    assert ext.closing_balance_cents == 274012  # $2,740.12
    assert ext.status == "VALIDATED"
    assert len(ext.transactions) == 23

    # The payment names the checking account in words; which of the portfolio's
    # own accounts that is, is resolved when the statement is imported (see
    # tests/test_multitenant_ingestion.py).
    payment_tx = [t for t in ext.transactions if "Online payment from CHK 9456" in t.raw_description]
    assert len(payment_tx) == 1
    assert payment_tx[0].amount_cents == -138531


def test_apple_card_parsing():
    pdf_path = DOCS_BASE / "Apple Card" / "Apple Card Statement - December 2025.pdf"
    if not pdf_path.exists():
        return

    ext = parse_apple_card(pdf_path)
    assert ext is not None
    assert ext.parser_name == "AppleCardParser"
    assert ext.statement_type == "CREDIT_CARD"
    assert ext.institution_name == "Goldman Sachs Bank USA"
    assert ext.opening_balance_cents == 266362  # $2,663.62
    assert ext.closing_balance_cents == 490185  # $4,901.85
    assert ext.status == "VALIDATED"
    assert len(ext.transactions) == 77

    # Check ACH payments from BofA checking
    ach_payments = [t for t in ext.transactions if "ACH Deposit" in t.raw_description]
    assert len(ach_payments) == 2
    assert sum(t.amount_cents for t in ach_payments) == -396362  # -$3,963.62


def test_bank_statement_vault_and_database_persistence(tmp_path):
    db_file = tmp_path / "test_bank.db"
    vault_dir = tmp_path / "vault"
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

    # Ingest one BofA checking statement
    chk_dir = DOCS_BASE / "BOFA Checking"
    if not chk_dir.exists():
        return

    with Session(engine) as session:
        report = ingest_folder(chk_dir, session, vault_dir, commit=True)
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
        assert len(txs) > 100  # Across all 3 statements
        states = session.exec(select(TransactionProcessingState)).all()
        assert len(states) == len(txs)
        for s in states:
            assert s.allocation_status == "UNALLOCATED"
            assert s.posting_status == "UNPOSTED"

        # Verify idempotency on second scan
        report2 = ingest_folder(chk_dir, session, vault_dir, commit=True)
        assert report2["duplicates"] == 3
        assert report2["imported"] == 0
