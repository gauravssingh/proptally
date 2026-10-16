"""Phase 2.1 Hardening Tests validating all 10 architectural and financial invariants."""

from datetime import date
from uuid import uuid4
import pytest
from sqlmodel import Session, select
from proptally.db.models import (
    Property,
    FinancialInstitution,
    FinancialAccount,
    LoanAccount,
    EscrowAccount,
    Statement,
    Document,
    Transaction,
    TransactionSplit,
    TransactionProcessingState,
    JournalEntry,
    JournalEntryLine,
)
from proptally.domain.accounting.coa import seed_chart_of_accounts
from proptally.domain.accounting.ledger import (
    create_journal_entry,
    add_journal_line,
    post_journal_entry,
    is_entry_reversed,
)
from proptally.domain.accounting.metrics import (
    calculate_property_financials,
    calculate_portfolio_equity,
)
from proptally.domain.transactions.splits import (
    apply_transaction_splits,
    post_transaction_to_ledger,
    reclassify_and_repost_transaction,
    SplitItem,
)
from proptally.domain.fixtures.demo_seed import seed_demo_data


@pytest.fixture(name="hardened_env")
def hardened_env_fixture(session: Session):
    """Sets up fresh Chart of Accounts and basic properties/accounts."""
    seed_chart_of_accounts(session)

    bofa = FinancialInstitution(id="inst_bofa_test", name="Bank of America")
    session.add(bofa)
    # Parent must be durable before children: SQLAlchemy orders INSERTs by mapper
    # sort key (module.ClassName), so "FinancialAccount" < "FinancialInstitution"
    # would otherwise emit the child row first and violate the FK. Same pattern as demo_seed.
    session.flush()

    chk = FinancialAccount(
        id="acct_bofa_chk_9456",
        institution_id=bofa.id,
        account_name="Advantage Checking",
        account_type="CHECKING",
        account_number_masked="...9456",
    )
    visa = FinancialAccount(
        id="acct_bofa_visa_9306",
        institution_id=bofa.id,
        account_name="Customized Cash Rewards Visa",
        account_type="CREDIT_CARD",
        account_number_masked="...9306",
    )
    session.add(chk)
    session.add(visa)

    larkspur = Property(
        id="prop_tinsley_huntley",
        name="12307 Larkspur St",
        address_line1="12307 Larkspur St",
        city="Westbrook",
        zip_code="60196",
        property_type="RENTAL_INVESTMENT",
        purchase_price_cents=42500000,
    )
    foxglove = Property(
        id="prop_cimmaron_pingree",
        name="2718 Foxglove Ct",
        address_line1="2718 Foxglove Ct",
        city="Oakhaven",
        zip_code="60199",
        property_type="RENTAL_INVESTMENT",
        purchase_price_cents=36000000,
    )
    elderberry = Property(
        id="prop_burkey_algonquin",
        name="3345 Elderberry Dr",
        address_line1="3345 Elderberry Dr",
        city="Cedar Grove",
        zip_code="60198",
        property_type="PRIMARY_RESIDENCE",
        purchase_price_cents=65000000,
    )
    session.add(larkspur)
    session.add(foxglove)
    session.add(elderberry)

    doc = Document(
        id=str(uuid4()),
        file_sha256="doc_sha256_test_hardened",
        original_filename="statement.pdf",
        storage_path="/path/stmt.pdf",
        file_size_bytes=1000,
    )
    session.add(doc)
    session.flush()

    stmt = Statement(
        id="stmt_test_hardened",
        document_id=doc.id,
        financial_account_id=chk.id,
        statement_type="CHECKING",
        active_parser_name="TestParser",
        active_parser_version="1.0",
        statement_date=date(2026, 3, 31),
    )
    session.add(stmt)
    session.commit()

    return {
        "chk": chk,
        "visa": visa,
        "larkspur": larkspur,
        "foxglove": foxglove,
        "elderberry": elderberry,
        "stmt": stmt,
    }


