"""Tests for ledger immutability triggers, direct SQL bypasses, and compensating reversals."""

from datetime import date
import pytest
from sqlalchemy import text
from sqlmodel import Session, select
from proptally.db.models import (
    AuditLog,
    FinancialAccount,
    FinancialInstitution,
    JournalEntry,
    JournalEntryLine,
    LoanAccount,
    Property,
)
from proptally.domain.accounting.coa import ensure_entity_ledger_accounts
from proptally.domain.accounting.ledger import (
    create_journal_entry,
    add_journal_line,
    post_journal_entry,
    reverse_journal_entry,
    is_entry_reversed,
)
from proptally.core.exceptions import TransactionAlreadyReversedError

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


def test_compensating_reversal_creates_balanced_entry_and_preserves_original(ledger_entities: Session):
    """Verifies that reversing a posted journal entry creates an exact compensating reversal entry,
    swaps debits and credits, derives reversal status from reversal_of_id, and preserves immutable audit trail."""
    session = ledger_entities
    entry = create_journal_entry(
        session=session,
        entry_date=date(2026, 5, 1),
        entry_type="EXPENSE_POST",
        description="Original repairs payment",
        idempotency_key="TEST:ORIGINAL:001",
    )
    # Line 1: Debit Repairs 5040 ($350.00)
    add_journal_line(session, entry.id, ledger_account_code=5040, debit_cents=35000, description="Repairs")
    # Line 2: Credit the checking account ($350.00)
    add_journal_line(session, entry.id, ledger_account_code=CHECKING_CODE, credit_cents=35000, description="Cash out")

    posted_entry = post_journal_entry(session, entry.id)
    assert posted_entry.status == "POSTED"
    assert is_entry_reversed(session, posted_entry.id) is False

    # Execute reversal
    reversal_date = date(2026, 5, 5)
    reversal_entry = reverse_journal_entry(
        session=session,
        entry_id=posted_entry.id,
        reversal_date=reversal_date,
        reason="Duplicate entry detected",
    )

    # 1. Check reversal entry properties
    assert reversal_entry.status == "POSTED"
    assert reversal_entry.entry_type == "REVERSAL"
    assert reversal_entry.entry_date == reversal_date
    assert reversal_entry.reversal_of_id == posted_entry.id
    assert "reversal of" in reversal_entry.description.lower()

    # 2. Check original entry state: derived status is reversed, but header row was NOT mutated
    assert is_entry_reversed(session, posted_entry.id) is True
    session.refresh(posted_entry)
    assert posted_entry.status == "POSTED"
    assert posted_entry.description == "Original repairs payment"
    assert posted_entry.reversal_of_id is None

    # 3. Check reversal lines swapped exactly
    rev_lines = session.exec(
        select(JournalEntryLine).where(JournalEntryLine.journal_entry_id == reversal_entry.id)
    ).all()
    assert len(rev_lines) == 2

    # Original debit 35000 in 5040 must now be credit 35000
    line_5040 = next(l for l in rev_lines if l.ledger_account_code == 5040)
    assert line_5040.credit_cents == 35000
    assert line_5040.debit_cents == 0

    # Original credit 35000 in the checking account must now be debit 35000
    line_checking = next(l for l in rev_lines if l.ledger_account_code == CHECKING_CODE)
    assert line_checking.debit_cents == 35000
    assert line_checking.credit_cents == 0

    # 4. Check audit log has recorded REVERSE
    audit_records = session.exec(
        select(AuditLog).where(AuditLog.entity_id == posted_entry.id)
    ).all()
    actions = [a.action for a in audit_records]
    assert "POST" in actions
    assert "REVERSE" in actions


def test_cannot_reverse_already_reversed_entry(ledger_entities: Session):
    """Negative test: Attempting to reverse a journal entry that is already marked reversed raises TransactionAlreadyReversedError."""
    session = ledger_entities
    entry = create_journal_entry(
        session=session,
        entry_date=date(2026, 5, 1),
        entry_type="EXPENSE_POST",
        description="Entry to be reversed twice",
    )
    add_journal_line(session, entry.id, ledger_account_code=5040, debit_cents=10000)
    add_journal_line(session, entry.id, ledger_account_code=CHECKING_CODE, credit_cents=10000)
    post_journal_entry(session, entry.id)

    # First reversal succeeds
    reverse_journal_entry(
        session=session,
        entry_id=entry.id,
        reversal_date=date(2026, 5, 2),
        reason="Initial reversal",
    )

    # Second reversal must fail
    with pytest.raises(TransactionAlreadyReversedError) as exc_info:
        reverse_journal_entry(
            session=session,
            entry_id=entry.id,
            reversal_date=date(2026, 5, 3),
            reason="Second reversal attempt",
        )
    assert "is already reversed" in str(exc_info.value)


