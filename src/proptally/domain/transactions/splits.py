"""Transaction splitting and ledger posting service enforcing exact reconciliation and governance."""

from datetime import datetime, timezone
from typing import List, Optional
from uuid import uuid4

from pydantic import BaseModel, Field
from sqlmodel import Session, select

from proptally.core.exceptions import AccountNotFoundError, InvalidSplitSumError
from proptally.db.models import (
    AuditLog,
    FinancialAccount,
    JournalEntry,
    LoanAccount,
    PostingAuthorization,
    Transaction,
    TransactionProcessingState,
    TransactionSplit,
)
from proptally.db.system_models import ROLE_CPA_REVIEWER
from proptally.domain.accounting.coa import (
    BANK_FEES_CODE,
    COMMISSIONS_CODE,
    FEE_INCOME_CODE,
    HAZARD_INSURANCE_CODE,
    HOA_CODE,
    LEGAL_AND_LATE_FEE_CODE,
    MANAGEMENT_CODE,
    MORTGAGE_ACCOUNT_TYPE,
    PERSONAL_DRAW_CODE,
    PROPERTY_TAX_CODE,
    RENTAL_INCOME_CODE,
    REPAIRS_CODE,
    SNOW_LANDSCAPING_CODE,
    SUPPLIES_CODE,
    TENANT_DEPOSITS_HELD_CODE,
    UTILITIES_CODE,
    allocate_account_ledger_account,
)
from proptally.domain.accounting.ledger import (
    add_journal_line,
    create_journal_entry,
    post_journal_entry,
    reverse_journal_entry,
)
from proptally.domain.transactions.policy import (
    PostingPolicy,
    assert_posting_authorized,
    evaluate_posting_policy,
    is_card_settlement,
)

#: Split category -> operating-expense ledger account. Categories are
#: portfolio-agnostic and the codes are the fixed standard accounts, so this map
#: is identical in every portfolio.
CATEGORY_TO_OPEX_CODE = {
    "TAXES": PROPERTY_TAX_CODE,
    "INSURANCE": HAZARD_INSURANCE_CODE,
    "REPAIRS": REPAIRS_CODE,
    "SUPPLIES": SUPPLIES_CODE,
    "SNOW_LANDSCAPING": SNOW_LANDSCAPING_CODE,
    "UTILITIES": UTILITIES_CODE,
    "BANK_FEES": BANK_FEES_CODE,
    "MANAGEMENT": MANAGEMENT_CODE,
    "COMMISSIONS": COMMISSIONS_CODE,
    "HOA": HOA_CODE,
    "LEGAL": LEGAL_AND_LATE_FEE_CODE,
    "PERSONAL": PERSONAL_DRAW_CODE,
}


class SplitItem(BaseModel):
    """Specification for an individual transaction split allocation."""
    property_id: Optional[str] = None  # None indicates personal / unassigned
    category: str  # 'RENT_INCOME', 'REPAIRS', 'SUPPLIES', 'TAXES', 'PERSONAL'
    amount_cents: int = Field(gt=0, description="Strictly positive integer cents")
    schedule_e_line: Optional[int] = None
    tax_review_status: str = "UNCLASSIFIED"
    notes: Optional[str] = None


