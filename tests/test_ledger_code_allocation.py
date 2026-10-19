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

    A guessed code would give one ledger account two meanings and make every figure
    derived from it ambiguous, so the allocator refuses instead. Capacity is reached
    by declaring entities -- the allocator hands out one account per entity and
    adopts an existing compatible account rather than minting a second one.
    """
    session.add(FinancialInstitution(id="inst_exhaust", name="Bank"))
    session.flush()
    start_, stop, step = ALLOCATION_RANGES[CARD_LIABILITY][0]
    capacity = len(range(start_, stop + 1, step))
    for index in range(capacity):
        _account(session, f"acct_exhaust_{index}", "CREDIT_CARD", BASE + timedelta(minutes=index), "inst_exhaust")
    session.flush()
    ensure_entity_ledger_accounts(session)

    codes = {
        session.get(FinancialAccount, f"acct_exhaust_{index}").ledger_account_code
        for index in range(capacity)
    }
    assert len(codes) == capacity, "each declared card must hold its own account"

    _account(session, "acct_exhaust_one_too_many", "CREDIT_CARD", BASE + timedelta(days=1), "inst_exhaust")
    session.flush()
    with pytest.raises(LedgerCodeExhaustedError):
        ensure_entity_ledger_accounts(session)


def test_adopting_is_idempotent_and_never_takes_a_standard_account(session: Session):
    """The escrow range passes over the shared receivable and does not re-mint.

    The escrow range spans 1110-1490, which contains the standard Rent Receivable
    account (1210). An entity must never be handed that account -- it means
    something else -- and an entity that already holds a code keeps it, so posting
    history stays attributed to the account it was written against.
    """
    seed_standard_accounts(session)
    session.add(FinancialInstitution(id="inst_escrow", name="Bank"))
    session.flush()
    for index in range(4):
        _account(session, f"acct_escrow_{index}", "CHECKING", BASE + timedelta(minutes=index), "inst_escrow")
        session.flush()
        session.add(Property(
            id=f"prop_escrow_{index}", name=f"Door {index}", address_line1=f"{index} St",
            city="Springfield", zip_code="62701", property_type="RENTAL_INVESTMENT",
            created_at=BASE + timedelta(hours=1, minutes=index),
        ))
        mortgage_account = _account(
            session, f"acct_escrow_mtg_{index}", "MORTGAGE",
            BASE + timedelta(hours=2, minutes=index), "inst_escrow",
        )
        session.flush()
        session.add(LoanAccount(
            id=f"loan_escrow_{index}", financial_account_id=mortgage_account.id,
            property_id=f"prop_escrow_{index}", loan_number=f"E-{index}",
            created_at=BASE + timedelta(hours=2, minutes=index),
        ))
    session.flush()

    ensure_entity_ledger_accounts(session)

    escrow_codes = [session.get(LoanAccount, f"loan_escrow_{i}").escrow_ledger_code for i in range(4)]
    assert len(set(escrow_codes)) == 4, "each loan must own its own escrow account"
    assert coa.RENT_RECEIVABLE_CODE not in escrow_codes
    assert session.get(LedgerAccount, coa.RENT_RECEIVABLE_CODE).name == "Rent Receivable (Accrued)"

    # Re-running changes nothing: the accounts are adopted, not re-created.
    before = {loan.id: loan.escrow_ledger_code for loan in session.exec(select(LoanAccount)).all()}
    ensure_entity_ledger_accounts(session)
    after = {loan.id: loan.escrow_ledger_code for loan in session.exec(select(LoanAccount)).all()}
    assert before == after


def test_loans_adopt_the_accounts_that_name_their_own_servicer(session: Session):
    """A migrated chart pairs each loan with its own accounts, not the next free ones.

    A real portfolio's chart named its accounts "Mortgage Debt: Rocket (1000000001)",
    "Mortgage Clearing: Rocket" and "Servicer Escrow Reserve: Rocket Mortgage". Pairing
    by position instead crossed Rocket and CrossCountry: the totals still added up,
    because the same accounts were in use, but each door's debt, interest and escrow
    reserve were attributed to the other door -- silently, and only per door.
    """
    session.add(FinancialInstitution(id="inst_rocket", name="Rocket Mortgage"))
    session.add(FinancialInstitution(id="inst_pnc", name="PNC Bank"))
    session.add(FinancialInstitution(id="inst_ccm", name="CrossCountry Mortgage"))
    session.flush()

    # The chart as the previous deployment wrote it, deliberately NOT in creation order.
    session.add_all([
        LedgerAccount(code=2110, name="Mortgage Debt: Rocket (1000000001)", account_type="LIABILITY", normal_balance="CREDIT"),
        LedgerAccount(code=2115, name="Mortgage Clearing: Rocket", account_type="LIABILITY", normal_balance="CREDIT"),
        LedgerAccount(code=1110, name="Servicer Escrow Reserve: Rocket Mortgage", account_type="ASSET", normal_balance="DEBIT"),
        LedgerAccount(code=2120, name="Mortgage Debt: PNC (1000000002)", account_type="LIABILITY", normal_balance="CREDIT"),
        LedgerAccount(code=2125, name="Mortgage Clearing: PNC", account_type="LIABILITY", normal_balance="CREDIT"),
        LedgerAccount(code=1120, name="Servicer Escrow Reserve: PNC Mortgage", account_type="ASSET", normal_balance="DEBIT"),
        LedgerAccount(code=2130, name="Mortgage Debt: CrossCountry (1000000003)", account_type="LIABILITY", normal_balance="CREDIT"),
        LedgerAccount(code=2135, name="Mortgage Clearing: CrossCountry", account_type="LIABILITY", normal_balance="CREDIT"),
        LedgerAccount(code=1130, name="Servicer Escrow Reserve: CrossCountry", account_type="ASSET", normal_balance="DEBIT"),
    ])
    session.flush()

    # CrossCountry's loan is created FIRST, so position-based adoption would give it
    # Rocket's accounts.
    for index, (loan_number, institution, expected) in enumerate((
        ("1000000003", "inst_ccm", (2130, 2135, 1130)),
        ("1000000001", "inst_rocket", (2110, 2115, 1110)),
        ("1000000002", "inst_pnc", (2120, 2125, 1120)),
    )):
        prop = Property(
            id=f"prop_pair_{index}", name=f"Door {index}", address_line1=f"{index} St",
            city="Springfield", zip_code="62701", property_type="RENTAL_INVESTMENT",
            created_at=BASE + timedelta(minutes=index),
        )
        account = FinancialAccount(
            id=f"acct_pair_{index}", institution_id=institution, account_name="Servicing",
            account_type="MORTGAGE", account_number_masked="...0000",
            created_at=BASE + timedelta(hours=1, minutes=index),
        )
        session.add(prop)
        session.add(account)
        session.flush()
        session.add(LoanAccount(
            id=f"loan_pair_{index}", financial_account_id=account.id, property_id=prop.id,
            loan_number=loan_number, created_at=BASE + timedelta(hours=2, minutes=index),
        ))
    session.flush()

    ensure_entity_ledger_accounts(session)

    for index, (loan_number, _institution, expected) in enumerate((
        ("1000000003", "inst_ccm", (2130, 2135, 1130)),
        ("1000000001", "inst_rocket", (2110, 2115, 1110)),
        ("1000000002", "inst_pnc", (2120, 2125, 1120)),
    )):
        loan = session.get(LoanAccount, f"loan_pair_{index}")
        actual = (loan.principal_ledger_code, loan.clearing_ledger_code, loan.escrow_ledger_code)
        assert actual == expected, f"loan {loan_number} was paired with {actual}, not {expected}"
        # And the account it claimed really does name it.
        servicer = session.get(LedgerAccount, actual[0]).name.upper()
        assert loan_number in servicer, servicer


def test_identity_matching_ignores_words_that_describe_the_account(session: Session):
    """Only distinctive tokens identify an account.

    Every clearing account contains "Mortgage", and several contain "Bank", so matching
    on those would pair a loan with whichever account came first. The words are filtered
    out, and a token must be long enough to be specific; what remains is the loan number
    or the servicer's own name.
    """
    from proptally.domain.accounting.coa import _adopt_by_identity

    session.add_all([
        LedgerAccount(code=2115, name="Mortgage Clearing: Rocket", account_type="LIABILITY", normal_balance="CREDIT"),
        LedgerAccount(code=2125, name="Mortgage Clearing: Someone Else Bank", account_type="LIABILITY", normal_balance="CREDIT"),
    ])
    session.flush()

    # Generic words alone identify nothing, so nothing is adopted: the caller falls back
    # to the next free account in the class's range.
    assert _adopt_by_identity(session, MORTGAGE_CLEARING, ("Mortgage Bank",), claimed=set()) is None
    assert _adopt_by_identity(session, MORTGAGE_CLEARING, ("Clearing", "Account"), claimed=set()) is None
    assert _adopt_by_identity(session, MORTGAGE_CLEARING, ("",), claimed=set()) is None

    # A distinctive token does identify its account -- including a shortened form of an
    # institution's name and a three-character servicer.
    assert _adopt_by_identity(session, MORTGAGE_CLEARING, ("Rocket Mortgage",), claimed=set()) == 2115
    assert _adopt_by_identity(session, MORTGAGE_CLEARING, ("Rocket",), claimed=set()) == 2115
    assert _adopt_by_identity(session, MORTGAGE_CLEARING, ("Somebody Else Bank",), claimed=set()) == 2125

    # An account already claimed by another entity is not offered again.
    assert _adopt_by_identity(session, MORTGAGE_CLEARING, ("Rocket",), claimed={2115}) is None
