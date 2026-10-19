"""Phase 2.1 review remediation: financial semantics of the metrics engine.

These tests assert *financial* outcomes derived by hand from each scenario, not a
restatement of the formula the implementation happens to use. Every expected
number below is an amount a person would compute from the transactions.
"""

from datetime import date

import pytest
from sqlmodel import Session, select

from proptally.db.models import (
    Document,
    FinancialAccount,
    FinancialInstitution,
    LoanAccount,
    MortgagePeriod,
    Property,
    Statement,
    Transaction,
    TransactionProcessingState,
    TransactionSplit,
)
from proptally.domain.accounting.coa import (
    MORTGAGE_INTEREST_CODE,
    RENT_RECEIVABLE_CODE,
    RENTAL_INCOME_CODE,
    REPAIRS_CODE,
    ensure_entity_ledger_accounts,
)
from proptally.domain.accounting.ledger import (
    add_journal_line,
    create_journal_entry,
    post_journal_entry,
)
from proptally.domain.accounting.metrics import (
    calculate_portfolio_equity,
    calculate_property_financials,
)

LARKSPUR = "prop_tinsley_huntley"
ELDERBERRY = "prop_burkey_algonquin"

# Ledger codes owned by the entities the fixture declares. They are allocated
# from the entity rows on every run (the checking account, the Visa liability,
# each door's fixed asset, and the Alder door's mortgage principal and escrow)
# and rebound here, so no scenario depends on a pinned account number.
CHECKING = None
CARD_LIABILITY = None
ALDER_ASSET = None
ROWAN_ASSET = None
ALDER_MORTGAGE = None
ALDER_ESCROW = None


@pytest.fixture(name="env")
def env_fixture(session: Session):
    """Two doors (one rental, one primary residence), a bank, a card and a statement."""
    bofa = FinancialInstitution(id="inst_env_bofa", name="Bank of America")
    session.add(bofa)
    session.flush()

    chk = FinancialAccount(
        id="acct_env_chk", institution_id=bofa.id, account_name="Checking",
        account_type="CHECKING", account_number_masked="...0001",
    )
    visa = FinancialAccount(
        id="acct_env_visa", institution_id=bofa.id, account_name="Visa",
        account_type="CREDIT_CARD", account_number_masked="...0002",
    )
    mortgage_acct = FinancialAccount(
        id="acct_env_mortgage", institution_id=bofa.id, account_name="Alder Mortgage Loan",
        account_type="MORTGAGE", account_number_masked="...0003",
    )
    session.add(chk)
    session.add(visa)
    session.add(mortgage_acct)
    larkspur = Property(
        id=LARKSPUR, name="Larkspur", address_line1="12307 Larkspur St", city="Westbrook",
        zip_code="60196", property_type="RENTAL_INVESTMENT", purchase_price_cents=42500000,
    )
    elderberry = Property(
        id=ELDERBERRY, name="Elderberry", address_line1="3345 Elderberry Dr", city="Cedar Grove",
        zip_code="60198", property_type="PRIMARY_RESIDENCE", purchase_price_cents=65000000,
    )
    session.add(larkspur)
    session.add(elderberry)
    session.flush()

    # The Alder door's mortgage loan owns the principal and escrow accounts the
    # debt-service scenarios post against.
    alder_loan = LoanAccount(
        id="loan_env_alder", financial_account_id=mortgage_acct.id, property_id=larkspur.id,
        loan_number="0700000001", term_months=360,
    )
    session.add(alder_loan)

    doc = Document(
        id="doc_env", file_sha256="sha_env", original_filename="f.pdf",
        storage_path="/p", file_size_bytes=1,
    )
    session.add(doc)
    session.flush()
    statement = Statement(
        id="stmt_env", document_id=doc.id, financial_account_id=chk.id,
        statement_type="CHECKING", active_parser_name="T", active_parser_version="1",
        statement_date=date(2026, 3, 31),
    )
    session.add(statement)
    session.flush()

    # Give every declared entity the ledger account it owns, then read the codes
    # off the rows: this portfolio's account numbers are its own.
    ensure_entity_ledger_accounts(session)

    global CHECKING, CARD_LIABILITY, ALDER_ASSET, ROWAN_ASSET
    global ALDER_MORTGAGE, ALDER_ESCROW
    CHECKING = chk.ledger_account_code
    CARD_LIABILITY = visa.ledger_account_code
    ALDER_ASSET = larkspur.asset_ledger_code
    ROWAN_ASSET = elderberry.asset_ledger_code
    ALDER_MORTGAGE = alder_loan.principal_ledger_code
    ALDER_ESCROW = alder_loan.escrow_ledger_code

    return {"larkspur": larkspur, "elderberry": elderberry, "statement": statement}


