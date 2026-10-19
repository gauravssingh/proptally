"""Tests for double-entry ledger posting, balancing, and database immutability triggers."""

from datetime import date

import pytest
from sqlalchemy import text
from sqlmodel import Session

from proptally.core.exceptions import (
    AccountNotFoundError,
    DuplicatePostingError,
    EmptyJournalEntryError,
    UnbalancedJournalEntryError,
)
from proptally.db.models import FinancialAccount, FinancialInstitution, LoanAccount, Property
from proptally.domain.accounting.coa import ensure_entity_ledger_accounts
from proptally.domain.accounting.ledger import (
    add_journal_line,
    create_journal_entry,
    post_journal_entry,
)

# Ledger codes allocated to the entities declared by ``ledger_entities``. The codes
# are read from the entity rows rather than hard-coded, because which numbers a
# portfolio uses is portfolio-specific. Bound to module globals because many test
# bodies in this module post to the same two accounts.
CHECKING_CODE: int = 0
MORTGAGE_CLEARING_CODE: int = 0


@pytest.fixture(name="ledger_entities")
def ledger_entities_fixture(session: Session) -> Session:
    """Declares the deposit account and mortgage loan that own the ledger codes
    these tests post to, then allocates those codes from the entity rows."""
    global CHECKING_CODE, MORTGAGE_CLEARING_CODE

    institution = FinancialInstitution(id="inst_test", name="Test Bank")
    session.add(institution)
    session.flush()

    checking = FinancialAccount(
        id="acct_test_checking",
        institution_id=institution.id,
        account_name="Test Checking",
        account_type="CHECKING",
        account_number_masked="...0001",
    )
    mortgage_account = FinancialAccount(
        id="acct_test_mortgage",
        institution_id=institution.id,
        account_name="Test Mortgage",
        account_type="MORTGAGE",
        account_number_masked="...0002",
    )
    prop = Property(
        id="prop_test",
        name="Test Property",
        address_line1="1 Test St",
        city="Springfield",
        zip_code="62701",
    )
    session.add(checking)
    session.add(mortgage_account)
    session.add(prop)
    session.flush()

    loan = LoanAccount(
        id="loan_test",
        financial_account_id=mortgage_account.id,
        property_id=prop.id,
        loan_number="TEST-0001",
    )
    session.add(loan)
    session.flush()

    ensure_entity_ledger_accounts(session)

    CHECKING_CODE = checking.ledger_account_code
    MORTGAGE_CLEARING_CODE = loan.clearing_ledger_code
    return session


def test_successful_balanced_posting(ledger_entities: Session):
    """Verifies that a mathematically balanced entry transitions to POSTED successfully."""
    session = ledger_entities
    entry = create_journal_entry(
        session=session,
        entry_date=date(2026, 5, 1),
        entry_type="CASH_SETTLEMENT",
        description="Mortgage clearing settlement",
        idempotency_key="TEST:BALANCED:001",
    )
    # Debit Mortgage Clearing $1,801.48 (180148 cents)
    add_journal_line(session, entry.id, ledger_account_code=MORTGAGE_CLEARING_CODE, debit_cents=180148)
    # Credit BoA Checking $1,801.48 (180148 cents)
    add_journal_line(session, entry.id, ledger_account_code=CHECKING_CODE, credit_cents=180148)

    posted = post_journal_entry(session, entry.id)
    assert posted.status == "POSTED"
    assert posted.posted_at is not None


def test_empty_journal_entry_cannot_be_posted(session: Session):
    """Negative test: An entry with zero lines must raise EmptyJournalEntryError."""
    entry = create_journal_entry(
        session=session,
        entry_date=date(2026, 5, 1),
        entry_type="CASH_SETTLEMENT",
        description="Empty entry test",
    )
    with pytest.raises(EmptyJournalEntryError):
        post_journal_entry(session, entry.id)


def test_unbalanced_journal_entry_cannot_be_posted(ledger_entities: Session):
    """Negative test: An entry where debits != credits must raise UnbalancedJournalEntryError."""
    session = ledger_entities
    entry = create_journal_entry(
        session=session,
        entry_date=date(2026, 5, 1),
        entry_type="EXPENSE_POST",
        description="Unbalanced entry test",
    )
    add_journal_line(session, entry.id, ledger_account_code=5040, debit_cents=25000)
    add_journal_line(session, entry.id, ledger_account_code=CHECKING_CODE, credit_cents=24900)  # Off by $1.00

    with pytest.raises(UnbalancedJournalEntryError) as exc_info:
        post_journal_entry(session, entry.id)
    assert "total debits (25000) != total credits (24900)" in str(exc_info.value)


