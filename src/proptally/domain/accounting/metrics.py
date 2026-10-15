"""Financial metrics calculation engine for PropTally V2.2.

Definitions implemented here (authoritative: docs/architecture/v2/03_FINANCIAL_DOMAIN_MODEL.md):

- GOI  = rental revenue + fee revenue (accrual, from POSTED entries)
- OpEx = property tax, insurance, repairs, supplies, landscaping, other
- NOI  = GOI - OpEx
- OCF  = cash rent received in checking - direct checking OpEx
         - rental share of card settlements
- Debt Service = principal + interest actually paid at the bank
- CADS = OCF - Debt Service
- Economic Net Income = NOI - mortgage interest - depreciation

Accounting invariants:

1. Only POSTED entries are read. DRAFT and BALANCED entries never contribute.
2. Reversals are compensating POSTED entries, so summing POSTED lines nets them
   out automatically; no separate reversal handling is required.
3. Rent is only treated as *collected* when the same journal entry moves cash
   into a checking account in the same direction as the revenue. Accrued rent
   (no cash movement) is reported but excluded from OCF.
4. Every figure is attributed through explicit journal-line property attribution
   and the property's classification. Untagged operating expenses are reported
   separately and never folded into rental metrics.
5. Escrow funding is a cash movement, not debt service: it is reported separately
   and never included in Debt Service or CADS.
6. The rental share of a settled credit-card payment comes from the settlement
   transaction's allocation, never from optional property tags on liability lines.
7. Primary residence activity is excluded from rental NOI/OCF/Debt Service/CADS by
   property classification. Primary residence debt remains in portfolio debt.
"""

from datetime import date
from typing import Any, Dict, Optional

from sqlmodel import Session, select

from proptally.db.models import (
    JournalEntry,
    JournalEntryLine,
    LoanAccount,
    MortgagePeriod,
    Property,
    PropertyValuation,
    TransactionSplit,
)

RENTAL_INVESTMENT = "RENTAL_INVESTMENT"
PRIMARY_RESIDENCE = "PRIMARY_RESIDENCE"

# Chart of accounts (see domain/accounting/coa.py).
CHECKING_CODES = (1010, 1020)
ESCROW_CODES = (1110, 1120, 1130)
CARD_LIABILITY_CODES = (2010, 2020)
MORTGAGE_DEBT_CODES = (2110, 2120, 2130)
MORTGAGE_CLEARING_CODES = (2115, 2125, 2135)
RENTAL_REVENUE_CODES = (4010, 4020)
FEE_REVENUE_CODES = (4015,)
MORTGAGE_INTEREST_CODE = 5010
MORTGAGE_LATE_FEE_CODE = 5080  # lender fee; part of a mortgage payment, not of debt service
PERSONAL_DRAW_CODE = 5090
OPEX_CODES = (5020, 5030, 5040, 5050, 5060, 5070, 5072, 5075, 5078, 5080)

# Split categories that represent a rental *operating expense* when a card
# payment settles them. Property attribution alone does not make an allocation an
# expense: PERSONAL is a draw, RENT_INCOME/FEE are income, and MORTGAGE settles
# a liability.
CARD_SETTLED_OPEX_CATEGORIES = frozenset({
    "TAXES",
    "INSURANCE",
    "REPAIRS",
    "SUPPLIES",
    "SNOW_LANDSCAPING",
    "MANAGEMENT",
    "COMMISSIONS",
    "HOA",
    "LEGAL",
})

# Principal ledger account per loan, for debt that predates line-level attribution
# (opening balances carry no property tag).
LOAN_PRINCIPAL_CODE = {
    "loan_rocket_1000000001": 2110,
    "loan_pnc_1000000002": 2120,
    "loan_ccm_1000000003": 2130,
}

# Evidence backing a reported principal balance.
SOURCE_SERVICER = "SERVICER_REPORTED"
SOURCE_LEDGER = "LEDGER_DERIVED"
SOURCE_UNAVAILABLE = "UNAVAILABLE"


