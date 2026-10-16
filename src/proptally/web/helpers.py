"""Jinja2 formatting filters and UI helpers."""

import hashlib
import re
from datetime import date
from pathlib import Path
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


def asset_version() -> str:
    """Cache-busting version for the design-system stylesheets.

    Static files are served with `Last-Modified` and `ETag` but no `Cache-Control`, so
    browsers fall back to heuristic freshness and can keep serving a stylesheet that has
    already changed — the change is then invisible until a hard refresh. Appending this
    to the link's URL makes a changed stylesheet a different URL, so the fix reaches the
    reader on a normal reload. It is derived from the files themselves, so it moves with
    them and needs no manual bump.
    """
    digest = hashlib.sha256()
    for name in ("tokens.css", "base.css", "components.css"):
        path = Path(__file__).resolve().parent / "static" / "css" / name
        try:
            stat = path.stat()
        except OSError:
            continue
        digest.update(f"{name}:{stat.st_mtime_ns}:{stat.st_size}".encode())
    return digest.hexdigest()[:10]


def initials(value: Optional[str]) -> str:
    """Monogram for an avatar: 'Gaurav Singh' -> 'GS', 'gaurav.singh@example.com' -> 'GS'.

    Two letters where there are two name parts to take them from, otherwise one, and never
    an empty circle — the avatar is the only image a self-hosted instance has for a person.
    """
    text = (value or "").strip()
    if not text:
        return "?"
    parts = [part for part in re.split(r"[\s._@-]+", text) if part]
    if len(parts) >= 2 and parts[0][:1] and parts[1][:1]:
        return (parts[0][:1] + parts[1][:1]).upper()
    return parts[0][:1].upper() if parts else "?"


def format_bytes(size: Optional[int]) -> str:
    """Formats a byte count for the administration screen: 288000 -> 281 KB.

    Binary units, one decimal place below a megabyte, because the figures this is used
    for are database and document stores whose exact size is not the point.
    """
    if size is None:
        return "—"
    if size < 1024:
        return f"{size} B"
    for unit, step in (("KB", 1024), ("MB", 1024**2), ("GB", 1024**3), ("TB", 1024**4)):
        if size < step * 1024 or unit == "TB":
            value = size / step
            return f"{value:,.0f} {unit}" if value >= 100 else f"{value:,.1f} {unit}"
    return f"{size} B"
