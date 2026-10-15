"""The ledger code allocator's contract: deterministic, monotonic, collision-free.

These are properties the rest of the application depends on, so they are asserted
directly rather than inferred from a page rendering:

* a new portfolio's entities each get a distinct account,
* allocated codes never move once assigned (a posted history stays readable),
* allocation follows ``(created_at, id)`` order, which is what makes a migrated
  single-portfolio database keep the account numbers its journals already use,
* running out of a class's range fails loudly instead of borrowing another
  class's range.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import Session, select

from proptally.core.exceptions import LedgerCodeExhaustedError
from proptally.db.models import (
    FinancialAccount,
    FinancialInstitution,
    LedgerAccount,
    LoanAccount,
    Property,
)
from proptally.domain.accounting import coa
from proptally.domain.accounting.coa import (
    ALLOCATION_RANGES,
    CARD_LIABILITY,
    DEPOSIT,
    ESCROW,
    MORTGAGE_CLEARING,
    MORTGAGE_PRINCIPAL,
    PROPERTY_ASSET,
    allocate_ledger_account,
    ensure_entity_ledger_accounts,
    seed_standard_accounts,
)

BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _account(session, account_id: str, account_type: str, created_at: datetime, institution_id: str) -> FinancialAccount:
    account = FinancialAccount(
        id=account_id, institution_id=institution_id, account_name=account_id,
        account_type=account_type, account_number_masked="...0000", created_at=created_at,
    )
    session.add(account)
    return account


def _portfolio(session: Session, *, properties: int = 1, loans: int = 1) -> None:
    """Declares a portfolio with explicit creation times, so allocation order is fixed."""
    session.add(FinancialInstitution(id="inst_alloc", name="Bank"))
    session.flush()

    for index in range(properties):
        session.add(Property(
            id=f"prop_alloc_{index}", name=f"Door {index}", address_line1=f"{index} Main St",
            city="Springfield", zip_code="62701", property_type="RENTAL_INVESTMENT",
            created_at=BASE + timedelta(minutes=index),
        ))
    session.flush()

    for index in range(2):
        _account(session, f"acct_alloc_dep_{index}", "CHECKING",
                 BASE + timedelta(hours=1, minutes=index), "inst_alloc")
        _account(session, f"acct_alloc_card_{index}", "CREDIT_CARD",
                 BASE + timedelta(hours=2, minutes=index), "inst_alloc")
    session.flush()

    for index in range(loans):
        mortgage_account = _account(session, f"acct_alloc_mtg_{index}", "MORTGAGE",
                                    BASE + timedelta(hours=3, minutes=index), "inst_alloc")
        session.flush()
        session.add(LoanAccount(
            id=f"loan_alloc_{index}", financial_account_id=mortgage_account.id,
            property_id=f"prop_alloc_{index}", loan_number=f"AL-{index}",
            created_at=BASE + timedelta(hours=3, minutes=index),
        ))
    session.flush()


def test_first_allocation_reproduces_the_original_portfolio_numbering(session: Session):
    """A migrated portfolio keeps the account numbers its history already uses.

    The reference deployment numbered its first property 1510, its first deposit
    account 1010, its first card 2010 and its first loan 2110/2115/1110. Allocation
    in ``(created_at, id)`` order must reproduce exactly those, or upgrading would
    orphan every posted journal line.
    """
    _portfolio(session)
    ensure_entity_ledger_accounts(session)

    assert session.get(Property, "prop_alloc_0").asset_ledger_code == 1510
    assert session.get(FinancialAccount, "acct_alloc_dep_0").ledger_account_code == 1010
    assert session.get(FinancialAccount, "acct_alloc_dep_1").ledger_account_code == 1020
    assert session.get(FinancialAccount, "acct_alloc_card_0").ledger_account_code == 2010
    assert session.get(FinancialAccount, "acct_alloc_card_1").ledger_account_code == 2020
    loan = session.get(LoanAccount, "loan_alloc_0")
    assert loan.principal_ledger_code == 2110
    assert loan.clearing_ledger_code == 2115
    assert loan.escrow_ledger_code == 1110


def test_every_entity_account_is_distinct_and_attributed_to_its_class(session: Session):
    """No two entities share an account, and each code sits in its class's range."""
    _portfolio(session, properties=3, loans=3)
    ensure_entity_ledger_accounts(session)

    codes = []
    for prop in session.exec(select(Property)).all():
        codes.append(prop.asset_ledger_code)
    for account in session.exec(select(FinancialAccount)).all():
        if account.account_type == "MORTGAGE":
            # A mortgage account's accounts belong to its loan, not to the account row.
            assert account.ledger_account_code is None
        else:
            codes.append(account.ledger_account_code)
    for loan in session.exec(select(LoanAccount)).all():
        codes.extend([loan.principal_ledger_code, loan.clearing_ledger_code, loan.escrow_ledger_code])

    assert len(codes) == len(set(codes)), "two entities were given the same ledger account"
    assert None not in codes

    in_range = {
        DEPOSIT: lambda c: 1010 <= c <= 1090 and c % 10 == 0,
        CARD_LIABILITY: lambda c: 2010 <= c <= 2090 and c % 10 == 0,
        PROPERTY_ASSET: lambda c: 1510 <= c <= 1990 and c % 10 == 0,
        MORTGAGE_PRINCIPAL: lambda c: 2110 <= c <= 2290 and c % 10 == 0,
        MORTGAGE_CLEARING: lambda c: 2115 <= c <= 2295 and c % 10 == 5,
        ESCROW: lambda c: 1110 <= c <= 1490 and c % 10 == 0,
    }
    for prop in session.exec(select(Property)).all():
        assert in_range[PROPERTY_ASSET](prop.asset_ledger_code)
    for loan in session.exec(select(LoanAccount)).all():
        assert in_range[MORTGAGE_PRINCIPAL](loan.principal_ledger_code)
        assert in_range[MORTGAGE_CLEARING](loan.clearing_ledger_code)
        assert in_range[ESCROW](loan.escrow_ledger_code)


