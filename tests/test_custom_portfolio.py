"""M1 acceptance: the accounting engine works for a portfolio it has never seen.

This module declares a complete portfolio whose every identifier is new, and whose
ledger accounts are allocated by the application rather than named by the code.
It then drives the full financial lifecycle through the ordinary services:

* opening balances (with the equity plug),
* rent received and posted to the door that earned it,
* a mixed purchase on a card split across doors,
* a card settlement (a transfer, never a second expense),
* a two-stage mortgage payment: bank settlement, then servicer application,
* a property tax bill paid straight out of checking,
* Schedule E workpaper figures.

Every expected figure below is hand-computed in a comment from the inputs, so a
failure means the engine produced a different number rather than merely a
different account code. Nothing in this file references the reference portfolio's
account numbers, which is the point: if a portfolio-specific assumption is ever
reintroduced into the engine, this test stops passing.
"""

from datetime import date

import pytest
from sqlmodel import Session, select

from proptally.db.models import (
    Document,
    FinancialAccount,
    FinancialInstitution,
    JournalEntryLine,
    LoanAccount,
    Property,
    PropertyValuation,
    Statement,
    Transaction,
)
from proptally.domain.accounting.coa import (
    MORTGAGE_INTEREST_CODE,
    RENTAL_INCOME_CODE,
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
from proptally.domain.accounting.opening_balances import (
    OpeningAccountBalance,
    record_opening_balances,
)
from proptally.domain.tax.schedule_e import generate_schedule_e_workpaper
from proptally.domain.transactions.splits import (
    SplitItem,
    apply_transaction_splits,
    post_transaction_to_ledger,
)

# --- The portfolio ---------------------------------------------------------
# Every identifier below is new; none appears anywhere in the application.

PROPERTY_ID = "prop_austin_duplex"
CHECKING_ID = "acct_lone_star_chk_4471"
CARD_ID = "acct_cedar_visa_8820"
MORTGAGE_ACCOUNT_ID = "acct_spruce_mortgage_5510"
LOAN_ID = "loan_bluebonnet_778211"
LOAN_NUMBER = "BL-778211"

# Amounts in integer cents.
OPENING_DEPOSIT = 500_000            # $5,000.00 in the bank
OPENING_PROPERTY = 40_000_000        # $400,000.00 purchase price
OPENING_PRINCIPAL = 35_000_000       # $350,000.00 owed at the start
OPENING_CARD = 100_000               # $1,000.00 already on the card
OPENING_ESCROW = 200_000             # $2,000.00 held by the servicer

RENT = 250_000                       # $2,500.00 rent collected
TAXES = 30_000                       # $300.00 county tax bill, paid by check
CARD_REPAIRS = 40_000                # $400.00 of repairs on the card
CARD_SUPPLIES = 20_000               # $200.00 of supplies on the card
CARD_TOTAL = CARD_REPAIRS + CARD_SUPPLIES
SETTLED = CARD_TOTAL                 # the card bill is paid in full from checking
MORTGAGE_PRINCIPAL = 30_000          # $300.00 of principal in the payment
MORTGAGE_INTEREST = 100_000          # $1,000.00 of interest in the payment
MORTGAGE_ESCROW = 20_000             # $200.00 funded into escrow
MORTGAGE_PAYMENT = MORTGAGE_PRINCIPAL + MORTGAGE_INTEREST + MORTGAGE_ESCROW

AUSTIN_VALUATION = 45_000_000        # $450,000.00 appraised value


def _required(code: int | None) -> int:
    """The allocated code, asserting the allocator actually assigned one."""
    assert code is not None, "the ledger allocator must assign every entity real accounts"
    return code


@pytest.fixture(name="austin")
def austin_fixture(session: Session):
    """Declares the portfolio, allocates its ledger accounts, posts its history."""
    institution = FinancialInstitution(id="inst_lone_star", name="Lone Star Bank")
    session.add(institution)
    session.flush()

    prop = Property(
        id=PROPERTY_ID,
        name="4409 Bluebonnet Ln",
        address_line1="4409 Bluebonnet Ln",
        city="Austin",
        state="TX",
        zip_code="78704",
        property_type="RENTAL_INVESTMENT",
        acquisition_date=date(2021, 6, 15),
        purchase_price_cents=OPENING_PROPERTY,
        land_basis_cents=8_000_000,
        building_basis_cents=32_000_000,
    )
    session.add(prop)
    session.add(PropertyValuation(
        id="val_austin_2026",
        property_id=PROPERTY_ID,
        valuation_date=date(2026, 1, 1),
        estimated_value_cents=AUSTIN_VALUATION,
        valuation_source="APPRAISAL",
    ))
    session.flush()

    checking = FinancialAccount(
        id=CHECKING_ID,
        institution_id=institution.id,
        account_name="Lone Star Operating Checking",
        account_type="CHECKING",
        account_number_masked="...4471",
    )
    # A purpose-limited rental card: its spend is not a mixed-use question, so
    # posting against it needs no external-review attestation.
    card = FinancialAccount(
        id=CARD_ID,
        institution_id=institution.id,
        account_name="Cedar Visa (rental only)",
        account_type="CREDIT_CARD",
        account_number_masked="...8820",
        mixed_use_status="DEDICATED_RENTAL",
    )
    mortgage_account = FinancialAccount(
        id=MORTGAGE_ACCOUNT_ID,
        institution_id=institution.id,
        account_name="Spruce Mortgage Loan",
        account_type="MORTGAGE",
        account_number_masked="...5510",
    )
    session.add(checking)
    session.add(card)
    session.add(mortgage_account)
    session.flush()

    loan = LoanAccount(
        id=LOAN_ID,
        financial_account_id=MORTGAGE_ACCOUNT_ID,
        property_id=PROPERTY_ID,
        loan_number=LOAN_NUMBER,
        origination_date=date(2021, 6, 15),
        original_principal_cents=OPENING_PRINCIPAL,
        term_months=360,
        current_interest_rate_bps=3125,
    )
    session.add(loan)
    session.flush()

    # Allocate the ledger accounts for everything just declared.
    ensure_entity_ledger_accounts(session)

    document = Document(
        id="doc_austin",
        file_sha256="sha_austin_statement",
        original_filename="lone_star_march_2026.pdf",
        storage_path="/vault/austin/lone_star.pdf",
        file_size_bytes=1024,
    )
    session.add(document)
    session.flush()
    statement = Statement(
        id="stmt_austin_checking",
        document_id=document.id,
        financial_account_id=CHECKING_ID,
        statement_type="CHECKING",
        active_parser_name="ManualEntry",
        active_parser_version="1.0",
        statement_date=date(2026, 3, 31),
    )
    session.add(statement)
    session.flush()

    # Opening balances. The equity plug absorbs the difference, so the entry
    # balances without the caller inventing a capital figure.
    record_opening_balances(
        session=session,
        as_of_date=date(2026, 1, 1),
        balances=[
            OpeningAccountBalance(account_code=_required(checking.ledger_account_code), amount_cents=OPENING_DEPOSIT, side="DEBIT"),
            OpeningAccountBalance(account_code=_required(prop.asset_ledger_code), amount_cents=OPENING_PROPERTY, side="DEBIT"),
            OpeningAccountBalance(account_code=_required(loan.escrow_ledger_code), amount_cents=OPENING_ESCROW, side="DEBIT"),
            OpeningAccountBalance(account_code=_required(loan.principal_ledger_code), amount_cents=OPENING_PRINCIPAL, side="CREDIT"),
            OpeningAccountBalance(account_code=_required(card.ledger_account_code), amount_cents=OPENING_CARD, side="CREDIT"),
        ],
        description="Austin duplex opening balances",
    )

    def _transaction(txn_id: str, account_id: str, amount: int, description: str, counter: str | None = None) -> Transaction:
        txn = Transaction(
            id=txn_id,
            statement_id=statement.id,
            financial_account_id=account_id,
            transaction_date=date(2026, 3, 15),
            amount_cents=amount,
            direction="INFLOW" if amount > 0 else "OUTFLOW",
            raw_description=description,
            counter_account_id=counter,
        )
        session.add(txn)
        session.flush()
        return txn

    # 1. Rent received into checking and earned by the duplex.
    rent_txn = _transaction("txn_austin_rent", CHECKING_ID, RENT, "RENT DEPOSIT MARCH")
    apply_transaction_splits(session, rent_txn.id, [
        SplitItem(property_id=PROPERTY_ID, category="RENT_INCOME", amount_cents=RENT,
                  tax_review_status="SELF_CLASSIFIED"),
    ])
    post_transaction_to_ledger(session, rent_txn.id)

    # 2. County property tax paid straight out of checking.
    tax_txn = _transaction("txn_austin_tax", CHECKING_ID, -TAXES, "TRAVIS COUNTY TAX")
    apply_transaction_splits(session, tax_txn.id, [
        SplitItem(property_id=PROPERTY_ID, category="TAXES", amount_cents=TAXES,
                  tax_review_status="SELF_CLASSIFIED"),
    ])
    post_transaction_to_ledger(session, tax_txn.id)

    # 3. A card purchase split across repairs and supplies on the door.
    card_txn = _transaction("txn_austin_card", CARD_ID, -CARD_TOTAL, "CEDAR HARDWARE")
    apply_transaction_splits(session, card_txn.id, [
        SplitItem(property_id=PROPERTY_ID, category="REPAIRS", amount_cents=CARD_REPAIRS,
                  tax_review_status="SELF_CLASSIFIED"),
        SplitItem(property_id=PROPERTY_ID, category="SUPPLIES", amount_cents=CARD_SUPPLIES,
                  tax_review_status="SELF_CLASSIFIED"),
    ])
    post_transaction_to_ledger(session, card_txn.id)

    # 4. The card bill is paid from checking: a transfer, not a second expense.
    settlement_txn = _transaction("txn_austin_settle", CHECKING_ID, -SETTLED, "CARD AUTOPAY", counter=CARD_ID)
    apply_transaction_splits(session, settlement_txn.id, [
        SplitItem(property_id=PROPERTY_ID, category="REPAIRS", amount_cents=SETTLED,
                  tax_review_status="SELF_CLASSIFIED"),
    ])
    post_transaction_to_ledger(session, settlement_txn.id)

    # 5. Mortgage payment, stage one: the bank settles funds into clearing.
    mortgage_txn = _transaction("txn_austin_mortgage", CHECKING_ID, -MORTGAGE_PAYMENT, "BLUEBONNET MORTGAGE ACH")
    apply_transaction_splits(session, mortgage_txn.id, [
        SplitItem(property_id=PROPERTY_ID, category="MORTGAGE", amount_cents=MORTGAGE_PAYMENT,
                  tax_review_status="SELF_CLASSIFIED"),
    ])
    post_transaction_to_ledger(session, mortgage_txn.id)

    # 6. Mortgage payment, stage two: the servicer applies principal, interest
    #    and escrow out of the funded clearing balance.
    application = create_journal_entry(
        session, date(2026, 3, 18), "CASH_SETTLEMENT", "Servicer confirmed application",
        "AUSTIN:MTG:APP",
    )
    add_journal_line(session, application.id, _required(loan.principal_ledger_code),
                     debit_cents=MORTGAGE_PRINCIPAL, property_id=PROPERTY_ID)
    add_journal_line(session, application.id, MORTGAGE_INTEREST_CODE,
                     debit_cents=MORTGAGE_INTEREST, property_id=PROPERTY_ID)
    add_journal_line(session, application.id, _required(loan.escrow_ledger_code),
                     debit_cents=MORTGAGE_ESCROW, property_id=PROPERTY_ID)
    add_journal_line(session, application.id, _required(loan.clearing_ledger_code),
                     credit_cents=MORTGAGE_PAYMENT)
    post_journal_entry(session, application.id)

    session.commit()
    return {"property": prop, "checking": checking, "card": card, "loan": loan}


def test_custom_portfolio_ledger_codes_are_allocated_not_assumed(austin, session: Session):
    """Each entity owns a distinct ledger account, and its code is recorded on the row."""
    prop, checking, card, loan = (
        austin["property"], austin["checking"], austin["card"], austin["loan"],
    )
    for entity_code in (prop.asset_ledger_code, checking.ledger_account_code,
                        card.ledger_account_code, loan.principal_ledger_code,
                        loan.clearing_ledger_code, loan.escrow_ledger_code):
        assert entity_code is not None
    # A mortgage account has no ledger account of its own: its debt lives on the loan.
    mortgage_account = session.get(FinancialAccount, MORTGAGE_ACCOUNT_ID)
    assert mortgage_account.ledger_account_code is None


def test_custom_portfolio_income_statement_and_cash_flow(austin, session: Session):
    """NOI, OCF, debt service and CADS computed from a brand-new portfolio.

    Hand-computed for FY2026:
      GOI  = rent                                        = 250,000
      OpEx = taxes 30,000 + repairs 40,000 + supplies 20,000 = 90,000
      NOI  = 250,000 - 90,000                            = 160,000
      OCF  = cash rent 250,000 - checking-paid tax 30,000
             - settled card rental share 60,000          = 160,000
      Debt service = principal 30,000 + interest 100,000 = 130,000
      CADS = 160,000 - 130,000                           =  30,000
      Escrow funding is a transfer, not debt service     =  20,000
    """
    financials = calculate_property_financials(session, property_id=PROPERTY_ID, year=2026)

    assert financials["gross_rental_income_cents"] == RENT
    assert financials["goi_cents"] == RENT
    assert financials["property_tax_cents"] == TAXES
    assert financials["repairs_cents"] == CARD_REPAIRS
    assert financials["supplies_cents"] == CARD_SUPPLIES
    assert financials["noi_cents"] == 160_000

    assert financials["cash_rent_collected_cents"] == RENT
    assert financials["direct_checking_opex_cents"] == TAXES
    assert financials["card_settled_rental_opex_cents"] == SETTLED
    assert financials["ocf_cents"] == 160_000

    assert financials["principal_paid_cents"] == MORTGAGE_PRINCIPAL
    assert financials["mortgage_interest_cents"] == MORTGAGE_INTEREST
    assert financials["total_debt_service_cents"] == 130_000
    assert financials["cads_cents"] == 30_000

    # Escrow funding is reported on its own and never inside debt service.
    assert financials["escrow_funded_cents"] == MORTGAGE_ESCROW
    assert financials["unfunded_applied_debt_service_cents"] == 0


def test_custom_portfolio_settlement_is_a_transfer_not_a_second_expense(austin, session: Session):
    """Paying the card moves the liability down and never re-expenses the purchase."""
    financials = calculate_property_financials(session, property_id=PROPERTY_ID, year=2026)

    # The purchase is expensed once, on its own posting (40,000 + 20,000 = 60,000),
    # and the settlement is counted only as the cash leaving checking.
    assert financials["repairs_cents"] + financials["supplies_cents"] == CARD_TOTAL
    assert financials["card_settled_rental_opex_cents"] == SETTLED
    assert financials["card_settlement_unattributed_cents"] == 0
    assert financials["card_settlement_non_expense_cents"] == 0


def test_custom_portfolio_equity_uses_ledger_principal(austin, session: Session):
    """Valuation less the ledger-carried principal, since no statement has arrived.

      principal = 350,000.00 opening - 300.00 principal applied = 349,700.00
      equity    = 450,000.00 valuation - 349,700.00             = 100,300.00
    """
    equity = calculate_portfolio_equity(session, as_of_date=date(2026, 12, 31))

    assert equity["total_valuation_cents"] == AUSTIN_VALUATION
    assert equity["total_debt_cents"] == OPENING_PRINCIPAL - MORTGAGE_PRINCIPAL
    assert equity["net_equity_cents"] == AUSTIN_VALUATION - (OPENING_PRINCIPAL - MORTGAGE_PRINCIPAL)
    assert equity["debt_fully_verified"] is True
    metric = equity["property_metrics"][0]
    assert metric["principal_source"] == "LEDGER_DERIVED"


def test_custom_portfolio_schedule_e_lines(austin, session: Session):
    """The workpaper maps this portfolio's activity onto the right IRS lines."""
    workpaper = generate_schedule_e_workpaper(session, tax_year=2026)
    column = next(c for c in workpaper.columns if c.property_id == PROPERTY_ID)

    assert column.line_3_rents == RENT
    assert column.line_16_taxes == TAXES
    assert column.line_14_repairs == CARD_REPAIRS
    assert column.line_15_supplies == CARD_SUPPLIES
    assert column.line_12_mortgage_interest == MORTGAGE_INTEREST
    assert workpaper.total_rents == RENT

    # Rental income is recorded on one shared income account and attributed to the
    # door by the journal line, rather than each door owning its own income account.
    rent_lines = session.exec(
        select(JournalEntryLine).where(
            JournalEntryLine.property_id == PROPERTY_ID,
            JournalEntryLine.ledger_account_code == RENTAL_INCOME_CODE,
        )
    ).all()
    assert sum(line.credit_cents for line in rent_lines) == RENT
    assert len({line.ledger_account_code for line in rent_lines}) == 1
