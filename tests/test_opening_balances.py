"""Tests for opening balance baseline migration, equity plug calculation, and idempotency."""

from datetime import date, datetime, timezone
import pytest
from sqlmodel import Session, select
from proptally.db.models import (
    FinancialAccount,
    FinancialInstitution,
    LoanAccount,
    Property,
)
from proptally.domain.accounting.coa import ensure_entity_ledger_accounts
from proptally.domain.accounting.opening_balances import (
    record_opening_balances,
    OpeningAccountBalance,
)
from proptally.db.models import JournalEntryLine
from proptally.core.exceptions import AccountNotFoundError, DuplicatePostingError

# Ledger codes allocated to the entities declared by ``opening_entities``. The
# codes are read from the entity rows rather than hard-coded, because which numbers
# a portfolio uses is portfolio-specific. Bound to module globals because the
# opening-balance lists reference them repeatedly.
CHECKING_CODE: int = 0
ESCROW1_CODE: int = 0
ESCROW2_CODE: int = 0
PROP1_CODE: int = 0
PROP2_CODE: int = 0
PROP3_CODE: int = 0
LOAN1_CODE: int = 0
LOAN2_CODE: int = 0
LOAN3_CODE: int = 0
CARD1_CODE: int = 0
CARD2_CODE: int = 0


@pytest.fixture(name="opening_entities")
def opening_entities_fixture(session: Session) -> Session:
    """Declares the properties, deposit account, cards and mortgage loans whose
    ledger accounts the opening balances are posted to, then allocates their codes."""
    global CHECKING_CODE, ESCROW1_CODE, ESCROW2_CODE
    global PROP1_CODE, PROP2_CODE, PROP3_CODE
    global LOAN1_CODE, LOAN2_CODE, LOAN3_CODE, CARD1_CODE, CARD2_CODE

    institution = FinancialInstitution(id="inst_test", name="Test Bank")
    session.add(institution)
    session.flush()

    checking = FinancialAccount(
        id="acct_test_checking",
        institution_id=institution.id,
        account_name="Test Checking",
        account_type="CHECKING",
        account_number_masked="...0001",
        created_at=datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
    )
    card1 = FinancialAccount(
        id="acct_test_card1",
        institution_id=institution.id,
        account_name="Test Card One",
        account_type="CREDIT_CARD",
        account_number_masked="...0002",
        created_at=datetime(2026, 1, 1, 0, 0, 1, tzinfo=timezone.utc),
    )
    card2 = FinancialAccount(
        id="acct_test_card2",
        institution_id=institution.id,
        account_name="Test Card Two",
        account_type="CREDIT_CARD",
        account_number_masked="...0003",
        created_at=datetime(2026, 1, 1, 0, 0, 2, tzinfo=timezone.utc),
    )
    session.add(checking)
    session.add(card1)
    session.add(card2)

    props = []
    for index in range(3):
        prop = Property(
            id=f"prop_test_{index + 1}",
            name=f"Test Property {index + 1}",
            address_line1=f"{index + 1} Test St",
            city="Springfield",
            zip_code="62701",
            created_at=datetime(2026, 1, 1, 0, 0, index, tzinfo=timezone.utc),
        )
        session.add(prop)
        props.append(prop)
    session.flush()

    loans = []
    for index, prop in enumerate(props):
        mortgage_account = FinancialAccount(
            id=f"acct_test_mortgage_{index + 1}",
            institution_id=institution.id,
            account_name=f"Test Mortgage {index + 1}",
            account_type="MORTGAGE",
            account_number_masked=f"...100{index + 1}",
            created_at=datetime(2026, 1, 1, 0, 0, index, tzinfo=timezone.utc),
        )
        session.add(mortgage_account)
        session.flush()
        loan = LoanAccount(
            id=f"loan_test_{index + 1}",
            financial_account_id=mortgage_account.id,
            property_id=prop.id,
            loan_number=f"TEST-000{index + 1}",
            created_at=datetime(2026, 1, 1, 0, 0, index, tzinfo=timezone.utc),
        )
        session.add(loan)
        loans.append(loan)
    session.flush()

    ensure_entity_ledger_accounts(session)

    CHECKING_CODE = checking.ledger_account_code
    CARD1_CODE = card1.ledger_account_code
    CARD2_CODE = card2.ledger_account_code
    PROP1_CODE, PROP2_CODE, PROP3_CODE = (p.asset_ledger_code for p in props)
    LOAN1_CODE, LOAN2_CODE, LOAN3_CODE = (l.principal_ledger_code for l in loans)
    ESCROW1_CODE, ESCROW2_CODE = loans[0].escrow_ledger_code, loans[1].escrow_ledger_code
    return session