def apply_transaction_splits(
    session: Session,
    transaction_id: str,
    splits: List[SplitItem],
    allow_partial: bool = False,
    commit: bool = True,
    actor_id: str = "system",
) -> TransactionProcessingState:
    """Allocates a parent transaction into one or more category/property splits.
    
    Invariants enforced:
    1. Parent transaction must exist.
    2. All split amounts must be strictly positive integers (> 0).
    3. Sum of splits must strictly equal abs(parent.amount_cents) unless allow_partial is True.
    4. If allow_partial is True, sum of splits must be <= abs(parent.amount_cents).

    Pass ``commit=False`` to join a caller-owned transaction.
    """
    txn = session.get(Transaction, transaction_id)
    if not txn:
        raise ValueError(f"Transaction {transaction_id} not found.")

    existing_state = session.get(TransactionProcessingState, transaction_id)
    if existing_state is not None and existing_state.posting_status == "POSTED":
        raise ValueError(
            f"Transaction {transaction_id} is already POSTED and cannot be re-split. "
            "Use reclassify_and_repost_transaction to preserve the posted history."
        )

    target_total = abs(txn.amount_cents)
    sum_splits = sum(s.amount_cents for s in splits)

    if sum_splits > target_total:
        raise InvalidSplitSumError(
            f"Sum of split amounts ({sum_splits} cents) exceeds parent transaction total ({target_total} cents)."
        )

    if not allow_partial and sum_splits != target_total:
        raise InvalidSplitSumError(
            f"Sum of split amounts ({sum_splits} cents) does not equal parent transaction total ({target_total} cents)."
        )

    # Clear existing splits for this transaction if re-splitting
    existing_splits = session.exec(
        select(TransactionSplit).where(TransactionSplit.transaction_id == transaction_id)
    ).all()
    for s in existing_splits:
        session.delete(s)
    session.flush()

    # Create new split records
    for s in splits:
        split_record = TransactionSplit(
            id=str(uuid4()),
            transaction_id=transaction_id,
            property_id=s.property_id,
            category=s.category,
            amount_cents=s.amount_cents,
            schedule_e_line=s.schedule_e_line,
            tax_review_status=s.tax_review_status,
            notes=s.notes,
        )
        session.add(split_record)

    # Update or create processing state
    proc_state = session.get(TransactionProcessingState, transaction_id)
    if not proc_state:
        proc_state = TransactionProcessingState(
            transaction_id=transaction_id,
            allocation_status="UNALLOCATED",
            allocated_cents=0,
            residual_cents=target_total,
            posting_status="UNPOSTED",
        )
        session.add(proc_state)

    proc_state.allocated_cents = sum_splits
    proc_state.residual_cents = target_total - sum_splits

    if proc_state.residual_cents == 0:
        proc_state.allocation_status = "FULLY_ALLOCATED"
    elif sum_splits > 0:
        proc_state.allocation_status = "PARTIALLY_ALLOCATED"
    else:
        proc_state.allocation_status = "UNALLOCATED"

    # Audit log
    audit = AuditLog(
        id=str(uuid4()),
        entity_name="transactions",
        entity_id=transaction_id,
        action="SPLIT",
        changed_by=actor_id,
        new_values_json=f'{{"splits_count": {len(splits)}, "status": "{proc_state.allocation_status}", "residual_cents": {proc_state.residual_cents}}}',
    )
    session.add(audit)
    if commit:
        session.commit()
        session.refresh(proc_state)
    else:
        session.flush()
    return proc_state


def _record_posting_authorization(
    session: Session,
    txn: Transaction,
    entry: JournalEntry,
    policy: PostingPolicy,
    splits: list[TransactionSplit],
    client_confirmed: bool,
    user_attested_cpa_review: bool,
    actor_id: str = "system",
    actor_role: Optional[str] = None,
    actor_verified_cpa_reviewer: bool = False,
) -> None:
    """Appends the immutable authorization record for a posting.

    Both gates are written independently with their own timestamp, together with
    what the policy required and the allocation it applied to. The parents are
    already flushed (the transaction was loaded and ``create_journal_entry``
    flushes the header), so the foreign keys are satisfied on insert.

    When an external-review attestation is supplied, it is attributed to the
    acting identity: ``authorized_by_user_id``/``authorized_by_role`` name who
    asserted it and in what capacity. ``verified_cpa_reviewer`` is set only for a
    sign-off by an identity the caller established as a ``CPA_REVIEWER`` member;
    an owner self-attestation can never be promoted to a verified authorization.
    """
    authorized_at = datetime.now(timezone.utc)
    attested = bool(user_attested_cpa_review)
    verified_reviewer = (
        attested
        and actor_role == ROLE_CPA_REVIEWER
        and bool(actor_verified_cpa_reviewer)
    )
    session.add(
        PostingAuthorization(
            id=str(uuid4()),
            transaction_id=txn.id,
            journal_entry_id=entry.id,
            required_client_confirmation=policy.requires_client_confirmation,
            required_cpa_review=policy.requires_cpa_review,
            client_confirmed=client_confirmed,
            client_confirmed_at=authorized_at if client_confirmed else None,
            user_attested_cpa_review=user_attested_cpa_review,
            user_attested_cpa_review_at=authorized_at if user_attested_cpa_review else None,
            authorized_by_user_id=actor_id if attested else None,
            authorized_by_role=actor_role if attested else None,
            verified_cpa_reviewer=verified_reviewer,
            split_count=len(splits),
            split_total_cents=sum(s.amount_cents for s in splits),
            policy_reasons="; ".join(policy.reasons) or None,
        )
    )
    session.flush()