def _resolve_period(
    year: Optional[int],
    start_date: Optional[date],
    end_date: Optional[date],
) -> tuple[Optional[date], Optional[date]]:
    """Resolves the reporting period, rejecting ambiguous filter combinations.

    ``year`` and an explicit date range are mutually exclusive: silently mixing
    them was the previous behaviour and made the filter non-deterministic.
    """
    if year is not None and (start_date is not None or end_date is not None):
        raise ValueError(
            "Conflicting reporting filters: pass either 'year' or an explicit "
            "'start_date'/'end_date' range, not both."
        )
    if year is not None:
        return date(year, 1, 1), date(year, 12, 31)
    if start_date is not None and end_date is not None and start_date > end_date:
        raise ValueError(
            f"Invalid reporting period: start_date {start_date} is after end_date {end_date}."
        )
    return start_date, end_date


def _net(lines, codes) -> int:
    """Signed movement over the given account codes; positive = debit."""
    return sum(l.debit_cents - l.credit_cents for l in lines if l.ledger_account_code in codes)


def _load_posted_ledger(session: Session, start_date=None, end_date=None, exclude_opening=True):
    """Loads POSTED entries in the period with their lines, keyed by entry id."""
    query = select(JournalEntry).where(JournalEntry.status == "POSTED")
    if exclude_opening:
        query = query.where(JournalEntry.entry_type != "OPENING_BALANCE")
    if start_date is not None:
        query = query.where(JournalEntry.entry_date >= start_date)
    if end_date is not None:
        query = query.where(JournalEntry.entry_date <= end_date)

    entries = session.exec(query).all()
    if not entries:
        return [], {}

    entry_ids = [e.id for e in entries]
    lines = session.exec(
        select(JournalEntryLine).where(JournalEntryLine.journal_entry_id.in_(entry_ids))
    ).all()

    lines_by_entry: Dict[str, list] = {}
    for line in lines:
        lines_by_entry.setdefault(line.journal_entry_id, []).append(line)
    return entries, lines_by_entry


def _reversed_entry_ids(session: Session) -> set:
    """Ids of POSTED entries that a POSTED compensating reversal cancels."""
    rows = session.exec(
        select(JournalEntry.reversal_of_id).where(
            JournalEntry.status == "POSTED",
            JournalEntry.reversal_of_id.is_not(None),
        )
    ).all()
    return {row for row in rows if row}


def _mortgage_payment_codes() -> frozenset:
    """Ledger accounts that belong to a mortgage payment or its servicing application.

    Used to reject entries that mix a mortgage posting with unrelated activity: an
    unrelated checking outflow in the same entry must not stand in for a payment.
    """
    return frozenset(
        set(MORTGAGE_DEBT_CODES)
        | {MORTGAGE_INTEREST_CODE, MORTGAGE_LATE_FEE_CODE}
        | set(ESCROW_CODES)
        | set(MORTGAGE_CLEARING_CODES)
    )


def _validated_clearing_funding(lines) -> dict | None:
    """Funding released by a standalone bank-to-clearing settlement.

    Strict allowlist rather than a broad exclusion list: the entry may contain
    only debits to mortgage clearing accounts and credits to an eligible bank
    account, and the bank credit must cover the clearing debits. Anything else —
    a mortgage principal or interest line, an escrow movement in either direction,
    or any other account — means the entry mixes funding with other accounting
    events and cannot be confidently read as a settlement, so it releases no
    funding.

    Returns ``{clearing_code: cents}`` for a valid settlement, or ``None`` when
    the entry is not one.
    """
    funding: dict[int, int] = {}
    bank_credited_cents = 0
    for line in lines:
        code = line.ledger_account_code
        if code in MORTGAGE_CLEARING_CODES:
            if line.credit_cents:
                return None  # clearing credited: an application, not funding
            funding[code] = funding.get(code, 0) + line.debit_cents
        elif code in CHECKING_CODES:
            if line.debit_cents:
                return None  # the bank account must be the payer
            bank_credited_cents += line.credit_cents
        else:
            return None  # an unexpected account makes the entry ambiguous

    total_funded_cents = sum(funding.values())
    if total_funded_cents <= 0 or bank_credited_cents < total_funded_cents:
        return None
    return funding


