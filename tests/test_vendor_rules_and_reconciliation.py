"""Tests for vendor classification model, review queue search, filtering, and batch rule application."""

from datetime import date

import pytest
from sqlmodel import Session, select

from proptally.db.connection import get_engine
from proptally.db.models import (
    Document,
    FinancialAccount,
    FinancialInstitution,
    Property,
    Statement,
    Transaction,
    TransactionProcessingState,
    TransactionSplit,
    VendorRule,
)
from proptally.db.schema import init_schema
from proptally.domain.accounting.coa import ensure_entity_ledger_accounts
from reference_portfolio import REFERENCE_VENDOR_RULES
from proptally.domain.transactions.suggest import (
    seed_vendor_rules,
    suggest_split_for_transaction,
)


def test_repeat_vendor_classification_model():
    """Verify built-in repeat vendor models (Hertz, Apple, Amazon, ParkWhiz, JNL, etc.)."""
    # 1. Hertz Vehicle Rental
    hertz_sugg = suggest_split_for_transaction("HERTZ RENT A CAR O HARE INTL", -18450)
    assert hertz_sugg is not None
    assert hertz_sugg.vendor_name == "Hertz Rent-A-Car"
    assert hertz_sugg.category == "PERSONAL"
    assert "Vehicle Rental" in hertz_sugg.service_type

    # 2. Apple Services & Cloud
    apple_sugg = suggest_split_for_transaction("APPLE.COM/BILL ONE APPLE PARK WAY CUPERTINO CA", -299)
    assert apple_sugg is not None
    assert apple_sugg.vendor_name == "Apple Services & App Store"
    assert apple_sugg.category == "PERSONAL"
    assert "Digital Subscriptions" in apple_sugg.service_type

    # 3. Apple Card Settlement
    applecard_sugg = suggest_split_for_transaction("APPLECARD GSBANK DES:PAYMENT ID:21529388", -150000)
    assert applecard_sugg is not None
    assert "Apple Card" in applecard_sugg.vendor_name
    assert applecard_sugg.category == "PERSONAL"

    # 4. Amazon Marketplace
    amzn_sugg = suggest_split_for_transaction("Amazon Seller Services#26/1 Bangalore IND", -3520)
    assert amzn_sugg is not None
    assert "Amazon" in amzn_sugg.vendor_name
    assert amzn_sugg.category == "PERSONAL"

    # 5. ParkWhiz Parking
    park_sugg = suggest_split_for_transaction("ParkWhiz, Inc. Austin TX", -1200)
    assert park_sugg is not None
    assert park_sugg.vendor_name == "ParkWhiz Parking"
    assert park_sugg.category == "PERSONAL"

    # 6. JNL Climate Control (HVAC Repairs)
    jnl_sugg = suggest_split_for_transaction("JNL CLIMATE CONTROL 847-555-0134 IL", -48500)
    assert jnl_sugg is not None
    assert jnl_sugg.vendor_name == "JNL Climate Control"
    assert jnl_sugg.category == "REPAIRS"
    assert jnl_sugg.requires_cpa_review is True

    # 7. Utilities & HOA
    comed_sugg = suggest_split_for_transaction("COMED PAYMENT 10 S DEARBORN ST", -8500)
    assert comed_sugg is not None
    assert comed_sugg.category == "UTILITIES"

    hoa_sugg = suggest_split_for_transaction("REGENCY SQUARE O DES:REGENCY SQ", -31000)
    assert hoa_sugg is not None
    assert hoa_sugg.category == "HOA"


@pytest.fixture
def queue_env(tmp_path, monkeypatch, owner):
    db_file = tmp_path / "test_queue.db"
    db_uri = f"sqlite:///{db_file}"
    engine = get_engine(db_uri)
    init_schema(engine)

    with Session(engine) as session:
        ensure_entity_ledger_accounts(session)
        seed_vendor_rules(session, REFERENCE_VENDOR_RULES)

        # Base document & institution
        doc = Document(id="doc_1", file_sha256="sha_abc", original_filename="test.pdf", storage_path="/tmp/test.pdf", file_size_bytes=100)
        session.add(doc)
        inst = FinancialInstitution(id="inst_bofa", name="Bank of America")
        session.add(inst)
        session.flush()

        acct1 = FinancialAccount(id="acct_bofa_chk", institution_id=inst.id, account_name="BofA Checking", account_type="CHECKING", account_number_masked="...9456")
        acct2 = FinancialAccount(id="acct_bofa_ccr", institution_id=inst.id, account_name="BofA CCR Visa", account_type="CREDIT_CARD", account_number_masked="...6196")
        session.add(acct1)
        session.add(acct2)

        prop1 = Property(id="prop_tinsley_huntley", name="12307 Larkspur St", address_line1="12307 Larkspur St", city="Westbrook", zip_code="60196")
        prop2 = Property(id="prop_cimmaron_pingree", name="582 Foxglove Ct", address_line1="582 Foxglove Ct", city="Oakhaven", zip_code="60199")
        session.add(prop1)
        session.add(prop2)
        session.flush()

        stmt1 = Statement(id="stmt_1", document_id="doc_1", financial_account_id=acct1.id, statement_type="CHECKING", active_parser_name="Test", active_parser_version="1.0", statement_date=date(2026, 2, 1))
        stmt2 = Statement(id="stmt_2", document_id="doc_1", financial_account_id=acct2.id, statement_type="CREDIT_CARD", active_parser_name="Test", active_parser_version="1.0", statement_date=date(2026, 2, 1))
        session.add(stmt1)
        session.add(stmt2)

        # Seed test transactions:
        # 3 Apple transactions
        for i in range(3):
            tx = Transaction(id=f"tx_apple_{i}", statement_id=stmt2.id, financial_account_id=acct2.id, transaction_date=date(2026, 1, 10 + i), amount_cents=-299, direction="OUTFLOW", raw_description=f"APPLE.COM/BILL CUPERTINO CA #{i}")
            session.add(tx)
            st = TransactionProcessingState(transaction_id=tx.id, allocation_status="UNALLOCATED", allocated_cents=0, residual_cents=299, posting_status="UNPOSTED")
            session.add(st)

        # 2 Hertz transactions
        for i in range(2):
            tx = Transaction(id=f"tx_hertz_{i}", statement_id=stmt2.id, financial_account_id=acct2.id, transaction_date=date(2026, 1, 15 + i), amount_cents=-14500, direction="OUTFLOW", raw_description=f"HERTZ RENT A CAR #{i}")
            session.add(tx)
            st = TransactionProcessingState(transaction_id=tx.id, allocation_status="UNALLOCATED", allocated_cents=0, residual_cents=14500, posting_status="UNPOSTED")
            session.add(st)

        # 1 Millbrook ATM deposit (CBD-1 gate)
        tx_atm = Transaction(id="tx_atm_1", statement_id=stmt1.id, financial_account_id=acct1.id, transaction_date=date(2026, 1, 5), amount_cents=85000, direction="INFLOW", raw_description="BKOFAMERICA ATM DEPOSIT CARPENTERSVILLE")
        session.add(tx_atm)
        st_atm = TransactionProcessingState(transaction_id=tx_atm.id, allocation_status="UNALLOCATED", allocated_cents=0, residual_cents=85000, posting_status="UNPOSTED")
        session.add(st_atm)

        session.commit()

    signed_in = owner(db_url=db_uri)
    yield signed_in, db_uri