def _ledger_code_for_account(session: Session, account) -> int:
    """The ledger account a bank, card or mortgage account settles in.

    The code lives on the account row (``FinancialAccount.ledger_account_code``),
    or on its loan for a mortgage account, which has no ledger account of its own.
    A row created before per-entity allocation existed is coded on demand rather
    than guessed, so two accounts can never share one ledger account by accident.
    """
    if account is None:
        raise ValueError("Cannot resolve a ledger account for a missing financial account.")

    if getattr(account, "account_type", None) == MORTGAGE_ACCOUNT_TYPE:
        loan = session.exec(
            select(LoanAccount).where(LoanAccount.financial_account_id == account.id)
        ).first()
        if loan is None or loan.clearing_ledger_code is None:
            raise ValueError(
                f"Mortgage account {account.id} has no loan, so it has no ledger accounts. "
                "Create the loan (LoanAccount) before posting against its settlement account."
            )
        return loan.clearing_ledger_code

    if account.ledger_account_code is not None:
        return account.ledger_account_code

    code = allocate_account_ledger_account(session, account)
    if code is None:
        raise ValueError(
            f"Account {account.id} of type {account.account_type!r} has no ledger allocation "
            "class, so no ledger account can be resolved for it."
        )
    return code


def _resolve_split_account_code(session: Session, category: str, property_id: Optional[str]) -> int:
    """Maps a split's category (and door, for mortgage payments) to a ledger account.

    Rental income is a single generic account for every door: which property earned
    it is recorded by the journal line's ``property_id`` attribution, not by giving
    each door its own income code.
    """
    cat_upper = category.upper()
    if cat_upper in ("RENT_INCOME", "RENT"):
        return RENTAL_INCOME_CODE
    if cat_upper in ("LATE_FEE", "FEE"):
        return FEE_INCOME_CODE
    if cat_upper == "SECURITY_DEPOSIT":
        return TENANT_DEPOSITS_HELD_CODE
    if cat_upper in CATEGORY_TO_OPEX_CODE:
        return CATEGORY_TO_OPEX_CODE[cat_upper]
    if cat_upper == "MORTGAGE":
        return _mortgage_clearing_code(session, property_id)
    if cat_upper == "PERSONAL":
        return PERSONAL_DRAW_CODE
    # An unknown category must never be booked as owner drawings: that would
    # silently make a real expense non-deductible. Fail loudly instead.
    raise AccountNotFoundError(
        f"Split category {category!r} has no ledger account mapping."
    )


def _mortgage_clearing_code(session: Session, property_id: Optional[str]) -> int:
    """The clearing account of the door's active loan, for a mortgage allocation."""
    if property_id:
        loan = session.exec(
            select(LoanAccount).where(
                LoanAccount.property_id == property_id,
                LoanAccount.is_active == True,  # noqa: E712
            )
        ).first()
        if loan is not None and loan.clearing_ledger_code is not None:
            return loan.clearing_ledger_code
    raise ValueError(
        "A MORTGAGE allocation needs the door's active loan to resolve its clearing "
        f"account, and property {property_id!r} has none. Create the loan first."
    )


def check_governance_authorization(
    session: Session,
    txn: Transaction,
    splits: List[TransactionSplit],
    client_confirmed: bool = False,
    user_attested_cpa_review: bool = False,
) -> PostingPolicy:
    """Enforces CBD-1 and CBD-2 by delegating to the shared policy module.

    This is the single enforcement point for every posting path: the register,
    the reconciliation queue and reclassification all reach it through
    ``post_transaction_to_ledger``. Authorization is only ever what the caller
    supplies from the request; split review status is not an authorization grant,
    and a user self-attestation is never treated as a verified CPA approval.
    """
    policy = evaluate_posting_policy(session, txn, splits)
    assert_posting_authorized(
        policy,
        client_confirmed=client_confirmed,
        user_attested_cpa_review=user_attested_cpa_review,
    )
    return policy