def test_1_ocf_and_cads_with_direct_checking_expenses(session: Session, hardened_env: dict):
    """1. OCF and CADS with direct checking expenses:
    Rent received: $2,500.00 into checking. Direct checking repair: $300.00.
    Debt service: $1,200.00 ($500 principal, $400 interest, $300 escrow).
    Verifies OCF = $2,200, Debt Service = $1,200, CADS = $1,000."""
    larkspur = hardened_env["larkspur"]

    # Rent collection
    je_rent = create_journal_entry(session, date(2026, 3, 1), "REVENUE_POST", "Rent collection", "TEST1:RENT")
    add_journal_line(session, je_rent.id, 1010, debit_cents=250000)
    add_journal_line(session, je_rent.id, 4010, credit_cents=250000, property_id=larkspur.id)
    post_journal_entry(session, je_rent.id)

    # Direct checking repair expense
    je_rep = create_journal_entry(session, date(2026, 3, 10), "EXPENSE_POST", "Direct repair check", "TEST1:REPAIR")
    add_journal_line(session, je_rep.id, 5040, debit_cents=30000, property_id=larkspur.id)
    add_journal_line(session, je_rep.id, 1010, credit_cents=30000)
    post_journal_entry(session, je_rep.id)

    # Debt service settlement & application
    je_debt = create_journal_entry(session, date(2026, 3, 28), "CASH_SETTLEMENT", "Mortgage settlement", "TEST1:DEBT")
    add_journal_line(session, je_debt.id, 2110, debit_cents=50000, property_id=larkspur.id)  # Principal
    add_journal_line(session, je_debt.id, 5010, debit_cents=40000, property_id=larkspur.id)  # Interest
    add_journal_line(session, je_debt.id, 1110, debit_cents=30000, property_id=larkspur.id)  # Escrow
    add_journal_line(session, je_debt.id, 1010, credit_cents=120000)
    post_journal_entry(session, je_debt.id)

    f = calculate_property_financials(session, property_id=larkspur.id)
    assert f["goi_cents"] == 250000
    assert f["repairs_cents"] == 30000
    assert f["noi_cents"] == 220000
    assert f["cash_rent_collected_cents"] == 250000
    assert f["direct_checking_opex_cents"] == 30000
    assert f["ocf_cents"] == 220000
    # Debt service is what the bank was paid: $500 principal + $400 interest.
    # The $300 escrow contribution is a transfer into a servicing reserve and is
    # reported separately.
    assert f["escrow_funded_cents"] == 30000
    assert f["total_debt_service_cents"] == 90000
    assert f["cads_cents"] == 130000  # OCF 220,000 - debt service 90,000


