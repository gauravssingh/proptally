"""Phase 2.1 remediation §1: credit-card settlement end to end.

The scenario runs entirely through the application's own services — a card
purchase, then a bank payment to that card recorded as a transaction with a
counter account. Nothing here hand-builds a settlement journal entry, so it
demonstrates that the normal workflow produces the ledger the metrics expect.
"""

from datetime import date

import pytest
from sqlmodel import Session, select

from proptally.core.exceptions import GovernanceAuthorizationError
from proptally.db.models import (
    Document,
    FinancialAccount,
    FinancialInstitution,
    JournalEntry,
    JournalEntryLine,
    Property,
    Statement,
    Transaction,
    TransactionProcessingState,
)
from proptally.domain.accounting.ledger import (
    add_journal_line,
    create_journal_entry,
    is_entry_reversed,
    post_journal_entry,
)
from proptally.domain.accounting.metrics import calculate_property_financials
from proptally.domain.transactions.policy import is_card_settlement
from proptally.domain.transactions.splits import (
    SplitItem,
    apply_transaction_splits,
    post_transaction_to_ledger,
    reclassify_and_repost_transaction,
)

LARKSPUR = "prop_tinsley_huntley"
ELDERBERRY = "prop_burkey_algonquin"
CHECKING = "acct_settle_chk"
MIXED_CARD = "acct_settle_visa"
DEDICATED_CARD = "acct_settle_rental_card"

RENTAL_400 = 40000
PERSONAL_200 = 20000
PAYMENT_600 = 60000


@pytest.fixture(name="settle_env")
def settle_env_fixture(session: Session):
    bofa = FinancialInstitution(id="inst_settle", name="Bank")
    session.add(bofa)
    session.flush()

    session.add(FinancialAccount(
        id=CHECKING, institution_id=bofa.id, account_name="Checking",
        account_type="CHECKING", account_number_masked="...0001",
    ))
    session.add(FinancialAccount(
        id=MIXED_CARD, institution_id=bofa.id, account_name="Shared Visa",
        account_type="CREDIT_CARD", account_number_masked="...0002",
        mixed_use_status="MIXED_USE",
    ))
    session.add(FinancialAccount(
        id=DEDICATED_CARD, institution_id=bofa.id, account_name="Rental-only card",
        account_type="CREDIT_CARD", account_number_masked="...0003",
        mixed_use_status="DEDICATED_RENTAL",
    ))
    session.add(Property(
        id=LARKSPUR, name="Larkspur", address_line1="a", city="Westbrook", zip_code="60196",
        property_type="RENTAL_INVESTMENT",
    ))
    session.add(Property(
        id=ELDERBERRY, name="Elderberry", address_line1="b", city="Cedar Grove", zip_code="60198",
        property_type="PRIMARY_RESIDENCE",
    ))
    doc = Document(
        id="doc_settle", file_sha256="sha_settle", original_filename="f.pdf",
        storage_path="/p", file_size_bytes=1,
    )
    session.add(doc)
    session.flush()
    statement = Statement(
        id="stmt_settle", document_id=doc.id, financial_account_id=CHECKING,
        statement_type="CHECKING", active_parser_name="T", active_parser_version="1",
        statement_date=date(2026, 3, 31),
    )
    session.add(statement)
    session.commit()
    return {"statement": statement}


def _make_txn(session, env, txn_id, account_id, amount_cents, description, counter=None, day=12):
    txn = Transaction(
        id=txn_id,
        statement_id=env["statement"].id,
        financial_account_id=account_id,
        transaction_date=date(2026, 3, day),
        amount_cents=amount_cents,
        direction="OUTFLOW" if amount_cents < 0 else "INFLOW",
        raw_description=description,
        counter_account_id=counter,
    )
    session.add(txn)
    session.flush()
    return txn


def _lines_for(session, entry_id):
    return session.exec(
        select(JournalEntryLine).where(JournalEntryLine.journal_entry_id == entry_id)
    ).all()


def _account_net(session, code):
    """Net movement on a ledger account across POSTED entries (debit - credit)."""
    rows = session.exec(
        select(JournalEntryLine)
        .join(JournalEntry, JournalEntryLine.journal_entry_id == JournalEntry.id)
        .where(JournalEntry.status == "POSTED")
        .where(JournalEntryLine.ledger_account_code == code)
    ).all()
    return sum(r.debit_cents - r.credit_cents for r in rows)


