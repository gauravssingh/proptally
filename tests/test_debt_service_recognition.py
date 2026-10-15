"""Review remediation: only a confirmed bank payment counts as debt service.

CADS is a headline metric, so principal and interest must not be recognised from
a servicer bill, an unfunded application, or a posting that only moves a clearing
balance. Each scenario below states the CADS treatment the review asked for.
"""

from datetime import date

import pytest
from sqlmodel import Session

from proptally.db.models import (
    Document,
    FinancialAccount,
    FinancialInstitution,
    LoanAccount,
    MortgagePeriod,
    Property,
    Statement,
)
from proptally.domain.accounting.coa import (
    INFLOW_SUSPENSE_CODE,
    MORTGAGE_INTEREST_CODE,
    ensure_entity_ledger_accounts,
)
from proptally.domain.accounting.ledger import (
    add_journal_line,
    create_journal_entry,
    post_journal_entry,
    reverse_journal_entry,
)
from proptally.domain.accounting.metrics import calculate_property_financials

LARKSPUR = "prop_tinsley_huntley"
# Entity-owned ledger codes. The allocator gives each property, deposit account
# and loan its own code, so these are bound to the rows ``ds_env`` creates rather
# than pinned to fixed numbers (see the fixture below).
CHECKING = None
CLEARING_ROCKET = None
PRINCIPAL_ROCKET = None
ESCROW_ROCKET = None
# Portfolio-invariant standard accounts.
INTEREST = MORTGAGE_INTEREST_CODE
SUSPENSE = INFLOW_SUSPENSE_CODE  # Unassigned Inflow Suspense, credits the unfunded remainder

PRINCIPAL_CENTS = 62000
INTEREST_CENTS = 66012
ESCROW_CENTS = 33800
PAYMENT_CENTS = PRINCIPAL_CENTS + INTEREST_CENTS + ESCROW_CENTS

RENT_CENTS = 300000


@pytest.fixture(name="ds_env")
def ds_env_fixture(session: Session):
    bofa = FinancialInstitution(id="inst_ds", name="Bank")
    session.add(bofa)
    session.flush()
    session.add(FinancialAccount(
        id="acct_ds_chk", institution_id=bofa.id, account_name="Checking",
        account_type="CHECKING", account_number_masked="...1",
    ))
    session.add(Property(
        id=LARKSPUR, name="Larkspur", address_line1="a", city="Westbrook", zip_code="60196",
        property_type="RENTAL_INVESTMENT",
    ))
    session.add(FinancialAccount(
        id="acct_ds_loan", institution_id=bofa.id, account_name="Rocket Loan",
        account_type="MORTGAGE", account_number_masked="...9",
    ))
    session.flush()
    session.add(LoanAccount(
        id="loan_ds", financial_account_id="acct_ds_loan", property_id=LARKSPUR,
        loan_number="DS-1", original_principal_cents=34000000, term_months=360,
    ))
    session.flush()

    # The property, deposit account and loan above now exist, so the allocator can
    # give them their ledger accounts. The module-level codes are rebound to the
    # rows this fixture just created: they are per-entity and must not be assumed.
    ensure_entity_ledger_accounts(session)
    global CHECKING, CLEARING_ROCKET, PRINCIPAL_ROCKET, ESCROW_ROCKET
    checking_account = session.get(FinancialAccount, "acct_ds_chk")
    rocket_loan = session.get(LoanAccount, "loan_ds")
    CHECKING = checking_account.ledger_account_code
    CLEARING_ROCKET = rocket_loan.clearing_ledger_code
    PRINCIPAL_ROCKET = rocket_loan.principal_ledger_code
    ESCROW_ROCKET = rocket_loan.escrow_ledger_code

    doc = Document(id="doc_ds", file_sha256="sha_ds", original_filename="f.pdf",
                   storage_path="/p", file_size_bytes=1)
    session.add(doc)
    session.flush()
    statement = Statement(
        id="stmt_ds", document_id=doc.id, financial_account_id="acct_ds_chk",
        statement_type="CHECKING", active_parser_name="T", active_parser_version="1",
        statement_date=date(2026, 3, 31),
    )
    session.add(statement)
    session.commit()
    return {"statement": statement}


def _post(session, key, day, lines, entry_type="CASH_SETTLEMENT"):
    entry = create_journal_entry(session, date(2026, 3, day), entry_type, key, key)
    for code, debit, credit, prop in lines:
        add_journal_line(session, entry.id, code, debit_cents=debit, credit_cents=credit, property_id=prop)
    return post_journal_entry(session, entry.id)


