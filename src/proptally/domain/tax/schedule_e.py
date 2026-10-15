"""IRS Form 1040 Schedule E tax workpaper generator and CPA export engine."""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from datetime import date
from typing import Dict, List, Optional
from sqlmodel import Session, select

from proptally.db.models import (
    JournalEntry,
    JournalEntryLine,
    Property,
    TransactionSplit,
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
    line_3_rents: int = 0
    line_5_advertising: int = 0
    line_6_auto: int = 0
    line_7_cleaning: int = 0
    line_8_commissions: int = 0
    line_9_insurance: int = 0
    line_10_legal: int = 0
    line_11_management: int = 0
    line_12_mortgage_interest: int = 0
    line_14_repairs: int = 0
    line_15_supplies: int = 0
    line_16_taxes: int = 0
    line_17_utilities: int = 0
    line_18_other: int = 0
    line_20_depreciation: int = 0
    depreciation_detail: Optional[DepreciationResult] = None

    @property
    def line_21_total_expenses(self) -> int:
        return (
            self.line_5_advertising
            + self.line_6_auto
            + self.line_7_cleaning
            + self.line_8_commissions
            + self.line_9_insurance
            + self.line_10_legal
            + self.line_11_management
            + self.line_12_mortgage_interest
            + self.line_14_repairs
            + self.line_15_supplies
            + self.line_16_taxes
            + self.line_17_utilities
            + self.line_18_other
            + self.line_20_depreciation
        )

    @property
    def line_22_net_income_or_loss(self) -> int:
        return self.line_3_rents - self.line_21_total_expenses


@dataclass
class ScheduleEWorkpaper:
    tax_year: int
    columns: List[ScheduleEPropertyColumn] = field(default_factory=list)

    @property
    def total_rents(self) -> int:
        return sum(c.line_3_rents for c in self.columns if c.is_rental)

    @property
    def total_expenses(self) -> int:
        return sum(c.line_21_total_expenses for c in self.columns if c.is_rental)

    @property
    def total_net_income(self) -> int:
        return sum(c.line_22_net_income_or_loss for c in self.columns if c.is_rental)


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
            col.line_20_depreciation = deprec.depreciation_cents
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
                col.line_3_rents += (credit - debit)

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
            elif code == 5060:  # Snow / Landscaping / Cleaning
                col.line_7_cleaning += (debit - credit)
            elif code in (5070, 5072):  # Management / Bank fees
                col.line_11_management += (debit - credit)
            elif code == 5075:  # Realtor Commissions & Placement (Line 8)
                col.line_8_commissions += (debit - credit)
            elif code == 5078:  # HOA Assessments (Line 18 Other)
                col.line_18_other += (debit - credit)
            elif code == 5080:  # Legal / Professional
                col.line_10_legal += (debit - credit)

        # 3. Also check TransactionSplits for direct allocations
        splits_query = (
            select(TransactionSplit)
            .where(
                TransactionSplit.property_id == prop.id,
                TransactionSplit.tax_review_status.in_(["AUTO_SUGGESTED", "CLIENT_CONFIRMED", "CPA_APPROVED"]),
            )
        )
        splits = session.exec(splits_query).all()
        # If no posted journal lines were found, splits provide the staged numbers
        if col.line_3_rents == 0:
            rent_splits = sum(s.amount_cents for s in splits if s.category == "RENT_INCOME")
            col.line_3_rents = rent_splits
        if col.line_11_management == 0:
            col.line_11_management = sum(s.amount_cents for s in splits if s.category == "MANAGEMENT")
        if col.line_8_commissions == 0:
            col.line_8_commissions = sum(s.amount_cents for s in splits if s.category == "COMMISSIONS")
        if col.line_18_other == 0:
            col.line_18_other = sum(s.amount_cents for s in splits if s.category == "HOA")

        workpaper.columns.append(col)

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
        ("Line 3a", "Rents Received", [c.line_3_rents for c in workpaper.columns], workpaper.total_rents),
        ("Line 5", "Advertising", [c.line_5_advertising for c in workpaper.columns], sum(c.line_5_advertising for c in workpaper.columns if c.is_rental)),
        ("Line 7", "Cleaning and Maintenance", [c.line_7_cleaning for c in workpaper.columns], sum(c.line_7_cleaning for c in workpaper.columns if c.is_rental)),
        ("Line 9", "Insurance", [c.line_9_insurance for c in workpaper.columns], sum(c.line_9_insurance for c in workpaper.columns if c.is_rental)),
        ("Line 10", "Legal and Professional Fees", [c.line_10_legal for c in workpaper.columns], sum(c.line_10_legal for c in workpaper.columns if c.is_rental)),
        ("Line 11", "Management Fees", [c.line_11_management for c in workpaper.columns], sum(c.line_11_management for c in workpaper.columns if c.is_rental)),
        ("Line 12", "Mortgage Interest Paid to Banks", [c.line_12_mortgage_interest for c in workpaper.columns], sum(c.line_12_mortgage_interest for c in workpaper.columns if c.is_rental)),
        ("Line 14", "Repairs", [c.line_14_repairs for c in workpaper.columns], sum(c.line_14_repairs for c in workpaper.columns if c.is_rental)),
        ("Line 15", "Supplies", [c.line_15_supplies for c in workpaper.columns], sum(c.line_15_supplies for c in workpaper.columns if c.is_rental)),
        ("Line 16", "Property Taxes", [c.line_16_taxes for c in workpaper.columns], sum(c.line_16_taxes for c in workpaper.columns if c.is_rental)),
        ("Line 17", "Utilities", [c.line_17_utilities for c in workpaper.columns], sum(c.line_17_utilities for c in workpaper.columns if c.is_rental)),
        ("Line 20", "Depreciation (27.5-Yr MACRS)", [c.line_20_depreciation for c in workpaper.columns], sum(c.line_20_depreciation for c in workpaper.columns if c.is_rental)),
        ("Line 21", "Total Expenses", [c.line_21_total_expenses for c in workpaper.columns], workpaper.total_expenses),
        ("Line 22", "Net Rental Income or (Loss)", [c.line_22_net_income_or_loss for c in workpaper.columns], workpaper.total_net_income),
    ]

    for line_code, desc, col_vals, total_val in rows:
        row = [line_code, desc]
        for val in col_vals:
            row.append(fmt_val(val))
        row.append(fmt_val(total_val))
        writer.writerow(row)

    return output.getvalue()