def test_card_purchase_then_bank_settlement_end_to_end(session: Session, settle_env):
    """$400 rental + $200 personal purchase, then a $600 payment from checking."""
    # Rent so cash flow is meaningful.
    rent = _make_txn(session, settle_env, "txn_settle_rent", CHECKING, 300000, "Rent", day=1)
    apply_transaction_splits(session, rent.id, [
        SplitItem(property_id=LARKSPUR, category="RENT_INCOME", amount_cents=300000,
                  tax_review_status="CLIENT_CONFIRMED")
    ])
    post_transaction_to_ledger(session, rent.id)

    # 1. The purchase on the mixed-use card: $400 rental repairs, $200 personal.
    purchase = _make_txn(session, settle_env, "txn_settle_purchase", MIXED_CARD,
                         -PAYMENT_600, "HOME DEPOT #1920", day=12)
    apply_transaction_splits(session, purchase.id, [
        SplitItem(property_id=LARKSPUR, category="REPAIRS", amount_cents=RENTAL_400,
                  tax_review_status="CLIENT_CONFIRMED"),
        SplitItem(property_id=None, category="PERSONAL", amount_cents=PERSONAL_200,
                  tax_review_status="CLIENT_CONFIRMED"),
    ])
    purchase_je = post_transaction_to_ledger(
        session, purchase.id, user_attested_cpa_review=True
    )
    assert purchase_je.entry_type == "EXPENSE_POST"

    after_purchase = calculate_property_financials(session, property_id=LARKSPUR)
    assert after_purchase["repairs_cents"] == RENTAL_400
    assert after_purchase["total_operating_expenses_cents"] == RENTAL_400

    # 2. The bank pays the card. The counter account is what marks this a
    #    settlement — the description says nothing about cards.
    settlement = _make_txn(session, settle_env, "txn_settle_payment", CHECKING,
                           -PAYMENT_600, "PAYMENT THANK YOU", counter=MIXED_CARD, day=22)
    apply_transaction_splits(session, settlement.id, [
        SplitItem(property_id=LARKSPUR, category="REPAIRS", amount_cents=RENTAL_400,
                  tax_review_status="CLIENT_CONFIRMED"),
        SplitItem(property_id=None, category="PERSONAL", amount_cents=PERSONAL_200,
                  tax_review_status="CLIENT_CONFIRMED"),
    ])
    assert is_card_settlement(
        session, settlement,
        source_account=session.get(FinancialAccount, CHECKING),
        counter_account=session.get(FinancialAccount, MIXED_CARD),
    )
    settlement_je = post_transaction_to_ledger(
        session, settlement.id, user_attested_cpa_review=True
    )

    # The settlement is a transfer: only the liability and the bank move.
    assert settlement_je.entry_type == "CARD_SETTLEMENT"
    lines = _lines_for(session, settlement_je.id)
    assert len(lines) == 2
    assert {(l.ledger_account_code, l.debit_cents, l.credit_cents) for l in lines} == {
        (2010, PAYMENT_600, 0),
        (1010, 0, PAYMENT_600),
    }
    assert sum(l.debit_cents for l in lines) == sum(l.credit_cents for l in lines)

    # The $600 payment reduced checking by $600 and the card liability by $600.
    assert _account_net(session, 1010) == 300000 - PAYMENT_600
    assert _account_net(session, 2010) == 0  # 600 charged, 600 paid

    # The expense is recognised exactly once: the settlement created none.
    financials = calculate_property_financials(session, property_id=LARKSPUR)
    assert financials["repairs_cents"] == RENTAL_400
    assert financials["total_operating_expenses_cents"] == RENTAL_400
    assert financials["repairs_cents"] != RENTAL_400 + PAYMENT_600

    # The personal $200 never becomes rental activity.
    assert financials["untagged_personal_draw_cents"] == PERSONAL_200
    assert financials["supplies_cents"] == 0

    # OCF reflects the rental share once, and only once.
    assert financials["cash_rent_collected_cents"] == 300000
    assert financials["card_settled_rental_opex_cents"] == RENTAL_400
    assert financials["ocf_cents"] == 300000 - RENTAL_400
    assert financials["direct_checking_opex_cents"] == 0