def test_2_ocf_and_cads_with_mixed_use_credit_card_purchases_and_settlements(session: Session, hardened_env: dict):
    """2. Mixed-use card purchase then settlement:
    May 5: card purchase $600 ($400 rental repairs on Alder, $200 personal draw).
    May 22: checking settles the card, allocated $400 rental / $200 personal.
    OpEx is recognised once, on swipe. OCF is untouched on swipe, and only the
    allocated rental share reduces it when the bank is actually paid."""
    chk = hardened_env["chk"]
    stmt = hardened_env["stmt"]
    larkspur = hardened_env["larkspur"]

    # Rent collection: $3,000
    je_rent = create_journal_entry(session, date(2026, 5, 1), "REVENUE_POST", "Rent collection", "TEST2:RENT")
    add_journal_line(session, je_rent.id, 1010, debit_cents=300000)
    add_journal_line(session, je_rent.id, 4010, credit_cents=300000, property_id=larkspur.id)
    post_journal_entry(session, je_rent.id)

    # Event 1 (May 05): Card purchase swipe ($400 rental, $200 personal)
    je_card = create_journal_entry(session, date(2026, 5, 5), "EXPENSE_POST", "Home Depot on Visa", "TEST2:CARD_SWIPE")
    add_journal_line(session, je_card.id, 5040, debit_cents=40000, property_id=larkspur.id)
    add_journal_line(session, je_card.id, 5090, debit_cents=20000)  # Personal
    add_journal_line(session, je_card.id, 2010, credit_cents=60000)
    post_journal_entry(session, je_card.id)

    # Metrics on May 05 before settlement:
    f_swipe = calculate_property_financials(session, property_id=larkspur.id, end_date=date(2026, 5, 10))
    assert f_swipe["noi_cents"] == 260000  # $3,000 - $400 OpEx recognized on swipe
    assert f_swipe["ocf_cents"] == 300000  # Cash in checking hasn't moved yet
    assert f_swipe["card_settled_rental_opex_cents"] == 0

    # Event 2 (May 22): the bank pays the card. The allocation records that $400
    # of the $600 payment relates to a rental door.
    settle_txn = Transaction(
        id="txn_settle_test2",
        statement_id=stmt.id,
        financial_account_id=chk.id,
        transaction_date=date(2026, 5, 22),
        amount_cents=-60000,
        direction="OUTFLOW",
        raw_description="Visa payoff from checking",
    )
    session.add(settle_txn)
    session.flush()
    session.add(TransactionSplit(
        id="sp_test2_rental", transaction_id=settle_txn.id, property_id=larkspur.id,
        category="REPAIRS", amount_cents=40000, tax_review_status="CLIENT_CONFIRMED",
    ))
    session.add(TransactionSplit(
        id="sp_test2_personal", transaction_id=settle_txn.id, property_id=None,
        category="PERSONAL", amount_cents=20000, tax_review_status="CLIENT_CONFIRMED",
    ))
    session.commit()

    je_pay = create_journal_entry(
        session, date(2026, 5, 22), "CASH_SETTLEMENT", "Visa payoff from checking",
        "TEST2:CARD_PAY", source_transaction_id=settle_txn.id,
    )
    add_journal_line(session, je_pay.id, 2010, debit_cents=60000)
    add_journal_line(session, je_pay.id, 1010, credit_cents=60000)
    post_journal_entry(session, je_pay.id)

    # Metrics for full month of May:
    f_full = calculate_property_financials(
        session, property_id=larkspur.id, start_date=date(2026, 5, 1), end_date=date(2026, 5, 31)
    )
    assert f_full["total_operating_expenses_cents"] == 40000  # OpEx is NOT counted again
    assert f_full["noi_cents"] == 260000
    assert f_full["card_settled_rental_opex_cents"] == 40000  # Only the allocated rental share
    assert f_full["ocf_cents"] == 260000  # $3,000 cash in - $400 settled rental share
    assert f_full["cads_cents"] == 260000


def test_3_primary_residence_exclusion_from_rental_cash_flow(session: Session, hardened_env: dict):
    """3. Primary residence exclusion from rental cash flow:
    Elderberry Dr is PRIMARY_RESIDENCE. Its expenses and debt must be excluded from rental metrics,
    but included in portfolio equity and total debt."""
    larkspur = hardened_env["larkspur"]
    elderberry = hardened_env["elderberry"]

    # Alder rent: $2,600
    je_alder = create_journal_entry(session, date(2026, 3, 1), "REVENUE_POST", "Alder rent", "TEST3:LARKSPUR")
    add_journal_line(session, je_alder.id, 1010, debit_cents=260000)
    add_journal_line(session, je_alder.id, 4010, credit_cents=260000, property_id=larkspur.id)
    post_journal_entry(session, je_alder.id)

    # Rowan repairs: $450
    je_rowan_rep = create_journal_entry(session, date(2026, 3, 5), "EXPENSE_POST", "Rowan home repairs", "TEST3:ROWAN_REP")
    add_journal_line(session, je_rowan_rep.id, 5040, debit_cents=45000, property_id=elderberry.id)
    add_journal_line(session, je_rowan_rep.id, 1010, credit_cents=45000)
    post_journal_entry(session, je_rowan_rep.id)

    # Rowan mortgage payment: $2,500 (CCM loan 2130)
    je_rowan_debt = create_journal_entry(session, date(2026, 3, 15), "CASH_SETTLEMENT", "Rowan mortgage payment", "TEST3:ROWAN_DEBT")
    add_journal_line(session, je_rowan_debt.id, 2130, debit_cents=100000, property_id=elderberry.id)
    add_journal_line(session, je_rowan_debt.id, 5010, debit_cents=150000, property_id=elderberry.id)
    add_journal_line(session, je_rowan_debt.id, 1010, credit_cents=250000)
    post_journal_entry(session, je_rowan_debt.id)

    # Portfolio-wide rental financials (property_id=None)
    rental_f = calculate_property_financials(session, property_id=None)
    assert rental_f["goi_cents"] == 260000
    assert rental_f["repairs_cents"] == 0  # Rowan repairs strictly excluded!
    assert rental_f["noi_cents"] == 260000
    assert rental_f["total_debt_service_cents"] == 0  # Rowan debt strictly excluded!
    assert rental_f["ocf_cents"] == 260000
    assert rental_f["cads_cents"] == 260000


