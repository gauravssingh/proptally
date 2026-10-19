"""IRS Form 1040 Schedule E tax workpaper generator and CPA export engine."""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from datetime import date
from typing import List, Optional

from sqlmodel import Session, select

from proptally.db.models import (
    JournalEntry,
    JournalEntryLine,
    Property,
    Transaction,
    TransactionProcessingState,
    TransactionSplit,
)
from proptally.domain.accounting.coa import (
    BANK_FEES_CODE,
    COMMISSIONS_CODE,
    HOA_CODE,
    MANAGEMENT_CODE,
    SNOW_LANDSCAPING_CODE,
    UTILITIES_CODE,
)
from proptally.domain.tax.depreciation import (
    DepreciationResult,
    calculate_annual_depreciation,
)


@dataclass
class ScheduleEPropertyColumn:
    property_id: str
    property_name: str
    property_address: str
    property_type: str
    is_rental: bool
    # Line Items (stored in integer cents)
    line_3_rents_received: int = 0
    line_5_advertising: int = 0
    line_6_auto: int = 0
    line_7_cleaning_maintenance: int = 0
    line_8_commissions: int = 0
    line_9_insurance: int = 0
    line_10_legal: int = 0
    line_11_management: int = 0
    line_12_mortgage_interest: int = 0
    line_14_repairs: int = 0
    line_15_supplies: int = 0
    line_16_taxes: int = 0
    line_17_utilities: int = 0
    line_19_other: int = 0
    line_18_depreciation: int = 0
    depreciation_detail: Optional[DepreciationResult] = None

    @property
    def line_20_total_expenses(self) -> int:
        return (
            self.line_5_advertising
            + self.line_6_auto
            + self.line_7_cleaning_maintenance
            + self.line_8_commissions
            + self.line_9_insurance
            + self.line_10_legal
            + self.line_11_management
            + self.line_12_mortgage_interest
            + self.line_14_repairs
            + self.line_15_supplies
            + self.line_16_taxes
            + self.line_17_utilities
            + self.line_19_other
            + self.line_18_depreciation
        )

    @property
    def line_21_net_income_or_loss(self) -> int:
        return self.line_3_rents_received - self.line_20_total_expenses


@dataclass
class ScheduleEWorkpaper:
    tax_year: int
    columns: List[ScheduleEPropertyColumn] = field(default_factory=list)
    #: Unposted allocations in the same year, reported as a diagnostic only.
    #: They are never mixed into the official posted-ledger figures.
    unposted_provisional_cents: int = 0
    unposted_split_count: int = 0

    @property
    def total_rents(self) -> int:
        return sum(c.line_3_rents_received for c in self.columns if c.is_rental)

    @property
    def total_expenses(self) -> int:
        return sum(c.line_20_total_expenses for c in self.columns if c.is_rental)

    @property
    def total_net_income(self) -> int:
        return sum(c.line_21_net_income_or_loss for c in self.columns if c.is_rental)


def generate_schedule_e_workpaper(session: Session, tax_year: int) -> ScheduleEWorkpaper:
    """Generates official Schedule E workpaper figures for all properties in a tax year."""
    properties = session.exec(select(Property).order_by(Property.id)).all()
    start_date = date(tax_year, 1, 1)
    end_date = date(tax_year, 12, 31)

    workpaper = ScheduleEWorkpaper(tax_year=tax_year)

    for prop in properties:
        is_rental = (prop.property_type == "RENTAL_INVESTMENT")
        col = ScheduleEPropertyColumn(
            property_id=prop.id,
            property_name=prop.name,
            property_address=f"{prop.address_line1}, {prop.city}, {prop.state} {prop.zip_code}",
            property_type=prop.property_type,
            is_rental=is_rental,
        )

        # 1. Depreciation (only if rental property)
        if is_rental:
            deprec = calculate_annual_depreciation(prop, tax_year)
            col.line_18_depreciation = deprec.depreciation_cents
            col.depreciation_detail = deprec

        # 2. Posted journal lines tagged to this property in tax year
        query = (
            select(JournalEntryLine, JournalEntry)
            .join(JournalEntry, JournalEntryLine.journal_entry_id == JournalEntry.id)
            .where(
                JournalEntryLine.property_id == prop.id,
                JournalEntry.entry_date >= start_date,
                JournalEntry.entry_date <= end_date,
                JournalEntry.status == "POSTED",
            )
        )
        lines = session.exec(query).all()

        for j_line, j_entry in lines:
            code = j_line.ledger_account_code
            debit = j_line.debit_cents
            credit = j_line.credit_cents

            # Revenue (normal balance credit)
            if code in (4010, 4015, 4020):
                col.line_3_rents_received += (credit - debit)

            # Operating expenses (normal balance debit)
            elif code == 5010:  # Mortgage Interest
                col.line_12_mortgage_interest += (debit - credit)
            elif code == 5020:  # Property Taxes
                col.line_16_taxes += (debit - credit)
            elif code == 5030:  # Insurance
                col.line_9_insurance += (debit - credit)
            elif code == 5040:  # Repairs
                col.line_14_repairs += (debit - credit)
            elif code == 5050:  # Supplies
                col.line_15_supplies += (debit - credit)
            elif code == SNOW_LANDSCAPING_CODE:  # Snow / Landscaping / Cleaning
                col.line_7_cleaning_maintenance += (debit - credit)
            elif code == MANAGEMENT_CODE:  # Management Fees (Line 11)
                col.line_11_management += (debit - credit)
            elif code == COMMISSIONS_CODE:  # Realtor Commissions & Placement (Line 8)
                col.line_8_commissions += (debit - credit)
            elif code in (HOA_CODE, BANK_FEES_CODE):  # HOA / bank charges (Line 18 Other)
                col.line_19_other += (debit - credit)
            elif code == UTILITIES_CODE:  # Utilities (Line 17)
                col.line_17_utilities += (debit - credit)
            elif code == 5080:  # Legal / Professional
                col.line_10_legal += (debit - credit)

        # Official Schedule E figures are posted-ledger only. Unposted splits are
        # reported separately below as a reconciliation diagnostic; mixing them
        # into a line silently changes the tax basis.
        workpaper.columns.append(col)

    provisional = session.exec(
        select(TransactionSplit)
        .join(Transaction, TransactionSplit.transaction_id == Transaction.id)
        .join(
            TransactionProcessingState,
            TransactionProcessingState.transaction_id == Transaction.id,
        )
        .where(
            Transaction.transaction_date >= start_date,
            Transaction.transaction_date <= end_date,
            TransactionProcessingState.posting_status == "UNPOSTED",
        )
    ).all()
    workpaper.unposted_split_count = len(provisional)
    workpaper.unposted_provisional_cents = sum(split.amount_cents for split in provisional)
    return workpaper


