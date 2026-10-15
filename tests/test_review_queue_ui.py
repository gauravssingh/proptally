"""Tests for Review Queue, multi-splitting, auto-suggestions, and governance gates."""

from datetime import date

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
)
from proptally.db.schema import init_schema
from proptally.domain.accounting.coa import ensure_entity_ledger_accounts
from proptally.domain.fixtures.demo_seed import DEMO_VENDOR_RULES
from proptally.domain.transactions.suggest import (
    seed_vendor_rules,
    suggest_split_for_transaction,
)


def test_auto_suggestion_rules(engine):
    """A portfolio's own rules route its rent deposits; generic merchants stay generic.

    A named tenant and a branch-deposit pattern are portfolio data, so they come
    from the portfolio's rule set (DEMO_VENDOR_RULES) rather than from the engine.
    """
    with Session(engine) as session:
        ensure_entity_ledger_accounts(session)
        seed_vendor_rules(session, DEMO_VENDOR_RULES)

        # Tyler Saxsma Zelle rent
        s1 = suggest_split_for_transaction("Zelle payment from TYLER J SAXSMA for rent", 48300, session=session)
        assert s1 is not None
        assert s1.property_id == "prop_tinsley_huntley"
        assert s1.category == "RENT_INCOME"

        # Millbrook ATM deposit
        s2 = suggest_split_for_transaction("BKOFAMERICA ATM DEPOSIT CARPENTERSVILLE", 85000, session=session)
        assert s2 is not None
        assert s2.property_id == "prop_cimmaron_pingree"
        assert s2.category == "RENT_INCOME"
        assert s2.requires_client_confirmation is True

        # Hardware vendor (portfolio-independent rule)
        s3 = suggest_split_for_transaction("MENARDS CARPENTERSVILLE", -45000, session=session)
        assert s3 is not None
        assert s3.category == "REPAIRS"
        assert s3.requires_cpa_review is True

        # Personal restaurant
        s4 = suggest_split_for_transaction("WOODLANDS RESTAURANT FAIRFAX VA", -5151, session=session)
        assert s4 is not None
        assert s4.category == "PERSONAL"


def test_review_queue_routes_and_multi_split(tmp_path, owner):
    db_file = tmp_path / "test_review.db"
    db_uri = f"sqlite:///{db_file}"
    engine = get_engine(db_uri)
    init_schema(engine)

    with Session(engine) as session:
        ensure_entity_ledger_accounts(session)
        doc = Document(
            id="doc_1",
            file_sha256="abc123testsha",
            original_filename="stmt.pdf",
            storage_path="/tmp/stmt.pdf",
            file_size_bytes=1024,
        )
        session.add(doc)
        inst = FinancialInstitution(id="inst_bofa", name="Bank of America")
        session.add(inst)
        session.flush()

        acct = FinancialAccount(
            id="acct_bofa_chk_9456",
            institution_id=inst.id,
            account_name="Checking",
            account_type="CHECKING",
            account_number_masked="...9456",
        )
        session.add(acct)
        prop_t = Property(id="prop_tinsley_huntley", name="Larkspur", address_line1="12307 Larkspur", city="Westbrook", zip_code="60196")
        prop_b = Property(id="prop_burkey_algonquin", name="Elderberry", address_line1="3345 Elderberry", city="Cedar Grove", zip_code="60198")
        session.add(prop_t)
        session.add(prop_b)
        session.flush()

        stmt = Statement(
            id="stmt_1",
            document_id="doc_1",
            financial_account_id=acct.id,
            statement_type="CHECKING",
            active_parser_name="TestParser",
            active_parser_version="1.0",
            statement_date=date(2026, 1, 15),
        )
        session.add(stmt)

        # 1. Unallocated Menards transaction
        tx_menards = Transaction(
            id="tx_menards_1",
            statement_id=stmt.id,
            financial_account_id=acct.id,
            transaction_date=date(2026, 1, 5),
            amount_cents=-50000,  # -$500.00
            direction="OUTFLOW",
            raw_description="MENARDS CARPENTERSVILLE HARDWARE",
        )
        session.add(tx_menards)
        state_menards = TransactionProcessingState(
            transaction_id=tx_menards.id,
            allocation_status="UNALLOCATED",
            allocated_cents=0,
            residual_cents=50000,
            posting_status="UNPOSTED",
        )
        session.add(state_menards)

        # 2. Unallocated Millbrook ATM deposit (CBD-1 Gate)
        tx_atm = Transaction(
            id="tx_atm_1",
            statement_id=stmt.id,
            financial_account_id=acct.id,
            transaction_date=date(2025, 12, 17),
            amount_cents=85000,  # +$850.00
            direction="INFLOW",
            raw_description="BKOFAMERICA ATM 12/17 DEPOSIT CARPENTERSVILLE",
            requires_client_confirmation=True,
        )
        session.add(tx_atm)
        state_atm = TransactionProcessingState(
            transaction_id=tx_atm.id,
            allocation_status="UNALLOCATED",
            allocated_cents=0,
            residual_cents=85000,
            posting_status="UNPOSTED",
        )
        session.add(state_atm)
        session.commit()

    signed_in = owner(db_url=db_uri)
    client = signed_in.client

    # 1. Test viewing reconciliation queue
    resp = client.get("/reconciliation")
    assert resp.status_code == 200
    assert "MENARDS CARPENTERSVILLE" in resp.text
    assert "Rule Suggestion" in resp.text

    # 2. Test CBD-1 gate rejection if confirmation missing
    resp_reject = signed_in.post("/reconciliation/assign", data={
        "transaction_id": "tx_atm_1",
        "property_id": "prop_cimmaron_pingree",
        "category": "RENT_INCOME",
        "client_confirmed": "false",
    })
    assert resp_reject.status_code == 403

    # 3. Test multi-splitting $500 Menards: $300 Alder repairs + $200 Rowan personal
    resp_split = signed_in.post("/reconciliation/split", data={
        "transaction_id": "tx_menards_1",
        "split_property_id": ["prop_tinsley_huntley", "prop_burkey_algonquin"],
        "split_category": ["REPAIRS", "PERSONAL"],
        "split_amount_cents": [30000, 20000],
        "client_confirmed": "true",
        "user_attested_cpa_review": "true",
        "post_if_fully_allocated": "true",
    }, follow_redirects=False)
    assert resp_split.status_code == 303

    # Verify split in DB
    with Session(engine) as session:
        splits = session.exec(select(TransactionSplit).where(TransactionSplit.transaction_id == "tx_menards_1")).all()
        assert len(splits) == 2
        assert {s.amount_cents for s in splits} == {30000, 20000}
        state = session.get(TransactionProcessingState, "tx_menards_1")
        assert state.allocation_status == "FULLY_ALLOCATED"
        assert state.posting_status == "POSTED"