def test_zero_amount_entry_cannot_be_posted(session: Session):
    """Negative test: An entry with 0 debits and 0 credits cannot be posted."""
    entry = create_journal_entry(
        session=session,
        entry_date=date(2026, 5, 1),
        entry_type="EXPENSE_POST",
        description="Zero amount test",
    )
    with pytest.raises(ValueError):
        add_journal_line(session, entry.id, ledger_account_code=5040, debit_cents=0, credit_cents=0)


def test_nonexistent_account_code_raises_error(session: Session):
    """Negative test: Adding a line with an invalid account code raises AccountNotFoundError."""
    entry = create_journal_entry(
        session=session,
        entry_date=date(2026, 5, 1),
        entry_type="EXPENSE_POST",
        description="Bad account test",
    )
    with pytest.raises(AccountNotFoundError):
        add_journal_line(session, entry.id, ledger_account_code=9999, debit_cents=1000)


def test_duplicate_idempotency_key_rejected(session: Session):
    """Negative test: Duplicate idempotency key raises DuplicatePostingError."""
    create_journal_entry(
        session=session,
        entry_date=date(2026, 5, 1),
        entry_type="CASH_SETTLEMENT",
        description="First attempt",
        idempotency_key="UNIQUE_KEY_123",
    )
    with pytest.raises(DuplicatePostingError):
        create_journal_entry(
            session=session,
            entry_date=date(2026, 5, 1),
            entry_type="CASH_SETTLEMENT",
            description="Duplicate attempt",
            idempotency_key="UNIQUE_KEY_123",
        )


def test_direct_sql_bypass_insert_lines_into_posted_entry_blocked_by_trigger(ledger_entities: Session):
    """Negative test: Bypassing Python and running raw SQL INSERT into a POSTED entry must be aborted by database trigger."""
    session = ledger_entities
    entry = create_journal_entry(session=session, entry_date=date(2026, 5, 1), entry_type="CASH_SETTLEMENT", description="Trigger test")
    add_journal_line(session, entry.id, ledger_account_code=MORTGAGE_CLEARING_CODE, debit_cents=1000)
    add_journal_line(session, entry.id, ledger_account_code=CHECKING_CODE, credit_cents=1000)
    post_journal_entry(session, entry.id)

    # Attempt direct SQL bypass to insert an unauthorized line into POSTED entry
    with pytest.raises(Exception) as exc_info:
        session.execute(text(
            f"INSERT INTO journal_entry_lines (id, journal_entry_id, ledger_account_code, debit_cents, credit_cents) "
            f"VALUES ('hacked_line', '{entry.id}', 5040, 500, 0);"
        ))
        session.commit()
    assert "ImmutableLedgerError: Cannot insert lines into a POSTED journal entry" in str(exc_info.value)


def test_direct_sql_bypass_update_posted_entry_blocked_by_trigger(ledger_entities: Session):
    """Negative test: Bypassing Python and running raw SQL UPDATE on a POSTED entry must be aborted by database trigger."""
    session = ledger_entities
    entry = create_journal_entry(session=session, entry_date=date(2026, 5, 1), entry_type="CASH_SETTLEMENT", description="Trigger test")
    add_journal_line(session, entry.id, ledger_account_code=MORTGAGE_CLEARING_CODE, debit_cents=1000)
    add_journal_line(session, entry.id, ledger_account_code=CHECKING_CODE, credit_cents=1000)
    post_journal_entry(session, entry.id)

    # Attempt direct SQL update of description on POSTED entry
    with pytest.raises(Exception) as exc_info:
        session.execute(text(
            f"UPDATE journal_entries SET description = 'tampered' WHERE id = '{entry.id}';"
        ))
        session.commit()
    assert "ImmutableLedgerError: Cannot update a POSTED journal entry" in str(exc_info.value)


def test_direct_sql_bypass_delete_posted_entry_blocked_by_trigger(ledger_entities: Session):
    """Negative test: Bypassing Python and running raw SQL DELETE on a POSTED entry must be aborted by database trigger."""
    session = ledger_entities
    entry = create_journal_entry(session=session, entry_date=date(2026, 5, 1), entry_type="CASH_SETTLEMENT", description="Trigger test")
    add_journal_line(session, entry.id, ledger_account_code=MORTGAGE_CLEARING_CODE, debit_cents=1000)
    add_journal_line(session, entry.id, ledger_account_code=CHECKING_CODE, credit_cents=1000)
    post_journal_entry(session, entry.id)

    # Attempt direct SQL delete of POSTED entry
    with pytest.raises(Exception) as exc_info:
        session.execute(text(
            f"DELETE FROM journal_entries WHERE id = '{entry.id}';"
        ))
        session.commit()
    assert "ImmutableLedgerError: Cannot delete a POSTED journal entry" in str(exc_info.value)
