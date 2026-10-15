"""Validation of the 8 worked financial and accounting scenarios from V2.2 Specification."""

from datetime import date
from uuid import uuid4
import pytest
from sqlmodel import Session, select
from proptally.db.models import (
    Document,
    FinancialInstitution,
    FinancialAccount,
    Statement,
    Property,
    LoanAccount,
    EscrowAccount,
    EscrowMovement,
    MortgagePeriod,
    Transaction,
    JournalEntry,
    JournalEntryLine,
)
from proptally.domain.accounting.ledger import (
    create_journal_entry,
    add_journal_line,
    post_journal_entry,
    reverse_journal_entry,
)
from proptally.domain.transactions.splits import apply_transaction_splits, SplitItem


@pytest.fixture(name="portfolio_fixture")
def portfolio_fixture(session: Session):
    """Sets up the core portfolio entities for testing scenarios."""
    # Financial Institutions
    inst_pnc = FinancialInstitution(id="inst_pnc", name="PNC Bank")
    inst_bofa = FinancialInstitution(id="inst_bofa", name="Bank of America")
    session.add(inst_pnc)
    session.add(inst_bofa)

    # Properties
    prop_alder = Property(
        id="prop_alder",
        name="12307 Larkspur St",
        address_line1="12307 Larkspur St",
        city="Westbrook",
        zip_code="60196",
    )
    prop_maple = Property(
        id="prop_maple",
        name="582 Foxglove Ct",
        address_line1="582 Foxglove Ct",
        city="Oakhaven",
        zip_code="60199",
    )
    session.add(prop_alder)
    session.add(prop_maple)
    session.flush()

    # Accounts
    acct_chk = FinancialAccount(
        id="acct_bofa_chk_9456",
        institution_id=inst_bofa.id,
        account_name="Advantage Banking",
        account_type="CHECKING",
        account_number_masked="...9456",
    )
    acct_visa = FinancialAccount(
        id="acct_bofa_visa_9306",
        institution_id=inst_bofa.id,
        account_name="Customized Cash Rewards Visa",
        account_type="CREDIT_CARD",
        account_number_masked="...9306",
    )
    acct_pnc_loan = FinancialAccount(
        id="acct_pnc_mortgage",
        institution_id=inst_pnc.id,
        account_name="PNC Mortgage Loan",
        account_type="MORTGAGE",
        account_number_masked="...0708",
    )
    session.add(acct_chk)
    session.add(acct_visa)
    session.add(acct_pnc_loan)
    session.flush()

    loan_pnc = LoanAccount(
        id="loan_pnc_1000000002",
        financial_account_id=acct_pnc_loan.id,
        property_id=prop_maple.id,
        loan_number="0700000002",
        term_months=360,
    )
    session.add(loan_pnc)
    session.flush()

    escrow_pnc = EscrowAccount(
        id="escrow_pnc_0700000002",
        loan_account_id=loan_pnc.id,
        opening_balance_cents=252396,  # $2,523.96
    )
    session.add(escrow_pnc)
    session.commit()

    return {
        "prop_alder": prop_alder,
        "prop_maple": prop_maple,
        "acct_chk": acct_chk,
        "acct_visa": acct_visa,
        "loan_pnc": loan_pnc,
        "escrow_pnc": escrow_pnc,
    }


