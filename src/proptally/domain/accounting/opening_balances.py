"""Controlled opening balance migration service for PropTally."""

from datetime import date
from typing import List, Optional

from pydantic import BaseModel
from sqlmodel import Session, select

from proptally.core.exceptions import AccountNotFoundError, DuplicatePostingError
from proptally.db.models import JournalEntry, LedgerAccount, LoanAccount
from proptally.domain.accounting.coa import OWNER_EQUITY_CODE
from proptally.domain.accounting.ledger import add_journal_line, create_journal_entry, post_journal_entry


class OpeningAccountBalance(BaseModel):
    """Specification for a baseline account balance."""
    account_code: int
    amount_cents: int  # Must be strictly positive integer cents
    side: str  # 'DEBIT' | 'CREDIT'
    property_id: Optional[str] = None
    description: Optional[str] = None


def record_opening_balances(
    session: Session,
    as_of_date: date,
    balances: List[OpeningAccountBalance],
    description: str = "Portfolio baseline opening balances",
) -> JournalEntry:
    """Creates, balances, and posts an official opening balance journal entry.
    
    The balancing difference between total assets and total liabilities is automatically
    attributed to Account 3010 (Owner's Equity / Capital), guaranteeing double-entry equilibrium.
    Idempotency key prevents duplicate opening balance execution.
    """
    # 1. Validate all balance specifications upfront before creating journal entry
    for b in balances:
        if b.amount_cents <= 0:
            raise ValueError(f"Account {b.account_code} opening balance must be greater than zero.")
        if b.side not in ("DEBIT", "CREDIT"):
            raise ValueError(f"Side must be 'DEBIT' or 'CREDIT', got '{b.side}'.")
        acct = session.get(LedgerAccount, b.account_code)
        if not acct:
            raise AccountNotFoundError(f"Account {b.account_code} does not exist in Chart of Accounts.")

    idempotency_key = f"OPENING_BALANCE:{as_of_date.isoformat()}"
    
    # Check if opening balances for this date already exist
    existing = session.exec(
        select(JournalEntry).where(JournalEntry.idempotency_key == idempotency_key)
    ).first()
    if existing:
        raise DuplicatePostingError(f"Opening balances for {as_of_date} have already been posted.")

    entry = create_journal_entry(
        session=session,
        entry_date=as_of_date,
        entry_type="OPENING_BALANCE",
        description=description,
        idempotency_key=idempotency_key,
    )

    total_debits = 0
    total_credits = 0

    for b in balances:
        if b.side == "DEBIT":
            add_journal_line(
                session, entry.id, b.account_code, debit_cents=b.amount_cents,
                property_id=b.property_id, description=b.description or "Opening asset balance"
            )
            total_debits += b.amount_cents
        elif b.side == "CREDIT":
            add_journal_line(
                session, entry.id, b.account_code, credit_cents=b.amount_cents,
                property_id=b.property_id, description=b.description or "Opening liability balance"
            )
            total_credits += b.amount_cents

    # Calculate equity plug for Account 3010 (Owner's Equity / Capital)
    equity_diff = total_debits - total_credits
    if equity_diff > 0:
        # Assets exceed liabilities -> Credit Equity
        add_journal_line(
            session, entry.id, 3010, credit_cents=equity_diff,
            description="Initial owner equity capital baseline"
        )
    elif equity_diff < 0:
        # Liabilities exceed assets -> Debit Equity
        add_journal_line(
            session, entry.id, 3010, debit_cents=abs(equity_diff),
            description="Initial owner equity deficit baseline"
        )
    # If equity_diff == 0, already balanced!

    posted_entry = post_journal_entry(session, entry.id)
    return posted_entry


def post_loan_opening_balances(
    session: Session,
    loan: LoanAccount,
    principal_cents: Optional[int],
    escrow_cents: Optional[int],
    *,
    commit: bool = True,
) -> JournalEntry | None:
    """Record a loan's opening principal and escrow reserve exactly once.

    Both figures belong to the loan, not to a dated portfolio baseline: offering
    the same balance in two places is how it gets counted twice. Idempotent on
    ``MORTGAGE_OPENING:<loan id>``, so a retried onboarding or a resubmitted
    wizard cannot double the debt.
    """
    principal_cents = int(principal_cents or 0)
    escrow_cents = int(escrow_cents or 0)
    if principal_cents <= 0 and escrow_cents <= 0:
        return None
    if loan.principal_ledger_code is None or loan.escrow_ledger_code is None:
        # Allocation may not have run yet; run it explicitly rather than guess.
        from proptally.domain.accounting.coa import ensure_entity_ledger_accounts

        ensure_entity_ledger_accounts(session, commit=False)
        session.refresh(loan)
    if loan.principal_ledger_code is None or loan.escrow_ledger_code is None:
        raise AccountNotFoundError(
            f"Loan {loan.id} has no principal/escrow ledger accounts."
        )

    key = f"MORTGAGE_OPENING:{loan.id}"
    existing = session.exec(
        select(JournalEntry).where(JournalEntry.idempotency_key == key)
    ).first()
    if existing is not None:
        return existing

    entry = create_journal_entry(
        session,
        entry_date=loan.origination_date or date.today(),
        entry_type="OPENING_BALANCE",
        description=f"Opening balances declared for loan {loan.loan_number}",
        idempotency_key=key,
    )
    if principal_cents:
        add_journal_line(
            session,
            entry.id,
            loan.principal_ledger_code,
            credit_cents=principal_cents,
            property_id=loan.property_id,
        )
    if escrow_cents:
        add_journal_line(
            session,
            entry.id,
            loan.escrow_ledger_code,
            debit_cents=escrow_cents,
            property_id=loan.property_id,
        )

    difference = principal_cents - escrow_cents
    if difference > 0:
        add_journal_line(
            session, entry.id, OWNER_EQUITY_CODE,
            debit_cents=difference, description="Opening mortgage liability assumed",
        )
    elif difference < 0:
        add_journal_line(
            session, entry.id, OWNER_EQUITY_CODE,
            credit_cents=-difference, description="Opening escrow reserve acquired",
        )
    return post_journal_entry(session, entry.id, commit=commit)
