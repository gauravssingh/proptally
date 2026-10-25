"""Tests for vendor classification model, review queue search, filtering, and batch rule application."""

from datetime import date

import pytest
from reference_portfolio import REFERENCE_VENDOR_RULES
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
        prop2 = Property(id="prop_cimmaron_pingree", name="2718 Foxglove Ct", address_line1="2718 Foxglove Ct", city="Oakhaven", zip_code="60199")
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


def test_review_queue_page_and_filtering(queue_env, parse_html):
    signed_in, _ = queue_env

    # 1. Base page load returns total count (6 items: 3 Apple + 2 Hertz + 1 ATM)
    res = signed_in.client.get("/reconciliation")
    assert res.status_code == 200
    html = res.text
    assert "Reconciliation & Review Queue" in html
    page = parse_html(html)
    summary = page.select_one(".pt-fs-caption strong")
    assert summary is not None and summary.get_text(strip=True) == "6"
    assert page.select_one("[data-clear-selection]") is not None
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
    page = parse_html(res_hertz.text)
    assert len(page.select('form[id^="rowForm_"]')) == 2

    # 4. Governance gate filter (?gate=CBD1)
    res_cbd1 = signed_in.client.get("/reconciliation?gate=CBD1")
    assert res_cbd1.status_code == 200
    assert "BKOFAMERICA ATM DEPOSIT CARPENTERSVILLE" in res_cbd1.text
    assert "APPLE.COM/BILL" not in res_cbd1.text


def test_vendor_chip_filters_by_suggestion_vendor_name(tmp_path, owner):
    """A chip labeled with a suggestion's vendor name must return those rows.

    "Retail / Dining" is a suggestion vendor name, not a substring of the bank
    text or the rule description, so the filter has to consider it explicitly.
    """
    db_uri = f"sqlite:///{tmp_path / 'vendor_chip.db'}"
    engine = get_engine(db_uri)
    init_schema(engine)

    with Session(engine) as session:
        ensure_entity_ledger_accounts(session)
        session.add(Document(
            id="doc_vendor_chip",
            file_sha256="sha_vendor_chip",
            original_filename="stmt.pdf",
            storage_path="/tmp/stmt.pdf",
            file_size_bytes=100,
        ))
        session.add(FinancialInstitution(id="inst_vendor_chip", name="Bank of America"))
        session.flush()
        session.add(FinancialAccount(
            id="acct_vendor_chip",
            institution_id="inst_vendor_chip",
            account_name="Checking",
            account_type="CHECKING",
            account_number_masked="...9456",
        ))
        session.add(Statement(
            id="stmt_vendor_chip",
            document_id="doc_vendor_chip",
            financial_account_id="acct_vendor_chip",
            statement_type="CHECKING",
            active_parser_name="Test",
            active_parser_version="1.0",
            statement_date=date(2026, 2, 1),
        ))
        for tx_id, description, amount in (
            ("tx_restaurant", "WOODLANDS RESTAURANT FAIRFAX VA", -5151),
            ("tx_apple", "APPLE.COM/BILL CUPERTINO CA", -299),
        ):
            session.add(Transaction(
                id=tx_id,
                statement_id="stmt_vendor_chip",
                financial_account_id="acct_vendor_chip",
                transaction_date=date(2026, 1, 10),
                amount_cents=amount,
                direction="OUTFLOW",
                raw_description=description,
            ))
            session.add(TransactionProcessingState(
                transaction_id=tx_id,
                allocation_status="UNALLOCATED",
                allocated_cents=0,
                residual_cents=abs(amount),
                posting_status="UNPOSTED",
            ))
        session.commit()

    signed_in = owner(db_url=db_uri)
    res = signed_in.client.get("/reconciliation?vendor_pattern=Retail%20%2F%20Dining")
    assert res.status_code == 200
    assert "WOODLANDS RESTAURANT" in res.text
    assert "APPLE.COM/BILL" not in res.text


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


def test_create_vendor_rule_with_apply_to_existing_does_not_500(queue_env):
    """The internal batch-apply call must not inherit a FastAPI Form default as rule_id."""
    signed_in, _ = queue_env

    res = signed_in.post(
        "/reconciliation/vendor-rules/create",
        data={
            "pattern": "APPLE.COM",
            "vendor_name": "Apple Services",
            "service_type": "Digital subscriptions",
            "default_category": "PERSONAL",
            "default_property_id": "",
            "confidence": "0.95",
            "apply_to_existing": "true",
        },
        follow_redirects=False,
    )

    assert res.status_code == 303
    assert "applied=3" in res.headers["location"]


