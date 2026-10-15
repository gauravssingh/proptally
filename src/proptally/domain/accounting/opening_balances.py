"""Controlled opening balance migration service for PropTally."""

from datetime import date
from typing import Dict, List, Optional
from pydantic import BaseModel
from sqlmodel import Session, select
from proptally.db.models import JournalEntry, LedgerAccount
from proptally.domain.accounting.ledger import create_journal_entry, add_journal_line, post_journal_entry
from proptally.core.exceptions import AccountNotFoundError, DuplicatePostingError


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
            raise ValueError(f"Account {b.account_code} opening balance must be strictly positive cents.")
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