def test_settlement_is_idempotent(session: Session, settle_env):
    """Posting the same settlement twice must not create a second entry."""
    settlement = _make_txn(session, settle_env, "txn_settle_twice", CHECKING,
                           -PAYMENT_600, "PAYMENT THANK YOU", counter=MIXED_CARD, day=22)
    apply_transaction_splits(session, settlement.id, [
        SplitItem(property_id=LARKSPUR, category="REPAIRS", amount_cents=RENTAL_400,
                  tax_review_status="CLIENT_CONFIRMED"),
        SplitItem(property_id=None, category="PERSONAL", amount_cents=PERSONAL_200,
                  tax_review_status="CLIENT_CONFIRMED"),
    ])

    first = post_transaction_to_ledger(session, settlement.id, user_attested_cpa_review=True)
    entries_after_first = len(session.exec(select(JournalEntry)).all())
    second = post_transaction_to_ledger(session, settlement.id, user_attested_cpa_review=True)

    assert first.id == second.id
    assert len(session.exec(select(JournalEntry)).all()) == entries_after_first
    # 2010 is a liability, so a debit reduces it: +60,000 here means the card was
    # paid down by exactly one $600 payment, not two.
    assert _account_net(session, 2010) == PAYMENT_600


def test_settlement_reclassification_reverses_and_reposts_atomically(session: Session, settle_env):
    """Correcting a settlement keeps the ledger immutable and never double pays."""
    settlement = _make_txn(session, settle_env, "txn_settle_reclass", CHECKING,
                           -PAYMENT_600, "PAYMENT THANK YOU", counter=MIXED_CARD, day=22)
    apply_transaction_splits(session, settlement.id, [
        SplitItem(property_id=LARKSPUR, category="REPAIRS", amount_cents=RENTAL_400,
                  tax_review_status="CLIENT_CONFIRMED"),
        SplitItem(property_id=None, category="PERSONAL", amount_cents=PERSONAL_200,
                  tax_review_status="CLIENT_CONFIRMED"),
    ])
    original = post_transaction_to_ledger(session, settlement.id, user_attested_cpa_review=True)

    # Reclassify: the entire payment is personal.
    replacement = reclassify_and_repost_transaction(
        session, settlement.id,
        [SplitItem(property_id=None, category="PERSONAL", amount_cents=PAYMENT_600,
                   tax_review_status="CLIENT_CONFIRMED")],
        user_attested_cpa_review=True,
    )

    assert session.get(JournalEntry, original.id).status == "POSTED"  # untouched
    assert is_entry_reversed(session, original.id) is True
    assert replacement.id != original.id
    assert replacement.entry_type == "CARD_SETTLEMENT"

    # The reversal cancels the original and the replacement re-applies it, so the
    # card is still paid down exactly once (+60,000 debit net on the liability).
    assert _account_net(session, 2010) == PAYMENT_600
    assert _account_net(session, 1010) == -PAYMENT_600  # cash left the bank once


def test_mixed_use_settlement_requires_review_before_posting(session: Session, settle_env):
    """The allocation of a mixed-use card payment is itself a reviewed decision."""
    settlement = _make_txn(session, settle_env, "txn_settle_gate", CHECKING,
                           -PAYMENT_600, "PAYMENT THANK YOU", counter=MIXED_CARD, day=22)
    apply_transaction_splits(session, settlement.id, [
        SplitItem(property_id=LARKSPUR, category="REPAIRS", amount_cents=RENTAL_400,
                  tax_review_status="CLIENT_CONFIRMED"),
        SplitItem(property_id=None, category="PERSONAL", amount_cents=PERSONAL_200,
                  tax_review_status="CLIENT_CONFIRMED"),
    ])

    with pytest.raises(GovernanceAuthorizationError, match="CBD-2 Policy Violation"):
        post_transaction_to_ledger(session, settlement.id)
    assert session.get(TransactionProcessingState, settlement.id).posting_status == "UNPOSTED"