def _rent(session):
    _post(session, "DS:RENT", 1, [
        (CHECKING, RENT_CENTS, 0, None),
        (4010, 0, RENT_CENTS, LARKSPUR),
    ], entry_type="REVENUE_POST")


def _settlement(session, key="DS:SETTLE", day=25):
    """Stage 1: the bank pays the servicer into the clearing account."""
    return _post(session, key, day, [
        (CLEARING_ROCKET, PAYMENT_CENTS, 0, None),
        (CHECKING, 0, PAYMENT_CENTS, None),
    ])


def _application(session, key="DS:APPLY", day=25):
    """Stage 2: the servicer applies principal, interest and escrow."""
    return _post(session, key, day, [
        (PRINCIPAL_ROCKET, PRINCIPAL_CENTS, 0, LARKSPUR),
        (INTEREST, INTEREST_CENTS, 0, LARKSPUR),
        (ESCROW_ROCKET, ESCROW_CENTS, 0, LARKSPUR),
        (CLEARING_ROCKET, 0, PAYMENT_CENTS, None),
    ])


def _financials(session):
    return calculate_property_financials(session, property_id=LARKSPUR)


def test_statement_received_but_not_paid_is_not_debt_service(session: Session, ds_env):
    """A servicer bill records an obligation, not a payment."""
    _rent(session)
    session.add(MortgagePeriod(
        id="mp_ds_1", statement_id=ds_env["statement"].id, loan_account_id="loan_ds",
        payment_due_date=date(2026, 4, 1), statement_date=date(2026, 3, 20),
        principal_billed_cents=PRINCIPAL_CENTS, interest_billed_cents=INTEREST_CENTS,
        escrow_billed_cents=ESCROW_CENTS, total_amount_billed_cents=PAYMENT_CENTS,
        outstanding_principal_cents=31045000, interest_rate_bps=3125,
    ))
    session.commit()

    f = _financials(session)
    assert f["total_debt_service_cents"] == 0
    assert f["unfunded_applied_debt_service_cents"] == 0
    assert f["cads_cents"] == RENT_CENTS  # nothing paid to the bank yet


def test_bank_settled_but_not_yet_applied_follows_the_settlement_policy(session: Session, ds_env):
    """Defined policy: a settled-but-unapplied payment is not debt service.

    The funds sit in the clearing account until the servicer applies them, so
    principal and interest have not been incurred. The cash is visible as an
    outstanding clearing balance rather than as debt service.
    """
    _rent(session)
    _settlement(session)

    f = _financials(session)
    assert f["total_debt_service_cents"] == 0
    assert f["principal_paid_cents"] == 0
    assert f["mortgage_interest_cents"] == 0
    assert f["cads_cents"] == RENT_CENTS


def test_servicer_application_after_settlement_is_counted_once(session: Session, ds_env):
    """Stage 2 does not recognise the expense a second time."""
    _rent(session)
    _settlement(session)
    _application(session)

    f = _financials(session)
    assert f["principal_paid_cents"] == PRINCIPAL_CENTS
    assert f["mortgage_interest_cents"] == INTEREST_CENTS
    assert f["total_debt_service_cents"] == PRINCIPAL_CENTS + INTEREST_CENTS
    assert f["escrow_funded_cents"] == ESCROW_CENTS          # reported, not debt service
    assert f["cads_cents"] == RENT_CENTS - (PRINCIPAL_CENTS + INTEREST_CENTS)
    assert f["unfunded_applied_debt_service_cents"] == 0


def test_application_posted_against_clearing_only_is_not_debt_service(session: Session, ds_env):
    """No bank payment funded the clearing account, so nothing is paid."""
    _rent(session)
    _application(session)

    f = _financials(session)
    assert f["total_debt_service_cents"] == 0
    assert f["principal_paid_cents"] == 0
    assert f["mortgage_interest_cents"] == 0
    # The unfunded application is surfaced rather than silently dropped.
    assert f["unfunded_applied_debt_service_cents"] == PRINCIPAL_CENTS + INTEREST_CENTS
    assert f["cads_cents"] == RENT_CENTS


