"""A synthetic portfolio, for tests only.

This is test fixture data, not product code: it builds one populated portfolio (three
doors, three mortgages, cards, opening balances, a rent deposit, a split card purchase
and two review-queue items) so that tests about rendering, metrics and posting run
against realistic content.

The application itself ships no sample portfolio. A new tenant starts empty and is
expected to be built up by its owner, so nothing in ``src/`` imports this module.
"""

from datetime import date
from uuid import uuid4

from sqlmodel import Session

from proptally.db.models import (
    Document,
    EscrowAccount,
    EscrowMovement,
    FinancialAccount,
    FinancialInstitution,
    Lease,
    LoanAccount,
    Property,
    PropertyValuation,
    Statement,
    Transaction,
    TransactionProcessingState,
)
from proptally.domain.accounting.coa import (
    MORTGAGE_INTEREST_CODE,
    PERSONAL_DRAW_CODE,
    RENTAL_INCOME_CODE,
    REPAIRS_CODE,
    SUPPLIES_CODE,
    ensure_entity_ledger_accounts,
)
from proptally.domain.accounting.ledger import add_journal_line, create_journal_entry, post_journal_entry
from proptally.domain.accounting.opening_balances import OpeningAccountBalance, record_opening_balances
from proptally.domain.transactions.splits import SplitItem, apply_transaction_splits
from proptally.domain.transactions.suggest import seed_vendor_rules

#: Vendor rules that belong to this reference portfolio rather than to the platform:
#: they name one owner's tenant and attribute a municipal utility or county tax bill to
#: one specific door. A real portfolio is seeded with the generic merchant rules only
#: (``suggest.DEFAULT_VENDOR_RULES``); an owner configures their equivalents through the
#: vendor-rule editor.
REFERENCE_VENDOR_RULES = [
    {
        "id": "vrule_tenant_saxsma",
        "pattern": "SAXSMA",
        "vendor_name": "Tyler Saxsma (Tenant)",
        "service_type": "Residential Tenant Rent Payment -> 12307 Larkspur St",
        "default_category": "RENT_INCOME",
        "default_property_id": "prop_tinsley_huntley",
        "confidence": 0.99,
        "requires_client_confirmation": False,
        "requires_cpa_review": False,
    },
    {
        "id": "vrule_carpentersville_atm",
        "pattern": "DEPOSIT CARPENTERSVILLE",
        "vendor_name": "Bank of America Millbrook ATM",
        "service_type": "Branch ATM Deposit (CBD-1 Candidate for 2718 Foxglove Ct)",
        "default_category": "RENT_INCOME",
        "default_property_id": "prop_cimmaron_pingree",
        "confidence": 0.75,
        "requires_client_confirmation": True,
        "requires_cpa_review": False,
    },
    {
        "id": "vrule_fairview_util",
        "pattern": "FAIRVIEW*UTILITY",
        "vendor_name": "Village of Westbrook Municipal Utility",
        "service_type": "Municipal Water & Sewer Utility -> 12307 Larkspur St",
        "default_category": "UTILITIES",
        "default_property_id": "prop_tinsley_huntley",
        "confidence": 0.95,
        "requires_client_confirmation": False,
        "requires_cpa_review": False,
    },
    {
        "id": "vrule_riverton_util",
        "pattern": "VILLAGE OF RIVERTON",
        "vendor_name": "Village of Cedar Grove Water & Sewer",
        "service_type": "Municipal Water & Sewer Utility (Primary Residence)",
        "default_category": "PERSONAL",
        "default_property_id": "prop_burkey_algonquin",
        "confidence": 0.95,
        "requires_client_confirmation": False,
        "requires_cpa_review": False,
    },
    {
        "id": "vrule_mchenry_tax",
        "pattern": "MC HENRY COUN",
        "vendor_name": "McHenry County Treasurer",
        "service_type": "County Real Estate Property Taxes (Schedule E Line 16)",
        "default_category": "TAXES",
        "default_property_id": "prop_tinsley_huntley",
        "confidence": 0.95,
        "requires_client_confirmation": False,
        "requires_cpa_review": False,
    },
]