def post_transaction_to_ledger(
    session: Session,
    transaction_id: str,
    client_confirmed: bool = False,
    user_attested_cpa_review: bool = False,
    idempotency_suffix: str = "",
    commit: bool = True,
    actor_id: str = "system",
    actor_role: Optional[str] = None,
    actor_verified_cpa_reviewer: bool = False,
) -> JournalEntry:
    """Atomically posts an approved and fully allocated transaction into double-entry journal entries.

    Invariants:
    1. Transaction must exist and be FULLY_ALLOCATED.
    2. Cannot post unclassified splits.
    3. Enforces CBD-1 and CBD-2 authorization gates.
    4. Idempotent: returns existing posted entry if already posted.
    5. Balance equilibrium: debits strictly equal credits.
    6. A credit-card settlement is a transfer: it moves the liability down and
       checking down, and never re-recognises the underlying expense.

    Pass ``commit=False`` to join a caller-owned transaction, so a multi-step
    operation (e.g. reclassification) can fail without leaving partial history.
    """
    txn = session.get(Transaction, transaction_id)
    if not txn:
        raise ValueError(f"Transaction {transaction_id} not found.")

    proc_state = session.get(TransactionProcessingState, transaction_id)
    if not proc_state or proc_state.allocation_status != "FULLY_ALLOCATED":
        status = proc_state.allocation_status if proc_state else "UNKNOWN"
        raise ValueError(f"Transaction {transaction_id} is '{status}'; must be 'FULLY_ALLOCATED' to post.")

    # Idempotency check: if already posted and active, return existing entry
    if proc_state.posting_status == "POSTED" and proc_state.journal_entry_id and not idempotency_suffix:
        existing_je = session.get(JournalEntry, proc_state.journal_entry_id)
        if existing_je and existing_je.status == "POSTED":
            return existing_je

    splits = session.exec(
        select(TransactionSplit).where(TransactionSplit.transaction_id == transaction_id)
    ).all()
    if not splits:
        raise ValueError(f"No splits found for transaction {transaction_id}.")

    split_total = sum(s.amount_cents for s in splits)
    if split_total != abs(txn.amount_cents):
        raise InvalidSplitSumError(
            f"Split total ({split_total} cents) does not equal transaction total "
            f"({abs(txn.amount_cents)} cents); refusing to post."
        )

    # Reject unclassified splits
    if any(s.tax_review_status == "UNCLASSIFIED" for s in splits):
        raise ValueError("Cannot post transaction with UNCLASSIFIED splits.")

    # Check governance rules
    policy = check_governance_authorization(
        session,
        txn,
        splits,
        client_confirmed=client_confirmed,
        user_attested_cpa_review=user_attested_cpa_review,
    )

    # Determine balancing account
    fin_acct = session.get(FinancialAccount, txn.financial_account_id)
    counter_acct = (
        session.get(FinancialAccount, txn.counter_account_id) if txn.counter_account_id else None
    )
    balancing_code = _ledger_code_for_account(session, fin_acct)

    total_amount = abs(txn.amount_cents)
    is_settlement = is_card_settlement(
        session, txn, source_account=fin_acct, counter_account=counter_acct
    )

    # Determine entry type and idempotency key
    if is_settlement:
        entry_type = "CARD_SETTLEMENT"
    elif txn.direction == "OUTFLOW":
        entry_type = "EXPENSE_POST"
    else:
        entry_type = "REVENUE_POST"
    base_key = f"TXN_POST:{txn.id}"
    key = f"{base_key}:{idempotency_suffix}" if idempotency_suffix else base_key

    # Create journal entry
    je = create_journal_entry(
        session=session,
        entry_date=txn.transaction_date,
        entry_type=entry_type,
        description=f"Posted transaction: {txn.raw_description}",
        idempotency_key=key,
        source_transaction_id=txn.id,
    )

    # Persist the authorization evidence with the posting. Both gates are stored
    # independently so the record shows what was required and what was supplied,
    # even when a later gate would have collapsed them into one status.
    _record_posting_authorization(
        session,
        txn=txn,
        entry=je,
        policy=policy,
        splits=splits,
        client_confirmed=client_confirmed,
        user_attested_cpa_review=user_attested_cpa_review,
        actor_id=actor_id,
        actor_role=actor_role,
        actor_verified_cpa_reviewer=actor_verified_cpa_reviewer,
    )

    if is_settlement:
        # Transfer: reduce the card liability and pay it from the deposit account.
        # The purchase already recognised the expense, and the allocation carried
        # on the splits is reporting attribution only, so nothing is expensed here.
        card_code = _ledger_code_for_account(session, counter_acct)
        add_journal_line(
            session,
            je.id,
            ledger_account_code=card_code,
            debit_cents=total_amount,
            description=f"Card settlement: {counter_acct.account_name}",
        )
        add_journal_line(
            session,
            je.id,
            ledger_account_code=balancing_code,
            credit_cents=total_amount,
            description="Card settlement paid from deposit account",
        )
    elif txn.direction == "INFLOW":
        # Debit cash/checking, credit revenue/splits
        add_journal_line(session, je.id, ledger_account_code=balancing_code, debit_cents=total_amount)
        for s in splits:
            code = _resolve_split_account_code(session, s.category, s.property_id)
            add_journal_line(
                session,
                je.id,
                ledger_account_code=code,
                credit_cents=s.amount_cents,
                property_id=s.property_id,
                description=s.notes or s.category,
            )
    else:
        # Debit expenses/splits, credit cash/card liability
        for s in splits:
            code = _resolve_split_account_code(session, s.category, s.property_id)
            add_journal_line(
                session,
                je.id,
                ledger_account_code=code,
                debit_cents=s.amount_cents,
                property_id=s.property_id,
                description=s.notes or s.category,
            )
        add_journal_line(session, je.id, ledger_account_code=balancing_code, credit_cents=total_amount)

    posted_entry = post_journal_entry(session, je.id, commit=False, actor_id=actor_id)

    # Update processing state
    proc_state.posting_status = "POSTED"
    proc_state.journal_entry_id = posted_entry.id
    session.add(proc_state)
    if commit:
        session.commit()
        session.refresh(proc_state)
    else:
        session.flush()
    return posted_entry


