"""Currency formatting for domain-level messages and reports."""

from __future__ import annotations


def format_cents(cents: int | None) -> str:
    """Formats integer cents as dollars: 260000 -> $2,600.00."""
    if cents is None:
        return "$0.00"
    if cents < 0:
        return f"-${abs(cents) / 100:,.2f}"
    return f"${cents / 100:,.2f}"


def format_cents_signed(cents: int | None) -> str:
    """Formats integer cents with an explicit sign: -180148 -> -$1,801.48."""
    if cents is None:
        return "$0.00"
    if cents < 0:
        return f"-${abs(cents) / 100:,.2f}"
    return f"+${cents / 100:,.2f}"
