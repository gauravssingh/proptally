"""Phase 2.1 remediation §4: mixed-use status is an account property.

The CBD-2 gate must follow how a *card is used*, not the fact that it is a card.
Each case below is the acceptance table from the review.
"""

from datetime import date

import pytest
from sqlmodel import Session

from proptally.core.exceptions import GovernanceAuthorizationError
from proptally.db.models import (
    Document,
    FinancialAccount,
    FinancialInstitution,
    Property,
    Statement,
    Transaction,
)
from proptally.domain.accounting.metrics import calculate_property_financials
from proptally.domain.transactions.policy import (
    DEDICATED_RENTAL,
    MIXED_USE,
    UNCLASSIFIED,
    assert_posting_authorized,
    evaluate_posting_policy,
)
from proptally.domain.transactions.splits import (
    SplitItem,
    apply_transaction_splits,
    post_transaction_to_ledger,
)

LARKSPUR = "prop_tinsley_huntley"
ELDERBERRY = "prop_burkey_algonquin"
CHECKING = "acct_mu_chk"
DEDICATED = "acct_mu_dedicated"
SHARED = "acct_mu_shared"
UNKNOWN = "acct_mu_unknown"


@pytest.fixture(name="mu_env")
def mu_env_fixture(session: Session):
    bofa = FinancialInstitution(id="inst_mu", name="Bank")
    session.add(bofa)
    session.flush()
    for acct_id, name, acct_type, status in [
        (CHECKING, "Checking", "CHECKING", UNCLASSIFIED),
        (DEDICATED, "Rental-only card", "CREDIT_CARD", DEDICATED_RENTAL),
        (SHARED, "Shared card", "CREDIT_CARD", MIXED_USE),
        (UNKNOWN, "New card", "CREDIT_CARD", UNCLASSIFIED),
    ]:
        session.add(FinancialAccount(
            id=acct_id, institution_id=bofa.id, account_name=name, account_type=acct_type,
            account_number_masked="...0", mixed_use_status=status,
        ))
    session.add(Property(
        id=LARKSPUR, name="T", address_line1="a", city="c", zip_code="z",
        property_type="RENTAL_INVESTMENT",
    ))
    session.add(Property(
        id=ELDERBERRY, name="B", address_line1="b", city="c", zip_code="z",
        property_type="PRIMARY_RESIDENCE",
    ))
    doc = Document(
        id="doc_mu", file_sha256="sha_mu", original_filename="f.pdf",
        storage_path="/p", file_size_bytes=1,
    )
    session.add(doc)
    session.flush()
    session.add(Statement(
        id="stmt_mu", document_id=doc.id, financial_account_id=CHECKING,
        statement_type="CHECKING", active_parser_name="T", active_parser_version="1",
        statement_date=date(2026, 3, 31),
    ))
    session.commit()
    return session


def _txn(session, txn_id, account_id, amount_cents, counter=None):
    txn = Transaction(
        id=txn_id, statement_id="stmt_mu", financial_account_id=account_id,
        transaction_date=date(2026, 3, 12), amount_cents=amount_cents,
        direction="OUTFLOW" if amount_cents < 0 else "INFLOW",
        raw_description="POS PURCHASE 4451",  # deliberately uninformative
        counter_account_id=counter,
    )
    session.add(txn)
    session.commit()
    return txn


class _Split:
    def __init__(self, category, property_id=LARKSPUR):
        self.category = category
        self.property_id = property_id


def test_dedicated_rental_card_is_not_gated(mu_env):
    """A purpose-limited rental card must not be blocked for being a card."""
    txn = _txn(mu_env, "txn_mu_ded", DEDICATED, -40000)
    policy = evaluate_posting_policy(mu_env, txn, [_Split("REPAIRS")])
    assert policy.requires_authorization is False
    assert_posting_authorized(policy)  # does not raise


def test_mixed_use_card_requires_review(mu_env):
    txn = _txn(mu_env, "txn_mu_shared", SHARED, -40000)
    policy = evaluate_posting_policy(mu_env, txn, [_Split("REPAIRS")])
    assert policy.requires_cpa_review is True
    assert "mixed-use" in " ".join(policy.reasons)
    with pytest.raises(GovernanceAuthorizationError, match="CBD-2 Policy Violation"):
        assert_posting_authorized(policy)
    assert_posting_authorized(policy, user_attested_cpa_review=True)


def test_unclassified_card_requires_review_and_is_never_assumed_dedicated(mu_env):
    """Unknown usage is the safe default: it must not silently pass as rental-only."""
    txn = _txn(mu_env, "txn_mu_unknown", UNKNOWN, -40000)
    policy = evaluate_posting_policy(mu_env, txn, [_Split("REPAIRS")])
    assert policy.requires_cpa_review is True
    assert "not classified as dedicated rental use" in " ".join(policy.reasons)
    with pytest.raises(GovernanceAuthorizationError):
        assert_posting_authorized(policy)