def test_direct_payment_from_checking_counts(session: Session, ds_env):
    """The other documented shape: the bank pays the servicer in one entry."""
    _rent(session)
    _post(session, "DS:DIRECT", 26, [
        (PRINCIPAL_ROCKET, PRINCIPAL_CENTS, 0, LARKSPUR),
        (INTEREST, INTEREST_CENTS, 0, LARKSPUR),
        (ESCROW_ROCKET, ESCROW_CENTS, 0, LARKSPUR),
        (CHECKING, 0, PAYMENT_CENTS, None),
    ])

    f = _financials(session)
    assert f["total_debt_service_cents"] == PRINCIPAL_CENTS + INTEREST_CENTS
    assert f["cads_cents"] == RENT_CENTS - (PRINCIPAL_CENTS + INTEREST_CENTS)


def test_reversed_payment_stops_being_counted(session: Session, ds_env):
    """A bounced or reversed payment must not remain in debt service."""
    _rent(session)
    settlement = _settlement(session)
    _application(session)

    counted = _financials(session)
    assert counted["total_debt_service_cents"] == PRINCIPAL_CENTS + INTEREST_CENTS

    reverse_journal_entry(session, settlement.id, date(2026, 3, 27), "Payment returned by bank")

    after = _financials(session)
    assert after["total_debt_service_cents"] == 0
    assert after["cads_cents"] == RENT_CENTS


# --- Partial funding: recognised debt service must be covered by real cash ---

def _fund(session, key, amount_cents, day=20):
    """Stage 1: the bank settles `amount_cents` into the Rocket clearing account."""
    return _post(session, key, day, [
        (CLEARING_ROCKET, amount_cents, 0, None),
        (CHECKING, 0, amount_cents, None),
    ])


def _apply(session, key, principal, interest, escrow=0, credited=None, day=25, notes=None):
    """Stage 2: the servicer applies a payment, crediting `credited` of clearing.

    When `credited` is less than the payment applied, the remainder is credited
    elsewhere, reproducing a partially funded application.
    """
    payment = principal + interest + escrow
    credited = payment if credited is None else credited
    lines = [
        (PRINCIPAL_ROCKET, principal, 0, LARKSPUR),
        (INTEREST, interest, 0, LARKSPUR),
    ]
    if escrow:
        lines.append((ESCROW_ROCKET, escrow, 0, LARKSPUR))
    lines.append((CLEARING_ROCKET, 0, credited, None))
    if payment > credited:
        lines.append((SUSPENSE, 0, payment - credited, None))
    return _post(session, key, day, lines)


def test_application_larger_than_its_funding_is_not_recognised(session: Session, ds_env):
    """The review's example: $10 funded, $100 principal + interest applied."""
    _rent(session)
    _fund(session, "PF:SMALL", 1000)
    _apply(session, "PF:BIG", principal=6000, interest=4000, credited=1000)

    f = _financials(session)
    assert f["total_debt_service_cents"] == 0
    assert f["principal_paid_cents"] == 0
    assert f["mortgage_interest_cents"] == 0
    assert f["unfunded_applied_debt_service_cents"] == 10000
    assert f["cads_cents"] == RENT_CENTS


def test_application_covered_by_its_funding_is_recognised(session: Session, ds_env):
    _rent(session)
    _fund(session, "PF:MATCH", 10000)
    _apply(session, "PF:MATCH_APP", principal=6000, interest=4000)

    f = _financials(session)
    assert f["total_debt_service_cents"] == 10000
    assert f["cads_cents"] == RENT_CENTS - 10000
    assert f["unfunded_applied_debt_service_cents"] == 0


def test_escrow_is_funded_but_never_debt_service(session: Session, ds_env):
    """$150 funded, $100 principal + interest and $50 escrow applied."""
    _rent(session)
    _fund(session, "PF:ESCROW", 15000)
    _apply(session, "PF:ESCROW_APP", principal=6000, interest=4000, escrow=5000)

    f = _financials(session)
    assert f["total_debt_service_cents"] == 10000
    assert f["escrow_funded_cents"] == 5000
    assert f["cads_cents"] == RENT_CENTS - 10000  # the escrow share does not reduce CADS


def test_several_partial_fundings_may_cover_one_application(session: Session, ds_env):
    _rent(session)
    _fund(session, "PF:PART1", 6000, day=18)
    _fund(session, "PF:PART2", 4000, day=20)
    _apply(session, "PF:PART_APP", principal=6000, interest=4000)

    f = _financials(session)
    assert f["total_debt_service_cents"] == 10000
    assert f["unfunded_applied_debt_service_cents"] == 0