def test_standard_portfolio_opening_balance_with_positive_equity_plug(opening_entities: Session):
    """Verifies opening balances across checking, escrow, properties, mortgages, and credit cards,
    ensuring that the equity difference is credited to Account 3010 (Owner's Equity)."""
    session = opening_entities
    as_of = date(2026, 1, 1)
    balances = [
        # Assets (Debit)
        OpeningAccountBalance(account_code=CHECKING_CODE, amount_cents=2500000, side="DEBIT", description="BoA Checking opening"),
        OpeningAccountBalance(account_code=ESCROW1_CODE, amount_cents=350000, side="DEBIT", description="Rocket Escrow opening"),
        OpeningAccountBalance(account_code=ESCROW2_CODE, amount_cents=420000, side="DEBIT", description="PNC Escrow opening"),
        OpeningAccountBalance(account_code=PROP1_CODE, amount_cents=45000000, side="DEBIT", description="Alder basis"),
        OpeningAccountBalance(account_code=PROP2_CODE, amount_cents=38000000, side="DEBIT", description="Maple basis"),
        OpeningAccountBalance(account_code=PROP3_CODE, amount_cents=32000000, side="DEBIT", description="Rowan basis"),
        # Liabilities (Credit)
        OpeningAccountBalance(account_code=LOAN1_CODE, amount_cents=31000000, side="CREDIT", description="Rocket loan balance"),
        OpeningAccountBalance(account_code=LOAN2_CODE, amount_cents=24000000, side="CREDIT", description="PNC loan balance"),
        OpeningAccountBalance(account_code=LOAN3_CODE, amount_cents=21500000, side="CREDIT", description="CCM loan balance"),
        OpeningAccountBalance(account_code=CARD2_CODE, amount_cents=150000, side="CREDIT", description="Apple Card opening balance"),
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


def test_deficit_opening_balance_with_debit_equity_plug(opening_entities: Session):
    """Verifies that if liabilities exceed assets, Account 3010 is debited to achieve equilibrium."""
    session = opening_entities
    as_of = date(2026, 1, 1)
    balances = [
        OpeningAccountBalance(account_code=CHECKING_CODE, amount_cents=100000, side="DEBIT"),   # $1,000 asset
        OpeningAccountBalance(account_code=CARD1_CODE, amount_cents=500000, side="CREDIT"),  # $5,000 liability
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


def test_perfectly_balanced_opening_balance_requires_no_equity_plug(opening_entities: Session):
    """Verifies that if assets exactly equal liabilities, no line is created for Account 3010."""
    session = opening_entities
    as_of = date(2026, 1, 1)
    balances = [
        OpeningAccountBalance(account_code=CHECKING_CODE, amount_cents=50000, side="DEBIT"),
        OpeningAccountBalance(account_code=CARD1_CODE, amount_cents=50000, side="CREDIT"),
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


def test_duplicate_opening_balance_date_rejected_by_idempotency(opening_entities: Session):
    """Negative test: Attempting to post opening balances twice for the same date raises DuplicatePostingError."""
    session = opening_entities
    as_of = date(2026, 1, 1)
    balances = [
        OpeningAccountBalance(account_code=CHECKING_CODE, amount_cents=10000, side="DEBIT"),
        OpeningAccountBalance(account_code=CARD1_CODE, amount_cents=10000, side="CREDIT"),
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


def test_opening_balance_with_zero_or_negative_amount_raises_error(opening_entities: Session):
    """Negative test: Providing zero or negative amount raises ValueError."""
    session = opening_entities
    with pytest.raises(ValueError):
        record_opening_balances(
            session=session,
            as_of_date=date(2026, 1, 1),
            balances=[OpeningAccountBalance(account_code=CHECKING_CODE, amount_cents=0, side="DEBIT")],
        )

    with pytest.raises(ValueError):
        record_opening_balances(
            session=session,
            as_of_date=date(2026, 1, 1),
            balances=[OpeningAccountBalance(account_code=CHECKING_CODE, amount_cents=-500, side="DEBIT")],
        )