def test_review_queue_page_and_filtering(queue_env):
    signed_in, _ = queue_env

    # 1. Base page load returns total count (6 items: 3 Apple + 2 Hertz + 1 ATM)
    res = signed_in.client.get("/reconciliation")
    assert res.status_code == 200
    html = res.text
    assert "Reconciliation & Review Queue" in html
    assert "6 Pending" in html
    assert "Apple Services" in html
    assert "Hertz Rent-A-Car" in html
    assert "Vendor Classification Rules" in html

    # 2. Search query filter (?q=apple)
    res_apple = signed_in.client.get("/reconciliation?q=apple")
    assert res_apple.status_code == 200
    assert "APPLE.COM/BILL" in res_apple.text
    assert "HERTZ RENT A CAR" not in res_apple.text

    # 3. Vendor pattern chip filter (?vendor_pattern=HERTZ)
    res_hertz = signed_in.client.get("/reconciliation?vendor_pattern=HERTZ")
    assert res_hertz.status_code == 200
    assert "HERTZ RENT A CAR" in res_hertz.text
    assert "APPLE.COM/BILL" not in res_hertz.text
    assert "Found 2 unallocated transactions" in res_hertz.text

    # 4. Governance gate filter (?gate=CBD1)
    res_cbd1 = signed_in.client.get("/reconciliation?gate=CBD1")
    assert res_cbd1.status_code == 200
    assert "BKOFAMERICA ATM DEPOSIT CARPENTERSVILLE" in res_cbd1.text
    assert "APPLE.COM/BILL" not in res_cbd1.text


def test_batch_vendor_rule_application(queue_env):
    signed_in, db_uri = queue_env

    # Batch classify all 3 Apple transactions as PERSONAL
    res_batch = signed_in.post(
        "/reconciliation/batch-apply-rule",
        data={
            "vendor_pattern": "APPLE.COM",
            "category": "PERSONAL",
            "property_id": "",
        },
        follow_redirects=True,
    )
    assert res_batch.status_code == 200
    assert "applied=3" in res_batch.text or "3" in res_batch.text

    # Verify DB: all 3 Apple transactions should now be FULLY_ALLOCATED and POSTED
    engine = get_engine(db_uri)
    with Session(engine) as session:
        for i in range(3):
            state = session.get(TransactionProcessingState, f"tx_apple_{i}")
            assert state.allocation_status == "FULLY_ALLOCATED"
            assert state.posting_status == "POSTED"
            splits = session.exec(select(TransactionSplit).where(TransactionSplit.transaction_id == f"tx_apple_{i}")).all()
            assert len(splits) == 1
            assert splits[0].category == "PERSONAL"

        # Verify Hertz transactions remain unallocated
        state_hertz = session.get(TransactionProcessingState, "tx_hertz_0")
        assert state_hertz.allocation_status == "UNALLOCATED"


def test_create_custom_vendor_rule(queue_env):
    signed_in, db_uri = queue_env

    # Create a new custom vendor rule
    res = signed_in.post(
        "/reconciliation/vendor-rules/create",
        data={
            "pattern": "AIRBNB",
            "vendor_name": "Airbnb Lodging",
            "service_type": "Guest Lodging & Travel",
            "default_category": "PERSONAL",
            "default_property_id": "",
            "confidence": 0.95,
        },
        follow_redirects=True,
    )
    assert res.status_code == 200

    engine = get_engine(db_uri)
    with Session(engine) as session:
        rule = session.exec(select(VendorRule).where(VendorRule.pattern == "AIRBNB")).first()
        assert rule is not None
        assert rule.vendor_name == "Airbnb Lodging"
        assert rule.service_type == "Guest Lodging & Travel"

    # Test that suggestion engine now recognizes the new rule
    sugg = suggest_split_for_transaction("AIRBNB HM4QWZP RESERVATION", -34000, session=session)
    assert sugg is not None
    assert sugg.vendor_name == "Airbnb Lodging"
    assert sugg.category == "PERSONAL"