def test_dedicated_rental_card_is_not_gated(session: Session, settle_env):
    """A purpose-limited rental card needs no mixed-use review."""
    purchase = _make_txn(session, settle_env, "txn_dedicated", DEDICATED_CARD,
                         -RENTAL_400, "SHERWIN WILLIAMS", day=14)
    apply_transaction_splits(session, purchase.id, [
        SplitItem(property_id=LARKSPUR, category="REPAIRS", amount_cents=RENTAL_400,
                  tax_review_status="CLIENT_CONFIRMED")
    ])
    entry = post_transaction_to_ledger(session, purchase.id)
    assert entry.status == "POSTED"

    # And a settlement of that same dedicated card is not gated either.
    payment = _make_txn(session, settle_env, "txn_dedicated_pay", CHECKING,
                        -RENTAL_400, "PAYMENT THANK YOU", counter=DEDICATED_CARD, day=24)
    apply_transaction_splits(session, payment.id, [
        SplitItem(property_id=LARKSPUR, category="REPAIRS", amount_cents=RENTAL_400,
                  tax_review_status="CLIENT_CONFIRMED")
    ])
    settlement = post_transaction_to_ledger(session, payment.id)
    assert settlement.entry_type == "CARD_SETTLEMENT"


def test_settlement_review_requirement_is_knowable_before_allocation(session: Session, settle_env):
    """The interface must be able to ask for the attestation before allocation.

    A settlement's allocation is what decides the rental share, so the gate
    follows the card being paid rather than any split that may not exist yet.
    """
    from proptally.domain.transactions.policy import evaluate_posting_policy

    settlement = _make_txn(session, settle_env, "txn_settle_prehint", CHECKING,
                           -PAYMENT_600, "PAYMENT THANK YOU", counter=MIXED_CARD, day=22)
    policy = evaluate_posting_policy(session, settlement)  # no splits yet
    assert policy.requires_cpa_review is True
    assert policy.should_offer_review is True
    assert "needs review before posting" in " ".join(policy.reasons)

    # A dedicated rental card is never offered the gate.
    dedicated = _make_txn(session, settle_env, "txn_settle_ded_pre", CHECKING,
                          -RENTAL_400, "PAYMENT THANK YOU", counter=DEDICATED_CARD, day=24)
    policy_dedicated = evaluate_posting_policy(session, dedicated)
    assert policy_dedicated.requires_cpa_review is False
    assert policy_dedicated.should_offer_review is False


def test_purchase_on_a_shared_card_offers_review_before_allocation(session: Session, settle_env):
    """An unallocated card charge on a shared card hints, and then requires once rental."""
    from proptally.domain.transactions.policy import evaluate_posting_policy

    purchase = _make_txn(session, settle_env, "txn_shared_prehint", MIXED_CARD,
                         -RENTAL_400, "POS PURCHASE 4451", day=14)
    pending = evaluate_posting_policy(session, purchase)
    assert pending.requires_cpa_review is False
    assert pending.may_require_review is True
    assert pending.should_offer_review is True

    rented = evaluate_posting_policy(session, purchase, [
        SplitItem(property_id=LARKSPUR, category="REPAIRS", amount_cents=RENTAL_400,
                  tax_review_status="SELF_CLASSIFIED")
    ])
    assert rented.requires_cpa_review is True


# --- Attribution: property attribution alone does not make an allocation an ---
# --- operating expense. Each case below is a settlement allocation and the  ---
# --- rental cash-flow outcome it must produce.                              ---

RENT_INCOME_CENTS = 300000


def _rent(session, env):
    txn = _make_txn(session, env, "txn_attr_rent", CHECKING, RENT_INCOME_CENTS, "Rent", day=1)
    apply_transaction_splits(session, txn.id, [
        SplitItem(property_id=LARKSPUR, category="RENT_INCOME", amount_cents=RENT_INCOME_CENTS,
                  tax_review_status="CLIENT_CONFIRMED")
    ])
    post_transaction_to_ledger(session, txn.id)


def _settle_allocated(session, env, case_id, allocations, card=MIXED_CARD):
    """Posts a card payment with the given (property, category, amount) allocation."""
    total = sum(a[2] for a in allocations)
    txn = _make_txn(session, env, f"txn_attr_{case_id}", CHECKING, -total,
                    "PAYMENT THANK YOU", counter=card, day=22)
    apply_transaction_splits(session, txn.id, [
        SplitItem(property_id=prop, category=category, amount_cents=amount,
                  tax_review_status="CLIENT_CONFIRMED")
        for prop, category, amount in allocations
    ])
    post_transaction_to_ledger(session, txn.id, user_attested_cpa_review=True)
    return txn


