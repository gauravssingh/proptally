"""Tests for transaction splitting service, positive split amounts, and exact reconciliation invariants."""

from datetime import date
from uuid import uuid4
import pytest
from pydantic import ValidationError
from sqlmodel import Session, select
from proptally.db.models import (
    Document,
    FinancialInstitution,
    FinancialAccount,
    Statement,
    Property,
    Transaction,
    TransactionSplit,
    TransactionProcessingState,
)
from proptally.domain.transactions.splits import apply_transaction_splits, SplitItem
from proptally.core.exceptions import InvalidSplitSumError


@pytest.fixture(name="sample_context")
def sample_context_fixture(session: Session):
    """Sets up prerequisite document, institution, account, statement, and property."""
    doc = Document(
        id=str(uuid4()),
        file_sha256="abc123sha256hash",
        original_filename="bofa_stmt_may2026.pdf",
        storage_path="/data/statements/bofa_stmt_may2026.pdf",
        file_size_bytes=102400,
    )
    session.add(doc)

    inst = FinancialInstitution(
        id="inst_bofa",
        name="Bank of America",
    )
    session.add(inst)

    prop = Property(
        id="prop_tinsley_huntley",
        name="12307 Larkspur St",
        address_line1="12307 Larkspur St",
        city="Westbrook",
        zip_code="60196",
    )
    session.add(prop)
    session.flush()

    acct = FinancialAccount(
        id="acct_bofa_chk_9456",
        institution_id=inst.id,
        account_name="Advantage Banking",
        account_type="CHECKING",
        account_number_masked="...9456",
    )
    session.add(acct)
    session.flush()

    stmt = Statement(
        id="stmt_bofa_202605",
        document_id=doc.id,
        financial_account_id=acct.id,
        statement_type="CHECKING",
        active_parser_name="BofACheckingParser",
        active_parser_version="1.0.0",
        statement_date=date(2026, 5, 31),
    )
    session.add(stmt)
    session.commit()
    return {
        "statement_id": stmt.id,
        "account_id": acct.id,
        "property_id": prop.id,
    }


def create_txn(
    session: Session,
    context: dict,
    amount_cents: int,
    description: str,
) -> Transaction:
    direction = "INFLOW" if amount_cents > 0 else "OUTFLOW"
    txn = Transaction(
        id=str(uuid4()),
        statement_id=context["statement_id"],
        financial_account_id=context["account_id"],
        transaction_date=date(2026, 5, 15),
        amount_cents=amount_cents,
        direction=direction,
        raw_description=description,
    )
    session.add(txn)
    session.commit()
    session.refresh(txn)
    return txn


def test_exact_split_outflow_expense(session: Session, sample_context: dict):
    """Verifies that a signed outflow (-$150.00) is allocated to strictly positive splits totaling $150.00."""
    txn = create_txn(session, sample_context, amount_cents=-15000, description="Home Depot Store #1920")

    splits = [
        SplitItem(
            property_id=sample_context["property_id"],
            category="REPAIRS",
            amount_cents=10000,  # $100.00 strictly positive
            schedule_e_line=14,
            notes="Plumbing supplies",
        ),
        SplitItem(
            property_id=sample_context["property_id"],
            category="SUPPLIES",
            amount_cents=5000,  # $50.00 strictly positive
            schedule_e_line=17,
            notes="Paint brushes and tarps",
        ),
    ]

    proc_state = apply_transaction_splits(session, txn.id, splits)

    assert proc_state.allocation_status == "FULLY_ALLOCATED"
    assert proc_state.allocated_cents == 15000
    assert proc_state.residual_cents == 0

    saved_splits = session.exec(
        select(TransactionSplit).where(TransactionSplit.transaction_id == txn.id)
    ).all()
    assert len(saved_splits) == 2
    assert all(s.amount_cents > 0 for s in saved_splits)


def test_exact_split_inflow_rent_and_fee(session: Session, sample_context: dict):
    """Verifies that a signed inflow (+$2,100.00) is allocated to positive splits totaling $2,100.00."""
    txn = create_txn(session, sample_context, amount_cents=210000, description="Zelle payment from tenant")

    splits = [
        SplitItem(
            property_id=sample_context["property_id"],
            category="RENT_INCOME",
            amount_cents=200000,  # $2,000.00 rent
            schedule_e_line=3,
        ),
        SplitItem(
            property_id=sample_context["property_id"],
            category="FEE_INCOME",
            amount_cents=10000,   # $100.00 late fee
            schedule_e_line=3,
        ),
    ]

    proc_state = apply_transaction_splits(session, txn.id, splits)

    assert proc_state.allocation_status == "FULLY_ALLOCATED"
    assert proc_state.allocated_cents == 210000
    assert proc_state.residual_cents == 0


