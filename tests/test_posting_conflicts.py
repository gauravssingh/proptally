"""A posted ledger entry and a lost processing state must not produce a 500.

The review queue offers "Assign & Post" on any transaction its state calls
unposted. When the ledger already holds the entry, the state is the thing that
is wrong: posting must adopt the entry when it records the same allocation, and
refuse with a clear message when it records a different one.
"""

from datetime import date
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event, func
from sqlmodel import Session, select

from proptally.db.connection import configure_sqlite_connection
from proptally.db.models import (
    Document,
    FinancialAccount,
    FinancialInstitution,
    JournalEntry,
    LoanAccount,
    Property,
    Statement,
    Transaction,
    TransactionProcessingState,
    TransactionSplit,
)
from proptally.db.schema import _heal_posted_transaction_states, init_schema
from proptally.domain.accounting.coa import ensure_entity_ledger_accounts
from proptally.domain.transactions.policy import classification_status_for
from proptally.domain.transactions.splits import (
    SplitItem,
    apply_transaction_splits,
    post_transaction_to_ledger,
)

# --- Fixtures -------------------------------------------------------------


def _mortgage_context(session: Session) -> dict:
    institution = FinancialInstitution(id="inst_conflict_bank", name="Test Bank")
    checking = FinancialAccount(
        id="acct_conflict_checking",
        institution_id=institution.id,
        account_name="Checking",
        account_type="CHECKING",
        account_number_masked="...0001",
    )
    mortgage = FinancialAccount(
        id="acct_conflict_mortgage",
        institution_id=institution.id,
        account_name="Mr Cooper",
        account_type="MORTGAGE",
        account_number_masked="...0002",
    )
    prop = Property(
        id="prop_conflict_tinsley",
        name="12307 TINSLEY ST",
        address_line1="12307 TINSLEY ST",
        city="HUNTLEY",
        state="IL",
        zip_code="60142",
        property_type="RENTAL_INVESTMENT",
    )
    session.add(institution)
    session.flush()
    session.add_all([checking, mortgage, prop])
    session.flush()
    session.add(
        LoanAccount(
            id="loan_conflict",
            financial_account_id=mortgage.id,
            property_id=prop.id,
            loan_number="CONFLICT-0001",
        )
    )
    session.flush()
    ensure_entity_ledger_accounts(session)

    document = Document(
        id=str(uuid4()),
        file_sha256="postingconflict" + uuid4().hex,
        original_filename="checking.pdf",
        storage_path="/tmp/checking.pdf",
        file_size_bytes=1,
    )
    session.add(document)
    session.flush()
    statement = Statement(
        id="stmt_conflict",
        document_id=document.id,
        financial_account_id=checking.id,
        statement_type="CHECKING",
        active_parser_name="BofACheckingParser",
        active_parser_version="1.0.0",
        statement_date=date(2026, 3, 31),
    )
    session.add(statement)
    session.commit()
    return {
        "account_id": checking.id,
        "statement_id": statement.id,
        "property_id": prop.id,
    }


def _add_txn(session: Session, context: dict, amount_cents: int, description: str) -> Transaction:
    txn = Transaction(
        id=str(uuid4()),
        statement_id=context["statement_id"],
        financial_account_id=context["account_id"],
        transaction_date=date(2026, 3, 3),
        amount_cents=amount_cents,
        direction="OUTFLOW" if amount_cents < 0 else "INFLOW",
        raw_description=description,
    )
    session.add(txn)
    session.add(
        TransactionProcessingState(
            transaction_id=txn.id,
            allocation_status="UNALLOCATED",
            allocated_cents=0,
            residual_cents=abs(amount_cents),
            posting_status="UNPOSTED",
        )
    )
    session.commit()
    session.refresh(txn)
    return txn


def _split(context: dict, category: str, cents: int) -> list[SplitItem]:
    return [
        SplitItem(
            property_id=context["property_id"],
            category=category,
            amount_cents=cents,
            tax_review_status=classification_status_for(False, False),
        )
    ]


def _lose_the_posting_projection(session: Session, txn_id: str) -> None:
    """Reproduce what an interrupted posting leaves behind."""
    state = session.get(TransactionProcessingState, txn_id)
    state.posting_status = "UNPOSTED"
    state.journal_entry_id = None
    session.add(state)
    session.commit()


def _posted_entries(session: Session, txn_id: str) -> int:
    return session.exec(
        select(func.count())
        .select_from(JournalEntry)
        .where(JournalEntry.source_transaction_id == txn_id)
        .where(JournalEntry.status == "POSTED")
    ).one()


# --- Domain ---------------------------------------------------------------