def reclassify_and_repost_transaction(
    session: Session,
    transaction_id: str,
    new_splits: list[SplitItem],
    client_confirmed: bool = False,
    user_attested_cpa_review: bool = False,
    reversal_reason: str = "Reclassification of transaction splits",
    actor_id: str = "system",
    actor_role: Optional[str] = None,
    actor_verified_cpa_reviewer: bool = False,
) -> JournalEntry:
    """Safely updates a transaction's splits by reversing the prior posted entry and posting a new replacement.

    Preserves complete accounting history: the previous posted entry remains immutable,
    a compensating reversal entry cancels it, and a new replacement entry is posted.

    The reversal, the reallocation and the replacement entry are committed as a single
    unit of work. If any rule rejects the new classification, the whole operation is
    rolled back and the ledger is left exactly as it stood before the call.
    """
    txn = session.get(Transaction, transaction_id)
    if not txn:
        raise ValueError(f"Transaction {transaction_id} not found.")

    proc_state = session.get(TransactionProcessingState, transaction_id)
    if not proc_state or proc_state.posting_status != "POSTED" or not proc_state.journal_entry_id:
        raise ValueError(f"Transaction {transaction_id} has no POSTED entry to reclassify.")

    reversal_target_id = proc_state.journal_entry_id

    try:
        # 1. Reverse the superseded entry (history is never edited in place).
        reverse_journal_entry(
            session=session,
            entry_id=reversal_target_id,
            reversal_date=datetime.now(timezone.utc).date(),
            reason=reversal_reason,
            commit=False,
            actor_id=actor_id,
        )
        proc_state.posting_status = "REVERSED"
        session.add(proc_state)
        session.flush()

        # 2. Allocate the corrected classification.
        apply_transaction_splits(
            session, transaction_id, new_splits, allow_partial=False, commit=False,
            actor_id=actor_id,
        )

        # 3. Post the replacement entry under a fresh idempotency key.
        replacement = post_transaction_to_ledger(
            session=session,
            transaction_id=transaction_id,
            client_confirmed=client_confirmed,
            user_attested_cpa_review=user_attested_cpa_review,
            idempotency_suffix=f"v_{uuid4().hex[:6]}",
            commit=False,
            actor_id=actor_id,
            actor_role=actor_role,
            actor_verified_cpa_reviewer=actor_verified_cpa_reviewer,
        )
        session.commit()
    except Exception:
        # Never leave a reversal stranded without its replacement.
        session.rollback()
        raise

    session.refresh(replacement)
    return replacement
