"""Double-entry journal posting, balance verification, and reversal service."""

from datetime import date, datetime, timezone
from typing import Optional, List, Dict, Any
from uuid import uuid4
from sqlmodel import Session, select
from proptally.db.models import JournalEntry, JournalEntryLine, LedgerAccount, AuditLog
from proptally.core.exceptions import (
    UnbalancedJournalEntryError,
    EmptyJournalEntryError,
    ImmutableLedgerError,
    DuplicatePostingError,
    AccountNotFoundError,
    TransactionAlreadyReversedError,
)


def create_journal_entry(
    session: Session,
    entry_date: date,
    entry_type: str,
    description: str,
    idempotency_key: Optional[str] = None,
    source_transaction_id: Optional[str] = None,
) -> JournalEntry:
    """Creates a new unposted journal entry header in DRAFT status.

    ``source_transaction_id`` records the transaction an entry was posted from so
    metrics can attribute cash movements through the originating allocation.
    """
    if idempotency_key:
        existing = session.exec(
            select(JournalEntry).where(JournalEntry.idempotency_key == idempotency_key)
        ).first()
        if existing:
            raise DuplicatePostingError(f"Journal entry with idempotency_key '{idempotency_key}' already exists.")

    entry = JournalEntry(
        id=str(uuid4()),
        entry_date=entry_date,
        entry_type=entry_type,
        status="DRAFT",
        idempotency_key=idempotency_key,
        source_transaction_id=source_transaction_id,
        description=description,
    )
    session.add(entry)
    session.flush()
    return entry


def add_journal_line(
    session: Session,
    entry_id: str,
    ledger_account_code: int,
    debit_cents: int = 0,
    credit_cents: int = 0,
    property_id: Optional[str] = None,
    description: Optional[str] = None,
) -> JournalEntryLine:
    """Appends a line to a DRAFT journal entry, validating account existence and signs."""
    # Check parent entry status
    entry = session.get(JournalEntry, entry_id)
    if not entry:
        raise ValueError(f"Journal entry {entry_id} not found.")
    if entry.status == "POSTED":
        raise ImmutableLedgerError("Cannot add lines to a POSTED journal entry.")

    # Validate account existence
    acct = session.get(LedgerAccount, ledger_account_code)
    if not acct:
        raise AccountNotFoundError(f"Ledger account {ledger_account_code} does not exist.")

    # Validate non-negative integer cents and exactly one side active
    if debit_cents < 0 or credit_cents < 0:
        raise ValueError("Line debit and credit cents must be non-negative.")
    if (debit_cents > 0 and credit_cents > 0) or (debit_cents == 0 and credit_cents == 0):
        raise ValueError("A journal line must specify either debit_cents > 0 or credit_cents > 0, not both or neither.")

    line = JournalEntryLine(
        id=str(uuid4()),
        journal_entry_id=entry_id,
        ledger_account_code=ledger_account_code,
        property_id=property_id,
        debit_cents=debit_cents,
        credit_cents=credit_cents,
        description=description,
    )
    session.add(line)
    session.flush()
    return line


