"""Portfolio controls and the reconciliation report.

Read-only detective controls over the published ledger. Nothing in this module
writes to the database: the report highlights drift, and the publish gate uses
the same checks to stop a portfolio going live with known integrity failures.

Sign conventions: ledger net = debit - credit. Assets and expenses are normally
debit-positive; liabilities, equity, and income are normally credit-negative.
Statement balances are normalised to the same sign so a card balance owed
(positive on the statement) reads as a negative ledger balance.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

from sqlalchemy import func
from sqlmodel import Session, select

from proptally.db.models import (
    Document,
    DocumentExtraction,
    FinancialAccount,
    JournalEntry,
    JournalEntryLine,
    LedgerAccount,
    MortgagePeriod,
    Property,
    Statement,
    Transaction,
    TransactionProcessingState,
    TransactionSplit,
    utc_now,
)
from proptally.domain.money import format_cents, format_cents_signed

BLOCKING = "blocking"
WARNING = "warning"
INFO = "info"

STATEMENT_TYPES = ("CHECKING", "CREDIT_CARD")
OPENING_ENTRY_TYPE = "OPENING_BALANCE"


@dataclass
class ControlCheck:
    key: str
    label: str
    severity: str
    passed: bool
    summary: str
    count: int = 0
    detail: str = ""
    samples: list[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        return "PASS" if self.passed else "FAIL"

    @property
    def blocking(self) -> bool:
        return self.severity == BLOCKING and not self.passed


@dataclass
class AccountReconciliation:
    account_id: str
    name: str
    account_type: str
    ledger_cents: Optional[int]
    statement_cents: Optional[int]
    difference_cents: Optional[int]
    unposted_cents: int
    unposted_count: int
    latest_statement_date: Optional[date]
    statement_count: int

    @property
    def reconciled(self) -> bool:
        return self.difference_cents == 0

    @property
    def status(self) -> str:
        if self.statement_cents is None:
            return "NO_STATEMENT"
        return "RECONCILED" if self.reconciled else "DRIFT"


@dataclass
class PropertyBasis:
    property_id: str
    name: str
    acquisition_date: Optional[date]
    purchase_price_cents: Optional[int]
    basis_cents: Optional[int]
    ledger_asset_cents: Optional[int]

    @property
    def missing_basis(self) -> bool:
        return not self.basis_cents


@dataclass
class ReconciliationReport:
    generated_at: datetime
    checks: list[ControlCheck]
    accounts: list[AccountReconciliation]
    properties: list[PropertyBasis]
    totals: dict[str, int]
    counts: dict[str, int]

    @property
    def failures(self) -> list[ControlCheck]:
        return [c for c in self.checks if not c.passed]

    @property
    def blocking_checks(self) -> list[ControlCheck]:
        return [c for c in self.checks if c.blocking]

    @property
    def warning_checks(self) -> list[ControlCheck]:
        return [c for c in self.checks if c.severity == WARNING and not c.passed]

    @property
    def info_checks(self) -> list[ControlCheck]:
        return [c for c in self.checks if c.severity == INFO and not c.passed]


def recent_posted_entries():
    return select(JournalEntry).where(JournalEntry.status == "POSTED")


def _account_net(session: Session) -> dict[int, int]:
    rows = session.exec(
        select(
            JournalEntryLine.ledger_account_code,
            func.coalesce(
                func.sum(JournalEntryLine.debit_cents - JournalEntryLine.credit_cents), 0
            ),
        )
        .join(JournalEntry, JournalEntry.id == JournalEntryLine.journal_entry_id)
        .where(JournalEntry.status == "POSTED")
        .group_by(JournalEntryLine.ledger_account_code)
    ).all()
    return {int(code): int(total or 0) for code, total in rows}


def _ledger_totals(session: Session) -> tuple[dict[str, int], dict[str, int]]:
    net = _account_net(session)
    types = {
        int(row.code): row.account_type
        for row in session.exec(select(LedgerAccount)).all()
    }
    signed = {"ASSET": 0, "LIABILITY": 0, "EQUITY": 0, "INCOME": 0, "EXPENSE": 0}
    for code, total in net.items():
        account_type = types.get(code)
        if account_type in signed:
            signed[account_type] += total
    display = {
        "assets": signed["ASSET"],
        "liabilities": -signed["LIABILITY"],
        "equity": -signed["EQUITY"],
        "income": -signed["INCOME"],
        "expenses": signed["EXPENSE"],
    }
    return signed, display


def _posted_debits_credits(session: Session) -> tuple[int, int]:
    debit, credit = session.exec(
        select(
            func.coalesce(func.sum(JournalEntryLine.debit_cents), 0),
            func.coalesce(func.sum(JournalEntryLine.credit_cents), 0),
        )
        .join(JournalEntry, JournalEntry.id == JournalEntryLine.journal_entry_id)
        .where(JournalEntry.status == "POSTED")
    ).one()
    return int(debit or 0), int(credit or 0)


def _statement_ledger_cents(statement: Statement) -> Optional[int]:
    if statement.closing_balance_cents is None:
        return None
    if statement.statement_type == "CREDIT_CARD":
        return -statement.closing_balance_cents
    return statement.closing_balance_cents


def _latest_statement(session: Session, account_id: str) -> Optional[Statement]:
    statement = session.exec(
        select(Statement)
        .where(
            Statement.financial_account_id == account_id,
            Statement.statement_type.in_(STATEMENT_TYPES),
        )
        .order_by(
            func.coalesce(Statement.period_end_date, Statement.statement_date).desc()
        )
    ).first()
    return statement


def _unposted(session: Session, account_id: str) -> tuple[int, int]:
    rows = session.exec(
        select(Transaction.amount_cents)
        .join(
            TransactionProcessingState,
            TransactionProcessingState.transaction_id == Transaction.id,
        )
        .where(
            Transaction.financial_account_id == account_id,
            TransactionProcessingState.posting_status != "POSTED",
        )
    ).all()
    amounts = [int(row) for row in rows]
    return len(amounts), sum(amounts)


def _all_unposted(session: Session) -> tuple[int, int]:
    count, total = session.exec(
        select(
            func.count(Transaction.id),
            func.coalesce(func.sum(Transaction.amount_cents), 0),
        )
        .join(
            TransactionProcessingState,
            TransactionProcessingState.transaction_id == Transaction.id,
        )
        .where(TransactionProcessingState.posting_status != "POSTED")
    ).one()
    return int(count or 0), int(total or 0)


def _accounts(session: Session, net: dict[int, int]) -> list[AccountReconciliation]:
    accounts = session.exec(
        select(FinancialAccount).where(FinancialAccount.is_active == True)
    ).all()
    result: list[AccountReconciliation] = []
    for account in accounts:
        if account.account_type == "MORTGAGE" or account.ledger_account_code is None:
            continue
        latest = _latest_statement(session, account.id)
        statement_cents = _statement_ledger_cents(latest) if latest else None
        ledger_cents = net.get(int(account.ledger_account_code), 0)
        difference = None if statement_cents is None else ledger_cents - statement_cents
        unposted_count, unposted_cents = _unposted(session, account.id)
        statement_count = session.exec(
            select(func.count(Statement.id)).where(
                Statement.financial_account_id == account.id,
                Statement.statement_type.in_(STATEMENT_TYPES),
            )
        ).one()
        result.append(
            AccountReconciliation(
                account_id=account.id,
                name=account.account_name,
                account_type=account.account_type,
                ledger_cents=ledger_cents,
                statement_cents=statement_cents,
                difference_cents=difference,
                unposted_cents=unposted_cents,
                unposted_count=unposted_count,
                latest_statement_date=(
                    (latest.period_end_date or latest.statement_date) if latest else None
                ),
                statement_count=int(statement_count or 0),
            )
        )
    result.sort(key=lambda row: row.name)
    return result


def _properties(session: Session, net: dict[int, int]) -> list[PropertyBasis]:
    rows = session.exec(select(Property).where(Property.is_active == True)).all()
    result: list[PropertyBasis] = []
    for prop in rows:
        parts = (prop.land_basis_cents or 0) + (prop.building_basis_cents or 0)
        basis = prop.purchase_price_cents or parts or None
        result.append(
            PropertyBasis(
                property_id=prop.id,
                name=prop.name,
                acquisition_date=prop.acquisition_date,
                purchase_price_cents=prop.purchase_price_cents,
                basis_cents=basis,
                ledger_asset_cents=(
                    net.get(int(prop.asset_ledger_code), 0)
                    if prop.asset_ledger_code is not None
                    else None
                ),
            )
        )
    result.sort(key=lambda row: row.name)
    return result


def _statement_math_failures(session: Session) -> list[str]:
    failures: list[str] = []
    statements = session.exec(
        select(Statement).where(Statement.statement_type.in_(STATEMENT_TYPES))
    ).all()
    net_by_statement = {
        statement_id: int(total or 0)
        for statement_id, total in session.exec(
            select(Transaction.statement_id, func.sum(Transaction.amount_cents))
            .where(Transaction.statement_id.in_([s.id for s in statements]))
            .group_by(Transaction.statement_id)
        ).all()
    } if statements else {}
    for statement in statements:
        net = net_by_statement.get(statement.id, 0)
        opening = statement.opening_balance_cents
        closing = statement.closing_balance_cents
        if opening is None or closing is None:
            continue
        # Stored card amounts are holder-signed: a charge is negative and a
        # payment positive, so the balance owed falls by the net:
        # opening - net == closing. Checking adds the net instead.
        if statement.statement_type == "CHECKING":
            ok = opening + net == closing
        else:
            ok = opening - net == closing
        if not ok:
            failures.append(statement.id)
    return failures


def _allocation_failures(session: Session) -> list[str]:
    rows = session.exec(
        select(Transaction, TransactionProcessingState)
        .join(
            TransactionProcessingState,
            TransactionProcessingState.transaction_id == Transaction.id,
        )
        .where(TransactionProcessingState.allocation_status != "UNALLOCATED")
    ).all()
    transaction_ids = [transaction.id for transaction, _ in rows]
    split_by_transaction = {
        transaction_id: int(total or 0)
        for transaction_id, total in session.exec(
            select(TransactionSplit.transaction_id, func.sum(TransactionSplit.amount_cents))
            .where(TransactionSplit.transaction_id.in_(transaction_ids))
            .group_by(TransactionSplit.transaction_id)
        ).all()
    } if transaction_ids else {}
    failures: list[str] = []
    for transaction, state in rows:
        split_total = split_by_transaction.get(transaction.id, 0)
        if split_total != int(state.allocated_cents):
            failures.append(transaction.id)
            continue
        if int(state.allocated_cents) + int(state.residual_cents) != abs(
            int(transaction.amount_cents)
        ):
            failures.append(transaction.id)
    return failures


def _mixed_opening_dates(session: Session) -> list[str]:
    entries = session.exec(
        select(JournalEntry)
        .where(
            JournalEntry.status == "POSTED",
            JournalEntry.entry_type == OPENING_ENTRY_TYPE,
        )
        .order_by(JournalEntry.entry_date)
    ).all()
    opening_by_code: dict[int, date] = {}
    for entry in entries:
        codes = session.exec(
            select(JournalEntryLine.ledger_account_code).where(
                JournalEntryLine.journal_entry_id == entry.id
            )
        ).all()
        for code in codes:
            opening_by_code.setdefault(int(code), entry.entry_date)

    flagged: list[str] = []
    accounts = session.exec(
        select(FinancialAccount).where(
            FinancialAccount.account_type.in_(STATEMENT_TYPES),
            FinancialAccount.ledger_account_code.is_not(None),
        )
    ).all()
    for account in accounts:
        first = session.exec(
            select(func.min(Statement.period_start_date)).where(
                Statement.financial_account_id == account.id,
                Statement.period_start_date.is_not(None),
                Statement.statement_type.in_(STATEMENT_TYPES),
            )
        ).one()
        opening_date = opening_by_code.get(int(account.ledger_account_code))
        if first and opening_date and first > opening_date:
            flagged.append(f"{account.account_name}: ledger opens {opening_date}, statements from {first}")
    return flagged


def _duplicate_extractions(session: Session) -> list[str]:
    rows = session.exec(
        select(
            DocumentExtraction.document_id,
            DocumentExtraction.parser_name,
            DocumentExtraction.parser_version,
            func.count(DocumentExtraction.id),
        ).group_by(
            DocumentExtraction.document_id,
            DocumentExtraction.parser_name,
            DocumentExtraction.parser_version,
        )
    ).all()
    duplicates = [str(row[0]) for row in rows if int(row[3]) > 1]
    return duplicates


def _unextracted_documents(session: Session) -> list[str]:
    rows = session.exec(
        select(Document.id, Document.original_filename)
        .outerjoin(DocumentExtraction, DocumentExtraction.document_id == Document.id)
        .where(Document.document_type.in_(("CHECKING_STATEMENT", "CREDIT_CARD_STATEMENT", "MORTGAGE_STATEMENT")))
        .where(DocumentExtraction.id.is_(None))
    ).all()
    return [str(row[1]) for row in rows]


def build_reconciliation_report(session: Session) -> ReconciliationReport:
    net = _account_net(session)
    signed, display = _ledger_totals(session)
    debit, credit = _posted_debits_credits(session)
    accounts = _accounts(session, net)
    properties = _properties(session, net)

    checks: list[ControlCheck] = []

    missing_basis = [p for p in properties if p.missing_basis]
    checks.append(
        ControlCheck(
            key="property_cost_basis",
            label="Property cost basis",
            severity=WARNING,
            passed=not missing_basis,
            count=len(missing_basis),
            summary=(
                "Every active property has a purchase price or land/building basis."
                if not missing_basis
                else f"{len(missing_basis)} active properties have no cost basis."
            ),
            detail=(
                "Basis is the largest asset on the balance sheet; without it equity is "
                "understated and depreciation cannot be computed."
            ),
            samples=[p.name for p in missing_basis],
        )
    )

    unbalanced = debit != credit
    checks.append(
        ControlCheck(
            key="ledger_balanced",
            label="Ledger balances",
            severity=BLOCKING,
            passed=not unbalanced,
            count=1 if unbalanced else 0,
            summary=(
                f"Posted debits and credits tie at {format_cents(debit)}."
                if not unbalanced
                else f"Posted debits {format_cents(debit)} do not equal credits {format_cents(credit)}."
            ),
        )
    )

    drift = [a for a in accounts if a.difference_cents not in (None, 0)]
    checks.append(
        ControlCheck(
            key="account_reconciliation",
            label="Account reconciliation",
            severity=WARNING,
            passed=not drift,
            count=len(drift),
            summary=(
                "Every account with a statement matches its latest closing balance."
                if not drift
                else f"{len(drift)} accounts differ from their latest statement."
            ),
            detail="Difference = posted ledger balance minus the latest statement balance.",
            samples=[
                f"{a.name}: {format_cents_signed(a.difference_cents)}" for a in drift
            ],
        )
    )

    unposted_count, unposted_cents = _all_unposted(session)
    checks.append(
        ControlCheck(
            key="unposted_transactions",
            label="Transactions posting",
            severity=WARNING,
            passed=unposted_count == 0,
            count=unposted_count,
            summary=(
                "Every imported transaction has been posted to the ledger."
                if unposted_count == 0
                else f"{unposted_count} transaction{'s' if unposted_count != 1 else ''} ({format_cents_signed(unposted_cents)}) {'are' if unposted_count != 1 else 'is'} unposted."
            ),
            samples=[a.name for a in accounts if a.unposted_count],
        )
    )

    math_failures = _statement_math_failures(session)
    checks.append(
        ControlCheck(
            key="statement_math",
            label="Statement arithmetic",
            severity=BLOCKING,
            passed=not math_failures,
            count=len(math_failures),
            summary=(
                "Opening balance plus transactions equals closing on every statement."
                if not math_failures
                else f"{len(math_failures)} statements do not balance internally."
            ),
            samples=math_failures[:5],
        )
    )

    allocation_failures = _allocation_failures(session)
    checks.append(
        ControlCheck(
            key="allocation_math",
            label="Split allocation",
            severity=BLOCKING,
            passed=not allocation_failures,
            count=len(allocation_failures),
            summary=(
                "Every allocation equals its transaction amount."
                if not allocation_failures
                else f"{len(allocation_failures)} transactions have inconsistent allocations."
            ),
            samples=allocation_failures[:5],
        )
    )

    mixed = _mixed_opening_dates(session)
    checks.append(
        ControlCheck(
            key="opening_balance_consistency",
            label="Opening balance dates",
            severity=WARNING,
            passed=not mixed,
            count=len(mixed),
            summary=(
                "Statement history begins at the ledger opening date."
                if not mixed
                else f"{len(mixed)} accounts have statements after their opening entry."
            ),
            detail="A later statement baseline double-counts or omits activity between the two dates.",
            samples=mixed,
        )
    )

    periods = session.exec(select(func.count(MortgagePeriod.id))).one()
    billing_entries = session.exec(
        select(func.count(JournalEntry.id)).where(
            JournalEntry.status == "POSTED",
            JournalEntry.entry_type == "MORTGAGE_BILLING",
        )
    ).one()
    periods_count = int(periods or 0)
    checks.append(
        ControlCheck(
            key="mortgage_billing",
            label="Mortgage billing",
            severity=WARNING,
            passed=not (periods_count > 0 and int(billing_entries or 0) == 0),
            count=periods_count if int(billing_entries or 0) == 0 else 0,
            summary=(
                "Mortgage statements have corresponding billing entries."
                if periods_count == 0 or int(billing_entries or 0) > 0
                else f"{periods_count} mortgage periods exist with no MORTGAGE_BILLING entry."
            ),
        )
    )

    pending = session.exec(
        select(func.count(Statement.id)).where(Statement.ingestion_status != "COMMITTED")
    ).one()
    checks.append(
        ControlCheck(
            key="statement_review",
            label="Statement approval",
            severity=WARNING,
            passed=not int(pending or 0),
            count=int(pending or 0),
            summary=(
                "All imported statements are approved."
                if not int(pending or 0)
                else f"{int(pending or 0)} statements are awaiting human approval."
            ),
        )
    )

    duplicates = _duplicate_extractions(session)
    checks.append(
        ControlCheck(
            key="duplicate_extractions",
            label="Duplicate extractions",
            severity=INFO,
            passed=not duplicates,
            count=len(duplicates),
            summary=(
                "No document has two extractions from the same parser version."
                if not duplicates
                else f"{len(duplicates)} documents have duplicate extraction rows."
            ),
            samples=duplicates[:5],
        )
    )

    unextracted = _unextracted_documents(session)
    checks.append(
        ControlCheck(
            key="unextracted_documents",
            label="Statement documents",
            severity=INFO,
            passed=not unextracted,
            count=len(unextracted),
            summary=(
                "Every statement document has an extraction."
                if not unextracted
                else f"{len(unextracted)} statement documents have no extraction."
            ),
            samples=unextracted[:5],
        )
    )

    counts = {
        "properties": len(properties),
        "accounts": len(accounts),
        "unposted_transactions": unposted_count,
        "statements": sum(a.statement_count for a in accounts),
    }
    return ReconciliationReport(
        generated_at=utc_now(),
        checks=checks,
        accounts=accounts,
        properties=properties,
        totals=display,
        counts=counts,
    )
