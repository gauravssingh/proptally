"""Post a servicer's statement application against a funded mortgage clearing account.

The second stage of the two-stage mortgage model: the bank settlement posts cash
into the loan's clearing account, and this service releases that cash to
principal, interest and escrow when the servicer's statement confirms how it was
applied. A statement by itself is only a bill and never becomes debt service;
the clearing balance must already cover the application.
"""

from __future__ import annotations

from datetime import date
from typing import Optional

from sqlmodel import Session, select

from proptally.db.models import JournalEntry, JournalEntryLine, LoanAccount, MortgagePeriod
from proptally.domain.accounting.coa import MORTGAGE_INTEREST_CODE
from proptally.domain.accounting.ledger import (
    add_journal_line,
    create_journal_entry,
    post_journal_entry,
)
from proptally.domain.money import format_cents


def apply_mortgage_period(
    session: Session,
    period_id: str,
    *,
    extra_principal_cents: int = 0,
    payment_date: Optional[date] = None,
    actor_id: str = "system",
) -> JournalEntry:
    """Release funded clearing cash into principal, interest and escrow.

    ``extra_principal_cents`` is the amount paid above the statement's scheduled
    principal. It is booked to principal, never to interest or expense, so CADS
    reflects the full principal reduction while NOI is untouched.

    Raises ``ValueError`` when the period is already settled, the loan has no
    clearing account, or the clearing balance does not cover the application.
    """
    period = session.get(MortgagePeriod, period_id)
    if period is None:
        raise ValueError(f"Mortgage period {period_id} not found.")
    if period.status == "FULLY_SETTLED" or period.total_amount_applied_cents > 0:
        raise ValueError("This statement period has already been applied.")

    extra = int(extra_principal_cents or 0)
    if extra < 0:
        raise ValueError("Extra principal cannot be negative.")

    loan = session.get(LoanAccount, period.loan_account_id)
    if loan is None or loan.clearing_ledger_code is None:
        raise ValueError("The loan has no clearing account, so it cannot apply a statement.")

    scheduled = (
        period.principal_billed_cents
        + period.interest_billed_cents
        + period.escrow_billed_cents
    )
    total = scheduled + extra

    clearing_lines = session.exec(
        select(JournalEntryLine)
        .join(JournalEntry, JournalEntryLine.journal_entry_id == JournalEntry.id)
        .where(
            JournalEntry.status == "POSTED",
            JournalEntryLine.ledger_account_code == loan.clearing_ledger_code,
        )
    ).all()
    funded = sum(line.debit_cents - line.credit_cents for line in clearing_lines)
    if funded < total:
        raise ValueError(
            "The mortgage clearing account is not funded for this application: "
            f"{format_cents(funded)} available, {format_cents(total)} required. Post the bank payment first."
        )

    entry_date = payment_date or period.payment_due_date
    entry = create_journal_entry(
        session,
        entry_date,
        "CASH_SETTLEMENT",
        f"Servicer application: loan {loan.loan_number} due {period.payment_due_date.isoformat()}",
        f"MORTGAGE_APP:{period.id}",
    )
    add_journal_line(
        session,
        entry.id,
        loan.principal_ledger_code,
        debit_cents=period.principal_billed_cents + extra,
        property_id=loan.property_id,
        description="Principal applied (including extra principal)",
    )
    add_journal_line(
        session,
        entry.id,
        MORTGAGE_INTEREST_CODE,
        debit_cents=period.interest_billed_cents,
        property_id=loan.property_id,
        description="Interest applied",
    )
    add_journal_line(
        session,
        entry.id,
        loan.escrow_ledger_code,
        debit_cents=period.escrow_billed_cents,
        property_id=loan.property_id,
        description="Escrow funded",
    )
    add_journal_line(
        session,
        entry.id,
        loan.clearing_ledger_code,
        credit_cents=total,
        description="Clearing released",
    )
    post_journal_entry(session, entry.id, commit=False, actor_id=actor_id)

    period.principal_applied_cents = period.principal_billed_cents + extra
    period.interest_applied_cents = period.interest_billed_cents
    period.escrow_applied_cents = period.escrow_billed_cents
    period.total_amount_applied_cents = total
    period.status = "FULLY_SETTLED"
    session.add(period)
    session.commit()
    session.refresh(entry)
    return entry