def test_partial_split_leaves_unallocated_residual(session: Session, sample_context: dict):
    """Verifies that allow_partial=True leaves a residual unallocated balance."""
    txn = create_txn(session, sample_context, amount_cents=-20000, description="Menards lumber & tools")

    splits = [
        SplitItem(
            property_id=sample_context["property_id"],
            category="REPAIRS",
            amount_cents=12000,  # $120.00 allocated of $200.00
            schedule_e_line=14,
        ),
    ]

    proc_state = apply_transaction_splits(session, txn.id, splits, allow_partial=True)

    assert proc_state.allocation_status == "PARTIALLY_ALLOCATED"
    assert proc_state.allocated_cents == 12000
    assert proc_state.residual_cents == 8000  # $80.00 residual


def test_over_allocation_raises_invalid_split_sum_error(session: Session, sample_context: dict):
    """Negative test: Allocating more than the absolute transaction amount raises InvalidSplitSumError."""
    txn = create_txn(session, sample_context, amount_cents=-10000, description="Hardware store")

    splits = [
        SplitItem(
            property_id=sample_context["property_id"],
            category="REPAIRS",
            amount_cents=12000,  # $120.00 exceeds $100.00
        ),
    ]

    with pytest.raises(InvalidSplitSumError) as exc_info:
        apply_transaction_splits(session, txn.id, splits)
    assert "exceeds parent transaction total" in str(exc_info.value)


def test_under_allocation_rejected_when_not_partial(session: Session, sample_context: dict):
    """Negative test: If allow_partial is False (default), partial sum raises InvalidSplitSumError."""
    txn = create_txn(session, sample_context, amount_cents=-10000, description="Hardware store")

    splits = [
        SplitItem(
            property_id=sample_context["property_id"],
            category="REPAIRS",
            amount_cents=7000,  # $70.00 is less than $100.00
        ),
    ]

    with pytest.raises(InvalidSplitSumError) as exc_info:
        apply_transaction_splits(session, txn.id, splits, allow_partial=False)
    assert "does not equal parent transaction total" in str(exc_info.value)


def test_zero_or_negative_split_amount_rejected_by_pydantic():
    """Negative test: SplitItem cannot be instantiated with zero or negative cents."""
    with pytest.raises(ValidationError):
        SplitItem(category="REPAIRS", amount_cents=0)

    with pytest.raises(ValidationError):
        SplitItem(category="REPAIRS", amount_cents=-500)


def test_mixed_personal_and_business_split(session: Session, sample_context: dict):
    """Verifies that an outflow on a mixed-use card can be split cleanly between business and personal."""
    txn = create_txn(session, sample_context, amount_cents=-50000, description="Apple Store & supplies")

    splits = [
        SplitItem(
            property_id=sample_context["property_id"],
            category="REPAIRS",
            amount_cents=35000,  # $350.00 Business
            schedule_e_line=14,
        ),
        SplitItem(
            property_id=None,    # Personal has no property_id
            category="PERSONAL",
            amount_cents=15000,  # $150.00 Personal
        ),
    ]

    proc_state = apply_transaction_splits(session, txn.id, splits)

    assert proc_state.allocation_status == "FULLY_ALLOCATED"
    assert proc_state.allocated_cents == 50000
    assert proc_state.residual_cents == 0


def test_resplitting_replaces_previous_splits(session: Session, sample_context: dict):
    """Verifies that re-splitting replaces old splits and updates the processing state correctly."""
    txn = create_txn(session, sample_context, amount_cents=-10000, description="Store purchase")

    # Initial split: 10000 as REPAIRS
    apply_transaction_splits(session, txn.id, [
        SplitItem(property_id=sample_context["property_id"], category="REPAIRS", amount_cents=10000)
    ])

    # Re-split: 6000 REPAIRS + 4000 SUPPLIES
    new_state = apply_transaction_splits(session, txn.id, [
        SplitItem(property_id=sample_context["property_id"], category="REPAIRS", amount_cents=6000),
        SplitItem(property_id=sample_context["property_id"], category="SUPPLIES", amount_cents=4000),
    ])

    assert new_state.allocation_status == "FULLY_ALLOCATED"
    assert new_state.allocated_cents == 10000

    saved_splits = session.exec(
        select(TransactionSplit).where(TransactionSplit.transaction_id == txn.id)
    ).all()
    assert len(saved_splits) == 2
    assert {s.category for s in saved_splits} == {"REPAIRS", "SUPPLIES"}
