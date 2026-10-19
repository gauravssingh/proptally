"""Tests for Realtor/Property Management fees, Tenant Placement commissions, single-property repairs, and HOA dues."""

from datetime import date

from sqlmodel import Session, select

from proptally.db.connection import get_engine
from proptally.db.models import (
    Document,
    FinancialAccount,
    FinancialInstitution,
    JournalEntry,
    JournalEntryLine,
    Property,
    Statement,
    Transaction,
)
from proptally.db.schema import init_schema
from proptally.domain.accounting.coa import STANDARD_ACCOUNTS, ensure_entity_ledger_accounts
from proptally.domain.tax.schedule_e import generate_schedule_e_workpaper
from proptally.domain.transactions.splits import (
    CATEGORY_TO_OPEX_CODE,
    SplitItem,
    apply_transaction_splits,
    post_transaction_to_ledger,
)
from proptally.domain.transactions.suggest import suggest_split_for_transaction


def test_chart_of_accounts_realtor_and_hoa_codes():
    codes = {acct["code"]: acct["name"] for acct in STANDARD_ACCOUNTS}
    assert 5072 in codes
    assert "Property Management Fees (Line 11)" in codes[5072]
    assert 5075 in codes
    assert "Realtor Commissions & Tenant Placement (Line 8)" in codes[5075]
    assert 5078 in codes
    assert "HOA Assessments & Dues (Line 19)" in codes[5078]

    assert CATEGORY_TO_OPEX_CODE["MANAGEMENT"] == 5072
    assert CATEGORY_TO_OPEX_CODE["COMMISSIONS"] == 5075
    assert CATEGORY_TO_OPEX_CODE["HOA"] == 5078
    assert CATEGORY_TO_OPEX_CODE["REPAIRS"] == 5040


def test_transaction_suggestions_realtor_and_management():
    # Management fees
    sug_mgmt = suggest_split_for_transaction("KEYREALTY PROPERTY MANAGEMENT FEE", -25000)
    assert sug_mgmt is not None
    assert sug_mgmt.category == "MANAGEMENT"
    assert sug_mgmt.confidence >= 0.85

    sug_realtor = suggest_split_for_transaction("REALTOR MGMT FEE FOR MARCH", -20000)
    assert sug_realtor is not None
    assert sug_realtor.category == "MANAGEMENT"

    # Tenant placement commissions
    sug_comm = suggest_split_for_transaction("TENANT PLACEMENT FEE - CLIENT RENTAL", -150000)
    assert sug_comm is not None
    assert sug_comm.category == "COMMISSIONS"
    assert sug_comm.confidence >= 0.90

    sug_placement = suggest_split_for_transaction("COMMISSION LEASING AGENT", -120000)
    assert sug_placement is not None
    assert sug_placement.category == "COMMISSIONS"