def test_posting_adopts_the_existing_entry_when_the_state_lost_it(session: Session):
    context = _mortgage_context(session)
    txn = _add_txn(session, context, -161812, "NSM DBAMR.COOPER DES:NSM DBAMR")
    apply_transaction_splits(session, txn.id, _split(context, "MORTGAGE", 161812))
    first = post_transaction_to_ledger(session, txn.id)

    _lose_the_posting_projection(session, txn.id)
    second = post_transaction_to_ledger(session, txn.id)

    assert second.id == first.id
    state = session.get(TransactionProcessingState, txn.id)
    assert state.posting_status == "POSTED"
    assert state.journal_entry_id == first.id
    assert _posted_entries(session, txn.id) == 1


def test_posting_refuses_an_allocation_the_ledger_does_not_carry(session: Session):
    context = _mortgage_context(session)
    txn = _add_txn(session, context, -161812, "NSM DBAMR.COOPER DES:NSM DBAMR")
    apply_transaction_splits(session, txn.id, _split(context, "MORTGAGE", 161812))
    post_transaction_to_ledger(session, txn.id)

    _lose_the_posting_projection(session, txn.id)
    apply_transaction_splits(session, txn.id, _split(context, "SUPPLIES", 161812))

    with pytest.raises(ValueError, match="does not match these allocations"):
        post_transaction_to_ledger(session, txn.id)

    assert _posted_entries(session, txn.id) == 1
    state = session.get(TransactionProcessingState, txn.id)
    assert state.posting_status == "UNPOSTED"


def test_the_migration_heals_states_left_behind_by_an_interrupted_posting(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'heal.db'}", connect_args={"check_same_thread": False}
    )
    event.listen(engine, "connect", configure_sqlite_connection)
    init_schema(engine)

    with Session(engine) as session:
        context = _mortgage_context(session)
        txn = _add_txn(session, context, -161812, "NSM DBAMR.COOPER DES:NSM DBAMR")
        apply_transaction_splits(session, txn.id, _split(context, "MORTGAGE", 161812))
        entry = post_transaction_to_ledger(session, txn.id)
        _lose_the_posting_projection(session, txn.id)
        txn_id, entry_id = txn.id, entry.id

    assert _heal_posted_transaction_states(engine) == 1

    with Session(engine) as session:
        state = session.get(TransactionProcessingState, txn_id)
        assert state.posting_status == "POSTED"
        assert state.journal_entry_id == entry_id


# --- Web ------------------------------------------------------------------


def test_assign_and_post_recovers_when_the_ledger_already_has_the_entry(owner, parse_html):
    signed_in = owner()
    with Session(signed_in.engine) as session:
        context = _mortgage_context(session)
        txn = _add_txn(session, context, -161812, "NSM DBAMR.COOPER DES:NSM DBAMR")
        apply_transaction_splits(session, txn.id, _split(context, "MORTGAGE", 161812))
        entry = post_transaction_to_ledger(session, txn.id)
        _lose_the_posting_projection(session, txn.id)
        txn_id, entry_id = txn.id, entry.id

    response = signed_in.post(
        "/reconciliation/assign",
        {
            "transaction_id": txn_id,
            "property_id": context["property_id"],
            "category": "MORTGAGE",
            "client_confirmed": "true",
        },
    )

    assert response.status_code == 200
    page = parse_html(response.text)
    assert "Assigned and posted to the ledger." in page.get_text()
    with Session(signed_in.engine) as session:
        state = session.get(TransactionProcessingState, txn_id)
        assert state.posting_status == "POSTED"
        assert state.journal_entry_id == entry_id


def test_assign_and_post_explains_a_conflicting_allocation(owner):
    signed_in = owner()
    with Session(signed_in.engine) as session:
        context = _mortgage_context(session)
        txn = _add_txn(session, context, -161812, "NSM DBAMR.COOPER DES:NSM DBAMR")
        apply_transaction_splits(session, txn.id, _split(context, "MORTGAGE", 161812))
        post_transaction_to_ledger(session, txn.id)
        _lose_the_posting_projection(session, txn.id)
        txn_id = txn.id

    response = signed_in.post(
        "/reconciliation/assign",
        {
            "transaction_id": txn_id,
            "property_id": context["property_id"],
            "category": "SUPPLIES",
            "client_confirmed": "true",
        },
        follow_redirects=False,
    )

    assert response.status_code == 409
    assert "does not match these allocations" in response.json()["detail"]

    # The refused posting must not have rewritten the allocation.
    with Session(signed_in.engine) as session:
        split = session.exec(
            select(TransactionSplit).where(TransactionSplit.transaction_id == txn_id)
        ).one()
        assert split.category == "MORTGAGE"
        assert session.get(TransactionProcessingState, txn_id).posting_status == "UNPOSTED"