def export_schedule_e_csv(workpaper: ScheduleEWorkpaper) -> str:
    """Exports Schedule E workpaper to formatted CSV string for CPA sharing."""
    output = io.StringIO()
    writer = csv.writer(output)

    # Header Row
    headers = ["IRS Line Item", "Line Description"]
    for col in workpaper.columns:
        headers.append(f"{col.property_name} ({col.property_type})")
    headers.append("Portfolio Rental Total")
    writer.writerow(headers)

    # Data Rows (in dollars)
    def fmt_val(cents: int) -> str:
        return f"{cents / 100:.2f}"

    rows = [
        ("Line 3", "Rents Received", [c.line_3_rents_received for c in workpaper.columns], workpaper.total_rents),
        ("Line 5", "Advertising", [c.line_5_advertising for c in workpaper.columns], sum(c.line_5_advertising for c in workpaper.columns if c.is_rental)),
        ("Line 6", "Auto and Travel", [c.line_6_auto for c in workpaper.columns], sum(c.line_6_auto for c in workpaper.columns if c.is_rental)),
        ("Line 7", "Cleaning and Maintenance", [c.line_7_cleaning_maintenance for c in workpaper.columns], sum(c.line_7_cleaning_maintenance for c in workpaper.columns if c.is_rental)),
        ("Line 8", "Commissions", [c.line_8_commissions for c in workpaper.columns], sum(c.line_8_commissions for c in workpaper.columns if c.is_rental)),
        ("Line 9", "Insurance", [c.line_9_insurance for c in workpaper.columns], sum(c.line_9_insurance for c in workpaper.columns if c.is_rental)),
        ("Line 10", "Legal and Professional Fees", [c.line_10_legal for c in workpaper.columns], sum(c.line_10_legal for c in workpaper.columns if c.is_rental)),
        ("Line 11", "Management Fees", [c.line_11_management for c in workpaper.columns], sum(c.line_11_management for c in workpaper.columns if c.is_rental)),
        ("Line 12", "Mortgage Interest Paid to Banks", [c.line_12_mortgage_interest for c in workpaper.columns], sum(c.line_12_mortgage_interest for c in workpaper.columns if c.is_rental)),
        ("Line 14", "Repairs", [c.line_14_repairs for c in workpaper.columns], sum(c.line_14_repairs for c in workpaper.columns if c.is_rental)),
        ("Line 15", "Supplies", [c.line_15_supplies for c in workpaper.columns], sum(c.line_15_supplies for c in workpaper.columns if c.is_rental)),
        ("Line 16", "Property Taxes", [c.line_16_taxes for c in workpaper.columns], sum(c.line_16_taxes for c in workpaper.columns if c.is_rental)),
        ("Line 17", "Utilities", [c.line_17_utilities for c in workpaper.columns], sum(c.line_17_utilities for c in workpaper.columns if c.is_rental)),
        ("Line 18", "Depreciation (27.5-Yr MACRS)", [c.line_18_depreciation for c in workpaper.columns], sum(c.line_18_depreciation for c in workpaper.columns if c.is_rental)),
        ("Line 19", "Other (HOA, bank fees)", [c.line_19_other for c in workpaper.columns], sum(c.line_19_other for c in workpaper.columns if c.is_rental)),
        ("Line 20", "Total Expenses", [c.line_20_total_expenses for c in workpaper.columns], workpaper.total_expenses),
        ("Line 21", "Net Rental Income or (Loss)", [c.line_21_net_income_or_loss for c in workpaper.columns], workpaper.total_net_income),
    ]

    for line_code, desc, col_vals, total_val in rows:
        row = [line_code, desc]
        for val in col_vals:
            row.append(fmt_val(val))
        row.append(fmt_val(total_val))
        writer.writerow(row)

    return output.getvalue()
