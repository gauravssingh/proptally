"""Deterministic first-pass parsers for supported mortgage statements."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

PARSER_VERSION = "0.2.0"


@dataclass
class MortgageExtraction:
    parser_name: str
    statement_type: str
    financial_account_id: str | None
    loan_account_id: str | None
    property_id: str | None
    statement_date: date | None
    payment_due_date: date | None
    principal_cents: int | None
    interest_cents: int | None
    escrow_cents: int | None
    fees_cents: int | None
    total_due_cents: int | None
    outstanding_principal_cents: int | None
    reported_interest_rate_milli_percent: int | None
    escrow_balance_cents: int | None = None
    field_ambiguities: list[str] = field(default_factory=list)
    status: str = "EXTRACTED"
    review_reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {key: value.isoformat() if isinstance(value, date) else value
                for key, value in asdict(self).items()}


LOANS = {
    "0700000001": {
        "vendor": "Rocket Mortgage", "account": "acct_rocket_5035",
        "loan": "loan_rocket_1000000001", "property": "prop_tinsley_huntley",
    },
    "0700000002": {
        "vendor": "PNC Mortgage", "account": "acct_pnc_0708",
        "loan": "loan_pnc_1000000002", "property": "prop_cimmaron_pingree",
    },
    "0700000003": {
        "vendor": "CrossCountry Mortgage", "account": "acct_ccm_9359",
        "loan": "loan_ccm_1000000003", "property": "prop_burkey_algonquin",
    },
}


def _date_after_label(text: str, labels: tuple[str, ...]) -> date | None:
    for label in labels:
        match = re.search(rf"{label}\s*[:\n]?\s*([A-Za-z]+\s+\d{{1,2}},?\s+\d{{4}}|\d{{1,2}}[/-]\d{{1,2}}[/-]\d{{2,4}})", text, re.IGNORECASE)
        if not match:
            continue
        raw = match.group(1).replace(",", "")
        for fmt in ("%B %d %Y", "%b %d %Y", "%m/%d/%Y", "%m/%d/%y", "%m-%d-%Y", "%m-%d-%y"):
            try:
                return datetime.strptime(raw, fmt).date()
            except ValueError:
                pass
    return None


def _money_after_label(text: str, labels: tuple[str, ...]) -> int | None:
    for label in labels:
        match = re.search(rf"{label}[^\S\r\n]*[:]?[^\S\r\n]*(?:\n\s*)?\$?\s*(-?[\d,]+\.\d{{2}})", text, re.IGNORECASE)
        if match:
            try:
                return int((Decimal(match.group(1).replace(",", "")) * 100).quantize(Decimal(1)))
            except InvalidOperation:
                continue
    return None


def _money_matches(text: str, labels: tuple[str, ...]) -> list[int]:
    label_pattern = "(?:" + "|".join(labels) + ")"
    pattern = re.compile(
        rf"^[ \t]*{label_pattern}[ \t]*:?[ \t]*(?:\r?\n[ \t]*)?"
        rf"\$?[ \t]*(-?[\d,]+\.\d{{2}})[ \t]*$",
        re.IGNORECASE | re.MULTILINE,
    )
    amounts = []
    for match in pattern.finditer(text):
        try:
            amounts.append(int((Decimal(match.group(1).replace(",", "")) * 100).quantize(Decimal(1))))
        except InvalidOperation:
            continue
    return amounts


def _extract_explanation_section(text: str) -> str | None:
    match = re.search(
        r"Explanation\s+of\s+Amounts?\s+Due(.*?)(?:Past\s+Payments?\s+Breakdown|Payment\s+history|Transaction\s+Activity|✂|Important\s+Messages|Escrow\s+disbursements|If\s+you\s+are\s+a\s+successor)",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    if match:
        return match.group(1)
    return None


def _rate_after_label(text: str) -> int | None:
    match = re.search(r"Interest\s+Rate[^%]{0,120}?([\d.]+)\s*%", text, re.IGNORECASE)
    if not match:
        match = re.search(r"([\d.]+)\s*%\s*(?:interest\s+rate|of\s+unpaid\s+principal)", text, re.IGNORECASE)
    if not match:
        return None
    try:
        return int((Decimal(match.group(1)) * 1000).quantize(Decimal(1)))
    except InvalidOperation:
        return None


def _identify(text: str, path: Path | None = None) -> tuple[str, dict] | None:
    lines = text.splitlines()
    identifier_label = re.compile(r"(?:loan|account)\s*(?:number|no\.?|#)", re.IGNORECASE)
    for loan_number, details in LOANS.items():
        exact_identifier = re.compile(
            r"(?<!\d)" + r"[\s-]*".join(loan_number) + r"(?!\d)"
        )
        for index, line in enumerate(lines):
            if exact_identifier.search(line) and identifier_label.search("\n".join(lines[max(index - 2, 0):index + 1])):
                return loan_number, details
    folder_vendor = {
        "rocket mortgage": "0700000001",
        "pnc mortgage": "0700000002",
        "cc mortgage": "0700000003",
        "crosscountry mortgage": "0700000003",
    }.get(path.parent.name.lower()) if path else None
    if folder_vendor:
        details = LOANS[folder_vendor]
        if details["vendor"].split()[0].lower() in text.lower() or "crosscountry" in text.lower():
            return folder_vendor, details
    return None


def parse_mortgage_pdf(path: Path, text: str) -> MortgageExtraction | None:
    identified = _identify(text, path)
    if not identified:
        return None
    _, details = identified
    lowered = text.lower()
    if "form 1098" in lowered or "mortgage interest statement" in lowered:
        statement_type = "FORM_1098"
    elif "escrow analysis" in lowered or "escrow account disclosure" in lowered or "annual escrow account" in lowered:
        statement_type = "ESCROW_ANALYSIS"
    else:
        statement_type = "MORTGAGE"

    explanation_section = _extract_explanation_section(text)
    payment_scope = explanation_section if explanation_section is not None else text

    ambiguities: list[str] = []

    def amount(labels: tuple[str, ...], field_name: str, scope: str = text) -> int | None:
        values = _money_matches(scope, labels)
        unique_values = list(dict.fromkeys(values))
        if len(unique_values) > 1:
            ambiguities.append(field_name)
            return None
        return unique_values[0] if unique_values else None

    principal_cents = amount((r"Principal(?:\s+Due)?",), "principal", scope=payment_scope)
    interest_cents = amount((r"Interest(?:\s+Due)?",), "interest", scope=payment_scope)
    escrow_cents = amount((r"Escrow(?:\s+Amount)?(?:\s+\([^\n)]*\))?(?:\s+Due)?",), "escrow", scope=payment_scope)
    fees_cents = amount((
        r"Fees?(?:\s+Due)?",
        r"Late\s+fees\s+and\s+charges",
        r"Total\s+Fees\s+and\s+Charges",
    ), "fees", scope=payment_scope) or 0
    total_due_cents = amount((
        r"Total\s+Amount\s+Due",
        r"Regular\s+Monthly\s+Payment",
        r"Amount\s+Due",
    ), "total due", scope=payment_scope)
    if total_due_cents is None:
        total_due_cents = amount((r"Total\s+Amount\s+Due", r"Amount\s+Due"), "total due", scope=text)

    # Rocket breakdown where escrow amount lists sub-items (Taxes, Insurance, Shortage)
    if escrow_cents is None and "escrow amount" in payment_scope.lower():
        tax_vals = _money_matches(payment_scope, (r"Taxes",))
        ins_vals = _money_matches(payment_scope, (r"Insurance",))
        short_vals = _money_matches(payment_scope, (r"Shortage",))
        if tax_vals and ins_vals:
            escrow_cents = tax_vals[0] + ins_vals[0] + (short_vals[0] if short_vals else 0)

    extraction = MortgageExtraction(
        parser_name=f"{details['vendor'].replace(' ', '')}Parser",
        statement_type=statement_type,
        financial_account_id=details["account"], loan_account_id=details["loan"],
        property_id=details["property"],
        statement_date=_date_after_label(text, (r"Statement\s+Date", r"Date\s+of\s+Statement")),
        payment_due_date=_date_after_label(text, (r"Payment\s+Due\s+Date", r"Due\s+date", r"Payment\s+Due")),
        principal_cents=principal_cents,
        interest_cents=interest_cents,
        escrow_cents=escrow_cents,
        fees_cents=fees_cents,
        total_due_cents=total_due_cents,
        outstanding_principal_cents=amount((
            r"Interest\s+Bearing\s+Principal\s+Balance",
            r"Outstanding\s+Principal(?:\s+Balance)?",
            r"Unpaid\s+Principal\s+Balance",
            r"Principal\s+Balance",
        ), "outstanding principal", scope=text),
        reported_interest_rate_milli_percent=_rate_after_label(text),
        escrow_balance_cents=amount((r"Escrow\s+Balance",), "escrow balance", scope=text),
        field_ambiguities=ambiguities,
    )

    return extraction


def validate_mortgage_extraction(extraction: MortgageExtraction) -> MortgageExtraction:
    """Apply structural and financial checks without approving the result."""
    extraction.status = "EXTRACTED"
    extraction.review_reasons.clear()
    extraction.review_reasons.extend(
        f"Multiple candidate values found for {name}."
        for name in extraction.field_ambiguities
    )
    if extraction.statement_type != "MORTGAGE":
        extraction.review_reasons.append("Auxiliary tax/escrow documents are retained for review, not booked as monthly activity.")
    else:
        required = {
            "statement date": extraction.statement_date,
            "payment due date": extraction.payment_due_date,
            "principal": extraction.principal_cents,
            "interest": extraction.interest_cents,
            "escrow": extraction.escrow_cents,
            "total due": extraction.total_due_cents,
            "outstanding principal": extraction.outstanding_principal_cents,
            "reported interest rate": extraction.reported_interest_rate_milli_percent,
        }
        extraction.review_reasons.extend(f"Could not extract {name}." for name, value in required.items() if value is None)
        if all(required[name] is not None for name in ("principal", "interest", "escrow", "total due")):
            fees = extraction.fees_cents or 0
            if extraction.principal_cents + extraction.interest_cents + extraction.escrow_cents + fees != extraction.total_due_cents:
                extraction.review_reasons.append("Principal + interest + escrow + fees does not equal total due.")
    extraction.status = "NEEDS_REVIEW" if extraction.review_reasons else "VALIDATED"
    return extraction


def extract_pdf(path: Path) -> MortgageExtraction | None:
    import pymupdf

    with pymupdf.open(path) as pdf:
        text = "\n".join(page.get_text() for page in pdf)
    extraction = parse_mortgage_pdf(path, text)
    return validate_mortgage_extraction(extraction) if extraction else None