def test_schedule_e_realtor_fees_and_single_property_repair(tmp_path):
    db_file = tmp_path / "test_realtor.db"
    db_uri = f"sqlite:///{db_file}"
    engine = get_engine(db_uri)
    init_schema(engine)

    with Session(engine) as session:
        ensure_entity_ledger_accounts(session)

        # Rental property: Alder
        larkspur = Property(
            id="prop_tinsley_huntley",
            name="12307 Larkspur St",
            address_line1="12307 Larkspur St",
            city="Westbrook",
            zip_code="60196",
            property_type="RENTAL_INVESTMENT",
            acquisition_date=date(2021, 6, 15),
            purchase_price_cents=42500000,
            land_basis_cents=8500000,
            building_basis_cents=34000000,
        )
        session.add(larkspur)

        doc = Document(
            id="doc_test_01",
            file_sha256="testsha256hash1234567890",
            original_filename="manual_realtor.pdf",
            storage_path="/test/manual_realtor.pdf",
            file_size_bytes=1024,
        )
        session.add(doc)

        inst = FinancialInstitution(
            id="inst_bofa",
            name="Bank of America",
        )
        session.add(inst)
        session.flush()

        # Bank account
        bank_acct = FinancialAccount(
            id="acct_bofa_chk_9456",
            institution_id=inst.id,
            account_name="Bank of America Checking (...9456)",
            account_type="CHECKING",
            account_number_masked="...9456",
            is_active=True,
        )
        session.add(bank_acct)
        session.flush()

        # Dummy Statement
        stmt = Statement(
            id="stmt_test_01",
            document_id=doc.id,
            financial_account_id=bank_acct.id,
            statement_type="CHECKING",
            active_parser_name="Manual",
            active_parser_version="1.0",
            statement_date=date(2026, 3, 1),
        )
        session.add(stmt)
        session.commit()

        # 1. Single property repair transaction ($350)
        txn_repair = Transaction(
            id="txn_repair_01",
            statement_id=stmt.id,
            financial_account_id=bank_acct.id,
            transaction_date=date(2026, 3, 10),
            amount_cents=-35000,
            direction="OUTFLOW",
            raw_description="Plumbing repair for Alder toilet leak",
        )
        session.add(txn_repair)
        session.flush()

        # Single property, single expense assignment
        splits_repair = [
            SplitItem(
                property_id=larkspur.id,
                category="REPAIRS",
                amount_cents=35000,
                tax_review_status="USER_CLASSIFIED",
            )
        ]
        apply_transaction_splits(session, txn_repair.id, splits_repair)
        post_transaction_to_ledger(session, txn_repair.id, client_confirmed=True)

        # 2. Monthly Property Management Fee ($200)
        txn_mgmt = Transaction(
            id="txn_mgmt_01",
            statement_id=stmt.id,
            financial_account_id=bank_acct.id,
            transaction_date=date(2026, 3, 15),
            amount_cents=-20000,
            direction="OUTFLOW",
            raw_description="Realtor Monthly Property Management Fee",
        )
        session.add(txn_mgmt)
        session.flush()

        splits_mgmt = [
            SplitItem(
                property_id=larkspur.id,
                category="MANAGEMENT",
                amount_cents=20000,
                tax_review_status="USER_CLASSIFIED",
            )
        ]
        apply_transaction_splits(session, txn_mgmt.id, splits_mgmt)
        post_transaction_to_ledger(session, txn_mgmt.id, client_confirmed=True)

        # 3. Tenant Placement Commission ($1,500)
        txn_comm = Transaction(
            id="txn_comm_01",
            statement_id=stmt.id,
            financial_account_id=bank_acct.id,
            transaction_date=date(2026, 3, 18),
            amount_cents=-150000,
            direction="OUTFLOW",
            raw_description="Realtor Tenant Placement Commission",
        )
        session.add(txn_comm)
        session.flush()

        splits_comm = [
            SplitItem(
                property_id=larkspur.id,
                category="COMMISSIONS",
                amount_cents=150000,
                tax_review_status="USER_CLASSIFIED",
            )
        ]
        apply_transaction_splits(session, txn_comm.id, splits_comm)
        post_transaction_to_ledger(session, txn_comm.id, client_confirmed=True)

        # 4. HOA Assessment ($150)
        txn_hoa = Transaction(
            id="txn_hoa_01",
            statement_id=stmt.id,
            financial_account_id=bank_acct.id,
            transaction_date=date(2026, 3, 20),
            amount_cents=-15000,
            direction="OUTFLOW",
            raw_description="Alder HOA Monthly Dues",
        )
        session.add(txn_hoa)
        session.flush()

        splits_hoa = [
            SplitItem(
                property_id=larkspur.id,
                category="HOA",
                amount_cents=15000,
                tax_review_status="USER_CLASSIFIED",
            )
        ]
        apply_transaction_splits(session, txn_hoa.id, splits_hoa)
        post_transaction_to_ledger(session, txn_hoa.id, client_confirmed=True)

        # Verify Journal Entries posted with correct accounts and balances
        jes = session.exec(select(JournalEntry)).all()
        assert len(jes) == 4
        for je in jes:
            lines = session.exec(select(JournalEntryLine).where(JournalEntryLine.journal_entry_id == je.id)).all()
            total_debits = sum(line.debit_cents for line in lines)
            total_credits = sum(line.credit_cents for line in lines)
            assert total_debits == total_credits

        # Verify Schedule E Workpaper
        wp = generate_schedule_e_workpaper(session, tax_year=2026)
        col = [c for c in wp.columns if c.property_id == larkspur.id][0]

        # Schedule E line assertions
        assert col.line_14_repairs == 35000        # $350.00
        assert col.line_11_management == 20000     # $200.00 (Schedule E Line 11 Management fees)
        assert col.line_8_commissions == 150000    # $1,500.00 (Schedule E Line 8 Commissions)
        assert col.line_19_other == 15000          # $150.00 (Schedule E Line 19 HOA Assessments)
