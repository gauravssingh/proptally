"""Checked-in synthetic bank statement PDFs for parser tests.

These PDFs are generated locally with PyMuPDF and contain only the text the
deterministic parsers read. They replace the machine-specific sample folder that
made the parser tests silently no-op on a clean checkout: every parser assertion
now runs everywhere.
"""

from __future__ import annotations

from pathlib import Path

import pymupdf


def _write_pdf(path: Path, pages: list[list[str]]) -> Path:
    document = pymupdf.open()
    for lines in pages:
        page = document.new_page()
        y = 60.0
        for line in lines:
            page.insert_text((50, y), line, fontsize=9)
            y += 14.0
    document.save(path)
    document.close()
    return path


def bofa_checking_pdf(
    path: Path,
    *,
    period_start: str = "December 16, 2025",
    period_end: str = "January 15, 2026",
) -> Path:
    """BofA Advantage Plus checking: two deposits and one payment, balanced."""
    return _write_pdf(
        path,
        [
            [
                "Bank of America",
                "Advantage Plus Banking",
                "Account number: 0029 1385 9456",
                f"for {period_start} to {period_end}",
                f"Beginning balance on {period_start} $3,926.95",
                f"Ending balance on {period_end} $485.86",
            ],
            [
                "Deposits and other additions",
                "01/05/26",
                "ATM DEPOSIT CARPENTERSVILLE",
                "$850.00",
                "01/06/26",
                "ATM DEPOSIT CARPENTERSVILLE",
                "$500.00",
                "Withdrawals and other subtractions",
                "01/10/26",
                "ONLINE PAYMENT TO LANDLORD",
                "-$4,791.09",
            ],
        ],
    )


def bofa_credit_card_pdf(path: Path) -> Path:
    """BofA Visa card: one payment and two purchases that foot exactly."""
    return _write_pdf(
        path,
        [
            [
                "Bank of America",
                "Visa Signature",
                "Account number: 4400 0000 9306",
                "December 9 - January 8, 2026",
                "Previous Balance $1,385.31",
                "New Balance Total $2,740.12",
            ],
            [
                "Payments and Other Credits",
                "01/05",
                "01/05",
                "Online payment from CHK 9456",
                "-$1,385.31",
                "Purchases and Adjustments",
                "01/06",
                "01/07",
                "GROCERY STORE",
                "$2,000.00",
                "01/08",
                "01/08",
                "HARDWARE STORE",
                "$740.12",
            ],
        ],
    )


def apple_card_pdf(path: Path) -> Path:
    """Apple Card: one ACH payment and one purchase that foot exactly."""
    return _write_pdf(
        path,
        [
            [
                "Apple Card",
                "Goldman Sachs Bank USA",
                "Card Number: ....4821",
                "Dec 1 - Dec 31, 2025",
                "Previous Total Balance $2,663.62",
                "Total Balance $4,901.85",
            ],
            [
                "12/05/2025",
                "ACH Deposit Payment",
                "-$1,000.00",
                "12/10/2025",
                "NEIGHBORHOOD MARKET",
                "$3,238.23",
            ],
        ],
    )


def apple_card_installment_pdf(path: Path) -> Path:
    """Current Apple Card layout: monthly balance, Daily Cash reversal, installment."""
    return _write_pdf(
        path,
        [
            [
                "Apple Card",
                "Goldman Sachs Bank USA",
                "Sep 1 - Sep 30, 2026",
                "Your September Balance",
                "as of Sep 30, 2026",
                "$1,130.20",
                "Previous Monthly Balance",
                "$1,000.00",
                "Previous Total Balance",
                "$1,000.00",
                "Total Balance",
                "$1,500.00",
                "Apple Card Monthly Installments",
                "$50.00",
            ],
            [
                "Payments",
                "09/01/2026",
                "ACH Deposit Internet transfer from account ending in 9456",
                "-$100.00",
                "Transactions",
                "09/02/2026",
                "NEIGHBORHOOD MARKET",
                "1%",
                "$0.50",
                "$200.00",
                "09/03/2026",
                "ONLINE STORE (return)",
                "-$20.00",
                "Daily Cash Adjustment",
                "-1%",
                "$0.20",
            ],
            [
                "Apple Card Monthly Installments",
                "01/15/2026",
                "Apple Online Store Cupertino CA",
                "$300.00",
                "TRANSACTION #abc",
                "This month's installment: $50.00",
                "Final installment: Jan 31, 2027",
            ],
        ],
    )


def apple_card_return_discrepancy_pdf(path: Path) -> Path:
    """Apple Card with a return whose lines do not foot to the printed total.

    The return is real and must be retained; the discrepancy must surface as
    NEEDS_REVIEW without any synthesized adjustment row.
    """
    return _write_pdf(
        path,
        [
            [
                "Apple Card",
                "Goldman Sachs Bank USA",
                "Card Number: ....4821",
                "Dec 1 - Dec 31, 2025",
                "Previous Total Balance $1,000.00",
                "Total Balance $1,200.00",
            ],
            [
                "12/05/2025",
                "ONLINE STORE",
                "$300.00",
                "12/15/2025",
                "ONLINE STORE (return)",
                "-$50.00",
            ],
        ],
    )