def test_checking_account_rental_expense_is_not_subject_to_the_card_rule(mu_env):
    txn = _txn(mu_env, "txn_mu_chk", CHECKING, -40000)
    policy = evaluate_posting_policy(mu_env, txn, [_Split("REPAIRS")])
    assert policy.requires_authorization is False


def test_personal_card_spend_is_not_rental_expense(mu_env):
    """A shared-card charge allocated entirely to personal is not a rental deduction."""
    txn = _txn(mu_env, "txn_mu_personal", SHARED, -40000)
    policy = evaluate_posting_policy(mu_env, txn, [_Split("PERSONAL", property_id=None)])
    assert policy.requires_authorization is False

    apply_transaction_splits(mu_env, txn.id, [
        SplitItem(property_id=None, category="PERSONAL", amount_cents=40000,
                  tax_review_status="CLIENT_CONFIRMED")
    ])
    post_transaction_to_ledger(mu_env, txn.id)

    financials = calculate_property_financials(mu_env, property_id=LARKSPUR)
    assert financials["repairs_cents"] == 0
    assert financials["total_operating_expenses_cents"] == 0
    assert financials["untagged_personal_draw_cents"] == 40000


def test_mixed_use_status_defaults_to_unclassified(mu_env):
    """A card created without an explicit classification is never dedicated rental."""
    fresh = FinancialAccount(
        id="acct_mu_default", institution_id="inst_mu", account_name="Unclassified",
        account_type="CREDIT_CARD", account_number_masked="...0",
    )
    mu_env.add(fresh)
    mu_env.commit()
    assert fresh.mixed_use_status == UNCLASSIFIED

    txn = _txn(mu_env, "txn_mu_default", "acct_mu_default", -40000)
    policy = evaluate_posting_policy(mu_env, txn, [_Split("REPAIRS")])
    assert policy.requires_cpa_review is True


# --- CBD-2 must distinguish a rental deduction from a personal expense -------

def test_primary_residence_repair_is_not_a_rental_deduction(mu_env):
    """A repair on the primary residence is not a Schedule E rental deduction."""
    txn = _txn(mu_env, "txn_mu_rowan", SHARED, -40000)
    policy = evaluate_posting_policy(mu_env, txn, [_Split("REPAIRS", property_id=ELDERBERRY)])
    assert policy.requires_cpa_review is False
    # Not a gate, but a deduction category on a shared card still gets the hint.
    assert policy.may_require_review is True
    assert_posting_authorized(policy)  # does not raise


def test_unattributed_personal_spend_is_not_gated_and_gets_no_hint(mu_env):
    """PERSONAL with no door is plainly not a rental deduction."""
    txn = _txn(mu_env, "txn_mu_noprop_personal", SHARED, -40000)
    policy = evaluate_posting_policy(mu_env, txn, [_Split("PERSONAL", property_id=None)])
    assert policy.requires_cpa_review is False
    assert policy.may_require_review is False
    assert policy.should_offer_review is False
    assert_posting_authorized(policy)


def test_unattributed_repair_on_a_shared_card_hints_but_does_not_gate(mu_env):
    """No door attribution yet: not a rental deduction, but it may become one."""
    txn = _txn(mu_env, "txn_mu_noprop_repairs", SHARED, -40000)
    policy = evaluate_posting_policy(mu_env, txn, [_Split("REPAIRS", property_id=None)])
    assert policy.requires_cpa_review is False
    assert policy.may_require_review is True
    assert_posting_authorized(policy)


def test_rental_repair_on_a_shared_card_is_gated(mu_env):
    """The documented CBD-2 case: an expense against a rental door."""
    txn = _txn(mu_env, "txn_mu_rental_repairs", SHARED, -40000)
    policy = evaluate_posting_policy(mu_env, txn, [_Split("REPAIRS", property_id=LARKSPUR)])
    assert policy.requires_cpa_review is True
    with pytest.raises(GovernanceAuthorizationError, match="CBD-2 Policy Violation"):
        assert_posting_authorized(policy)


def test_mixed_allocation_is_gated_by_its_rental_portion(mu_env):
    """One rental deduction in the allocation is enough to require review."""
    txn = _txn(mu_env, "txn_mu_mixed", SHARED, -40000)
    policy = evaluate_posting_policy(mu_env, txn, [
        _Split("PERSONAL", property_id=None),
        _Split("REPAIRS", property_id=LARKSPUR),
    ])
    assert policy.requires_cpa_review is True
