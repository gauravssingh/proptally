"""Review remediation: posting authorization evidence survives independently.

The review found that when a transaction needed both gates, the persisted split
status collapsed to CLIENT_CONFIRMED and the CBD-2 self-attestation was lost from
the record. Enforcement was correct; the *evidence* was not. These tests prove
both authorizations, their timestamps and the posting they permitted are readable
back from the database after posting and after reclassification.
"""

from datetime import date

import pytest
from sqlalchemy import text
from sqlmodel import Session, select

from proptally.db.models import (
    Document,
    FinancialAccount,
    FinancialInstitution,
    PostingAuthorization,
    Property,
    Statement,
    Transaction,
    TransactionSplit,
)
from proptally.domain.transactions.splits import (
    SplitItem,
    apply_transaction_splits,
    post_transaction_to_ledger,
    reclassify_and_repost_transaction,
)

LARKSPUR = "prop_tinsley_huntley"
CHECKING = "acct_ev_chk"
SHARED_CARD = "acct_ev_card"
DEDUCTION_CENTS = 12900


@pytest.fixture(name="ev_env")
def ev_env_fixture(session: Session):
    bank = FinancialInstitution(id="inst_ev", name="Bank")
    session.add(bank)
    session.flush()
    session.add(FinancialAccount(
        id=CHECKING, institution_id=bank.id, account_name="Checking",
        account_type="CHECKING", account_number_masked="...1",
    ))
    session.add(FinancialAccount(
        id=SHARED_CARD, institution_id=bank.id, account_name="Shared card",
        account_type="CREDIT_CARD", account_number_masked="...2",
        mixed_use_status="MIXED_USE",
    ))
    session.add(Property(
        id=LARKSPUR, name="Larkspur", address_line1="a", city="Westbrook", zip_code="60196",
        property_type="RENTAL_INVESTMENT",
    ))
    doc = Document(id="doc_ev", file_sha256="sha_ev", original_filename="f.pdf",
                   storage_path="/p", file_size_bytes=1)
    session.add(doc)
    session.flush()
    session.add(Statement(
        id="stmt_ev", document_id=doc.id, financial_account_id=CHECKING,
        statement_type="CHECKING", active_parser_name="T", active_parser_version="1",
        statement_date=date(2026, 3, 31),
    ))
    session.commit()
    return {"session": session}


def _both_gates_txn(session, txn_id, account_id=SHARED_CARD):
    """A transaction whose policy demands client confirmation AND external review."""
    txn = Transaction(
        id=txn_id,
        statement_id="stmt_ev",
        financial_account_id=account_id,
        transaction_date=date(2026, 3, 22),
        amount_cents=-DEDUCTION_CENTS,
        direction="OUTFLOW",
        raw_description="POS PURCHASE 4451",
        requires_client_confirmation=True,
        requires_cpa_review=True,
    )
    session.add(txn)
    session.commit()
    return txn


def test_both_authorizations_are_persisted_independently(session: Session, ev_env):
    """Posting with both flags must leave both visible, not one collapsed."""
    _both_gates_txn(session, "txn_evidence_both")
    apply_transaction_splits(session, "txn_evidence_both", [
        SplitItem(property_id=LARKSPUR, category="SUPPLIES", amount_cents=DEDUCTION_CENTS,
                  tax_review_status="CLIENT_CONFIRMED+USER_ATTESTED_CPA_REVIEW")
    ])

    entry = post_transaction_to_ledger(
        session, "txn_evidence_both", client_confirmed=True, user_attested_cpa_review=True
    )

    records = session.exec(
        select(PostingAuthorization).where(
            PostingAuthorization.transaction_id == "txn_evidence_both"
        )
    ).all()
    assert len(records) == 1
    record = records[0]

    # Both authorizations, independently, with their own timestamps.
    assert record.client_confirmed is True
    assert record.client_confirmed_at is not None
    assert record.user_attested_cpa_review is True
    assert record.user_attested_cpa_review_at is not None

    # What the policy required at the time is recorded alongside what was given.
    assert record.required_client_confirmation is True
    assert record.required_cpa_review is True

    # The posting it permitted and the allocation it applied to.
    assert record.journal_entry_id == entry.id
    assert record.split_count == 1
    assert record.split_total_cents == DEDUCTION_CENTS
    assert record.policy_reasons


