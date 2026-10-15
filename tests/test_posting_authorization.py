"""Phase 2.1 review remediation: CBD-1 / CBD-2 posting authorization.

Two layers are covered:

* the domain policy inputs, proving authorization does not hinge on wording alone
* every HTTP posting path, proving a caller cannot bypass governance by choosing
  a different endpoint
"""

from datetime import date

import pytest
from sqlmodel import Session, select

from proptally.core.exceptions import GovernanceAuthorizationError
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
from proptally.domain.transactions.policy import (
    assert_posting_authorized,
    evaluate_posting_policy,
)
from proptally.domain.transactions.splits import post_transaction_to_ledger

CBD1_TXN = "txn_atm_deposit_cbd1_20260320"
CBD2_TXN = "txn_apple_store_cbd2_20260322"
LARKSPUR = "prop_tinsley_huntley"


# --- Policy inputs: durable flags and structure, not wording ---------------

@pytest.fixture(name="policy_env")
def policy_env_fixture(session: Session):
    bofa = FinancialInstitution(id="inst_pol", name="Bank")
    session.add(bofa)
    session.flush()
    session.add(FinancialAccount(
        id="acct_pol_chk", institution_id=bofa.id, account_name="Checking",
        account_type="CHECKING", account_number_masked="...1",
    ))
    session.add(FinancialAccount(
        id="acct_pol_card", institution_id=bofa.id, account_name="Card",
        account_type="CREDIT_CARD", account_number_masked="...2",
    ))
    session.add(Property(
        id=LARKSPUR, name="T", address_line1="a", city="c", zip_code="z",
        property_type="RENTAL_INVESTMENT",
    ))
    doc = Document(
        id="doc_pol", file_sha256="sha_pol", original_filename="f.pdf",
        storage_path="/p", file_size_bytes=1,
    )
    session.add(doc)
    session.flush()
    session.add(Statement(
        id="stmt_pol", document_id=doc.id, financial_account_id="acct_pol_chk",
        statement_type="CHECKING", active_parser_name="T", active_parser_version="1",
        statement_date=date(2026, 3, 31),
    ))
    session.commit()
    session.info["statement_id"] = "stmt_pol"
    return session


def _txn(session, txn_id, account_id, description, **flags):
    txn = Transaction(
        id=txn_id, statement_id=session.info["statement_id"],
        financial_account_id=account_id,
        transaction_date=date(2026, 3, 1), amount_cents=10000, direction="INFLOW",
        raw_description=description, **flags,
    )
    session.add(txn)
    session.commit()
    return txn


class _Split:
    """Minimal duck type for a split allocation."""

    def __init__(self, category, property_id=LARKSPUR):
        self.category = category
        self.property_id = property_id


def test_cbd1_gate_comes_from_the_durable_flag_not_the_wording(policy_env):
    """An innocuous description still requires confirmation when the record says so."""
    txn = _txn(policy_env, "t_flag", "acct_pol_chk", "Deposit 00123",
               requires_client_confirmation=True)
    policy = evaluate_posting_policy(policy_env, txn, [_Split("RENT_INCOME")])
    assert policy.requires_client_confirmation is True
    with pytest.raises(GovernanceAuthorizationError, match="CBD-1 Policy Violation"):
        assert_posting_authorized(policy, client_confirmed=False)


def test_description_alone_does_not_create_a_cbd2_requirement(policy_env):
    """A deposit account expense mentioning nothing is not a mixed-use card charge."""
    txn = _txn(policy_env, "t_plain", "acct_pol_chk", "Repair supplies")
    policy = evaluate_posting_policy(policy_env, txn, [_Split("REPAIRS")])
    assert policy.requires_authorization is False


def test_cbd2_gate_is_detected_structurally_from_a_credit_card_deduction(policy_env):
    """Credit-card funded rental deduction is mixed-use with no wording needed at all."""
    txn = _txn(policy_env, "t_card", "acct_pol_card", "POS PURCHASE 4451")
    policy = evaluate_posting_policy(policy_env, txn, [_Split("REPAIRS")])
    assert policy.requires_cpa_review is True
    assert policy.requires_client_confirmation is False
    with pytest.raises(GovernanceAuthorizationError, match="CBD-2 Policy Violation"):
        assert_posting_authorized(policy, user_attested_cpa_review=False, client_confirmed=False)