def test_4_reporting_period_and_year_filtering(session: Session, hardened_env: dict):
    """4. Reporting-period and year filtering:
    Entries in 2025 and 2026 must be isolated by year parameter."""
    larkspur = hardened_env["larkspur"]

    # 2025 entry
    je_2025 = create_journal_entry(session, date(2025, 6, 1), "REVENUE_POST", "2025 rent", "TEST4:2025")
    add_journal_line(session, je_2025.id, 1010, debit_cents=200000)
    add_journal_line(session, je_2025.id, 4010, credit_cents=200000, property_id=larkspur.id)
    post_journal_entry(session, je_2025.id)

    # 2026 entry
    je_2026 = create_journal_entry(session, date(2026, 6, 1), "REVENUE_POST", "2026 rent", "TEST4:2026")
    add_journal_line(session, je_2026.id, 1010, debit_cents=300000)
    add_journal_line(session, je_2026.id, 4010, credit_cents=300000, property_id=larkspur.id)
    post_journal_entry(session, je_2026.id)

    f_2025 = calculate_property_financials(session, property_id=larkspur.id, year=2025)
    f_2026 = calculate_property_financials(session, property_id=larkspur.id, year=2026)

    assert f_2025["goi_cents"] == 200000
    assert f_2026["goi_cents"] == 300000


def test_5_draft_journal_entries_excluded_from_debt_reporting(session: Session, hardened_env: dict):
    """5. Draft journal entries excluded from debt reporting:
    DRAFT entries must have zero effect on calculated debt and portfolio equity."""
    # Create opening debt balance: $300,000
    je_open = create_journal_entry(session, date(2026, 1, 1), "OPENING_BALANCE", "Opening debt", "TEST5:OPEN")
    add_journal_line(session, je_open.id, 1510, debit_cents=30000000)
    add_journal_line(session, je_open.id, 2110, credit_cents=30000000)
    post_journal_entry(session, je_open.id)

    # Pre-draft check
    eq_before = calculate_portfolio_equity(session)
    assert eq_before["total_debt_cents"] == 30000000

    # Create DRAFT paydown entry of $50,000 (NOT POSTED)
    je_draft = create_journal_entry(session, date(2026, 2, 1), "CASH_SETTLEMENT", "Draft paydown", "TEST5:DRAFT")
    add_journal_line(session, je_draft.id, 2110, debit_cents=5000000)
    add_journal_line(session, je_draft.id, 1010, credit_cents=5000000)
    # Status remains DRAFT!
    session.commit()

    eq_draft = calculate_portfolio_equity(session)
    assert eq_draft["total_debt_cents"] == 30000000  # STILL $300,000!

    # Post it
    post_journal_entry(session, je_draft.id)
    eq_posted = calculate_portfolio_equity(session)
    assert eq_posted["total_debt_cents"] == 25000000  # NOW reduced to $250,000


def test_6_manual_transaction_posting_lifecycle(session: Session, hardened_env: dict):
    """6. Manual transaction posting lifecycle:
    Entry -> Split -> Approval -> Post to Ledger -> Idempotency."""
    chk = hardened_env["chk"]
    stmt = hardened_env["stmt"]
    larkspur = hardened_env["larkspur"]

    # 1. Entry
    txn = Transaction(
        id="txn_lifecycle_test",
        statement_id=stmt.id,
        financial_account_id=chk.id,
        transaction_date=date(2026, 3, 15),
        amount_cents=120000,
        direction="INFLOW",
        raw_description="Manual tenant deposit",
    )
    session.add(txn)
    session.commit()

    # 2. Split with approval
    splits = [
        SplitItem(property_id=larkspur.id, category="RENT_INCOME", amount_cents=120000, tax_review_status="CLIENT_CONFIRMED")
    ]
    proc_state = apply_transaction_splits(session, txn.id, splits)
    assert proc_state.allocation_status == "FULLY_ALLOCATED"
    assert proc_state.posting_status == "UNPOSTED"

    # 3. Post to ledger
    posted_je = post_transaction_to_ledger(session, txn.id, client_confirmed=True)
    assert posted_je.status == "POSTED"
    session.refresh(proc_state)
    assert proc_state.posting_status == "POSTED"
    assert proc_state.journal_entry_id == posted_je.id


