"""Tests for opening balance baseline migration, equity plug calculation, and idempotency."""

from datetime import date
import pytest
from sqlmodel import Session, select
from proptally.domain.accounting.opening_balances import (
    record_opening_balances,
    OpeningAccountBalance,
)
from proptally.db.models import JournalEntryLine
from proptally.core.exceptions import AccountNotFoundError, DuplicatePostingError


def test_standard_portfolio_opening_balance_with_positive_equity_plug(session: Session):
    """Verifies opening balances across checking, escrow, properties, mortgages, and credit cards,
    ensuring that the equity difference is credited to Account 3010 (Owner's Equity)."""
    as_of = date(2026, 1, 1)
    balances = [
        # Assets (Debit)
        OpeningAccountBalance(account_code=1010, amount_cents=2500000, side="DEBIT", description="BoA Checking opening"),
        OpeningAccountBalance(account_code=1110, amount_cents=350000, side="DEBIT", description="Rocket Escrow opening"),
        OpeningAccountBalance(account_code=1120, amount_cents=420000, side="DEBIT", description="PNC Escrow opening"),
        OpeningAccountBalance(account_code=1510, amount_cents=45000000, side="DEBIT", description="Alder basis"),
        OpeningAccountBalance(account_code=1520, amount_cents=38000000, side="DEBIT", description="Maple basis"),
        OpeningAccountBalance(account_code=1530, amount_cents=32000000, side="DEBIT", description="Rowan basis"),
        # Liabilities (Credit)
        OpeningAccountBalance(account_code=2110, amount_cents=31000000, side="CREDIT", description="Rocket loan balance"),
        OpeningAccountBalance(account_code=2120, amount_cents=24000000, side="CREDIT", description="PNC loan balance"),
        OpeningAccountBalance(account_code=2130, amount_cents=21500000, side="CREDIT", description="CCM loan balance"),
        OpeningAccountBalance(account_code=2020, amount_cents=150000, side="CREDIT", description="Apple Card opening balance"),
    ]

    total_assets = 2500000 + 350000 + 420000 + 45000000 + 38000000 + 32000000  # 118,270,000 cents ($1,182,700.00)
    total_liabilities = 31000000 + 24000000 + 21500000 + 150000              # 76,650,000 cents ($766,500.00)
    expected_equity_credit = total_assets - total_liabilities                # 41,620,000 cents ($416,200.00)

    entry = record_opening_balances(
        session=session,
        as_of_date=as_of,
        balances=balances,
        description="Portfolio 2026 opening baseline",
    )

    assert entry.status == "POSTED"
    assert entry.entry_type == "OPENING_BALANCE"
    assert entry.entry_date == as_of

    lines = session.exec(
        select(JournalEntryLine).where(JournalEntryLine.journal_entry_id == entry.id)
    ).all()

    total_debits = sum(l.debit_cents for l in lines)
    total_credits = sum(l.credit_cents for l in lines)

    assert total_debits == total_assets
    assert total_credits == total_assets  # Fully balanced with equity plug!

    equity_line = next(l for l in lines if l.ledger_account_code == 3010)
    assert equity_line.credit_cents == expected_equity_credit
    assert equity_line.debit_cents == 0


def test_deficit_opening_balance_with_debit_equity_plug(session: Session):
    """Verifies that if liabilities exceed assets, Account 3010 is debited to achieve equilibrium."""
    as_of = date(2026, 1, 1)
    balances = [
        OpeningAccountBalance(account_code=1010, amount_cents=100000, side="DEBIT"),   # $1,000 asset
        OpeningAccountBalance(account_code=2010, amount_cents=500000, side="CREDIT"),  # $5,000 liability
    ]

    entry = record_opening_balances(
        session=session,
        as_of_date=as_of,
        balances=balances,
    )

    lines = session.exec(
        select(JournalEntryLine).where(JournalEntryLine.journal_entry_id == entry.id)
    ).all()

    equity_line = next(l for l in lines if l.ledger_account_code == 3010)
    assert equity_line.debit_cents == 400000  # $4,000 equity deficit plug
    assert equity_line.credit_cents == 0

    total_debits = sum(l.debit_cents for l in lines)
    total_credits = sum(l.credit_cents for l in lines)
    assert total_debits == total_credits == 500000


def test_perfectly_balanced_opening_balance_requires_no_equity_plug(session: Session):
    """Verifies that if assets exactly equal liabilities, no line is created for Account 3010."""
    as_of = date(2026, 1, 1)
    balances = [
        OpeningAccountBalance(account_code=1010, amount_cents=50000, side="DEBIT"),
        OpeningAccountBalance(account_code=2010, amount_cents=50000, side="CREDIT"),
    ]

    entry = record_opening_balances(
        session=session,
        as_of_date=as_of,
        balances=balances,
    )

    lines = session.exec(
        select(JournalEntryLine).where(JournalEntryLine.journal_entry_id == entry.id)
    ).all()

    equity_lines = [l for l in lines if l.ledger_account_code == 3010]
    assert len(equity_lines) == 0
    assert sum(l.debit_cents for l in lines) == sum(l.credit_cents for l in lines) == 50000


def test_duplicate_opening_balance_date_rejected_by_idempotency(session: Session):
    """Negative test: Attempting to post opening balances twice for the same date raises DuplicatePostingError."""
    as_of = date(2026, 1, 1)
    balances = [
        OpeningAccountBalance(account_code=1010, amount_cents=10000, side="DEBIT"),
        OpeningAccountBalance(account_code=2010, amount_cents=10000, side="CREDIT"),
    ]

    record_opening_balances(session=session, as_of_date=as_of, balances=balances)

    with pytest.raises(DuplicatePostingError):
        record_opening_balances(session=session, as_of_date=as_of, balances=balances)


def test_opening_balance_with_nonexistent_account_raises_error(session: Session):
    """Negative test: Providing an unknown account code raises AccountNotFoundError."""
    with pytest.raises(AccountNotFoundError):
        record_opening_balances(
            session=session,
            as_of_date=date(2026, 1, 1),
            balances=[OpeningAccountBalance(account_code=9999, amount_cents=1000, side="DEBIT")],
        )


def test_opening_balance_with_zero_or_negative_amount_raises_error(session: Session):
    """Negative test: Providing zero or negative amount raises ValueError."""
    with pytest.raises(ValueError):
        record_opening_balances(
            session=session,
            as_of_date=date(2026, 1, 1),
            balances=[OpeningAccountBalance(account_code=1010, amount_cents=0, side="DEBIT")],
        )

    with pytest.raises(ValueError):
        record_opening_balances(
            session=session,
            as_of_date=date(2026, 1, 1),
            balances=[OpeningAccountBalance(account_code=1010, amount_cents=-500, side="DEBIT")],
        )
