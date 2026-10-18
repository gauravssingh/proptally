"""Field metadata for the review screen and the provisioning gate.

One table describes how each proposed value is labelled, displayed, edited and
validated. The review screen, the edit endpoint and the approval gate all read it,
so the screen cannot offer an edit the gate does not understand, and a required
field cannot be silently missing because a template forgot to ask.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from proptally.db.onboarding_models import (
    ENTITY_ACCOUNT,
    ENTITY_LEASE,
    ENTITY_MORTGAGE,
    ENTITY_PROPERTY,
)

#: type is one of: text, money, date, int, percentage, list, enum.
FIELD_SPECS: dict[str, dict[str, Any]] = {
    "name": {"label": "Display name", "type": "text"},
    "address_line1": {"label": "Street address", "type": "text"},
    "address_line2": {"label": "Unit / address line 2", "type": "text"},
    "city": {"label": "City", "type": "text"},
    "state": {"label": "State", "type": "text"},
    "zip_code": {"label": "ZIP / postal code", "type": "text"},
    "property_type": {
        "label": "Property type", "type": "enum",
        "choices": (("RENTAL_INVESTMENT", "Rental investment (Schedule E)"),
                    ("PRIMARY_RESIDENCE", "Primary residence")),
    },
    "acquisition_date": {"label": "Purchase date", "type": "date"},
    "purchase_price_cents": {"label": "Purchase price", "type": "money"},
    "owner_names": {"label": "Owner names", "type": "list"},
    "seller_names": {"label": "Seller names", "type": "list"},
    "registration_number": {"label": "Registration / document number", "type": "text"},
    "ownership_percentage": {"label": "Ownership share (%)", "type": "percentage"},
    "short_label": {"label": "Short label", "type": "text"},
    "lender": {"label": "Lender / servicer", "type": "text"},
    "loan_number": {"label": "Loan number", "type": "text"},
    "account_number_masked": {"label": "Masked account number", "type": "text"},
    "original_principal_cents": {"label": "Original principal", "type": "money"},
    "outstanding_principal_cents": {"label": "Outstanding principal", "type": "money"},
    "interest_rate_bps": {"label": "Interest rate", "type": "percentage_rate"},
    "term_months": {"label": "Term (months)", "type": "int"},
    "emi_amount_cents": {"label": "Monthly payment", "type": "money"},
    "origination_date": {"label": "Origination date", "type": "date"},
    "maturity_date": {"label": "Maturity date", "type": "date"},
    "statement_date": {"label": "Statement date", "type": "date"},
    "escrow_balance_cents": {"label": "Escrow balance", "type": "money"},
    "rate_type": {
        "label": "Rate type", "type": "enum",
        "choices": (("FIXED", "Fixed"), ("ARM", "Adjustable (ARM)")),
    },
    "property_group_key": {"label": "Linked property", "type": "property"},
    "property_address": {"label": "Property address (from document)", "type": "text_readonly"},
    "institution_name": {"label": "Institution", "type": "text"},
    "account_name": {"label": "Account name", "type": "text"},
    "account_type": {
        "label": "Account type", "type": "enum",
        "choices": (("CHECKING", "Checking"), ("SAVINGS", "Savings"),
                    ("CREDIT_CARD", "Credit card")),
    },
    "currency": {"label": "Currency", "type": "text"},
    "opening_balance_cents": {"label": "Opening balance", "type": "money_readonly"},
    "closing_balance_cents": {"label": "Closing balance", "type": "money_readonly"},
    "statement_period_start": {"label": "Statement period start", "type": "date_readonly"},
    "statement_period_end": {"label": "Statement period end", "type": "date_readonly"},
    "tenant_name": {"label": "Tenant", "type": "text"},
    "landlord": {"label": "Landlord", "type": "text"},
    "monthly_rent_cents": {"label": "Monthly rent", "type": "money"},
    "security_deposit_cents": {"label": "Security deposit", "type": "money"},
    "lease_start": {"label": "Lease start", "type": "date"},
    "lease_end": {"label": "Lease end", "type": "date"},
    "payment_frequency": {
        "label": "Payment frequency", "type": "enum",
        "choices": (("MONTHLY", "Monthly"), ("QUARTERLY", "Quarterly"),
                    ("ANNUAL", "Annual"), ("WEEKLY", "Weekly")),
    },
}

#: The order a reviewer expects: identity, then the facts that decide the record,
#: then supporting detail. JSON payloads are key-sorted, so the screen imposes this.
FIELD_ORDER = {
    ENTITY_PROPERTY: (
        "address_line1", "address_line2", "city", "state", "zip_code", "name",
        "property_type", "acquisition_date", "purchase_price_cents", "owner_names",
        "seller_names", "ownership_percentage", "registration_number", "short_label",
    ),
    ENTITY_MORTGAGE: (
        "lender", "loan_number", "account_number_masked", "property_group_key",
        "property_address", "original_principal_cents", "outstanding_principal_cents",
        "interest_rate_bps", "rate_type", "term_months", "emi_amount_cents",
        "origination_date", "maturity_date", "statement_date", "escrow_balance_cents",
    ),
    ENTITY_ACCOUNT: (
        "institution_name", "account_name", "account_type", "account_number_masked",
        "currency", "statement_period_start", "statement_period_end",
        "opening_balance_cents", "closing_balance_cents",
    ),
    ENTITY_LEASE: (
        "property_group_key", "property_address", "tenant_name", "landlord",
        "monthly_rent_cents", "security_deposit_cents", "lease_start", "lease_end",
        "payment_frequency",
    ),
}

ENTITY_LABELS = {
    ENTITY_PROPERTY: "Property",
    ENTITY_MORTGAGE: "Mortgage",
    ENTITY_ACCOUNT: "Financial account",
    ENTITY_LEASE: "Rental agreement",
}

#: The fields that must hold a value before the record can be accepted. They are
#: the minimum the domain model needs to create a meaningful row, not every field.
REQUIRED_FIELDS = {
    ENTITY_PROPERTY: ("address_line1", "city"),
    ENTITY_ACCOUNT: ("institution_name", "account_type", "account_number_masked"),
    ENTITY_MORTGAGE: ("lender", "loan_number", "property_group_key"),
    ENTITY_LEASE: ("tenant_name", "monthly_rent_cents", "lease_start", "property_group_key"),
}

#: Fields that are shown but are not written into the domain record; they are
#: supporting history or an association resolved at import time.
DISPLAY_ONLY = {
    "opening_balance_cents", "closing_balance_cents", "statement_period_start",
    "statement_period_end", "property_address", "property_city", "property_state",
    "property_zip", "association_status",
}

#: Fields the customer may edit on the review screen.
READONLY_TYPES = {"text_readonly", "money_readonly", "date_readonly", "property"}


def field_spec(name: str) -> dict[str, Any]:
    return FIELD_SPECS.get(name, {"label": name.replace("_", " ").title(), "type": "text"})


def display_value(name: str, value: Any) -> str:
    if value in (None, ""):
        return "—"
    spec = field_spec(name)
    kind = spec["type"]
    if kind in {"money", "money_readonly"}:
        cents = int(value)
        sign = "-" if cents < 0 else ""
        return f"{sign}${abs(cents) / 100:,.2f}"
    if kind == "percentage_rate":
        return f"{int(value) / 1000:.3f}%"
    if kind == "percentage":
        return f"{int(value)}%"
    if kind in {"date", "date_readonly"}:
        return str(value)[:10]
    if kind == "list":
        return ", ".join(str(item) for item in value) if isinstance(value, list) else str(value)
    if kind == "enum":
        choices = dict(spec.get("choices", ()))
        return choices.get(str(value), str(value).replace("_", " ").title())
    return str(value)


def parse_value(name: str, raw: str) -> Any:
    """Parse an edited form value, raising ``ValueError`` with a usable message."""
    spec = field_spec(name)
    kind = spec["type"]
    text = (raw or "").strip()
    if not text:
        return None
    if kind in {"money", "money_readonly"}:
        cleaned = text.replace("$", "").replace(",", "").replace("₹", "")
        try:
            return int((Decimal(cleaned) * 100).quantize(Decimal(1)))
        except InvalidOperation as exc:
            raise ValueError("Enter a money amount, for example 425000.00.") from exc
    if kind == "percentage_rate":
        cleaned = text.replace("%", "").replace(",", "")
        try:
            return int((Decimal(cleaned) * 1000).quantize(Decimal(1)))
        except InvalidOperation as exc:
            raise ValueError("Enter an interest rate, for example 3.125.") from exc
    if kind == "percentage":
        cleaned = text.replace("%", "")
        try:
            value = int(Decimal(cleaned))
        except InvalidOperation as exc:
            raise ValueError("Enter a whole percentage.") from exc
        if not 0 <= value <= 100:
            raise ValueError("Ownership share must be between 0 and 100.")
        return value
    if kind in {"date", "date_readonly"}:
        try:
            return date.fromisoformat(text).isoformat()
        except ValueError as exc:
            raise ValueError("Enter a date in YYYY-MM-DD form.") from exc
    if kind == "int":
        try:
            return int(text)
        except ValueError as exc:
            raise ValueError("Enter a whole number.") from exc
    if kind == "list":
        return [part.strip() for part in text.split(",") if part.strip()]
    if kind == "enum":
        allowed = {value for value, _label in spec.get("choices", ())}
        if text not in allowed:
            raise ValueError(f"Choose one of: {', '.join(sorted(allowed))}.")
        return text
    return text


def ordered_fields(entity_type: str, names) -> list[str]:
    """The entity's fields in review order, followed by any provider-added keys."""
    order = FIELD_ORDER.get(entity_type, ())
    known = [name for name in order if name in names]
    extra = sorted(name for name in names if name not in order)
    return known + extra


def editable(name: str) -> bool:
    return field_spec(name)["type"] not in READONLY_TYPES


def missing_required(entity_type: str, payload: dict) -> list[str]:
    return [
        FIELD_SPECS.get(name, {"label": name})["label"]
        for name in REQUIRED_FIELDS.get(entity_type, ())
        if payload.get(name) in (None, "", [])
    ]