def post_journal_entry(session: Session, entry_id: str, commit: bool = True) -> JournalEntry:
    """Atomically validates equilibrium and posts a journal entry to permanent ledger.

    Invariants enforced:
    1. Entry must exist and have status 'DRAFT' or 'BALANCED'.
    2. Entry must have >= 1 line.
    3. Sum of debits must strictly equal sum of credits.
    4. Total debits must be strictly greater than zero.

    Pass ``commit=False`` to join a caller-owned transaction; the caller is then
    responsible for committing or rolling back.
    """
    entry = session.get(JournalEntry, entry_id)
    if not entry:
        raise ValueError(f"Journal entry {entry_id} not found.")
    if entry.status == "POSTED":
        raise ImmutableLedgerError(f"Journal entry {entry_id} is already POSTED.")

    lines = session.exec(
        select(JournalEntryLine).where(JournalEntryLine.journal_entry_id == entry_id)
    ).all()

    if not lines:
        raise EmptyJournalEntryError(f"Journal entry {entry_id} has no lines and cannot be posted.")

    total_debits = sum(line.debit_cents for line in lines)
    total_credits = sum(line.credit_cents for line in lines)

    if total_debits != total_credits:
        raise UnbalancedJournalEntryError(
            f"Unbalanced journal entry {entry_id}: total debits ({total_debits}) != total credits ({total_credits})."
        )
    if total_debits <= 0:
        raise UnbalancedJournalEntryError(
            f"Journal entry {entry_id} total amount must be strictly greater than 0, got {total_debits}."
        )

    # Transition status to POSTED
    entry.status = "POSTED"
    entry.posted_at = datetime.now(timezone.utc)
    session.add(entry)

    # Record audit log
    audit = AuditLog(
        id=str(uuid4()),
        entity_name="journal_entries",
        entity_id=entry_id,
        action="POST",
        changed_by="system",
        new_values_json=f'{{"total_cents": {total_debits}, "lines_count": {len(lines)}}}',
    )
    session.add(audit)
    if commit:
        session.commit()
        session.refresh(entry)
    else:
        session.flush()
    return entry


def is_entry_reversed(session: Session, entry_id: str) -> bool:
    """Derives reversal status from whether a posted compensating reversal entry exists referencing this entry."""
    rev = session.exec(
        select(JournalEntry).where(
            JournalEntry.reversal_of_id == entry_id,
            JournalEntry.status == "POSTED",
        )
    ).first()
    return rev is not None


def reverse_journal_entry(
    session: Session,
    entry_id: str,
    reversal_date: date,
    reason: str,
    commit: bool = True,
) -> JournalEntry:
    """Creates and posts an independent compensating reversal entry, preserving audit history.

    The original entry remains 100% physically immutable in the database.
    Reversal status is derived through the reversal_of_id reference on the compensating entry.

    Pass ``commit=False`` to join a caller-owned transaction; the caller is then
    responsible for committing or rolling back.
    """
    orig_entry = session.get(JournalEntry, entry_id)
    if not orig_entry:
        raise ValueError(f"Journal entry {entry_id} not found.")
    if orig_entry.status != "POSTED":
        raise ValueError(f"Cannot reverse entry {entry_id} with status '{orig_entry.status}'; must be 'POSTED'.")
    if is_entry_reversed(session, entry_id):
        raise TransactionAlreadyReversedError(f"Journal entry {entry_id} is already reversed.")

    orig_lines = session.exec(
        select(JournalEntryLine).where(JournalEntryLine.journal_entry_id == entry_id)
    ).all()

    # 1. Create compensating reversal header
    reversal_header = JournalEntry(
        id=str(uuid4()),
        entry_date=reversal_date,
        entry_type="REVERSAL",
        status="DRAFT",
        reversal_of_id=orig_entry.id,
        description=f"Compensating reversal of {orig_entry.id}: {reason}",
    )
    session.add(reversal_header)
    session.flush()

    # 2. Add inverted lines (debits become credits, credits become debits)
    for line in orig_lines:
        rev_line = JournalEntryLine(
            id=str(uuid4()),
            journal_entry_id=reversal_header.id,
            ledger_account_code=line.ledger_account_code,
            property_id=line.property_id,
            debit_cents=line.credit_cents,   # INVERTED
            credit_cents=line.debit_cents,   # INVERTED
            description=f"Reversal of line {line.id}",
        )
        session.add(rev_line)
    session.flush()

    # 3. Post the reversal entry
    posted_reversal = post_journal_entry(session, reversal_header.id, commit=commit)

    # 4. Record audit log (original entry is NOT mutated; remains strictly immutable)
    audit = AuditLog(
        id=str(uuid4()),
        entity_name="journal_entries",
        entity_id=orig_entry.id,
        action="REVERSE",
        changed_by="system",
        reason=reason,
        new_values_json=f'{{"reversal_entry_id": "{posted_reversal.id}"}}',
    )
    session.add(audit)
    if commit:
        session.commit()
    else:
        session.flush()
    return posted_reversal
