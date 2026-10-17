"""Parsing primitives shared by the HTML setup forms.

The portfolio setup screen, the property detail editor and the guided setup
wizard all submit the same field names. Parsing lives here so a rejected
submission is rejected identically wherever it came from, and a caller can
reuse a parser without importing a route module.
"""

from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Optional
from uuid import uuid4


class FormError(ValueError):
    """A rejected submission, reported on the screen instead of as an error page."""


def clean(value) -> str:
    return (value or "").strip()


def cents(value, label: str) -> Optional[int]:
    """Form money ("1,234.56", "$1234.56") as integer cents; blank means "not set"."""
    text = clean(value).replace("$", "").replace(",", "")
    if not text:
        return None
    try:
        amount = Decimal(text)
    except InvalidOperation:
        raise FormError(f"{label}: {value!r} is not a money amount.")
    return int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def day(value, label: str) -> Optional[date]:
    """Form date as ``date``; blank means "not set"."""
    text = clean(value)
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise FormError(f"{label}: {value!r} is not a date.")


def months(value, label: str) -> int:
    text = clean(value)
    if not text:
        return 360
    try:
        parsed = int(text)
    except ValueError:
        raise FormError(f"{label}: {value!r} is not a whole number of months.")
    if parsed <= 0:
        raise FormError(f"{label} must be a positive number of months.")
    return parsed


def rate_bps(value, label: str) -> Optional[int]:
    """A percentage as typed ("3.125") into the stored thousandths of a percent."""
    text = clean(value).replace("%", "")
    if not text:
        return None
    try:
        rate = Decimal(text)
    except InvalidOperation:
        raise FormError(f"{label}: {value!r} is not an interest rate.")
    if rate < 0:
        raise FormError(f"{label} cannot be negative.")
    return int((rate * 1000).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def required(value, label: str) -> str:
    text = clean(value)
    if not text:
        raise FormError(f"{label} is required.")
    return text


def choice(value, allowed: tuple, label: str) -> str:
    text = clean(value)
    if text not in allowed:
        raise FormError(f"{label} must be one of: {', '.join(allowed)}.")
    return text


def optional_choice(value, allowed: tuple, label: str) -> Optional[str]:
    text = clean(value)
    if not text:
        return None
    return choice(text, allowed, label)


def flag(value) -> bool:
    return clean(value).lower() in {"true", "1", "yes", "on"}


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:12]}"