def test_partial_funding_short_of_the_application_is_not_apportioned(session: Session, ds_env):
    """$60 + $40 funded against a $150 application: recognised only if covered."""
    _rent(session)
    _fund(session, "PF:SHORT1", 6000, day=18)
    _fund(session, "PF:SHORT2", 4000, day=20)
    _apply(session, "PF:SHORT_APP", principal=9000, interest=6000)

    f = _financials(session)
    # 15000 applied against 10000 funded: the ledger cannot say which part is
    # funded, so nothing is recognised rather than apportioning on a guess.
    assert f["total_debt_service_cents"] == 0
    assert f["unfunded_applied_debt_service_cents"] == 15000


def test_one_funded_balance_cannot_pay_two_applications(session: Session, ds_env):
    """Funding is consumed as it is drawn, so it is never counted twice."""
    _rent(session)
    _fund(session, "PF:ONCE", 10000)
    _apply(session, "PF:ONCE_A", principal=6000, interest=4000, day=25)
    _apply(session, "PF:ONCE_B", principal=6000, interest=4000, day=26)

    f = _financials(session)
    assert f["total_debt_service_cents"] == 10000  # not 20000
    assert f["unfunded_applied_debt_service_cents"] == 10000


def test_application_exceeding_the_remaining_balance_is_not_recognised(session: Session, ds_env):
    _rent(session)
    _fund(session, "PF:REM", 15000)
    _apply(session, "PF:REM_A", principal=6000, interest=4000, day=25)
    # Only 5000 of funding remains.
    _apply(session, "PF:REM_B", principal=6000, interest=4000, day=26)

    f = _financials(session)
    assert f["total_debt_service_cents"] == 10000
    assert f["unfunded_applied_debt_service_cents"] == 10000


def test_unsupported_application_does_not_free_its_funding(session: Session, ds_env):
    """Funding consumed by an unsupported application cannot fund a later one."""
    _rent(session)
    _fund(session, "PF:BURN", 10000)
    _apply(session, "PF:BURN_BAD", principal=6000, interest=4000, credited=10000, day=25)
    # Same money must not support a second application.
    _apply(session, "PF:BURN_B", principal=6000, interest=4000, day=26)

    f = _financials(session)
    assert f["total_debt_service_cents"] == 10000
    assert f["unfunded_applied_debt_service_cents"] == 10000


def test_reversed_funding_cannot_support_debt_service(session: Session, ds_env):
    _rent(session)
    funding = _fund(session, "PF:REV_FUND", 10000)
    _apply(session, "PF:REV_FUND_APP", principal=6000, interest=4000)

    before = _financials(session)
    assert before["total_debt_service_cents"] == 10000

    reverse_journal_entry(session, funding.id, date(2026, 3, 27), "Funding returned by bank")

    after = _financials(session)
    assert after["total_debt_service_cents"] == 0
    assert after["cads_cents"] == RENT_CENTS


def test_reversed_application_does_not_contribute(session: Session, ds_env):
    _rent(session)
    _fund(session, "PF:REV_APP_FUND", 10000)
    application = _apply(session, "PF:REV_APP", principal=6000, interest=4000)

    assert _financials(session)["total_debt_service_cents"] == 10000

    reverse_journal_entry(session, application.id, date(2026, 3, 27), "Application reversed")

    after = _financials(session)
    assert after["total_debt_service_cents"] == 0
    assert after["unfunded_applied_debt_service_cents"] == 0


def test_unrelated_checking_outflow_does_not_make_a_direct_payment(session: Session, ds_env):
    """A mortgage debit beside an unrelated outflow is not a mortgage payment."""
    _rent(session)
    # Principal posted with an unrelated repair paid from the same bank account.
    _post(session, "PF:UNRELATED", 26, [
        (PRINCIPAL_ROCKET, PRINCIPAL_CENTS, 0, LARKSPUR),
        (5040, 40000, 0, LARKSPUR),      # unrelated repair expense
        (CHECKING, 0, PRINCIPAL_CENTS + 40000, None),
    ])

    f = _financials(session)
    assert f["total_debt_service_cents"] == 0
    assert f["unfunded_applied_debt_service_cents"] == PRINCIPAL_CENTS


def test_direct_payment_must_cover_the_whole_payment(session: Session, ds_env):
    """A partial bank outflow cannot pay the full principal, interest and escrow."""
    _rent(session)
    _post(session, "PF:PARTIAL_DIRECT", 26, [
        (PRINCIPAL_ROCKET, PRINCIPAL_CENTS, 0, LARKSPUR),
        (INTEREST, INTEREST_CENTS, 0, LARKSPUR),
        (ESCROW_ROCKET, ESCROW_CENTS, 0, LARKSPUR),
        (CHECKING, 0, PRINCIPAL_CENTS, None),          # short of the full payment
        (SUSPENSE, 0, INTEREST_CENTS + ESCROW_CENTS, None),
    ])

    f = _financials(session)
    assert f["total_debt_service_cents"] == 0