def test_allocation_never_renumbers_an_entity_that_already_has_a_code(session: Session):
    """Re-running the allocator is idempotent, so posted history stays readable."""
    _portfolio(session, properties=2, loans=2)
    ensure_entity_ledger_accounts(session)
    before = {
        prop.id: prop.asset_ledger_code for prop in session.exec(select(Property)).all()
    }
    loan_before = {
        loan.id: (loan.principal_ledger_code, loan.clearing_ledger_code, loan.escrow_ledger_code)
        for loan in session.exec(select(LoanAccount)).all()
    }

    # A new door arrives later; it must not disturb the existing numbering.
    session.add(Property(
        id="prop_alloc_late", name="Late Door", address_line1="99 Late St",
        city="Springfield", zip_code="62701", property_type="RENTAL_INVESTMENT",
        created_at=BASE + timedelta(days=1),
    ))
    session.flush()
    result = ensure_entity_ledger_accounts(session)

    after = {prop.id: prop.asset_ledger_code for prop in session.exec(select(Property)).all()}
    assert {k: v for k, v in after.items() if k != "prop_alloc_late"} == before
    assert after["prop_alloc_late"] not in before.values()
    assert result["properties"] == 1, "only the new door should have been allocated"

    loan_after = {
        loan.id: (loan.principal_ledger_code, loan.clearing_ledger_code, loan.escrow_ledger_code)
        for loan in session.exec(select(LoanAccount)).all()
    }
    assert loan_after == loan_before


def test_standard_accounts_are_seeded_once_and_shared(session: Session):
    """Portfolio-invariant accounts are seeded exactly once and never re-created."""
    seed_standard_accounts(session)
    first = {row.code for row in session.exec(select(LedgerAccount)).all()}
    seed_standard_accounts(session)
    second = {row.code for row in session.exec(select(LedgerAccount)).all()}

    assert first == second
    assert coa.RENTAL_INCOME_CODE in first
    assert coa.INFLOW_SUSPENSE_CODE in first
    assert len([c for c in first if c == coa.MORTGAGE_INTEREST_CODE]) == 1


def test_escaping_a_class_range_fails_loudly(session: Session):
    """Exhaustion raises rather than silently borrowing another class's range.

    A guessed code would give one ledger account two meanings and make every
    figure derived from it ambiguous, so the allocator refuses instead.
    """
    seed_standard_accounts(session)
    start, stop, step = ALLOCATION_RANGES[CARD_LIABILITY][0]
    capacity = len(range(start, stop + 1, step))
    for _ in range(capacity):
        allocate_ledger_account(session, CARD_LIABILITY, "card")
    session.commit()

    with pytest.raises(LedgerCodeExhaustedError):
        allocate_ledger_account(session, CARD_LIABILITY, "one card too many")


def test_allocator_skips_codes_already_taken_by_other_classes(session: Session):
    """Ranges may pass over a standard account; the allocator steps around it.

    The escrow range spans 1110-1490, which contains the shared Rent Receivable
    account (1210). Escrow allocation must skip it rather than replace it.
    """
    seed_standard_accounts(session)
    receivable = session.get(LedgerAccount, coa.RENT_RECEIVABLE_CODE)
    assert receivable is not None

    allocated = [
        allocate_ledger_account(session, ESCROW, f"escrow {index}").code
        for index in range(4)
    ]
    session.commit()

    assert coa.RENT_RECEIVABLE_CODE not in allocated
    assert len(set(allocated)) == 4
    assert session.get(LedgerAccount, coa.RENT_RECEIVABLE_CODE).name == receivable.name