def test_7_classification_changes_after_posting_including_reversal(session: Session, hardened_env: dict):
    """7. Classification changes after posting, including reversal:
    Post transaction with repairs -> Reclassify into Repairs + Supplies.
    Original entry remains immutable, compensating reversal entry posts, replacement posts."""
    chk = hardened_env["chk"]
    stmt = hardened_env["stmt"]
    larkspur = hardened_env["larkspur"]

    txn = Transaction(
        id="txn_reclass_test",
        statement_id=stmt.id,
        financial_account_id=chk.id,
        transaction_date=date(2026, 3, 10),
        amount_cents=-50000,
        direction="OUTFLOW",
        raw_description="Hardware store purchase",
    )
    session.add(txn)
    session.commit()

    # Post initial split: 100% repairs ($500)
    splits_v1 = [
        SplitItem(property_id=larkspur.id, category="REPAIRS", amount_cents=50000, tax_review_status="CLIENT_CONFIRMED")
    ]
    apply_transaction_splits(session, txn.id, splits_v1)
    orig_je = post_transaction_to_ledger(session, txn.id, client_confirmed=True)
    assert orig_je.status == "POSTED"

    # Reclassify: $300 repairs, $200 supplies
    splits_v2 = [
        SplitItem(property_id=larkspur.id, category="REPAIRS", amount_cents=30000, tax_review_status="CLIENT_CONFIRMED"),
        SplitItem(property_id=larkspur.id, category="SUPPLIES", amount_cents=20000, tax_review_status="CLIENT_CONFIRMED"),
    ]
    new_je = reclassify_and_repost_transaction(session, txn.id, splits_v2, client_confirmed=True)

    # Invariants verification:
    # A. Original entry is untouched and marked reversed via reversal compensating entry
    session.refresh(orig_je)
    assert orig_je.status == "POSTED"
    assert is_entry_reversed(session, orig_je.id) is True

    # B. New entry is posted
    assert new_je.status == "POSTED"
    assert new_je.id != orig_je.id

    # C. Financials reflect exact new splits
    f = calculate_property_financials(session, property_id=larkspur.id)
    assert f["repairs_cents"] == 30000
    assert f["supplies_cents"] == 20000
    assert f["total_operating_expenses_cents"] == 50000


def test_8_cbd1_and_cbd2_backend_authorization_enforcement(session: Session, hardened_env: dict):
    """8. CBD-1 and CBD-2 backend authorization enforcement:
    CBD-1 ATM cash deposit requires client confirmation.
    CBD-2 mixed-use card requires CPA review or client confirmation."""
    chk = hardened_env["chk"]
    visa = hardened_env["visa"]
    stmt = hardened_env["stmt"]
    larkspur = hardened_env["larkspur"]

    # CBD-1 Test
    txn_cbd1 = Transaction(
        id="txn_cbd1_auth_test",
        statement_id=stmt.id,
        financial_account_id=chk.id,
        transaction_date=date(2026, 3, 20),
        amount_cents=150000,
        direction="INFLOW",
        raw_description="ATM CASH DEPOSIT #4928 (CBD-1 Gate)",
    )
    session.add(txn_cbd1)
    session.commit()

    apply_transaction_splits(session, txn_cbd1.id, [
        SplitItem(property_id=larkspur.id, category="RENT_INCOME", amount_cents=150000, tax_review_status="AUTO_SUGGESTED")
    ])

    # Unconfirmed attempt must raise PermissionError
    with pytest.raises(PermissionError, match="CBD-1 Policy Violation"):
        post_transaction_to_ledger(session, txn_cbd1.id, client_confirmed=False)

    # Confirmed attempt succeeds
    je_cbd1 = post_transaction_to_ledger(session, txn_cbd1.id, client_confirmed=True)
    assert je_cbd1.status == "POSTED"

    # CBD-2 Test
    txn_cbd2 = Transaction(
        id="txn_cbd2_auth_test",
        statement_id=stmt.id,
        financial_account_id=visa.id,
        transaction_date=date(2026, 3, 22),
        amount_cents=-12900,
        direction="OUTFLOW",
        raw_description="APPLE.COM/BILL (CBD-2 Mixed-Use)",
    )
    session.add(txn_cbd2)
    session.commit()

    apply_transaction_splits(session, txn_cbd2.id, [
        SplitItem(property_id=larkspur.id, category="SUPPLIES", amount_cents=12900, tax_review_status="AUTO_SUGGESTED")
    ])

    with pytest.raises(PermissionError, match="CBD-2 Policy Violation"):
        post_transaction_to_ledger(session, txn_cbd2.id, user_attested_cpa_review=False, client_confirmed=False)

    je_cbd2 = post_transaction_to_ledger(session, txn_cbd2.id, user_attested_cpa_review=True)
    assert je_cbd2.status == "POSTED"


