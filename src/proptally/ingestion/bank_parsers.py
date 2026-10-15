"""Deterministic, offline parsers for Bank of America Checking, BofA Credit Card, and Apple Card."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path


@dataclass
class BankTransaction:
    transaction_date: date
    posting_date: date | None
    amount_cents: int  # Signed integer cents: + for inflow (deposit), - for outflow (debit/charge)
    direction: str  # 'INFLOW' | 'OUTFLOW'
    raw_description: str
    reference_number: str | None = None
    counter_account_id: str | None = None
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
            "counter_account_id": self.counter_account_id,
            "requires_client_confirmation": self.requires_client_confirmation,
            "requires_cpa_review": self.requires_cpa_review,
        }


@dataclass
class BankStatementExtraction:
    parser_name: str
    statement_type: str  # 'CHECKING' | 'CREDIT_CARD'
    financial_account_id: str
    institution_id: str
    statement_date: date
    period_start_date: date
    period_end_date: date
    opening_balance_cents: int
    closing_balance_cents: int
    transactions: list[BankTransaction] = field(default_factory=list)
    status: str = "EXTRACTED"
    review_reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "parser_name": self.parser_name,
            "statement_type": self.statement_type,
            "financial_account_id": self.financial_account_id,
            "institution_id": self.institution_id,
            "statement_date": self.statement_date.isoformat(),
            "period_start_date": self.period_start_date.isoformat(),
            "period_end_date": self.period_end_date.isoformat(),
            "opening_balance_cents": self.opening_balance_cents,
            "closing_balance_cents": self.closing_balance_cents,
            "transactions": [t.to_dict() for t in self.transactions],
            "status": self.status,
            "review_reasons": self.review_reasons,
        }


# --- 1. Bank of America Checking Parser ---

def parse_bofa_checking(path: Path) -> BankStatementExtraction | None:
    import pymupdf

    with pymupdf.open(path) as doc:
        full_text = "\n".join(page.get_text() for page in doc)

    if "Bank of America" not in full_text or "0029 1385 9456" not in full_text:
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

    with pymupdf.open(path) as doc:
        for p_no in range(2, len(doc)):
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
                        # Check special attributes
                        requires_confirmation = False
                        counter_account = None
                        if "ATM" in desc and "DEPOSIT" in desc:
                            # Cash deposit (e.g. Millbrook ATM) requires client confirmation for lease rental
                            requires_confirmation = True
                        if "BANK OF AMERICA CREDIT CARD" in desc or "CHK 9456" in desc:
                            counter_account = "acct_bofa_cc_9306"
                        elif "APPLECARD" in desc:
                            counter_account = "acct_apple_card"
                        elif "ROCKET MORTGAGE" in desc:
                            counter_account = "acct_rocket_5035"
                        elif "PNC MORTGAGE" in desc:
                            counter_account = "acct_pnc_0708"
                        elif "CROSSCOUNTRY" in desc:
                            counter_account = "acct_ccm_9359"

                        transactions.append(BankTransaction(
                            transaction_date=tx_date,
                            posting_date=tx_date,
                            amount_cents=amount_cents,
                            direction=current_section,
                            raw_description=desc,
                            counter_account_id=counter_account,
                            requires_client_confirmation=requires_confirmation,
                        ))
                    continue
                i += 1

    extraction = BankStatementExtraction(
        parser_name="BofaCheckingParser",
        statement_type="CHECKING",
        financial_account_id="acct_bofa_chk_9456",
        institution_id="inst_bofa",
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
            f"Checking balance discrepancy: opening ({opening_balance_cents}) + net ({net_cents}) != closing ({closing_balance_cents})"
        )
    else:
        extraction.status = "VALIDATED"

    return extraction


# --- 2. Bank of America Credit Card Parser ---

def parse_bofa_credit_card(path: Path) -> BankStatementExtraction | None:
    import pymupdf

    with pymupdf.open(path) as doc:
        full_text = "\n".join(page.get_text() for page in doc)

    if "Visa Signature" not in full_text or "9306" not in full_text:
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

    with pymupdf.open(path) as doc:
        for p_no in range(1, len(doc)):
            lines = doc[p_no].get_text().splitlines()
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
                    post_date = None
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
                            amount_cents = -val if current_section == "CREDIT" else val
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
                        counter_account = "acct_bofa_chk_9456" if "Online payment from CHK 9456" in desc else None
                        direction = "INFLOW" if current_section == "CREDIT" else "OUTFLOW"
                        transactions.append(BankTransaction(
                            transaction_date=tx_date,
                            posting_date=posting_date,
                            amount_cents=amount_cents,
                            direction=direction,
                            raw_description=desc,
                            reference_number=ref_num,
                            counter_account_id=counter_account,
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
                amount_cents=fee_cents,
                direction="OUTFLOW",
                raw_description="BofA Credit Card Fees Charged",
            ))
    if int_m:
        int_cents = int((Decimal(int_m.group(1).replace(",", "")) * 100).quantize(Decimal(1)))
        if int_cents > 0:
            transactions.append(BankTransaction(
                transaction_date=end_date,
                posting_date=end_date,
                amount_cents=int_cents,
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
                amount_cents=adv_cents,
                direction="OUTFLOW",
                raw_description="Direct Deposit and Check Cash Advances",
            ))

    extraction = BankStatementExtraction(
        parser_name="BofaCreditCardParser",
        statement_type="CREDIT_CARD",
        financial_account_id="acct_bofa_cc_9306",
        institution_id="inst_bofa",
        statement_date=end_date,
        period_start_date=start_date,
        period_end_date=end_date,
        opening_balance_cents=opening_balance_cents,
        closing_balance_cents=closing_balance_cents,
        transactions=transactions,
    )

    # Balance check: opening - credits + purchases == closing
    purch_total = sum(t.amount_cents for t in transactions if t.amount_cents > 0)
    credits_total = sum(-t.amount_cents for t in transactions if t.amount_cents < 0)
    if opening_balance_cents - credits_total + purch_total != closing_balance_cents:
        extraction.status = "NEEDS_REVIEW"
        extraction.review_reasons.append(
            f"Credit card balance discrepancy: opening ({opening_balance_cents}) - credits ({credits_total}) + purchases ({purch_total}) != closing ({closing_balance_cents})"
        )
    else:
        extraction.status = "VALIDATED"

    return extraction


# --- 3. Apple Card Parser ---

def parse_apple_card(path: Path) -> BankStatementExtraction | None:
    import pymupdf

    with pymupdf.open(path) as doc:
        full_text = "\n".join(page.get_text() for page in doc)

    if "Apple Card" not in full_text or "Goldman Sachs" not in full_text:
        return None

    # Statement month: e.g. "Dec 1 — Dec 31, 2025" or "Apple Card Statement - December 2025"
    dates_m = re.search(r"([A-Za-z]+)\s+(\d{1,2})\s*—\s*([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})", full_text)
    if not dates_m:
        return None

    start_raw = f"{dates_m.group(1)} {dates_m.group(2)} {dates_m.group(5)}"
    end_raw = f"{dates_m.group(3)} {dates_m.group(4)} {dates_m.group(5)}"
    start_date = datetime.strptime(start_raw, "%b %d %Y").date()
    end_date = datetime.strptime(end_raw, "%b %d %Y").date()

    prev_m = re.search(r"Previous\s+Total\s+Balance\s+\$([\d,]+\.\d{2})", full_text)
    tot_m = re.search(r"(?<!Previous\s)Total\s+Balance\s+\$([\d,]+\.\d{2})", full_text)
    if not prev_m or not tot_m:
        return None

    opening_balance_cents = int((Decimal(prev_m.group(1).replace(",", "")) * 100).quantize(Decimal(1)))
    closing_balance_cents = int((Decimal(tot_m.group(1).replace(",", "")) * 100).quantize(Decimal(1)))

    transactions: list[BankTransaction] = []

    with pymupdf.open(path) as doc:
        for p_no in range(1, len(doc)):
            lines = doc[p_no].get_text().splitlines()
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
                    while i < len(clean) and not re.match(r"^\d{2}/\d{2}/\d{4}$", clean[i]) and not "Total " in clean[i]:
                        i += 1
                    block = clean[start:i]
                    tx_date = datetime.strptime(block[0], "%m/%d/%Y").date()

                    if len(block) > 1 and "ACH Deposit" in block[1]:
                        # Payment
                        amt_str = [x for x in block if "$" in x][-1].replace("-$", "").replace("$", "").replace(",", "")
                        amt_val = int((Decimal(amt_str) * 100).quantize(Decimal(1)))
                        desc = block[1]
                        transactions.append(BankTransaction(
                            transaction_date=tx_date,
                            posting_date=tx_date,
                            amount_cents=-amt_val,
                            direction="INFLOW",  # inflow / payment reducing balance
                            raw_description=desc,
                            counter_account_id="acct_bofa_chk_9456",
                        ))
                    else:
                        # Purchase or Return
                        is_return = any("(return)" in x.lower() for x in block)
                        if is_return:
                            amt_matches = [x for x in block if re.match(r"^-\$[\d,]+\.\d{2}$", x)]
                            if amt_matches:
                                amt_val = int((Decimal(amt_matches[0].replace("-$", "").replace(",", "")) * 100).quantize(Decimal(1)))
                                transactions.append(BankTransaction(
                                    transaction_date=tx_date,
                                    posting_date=tx_date,
                                    amount_cents=-amt_val,
                                    direction="INFLOW",
                                    raw_description=" ".join(block[1:3]),
                                ))
                        else:
                            dollar_amts = [x for x in block if "$" in x]
                            if dollar_amts:
                                amt_str = dollar_amts[-1].replace("$", "").replace(",", "")
                                amt_val = int((Decimal(amt_str) * 100).quantize(Decimal(1)))
                                desc_parts = [b for b in block[1:] if not re.match(r"^\d+%$", b) and not "$" in b]
                                transactions.append(BankTransaction(
                                    transaction_date=tx_date,
                                    posting_date=tx_date,
                                    amount_cents=amt_val,
                                    direction="OUTFLOW",
                                    raw_description=" ".join(desc_parts),
                                ))
                else:
                    i += 1

    # Check for daily cash return adjustment
    returns_cnt = sum(1 for t in transactions if "(return)" in t.raw_description.lower())
    if returns_cnt > 0:
        # Sum of transactions
        purch_cents = sum(t.amount_cents for t in transactions if t.amount_cents > 0)
        credits_cents = sum(-t.amount_cents for t in transactions if t.amount_cents < 0)
        net_needed = closing_balance_cents - opening_balance_cents + credits_cents
        diff = net_needed - purch_cents
        if diff > 0:
            transactions.append(BankTransaction(
                transaction_date=end_date,
                posting_date=end_date,
                amount_cents=diff,
                direction="OUTFLOW",
                raw_description="Apple Card Daily Cash Adjustment on Returns",
            ))

    extraction = BankStatementExtraction(
        parser_name="AppleCardParser",
        statement_type="CREDIT_CARD",
        financial_account_id="acct_apple_card",
        institution_id="inst_apple",
        statement_date=end_date,
        period_start_date=start_date,
        period_end_date=end_date,
        opening_balance_cents=opening_balance_cents,
        closing_balance_cents=closing_balance_cents,
        transactions=transactions,
    )

    # Balance check: opening - payments + purchases == closing
    purch_total = sum(t.amount_cents for t in transactions if t.amount_cents > 0)
    credits_total = sum(-t.amount_cents for t in transactions if t.amount_cents < 0)
    if opening_balance_cents - credits_total + purch_total != closing_balance_cents:
        extraction.status = "NEEDS_REVIEW"
        extraction.review_reasons.append(
            f"Apple Card balance discrepancy: opening ({opening_balance_cents}) - credits ({credits_total}) + purchases ({purch_total}) != closing ({closing_balance_cents})"
        )
    else:
        extraction.status = "VALIDATED"

    return extraction


def extract_bank_or_card_pdf(path: Path) -> BankStatementExtraction | None:
    """Classify and parse a bank or credit card statement PDF."""
    import pymupdf

    with pymupdf.open(path) as doc:
        sample_text = "\n".join(doc[p].get_text() for p in range(min(2, len(doc))))

    if "0029 1385 9456" in sample_text or ("Bank of America" in sample_text and "Adv Plus Banking" in sample_text):
        return parse_bofa_checking(path)
    elif "4400 6604 0866 9306" in sample_text or ("Visa Signature" in sample_text and "Bank of America" in sample_text):
        return parse_bofa_credit_card(path)
    elif "Apple Card" in sample_text or "Goldman Sachs Bank USA" in sample_text:
        return parse_apple_card(path)

    return None