def _confirmed_debt_service_entry_ids(session: Session) -> set:
    """Entries whose applied principal and interest are backed by a real payment.

    PropTally records a mortgage payment in up to two stages: the bank settles
    funds, then the servicer applies them to principal, interest and escrow.
    Debt service is recognised only for an application supported by an actual
    payment, which is either

    * a **direct payment** — a pure mortgage-payment entry that pays the servicer
      from a deposit account, or
    * a **servicer application** — the entry applies the payment out of a
      mortgage clearing account that a bank settlement has already funded.

    Recognition requires the funded cash to cover the *whole payment applied from
    the clearing account* (principal + interest + positive escrow contributions),
    not merely the clearing credit: an entry that credits a small clearing amount
    while debiting a much larger principal and interest is not funded and
    recognises nothing. Partial coverage is never apportioned on an invented
    basis — the unsupported amount is reported by the caller instead.

    Escrow is measured **gross, never net**. A credit to escrow is a refund,
    release or adjustment rather than funding, so it must not reduce the amount of
    principal and interest requiring cover. An entry that both applies a payment
    and credits escrow cannot have that relationship established from the ledger,
    so it fails closed and recognises nothing rather than inferring that the
    credit funded principal or interest.

    Funding is consumed as it is drawn, so the same money cannot support two
    applications. A servicer bill, an unfunded application and a clearing-only
    posting recognise nothing. Reversed entries and their compensating reversals
    are skipped, so a bounced payment stops being counted and its funding returns
    to the clearing balance.

    Funding is evaluated across all posted entries rather than the reporting
    period, so a payment settled in one period and applied in the next is still
    recognised. Entries are replayed in (date, recording order) sequence, the
    order in which the two stages are recorded.
    """
    entries, lines_by_entry = _load_posted_ledger(session)
    if not entries:
        return set()

    mortgage_codes = _mortgage_payment_codes()
    reversed_ids = _reversed_entry_ids(session)
    funded: dict[int, int] = {code: 0 for code in MORTGAGE_CLEARING_CODES}
    confirmed = set()

    for entry in sorted(entries, key=lambda e: (e.entry_date, e.created_at)):
        if entry.id in reversed_ids or entry.entry_type == "REVERSAL":
            continue
        lines = lines_by_entry.get(entry.id, [])
        if not lines:
            continue

        applied_cents = _net(lines, MORTGAGE_DEBT_CODES) + _net(lines, (MORTGAGE_INTEREST_CODE,))
        # Escrow is measured GROSS and never netted. A credit to escrow is a
        # refund, release or adjustment, not funding, so it must not reduce the
        # amount of principal and interest that has to be covered — netting was
        # what let a credit manufacture evidence that principal was paid.
        escrow_contribution_cents = sum(
            l.debit_cents for l in lines if l.ledger_account_code in ESCROW_CODES
        )
        escrow_credit_cents = sum(
            l.credit_cents for l in lines if l.ledger_account_code in ESCROW_CODES
        )
        # A payment entry that also releases escrow cannot be related to the
        # payment with confidence. Fail closed rather than infer that the credit
        # funded principal or interest, or that an unrelated escrow debit was part
        # of this payment.
        coherent_payment = escrow_credit_cents == 0
        payment_cents = applied_cents + escrow_contribution_cents
        cash_out_cents = -_net(lines, CHECKING_CODES)
        unrelated_debits = sum(
            l.debit_cents for l in lines if l.ledger_account_code not in mortgage_codes
        )

        # Direct payment: a mortgage-payment entry that pays the servicer from a
        # deposit account. The outflow must cover the entire payment and the entry
        # must contain nothing else, so an unrelated outflow cannot qualify.
        if (
            applied_cents > 0
            and coherent_payment
            and unrelated_debits == 0
            and cash_out_cents >= payment_cents
        ):
            confirmed.add(entry.id)
            continue

        # Clearing funding is released only by a standalone bank-to-clearing
        # settlement. An entry that also applies principal or interest, touches
        # escrow, or involves any other account is ambiguous and fails closed, so
        # it cannot create funding a later application could consume.
        funding_by_code = _validated_clearing_funding(lines)
        if funding_by_code is not None:
            for code, amount_cents in funding_by_code.items():
                funded[code] += amount_cents
            continue

        # Two-stage: apply from a clearing account funded by such a settlement.
        for code in MORTGAGE_CLEARING_CODES:
            clearing_net = _net(lines, (code,))
            if clearing_net < 0:
                credited_cents = -clearing_net
                # Two conditions, both required: the payment applied out of the
                # clearing account must not exceed the clearing credit taken, and
                # that credit must be within the funded balance. Checking only the
                # credit recognised funded debt service from a larger unsupported
                # application.
                supported = (
                    applied_cents > 0
                    and coherent_payment
                    and payment_cents <= credited_cents <= funded[code]
                )
                if supported:
                    confirmed.add(entry.id)
                # The clearing credit is consumed either way, so funding that did
                # not support a recognised application cannot be spent again.
                funded[code] = max(0, funded[code] - credited_cents)

    return confirmed


