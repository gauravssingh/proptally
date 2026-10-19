"""Deterministic first-pass parsers for supported mortgage statements.

A parser reports only what the document itself says: the loan number printed on
it, the servicer name printed before that number, the last four digits of the
account, the payment amounts and the statement type. It never names a portfolio
entity, so the same statement is parseable for every portfolio. Matching the
observed identity against the portfolio's own registered accounts is a separate
step (``proptally.ingestion.service.resolve_extraction_data``), which fills the
``financial_account_id`` / ``loan_account_id`` / ``property_id`` fields.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

PARSER_VERSION = "0.3.0"


@dataclass
class MortgageExtraction:
    parser_name: str
    statement_type: str
    # Observed identity: what the document itself says about which account it is.
    servicer_name: str | None
    loan_number: str | None
    account_last4: str | None
    statement_date: date | None
    payment_due_date: date | None
    principal_cents: int | None
    interest_cents: int | None
    escrow_cents: int | None
    fees_cents: int | None
    total_due_cents: int | None
    outstanding_principal_cents: int | None
    reported_interest_rate_milli_percent: int | None
    # Resolved identity: filled in by the ingestion service from this portfolio's
    # own registered accounts, never by a parser.
    financial_account_id: str | None = None
    loan_account_id: str | None = None
    property_id: str | None = None
    escrow_balance_cents: int | None = None
    field_ambiguities: list[str] = field(default_factory=list)
    status: str = "EXTRACTED"
    review_reasons: list[str] = field(default_factory=list)
    # Why this statement could not be attached to a registered account. Kept
    # apart from the structural reasons above so re-validating rewrites only the
    # latter.
    resolution_reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {key: value.isoformat() if isinstance(value, date) else value
                for key, value in asdict(self).items()}


#: The label a statement prints in front of its loan or account number.
_IDENTIFIER_LABEL = re.compile(r"(?:loan|account)\s*(?:number|no\.?|#)\s*[:#]?\s*", re.IGNORECASE)

#: A printed number: at least four digits, grouped with spaces or dashes.
_PRINTED_NUMBER = re.compile(r"(\d[\d\s\-]{2,}\d)")

#: An explicit servicer label, when the statement carries one.
_SERVICER_LABEL = re.compile(
    r"(?:loan\s+servicer|servicer|mortgagee|lender)\s*[:#]\s*([^\n]{2,60})", re.IGNORECASE
)

#: Points in the document that only a servicer's statement carries.
_ISSUE_DATE_LABELS = ("statement date", "date of statement", "payment due date", "payment due")

#: The payment vocabulary a monthly mortgage statement is built from.
_PAYMENT_LABELS = ("principal", "escrow", "interest rate", "amount due", "monthly payment")

#: Form 1098 (mortgage interest statement) names itself, as does an annual
#: escrow account disclosure. Both are recognised servicer formats that are not
#: monthly billing statements.
_INTEREST_STATEMENT = re.compile(r"form\s*1098|mortgage\s+interest\s+statement", re.IGNORECASE)
_ESCROW_ANALYSIS = re.compile(r"escrow\s+analysis|escrow\s+account\s+disclosure|annual\s+escrow\s+account", re.IGNORECASE)


def _digits(value: str | None) -> str:
    """Digits only; statements group printed numbers with spaces and dashes."""
    return re.sub(r"\D", "", value or "")


def _observed_identity(text: str) -> tuple[str | None, str | None]:
    """The loan number this document prints, and the name printed before the label.

    Both are read off the document. Neither is checked against any list of known
    portfolios: an unknown loan number is still a recognised statement.
    """
    for match in _IDENTIFIER_LABEL.finditer(text):
        tail = text[match.end():match.end() + 40].lstrip(" :#")
        number = _PRINTED_NUMBER.match(tail)
        if number is None:
            continue
        digits = _digits(number.group(1))
        if len(digits) < 4:
            continue
        line_start = text.rfind("\n", 0, match.start()) + 1
        printed_before_label = text[line_start:match.start()].strip(" \t,;:-")
        return digits, printed_before_label or None
    return None, None


def _servicer_name(text: str, fallback: str | None) -> str | None:
    """The institution that issued the statement, as the document names it."""
    match = _SERVICER_LABEL.search(text)
    if match:
        name = " ".join(match.group(1).split()).strip(" ,.;:-")
        if name:
            return name[:60]
    return fallback


def _statement_type(text: str) -> str:
    """Which kind of servicer document this is, from its own headings."""
    if _INTEREST_STATEMENT.search(text):
        return "FORM_1098"
    if _ESCROW_ANALYSIS.search(text):
        return "ESCROW_ANALYSIS"
    return "MORTGAGE"


def _looks_like_mortgage_statement(text: str, statement_type: str) -> bool:
    """Whether the document is shaped like a servicer statement at all.

    Requires an issue or due date plus the payment vocabulary, so a bare account
    number quoted in some other document is not mistaken for a statement. The
    auxiliary formats are recognised by their own headings.
    """
    lowered = text.lower()
    if statement_type != "MORTGAGE":
        return True
    if not any(label in lowered for label in _ISSUE_DATE_LABELS):
        return False
    return sum(1 for label in _PAYMENT_LABELS if label in lowered) >= 2


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


def parse_mortgage_pdf(path: Path | None, text: str) -> MortgageExtraction | None:
    """Parses a recognised mortgage statement, reporting only what it observes.

    ``path`` is accepted because every caller already holds it; identification
    reads the text only. The entity ids are left empty on purpose: the ingestion
    service resolves them against the portfolio that is asking.
    """
    loan_number, printed_before_label = _observed_identity(text)
    if loan_number is None:
        return None
    statement_type = _statement_type(text)
    if not _looks_like_mortgage_statement(text, statement_type):
        return None
    servicer_name = _servicer_name(text, printed_before_label)

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
        parser_name="MortgageStatementParser",
        statement_type=statement_type,
        servicer_name=servicer_name,
        loan_number=loan_number,
        account_last4=loan_number[-4:],
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
    extraction.status = "NEEDS_REVIEW" if (extraction.review_reasons or extraction.resolution_reasons) else "VALIDATED"
    return extraction


def extract_pdf(path: Path) -> MortgageExtraction | None:
    import pymupdf

    with pymupdf.open(path) as pdf:
        text = "\n".join(page.get_text() for page in pdf)
    extraction = parse_mortgage_pdf(path, text)
    return validate_mortgage_extraction(extraction) if extraction else None
