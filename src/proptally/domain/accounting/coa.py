"""Standard Chart of Accounts (COA) definitions and initialization."""

from typing import List, Dict, Any
from sqlmodel import Session, select
from proptally.db.models import LedgerAccount

STANDARD_ACCOUNTS: List[Dict[str, Any]] = [
    # 1000-1999: Assets (Normal balance: DEBIT)
    {"code": 1010, "name": "Bank of America Checking (...9456)", "account_type": "ASSET", "normal_balance": "DEBIT"},
    {"code": 1110, "name": "Servicer Escrow Reserve: Rocket Mortgage", "account_type": "ASSET", "normal_balance": "DEBIT"},
    {"code": 1120, "name": "Servicer Escrow Reserve: PNC Mortgage", "account_type": "ASSET", "normal_balance": "DEBIT"},
    {"code": 1130, "name": "Servicer Escrow Reserve: CrossCountry", "account_type": "ASSET", "normal_balance": "DEBIT"},
    {"code": 1210, "name": "Rent Receivable (Accrued)", "account_type": "ASSET", "normal_balance": "DEBIT"},
    {"code": 1510, "name": "Property Asset: 12307 Larkspur St", "account_type": "ASSET", "normal_balance": "DEBIT"},
    {"code": 1520, "name": "Property Asset: 2718 Foxglove Ct", "account_type": "ASSET", "normal_balance": "DEBIT"},
    {"code": 1530, "name": "Property Asset: 3345 Elderberry Dr", "account_type": "ASSET", "normal_balance": "DEBIT"},

    # 2000-2999: Liabilities (Normal balance: CREDIT)
    {"code": 2010, "name": "Bank of America Visa (...9306)", "account_type": "LIABILITY", "normal_balance": "CREDIT"},
    {"code": 2020, "name": "Apple Card", "account_type": "LIABILITY", "normal_balance": "CREDIT"},
    {"code": 2110, "name": "Mortgage Debt: Rocket (0700000001)", "account_type": "LIABILITY", "normal_balance": "CREDIT"},
    {"code": 2115, "name": "Mortgage Clearing: Rocket", "account_type": "LIABILITY", "normal_balance": "CREDIT"},
    {"code": 2120, "name": "Mortgage Debt: PNC (0700000002)", "account_type": "LIABILITY", "normal_balance": "CREDIT"},
    {"code": 2125, "name": "Mortgage Clearing: PNC", "account_type": "LIABILITY", "normal_balance": "CREDIT"},
    {"code": 2130, "name": "Mortgage Debt: CrossCountry (0700000003)", "account_type": "LIABILITY", "normal_balance": "CREDIT"},
    {"code": 2135, "name": "Mortgage Clearing: CrossCountry", "account_type": "LIABILITY", "normal_balance": "CREDIT"},
    {"code": 2210, "name": "Tenant Security Deposits Held", "account_type": "LIABILITY", "normal_balance": "CREDIT"},
    {"code": 2999, "name": "Unassigned Inflow Suspense", "account_type": "LIABILITY", "normal_balance": "CREDIT"},

    # 3000-3999: Equity (Normal balance: CREDIT)
    {"code": 3010, "name": "Owner's Equity / Capital", "account_type": "EQUITY", "normal_balance": "CREDIT"},

    # 4000-4999: Income (Normal balance: CREDIT)
    {"code": 4010, "name": "Rental Income: 12307 Larkspur St", "account_type": "INCOME", "normal_balance": "CREDIT"},
    {"code": 4015, "name": "Fee Income: 12307 Larkspur St", "account_type": "INCOME", "normal_balance": "CREDIT"},
    {"code": 4020, "name": "Rental Income: 2718 Foxglove Ct", "account_type": "INCOME", "normal_balance": "CREDIT"},

    # 5000-5999: Expenses & Draws (Normal balance: DEBIT)
    {"code": 5010, "name": "Mortgage Interest Expense", "account_type": "EXPENSE", "normal_balance": "DEBIT"},
    {"code": 5020, "name": "Property Tax Expense", "account_type": "EXPENSE", "normal_balance": "DEBIT"},
    {"code": 5030, "name": "Hazard Insurance Expense", "account_type": "EXPENSE", "normal_balance": "DEBIT"},
    {"code": 5040, "name": "Repairs & Maintenance Expense", "account_type": "EXPENSE", "normal_balance": "DEBIT"},
    {"code": 5050, "name": "Supplies Expense", "account_type": "EXPENSE", "normal_balance": "DEBIT"},
    {"code": 5060, "name": "Snow Removal & Landscaping", "account_type": "EXPENSE", "normal_balance": "DEBIT"},
    {"code": 5070, "name": "Bank Service Charges", "account_type": "EXPENSE", "normal_balance": "DEBIT"},
    {"code": 5072, "name": "Property Management Fees (Line 11)", "account_type": "EXPENSE", "normal_balance": "DEBIT"},
    {"code": 5075, "name": "Realtor Commissions & Tenant Placement (Line 8)", "account_type": "EXPENSE", "normal_balance": "DEBIT"},
    {"code": 5078, "name": "HOA Assessments & Dues (Line 19)", "account_type": "EXPENSE", "normal_balance": "DEBIT"},
    {"code": 5080, "name": "Mortgage Late Fees Expense", "account_type": "EXPENSE", "normal_balance": "DEBIT"},
    {"code": 5090, "name": "Personal / Non-Deductible Drawings", "account_type": "EQUITY", "normal_balance": "DEBIT"},
]


def seed_chart_of_accounts(session: Session) -> None:
    """Populates standard ledger accounts idempotently."""
    for acct_data in STANDARD_ACCOUNTS:
        existing = session.exec(select(LedgerAccount).where(LedgerAccount.code == acct_data["code"])).first()
        if not existing:
            session.add(LedgerAccount(**acct_data))
    session.commit()