def _rental_scope(session: Session, property_id: Optional[str]):
    """Returns (in-scope property ids, property type) for rental metrics."""
    if property_id is not None:
        prop = session.get(Property, property_id)
        if prop is None:
            raise ValueError(f"Property {property_id} not found.")
        if prop.property_type != RENTAL_INVESTMENT:
            return set(), prop.property_type
        return {prop.id}, prop.property_type

    rental = session.exec(
        select(Property).where(Property.property_type == RENTAL_INVESTMENT)
    ).all()
    return {p.id for p in rental}, None


def calculate_property_financials(
    session: Session,
    property_id: Optional[str] = None,
    year: Optional[int] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    depreciation_cents: int = 0,
) -> Dict[str, Any]:
    """Calculates GOI, OpEx, NOI, OCF, Debt Service, CADS and Economic Net Income.

    Rental metrics are rental-only: a primary residence returns
    ``rental_metrics_applicable = False`` with zeroed figures rather than
    silently reporting rental-style numbers for owner-occupied activity.
    """
    start_date, end_date = _resolve_period(year, start_date, end_date)
    in_scope, scope_property_type = _rental_scope(session, property_id)

    result = _empty_financials_dict()
    result["scope_property_type"] = scope_property_type
    result["period_start"] = start_date
    result["period_end"] = end_date
    result["rental_metrics_applicable"] = bool(in_scope)
    result["in_scope_property_ids"] = sorted(in_scope)

    if not in_scope:
        return result

    entries, lines_by_entry = _load_posted_ledger(session, start_date, end_date)
    reversed_ids = _reversed_entry_ids(session)
    debt_service_entry_ids = _confirmed_debt_service_entry_ids(session)

    included_dates = []
    for entry in entries:
        entry_lines = lines_by_entry.get(entry.id, [])
        if not entry_lines:
            continue
        included_dates.append(entry.entry_date)

        checking_net = _net(entry_lines, CHECKING_CODES)
        scoped = [l for l in entry_lines if l.property_id in in_scope]
        untagged = [l for l in entry_lines if l.property_id is None]

        revenue_net = sum(
            l.credit_cents - l.debit_cents
            for l in scoped
            if l.ledger_account_code in RENTAL_REVENUE_CODES
        )
        fee_net = sum(
            l.credit_cents - l.debit_cents
            for l in scoped
            if l.ledger_account_code in FEE_REVENUE_CODES
        )

        result["gross_rental_income_cents"] += revenue_net
        result["fee_income_cents"] += fee_net

        # Cash rent requires this entry to move cash into checking in the same
        # direction as the revenue. Accrued rent never counts as collected.
        recognised_revenue = revenue_net + fee_net
        if (
            checking_net != 0
            and recognised_revenue != 0
            and (checking_net > 0) == (recognised_revenue > 0)
        ):
            result["cash_rent_collected_cents"] += recognised_revenue

        for line in scoped:
            code = line.ledger_account_code
            net = line.debit_cents - line.credit_cents
            if code == 5020:
                result["property_tax_cents"] += net
            elif code == 5030:
                result["hazard_insurance_cents"] += net
            elif code == 5040:
                result["repairs_cents"] += net
            elif code == 5050:
                result["supplies_cents"] += net
            elif code == 5060:
                result["snow_landscaping_cents"] += net
            elif code in (5070, 5080):
                result["other_operating_expense_cents"] += net
            elif code == PERSONAL_DRAW_CODE:
                result["personal_draws_cents"] += net

        opex_net = sum(
            l.debit_cents - l.credit_cents for l in scoped if l.ledger_account_code in OPEX_CODES
        )
        # Direct checking OpEx: the same entry pays it from checking.
        if opex_net != 0 and checking_net != 0 and (checking_net < 0) == (opex_net > 0):
            result["direct_checking_opex_cents"] += opex_net

        # Debt service counts only when an actual payment supports the
        # application; a bill, an unfunded application or a clearing-only posting
        # must not reduce CADS.
        applied_cents = _net(scoped, MORTGAGE_DEBT_CODES) + _net(scoped, (MORTGAGE_INTEREST_CODE,))
        if entry.id in debt_service_entry_ids:
            result["principal_paid_cents"] += _net(scoped, MORTGAGE_DEBT_CODES)
            result["mortgage_interest_cents"] += _net(scoped, (MORTGAGE_INTEREST_CODE,))
        elif (
            applied_cents > 0
            and entry.id not in reversed_ids
            and entry.entry_type != "REVERSAL"
        ):
            # Applied principal and interest with no confirmed payment behind it.
            # Void entries (reversed, or a compensating reversal) are excluded:
            # they represent nothing at all, not an unsupported payment.
            result["unfunded_applied_debt_service_cents"] += applied_cents
        # Escrow funding is a cash movement reported on its own; it is NOT debt service.
        result["escrow_funded_cents"] += _net(scoped, ESCROW_CODES)
        # Untagged operating activity is reported, never folded into rental metrics.
        result["untagged_operating_expense_cents"] += sum(
            l.debit_cents - l.credit_cents
            for l in untagged
            if l.ledger_account_code in OPEX_CODES
        )
        result["untagged_personal_draw_cents"] += sum(
            l.debit_cents - l.credit_cents
            for l in untagged
            if l.ledger_account_code == PERSONAL_DRAW_CODE
        )

    _accumulate_card_settlements(
        session, in_scope, reversed_ids, start_date, end_date, lines_by_entry, result
    )

    # The window the figures actually cover, so a label can never claim a period
    # the calculation did not use.
    if included_dates:
        result["coverage_start"] = min(included_dates)
        result["coverage_end"] = max(included_dates)

    result["goi_cents"] = result["gross_rental_income_cents"] + result["fee_income_cents"]
    result["total_revenue_cents"] = result["goi_cents"]
    result["total_operating_expenses_cents"] = (
        result["property_tax_cents"]
        + result["hazard_insurance_cents"]
        + result["repairs_cents"]
        + result["supplies_cents"]
        + result["snow_landscaping_cents"]
        + result["other_operating_expense_cents"]
    )
    result["noi_cents"] = result["goi_cents"] - result["total_operating_expenses_cents"]

    # Actual bank debt service: principal and interest only.
    result["total_debt_service_cents"] = (
        result["principal_paid_cents"] + result["mortgage_interest_cents"]
    )

    result["ocf_cents"] = (
        result["cash_rent_collected_cents"]
        - result["direct_checking_opex_cents"]
        - result["card_settled_rental_opex_cents"]
    )
    result["cads_cents"] = result["ocf_cents"] - result["total_debt_service_cents"]
    result["economic_net_income_cents"] = (
        result["noi_cents"] - result["mortgage_interest_cents"] - depreciation_cents
    )
    return result


