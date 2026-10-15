"""Chart of Accounts: fixed standard accounts plus per-entity code allocation.

Two kinds of ledger account exist, and the distinction is what makes the
accounting engine portfolio-agnostic:

1. **Standard accounts** (``STANDARD_ACCOUNTS``) carry fixed, generic codes that
   mean the same thing in every portfolio: rent receivable, owner equity, tenant
   deposits held, the inflow suspense account, rental and fee income, the
   operating-expense lines, and personal drawings. Every portfolio is seeded with
   exactly these, and they are the only codes any module names directly
   (``RENTAL_INCOME_CODE``, ``OPEX_CODES``, and so on).

2. **Entity accounts** are one per property, deposit account, credit card and
   mortgage loan. Their codes are allocated from ``ALLOCATION_RANGES`` and stored
   on the owning row: ``Property.asset_ledger_code``,
   ``FinancialAccount.ledger_account_code``, and
   ``LoanAccount.principal_ledger_code`` / ``clearing_ledger_code`` /
   ``escrow_ledger_code``. Readers (metrics, routes, splitting) derive their code
   sets from those columns, so no module needs to know any particular portfolio's
   account numbers.

Allocation is deterministic, monotonic and idempotent. Codes are handed out in
the documented range order, skipping codes already present in
``ledger_accounts``, so re-running the allocator never renumbers an existing
account. The first allocation in each range reproduces the codes the original
single-portfolio deployment used (1010, 1110, 1510, 2010, 2110/2115, ...), which
is what lets a migrated database keep classifying its existing journal lines
identically.
"""

from typing import Dict, List, Optional, Tuple

from sqlmodel import Session, select

from proptally.core.exceptions import LedgerCodeExhaustedError
from proptally.db.models import (
    FinancialAccount,
    LedgerAccount,
    LoanAccount,
    Property,
)

# --- 1. Fixed standard account codes --------------------------------------
#
# These are the only ledger codes named directly anywhere in the codebase. They
# are generic by construction: per-property and per-account detail lives in the
# allocated codes below and in journal-line attribution, never in a code's name.

RENT_RECEIVABLE_CODE = 1210
TENANT_DEPOSITS_HELD_CODE = 2210
INFLOW_SUSPENSE_CODE = 2999
OWNER_EQUITY_CODE = 3010

RENTAL_INCOME_CODE = 4010
FEE_INCOME_CODE = 4015
#: Rental income code used by the pre-multi-tenant chart of accounts. Retained so
#: journal lines posted before the upgrade keep reporting as rental revenue.
LEGACY_RENTAL_INCOME_CODE = 4020

MORTGAGE_INTEREST_CODE = 5010
PROPERTY_TAX_CODE = 5020
HAZARD_INSURANCE_CODE = 5030
REPAIRS_CODE = 5040
SUPPLIES_CODE = 5050
SNOW_LANDSCAPING_CODE = 5060
BANK_FEES_CODE = 5070
MANAGEMENT_CODE = 5072
COMMISSIONS_CODE = 5075
HOA_CODE = 5078
#: Legal expense. The original chart also used 5080 for mortgage late fees; the
#: two share a code historically, and splitting them would change reported figures,
#: so 5080 keeps its meaning as "other operating expense" in both roles.
LEGAL_AND_LATE_FEE_CODE = 5080
PERSONAL_DRAW_CODE = 5090

#: Operating-expense codes. Property tax, insurance, repairs, supplies,
#: landscaping, bank fees, management, commissions, HOA, and legal/late fees.
OPEX_CODES: Tuple[int, ...] = (
    PROPERTY_TAX_CODE,
    HAZARD_INSURANCE_CODE,
    REPAIRS_CODE,
    SUPPLIES_CODE,
    SNOW_LANDSCAPING_CODE,
    BANK_FEES_CODE,
    MANAGEMENT_CODE,
    COMMISSIONS_CODE,
    HOA_CODE,
    LEGAL_AND_LATE_FEE_CODE,
)