def post_entry(session, entry_date, entry_type, description, lines, key=None, source_txn=None):
    """Creates and posts a journal entry from (code, debit, credit, property) tuples."""
    entry = create_journal_entry(
        session, entry_date, entry_type, description, key, source_transaction_id=source_txn
    )
    for code, debit, credit, prop in lines:
        add_journal_line(session, entry.id, code, debit_cents=debit, credit_cents=credit, property_id=prop)
    return post_journal_entry(session, entry.id)


def rent_received(session, amount_cents, key, prop=LARKSPUR, day=1):
    """Rent that genuinely landed in the checking account."""
    return post_entry(session, date(2026, 3, day), "REVENUE_POST", "Rent received", [
        (CHECKING, amount_cents, 0, None),
        (RENTAL_INCOME_CODE, 0, amount_cents, prop),
    ], key=key)


# --- 1 & 2: accrued rent versus rent actually received in checking ----------

def test_accrued_rent_is_revenue_but_not_collected_cash(session: Session, env):
    """Revenue is recognised when earned; only a checking receipt makes it OCF."""
    post_entry(session, date(2026, 3, 1), "REVENUE_POST", "March rent accrued", [
        (RENT_RECEIVABLE_CODE, 200000, 0, LARKSPUR),   # rent receivable: no cash moved
        (RENTAL_INCOME_CODE, 0, 200000, LARKSPUR),
    ], key="T1:ACCRUED")

    accrued = calculate_property_financials(session, property_id=LARKSPUR)
    assert accrued["gross_rental_income_cents"] == 200000
    assert accrued["cash_rent_collected_cents"] == 0
    assert accrued["ocf_cents"] == 0
    assert accrued["noi_cents"] == 200000  # NOI is accrual based

    rent_received(session, 300000, "T1:CASH", day=5)

    combined = calculate_property_financials(session, property_id=LARKSPUR)
    assert combined["gross_rental_income_cents"] == 500000
    assert combined["cash_rent_collected_cents"] == 300000
    assert combined["ocf_cents"] == 300000


def test_rent_is_only_counted_when_checking_actually_received_it(session: Session, env):
    """A revenue credit settled to a non-cash asset is never treated as collected."""
    post_entry(session, date(2026, 3, 2), "REVENUE_POST", "Rent posted to receivable", [
        (RENT_RECEIVABLE_CODE, 150000, 0, LARKSPUR),
        (RENTAL_INCOME_CODE, 0, 150000, LARKSPUR),
    ], key="T2:RECV")

    financials = calculate_property_financials(session, property_id=LARKSPUR)
    assert financials["gross_rental_income_cents"] == 150000
    assert financials["cash_rent_collected_cents"] == 0
    assert financials["cads_cents"] == 0

    rent_received(session, 150000, "T2:CASH", day=3)

    after = calculate_property_financials(session, property_id=LARKSPUR)
    assert after["cash_rent_collected_cents"] == 150000
    assert after["ocf_cents"] == 150000


# --- 3 & 4: debt service contains principal and interest, not escrow --------

def _mortgage_payment(session, key="T34:PAY", day=28):
    return post_entry(session, date(2026, 3, day), "CASH_SETTLEMENT", "Mortgage payment", [
        (ALDER_MORTGAGE, 50000, 0, LARKSPUR),            # principal
        (MORTGAGE_INTEREST_CODE, 40000, 0, LARKSPUR),    # interest
        (ALDER_ESCROW, 30000, 0, LARKSPUR),              # escrow funding: a transfer, not debt service
        (CHECKING, 0, 120000, None),
    ], key=key)


