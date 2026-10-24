"""Deterministic, offline parsers for Bank of America Checking, BofA Credit Card, and Apple Card.

Like the mortgage parser, these report only what the document itself says: the
institution that issued it, the last four digits of the account it belongs to,
and the transactions. Which portfolio account that is, and which counter-account
a transfer names, are decided by the ingestion service against the portfolio's
own registered accounts -- these parsers hold no account numbers or ids.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from proptally.domain.money import format_cents


@dataclass
class BankTransaction:
    transaction_date: date
    posting_date: date | None
    amount_cents: int  # Signed integer cents: + for inflow (deposit), - for outflow (debit/charge)
    direction: str  # 'INFLOW' | 'OUTFLOW'
    raw_description: str
    reference_number: str | None = None
    requires_client_confirmation: bool = False
    requires_cpa_review: bool = False

    def to_dict(self) -> dict:
        return {
            "transaction_date": self.transaction_date.isoformat(),
            "posting_date": self.posting_date.isoformat() if self.posting_date else None,
            "amount_cents": self.amount_cents,
            "direction": self.direction,
            "raw_description": self.raw_description,
            "reference_number": self.reference_number,
            "requires_client_confirmation": self.requires_client_confirmation,
            "requires_cpa_review": self.requires_cpa_review,
        }


@dataclass
class BankStatementExtraction:
    parser_name: str
    statement_type: str  # 'CHECKING' | 'CREDIT_CARD'
    # Observed identity: what the document itself says about which account it is.
    institution_name: str | None
    account_last4: str | None
    statement_date: date
    period_start_date: date
    period_end_date: date
    opening_balance_cents: int
    closing_balance_cents: int
    # Resolved identity: filled in by the ingestion service, never by a parser.
    institution_id: str | None = None
    financial_account_id: str | None = None
    transactions: list[BankTransaction] = field(default_factory=list)
    status: str = "EXTRACTED"
    review_reasons: list[str] = field(default_factory=list)
    # Why this statement could not be attached to a registered account.
    resolution_reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "parser_name": self.parser_name,
            "statement_type": self.statement_type,
            "institution_name": self.institution_name,
            "account_last4": self.account_last4,
            "institution_id": self.institution_id,
            "financial_account_id": self.financial_account_id,
            "statement_date": self.statement_date.isoformat(),
            "period_start_date": self.period_start_date.isoformat(),
            "period_end_date": self.period_end_date.isoformat(),
            "opening_balance_cents": self.opening_balance_cents,
            "closing_balance_cents": self.closing_balance_cents,
            "transactions": [t.to_dict() for t in self.transactions],
            "status": self.status,
            "review_reasons": self.review_reasons,
            "resolution_reasons": self.resolution_reasons,
        }


#: Labels a statement prints in front of an account or card number.
_NUMBER_LABEL = re.compile(
    r"(?:account|card)\s*(?:number|no\.?|#)?\s*[:#]?\s*"
    r"((?:[*\u2022xX]{2,}|(?:\d{4}[ \-]){0,3}\d)[\d\s\-*\u2022xX]*\d)",
    re.IGNORECASE,
)

#: A long printed number, grouped in fours: 0029 1385 9456.
_GROUPED_NUMBER = re.compile(r"\b(\d{4}(?:[ \-]\d{4}){2,3})\b")

#: A masked number: at least three mask characters then the visible digits.
_MASKED_NUMBER = re.compile(r"[*\u2022xX]{3,}[ \t]*(\d{2,4})\b")

#: The BofA deposit-account statement template, recognised by its own headings.
_CHECKING_TEMPLATE = re.compile(
    r"adv(?:antage)?\s+plus\s+banking|advantage\s+banking"
    r"|deposits\s+and\s+other\s+additions",
    re.IGNORECASE,
)


def _page_texts(path: Path) -> list[str]:
    import pymupdf

    with pymupdf.open(path) as document:
        return [page.get_text() for page in document]


def _read_text(path: Path) -> str:
    return "\n".join(_page_texts(path))


def _digits(value: str | None) -> str:
    return re.sub(r"\D", "", value or "")


def _observed_account_last4(text: str) -> str | None:
    """The last four digits of the account number this document prints, if any.

    Read off the document only: a masked ``•••• 4321``, a grouped
    ``0029 1385 9456``, or a labelled number all yield the same four digits.
    """
    for pattern in (_NUMBER_LABEL, _GROUPED_NUMBER, _MASKED_NUMBER):
        match = pattern.search(text)
        if match:
            digits = _digits(match.group(1))
            if len(digits) >= 4:
                return digits[-4:]
    return None


# --- 1. Bank of America Checking Parser ---

def parse_bofa_checking(path: Path) -> BankStatementExtraction | None:
    full_text = _read_text(path)

    if "Bank of America" not in full_text or not _CHECKING_TEMPLATE.search(full_text):
        return None

    # Dates
    dates_m = re.search(r"for\s+([A-Za-z]+\s+\d{1,2},?\s+\d{4})\s+to\s+([A-Za-z]+\s+\d{1,2},?\s+\d{4})", full_text)
    if not dates_m:
        return None
    start_date = datetime.strptime(dates_m.group(1).replace(",", ""), "%B %d %Y").date()
    end_date = datetime.strptime(dates_m.group(2).replace(",", ""), "%B %d %Y").date()

    # Balances
    beg_m = re.search(r"Beginning\s+balance\s+on[^\$]+\$([\d,]+\.\d{2})", full_text)
    end_m = re.search(r"Ending\s+balance\s+on[^\$]+\$([\d,]+\.\d{2})", full_text)
    if not beg_m or not end_m:
        return None

    opening_balance_cents = int((Decimal(beg_m.group(1).replace(",", "")) * 100).quantize(Decimal(1)))
    closing_balance_cents = int((Decimal(end_m.group(1).replace(",", "")) * 100).quantize(Decimal(1)))

    transactions: list[BankTransaction] = []
    current_section = None

    import pymupdf

    with pymupdf.open(path) as doc:
        # Activity can start on any page; the section headings decide what is a
        # transaction, so scanning from the first page cannot invent rows.
        for p_no in range(len(doc)):
            lines = doc[p_no].get_text().splitlines()
            i = 0
            while i < len(lines):
                line = lines[i].strip()
                if "Deposits and other additions" in line and "Total" not in line:
                    current_section = "INFLOW"
                    i += 1
                    continue
                elif any(s in line for s in ["Withdrawals and other subtractions", "ATM and debit card subtractions", "Other subtractions", "Checks", "Service fees"]) and "Total" not in line:
                    current_section = "OUTFLOW"
                    i += 1
                    continue
                elif "Total " in line:
                    current_section = None
                    i += 1
                    continue

                dm = re.match(r"^(\d{2}/\d{2}/\d{2})$", line)
                if dm and current_section:
                    tx_date = datetime.strptime(dm.group(1), "%m/%d/%y").date()
                    i += 1
                    desc_lines = []
                    amount_cents = None
                    while i < len(lines):
                        cand = lines[i].strip()
                        am = re.match(r"^-?\$?([\d,]+\.\d{2})$", cand)
                        if am and desc_lines:
                            amt_val = int((Decimal(am.group(1).replace(",", "")) * 100).quantize(Decimal(1)))
                            amount_cents = amt_val if current_section == "INFLOW" else -amt_val
                            i += 1
                            break
                        else:
                            desc_lines.append(cand)
                            i += 1

                    if amount_cents is not None:
                        desc = " ".join(desc_lines)
                        # An ATM cash deposit is the one entry whose source this
                        # parser cannot know: it needs the client to say which door
                        # the rent was for.
                        requires_confirmation = "ATM" in desc and "DEPOSIT" in desc
                        transactions.append(BankTransaction(
                            transaction_date=tx_date,
                            posting_date=tx_date,
                            amount_cents=amount_cents,
                            direction=current_section,
                            raw_description=desc,
                            requires_client_confirmation=requires_confirmation,
                        ))
                    continue
                i += 1

    extraction = BankStatementExtraction(
        parser_name="BofaCheckingParser",
        statement_type="CHECKING",
        institution_name="Bank of America",
        account_last4=_observed_account_last4(full_text),
        statement_date=end_date,
        period_start_date=start_date,
        period_end_date=end_date,
        opening_balance_cents=opening_balance_cents,
        closing_balance_cents=closing_balance_cents,
        transactions=transactions,
    )

    # Balance check: opening + net transactions == closing
    net_cents = sum(t.amount_cents for t in transactions)
    if opening_balance_cents + net_cents != closing_balance_cents:
        extraction.status = "NEEDS_REVIEW"
        extraction.review_reasons.append(
            f"Checking balance discrepancy: opening ({format_cents(opening_balance_cents)}) + net ({format_cents(net_cents)}) != closing ({format_cents(closing_balance_cents)})"
        )
    else:
        extraction.status = "VALIDATED"

    return extraction


# --- 2. Bank of America Credit Card Parser ---

def parse_bofa_credit_card(path: Path) -> BankStatementExtraction | None:
    full_text = _read_text(path)

    if "Visa Signature" not in full_text or "Bank of America" not in full_text:
        return None

    # Statement cycle dates: e.g. "December 9 - January 8, 2026" or "July 9 - August 8, 2026"
    dates_m = re.search(r"([A-Za-z]+\s+\d{1,2})\s*-\s*([A-Za-z]+\s+\d{1,2},?\s+(\d{4}))", full_text)
    if not dates_m:
        return None

    year_str = dates_m.group(3)
    end_raw = dates_m.group(2).replace(",", "")
    end_date = datetime.strptime(end_raw, "%B %d %Y").date()

    # If start date month is later in year than end date month (e.g. Dec - Jan 2026), start year was prior year
    start_raw = f"{dates_m.group(1)} {year_str}"
    start_cand = datetime.strptime(start_raw, "%B %d %Y").date()
    if start_cand > end_date:
        start_date = date(end_date.year - 1, start_cand.month, start_cand.day)
    else:
        start_date = start_cand

    # Balances
    prev_m = re.search(r"Previous\s+Balance\s+\$([\d,]+\.\d{2})", full_text)
    new_m = re.search(r"New\s+Balance\s+Total\s+\$([\d,]+\.\d{2})", full_text)
    if not prev_m or not new_m:
        return None

    opening_balance_cents = int((Decimal(prev_m.group(1).replace(",", "")) * 100).quantize(Decimal(1)))
    closing_balance_cents = int((Decimal(new_m.group(1).replace(",", "")) * 100).quantize(Decimal(1)))

    transactions: list[BankTransaction] = []
    current_section = None

    pages = _page_texts(path)
    for p_no in range(1, len(pages)):
        lines = pages[p_no].splitlines()
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            if "Payments and Other Credits" in line and "TOTAL" not in line:
                current_section = "CREDIT"
                i += 1
                continue
            elif "Purchases and Adjustments" in line and "TOTAL" not in line:
                current_section = "PURCHASE"
                i += 1
                continue
            elif "TOTAL " in line or "Total " in line:
                current_section = None
                i += 1
                continue

            dm = re.match(r"^(\d{2}/\d{2})$", line)
            if dm and current_section:
                trans_m_d = dm.group(1)
                i += 1
                if i < len(lines) and re.match(r"^(\d{2}/\d{2})$", lines[i].strip()):
                    post_m_d = lines[i].strip()
                    i += 1
                else:
                    post_m_d = trans_m_d

                # Deduce transaction year
                tm, td = map(int, trans_m_d.split("/"))
                t_year = end_date.year if tm <= end_date.month else end_date.year - 1
                tx_date = date(t_year, tm, td)
                pm, pd = map(int, post_m_d.split("/"))
                p_year = end_date.year if pm <= end_date.month else end_date.year - 1
                posting_date = date(p_year, pm, pd)

                desc_lines = []
                ref_num = None
                amount_cents = None

                while i < len(lines):
                    cand = lines[i].strip()
                    am = re.match(r"^-?\$?([\d,]+\.\d{2})$", cand)
                    if am and desc_lines:
                        val = int((Decimal(am.group(1).replace(",", "")) * 100).quantize(Decimal(1)))
                        # Signed-cents convention shared with checking: a payment
                        # or credit is an inflow (+), a purchase is an outflow (-).
                        amount_cents = val if current_section == "CREDIT" else -val
                        i += 1
                        break
                    elif re.match(r"^\d{4}$", cand) and len(desc_lines) > 0:
                        ref_num = cand
                        i += 1
                    else:
                        desc_lines.append(cand)
                        i += 1

                if amount_cents is not None:
                    desc = " ".join(desc_lines)
                    direction = "INFLOW" if current_section == "CREDIT" else "OUTFLOW"
                    transactions.append(BankTransaction(
                        transaction_date=tx_date,
                        posting_date=posting_date,
                        amount_cents=amount_cents,
                        direction=direction,
                        raw_description=desc,
                        reference_number=ref_num,
                    ))
                continue
            i += 1

    # Add interest and fees charged if any
    fees_m = re.search(r"Fees\s+Charged\s+\$([\d,]+\.\d{2})", full_text)
    int_m = re.search(r"Interest\s+Charged\s+\$([\d,]+\.\d{2})", full_text)
    if fees_m:
        fee_cents = int((Decimal(fees_m.group(1).replace(",", "")) * 100).quantize(Decimal(1)))
        if fee_cents > 0:
            transactions.append(BankTransaction(
                transaction_date=end_date,
                posting_date=end_date,
                amount_cents=-fee_cents,
                direction="OUTFLOW",
                raw_description="BofA Credit Card Fees Charged",
            ))
    if int_m:
        int_cents = int((Decimal(int_m.group(1).replace(",", "")) * 100).quantize(Decimal(1)))
        if int_cents > 0:
            transactions.append(BankTransaction(
                transaction_date=end_date,
                posting_date=end_date,
                amount_cents=-int_cents,
                direction="OUTFLOW",
                raw_description="BofA Credit Card Interest Charged",
            ))

    # Add cash advances if any
    adv_m = re.search(r"Direct Deposit and Check Cash Advances\s+\$([\d,]+\.\d{2})", full_text)
    if adv_m:
        adv_cents = int((Decimal(adv_m.group(1).replace(",", "")) * 100).quantize(Decimal(1)))
        if adv_cents > 0:
            transactions.append(BankTransaction(
                transaction_date=end_date,
                posting_date=end_date,
                amount_cents=-adv_cents,
                direction="OUTFLOW",
                raw_description="Direct Deposit and Check Cash Advances",
            ))

    extraction = BankStatementExtraction(
        parser_name="BofaCreditCardParser",
        statement_type="CREDIT_CARD",
        institution_name="Bank of America",
        account_last4=_observed_account_last4(full_text),
        statement_date=end_date,
        period_start_date=start_date,
        period_end_date=end_date,
        opening_balance_cents=opening_balance_cents,
        closing_balance_cents=closing_balance_cents,
        transactions=transactions,
    )

    # A card's opening balance is what is owed. Signed cash-flow cents reduce
    # that balance for inflows (payments/credits) and raise it for outflows
    # (purchases), so owed = opening - net.
    net_cents = sum(t.amount_cents for t in transactions)
    if opening_balance_cents - net_cents != closing_balance_cents:
        extraction.status = "NEEDS_REVIEW"
        extraction.review_reasons.append(
            f"Credit card balance discrepancy: opening ({format_cents(opening_balance_cents)}) - net ({format_cents(net_cents)}) != closing ({format_cents(closing_balance_cents)})"
        )
    else:
        extraction.status = "VALIDATED"

    return extraction


# --- 3. Apple Card Parser ---

def _apple_card_last4(text: str) -> str | None:
    """The Apple Card's own last four digits, never the linked bank account's.

    Apple Card statements print ACH payments as "account ending in 9456" and do
    not always print the card number. Using the generic observer would attach
    the checking account's digits to the card.
    """
    for pattern in (
        r"Card\s+Number\s*:?\s*[^\d\n]*(\d{4})",
        r"Apple\s+Card\s*(?:ending\s+in|number)?\s*[^\d\n]*(\d{4})",
    ):
        match = re.search(pattern, text, re.I)
        if match:
            return match.group(1)
    return None


def parse_apple_card(path: Path) -> BankStatementExtraction | None:
    full_text = _read_text(path)

    if "Apple Card" not in full_text or "Goldman Sachs" not in full_text:
        return None

    # Statement month: e.g. "Dec 1 — Dec 31, 2025" or "Apple Card Statement - December 2025"
    dates_m = re.search(r"([A-Za-z]+)\s+(\d{1,2})\s*[—–-]\s*([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})", full_text)
    if not dates_m:
        return None

    start_raw = f"{dates_m.group(1)} {dates_m.group(2)} {dates_m.group(5)}"
    end_raw = f"{dates_m.group(3)} {dates_m.group(4)} {dates_m.group(5)}"
    start_date = datetime.strptime(start_raw, "%b %d %Y").date()
    end_date = datetime.strptime(end_raw, "%b %d %Y").date()

    # The monthly balance excludes Apple Card Monthly Installments; the total
    # balance includes them. Installments are financing, not this period's cash
    # activity, so reconcile against the monthly balance when printed.
    prev_m = re.search(r"Previous\s+Monthly\s+Balance\s+\$([\d,]+\.\d{2})", full_text)
    tot_m = re.search(
        r"Your\s+[A-Za-z]+\s+Balance\s+as\s+of[^$]*\$([\d,]+\.\d{2})", full_text, re.S
    )
    if not prev_m or not tot_m:
        prev_m = re.search(r"Previous\s+Total\s+Balance\s+\$([\d,]+\.\d{2})", full_text)
        tot_m = re.search(r"(?<!Previous\s)Total\s+Balance\s+\$([\d,]+\.\d{2})", full_text)
    if not prev_m or not tot_m:
        return None

    opening_balance_cents = int((Decimal(prev_m.group(1).replace(",", "")) * 100).quantize(Decimal(1)))
    closing_balance_cents = int((Decimal(tot_m.group(1).replace(",", "")) * 100).quantize(Decimal(1)))

    transactions: list[BankTransaction] = []

    def money_cents(values: list[str]) -> list[int]:
        """Every printed dollar amount, in cents, preserving sign and order.

        Apple Card puts two amounts on a purchase line (Daily Cash and the
        purchase), so splitting on whitespace or taking the last ``$`` token can
        produce a string like ``0.21 20.95``. Read the numbers themselves.
        """
        amounts: list[int] = []
        for value in values:
            for match in re.finditer(r"-?\$([\d,]+\.\d{2})", value):
                cents = int((Decimal(match.group(1).replace(",", "")) * 100).quantize(Decimal(1)))
                amounts.append(-cents if match.group(0).startswith("-") else cents)
        return amounts

    pages = _page_texts(path)
    for p_no in range(1, len(pages)):
        lines = pages[p_no].splitlines()
        clean = [
            l.strip() for l in lines
            if not (
                re.match(r"^Page \d+ /\d+$", l.strip())
                or "Apple Card is issued" in l
                or "Apple Card Customer" in l
                or "@" in l
                or "Statement" == l.strip()
                or re.match(r"^[A-Za-z]+ \d+ — [A-Za-z]+ \d+, \d{4}$", l.strip())
            )
        ]

        i = 0
        while i < len(clean):
            if re.match(r"^\d{2}/\d{2}/\d{4}$", clean[i]):
                start = i
                i += 1
                while i < len(clean) and not re.match(r"^\d{2}/\d{2}/\d{4}$", clean[i]) and "Total " not in clean[i]:
                    i += 1
                block = clean[start:i]
                tx_date = datetime.strptime(block[0], "%m/%d/%Y").date()

                try:
                    amounts = money_cents(block[1:])
                    if any("ACH Deposit" in line for line in block[1:]):
                        # Payment: the printed negative amount reduces the balance
                        # owed. A payment line is an inflow in cash-flow terms.
                        payment = next((a for a in amounts if a < 0), None)
                        if payment is not None:
                            transactions.append(BankTransaction(
                                transaction_date=tx_date,
                                posting_date=tx_date,
                                amount_cents=-payment,
                                direction="INFLOW",
                                raw_description=next(
                                    (line for line in block[1:] if "ACH Deposit" in line), "ACH Deposit"
                                ),
                            ))
                    elif any("(return)" in line.lower() for line in block):
                        returns = [a for a in amounts if a < 0]
                        if returns:
                            transactions.append(BankTransaction(
                                transaction_date=tx_date,
                                posting_date=tx_date,
                                amount_cents=-returns[0],
                                direction="INFLOW",
                                raw_description=" ".join(block[1:3]),
                            ))
                        # A return also reverses the Daily Cash earned on the
                        # original purchase. The reversal is printed as a
                        # positive number, but it is a charge back to the card:
                        # the balance owed rises, so it is an outflow.
                        for adjustment in (a for a in amounts if a > 0):
                            transactions.append(BankTransaction(
                                transaction_date=tx_date,
                                posting_date=tx_date,
                                amount_cents=-adjustment,
                                direction="OUTFLOW",
                                raw_description="Daily Cash Adjustment",
                            ))
                    elif amounts:
                        # Purchase: Daily Cash may precede the amount; the last
                        # amount on the block is the purchase itself.
                        purchase = amounts[-1]
                        desc_parts = [b for b in block[1:] if not re.match(r"^\d+%$", b) and "$" not in b]
                        transactions.append(BankTransaction(
                            transaction_date=tx_date,
                            posting_date=tx_date,
                            amount_cents=-abs(purchase),
                            direction="OUTFLOW",
                            raw_description=" ".join(desc_parts),
                        ))
                except (ArithmeticError, ValueError, IndexError):
                    # One malformed block must not discard the whole statement;
                    # the balance check below will surface any resulting gap.
                    pass
            else:
                i += 1

    # Never synthesize a row to make the balance foot. A return line that does
    # not reconcile with the printed balances must surface as NEEDS_REVIEW, not
    # be absorbed into an invented transaction.
    extraction = BankStatementExtraction(
        parser_name="AppleCardParser",
        statement_type="CREDIT_CARD",
        institution_name="Goldman Sachs Bank USA",
        account_last4=_apple_card_last4(full_text),
        statement_date=end_date,
        period_start_date=start_date,
        period_end_date=end_date,
        opening_balance_cents=opening_balance_cents,
        closing_balance_cents=closing_balance_cents,
        transactions=transactions,
    )

    # A card's opening balance is what is owed. Signed cash-flow cents reduce
    # that balance for inflows (payments/returns) and raise it for outflows, so
    # owed = opening - net.
    net_cents = sum(t.amount_cents for t in transactions)
    if opening_balance_cents - net_cents != closing_balance_cents:
        extraction.status = "NEEDS_REVIEW"
        extraction.review_reasons.append(
            f"Apple Card balance discrepancy: opening ({format_cents(opening_balance_cents)}) - net ({format_cents(net_cents)}) != closing ({format_cents(closing_balance_cents)})"
        )
    else:
        extraction.status = "VALIDATED"

    return extraction


def extract_bank_or_card_pdf(path: Path) -> BankStatementExtraction | None:
    """Classify and parse a bank or credit card statement PDF.

    Classification reads the template's own headings only: which account the
    statement belongs to is reported by the parser and resolved afterwards.
    """
    import pymupdf

    with pymupdf.open(path) as doc:
        sample_text = "\n".join(doc[p].get_text() for p in range(min(2, len(doc))))

    if "Bank of America" in sample_text and _CHECKING_TEMPLATE.search(sample_text):
        return parse_bofa_checking(path)
    if "Visa Signature" in sample_text and "Bank of America" in sample_text:
        return parse_bofa_credit_card(path)
    if "Apple Card" in sample_text or "Goldman Sachs Bank USA" in sample_text:
        return parse_apple_card(path)

    return None