def _accumulate_card_settlements(
    session: Session,
    in_scope: set,
    reversed_ids: set,
    start_date,
    end_date,
    lines_by_entry: Dict[str, list],
    result: Dict[str, Any],
) -> None:
    """Attributes the rental share of settled credit-card payments.

    A settlement is recognised when a linked POSTED entry moves cash out of
    checking and reduces a card liability. The rental share then comes from that
    settlement transaction's own allocation (its splits). A settlement with no
    linked allocation is reported as unattributed instead of being guessed from
    whatever property tags happen to be on the liability lines.
    """
    settlement_entries: Dict[str, JournalEntry] = {}
    query = select(JournalEntry).where(
        JournalEntry.status == "POSTED",
        JournalEntry.source_transaction_id.is_not(None),
    )
    if start_date is not None:
        query = query.where(JournalEntry.entry_date >= start_date)
    if end_date is not None:
        query = query.where(JournalEntry.entry_date <= end_date)

    for entry in session.exec(query).all():
        if entry.id in reversed_ids:
            continue
        lines = lines_by_entry.get(entry.id)
        if not lines:
            continue
        checking_net = _net(lines, CHECKING_CODES)
        card_net = _net(lines, CARD_LIABILITY_CODES)
        # Cash leaves checking to pay down a card liability.
        if checking_net < 0 and card_net > 0:
            settlement_entries[entry.source_transaction_id] = entry

    if not settlement_entries:
        return

    allocations = session.exec(
        select(TransactionSplit).where(
            TransactionSplit.transaction_id.in_(list(settlement_entries))
        )
    ).all()

    attributed: Dict[str, int] = {}
    for split in allocations:
        attributed[split.transaction_id] = (
            attributed.get(split.transaction_id, 0) + split.amount_cents
        )
        # Two independent conditions: the allocation must belong to a rental door
        # in scope, AND it must be an operating expense category. A personal or
        # income allocation that happens to carry a rental property ID is not
        # rental card OpEx.
        if (
            split.property_id in in_scope
            and str(split.category).upper() in CARD_SETTLED_OPEX_CATEGORIES
        ):
            result["card_settled_rental_opex_cents"] += split.amount_cents
        elif split.property_id in in_scope:
            result["card_settlement_non_expense_cents"] += split.amount_cents

    # A settlement whose allocation does not cover the settled amount is only
    # partly attributable; surface the remainder rather than assuming it.
    for txn_id, entry in settlement_entries.items():
        settled = _net(lines_by_entry.get(entry.id, []), CARD_LIABILITY_CODES)
        result["card_settlement_unattributed_cents"] += max(
            0, settled - attributed.get(txn_id, 0)
        )


