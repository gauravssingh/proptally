"""Bulk classification from the review queue."""

from sqlmodel import Session
from test_posting_conflicts import _add_txn, _mortgage_context

from proptally.db.models import TransactionProcessingState


def test_bulk_classify_posts_ungated_rows_and_holds_gated_ones(owner):
    signed_in = owner()
    with Session(signed_in.engine) as session:
        context = _mortgage_context(session)
        plain = _add_txn(session, context, -2500, "HERTZ RENT-A-CAR")
        gated = _add_txn(session, context, -5000, "ATM CASH DEPOSIT FEE")
        gated.requires_client_confirmation = True
        session.add(gated)
        session.commit()
        plain_id, gated_id = plain.id, gated.id

    response = signed_in.post(
        "/reconciliation/bulk-classify",
        {
            "transaction_id": [plain_id, gated_id],
            "property_id": "",
            "category": "PERSONAL",
        },
    )

    assert response.status_code == 200
    assert "posted 1" in response.text
    assert "held 1" in response.text

    with Session(signed_in.engine) as session:
        assert session.get(TransactionProcessingState, plain_id).posting_status == "POSTED"
        held = session.get(TransactionProcessingState, gated_id)
        assert held.allocation_status == "FULLY_ALLOCATED"
        assert held.posting_status == "UNPOSTED"