#: Rental revenue codes, including the legacy per-door income code.
RENTAL_REVENUE_CODES: Tuple[int, ...] = (RENTAL_INCOME_CODE, LEGACY_RENTAL_INCOME_CODE)
FEE_REVENUE_CODES: Tuple[int, ...] = (FEE_INCOME_CODE,)

#: Mortgage payment accounts that are lender fees rather than debt service.
MORTGAGE_LATE_FEE_CODE = LEGAL_AND_LATE_FEE_CODE

STANDARD_ACCOUNTS: List[Dict[str, object]] = [
    {"code": RENT_RECEIVABLE_CODE, "name": "Rent Receivable (Accrued)", "account_type": "ASSET", "normal_balance": "DEBIT"},
    {"code": TENANT_DEPOSITS_HELD_CODE, "name": "Tenant Security Deposits Held", "account_type": "LIABILITY", "normal_balance": "CREDIT"},
    {"code": INFLOW_SUSPENSE_CODE, "name": "Unassigned Inflow Suspense", "account_type": "LIABILITY", "normal_balance": "CREDIT"},
    {"code": OWNER_EQUITY_CODE, "name": "Owner's Equity / Capital", "account_type": "EQUITY", "normal_balance": "CREDIT"},
    {"code": RENTAL_INCOME_CODE, "name": "Rental Income", "account_type": "INCOME", "normal_balance": "CREDIT"},
    {"code": FEE_INCOME_CODE, "name": "Fee Income", "account_type": "INCOME", "normal_balance": "CREDIT"},
    {"code": MORTGAGE_INTEREST_CODE, "name": "Mortgage Interest Expense", "account_type": "EXPENSE", "normal_balance": "DEBIT"},
    {"code": PROPERTY_TAX_CODE, "name": "Property Tax Expense", "account_type": "EXPENSE", "normal_balance": "DEBIT"},
    {"code": HAZARD_INSURANCE_CODE, "name": "Hazard Insurance Expense", "account_type": "EXPENSE", "normal_balance": "DEBIT"},
    {"code": REPAIRS_CODE, "name": "Repairs & Maintenance Expense", "account_type": "EXPENSE", "normal_balance": "DEBIT"},
    {"code": SUPPLIES_CODE, "name": "Supplies Expense", "account_type": "EXPENSE", "normal_balance": "DEBIT"},
    {"code": SNOW_LANDSCAPING_CODE, "name": "Snow Removal & Landscaping", "account_type": "EXPENSE", "normal_balance": "DEBIT"},
    {"code": BANK_FEES_CODE, "name": "Bank Service Charges", "account_type": "EXPENSE", "normal_balance": "DEBIT"},
    {"code": MANAGEMENT_CODE, "name": "Property Management Fees (Line 11)", "account_type": "EXPENSE", "normal_balance": "DEBIT"},
    {"code": COMMISSIONS_CODE, "name": "Realtor Commissions & Tenant Placement (Line 8)", "account_type": "EXPENSE", "normal_balance": "DEBIT"},
    {"code": HOA_CODE, "name": "HOA Assessments & Dues (Line 19)", "account_type": "EXPENSE", "normal_balance": "DEBIT"},
    {"code": LEGAL_AND_LATE_FEE_CODE, "name": "Mortgage Late Fees / Legal Expense", "account_type": "EXPENSE", "normal_balance": "DEBIT"},
    {"code": PERSONAL_DRAW_CODE, "name": "Personal / Non-Deductible Drawings", "account_type": "EQUITY", "normal_balance": "DEBIT"},
]


# --- 2. Entity account allocation -----------------------------------------

#: Allocation class identifiers. A class is a ledger meaning, not a data type:
#: deposit and escrow accounts are both assets but are never interchangeable.
DEPOSIT = "DEPOSIT"
ESCROW = "ESCROW"
PROPERTY_ASSET = "PROPERTY_ASSET"
CARD_LIABILITY = "CARD_LIABILITY"
MORTGAGE_PRINCIPAL = "MORTGAGE_PRINCIPAL"
MORTGAGE_CLEARING = "MORTGAGE_CLEARING"