def test_escrow_funding_is_not_debt_service(session: Session, env):
    """Escrow is a transfer into a servicing reserve, so it stays out of CADS."""
    _mortgage_payment(session)

    financials = calculate_property_financials(session, property_id=LARKSPUR)
    assert financials["escrow_funded_cents"] == 30000
    assert financials["total_debt_service_cents"] == 90000
    assert financials["total_debt_service_cents"] != 120000
    assert financials["cads_cents"] == -90000  # no rent received in this scenario


def test_principal_and_interest_make_up_actual_debt_service(session: Session, env):
    """Debt service is exactly what the bank was paid: principal plus interest."""
    rent_received(session, 300000, "T4:RENT")
    _mortgage_payment(session)

    financials = calculate_property_financials(session, property_id=LARKSPUR)
    assert financials["principal_paid_cents"] == 50000
    assert financials["mortgage_interest_cents"] == 40000
    assert financials["total_debt_service_cents"] == 90000
    assert financials["ocf_cents"] == 300000
    assert financials["cads_cents"] == 210000  # 300,000 cash less 90,000 bank debt service


# --- 5: untagged expenses never inflate rental metrics ---------------------

def test_unallocated_expenses_are_reported_but_excluded(session: Session, env):
    """An expense with no door attribution cannot be charged to rental activity."""
    rent_received(session, 300000, "T5:RENT")

    post_entry(session, date(2026, 3, 8), "EXPENSE_POST", "Untagged repair", [
        (REPAIRS_CODE, 10000, 0, None),       # no property attribution
        (CHECKING, 0, 10000, None),
    ], key="T5:UNTAGGED")

    post_entry(session, date(2026, 3, 9), "EXPENSE_POST", "Tagged repair", [
        (REPAIRS_CODE, 25000, 0, LARKSPUR),
        (CHECKING, 0, 25000, None),
    ], key="T5:TAGGED")

    financials = calculate_property_financials(session, property_id=LARKSPUR)
    assert financials["repairs_cents"] == 25000
    assert financials["untagged_operating_expense_cents"] == 10000
    assert financials["direct_checking_opex_cents"] == 25000
    assert financials["ocf_cents"] == 275000  # 300,000 in, less only the tagged 25,000
    assert financials["noi_cents"] == 275000


# --- 6: mixed-use card settlement uses the transaction allocation ----------

def _card_settlement(session, env, *, liability_property_tag=None):
    """Settles a $600 card bill: $400 rental, $200 personal."""
    txn = Transaction(
        id="txn_settlement_mixed",
        statement_id=env["statement"].id,
        financial_account_id="acct_env_chk",
        transaction_date=date(2026, 3, 20),
        amount_cents=-60000,
        direction="OUTFLOW",
        raw_description="Visa payoff from checking",
    )
    session.add(txn)
    session.flush()
    session.add(TransactionSplit(
        id="split_settle_rental", transaction_id=txn.id, property_id=LARKSPUR,
        category="REPAIRS", amount_cents=40000, tax_review_status="CLIENT_CONFIRMED",
    ))
    session.add(TransactionSplit(
        id="split_settle_personal", transaction_id=txn.id, property_id=None,
        category="PERSONAL", amount_cents=20000, tax_review_status="CLIENT_CONFIRMED",
    ))
    session.add(TransactionProcessingState(
        transaction_id=txn.id, allocation_status="FULLY_ALLOCATED",
        allocated_cents=60000, residual_cents=0, posting_status="UNPOSTED",
    ))
    session.commit()

    post_entry(session, date(2026, 3, 20), "CASH_SETTLEMENT", "Visa payoff", [
        (CARD_LIABILITY, 60000, 0, liability_property_tag),   # card liability cleared
        (CHECKING, 0, 60000, None),
    ], key="T6:SETTLE", source_txn=txn.id)
    return txn


def test_mixed_use_card_settlement_uses_the_transaction_allocation(session: Session, env):
    """The rental share is the allocated 400, not the whole 600 payment."""
    rent_received(session, 300000, "T6:RENT")
    # The liability line is tagged with the full amount on purpose: the split
    # allocation must win over that tag.
    _card_settlement(session, env, liability_property_tag=LARKSPUR)

    financials = calculate_property_financials(session, property_id=LARKSPUR)
    assert financials["card_settled_rental_opex_cents"] == 40000
    assert financials["ocf_cents"] == 260000  # 300,000 received less 40,000 rental share
    assert financials["cads_cents"] == 260000