def calculate_portfolio_equity(
    session: Session,
    as_of_date: Optional[date] = None,
) -> Dict[str, Any]:
    """Calculates portfolio valuation, outstanding debt and net equity.

    Includes every active property and loan, including the primary residence.

    Each property's debt is reported with its evidence:

    * ``SERVICER_REPORTED`` - the latest authoritative statement balance.
    * ``LEDGER_DERIVED`` - reconstructed from POSTED ledger lines.
    * ``UNAVAILABLE`` - neither exists. The original loan principal is never
      substituted for a current balance.
    """
    properties = session.exec(select(Property).where(Property.is_active == True)).all()

    debt_query = (
        select(JournalEntryLine)
        .join(JournalEntry, JournalEntryLine.journal_entry_id == JournalEntry.id)
        .where(JournalEntry.status == "POSTED")
    )
    if as_of_date is not None:
        debt_query = debt_query.where(JournalEntry.entry_date <= as_of_date)

    debt_by_code: Dict[int, int] = {}
    debt_by_property: Dict[str, int] = {}
    for line in session.exec(debt_query).all():
        code = line.ledger_account_code
        if code not in MORTGAGE_DEBT_CODES:
            continue
        net_cents = line.credit_cents - line.debit_cents
        debt_by_code[code] = debt_by_code.get(code, 0) + net_cents
        if line.property_id:
            debt_by_property[line.property_id] = debt_by_property.get(line.property_id, 0) + net_cents

    # Everything the POSTED ledger records as mortgage debt. Debt that cannot be
    # attributed to a door is still real debt and must not disappear from the
    # portfolio total.
    ledger_total_debt_cents = sum(max(0, net) for net in debt_by_code.values())

    total_valuation_cents = 0
    total_debt_cents = 0
    attributed_ledger_cents = 0
    property_metrics = []
    unavailable_property_ids = []

    for prop in properties:
        val_query = select(PropertyValuation).where(PropertyValuation.property_id == prop.id)
        if as_of_date is not None:
            val_query = val_query.where(PropertyValuation.valuation_date <= as_of_date)
        valuation = session.exec(
            val_query.order_by(PropertyValuation.valuation_date.desc())
        ).first()
        val_cents = valuation.estimated_value_cents if valuation else (prop.purchase_price_cents or 0)
        total_valuation_cents += val_cents

        loan = session.exec(
            select(LoanAccount).where(
                LoanAccount.property_id == prop.id, LoanAccount.is_active == True
            )
        ).first()

        ledger_principal_cents = _ledger_principal_cents(
            prop.id, loan, debt_by_code, debt_by_property
        )
        servicer_principal_cents = _servicer_principal_cents(session, loan, as_of_date)

        if servicer_principal_cents is not None:
            principal_cents, principal_source = servicer_principal_cents, SOURCE_SERVICER
        elif ledger_principal_cents is not None:
            principal_cents, principal_source = ledger_principal_cents, SOURCE_LEDGER
        else:
            principal_cents, principal_source = None, SOURCE_UNAVAILABLE
            unavailable_property_ids.append(prop.id)

        if ledger_principal_cents is not None:
            attributed_ledger_cents += ledger_principal_cents
        if principal_cents is not None:
            total_debt_cents += principal_cents

        property_metrics.append({
            "property": prop,
            "valuation_cents": val_cents,
            "loan": loan,
            "principal_cents": principal_cents,
            "principal_source": principal_source,
            "ledger_principal_cents": ledger_principal_cents,
            "servicer_reported_principal_cents": servicer_principal_cents,
            "equity_cents": val_cents - principal_cents if principal_cents is not None else None,
        })

    # Posted debt not attributable to any door (for example an opening balance on
    # a loan that has no loan_accounts row). Counted once, and labelled.
    unattributed_ledger_cents = max(0, ledger_total_debt_cents - attributed_ledger_cents)
    total_debt_cents += unattributed_ledger_cents

    return {
        "total_valuation_cents": total_valuation_cents,
        "total_debt_cents": total_debt_cents,
        "ledger_total_debt_cents": ledger_total_debt_cents,
        "unattributed_ledger_debt_cents": unattributed_ledger_cents,
        "net_equity_cents": total_valuation_cents - total_debt_cents,
        "unavailable_property_ids": unavailable_property_ids,
        "debt_fully_verified": not unavailable_property_ids and unattributed_ledger_cents == 0,
        "property_metrics": property_metrics,
    }