#: Candidate codes per class, as ``(start, stop_inclusive, step)`` triples walked
#: in order. Ranges are disjoint from each other and from the standard codes
#: (the escrow range wraps around 1210, which the allocator skips because it is
#: already occupied). Capacity: 9 deposit accounts, 38 escrow reserves,
#: 49 properties, 9 cards, 19 mortgage loans -- far beyond the 1-10 door
#: portfolio the product targets, and exhaustion raises rather than silently
#: reusing another class's range.
ALLOCATION_RANGES: Dict[str, Tuple[Tuple[int, int, int], ...]] = {
    DEPOSIT: ((1010, 1090, 10),),
    ESCROW: ((1110, 1490, 10),),
    PROPERTY_ASSET: ((1510, 1990, 10),),
    CARD_LIABILITY: ((2010, 2090, 10),),
    MORTGAGE_PRINCIPAL: ((2110, 2290, 10),),
    MORTGAGE_CLEARING: ((2115, 2295, 10),),
}

#: ``(account_type, normal_balance)`` for each allocation class.
_CLASS_SHAPE: Dict[str, Tuple[str, str]] = {
    DEPOSIT: ("ASSET", "DEBIT"),
    ESCROW: ("ASSET", "DEBIT"),
    PROPERTY_ASSET: ("ASSET", "DEBIT"),
    CARD_LIABILITY: ("LIABILITY", "CREDIT"),
    MORTGAGE_PRINCIPAL: ("LIABILITY", "CREDIT"),
    MORTGAGE_CLEARING: ("LIABILITY", "CREDIT"),
}

#: Account types whose ledger account is allocated directly on the account row.
_DEPOSIT_ACCOUNT_TYPES = ("CHECKING", "SAVINGS")
CARD_ACCOUNT_TYPE = "CREDIT_CARD"
MORTGAGE_ACCOUNT_TYPE = "MORTGAGE"


def seed_standard_accounts(session: Session) -> None:
    """Populates the fixed standard accounts idempotently."""
    for spec in STANDARD_ACCOUNTS:
        existing = session.get(LedgerAccount, spec["code"])
        if existing is None:
            session.add(LedgerAccount(**spec))
    session.flush()


def _occupied_codes(session: Session) -> set:
    """Every ledger code already in use, regardless of class."""
    return {row for row in session.exec(select(LedgerAccount.code)).all()}


def _next_code(session: Session, allocation_class: str) -> int:
    """The first unoccupied code in the class's ranges."""
    occupied = _occupied_codes(session)
    for start, stop, step in ALLOCATION_RANGES[allocation_class]:
        for code in range(start, stop + 1, step):
            if code not in occupied:
                return code
    raise LedgerCodeExhaustedError(
        f"No free ledger account code left for {allocation_class}. "
        f"Ranges tried: {ALLOCATION_RANGES[allocation_class]}."
    )


def allocate_ledger_account(session: Session, allocation_class: str, name: str) -> LedgerAccount:
    """Creates one ledger account in the next free code of ``allocation_class``."""
    if allocation_class not in ALLOCATION_RANGES:
        raise ValueError(f"Unknown ledger allocation class: {allocation_class!r}")
    account_type, normal_balance = _CLASS_SHAPE[allocation_class]
    record = LedgerAccount(
        code=_next_code(session, allocation_class),
        name=name,
        account_type=account_type,
        normal_balance=normal_balance,
    )
    session.add(record)
    session.flush()
    return record


def _loan_label(session: Session, loan: LoanAccount) -> str:
    """Human label for a loan's ledger accounts; identity is the loan row itself."""
    prop = session.get(Property, loan.property_id) if loan.property_id else None
    if prop is not None:
        return prop.name or prop.address_line1 or loan.id
    return loan.loan_number or loan.id