def test_settlement_without_linked_allocation_is_not_guessed(session: Session, env):
    """A settlement with no linked transaction is reported, never inferred from tags."""
    rent_received(session, 300000, "T6B:RENT")
    # Tagged liability lines, but no source transaction to allocate against.
    post_entry(session, date(2026, 3, 20), "CASH_SETTLEMENT", "Unlinked payoff", [
        (CARD_LIABILITY, 60000, 0, LARKSPUR),
        (CHECKING, 0, 60000, None),
    ], key="T6B:SETTLE")

    financials = calculate_property_financials(session, property_id=LARKSPUR)
    assert financials["card_settled_rental_opex_cents"] == 0
    assert financials["ocf_cents"] == 300000


# --- 7: primary residence excluded by classification, not by account code --

def test_primary_residence_excluded_by_classification_not_by_account_code(session: Session, env):
    """Rowan activity sits on the SAME ledger codes as a rental and is still excluded."""
    # Opening debt for both doors on the same liability account: the Alder door's
    # mortgage principal, which the primary residence also happens to bank with.
    post_entry(session, date(2026, 1, 1), "OPENING_BALANCE", "Alder opening", [
        (ALDER_ASSET, 20000000, 0, None),
        (ALDER_MORTGAGE, 0, 20000000, LARKSPUR),
    ], key="T7:OPEN_T")
    post_entry(session, date(2026, 1, 1), "OPENING_BALANCE", "Rowan opening", [
        (ROWAN_ASSET, 50000000, 0, None),
        (ALDER_MORTGAGE, 0, 50000000, ELDERBERRY),
    ], key="T7:OPEN_B")

    post_entry(session, date(2026, 3, 28), "CASH_SETTLEMENT", "Alder payment", [
        (ALDER_MORTGAGE, 50000, 0, LARKSPUR),
        (MORTGAGE_INTEREST_CODE, 40000, 0, LARKSPUR),
        (CHECKING, 0, 90000, None),
    ], key="T7:LARKSPUR")
    # Primary residence on the very same mortgage principal, interest and repairs
    # account codes as the rental.
    post_entry(session, date(2026, 3, 29), "CASH_SETTLEMENT", "Rowan payment", [
        (ALDER_MORTGAGE, 70000, 0, ELDERBERRY),
        (MORTGAGE_INTEREST_CODE, 90000, 0, ELDERBERRY),
        (CHECKING, 0, 160000, None),
    ], key="T7:ELDERBERRY")
    post_entry(session, date(2026, 3, 30), "EXPENSE_POST", "Rowan roof repair", [
        (REPAIRS_CODE, 45000, 0, ELDERBERRY),
        (CHECKING, 0, 45000, None),
    ], key="T7:ROWAN_REPAIR")
    rent_received(session, 300000, "T7:RENT")

    portfolio = calculate_property_financials(session)
    assert portfolio["gross_rental_income_cents"] == 300000
    assert portfolio["repairs_cents"] == 0
    assert portfolio["principal_paid_cents"] == 50000
    assert portfolio["mortgage_interest_cents"] == 40000
    assert portfolio["total_debt_service_cents"] == 90000
    assert portfolio["cads_cents"] == 210000

    # Querying the primary residence directly is refused as rental activity.
    elderberry = calculate_property_financials(session, property_id=ELDERBERRY)
    assert elderberry["rental_metrics_applicable"] is False
    assert elderberry["scope_property_type"] == "PRIMARY_RESIDENCE"
    assert elderberry["total_debt_service_cents"] == 0
    assert elderberry["repairs_cents"] == 0

    # Its debt still counts toward portfolio debt.
    equity = calculate_portfolio_equity(session)
    rowan_metric = next(m for m in equity["property_metrics"] if m["property"].id == ELDERBERRY)
    assert rowan_metric["principal_source"] == "LEDGER_DERIVED"
    assert rowan_metric["principal_cents"] == 50000000 - 70000


# --- 8: deterministic period filters --------------------------------------

def test_year_and_explicit_range_are_mutually_exclusive(session: Session, env):
    with pytest.raises(ValueError, match="Conflicting reporting filters"):
        calculate_property_financials(session, property_id=LARKSPUR, year=2026, start_date=date(2026, 1, 1))
    with pytest.raises(ValueError, match="Conflicting reporting filters"):
        calculate_property_financials(session, property_id=LARKSPUR, year=2026, end_date=date(2026, 6, 30))
    with pytest.raises(ValueError, match="Conflicting reporting filters"):
        calculate_property_financials(
            session, property_id=LARKSPUR, year=2026,
            start_date=date(2026, 1, 1), end_date=date(2026, 6, 30),
        )