def _ledger_principal_cents(prop_id, loan, debt_by_code, debt_by_property):
    """POSTED ledger principal for a property, or None when there is no evidence.

    A property's loan principal account carries the opening balance (which has no
    property tag) plus every tagged movement, so the account is preferred over
    line-level tags whenever the loan is known.
    """
    code = LOAN_PRINCIPAL_CODE.get(loan.id) if loan else None
    if code is not None and code in debt_by_code:
        return max(0, debt_by_code[code])
    if prop_id in debt_by_property:
        return max(0, debt_by_property[prop_id])
    return None


def _servicer_principal_cents(session, loan, as_of_date):
    """Latest authoritative servicer-reported principal, or None if unavailable."""
    if loan is None:
        return None
    query = select(MortgagePeriod).where(MortgagePeriod.loan_account_id == loan.id)
    if as_of_date is not None:
        query = query.where(MortgagePeriod.statement_date <= as_of_date)
    period = session.exec(query.order_by(MortgagePeriod.statement_date.desc())).first()
    if period is None or period.outstanding_principal_cents is None:
        return None
    return period.outstanding_principal_cents


def _empty_financials_dict() -> Dict[str, Any]:
    return {
        "gross_rental_income_cents": 0,
        "fee_income_cents": 0,
        "goi_cents": 0,
        "total_revenue_cents": 0,
        "property_tax_cents": 0,
        "hazard_insurance_cents": 0,
        "repairs_cents": 0,
        "supplies_cents": 0,
        "snow_landscaping_cents": 0,
        "other_operating_expense_cents": 0,
        "total_operating_expenses_cents": 0,
        "untagged_operating_expense_cents": 0,
        "untagged_personal_draw_cents": 0,
        "noi_cents": 0,
        "mortgage_interest_cents": 0,
        "principal_paid_cents": 0,
        "escrow_funded_cents": 0,
        "total_debt_service_cents": 0,
        "unfunded_applied_debt_service_cents": 0,
        "personal_draws_cents": 0,
        "cash_rent_collected_cents": 0,
        "direct_checking_opex_cents": 0,
        "card_settled_rental_opex_cents": 0,
        "card_settlement_non_expense_cents": 0,
        "card_settlement_unattributed_cents": 0,
        "ocf_cents": 0,
        "cads_cents": 0,
        "economic_net_income_cents": 0,
        "rental_metrics_applicable": False,
        "scope_property_type": None,
        "period_start": None,
        "period_end": None,
        "coverage_start": None,
        "coverage_end": None,
        "in_scope_property_ids": [],
    }