def test_self_attestation_survives_alongside_client_confirmation(session: Session, ev_env):
    """The split summary must not present the attestation as client confirmation."""
    _both_gates_txn(session, "txn_evidence_status")
    apply_transaction_splits(session, "txn_evidence_status", [
        SplitItem(property_id=LARKSPUR, category="SUPPLIES", amount_cents=DEDUCTION_CENTS,
                  tax_review_status="CLIENT_CONFIRMED+USER_ATTESTED_CPA_REVIEW")
    ])
    post_transaction_to_ledger(
        session, "txn_evidence_status", client_confirmed=True, user_attested_cpa_review=True
    )

    split = session.exec(
        select(TransactionSplit).where(TransactionSplit.transaction_id == "txn_evidence_status")
    ).one()
    assert "USER_ATTESTED_CPA_REVIEW" in split.tax_review_status
    assert split.tax_review_status != "CLIENT_CONFIRMED"  # not collapsed


def test_authorization_evidence_is_immutable(session: Session, ev_env):
    """The record of who authorized what must not be editable or removable."""
    _both_gates_txn(session, "txn_evidence_immutable")
    apply_transaction_splits(session, "txn_evidence_immutable", [
        SplitItem(property_id=LARKSPUR, category="SUPPLIES", amount_cents=DEDUCTION_CENTS,
                  tax_review_status="CLIENT_CONFIRMED+USER_ATTESTED_CPA_REVIEW")
    ])
    post_transaction_to_ledger(
        session, "txn_evidence_immutable", client_confirmed=True, user_attested_cpa_review=True
    )
    record = session.exec(
        select(PostingAuthorization).where(
            PostingAuthorization.transaction_id == "txn_evidence_immutable"
        )
    ).one()

    engine = session.get_bind()
    with pytest.raises(Exception, match="immutable"), engine.begin() as conn:
        conn.execute(text("UPDATE posting_authorizations SET client_confirmed = 0"))
    with pytest.raises(Exception, match="immutable"), engine.begin() as conn:
        conn.execute(text("DELETE FROM posting_authorizations"))

    session.refresh(record)
    assert record.client_confirmed is True and record.user_attested_cpa_review is True


def test_evidence_survives_reclassification(session: Session, ev_env):
    """A reclassification appends evidence for the replacement posting."""
    _both_gates_txn(session, "txn_evidence_reclass")
    apply_transaction_splits(session, "txn_evidence_reclass", [
        SplitItem(property_id=LARKSPUR, category="SUPPLIES", amount_cents=DEDUCTION_CENTS,
                  tax_review_status="CLIENT_CONFIRMED+USER_ATTESTED_CPA_REVIEW")
    ])
    first = post_transaction_to_ledger(
        session, "txn_evidence_reclass", client_confirmed=True, user_attested_cpa_review=True
    )

    second = reclassify_and_repost_transaction(
        session, "txn_evidence_reclass",
        [SplitItem(property_id=LARKSPUR, category="REPAIRS", amount_cents=DEDUCTION_CENTS,
                   tax_review_status="CLIENT_CONFIRMED+USER_ATTESTED_CPA_REVIEW")],
        client_confirmed=True,
        user_attested_cpa_review=True,
    )

    records = session.exec(
        select(PostingAuthorization)
        .where(PostingAuthorization.transaction_id == "txn_evidence_reclass")
        .order_by(PostingAuthorization.created_at)
    ).all()
    assert len(records) == 2
    # Each authorization points at the posting it permitted.
    assert {r.journal_entry_id for r in records} == {first.id, second.id}
    for record in records:
        assert record.client_confirmed and record.user_attested_cpa_review
        assert record.client_confirmed_at and record.user_attested_cpa_review_at