def test_a_rule_requiring_confirmation_stages_instead_of_authorising(queue_env):
    """Requirement flags are policy; they must never be supplied as authorization."""
    signed_in, db_uri = queue_env
    engine = get_engine(db_uri)
    with Session(engine) as session:
        session.add(VendorRule(
            id="vrule_apple_confirm",
            pattern="APPLE",
            vendor_name="Apple",
            service_type="Apple purchases",
            default_category="PERSONAL",
            requires_client_confirmation=True,
            is_active=True,
        ))
        session.commit()

    response = signed_in.post(
        "/reconciliation/batch-apply-rule",
        data={"vendor_pattern": "APPLE", "category": "PERSONAL"},
        follow_redirects=False,
    )
    assert response.status_code == 303

    with Session(engine) as session:
        for index in range(3):
            txn_id = f"tx_apple_{index}"
            state = session.get(TransactionProcessingState, txn_id)
            assert state.posting_status == "UNPOSTED"
            assert state.allocation_status == "FULLY_ALLOCATED"
            txn = session.get(Transaction, txn_id)
            assert txn.requires_client_confirmation is True
            assert session.exec(
                select(TransactionSplit).where(TransactionSplit.transaction_id == txn_id)
            ).all()


def test_batch_vendor_rule_by_vendor_display_name_and_truncated_raw_desc(queue_env, parse_html):
    """Batch action succeeds when vendor_pattern is the vendor display name,
    the rule pattern is a prefix, and the raw description is truncated bank text.
    """
    signed_in, db_uri = queue_env
    engine = get_engine(db_uri)
    with Session(engine) as session:
        for i in range(3):
            tx = Transaction(
                id=f"tx_hughes_{i}",
                statement_id="stmt_1",
                financial_account_id="acct_bofa_chk",
                transaction_date=date(2026, 1, 15 + i),
                amount_cents=461442,
                direction="INFLOW",
                raw_description=f"HUGHES NETWORK S DES:PAYRLL DEP ID:000031127{i}",
            )
            session.add(tx)
            st = TransactionProcessingState(
                transaction_id=tx.id,
                allocation_status="UNALLOCATED",
                allocated_cents=0,
                residual_cents=461442,
                posting_status="UNPOSTED",
            )
            session.add(st)
        session.commit()

    # 1. Reconciliation view matches on vendor display name
    res = signed_in.client.get("/reconciliation?vendor_pattern=Hughes Network Systems")
    assert res.status_code == 200
    page = parse_html(res.text)
    assert len(page.select('form[id^="rowForm_"]')) == 3

    # 2. Batch post using the vendor display name
    res_batch = signed_in.post(
        "/reconciliation/batch-apply-rule",
        data={
            "vendor_pattern": "Hughes Network Systems",
            "category": "PERSONAL",
            "property_id": "",
        },
        follow_redirects=True,
    )
    assert res_batch.status_code == 200
    assert "applied=3" in res_batch.text or "3" in res_batch.text

    # 3. Verify all 3 are fully allocated and posted to the ledger
    with Session(engine) as session:
        for i in range(3):
            st = session.get(TransactionProcessingState, f"tx_hughes_{i}")
            assert st.allocation_status == "FULLY_ALLOCATED"
            assert st.posting_status == "POSTED"


def test_vendor_rules_table_displays_property_name_not_id(queue_env):
    """The saved vendor rules table in the reconciliation view displays the property's
    name/short label instead of the raw property ID.
    """
    signed_in, db_uri = queue_env
    engine = get_engine(db_uri)
    with Session(engine) as session:
        prop = session.exec(select(Property)).first()
        assert prop is not None
        prop_id = prop.id
        expected_name = prop.short_label or prop.name
        rule = session.get(VendorRule, "vrule_regency_hoa")
        if rule:
            rule.default_property_id = prop_id
        else:
            rule = VendorRule(
                id="vrule_regency_hoa",
                pattern="REGENCY SQUARE",
                vendor_name="Regency Square HOA",
                service_type="Homeowners Association Assessments & Common Dues",
                default_category="HOA",
                default_property_id=prop_id,
                confidence=0.95,
                is_active=True,
            )
            session.add(rule)
        session.commit()

    soup = signed_in.soup("/reconciliation")
    rule_cell = soup.find("td", string=lambda s: s and "REGENCY SQUARE" in s)
    assert rule_cell is not None
    row = rule_cell.find_parent("tr")
    row_text = row.get_text()

    # The raw property ID must NOT appear in the rendered row
    assert prop_id not in row_text
    # The property name or short label MUST appear
    assert expected_name in row_text

