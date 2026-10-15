"""Tests for ledger immutability triggers, direct SQL bypasses, and compensating reversals."""

from datetime import date
import pytest
from sqlalchemy import text
from sqlmodel import Session, select
from proptally.domain.accounting.ledger import (
    create_journal_entry,
    add_journal_line,
    post_journal_entry,
    reverse_journal_entry,
    is_entry_reversed,
)
from proptally.db.models import JournalEntry, JournalEntryLine, AuditLog
from proptally.core.exceptions import TransactionAlreadyReversedError


def test_compensating_reversal_creates_balanced_entry_and_preserves_original(session: Session):
    """Verifies that reversing a posted journal entry creates an exact compensating reversal entry,
    swaps debits and credits, derives reversal status from reversal_of_id, and preserves immutable audit trail."""
    entry = create_journal_entry(
        session=session,
        entry_date=date(2026, 5, 1),
        entry_type="EXPENSE_POST",
        description="Original repairs payment",
        idempotency_key="TEST:ORIGINAL:001",
    )
    # Line 1: Debit Repairs 5040 ($350.00)
    add_journal_line(session, entry.id, ledger_account_code=5040, debit_cents=35000, description="Repairs")
    # Line 2: Credit BoA Checking 1010 ($350.00)
    add_journal_line(session, entry.id, ledger_account_code=1010, credit_cents=35000, description="Cash out")

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

    # Original credit 35000 in 1010 must now be debit 35000
    line_1010 = next(l for l in rev_lines if l.ledger_account_code == 1010)
    assert line_1010.debit_cents == 35000
    assert line_1010.credit_cents == 0

    # 4. Check audit log has recorded REVERSE
    audit_records = session.exec(
        select(AuditLog).where(AuditLog.entity_id == posted_entry.id)
    ).all()
    actions = [a.action for a in audit_records]
    assert "POST" in actions
    assert "REVERSE" in actions


def test_cannot_reverse_already_reversed_entry(session: Session):
    """Negative test: Attempting to reverse a journal entry that is already marked reversed raises TransactionAlreadyReversedError."""
    entry = create_journal_entry(
        session=session,
        entry_date=date(2026, 5, 1),
        entry_type="EXPENSE_POST",
        description="Entry to be reversed twice",
    )
    add_journal_line(session, entry.id, ledger_account_code=5040, debit_cents=10000)
    add_journal_line(session, entry.id, ledger_account_code=1010, credit_cents=10000)
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


def test_direct_sql_update_lines_of_posted_entry_blocked_by_trigger(session: Session):
    """Negative test: Raw SQL UPDATE on a line belonging to a POSTED entry is blocked by trg_prevent_posted_lines_update."""
    entry = create_journal_entry(session=session, entry_date=date(2026, 5, 1), entry_type="CASH_SETTLEMENT", description="Trigger test")
    line = add_journal_line(session, entry.id, ledger_account_code=2125, debit_cents=5000)
    add_journal_line(session, entry.id, ledger_account_code=1010, credit_cents=5000)
    post_journal_entry(session, entry.id)

    # Direct SQL UPDATE of line amount
    with pytest.raises(Exception) as exc_info:
        session.execute(text(
            f"UPDATE journal_entry_lines SET debit_cents = 99999 WHERE id = '{line.id}';"
        ))
        session.commit()
    assert "ImmutableLedgerError: Cannot update lines of a POSTED journal entry" in str(exc_info.value)


def test_direct_sql_delete_lines_of_posted_entry_blocked_by_trigger(session: Session):
    """Negative test: Raw SQL DELETE on a line belonging to a POSTED entry is blocked by trg_prevent_posted_lines_delete."""
    entry = create_journal_entry(session=session, entry_date=date(2026, 5, 1), entry_type="CASH_SETTLEMENT", description="Trigger test")
    line = add_journal_line(session, entry.id, ledger_account_code=2125, debit_cents=5000)
    add_journal_line(session, entry.id, ledger_account_code=1010, credit_cents=5000)
    post_journal_entry(session, entry.id)

    # Direct SQL DELETE of line
    with pytest.raises(Exception) as exc_info:
        session.execute(text(
            f"DELETE FROM journal_entry_lines WHERE id = '{line.id}';"
        ))
        session.commit()
    assert "ImmutableLedgerError: Cannot delete lines of a POSTED journal entry" in str(exc_info.value)


def test_reversal_succeeds_while_direct_sql_update_on_original_posted_header_is_prohibited(session: Session):
    """Verifies that reversing an entry succeeds purely through compensating relationship,
    while direct SQL attempts to mutate ANY field of the original posted entry remain strictly prohibited."""
    entry = create_journal_entry(
        session=session,
        entry_date=date(2026, 5, 1),
        entry_type="EXPENSE_POST",
        description="Original maintenance expense",
    )
    add_journal_line(session, entry.id, ledger_account_code=5040, debit_cents=20000)
    add_journal_line(session, entry.id, ledger_account_code=1010, credit_cents=20000)
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