def test_period_filters_select_the_right_entries(session: Session, env):
    post_entry(session, date(2025, 6, 1), "REVENUE_POST", "2025 rent", [
        (CHECKING, 100000, 0, None), (RENTAL_INCOME_CODE, 0, 100000, LARKSPUR),
    ], key="T8:2025")
    post_entry(session, date(2026, 6, 1), "REVENUE_POST", "2026 rent", [
        (CHECKING, 250000, 0, None), (RENTAL_INCOME_CODE, 0, 250000, LARKSPUR),
    ], key="T8:2026")

    assert calculate_property_financials(session, property_id=LARKSPUR, year=2025)["goi_cents"] == 100000
    assert calculate_property_financials(session, property_id=LARKSPUR, year=2026)["goi_cents"] == 250000
    ranged = calculate_property_financials(
        session, property_id=LARKSPUR, start_date=date(2026, 1, 1), end_date=date(2026, 5, 31)
    )
    assert ranged["goi_cents"] == 0


def test_inverted_date_range_is_rejected(session: Session, env):
    with pytest.raises(ValueError, match="Invalid reporting period"):
        calculate_property_financials(
            session, property_id=LARKSPUR, start_date=date(2026, 6, 1), end_date=date(2026, 1, 1)
        )


# --- 9: original principal is never reported as current debt ---------------

def _record_original_principal(session, original_principal_cents):
    """Records the door's original loan amount; current debt is never read from it.

    The loan itself is already declared by the fixture, so this only supplies the
    origination figure a servicer-reported or ledger-derived balance must beat.
    """
    loan = session.exec(select(LoanAccount).where(LoanAccount.property_id == LARKSPUR)).one()
    loan.original_principal_cents = original_principal_cents
    session.commit()
    return loan


def test_original_principal_is_never_reported_as_current_debt(session: Session, env):
    loan = _record_original_principal(session, 99000000)

    equity = calculate_portfolio_equity(session)
    metric = next(m for m in equity["property_metrics"] if m["property"].id == LARKSPUR)
    assert metric["principal_source"] == "UNAVAILABLE"
    assert metric["principal_cents"] is None
    assert metric["servicer_reported_principal_cents"] is None
    assert equity["total_debt_cents"] == 0
    assert LARKSPUR in equity["unavailable_property_ids"]

    # A servicer statement is authoritative and takes precedence.
    session.add(MortgagePeriod(
        id="mp_env_1", statement_id=env["statement"].id, loan_account_id=loan.id,
        payment_due_date=date(2026, 4, 1), statement_date=date(2026, 3, 31),
        principal_billed_cents=0, interest_billed_cents=0, escrow_billed_cents=0,
        total_amount_billed_cents=0, outstanding_principal_cents=44500000,
        interest_rate_bps=3125,
    ))
    session.commit()

    reported = calculate_portfolio_equity(session)
    metric = next(m for m in reported["property_metrics"] if m["property"].id == LARKSPUR)
    assert metric["principal_source"] == "SERVICER_REPORTED"
    assert metric["principal_cents"] == 44500000
    assert reported["total_debt_cents"] == 44500000


def test_ledger_principal_is_used_and_is_not_the_original_amount(session: Session, env):
    """Posted ledger debt wins over the original loan amount, which is never reported."""
    _record_original_principal(session, 77000000)
    post_entry(session, date(2026, 1, 1), "OPENING_BALANCE", "Alder opening debt", [
        (ALDER_ASSET, 20000000, 0, None),
        (ALDER_MORTGAGE, 0, 20000000, LARKSPUR),
    ], key="T9:OPEN")

    equity = calculate_portfolio_equity(session)
    metric = next(m for m in equity["property_metrics"] if m["property"].id == LARKSPUR)
    assert metric["principal_source"] == "LEDGER_DERIVED"
    assert metric["principal_cents"] == 20000000
    assert metric["principal_cents"] != 77000000
    assert equity["total_debt_cents"] == 20000000
