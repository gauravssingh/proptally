"""Jinja2 formatting filters and UI helpers."""

from datetime import date
from typing import Optional


def period_label(financials) -> str:
    """Describes the window a set of financial figures actually covers.

    Used by every view that reports period-sensitive metrics so the label can
    never claim a window the calculation did not use. A one-sided filter is
    labelled as open-ended, and a filtered window that contains no posted
    activity says so instead of falling back to lifetime coverage.
    """
    start = financials.get("period_start")
    end = financials.get("period_end")
    coverage_start = financials.get("coverage_start")
    coverage_end = financials.get("coverage_end")

    if start or end:
        # A filter was requested. Name it, and say when it caught nothing.
        if start and end:
            window = f"{start:%d %b %Y} – {end:%d %b %Y}"
        elif start:
            window = f"From {start:%d %b %Y} onwards"
        else:
            window = f"Through {end:%d %b %Y}"
        if coverage_start and coverage_end:
            return window
        return f"{window} · no posted activity in this period"

    if coverage_start and coverage_end:
        return f"All posted entries · {coverage_start:%d %b %Y} – {coverage_end:%d %b %Y}"
    return "All posted entries · no posted activity"


def format_cents(cents: Optional[int]) -> str:
    """Formats integer cents as standard currency: 260000 -> $2,600.00."""
    if cents is None:
        return "$0.00"
    dollars = cents / 100.0
    return f"${dollars:,.2f}"


def format_cents_sign(cents: Optional[int]) -> str:
    """Formats integer cents with signed indicator: -180148 -> -$1,801.48."""
    if cents is None:
        return "$0.00"
    if cents < 0:
        return f"-${abs(cents) / 100.0:,.2f}"
    return f"+${cents / 100.0:,.2f}"


def format_bps(rate_millipercent: Optional[int]) -> str:
    """Formats legacy rate fields stored in thousandths of a percent."""
    if rate_millipercent is None:
        return "—"
    pct = rate_millipercent / 1000.0
    return f"{pct:.3f}%"


def format_date(d: Optional[date]) -> str:
    """Formats date cleanly: 2026-03-28 -> Mar 28, 2026."""
    if not d:
        return "—"
    return d.strftime("%b %d, %Y")