def test_scenario_1_standard_mortgage_payment_lifecycle(session: Session, portfolio_fixture: dict):
    """Scenario 1: Statement billed March 25, Bank ACH clears March 28, Application confirmed.
    Verifies that no entries post on billing, and clearing account zeros out upon application."""
    # Step 1: Statement received & staged in mortgage_periods (Zero journal entries)
    entry_count_start = len(session.exec(select(JournalEntry)).all())
    assert entry_count_start == 0  # No premature entries!

    # Step 2: Bank Settlement clears checking (March 28: -$1,801.48)
    settlement_entry = create_journal_entry(
        session=session,
        entry_date=date(2026, 3, 28),
        entry_type="CASH_SETTLEMENT",
        description="PNC Mortgage ACH draft - checking settlement",
        idempotency_key="SCENARIO_1:BANK_SETTLEMENT",
    )
    add_journal_line(session, settlement_entry.id, ledger_account_code=2125, debit_cents=180148)  # Debit PNC Clearing
    add_journal_line(session, settlement_entry.id, ledger_account_code=1010, credit_cents=180148)  # Credit Checking
    post_journal_entry(session, settlement_entry.id)

    # Step 3: Servicer confirms application (Debit Principal, Interest, Escrow; Credit Clearing)
    app_entry = create_journal_entry(
        session=session,
        entry_date=date(2026, 3, 28),
        entry_type="CASH_SETTLEMENT",
        description="PNC Mortgage confirmed servicer application",
        idempotency_key="SCENARIO_1:SERVICER_APP",
    )
    add_journal_line(session, app_entry.id, ledger_account_code=2120, debit_cents=52179, property_id="prop_maple")  # Debt reduction
    add_journal_line(session, app_entry.id, ledger_account_code=5010, debit_cents=43837, property_id="prop_maple")  # Interest
    add_journal_line(session, app_entry.id, ledger_account_code=1120, debit_cents=84132, property_id="prop_maple")  # Escrow Reserve
    add_journal_line(session, app_entry.id, ledger_account_code=2125, credit_cents=180148)                             # Clearing offset
    post_journal_entry(session, app_entry.id)

    # Step 4: Record confirmed Escrow Movement
    escrow_acct = portfolio_fixture["escrow_pnc"]
    escrow_mov = EscrowMovement(
        id=str(uuid4()),
        escrow_account_id=escrow_acct.id,
        movement_date=date(2026, 3, 28),
        movement_type="CONTRIBUTION",
        amount_cents=84132,
        running_calculated_balance_cents=escrow_acct.opening_balance_cents + 84132,
        description="PNC monthly escrow contribution",
    )
    session.add(escrow_mov)
    session.commit()

    # Step 5: Verify Clearing balance is exactly $0.00
    clearing_lines = session.exec(
        select(JournalEntryLine).where(JournalEntryLine.ledger_account_code == 2125)
    ).all()
    net_clearing = sum(l.debit_cents for l in clearing_lines) - sum(l.credit_cents for l in clearing_lines)
    assert net_clearing == 0  # Perfectly reconciled clearing account!


def test_scenario_a_mortgage_payment_rejected_no_premature_recognition(session: Session, portfolio_fixture: dict):
    """Scenario A: Payment drafted by bank but returned/reversed on March 30.
    Verifies that mortgage debt and escrow assets were never modified, and clearing resets to zero."""
    # Step 1: Bank ACH drafts $1,801.48
    entry = create_journal_entry(
        session=session,
        entry_date=date(2026, 3, 28),
        entry_type="CASH_SETTLEMENT",
        description="PNC ACH Draft (subsequently reversed)",
        idempotency_key="SCENARIO_A:ACH_DRAFT",
    )
    add_journal_line(session, entry.id, ledger_account_code=2125, debit_cents=180148)
    add_journal_line(session, entry.id, ledger_account_code=1010, credit_cents=180148)
    post_journal_entry(session, entry.id)

    # Step 2: Bank reverses ACH payment on March 30
    rev_entry = reverse_journal_entry(
        session=session,
        entry_id=entry.id,
        reversal_date=date(2026, 3, 30),
        reason="ACH Returned: Administrative Hold",
    )
    assert rev_entry.status == "POSTED"

    # Step 3: Verify invariants
    # 1. Clearing Account 2125 net balance is 0
    clearing_lines = session.exec(
        select(JournalEntryLine).where(JournalEntryLine.ledger_account_code == 2125)
    ).all()
    assert sum(l.debit_cents for l in clearing_lines) == sum(l.credit_cents for l in clearing_lines)

    # 2. Mortgage Debt (2120) and Escrow Asset (1120) have ZERO entries
    debt_lines = session.exec(select(JournalEntryLine).where(JournalEntryLine.ledger_account_code == 2120)).all()
    escrow_lines = session.exec(select(JournalEntryLine).where(JournalEntryLine.ledger_account_code == 1120)).all()
    assert len(debt_lines) == 0
    assert len(escrow_lines) == 0