def test_9_no_duplicate_journal_entries_on_repeated_requests(session: Session, hardened_env: dict):
    """9. No duplicate journal entries on repeated requests:
    Submitting post request multiple times returns existing entry idempotently without duplication."""
    chk = hardened_env["chk"]
    stmt = hardened_env["stmt"]
    larkspur = hardened_env["larkspur"]

    txn = Transaction(
        id="txn_idempotency_test",
        statement_id=stmt.id,
        financial_account_id=chk.id,
        transaction_date=date(2026, 3, 10),
        amount_cents=80000,
        direction="INFLOW",
        raw_description="Tenant Zelle payment",
    )
    session.add(txn)
    session.commit()

    apply_transaction_splits(session, txn.id, [
        SplitItem(property_id=larkspur.id, category="RENT_INCOME", amount_cents=80000, tax_review_status="CLIENT_CONFIRMED")
    ])

    count_before = len(session.exec(select(JournalEntry)).all())
    je1 = post_transaction_to_ledger(session, txn.id, client_confirmed=True)
    count_after_first = len(session.exec(select(JournalEntry)).all())
    assert count_after_first == count_before + 1

    # Second call must be idempotent
    je2 = post_transaction_to_ledger(session, txn.id, client_confirmed=True)
    count_after_second = len(session.exec(select(JournalEntry)).all())

    assert je1.id == je2.id
    assert count_after_second == count_after_first


def test_10_dashboard_totals_reconciling_to_posted_ledger_balances(session: Session):
    """10. Dashboard totals reconciling to posted ledger balances:
    All metrics on dashboard reconcile to the exact cent with the double-entry general ledger."""
    # Seed complete demo data
    seed_demo_data(session)

    equity = calculate_portfolio_equity(session)
    fin = calculate_property_financials(session)

    # 1. Total debt reconciles with credit balances on 2110, 2120, 2130
    debt_lines = session.exec(
        select(JournalEntryLine)
        .join(JournalEntry, JournalEntryLine.journal_entry_id == JournalEntry.id)
        .where(JournalEntry.status == "POSTED")
        .where(JournalEntryLine.ledger_account_code.in_([2110, 2120, 2130]))
    ).all()
    ledger_debt = sum(l.credit_cents - l.debit_cents for l in debt_lines)
    assert equity["ledger_total_debt_cents"] == ledger_debt
    # The demo ships no servicer statements, so the ledger is the only evidence
    # and every dollar of it is attributed to a door.
    assert equity["total_debt_cents"] == ledger_debt
    assert equity["unattributed_ledger_debt_cents"] == 0
    assert all(m["principal_source"] == "LEDGER_DERIVED" for m in equity["property_metrics"])

    # 2. Rental revenue reconciles with credits to 4010, 4020 for rental doors
    rental_rev_lines = session.exec(
        select(JournalEntryLine)
        .join(JournalEntry, JournalEntryLine.journal_entry_id == JournalEntry.id)
        .where(JournalEntry.status == "POSTED")
        .where(JournalEntryLine.ledger_account_code.in_([4010, 4020]))
        .where(JournalEntryLine.property_id.in_(["prop_tinsley_huntley", "prop_cimmaron_pingree"]))
    ).all()
    assert fin["gross_rental_income_cents"] == sum(l.credit_cents - l.debit_cents for l in rental_rev_lines)

    # 3. Hand-derived portfolio figures from the synthetic fixtures:
    #    rent received 2,600.00; repairs 250.00 + supplies 150.00 = 400.00 OpEx
    #    Rocket 620.00 principal + 660.12 interest + 338.00 escrow
    #    PNC    521.79 principal + 438.37 interest + 841.32 escrow
    assert fin["noi_cents"] == 220000
    assert fin["total_operating_expenses_cents"] == 40000
    assert fin["ocf_cents"] == 260000
    assert fin["principal_paid_cents"] == 114179
    assert fin["mortgage_interest_cents"] == 109849
    assert fin["total_debt_service_cents"] == 224028
    # Escrow funding is real cash out but is not debt service.
    assert fin["escrow_funded_cents"] == 117932
    assert fin["total_debt_service_cents"] == (
        fin["principal_paid_cents"] + fin["mortgage_interest_cents"]
    )
    assert fin["cads_cents"] == 35972  # 2,600.00 received less 2,240.28 paid to the banks