def seed_reference_portfolio(session: Session) -> None:
    """Populates the reference portfolio: full entities, balances and review items."""
    # 1. Seed the fixed standard Chart of Accounts and this portfolio's own
    #    vendor rules (the generic merchant rules are seeded separately, by the
    #    schema migration that creates the table).
    ensure_entity_ledger_accounts(session)
    seed_vendor_rules(session, REFERENCE_VENDOR_RULES)

    # Check if already seeded
    existing_prop = session.get(Property, "prop_tinsley_huntley")
    if existing_prop:
        return

    # 2. Properties
    larkspur = Property(
        id="prop_tinsley_huntley",
        name="12307 Larkspur St",
        address_line1="12307 Larkspur St",
        city="Westbrook",
        state="IL",
        zip_code="60196",
        property_type="RENTAL_INVESTMENT",
        acquisition_date=date(2021, 6, 15),
        purchase_price_cents=42500000,
        land_basis_cents=8500000,
        building_basis_cents=34000000,
    )
    foxglove = Property(
        id="prop_cimmaron_pingree",
        name="2718 Foxglove Ct",
        address_line1="2718 Foxglove Ct",
        city="Oakhaven",
        state="IL",
        zip_code="60199",
        property_type="RENTAL_INVESTMENT",
        acquisition_date=date(2020, 11, 20),
        purchase_price_cents=36000000,
        land_basis_cents=7200000,
        building_basis_cents=28800000,
    )
    elderberry = Property(
        id="prop_burkey_algonquin",
        name="3345 Elderberry Dr",
        address_line1="3345 Elderberry Dr",
        city="Cedar Grove",
        state="IL",
        zip_code="60198",
        property_type="PRIMARY_RESIDENCE",
        acquisition_date=date(2022, 4, 10),
        purchase_price_cents=65000000,
        land_basis_cents=13000000,
        building_basis_cents=52000000,
    )
    session.add(larkspur)
    session.add(foxglove)
    session.add(elderberry)
    session.flush()

    # Valuations
    val_alder = PropertyValuation(
        id=str(uuid4()),
        property_id=larkspur.id,
        valuation_date=date(2026, 1, 1),
        estimated_value_cents=46500000,
        valuation_source="CLIENT_MANUAL",
        notes="2026 Q1 Estimated Fair Market Value",
    )
    val_maple = PropertyValuation(
        id=str(uuid4()),
        property_id=foxglove.id,
        valuation_date=date(2026, 1, 1),
        estimated_value_cents=39500000,
        valuation_source="CLIENT_MANUAL",
        notes="2026 Q1 Estimated Fair Market Value",
    )
    val_rowan = PropertyValuation(
        id=str(uuid4()),
        property_id=elderberry.id,
        valuation_date=date(2026, 1, 1),
        estimated_value_cents=71000000,
        valuation_source="CLIENT_MANUAL",
        notes="2026 Q1 Primary Residence Value",
    )
    session.add(val_alder)
    session.add(val_maple)
    session.add(val_rowan)

    # Leases
    lease_alder = Lease(
        id=str(uuid4()),
        property_id=larkspur.id,
        tenant_name="Riley Alvarez",
        start_date=date(2025, 8, 1),
        end_date=date(2026, 7, 31),
        monthly_rent_cents=260000,
        security_deposit_cents=260000,
        is_active=True,
    )
    lease_maple = Lease(
        id=str(uuid4()),
        property_id=foxglove.id,
        tenant_name="Maple Tenant",
        start_date=date(2025, 6, 1),
        end_date=date(2026, 5, 31),
        monthly_rent_cents=210000,
        security_deposit_cents=210000,
        is_active=True,
    )
    session.add(lease_alder)
    session.add(lease_maple)
    session.flush()

    # 3. Financial Institutions
    bofa = FinancialInstitution(id="inst_bofa", name="Bank of America")
    rocket = FinancialInstitution(id="inst_rocket", name="Rocket Mortgage")
    pnc = FinancialInstitution(id="inst_pnc", name="PNC Bank")
    ccm = FinancialInstitution(id="inst_ccm", name="CrossCountry Mortgage")
    apple = FinancialInstitution(id="inst_apple", name="Apple / Goldman Sachs")
    session.add(bofa)
    session.add(rocket)
    session.add(pnc)
    session.add(ccm)
    session.add(apple)
    session.flush()

    # 4. Financial Accounts
    chk_bofa = FinancialAccount(
        id="acct_bofa_chk_9456",
        institution_id=bofa.id,
        account_name="BoA Core Checking",
        account_type="CHECKING",
        account_number_masked="...9456",
    )
    visa_bofa = FinancialAccount(
        id="acct_bofa_visa_9306",
        institution_id=bofa.id,
        account_name="BoA Customized Cash Rewards Visa",
        account_type="CREDIT_CARD",
        account_number_masked="...9306",
        mixed_use_status="MIXED_USE",
    )
    apple_card = FinancialAccount(
        id="acct_apple_card",
        institution_id=apple.id,
        account_name="Apple Card Mastercard",
        account_type="CREDIT_CARD",
        account_number_masked="...4321",
        mixed_use_status="MIXED_USE",
    )
    loan_acct_rocket = FinancialAccount(
        id="acct_rocket_mortgage",
        institution_id=rocket.id,
        account_name="Rocket Mortgage Loan",
        account_type="MORTGAGE",
        account_number_masked="...5035",
    )
    loan_acct_pnc = FinancialAccount(
        id="acct_pnc_mortgage",
        institution_id=pnc.id,
        account_name="PNC Mortgage Loan",
        account_type="MORTGAGE",
        account_number_masked="...0708",
    )
    loan_acct_ccm = FinancialAccount(
        id="acct_ccm_mortgage",
        institution_id=ccm.id,
        account_name="CrossCountry Mortgage Loan",
        account_type="MORTGAGE",
        account_number_masked="...9359",
    )
    session.add(chk_bofa)
    session.add(visa_bofa)
    session.add(apple_card)
    session.add(loan_acct_rocket)
    session.add(loan_acct_pnc)
    session.add(loan_acct_ccm)
    session.flush()

    # Loans
    loan_rocket = LoanAccount(
        id="loan_rocket_1000000001",
        financial_account_id=loan_acct_rocket.id,
        property_id=larkspur.id,
        loan_number="0700000001",
        origination_date=date(2021, 6, 15),
        original_principal_cents=34000000,
        current_interest_rate_bps=3125,
        term_months=360,
    )
    loan_pnc = LoanAccount(
        id="loan_pnc_1000000002",
        financial_account_id=loan_acct_pnc.id,
        property_id=foxglove.id,
        loan_number="0700000002",
        origination_date=date(2020, 11, 20),
        original_principal_cents=28800000,
        current_interest_rate_bps=2500,
        term_months=360,
    )
    loan_ccm = LoanAccount(
        id="loan_ccm_1000000003",
        financial_account_id=loan_acct_ccm.id,
        property_id=elderberry.id,
        loan_number="0700000003",
        origination_date=date(2022, 4, 10),
        original_principal_cents=52000000,
        current_interest_rate_bps=6250,
        term_months=360,
    )
    session.add(loan_rocket)
    session.add(loan_pnc)
    session.add(loan_ccm)
    session.flush()

    # Escrow Accounts
    escrow_rocket = EscrowAccount(
        id="escrow_rocket_0700000001",
        loan_account_id=loan_rocket.id,
        opening_balance_cents=284000,
        projected_annual_tax_cents=560000,
        projected_annual_insurance_cents=120000,
    )
    escrow_pnc = EscrowAccount(
        id="escrow_pnc_0700000002",
        loan_account_id=loan_pnc.id,
        opening_balance_cents=252396,
        projected_annual_tax_cents=857840,
        projected_annual_insurance_cents=151744,
    )
    session.add(escrow_rocket)
    session.add(escrow_pnc)
    session.flush()

    # 5. Per-entity ledger accounts.
    # Each property, deposit account, card and loan owns its own ledger account;
    # the codes are allocated here, before any journal line references them, so the
    # seeder never needs to know a portfolio's account numbers.
    ensure_entity_ledger_accounts(session)

    # 6. Opening Balance Baseline (Jan 01, 2026)
    opening_balances = [
        # Checking Cash
        OpeningAccountBalance(account_code=chk_bofa.ledger_account_code, amount_cents=1850000, side="DEBIT", description="BoA Checking Jan 1 balance"),
        # Escrow Reserves
        OpeningAccountBalance(account_code=loan_rocket.escrow_ledger_code, amount_cents=284000, side="DEBIT", description="Rocket Escrow opening"),
        OpeningAccountBalance(account_code=loan_pnc.escrow_ledger_code, amount_cents=252396, side="DEBIT", description="PNC Escrow opening"),
        # Properties
        OpeningAccountBalance(account_code=larkspur.asset_ledger_code, amount_cents=42500000, side="DEBIT", description="Alder Cost Basis"),
        OpeningAccountBalance(account_code=foxglove.asset_ledger_code, amount_cents=36000000, side="DEBIT", description="Maple Cost Basis"),
        OpeningAccountBalance(account_code=elderberry.asset_ledger_code, amount_cents=65000000, side="DEBIT", description="Rowan Cost Basis"),
        # Mortgage Liabilities
        OpeningAccountBalance(account_code=loan_rocket.principal_ledger_code, amount_cents=31045000, side="CREDIT", description="Rocket principal balance"),
        OpeningAccountBalance(account_code=loan_pnc.principal_ledger_code, amount_cents=23892000, side="CREDIT", description="PNC principal balance"),
        OpeningAccountBalance(account_code=loan_ccm.principal_ledger_code, amount_cents=48000000, side="CREDIT", description="CrossCountry principal balance"),
        # Cards
        OpeningAccountBalance(account_code=visa_bofa.ledger_account_code, amount_cents=124000, side="CREDIT", description="BoA Visa opening balance"),
        OpeningAccountBalance(account_code=apple_card.ledger_account_code, amount_cents=86000, side="CREDIT", description="Apple Card opening balance"),
    ]
    record_opening_balances(
        session=session,
        as_of_date=date(2026, 1, 1),
        balances=opening_balances,
        description="Portfolio Opening Balance Baseline (Jan 1, 2026)",
    )

    # 6. Synthetic Document & Statements
    doc = Document(
        id=str(uuid4()),
        file_sha256="synth_doc_sha256_demo",
        original_filename="synthetic_q1_portfolio_statement.pdf",
        storage_path="/synthetic/fixtures/q1_statement.pdf",
        file_size_bytes=204800,
    )
    session.add(doc)
    session.flush()

    stmt_chk = Statement(
        id="stmt_demo_bofa_chk",
        document_id=doc.id,
        financial_account_id=chk_bofa.id,
        statement_type="CHECKING",
        active_parser_name="SyntheticCheckingParser",
        active_parser_version="1.0.0",
        statement_date=date(2026, 3, 31),
    )
    session.add(stmt_chk)
    session.flush()

    # 7. Recorded Transactions & Workflows
    # Txn 1: Zelle Rent Inflow for Alder ($2,600.00)
    txn_rent = Transaction(
        id="txn_rent_alder_202603",
        statement_id=stmt_chk.id,
        financial_account_id=chk_bofa.id,
        transaction_date=date(2026, 3, 1),
        amount_cents=260000,
        direction="INFLOW",
        raw_description="Zelle transfer from Riley Alvarez - March Rent",
    )
    session.add(txn_rent)
    session.flush()
    apply_transaction_splits(session, txn_rent.id, [
        SplitItem(property_id=larkspur.id, category="RENT_INCOME", amount_cents=260000, schedule_e_line=3)
    ])

    # Post rent journal entry
    rent_je = create_journal_entry(
        session, date(2026, 3, 1), "EXPENSE_POST", "Rent received: 12307 Larkspur St",
        "TXN:RENT:LARKSPUR:202603", source_transaction_id=txn_rent.id,
    )
    add_journal_line(session, rent_je.id, chk_bofa.ledger_account_code, debit_cents=260000)
    add_journal_line(session, rent_je.id, RENTAL_INCOME_CODE, credit_cents=260000, property_id=larkspur.id)
    post_journal_entry(session, rent_je.id)

    # Txn 2: BofA Checking PNC Mortgage ACH Payment (-$1,801.48)
    txn_pnc = Transaction(
        id="txn_pnc_ach_20260328",
        statement_id=stmt_chk.id,
        financial_account_id=chk_bofa.id,
        transaction_date=date(2026, 3, 28),
        amount_cents=-180148,
        direction="OUTFLOW",
        raw_description="PNC MORTGAGE ACH DEBIT 0700000002",
    )
    session.add(txn_pnc)
    session.flush()
    apply_transaction_splits(session, txn_pnc.id, [
        SplitItem(property_id=foxglove.id, category="MORTGAGE", amount_cents=180148)
    ])

    # Journal Entries for PNC Mortgage (Cash Settlement + Application)
    je_pnc_cash = create_journal_entry(
        session, date(2026, 3, 28), "CASH_SETTLEMENT", "PNC Mortgage Bank ACH draft",
        "TXN:PNC:CASH:20260328", source_transaction_id=txn_pnc.id,
    )
    add_journal_line(session, je_pnc_cash.id, loan_pnc.clearing_ledger_code, debit_cents=180148)
    add_journal_line(session, je_pnc_cash.id, chk_bofa.ledger_account_code, credit_cents=180148)
    post_journal_entry(session, je_pnc_cash.id)

    je_pnc_app = create_journal_entry(
        session, date(2026, 3, 28), "CASH_SETTLEMENT", "PNC Servicer confirmed application", "TXN:PNC:APP:20260328"
    )
    add_journal_line(session, je_pnc_app.id, loan_pnc.principal_ledger_code, debit_cents=52179, property_id=foxglove.id)
    add_journal_line(session, je_pnc_app.id, MORTGAGE_INTEREST_CODE, debit_cents=43837, property_id=foxglove.id)
    add_journal_line(session, je_pnc_app.id, loan_pnc.escrow_ledger_code, debit_cents=84132, property_id=foxglove.id)
    add_journal_line(session, je_pnc_app.id, loan_pnc.clearing_ledger_code, credit_cents=180148)
    post_journal_entry(session, je_pnc_app.id)

    # Escrow Movement for PNC
    mov_pnc = EscrowMovement(
        id=str(uuid4()),
        escrow_account_id=escrow_pnc.id,
        movement_date=date(2026, 3, 28),
        movement_type="CONTRIBUTION",
        amount_cents=84132,
        running_calculated_balance_cents=escrow_pnc.opening_balance_cents + 84132,
        description="PNC March escrow contribution",
    )
    session.add(mov_pnc)

    # Txn 3: BofA Checking Rocket Mortgage ACH Payment (-$1,618.12)
    txn_rocket = Transaction(
        id="txn_rocket_ach_20260325",
        statement_id=stmt_chk.id,
        financial_account_id=chk_bofa.id,
        transaction_date=date(2026, 3, 25),
        amount_cents=-161812,
        direction="OUTFLOW",
        raw_description="ROCKET MORTGAGE ACH DEBIT 0700000001",
    )
    session.add(txn_rocket)
    session.flush()
    apply_transaction_splits(session, txn_rocket.id, [
        SplitItem(property_id=larkspur.id, category="MORTGAGE", amount_cents=161812)
    ])

    je_rock_cash = create_journal_entry(
        session, date(2026, 3, 25), "CASH_SETTLEMENT", "Rocket Mortgage Bank ACH draft",
        "TXN:ROCKET:CASH:20260325", source_transaction_id=txn_rocket.id,
    )
    add_journal_line(session, je_rock_cash.id, loan_rocket.clearing_ledger_code, debit_cents=161812)
    add_journal_line(session, je_rock_cash.id, chk_bofa.ledger_account_code, credit_cents=161812)
    post_journal_entry(session, je_rock_cash.id)

    je_rock_app = create_journal_entry(
        session, date(2026, 3, 25), "CASH_SETTLEMENT", "Rocket Servicer confirmed application", "TXN:ROCKET:APP:20260325"
    )
    add_journal_line(session, je_rock_app.id, loan_rocket.principal_ledger_code, debit_cents=62000, property_id=larkspur.id)
    add_journal_line(session, je_rock_app.id, MORTGAGE_INTEREST_CODE, debit_cents=66012, property_id=larkspur.id)
    add_journal_line(session, je_rock_app.id, loan_rocket.escrow_ledger_code, debit_cents=33800, property_id=larkspur.id)
    add_journal_line(session, je_rock_app.id, loan_rocket.clearing_ledger_code, credit_cents=161812)
    post_journal_entry(session, je_rock_app.id)

    mov_rocket = EscrowMovement(
        id=str(uuid4()),
        escrow_account_id=escrow_rocket.id,
        movement_date=date(2026, 3, 25),
        movement_type="CONTRIBUTION",
        amount_cents=33800,
        running_calculated_balance_cents=escrow_rocket.opening_balance_cents + 33800,
        description="Rocket March escrow contribution",
    )
    session.add(mov_rocket)

    # Txn 4: Home Depot Outflow (-$500.00) Split across Alder ($250), Maple ($150), Personal ($100)
    txn_hd = Transaction(
        id="txn_hd_split_20260312",
        statement_id=stmt_chk.id,
        financial_account_id=visa_bofa.id,
        transaction_date=date(2026, 3, 12),
        amount_cents=-50000,
        direction="OUTFLOW",
        raw_description="HOME DEPOT #1920 RIVERTON IL",
    )
    session.add(txn_hd)
    session.flush()
    apply_transaction_splits(session, txn_hd.id, [
        SplitItem(property_id=larkspur.id, category="REPAIRS", amount_cents=25000, schedule_e_line=14, notes="Plumbing fixture replacement"),
        SplitItem(property_id=foxglove.id, category="SUPPLIES", amount_cents=15000, schedule_e_line=17, notes="Paint and locks"),
        SplitItem(property_id=None, category="PERSONAL", amount_cents=10000, notes="Cedar Grove home garden supplies"),
    ])

    je_hd = create_journal_entry(
        session, date(2026, 3, 12), "EXPENSE_POST", "Home Depot split purchase",
        "TXN:HD:SPLIT:20260312", source_transaction_id=txn_hd.id,
    )
    add_journal_line(session, je_hd.id, REPAIRS_CODE, debit_cents=25000, property_id=larkspur.id)
    add_journal_line(session, je_hd.id, SUPPLIES_CODE, debit_cents=15000, property_id=foxglove.id)
    add_journal_line(session, je_hd.id, PERSONAL_DRAW_CODE, debit_cents=10000)
    add_journal_line(session, je_hd.id, visa_bofa.ledger_account_code, credit_cents=50000)
    post_journal_entry(session, je_hd.id)

    # Txn 5: Menards Purchase (-$220.00) PARTIALLY ALLOCATED ($140 allocated, $80 residual)
    txn_menards = Transaction(
        id="txn_menards_partial_20260318",
        statement_id=stmt_chk.id,
        financial_account_id=chk_bofa.id,
        transaction_date=date(2026, 3, 18),
        amount_cents=-22000,
        direction="OUTFLOW",
        raw_description="MENARDS CARPENTERSVILLE IL #3021",
    )
    session.add(txn_menards)
    session.flush()
    apply_transaction_splits(session, txn_menards.id, [
        SplitItem(property_id=larkspur.id, category="REPAIRS", amount_cents=14000, schedule_e_line=14, notes="Door repair lumber")
    ], allow_partial=True)

    # Txn 6: CBD-1 Case — ATM Deposit (+$1,500.00) UNALLOCATED in Review Queue
    txn_atm = Transaction(
        id="txn_atm_deposit_cbd1_20260320",
        statement_id=stmt_chk.id,
        financial_account_id=chk_bofa.id,
        transaction_date=date(2026, 3, 20),
        amount_cents=150000,
        direction="INFLOW",
        raw_description="ATM CASH DEPOSIT #4928 FAIRVIEW IL (CBD-1 Pending Review)",
        requires_client_confirmation=True,
    )
    session.add(txn_atm)
    session.flush()
    state_atm = TransactionProcessingState(
        transaction_id=txn_atm.id,
        allocation_status="UNALLOCATED",
        allocated_cents=0,
        residual_cents=150000,
        is_reconciled=False,
    )
    session.add(state_atm)

    # Txn 7: CBD-2 Case — Apple Card Purchase (-$129.00) UNCLASSIFIED in Review Queue
    txn_apple = Transaction(
        id="txn_apple_store_cbd2_20260322",
        statement_id=stmt_chk.id,
        financial_account_id=apple_card.id,
        transaction_date=date(2026, 3, 22),
        amount_cents=-12900,
        direction="OUTFLOW",
        raw_description="APPLE.COM/BILL 800-692-7753 CA (CBD-2 Mixed-Use Review)",
        requires_cpa_review=True,
    )
    session.add(txn_apple)
    session.flush()
    state_apple = TransactionProcessingState(
        transaction_id=txn_apple.id,
        allocation_status="UNALLOCATED",
        allocated_cents=0,
        residual_cents=12900,
        is_reconciled=False,
    )
    session.add(state_apple)

    session.commit()