def test_cannot_reverse_unposted_draft_entry(session: Session):
    """Negative test: Attempting to reverse a draft (unposted) entry raises ValueError."""
    entry = create_journal_entry(
        session=session,
        entry_date=date(2026, 5, 1),
        entry_type="EXPENSE_POST",
        description="Draft entry",
    )
    with pytest.raises(ValueError) as exc_info:
        reverse_journal_entry(
            session=session,
            entry_id=entry.id,
            reversal_date=date(2026, 5, 2),
            reason="Illegal attempt on draft",
        )
    assert "must be 'POSTED'" in str(exc_info.value)


def test_direct_sql_update_lines_of_posted_entry_blocked_by_trigger(ledger_entities: Session):
    """Negative test: Raw SQL UPDATE on a line belonging to a POSTED entry is blocked by trg_prevent_posted_lines_update."""
    session = ledger_entities
    entry = create_journal_entry(session=session, entry_date=date(2026, 5, 1), entry_type="CASH_SETTLEMENT", description="Trigger test")
    line = add_journal_line(session, entry.id, ledger_account_code=MORTGAGE_CLEARING_CODE, debit_cents=5000)
    add_journal_line(session, entry.id, ledger_account_code=CHECKING_CODE, credit_cents=5000)
    post_journal_entry(session, entry.id)

    # Direct SQL UPDATE of line amount
    with pytest.raises(Exception) as exc_info:
        session.execute(text(
            f"UPDATE journal_entry_lines SET debit_cents = 99999 WHERE id = '{line.id}';"
        ))
        session.commit()
    assert "ImmutableLedgerError: Cannot update lines of a POSTED journal entry" in str(exc_info.value)


def test_direct_sql_delete_lines_of_posted_entry_blocked_by_trigger(ledger_entities: Session):
    """Negative test: Raw SQL DELETE on a line belonging to a POSTED entry is blocked by trg_prevent_posted_lines_delete."""
    session = ledger_entities
    entry = create_journal_entry(session=session, entry_date=date(2026, 5, 1), entry_type="CASH_SETTLEMENT", description="Trigger test")
    line = add_journal_line(session, entry.id, ledger_account_code=MORTGAGE_CLEARING_CODE, debit_cents=5000)
    add_journal_line(session, entry.id, ledger_account_code=CHECKING_CODE, credit_cents=5000)
    post_journal_entry(session, entry.id)

    # Direct SQL DELETE of line
    with pytest.raises(Exception) as exc_info:
        session.execute(text(
            f"DELETE FROM journal_entry_lines WHERE id = '{line.id}';"
        ))
        session.commit()
    assert "ImmutableLedgerError: Cannot delete lines of a POSTED journal entry" in str(exc_info.value)


def test_reversal_succeeds_while_direct_sql_update_on_original_posted_header_is_prohibited(ledger_entities: Session):
    """Verifies that reversing an entry succeeds purely through compensating relationship,
    while direct SQL attempts to mutate ANY field of the original posted entry remain strictly prohibited."""
    session = ledger_entities
    entry = create_journal_entry(
        session=session,
        entry_date=date(2026, 5, 1),
        entry_type="EXPENSE_POST",
        description="Original maintenance expense",
    )
    add_journal_line(session, entry.id, ledger_account_code=5040, debit_cents=20000)
    add_journal_line(session, entry.id, ledger_account_code=CHECKING_CODE, credit_cents=20000)
    post_journal_entry(session, entry.id)

    # 1. Direct SQL update to header is blocked by prevent_posted_entry_update
    with pytest.raises(Exception) as exc_info:
        session.execute(text(
            f"UPDATE journal_entries SET description = 'tampered' WHERE id = '{entry.id}';"
        ))
        session.commit()
    assert "ImmutableLedgerError: Cannot update a POSTED journal entry" in str(exc_info.value)

    # 2. Reversal succeeds without mutating the original entry
    rev_entry = reverse_journal_entry(
        session=session,
        entry_id=entry.id,
        reversal_date=date(2026, 5, 2),
        reason="Invoice voided",
    )
    assert rev_entry.status == "POSTED"
    assert rev_entry.reversal_of_id == entry.id

    # 3. Original entry header remains unchanged and derived status is True
    session.refresh(entry)
    assert entry.status == "POSTED"
    assert entry.description == "Original maintenance expense"
    assert is_entry_reversed(session, entry.id) is True

    # 4. Direct SQL update to original header STILL fails (immutability preserved forever)
    with pytest.raises(Exception) as exc_info:
        session.execute(text(
            f"UPDATE journal_entries SET description = 'tampered_after_reversal' WHERE id = '{entry.id}';"
        ))
        session.commit()
    assert "ImmutableLedgerError: Cannot update a POSTED journal entry" in str(exc_info.value)