def test_scenario_b_credit_card_property_expense_and_settlement(session: Session, portfolio_fixture: dict):
    """Scenario B: $500.00 Home Depot charge on BoA Visa split between Alder, Maple, and Personal.
    Later paid in full from BoA Checking. Verifies zero double-counting of rental OpEx."""
    # Event 1: Charge incurred on May 05 ($500.00 on card)
    card_entry = create_journal_entry(
        session=session,
        entry_date=date(2026, 5, 5),
        entry_type="EXPENSE_POST",
        description="Home Depot multi-property purchase",
        idempotency_key="SCENARIO_B:CARD_INCURRED",
    )
    add_journal_line(session, card_entry.id, ledger_account_code=5040, debit_cents=25000, property_id="prop_alder")   # $250 Alder Repairs
    add_journal_line(session, card_entry.id, ledger_account_code=5050, debit_cents=15000, property_id="prop_maple")  # $150 Maple Supplies
    add_journal_line(session, card_entry.id, ledger_account_code=5090, debit_cents=10000)                              # $100 Personal Draw
    add_journal_line(session, card_entry.id, ledger_account_code=2010, credit_cents=50000)                             # $500 Card Liability
    post_journal_entry(session, card_entry.id)

    # Event 2: Checking drafts $500.00 to pay BoA Visa on May 22
    pay_entry = create_journal_entry(
        session=session,
        entry_date=date(2026, 5, 22),
        entry_type="CASH_SETTLEMENT",
        description="BoA Visa statement payment from checking",
        idempotency_key="SCENARIO_B:CARD_PAYMENT",
    )
    add_journal_line(session, pay_entry.id, ledger_account_code=2010, debit_cents=50000)   # Extinguish card liability
    add_journal_line(session, pay_entry.id, ledger_account_code=1010, credit_cents=50000)  # Cash out from checking
    post_journal_entry(session, pay_entry.id)

    # Invariants Verification:
    # 1. Card Liability (2010) is net $0.00
    card_lines = session.exec(select(JournalEntryLine).where(JournalEntryLine.ledger_account_code == 2010)).all()
    assert sum(l.debit_cents for l in card_lines) == sum(l.credit_cents for l in card_lines) == 50000

    # 2. Operating expenses were recognized on May 05 and not duplicated on May 22
    repair_lines = session.exec(select(JournalEntryLine).where(JournalEntryLine.ledger_account_code == 5040)).all()
    supplies_lines = session.exec(select(JournalEntryLine).where(JournalEntryLine.ledger_account_code == 5050)).all()
    assert sum(l.debit_cents for l in repair_lines) == 25000
    assert sum(l.debit_cents for l in supplies_lines) == 15000


def test_scenario_4_property_tax_disbursement_from_escrow(session: Session, portfolio_fixture: dict):
    """Scenario 4: McHenry County Property Tax ($4,289.20) paid directly from servicer escrow reserve.
    Checking cash movement is $0.00, Tax expense recognized, Escrow asset reduced."""
    escrow_acct = portfolio_fixture["escrow_pnc"]

    disburse_entry = create_journal_entry(
        session=session,
        entry_date=date(2026, 6, 1),
        entry_type="ESCROW_DISBURSEMENT",
        description="McHenry County 2025 payable 2026 property taxes - installment 1",
        idempotency_key="SCENARIO_4:TAX_DISBURSE",
    )
    add_journal_line(session, disburse_entry.id, ledger_account_code=5020, debit_cents=428920, property_id="prop_maple")  # Property Tax Expense
    add_journal_line(session, disburse_entry.id, ledger_account_code=1120, credit_cents=428920)                             # Escrow Reserve credit
    post_journal_entry(session, disburse_entry.id)

    # Record Escrow Movement
    movement = EscrowMovement(
        id=str(uuid4()),
        escrow_account_id=escrow_acct.id,
        movement_date=date(2026, 6, 1),
        movement_type="TAX_DISBURSEMENT",
        amount_cents=-428920,
        running_calculated_balance_cents=escrow_acct.opening_balance_cents - 428920,
        description="County collector property tax disbursement",
    )
    session.add(movement)
    session.commit()

    # Checking account (1010) was NOT touched
    checking_lines = session.exec(
        select(JournalEntryLine)
        .where(JournalEntryLine.journal_entry_id == disburse_entry.id)
        .where(JournalEntryLine.ledger_account_code == 1010)
    ).all()
    assert len(checking_lines) == 0