def allocate_loan_ledger_accounts(session: Session, loan: LoanAccount) -> None:
    """Assigns the principal, clearing and escrow ledger accounts for one loan.

    All three are allocated together because the mortgage lifecycle always needs
    them as a set: the servicer application debits principal, the bank settlement
    debits clearing, and escrow funding debits the reserve.
    """
    label = _loan_label(session, loan)
    principal = allocate_ledger_account(session, MORTGAGE_PRINCIPAL, f"Mortgage Debt: {label}")
    clearing = allocate_ledger_account(session, MORTGAGE_CLEARING, f"Mortgage Clearing: {label}")
    escrow = allocate_ledger_account(session, ESCROW, f"Servicer Escrow Reserve: {label}")
    loan.principal_ledger_code = principal.code
    loan.clearing_ledger_code = clearing.code
    loan.escrow_ledger_code = escrow.code
    session.add(loan)
    session.flush()


def allocate_account_ledger_account(session: Session, account: FinancialAccount) -> Optional[int]:
    """Assigns the ledger account for a deposit or credit-card account.

    Mortgage accounts have no ledger account of their own: their debt and clearing
    accounts are allocated on the loan (``allocate_loan_ledger_accounts``), so this
    returns ``None`` for them and the caller resolves the code through the loan.
    """
    if account.account_type in _DEPOSIT_ACCOUNT_TYPES:
        allocation_class, prefix = DEPOSIT, "Deposit Account"
    elif account.account_type == CARD_ACCOUNT_TYPE:
        allocation_class, prefix = CARD_LIABILITY, "Credit Card"
    else:
        return None

    record = allocate_ledger_account(session, allocation_class, f"{prefix}: {account.account_name}")
    account.ledger_account_code = record.code
    session.add(account)
    session.flush()
    return record.code


def allocate_property_ledger_account(session: Session, prop: Property) -> None:
    """Assigns the fixed-asset ledger account for one property."""
    record = allocate_ledger_account(
        session, PROPERTY_ASSET, f"Property Asset: {prop.name or prop.address_line1 or prop.id}"
    )
    prop.asset_ledger_code = record.code
    session.add(prop)
    session.flush()


def ensure_entity_ledger_accounts(session: Session, *, commit: bool = True) -> dict:
    """Idempotently gives every entity the ledger accounts it needs.

    Safe to run repeatedly and on a database that predates this module: entities
    that already hold a code are left exactly as they are, so a portfolio's account
    numbering never changes underneath its posted history. New entities are coded in
    ``(created_at, id)`` order, which for a migrated single-portfolio database
    reproduces that portfolio's original codes.

    Returns a count per class for reporting and tests.
    """
    seed_standard_accounts(session)

    allocated = {"properties": 0, "accounts": 0, "loans": 0}

    properties = session.exec(
        select(Property)
        .where(Property.asset_ledger_code.is_(None))
        .order_by(Property.created_at, Property.id)
    ).all()
    for prop in properties:
        allocate_property_ledger_account(session, prop)
        allocated["properties"] += 1

    accounts = session.exec(
        select(FinancialAccount)
        .where(FinancialAccount.ledger_account_code.is_(None))
        .where(FinancialAccount.account_type != MORTGAGE_ACCOUNT_TYPE)
        .order_by(FinancialAccount.created_at, FinancialAccount.id)
    ).all()
    for account in accounts:
        if allocate_account_ledger_account(session, account) is not None:
            allocated["accounts"] += 1

    loans = session.exec(
        select(LoanAccount)
        .where(LoanAccount.principal_ledger_code.is_(None))
        .order_by(LoanAccount.created_at, LoanAccount.id)
    ).all()
    for loan in loans:
        allocate_loan_ledger_accounts(session, loan)
        allocated["loans"] += 1

    if commit:
        session.commit()
    return allocated