# --- Escrow credits must never reduce the funding requirement ----------------
# A credit to escrow is a refund, release or adjustment — not funding. Netting it
# against the payment would let it manufacture evidence that principal and
# interest were paid.

def test_negative_escrow_credit_cannot_manufacture_a_direct_payment(session: Session, ds_env):
    """Test A: $100 principal, $90 escrow credit, $10 from checking."""
    _rent(session)
    _post(session, "NEG:A", 26, [
        (PRINCIPAL_ROCKET, 10000, 0, LARKSPUR),   # principal applied
        (ESCROW_ROCKET, 0, 9000, LARKSPUR),       # escrow released, not funded
        (CHECKING, 0, 1000, None),               # only $10 actually left the bank
    ])

    f = _financials(session)
    assert f["total_debt_service_cents"] == 0
    assert f["principal_paid_cents"] == 0
    assert f["unfunded_applied_debt_service_cents"] == 10000
    assert f["cads_cents"] == RENT_CENTS  # the unsupported principal must not reduce CADS


def test_negative_escrow_credit_cannot_manufacture_a_clearing_application(session: Session, ds_env):
    """Test B: $10 funded, $100 applied with a $90 escrow credit in the entry."""
    _rent(session)
    _fund(session, "NEG:B_FUND", 1000)
    _post(session, "NEG:B", 26, [
        (PRINCIPAL_ROCKET, 6000, 0, LARKSPUR),
        (INTEREST, 4000, 0, LARKSPUR),
        (ESCROW_ROCKET, 0, 9000, LARKSPUR),       # escrow credit inside the application
        (CLEARING_ROCKET, 0, 1000, None),        # only the amount actually consumed
    ])

    f = _financials(session)
    assert f["total_debt_service_cents"] == 0
    assert f["unfunded_applied_debt_service_cents"] == 10000
    assert f["cads_cents"] == RENT_CENTS


def test_escrow_credit_beside_a_funded_payment_fails_closed(session: Session, ds_env):
    """Even with enough funding, an escrow credit makes the entry unrelatable."""
    _rent(session)
    _fund(session, "NEG:BIG_FUND", 10000)
    _post(session, "NEG:BIG", 26, [
        (PRINCIPAL_ROCKET, 6000, 0, LARKSPUR),
        (INTEREST, 4000, 0, LARKSPUR),
        (ESCROW_ROCKET, 0, 9000, LARKSPUR),
        (CLEARING_ROCKET, 0, 10000, None),
        (SUSPENSE, 9000, 0, None),
    ])

    f = _financials(session)
    assert f["total_debt_service_cents"] == 0
    assert f["unfunded_applied_debt_service_cents"] == 10000


def test_normal_payment_with_positive_escrow_contribution(session: Session, ds_env):
    """Test C: $150 funded, $100 principal + interest, $50 escrow contribution."""
    _rent(session)
    _fund(session, "ESC:PLUS_FUND", 15000)
    _apply(session, "ESC:PLUS", principal=6000, interest=4000, escrow=5000)

    f = _financials(session)
    assert f["total_debt_service_cents"] == 10000
    assert f["escrow_funded_cents"] == 5000
    assert f["cads_cents"] == RENT_CENTS - 10000  # only debt service reduces CADS


def test_normal_payment_without_escrow(session: Session, ds_env):
    """Test D: $100 funded, $100 principal and interest, no escrow movement."""
    _rent(session)
    _fund(session, "ESC:NONE_FUND", 10000)
    _apply(session, "ESC:NONE", principal=6000, interest=4000)

    f = _financials(session)
    assert f["total_debt_service_cents"] == 10000
    assert f["escrow_funded_cents"] == 0
    assert f["cads_cents"] == RENT_CENTS - 10000