def test_11_reclassification_failure_leaves_history_intact(session: Session, hardened_env: dict):
    """11. A rejected reclassification must not strand a reversal.

    Reversing and reposting is one unit of work: if the replacement classification
    cannot be posted, the superseded entry must remain POSTED and un-reversed.
    """
    chk = hardened_env["chk"]
    stmt = hardened_env["stmt"]
    larkspur = hardened_env["larkspur"]

    txn = Transaction(
        id="txn_reclass_atomicity_test",
        statement_id=stmt.id,
        financial_account_id=chk.id,
        transaction_date=date(2026, 4, 10),
        amount_cents=-50000,
        direction="OUTFLOW",
        raw_description="Hardware store purchase (atomicity)",
    )
    session.add(txn)
    session.commit()

    apply_transaction_splits(session, txn.id, [
        SplitItem(property_id=larkspur.id, category="REPAIRS", amount_cents=50000, tax_review_status="CLIENT_CONFIRMED")
    ])
    orig_je = post_transaction_to_ledger(session, txn.id, client_confirmed=True)
    entries_before = len(session.exec(select(JournalEntry)).all())

    # Unclassified replacement splits must be rejected outright.
    with pytest.raises(ValueError, match="UNCLASSIFIED"):
        reclassify_and_repost_transaction(session, txn.id, [
            SplitItem(property_id=larkspur.id, category="SUPPLIES", amount_cents=50000, tax_review_status="UNCLASSIFIED")
        ])

    session.refresh(orig_je)
    proc_state = session.get(TransactionProcessingState, txn.id)
    assert orig_je.status == "POSTED"
    assert is_entry_reversed(session, orig_je.id) is False
    assert proc_state.posting_status == "POSTED"
    assert proc_state.journal_entry_id == orig_je.id
    assert len(session.exec(select(JournalEntry)).all()) == entries_before
    assert session.exec(
        select(JournalEntry).where(JournalEntry.entry_type == "REVERSAL")
    ).all() == []

    # The original classification still drives the reports.
    f = calculate_property_financials(session, property_id=larkspur.id)
    assert f["repairs_cents"] == 50000
    assert f["supplies_cents"] == 0

    # An approved replacement still succeeds and preserves history.
    new_je = reclassify_and_repost_transaction(session, txn.id, [
        SplitItem(property_id=larkspur.id, category="SUPPLIES", amount_cents=50000, tax_review_status="CLIENT_CONFIRMED")
    ], client_confirmed=True)

    assert new_je.status == "POSTED"
    assert new_je.id != orig_je.id
    assert is_entry_reversed(session, orig_je.id) is True
    f_after = calculate_property_financials(session, property_id=larkspur.id)
    assert f_after["repairs_cents"] == 0
    assert f_after["supplies_cents"] == 50000