def test_personal_card_spend_is_not_mixed_use(policy_env):
    """A card charge allocated entirely to personal is not a rental deduction."""
    txn = _txn(policy_env, "t_card_personal", "acct_pol_card", "POS PURCHASE 4452")
    policy = evaluate_posting_policy(policy_env, txn, [_Split("PERSONAL", property_id=None)])
    assert policy.requires_cpa_review is False


def test_split_review_status_is_not_an_authorization_grant(policy_env):
    """Writing CLIENT_CONFIRMED onto a split cannot unlock a gated posting."""
    txn = _txn(policy_env, "t_no_grant", "acct_pol_chk", "ATM CASH DEPOSIT #1")
    policy = evaluate_posting_policy(policy_env, txn, [_Split("RENT_INCOME")])
    assert policy.requires_client_confirmation is True
    with pytest.raises(GovernanceAuthorizationError):
        assert_posting_authorized(policy, client_confirmed=False, user_attested_cpa_review=False)
    # Only the explicit parameter opens it.
    assert_posting_authorized(policy, client_confirmed=True)


# --- Every HTTP posting path enforces the same rules -----------------------

@pytest.fixture(name="client")
def client_fixture(tmp_path):
    from starlette.testclient import TestClient

    from proptally.web.app import create_app

    db_path = tmp_path / "auth.db"
    app = create_app(db_url=f"sqlite:///{db_path}")
    with TestClient(app) as test_client:
        test_client.db_url = f"sqlite:///{db_path}"
        yield test_client


def _open_session(client):
    from proptally.db.connection import get_engine
    return Session(get_engine(client.db_url))


def _posting_state(client, txn_id):
    with _open_session(client) as session:
        state = session.get(TransactionProcessingState, txn_id)
        return (state.allocation_status, state.posting_status) if state else (None, None)


def test_register_split_route_requires_and_honours_client_confirmation(client):
    """POST /transactions/split cannot post a CBD-1 item without confirmation."""
    rejected = client.post(
        "/transactions/split",
        data={
            "transaction_id": CBD1_TXN,
            "property_id[]": "prop_cimmaron_pingree",
            "category[]": "RENT_INCOME",
            "amount_dollars[]": "1500.00",
            "post_to_ledger": "true",
        },
    )
    assert rejected.status_code == 403
    assert "CBD-1" in rejected.text
    assert _posting_state(client, CBD1_TXN)[1] != "POSTED"

    accepted = client.post(
        "/transactions/split",
        data={
            "transaction_id": CBD1_TXN,
            "property_id[]": "prop_cimmaron_pingree",
            "category[]": "RENT_INCOME",
            "amount_dollars[]": "1500.00",
            "post_to_ledger": "true",
            "client_confirmed": "true",
        },
        follow_redirects=True,
    )
    assert accepted.status_code == 200
    assert _posting_state(client, CBD1_TXN)[1] == "POSTED"


def test_register_split_route_blocks_a_mixed_use_card_charge_without_review(client):
    """The structural CBD-2 rule applies on the register too."""
    rejected = client.post(
        "/transactions/split",
        data={
            "transaction_id": CBD2_TXN,
            "property_id[]": LARKSPUR,
            "category[]": "SUPPLIES",
            "amount_dollars[]": "129.00",
            "post_to_ledger": "true",
        },
    )
    assert rejected.status_code == 403
    assert "CBD-2" in rejected.text
    assert _posting_state(client, CBD2_TXN)[1] != "POSTED"

    accepted = client.post(
        "/transactions/split",
        data={
            "transaction_id": CBD2_TXN,
            "property_id[]": LARKSPUR,
            "category[]": "SUPPLIES",
            "amount_dollars[]": "129.00",
            "post_to_ledger": "true",
            "user_attested_cpa_review": "true",
        },
        follow_redirects=True,
    )
    assert accepted.status_code == 200
    assert _posting_state(client, CBD2_TXN)[1] == "POSTED"