def test_separate_escrow_release_does_not_alter_debt_service(session: Session, ds_env):
    """Test E: an independent escrow release is not part of the mortgage payment."""
    _rent(session)
    _fund(session, "ESC:SEP_FUND", 15000)
    _apply(session, "ESC:SEP_APP", principal=6000, interest=4000, escrow=5000)

    before = _financials(session)
    assert before["total_debt_service_cents"] == 10000

    # A refund of escrow to the borrower, recorded independently.
    _post(session, "ESC:RELEASE", 28, [
        (CHECKING, 9000, 0, None),
        (ESCROW_ROCKET, 0, 9000, LARKSPUR),
    ], entry_type="ESCROW_REFUND")

    after = _financials(session)
    assert after["total_debt_service_cents"] == 10000
    assert after["principal_paid_cents"] == 6000
    assert after["mortgage_interest_cents"] == 4000
    assert after["cads_cents"] == RENT_CENTS - 10000


# --- Only a standalone bank-to-clearing settlement may release funding --------
# Funding must not come from an entry that mixes settlement with an application,
# escrow activity or any other account: such an entry is ambiguous, and treating
# it as funding would let it pay for a later application.

def test_mixed_clearing_entry_creates_no_funding(session: Session, ds_env):
    """Test A: the invalid mixed journal must not fund a later application."""
    _rent(session)
    _post(session, "MIX:MIXED", 20, [
        (CLEARING_ROCKET, 10000, 0, None),       # clearing funding
        (PRINCIPAL_ROCKET, 9000, 0, LARKSPUR),    # ... combined with an application
        (ESCROW_ROCKET, 0, 9000, LARKSPUR),       # ... and an escrow credit
        (CHECKING, 0, 10000, None),
    ])
    # A separate $100 application against the same clearing account.
    _apply(session, "MIX:APP", principal=6000, interest=4000, day=25)

    f = _financials(session)
    assert f["total_debt_service_cents"] == 0
    # Both the mixed entry's principal (9000) and the application (10000) are
    # unsupported applied principal and interest.
    assert f["unfunded_applied_debt_service_cents"] == 19000
    assert f["cads_cents"] == RENT_CENTS


def test_valid_standalone_clearing_funding_still_works(session: Session, ds_env):
    """Test B: a plain checking-to-clearing transfer still funds an application."""
    _rent(session)
    _fund(session, "MIX:VALID_FUND", 10000)          # debit clearing / credit checking only
    _apply(session, "MIX:VALID_APP", principal=6000, interest=4000, day=25)

    f = _financials(session)
    assert f["total_debt_service_cents"] == 10000
    assert f["principal_paid_cents"] == 6000
    assert f["mortgage_interest_cents"] == 4000
    assert f["unfunded_applied_debt_service_cents"] == 0
    assert f["cads_cents"] == RENT_CENTS - 10000


def test_clearing_funding_with_escrow_activity_is_rejected(session: Session, ds_env):
    """Test C: an escrow movement in the funding entry makes it ambiguous."""
    _rent(session)
    _post(session, "MIX:ESCROW_FUND", 20, [
        (CLEARING_ROCKET, 10000, 0, None),
        (ESCROW_ROCKET, 0, 9000, LARKSPUR),       # escrow credit alongside funding
        (CHECKING, 0, 1000, None),
    ])
    _apply(session, "MIX:ESCROW_APP", principal=6000, interest=4000, day=25)

    f = _financials(session)
    assert f["total_debt_service_cents"] == 0
    assert f["unfunded_applied_debt_service_cents"] == 10000
    assert f["cads_cents"] == RENT_CENTS


def test_clearing_funding_with_an_unrelated_account_is_rejected(session: Session, ds_env):
    """A third account in the funding entry makes it ambiguous, so it fails closed."""
    _rent(session)
    _post(session, "MIX:OTHER_FUND", 20, [
        (CLEARING_ROCKET, 10000, 0, None),
        (5040, 40000, 0, LARKSPUR),               # unrelated repair
        (CHECKING, 0, 50000, None),
    ])
    _apply(session, "MIX:OTHER_APP", principal=6000, interest=4000, day=25)

    f = _financials(session)
    assert f["total_debt_service_cents"] == 0
    assert f["unfunded_applied_debt_service_cents"] == 10000


def test_clearing_funding_debited_from_the_bank_is_rejected(session: Session, ds_env):
    """The bank account must be the payer, not the payee."""
    _rent(session)
    _post(session, "MIX:REVERSE_FUND", 20, [
        (CLEARING_ROCKET, 0, 10000, None),       # clearing credited
        (CHECKING, 10000, 0, None),              # bank debited
    ])
    _apply(session, "MIX:REVERSE_APP", principal=6000, interest=4000, day=25)

    f = _financials(session)
    assert f["total_debt_service_cents"] == 0