def _rental_cash_flow(session):
    f = calculate_property_financials(session, property_id=LARKSPUR)
    return f


@pytest.mark.parametrize(
    "case_id, allocations, expected_rental_opex, expected_ocf",
    [
        # 1. Rental property + an operating expense category -> counted.
        ("rental_repairs", [(LARKSPUR, "REPAIRS", 40000)], 40000, RENT_INCOME_CENTS - 40000),
        # 2. Rental property + PERSONAL -> a draw, not rental card OpEx.
        ("rental_personal", [(LARKSPUR, "PERSONAL", 40000)], 0, RENT_INCOME_CENTS),
        # 3. Rental property + RENT_INCOME -> income wrongly tagged to a door.
        ("rental_income", [(LARKSPUR, "RENT_INCOME", 40000)], 0, RENT_INCOME_CENTS),
        # 4. No property + PERSONAL -> not attributable to a door at all.
        ("untagged_personal", [(None, "PERSONAL", 40000)], 0, RENT_INCOME_CENTS),
        # 5. Primary residence + REPAIRS -> outside rental scope.
        ("rowan_repairs", [(ELDERBERRY, "REPAIRS", 40000)], 0, RENT_INCOME_CENTS),
        # 6. Mixed allocation -> only the eligible rental expense reduces OCF.
        ("mixed", [(LARKSPUR, "REPAIRS", 40000), (None, "PERSONAL", 20000)],
         40000, RENT_INCOME_CENTS - 40000),
        # 7. Utilities and bank fees are operating expenses too.
        ("rental_utilities", [(LARKSPUR, "UTILITIES", 40000)],
         40000, RENT_INCOME_CENTS - 40000),
        ("rental_bank_fees", [(LARKSPUR, "BANK_FEES", 40000)],
         40000, RENT_INCOME_CENTS - 40000),
    ],
    ids=lambda v: v if isinstance(v, str) else "",
)
def test_settlement_attribution_by_category_and_scope(
    session: Session, settle_env, case_id, allocations, expected_rental_opex, expected_ocf
):
    _rent(session, settle_env)
    before = _rental_cash_flow(session)

    _settle_allocated(session, settle_env, case_id, allocations)

    after = _rental_cash_flow(session)
    assert after["card_settled_rental_opex_cents"] == expected_rental_opex
    assert after["ocf_cents"] == expected_ocf
    # A settlement is never an expense: NOI is untouched by it.
    assert after["noi_cents"] == before["noi_cents"]
    assert after["total_operating_expenses_cents"] == before["total_operating_expenses_cents"]


def test_settlement_attribution_keeps_partial_allocation_visible(session: Session, settle_env):
    """A settlement allocated only in part still reports the unattributed remainder."""
    _rent(session, settle_env)
    _settle_allocated(session, settle_env, "partial", [(LARKSPUR, "REPAIRS", 40000)])

    f = _rental_cash_flow(session)
    # The payment was $400 because the allocation defined its size; nothing is
    # unattributed when the allocation covers it.
    assert f["card_settlement_unattributed_cents"] == 0
    assert f["card_settled_rental_opex_cents"] == 40000

    # A second payment of $600 allocated only $400 reports the $200 remainder.
    txn = _make_txn(session, settle_env, "txn_attr_under", CHECKING, -60000,
                    "PAYMENT THANK YOU", counter=MIXED_CARD, day=24)
    apply_transaction_splits(session, txn.id, [
        SplitItem(property_id=LARKSPUR, category="REPAIRS", amount_cents=40000,
                  tax_review_status="CLIENT_CONFIRMED"),
    ], allow_partial=True)
    entry = create_journal_entry(session, date(2026, 3, 24), "CASH_SETTLEMENT",
                                 "Visa payoff partial", "ATTR:PARTIAL", source_transaction_id=txn.id)
    add_journal_line(session, entry.id, 2010, debit_cents=60000)
    add_journal_line(session, entry.id, 1010, credit_cents=60000)
    post_journal_entry(session, entry.id)

    f2 = _rental_cash_flow(session)
    assert f2["card_settled_rental_opex_cents"] == 80000  # both payments' eligible share
    assert f2["card_settlement_unattributed_cents"] == 20000