def test_reconciliation_route_enforces_the_same_rules(client):
    """POST /reconciliation/assign is governed identically."""
    rejected = client.post(
        "/reconciliation/assign",
        data={
            "transaction_id": CBD2_TXN,
            "property_id": LARKSPUR,
            "category": "SUPPLIES",
        },
    )
    assert rejected.status_code == 403
    assert "CBD-2" in rejected.text
    # Nothing was mutated by the rejected request.
    assert _posting_state(client, CBD2_TXN)[0] == "UNALLOCATED"
    with _open_session(client) as session:
        assert session.exec(
            select(TransactionSplit).where(TransactionSplit.transaction_id == CBD2_TXN)
        ).all() == []

    accepted = client.post(
        "/reconciliation/assign",
        data={
            "transaction_id": CBD2_TXN,
            "property_id": LARKSPUR,
            "category": "SUPPLIES",
            "user_attested_cpa_review": "true",
        },
        follow_redirects=True,
    )
    assert accepted.status_code == 200
    assert _posting_state(client, CBD2_TXN)[1] == "POSTED"


def test_client_confirmation_does_not_satisfy_the_cbd2_gate(client):
    """CBD-2 is an external-review gate; the client cannot authorize it alone."""
    rejected = client.post(
        "/reconciliation/assign",
        data={
            "transaction_id": CBD2_TXN,
            "property_id": LARKSPUR,
            "category": "SUPPLIES",
            "client_confirmed": "true",
        },
    )
    assert rejected.status_code == 403
    assert "Client confirmation does not satisfy this gate" in rejected.text
    assert _posting_state(client, CBD2_TXN)[1] != "POSTED"

    # The CBD-1 gate keeps its own semantics: there, client confirmation is the gate.
    cbd1_ok = client.post(
        "/transactions/split",
        data={
            "transaction_id": CBD1_TXN,
            "property_id[]": "prop_cimmaron_pingree",
            "category[]": "RENT_INCOME",
            "amount_dollars[]": "1500.00",
            "post_to_ledger": "true",
            "client_confirmed": "true",
        },
        follow_redirects=True,
    )
    assert cbd1_ok.status_code == 200
    assert _posting_state(client, CBD1_TXN)[1] == "POSTED"


def test_direct_post_route_enforces_the_same_rules(client):
    """POST /transactions/{id}/post cannot be used to sidestep the gates."""
    # Allocate the CBD-1 item without posting it.
    allocated = client.post(
        "/transactions/split",
        data={
            "transaction_id": CBD1_TXN,
            "property_id[]": "prop_cimmaron_pingree",
            "category[]": "RENT_INCOME",
            "amount_dollars[]": "1500.00",
        },
        follow_redirects=True,
    )
    assert allocated.status_code == 200
    assert _posting_state(client, CBD1_TXN) == ("FULLY_ALLOCATED", "UNPOSTED")

    rejected = client.post(f"/transactions/{CBD1_TXN}/post")
    assert rejected.status_code == 403
    assert "CBD-1" in rejected.text
    assert _posting_state(client, CBD1_TXN)[1] == "UNPOSTED"

    accepted = client.post(
        f"/transactions/{CBD1_TXN}/post",
        data={"client_confirmed": "true"},
        follow_redirects=True,
    )
    assert accepted.status_code == 200
    assert _posting_state(client, CBD1_TXN)[1] == "POSTED"


def test_domain_service_is_the_single_enforcement_point(client):
    """Bypassing the routes entirely still cannot post without authorization."""
    from proptally.domain.transactions.splits import SplitItem, apply_transaction_splits

    with _open_session(client) as session:
        txn = session.get(Transaction, CBD1_TXN)
        assert txn.requires_client_confirmation is True
        # Fully allocate it, marking the split as client confirmed.
        apply_transaction_splits(session, CBD1_TXN, [SplitItem(
            property_id="prop_cimmaron_pingree", category="RENT_INCOME",
            amount_cents=150000, tax_review_status="CLIENT_CONFIRMED",
        )])
        # The split status is not a grant: the explicit parameter is still required.
        with pytest.raises(GovernanceAuthorizationError, match="CBD-1 Policy Violation"):
            post_transaction_to_ledger(session, CBD1_TXN, client_confirmed=False)
        assert session.get(TransactionProcessingState, CBD1_TXN).posting_status == "UNPOSTED"
