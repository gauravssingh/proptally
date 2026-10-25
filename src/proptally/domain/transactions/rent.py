"""Tenant rent evidence, candidate discovery, and rent payment posting.

A bank description is the only tenant evidence most portfolios have: no lease
documents were imported. A Zelle payment names the remitter and, when the payer
writes one, a memo saying whether the money is rent or a deposit; a bare
``Zelle Transfer Conf#`` names the remitter only. This module turns that
evidence into reviewable candidates and posts only what a person confirms.

Three rules keep it honest:

* discovery produces *proposals*. A door is proposed from a confirmed remitter
  alias, a registered lease, a memo address or a lease rent amount; anything
  else is shown as needing a door rather than guessed.
* a deposit is a tenant liability, not Schedule E rent. A memo that mixes rent
  and a deposit is left for manual splitting because the amounts are not stated
  separately.
* posting goes through the same split/posting services and the same CBD-1 /
  CBD-2 policy as every other entry point. A gated payment is allocated and
  left unposted so a human can supply the authorization.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Iterable, Sequence
from uuid import uuid4

from sqlalchemy import func
from sqlmodel import Session, select

from proptally.db.models import (
    AuditLog,
    Lease,
    Property,
    RentDismissal,
    TenantAlias,
    Transaction,
    TransactionProcessingState,
)
from proptally.db.tenant_manager import slugify
from proptally.domain.transactions.policy import (
    assert_posting_authorized,
    classification_status_for,
    evaluate_posting_policy,
)
from proptally.domain.transactions.splits import (
    SplitItem,
    apply_transaction_splits,
    post_transaction_to_ledger,
)

_RENT_WORD = re.compile(r"\brent(?:al)?\b", re.I)
_DEPOSIT_WORD = re.compile(r"\bdeposit\b", re.I)
# "Zelle payment from NAME for "memo"" - the richest form: remitter and intent.
_ZELLE_MEMO = re.compile(
    r"Zelle\s+payment\s+from\s+(?P<name>.+?)\s+for\s+\"(?P<memo>[^\"]*)\"",
    re.I,
)
# "Zelle payment from NAME Conf# ..." - a remitter with no memo.
_ZELLE_FROM_NAMED = re.compile(
    r"Zelle\s+payment\s+from\s+(?P<name>.+?)(?:\s+Conf#|$)",
    re.I,
)
# "Zelle Transfer Conf# ABC123; NAME" - the bank's transfer record, name only.
_ZELLE_CONF_NAMED = re.compile(
    r"Zelle\s+Transfer\s+Conf#\s*\S+\s*;\s*(?P<name>.+)",
    re.I,
)
# A street number and street name, guarded against matching the tail of a
# decimal amount such as "$204.12 for lunch".
_ADDRESS_HINT = re.compile(
    r"(?<![\d.])\b(\d{2,6}\s+[A-Za-z][A-Za-z0-9 .'-]{1,40})",
    re.I,
)
# Tokens that never distinguish one remitter from another.
_NAME_NOISE = frozenset({"llc", "inc", "ltd", "co", "corp", "company"})
_RENT_CATEGORY = "RENT_INCOME"
_DEPOSIT_CATEGORY = "SECURITY_DEPOSIT"


@dataclass(frozen=True)
class TenantPayment:
    """The tenant evidence a bank description carries."""

    tenant_name: str
    memo: str
    property_hint: str | None
    is_rent: bool
    is_security_deposit: bool
    #: "zelle_memo" when the payer wrote a memo, "zelle_remitter" when the bank
    #: named only the remitter.
    name_source: str = "zelle_memo"

    @property
    def is_mixed_deposit_and_rent(self) -> bool:
        return self.is_rent and self.is_security_deposit

    @property
    def names_remitter_only(self) -> bool:
        return self.name_source == "zelle_remitter"


@dataclass(frozen=True)
class RentCandidate:
    """One unallocated inflow and what the evidence supports for it."""

    transaction: Transaction
    state: TransactionProcessingState | None
    payment: TenantPayment | None
    door: Property | None
    tenant_name: str | None
    category: str | None
    confidence: float
    #: The remitter exactly as the bank printed it; the alias key is derived
    #: from this, never from the lease's tenant name.
    remitter: str | None = None
    reasons: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()

    @property
    def is_settled(self) -> bool:
        state = self.state
        if state is None:
            return False
        return state.allocation_status == "FULLY_ALLOCATED" or state.posting_status == "POSTED"

    @property
    def is_actionable(self) -> bool:
        """Whether the evidence is complete enough to post without another choice."""
        return bool(self.door is not None and self.category is not None and not self.blockers)

    @property
    def amount_cents(self) -> int:
        return abs(self.transaction.amount_cents)


@dataclass(frozen=True)
class RentApplyOutcome:
    """What happened to one payment in an apply run."""

    transaction_id: str
    status: str  # POSTED | ALLOCATED | SKIPPED | ERROR | WOULD_POST | WOULD_ALLOCATE
    reason: str = ""
    tenant_name: str | None = None
    door_name: str | None = None
    category: str | None = None
    amount_cents: int = 0


@dataclass
class RentApplyReport:
    """Per-payment outcomes, so a run can report what it refused to do."""

    outcomes: list[RentApplyOutcome] = field(default_factory=list)
    leases_created: list[str] = field(default_factory=list)

    def _with_status(self, *statuses: str) -> list[RentApplyOutcome]:
        return [outcome for outcome in self.outcomes if outcome.status in statuses]

    @property
    def posted(self) -> list[RentApplyOutcome]:
        return self._with_status("POSTED")

    @property
    def allocated(self) -> list[RentApplyOutcome]:
        return self._with_status("ALLOCATED")

    @property
    def skipped(self) -> list[RentApplyOutcome]:
        return self._with_status("SKIPPED")

    @property
    def errors(self) -> list[RentApplyOutcome]:
        return self._with_status("ERROR")

    @property
    def rent_cents(self) -> int:
        return sum(
            outcome.amount_cents
            for outcome in self._with_status("POSTED", "ALLOCATED")
            if outcome.category == _RENT_CATEGORY
        )

    @property
    def deposit_cents(self) -> int:
        return sum(
            outcome.amount_cents
            for outcome in self._with_status("POSTED", "ALLOCATED")
            if outcome.category == _DEPOSIT_CATEGORY
        )


# --- Evidence -------------------------------------------------------------


def _display_name(value: str) -> str:
    """Turn a bank's upper-case remitter into a display name without guessing."""
    cleaned = re.sub(r"\s+", " ", value or "").strip(" ,;.-")
    if "," in cleaned:
        # "FRANKLIN, JOSE" and "FRANKLIN, JOSE A" are written last name first.
        last, _, first = cleaned.partition(",")
        cleaned = f"{first.strip()} {last.strip()}".strip()
    return cleaned.title()


def _name_key(value: str | None) -> str:
    """The remitter identity used for matching: no casing, initials or suffixes.

    "TYLER J SAXSMA", "Tyler Saxsma" and "SAXSMA, TYLER" all reduce to
    "tyler saxsma", so a lease and a bank remitter can be compared. Family
    members who share a surname keep distinct keys because the first name is
    part of the key.
    """
    cleaned = (value or "").casefold()
    if "," in cleaned:
        last, _, first = cleaned.partition(",")
        cleaned = f"{first} {last}"
    tokens = re.findall(r"[a-z]+", cleaned)
    kept = [token for token in tokens if len(token) > 1 and token not in _NAME_NOISE]
    return " ".join(kept)


def _property_hint(memo: str) -> str | None:
    match = _ADDRESS_HINT.search(memo or "")
    if not match:
        return None
    hint = re.sub(r"\s+", " ", match.group(1)).strip(" .,;")
    # Keep a street number and street name; drop trailing prose such as "pro-rent".
    tokens = hint.split()
    return " ".join(tokens[:3]) if len(tokens) >= 2 else None


def extract_tenant_payment(raw_description: str) -> TenantPayment | None:
    """Read remitter, memo and rent/deposit intent from a bank description.

    A quoted memo is the strongest evidence. When the bank names only the
    remitter, that name is still returned - as a candidate to confirm, never as
    a classification - because the person on the other side of the transfer is
    the thing a reviewer needs to see. ``Zelle payment to`` is an expense to
    someone else and is not tenant evidence.
    """
    description = " ".join((raw_description or "").split())
    match = _ZELLE_MEMO.search(description)
    if match:
        memo = match.group("memo").strip()
        name = _display_name(match.group("name"))
        if not name:
            return None
        return TenantPayment(
            tenant_name=name,
            memo=memo,
            property_hint=_property_hint(memo),
            is_rent=bool(_RENT_WORD.search(memo)),
            is_security_deposit=bool(_DEPOSIT_WORD.search(memo)),
            name_source="zelle_memo",
        )

    match = _ZELLE_CONF_NAMED.search(description) or _ZELLE_FROM_NAMED.search(description)
    if match:
        name = _display_name(match.group("name"))
        if not name:
            return None
        return TenantPayment(
            tenant_name=name,
            memo="",
            property_hint=None,
            is_rent=False,
            is_security_deposit=False,
            name_source="zelle_remitter",
        )
    return None


def _normalise_address(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()


def find_property_for_hint(session: Session, hint: str | None) -> Property | None:
    """Resolve an address fragment to exactly one active rental door."""
    if not hint:
        return None

    # A memo says "12307 Tinsley rent", the door says "12307 TINSLEY ST": the
    # street number and street name are the discriminating pair, and the words
    # after them are the payer's prose.
    tokens = _normalise_address(hint).split()
    if len(tokens) < 2:
        return None
    wanted = " ".join(tokens[:2])

    matches = []
    for prop in session.exec(
        select(Property).where(
            Property.property_type == "RENTAL_INVESTMENT",
            Property.is_active == True,  # noqa: E712
        )
    ).all():
        haystack = _normalise_address(f"{prop.name} {prop.address_line1}")
        if wanted in haystack:
            matches.append(prop)
    return matches[0] if len(matches) == 1 else None


# --- Candidate discovery --------------------------------------------------


def _active_rentals(session: Session) -> dict[str, Property]:
    doors = session.exec(
        select(Property).where(
            Property.property_type == "RENTAL_INVESTMENT",
            Property.is_active == True,  # noqa: E712
        )
    ).all()
    return {door.id: door for door in doors}


def _leases(session: Session) -> list[Lease]:
    """Every lease, active or not: a historical payment is attributed by its date."""
    return session.exec(select(Lease)).all()


def _lease_covers(lease: Lease, day: date) -> bool:
    """Whether a payment on ``day`` falls inside this lease term."""
    if lease.start_date and day < lease.start_date:
        return False
    if lease.end_date and day > lease.end_date:
        return False
    return True


def _leases_by_key(leases: Iterable[Lease]) -> dict[str, list[Lease]]:
    grouped: dict[str, list[Lease]] = defaultdict(list)
    for lease in leases:
        key = _name_key(lease.tenant_name)
        if key:
            grouped[key].append(lease)
    return grouped


def _aliases_by_key(session: Session) -> dict[str, TenantAlias]:
    return {alias.alias_key: alias for alias in session.exec(select(TenantAlias)).all()}


def dismissed_rent_transactions(session: Session) -> set[str]:
    """Transactions a person has ruled out as rent received."""
    return {row.transaction_id for row in session.exec(select(RentDismissal)).all()}


def dismiss_rent_candidate(
    session: Session,
    transaction_id: str,
    *,
    reason: str | None = None,
    actor_id: str = "rent-workbench",
) -> bool:
    """Keep a transaction out of the rent workbench without classifying it.

    The transaction stays in the review queue: "not rent" is a statement about
    the rent suggestion, not a decision about what the payment actually is.
    """
    txn = session.get(Transaction, transaction_id)
    if txn is None:
        raise ValueError("Transaction not found.")
    if txn.amount_cents <= 0:
        raise ValueError("Only inflows can be ruled out as rent received.")
    if session.get(RentDismissal, transaction_id) is not None:
        return False

    session.add(RentDismissal(transaction_id=transaction_id, reason=reason))
    session.add(
        AuditLog(
            id=str(uuid4()),
            entity_name="transactions",
            entity_id=transaction_id,
            action="RENT_DISMISSED",
            changed_by=actor_id,
            reason=reason,
        )
    )
    session.commit()
    return True


def restore_rent_candidate(
    session: Session,
    transaction_id: str,
    *,
    actor_id: str = "rent-workbench",
) -> bool:
    """Undo a "not rent" decision so the transaction is a candidate again."""
    dismissal = session.get(RentDismissal, transaction_id)
    if dismissal is None:
        return False
    session.delete(dismissal)
    session.add(
        AuditLog(
            id=str(uuid4()),
            entity_name="transactions",
            entity_id=transaction_id,
            action="RENT_RESTORED",
            changed_by=actor_id,
        )
    )
    session.commit()
    return True


def find_rent_candidates(
    session: Session,
    *,
    q: str | None = None,
    property_id: str | None = None,
    include_settled: bool = False,
    include_dismissed: bool = False,
    include_all: bool = False,
    max_scan: int = 1000,
) -> list[RentCandidate]:
    """Every inflow worth calling rent, newest first, with its evidence.

    ``include_all`` returns the whole scanned window instead of only rows that
    carry rent evidence; the apply path uses it so a row the reviewer found by
    search can still be classified. ``include_dismissed`` is for the "not rent"
    view. ``max_scan`` bounds the work: a portfolio with more inflows than that
    narrows the window with ``q`` or a door filter.
    """
    dismissed = dismissed_rent_transactions(session)
    rentals = _active_rentals(session)
    leases = _leases(session)
    leases_by_key = _leases_by_key(leases)
    aliases_by_key = _aliases_by_key(session)
    leases_by_amount: dict[int, list[Lease]] = defaultdict(list)
    for lease in leases:
        if lease.monthly_rent_cents:
            leases_by_amount[lease.monthly_rent_cents].append(lease)

    query = (
        select(Transaction)
        .where(Transaction.amount_cents > 0)
        .order_by(Transaction.transaction_date.desc(), Transaction.id.desc())
        .limit(max_scan)
    )
    if q and q.strip():
        query = query.where(func.lower(Transaction.raw_description).contains(q.strip().lower()))

    candidates: list[RentCandidate] = []
    for txn in session.exec(query).all():
        if txn.id in dismissed and not include_dismissed:
            continue
        state = session.get(TransactionProcessingState, txn.id)
        settled = bool(
            state is not None
            and (state.allocation_status == "FULLY_ALLOCATED" or state.posting_status == "POSTED")
        )
        if settled and not include_settled:
            continue

        payment = extract_tenant_payment(txn.raw_description)
        key = _name_key(payment.tenant_name) if payment else ""
        alias = aliases_by_key.get(key) if key else None
        lease_matches = [
            lease for lease in leases_by_key.get(key, []) if lease.property_id in rentals
        ] if key else []
        # A remitter is only attributable while their lease actually covers the
        # payment date: the previous tenant's payments are not the new tenant's.
        covering_leases = [
            lease for lease in lease_matches if _lease_covers(lease, txn.transaction_date)
        ]
        lease_doors = {lease.property_id for lease in covering_leases}
        remitter_outside_lease = bool(lease_matches) and not covering_leases

        proposed: Property | None = None
        tenant_name = payment.tenant_name if payment else None
        confidence = 0.0
        reasons: list[str] = []
        blockers: list[str] = []

        if alias is not None and alias.property_id in rentals:
            proposed = rentals[alias.property_id]
            tenant_name = alias.tenant_name
            confidence = 0.95
            reasons.append(f"Remitter confirmed as {alias.tenant_name} on {proposed.name}")
        elif len(lease_doors) == 1:
            lease = covering_leases[0]
            proposed = rentals[lease.property_id]
            tenant_name = lease.tenant_name
            confidence = 0.90
            reasons.append(f"Remitter matches the lease on {proposed.name}")
        elif len(lease_doors) > 1:
            blockers.append("Remitter matches leases on more than one door")
        elif remitter_outside_lease:
            blockers.append(
                f"{payment.tenant_name}'s lease does not cover this payment date"
            )

        if proposed is None and payment is not None and payment.property_hint:
            hinted = find_property_for_hint(session, payment.property_hint)
            if hinted is not None:
                proposed = hinted
                confidence = max(confidence, 0.85)
                reasons.append(f"Memo names {payment.property_hint}")

        amount_lease: Lease | None = None
        if proposed is None and not remitter_outside_lease:
            amount_matches = [
                lease for lease in leases_by_amount.get(abs(txn.amount_cents), [])
                if lease.property_id in rentals
                and _lease_covers(lease, txn.transaction_date)
            ]
            amount_doors = {lease.property_id for lease in amount_matches}
            if len(amount_doors) == 1:
                amount_lease = amount_matches[0]
                proposed = rentals[amount_lease.property_id]
                tenant_name = tenant_name or amount_lease.tenant_name
                confidence = max(confidence, 0.60)
                # A matching amount is a hint for a reviewer, never evidence on
                # its own: a company's own transfer can equal the rent.
                reasons.append(f"Amount equals the rent on {proposed.name}; not evidence by itself")
            elif len(amount_doors) > 1:
                blockers.append("Amount matches rent on more than one door")

        if property_id:
            if proposed is None or proposed.id != property_id:
                continue

        category: str | None = None
        if payment is not None and payment.is_mixed_deposit_and_rent:
            reasons.append("Memo names both rent and a deposit")
            blockers.append("Memo mixes rent and a deposit; split it manually")
        elif payment is not None and payment.is_security_deposit:
            category = _DEPOSIT_CATEGORY
            reasons.append("Memo says deposit")
        elif payment is not None and payment.is_rent:
            category = _RENT_CATEGORY
            reasons.append("Memo says rent")
        elif alias is not None or lease_doors:
            category = _RENT_CATEGORY
            reasons.append("Known tenant; the memo carries no rent wording")

        has_rent_evidence = bool(
            payment is not None and (payment.is_rent or payment.is_security_deposit)
        )
        relevant = has_rent_evidence or alias is not None or bool(lease_doors)
        if not relevant and not include_all and not (q and q.strip()):
            continue
        if category is None and not blockers:
            blockers.append("The bank record does not say rent or deposit")
        if proposed is None and not any("deposit; split" in blocker for blocker in blockers):
            blockers.append("Choose the property")

        candidates.append(
            RentCandidate(
                transaction=txn,
                state=state,
                payment=payment,
                door=proposed,
                tenant_name=tenant_name,
                remitter=payment.tenant_name if payment is not None else None,
                category=category,
                confidence=round(confidence, 2),
                reasons=tuple(reasons),
                blockers=tuple(blockers),
            )
        )
    return candidates


# --- Posting --------------------------------------------------------------


def _lease_id(tenant_name: str, property_id: str) -> str:
    return f"lease_{slugify(tenant_name)}_{property_id}"


def _upsert_lease(
    session: Session,
    *,
    tenant_name: str,
    property_id: str,
    start_date: date,
    monthly_rent_cents: int,
) -> Lease | None:
    """Create a tenant record when the payment history supports its terms.

    A lease is only created when at least two rent payments at the same amount
    exist. One partial payment is not a lease, and a security deposit is not
    rent. Existing leases are left alone: a human remains the authority on
    lease terms.
    """
    lease_id = _lease_id(tenant_name, property_id)
    if session.get(Lease, lease_id) is not None:
        return None
    lease = Lease(
        id=lease_id,
        property_id=property_id,
        tenant_name=tenant_name,
        start_date=start_date,
        monthly_rent_cents=monthly_rent_cents,
        security_deposit_cents=None,
    )
    session.add(lease)
    session.flush()
    return lease


def _lease_for_key(
    session: Session, key: str, property_id: str, day: date
) -> Lease | None:
    """The lease for a remitter on a door, if it covers the payment date."""
    for lease in _leases(session):
        if (
            lease.property_id == property_id
            and _name_key(lease.tenant_name) == key
            and _lease_covers(lease, day)
        ):
            return lease
    return None


def _record_alias(
    session: Session,
    *,
    remitter: str | None,
    tenant_name: str | None,
    property_id: str,
    payment_date: date,
) -> None:
    """Remember which door a confirmed remitter pays, for the next payment.

    Written only after a person confirmed an attribution, so the alias is a
    decision record rather than an inference. A later confirmation for a
    different door updates it.
    """
    key = _name_key(remitter)
    if not key:
        return
    alias_id = f"alias_{hashlib.sha1(key.encode('utf-8')).hexdigest()[:16]}"
    lease = _lease_for_key(session, key, property_id, payment_date)
    display_name = (remitter or "").strip() or (tenant_name or "")
    tenant_label = lease.tenant_name if lease is not None else (tenant_name or display_name)

    existing = session.get(TenantAlias, alias_id)
    if existing is None:
        session.add(
            TenantAlias(
                id=alias_id,
                alias_key=key,
                display_name=display_name,
                tenant_name=tenant_label,
                property_id=property_id,
                lease_id=lease.id if lease is not None else None,
            )
        )
        return
    existing.display_name = display_name
    existing.tenant_name = tenant_label
    existing.property_id = property_id
    existing.lease_id = lease.id if lease is not None else None
    session.add(existing)


def _post_rent_payment(
    session: Session,
    *,
    txn: Transaction,
    prop: Property,
    category: str,
    tenant_name: str | None,
    remitter: str | None,
    memo: str | None,
    client_confirmed: bool,
    user_attested_cpa_review: bool,
    actor_id: str,
    dry_run: bool,
) -> RentApplyOutcome:
    amount = abs(txn.amount_cents)
    note = f"Rent received: {tenant_name or 'tenant'}"
    if memo:
        note += f"; memo: {memo}"
    splits = [
        SplitItem(
            property_id=prop.id,
            category=category,
            amount_cents=amount,
            schedule_e_line=3 if category == _RENT_CATEGORY else None,
            tax_review_status=classification_status_for(
                client_confirmed, user_attested_cpa_review
            ),
            notes=note,
        )
    ]
    policy = evaluate_posting_policy(session, txn, splits)

    outcome: dict[str, Any] = dict(
        transaction_id=txn.id,
        tenant_name=tenant_name,
        door_name=prop.name,
        category=category,
        amount_cents=amount,
    )

    unmet_gate = (
        policy.requires_client_confirmation and not client_confirmed
    ) or (policy.requires_cpa_review and not user_attested_cpa_review)

    if unmet_gate:
        if dry_run:
            return RentApplyOutcome(
                status="WOULD_ALLOCATE",
                reason="Needs a governance sign-off before it can post",
                **outcome,
            )
        txn.requires_client_confirmation = (
            txn.requires_client_confirmation or policy.requires_client_confirmation
        )
        txn.requires_cpa_review = txn.requires_cpa_review or policy.requires_cpa_review
        session.add(txn)
        apply_transaction_splits(
            session, txn.id, splits, allow_partial=False, commit=False, actor_id=actor_id
        )
        _record_alias(
            session,
            remitter=remitter or tenant_name,
            tenant_name=tenant_name,
            property_id=prop.id,
            payment_date=txn.transaction_date,
        )
        session.commit()
        return RentApplyOutcome(
            status="ALLOCATED",
            reason="Classified and left unposted for a governance sign-off",
            **outcome,
        )

    assert_posting_authorized(
        policy,
        client_confirmed=client_confirmed,
        user_attested_cpa_review=user_attested_cpa_review,
    )
    if dry_run:
        return RentApplyOutcome(status="WOULD_POST", reason="Ready to post", **outcome)

    apply_transaction_splits(
        session, txn.id, splits, allow_partial=False, commit=False, actor_id=actor_id
    )
    post_transaction_to_ledger(
        session=session,
        transaction_id=txn.id,
        client_confirmed=client_confirmed,
        user_attested_cpa_review=user_attested_cpa_review,
        commit=False,
        actor_id=actor_id,
    )
    _record_alias(
        session,
        remitter=remitter or tenant_name,
        tenant_name=tenant_name,
        property_id=prop.id,
        payment_date=txn.transaction_date,
    )
    session.commit()
    return RentApplyOutcome(status="POSTED", reason="Posted to the ledger", **outcome)


def _safe_post(
    session: Session,
    *,
    candidate: RentCandidate,
    prop: Property,
    category: str,
    tenant_name: str | None,
    remitter: str | None,
    memo: str | None,
    client_confirmed: bool,
    user_attested_cpa_review: bool,
    actor_id: str,
    dry_run: bool,
) -> RentApplyOutcome:
    try:
        return _post_rent_payment(
            session,
            txn=candidate.transaction,
            prop=prop,
            category=category,
            tenant_name=tenant_name,
            remitter=remitter,
            memo=memo,
            client_confirmed=client_confirmed,
            user_attested_cpa_review=user_attested_cpa_review,
            actor_id=actor_id,
            dry_run=dry_run,
        )
    except Exception as exc:  # noqa: BLE001 - one bad payment must not stop the run
        session.rollback()
        return RentApplyOutcome(
            transaction_id=candidate.transaction.id,
            status="ERROR",
            reason=str(exc),
            tenant_name=tenant_name,
            door_name=prop.name,
            category=category,
            amount_cents=abs(candidate.transaction.amount_cents),
        )


def _link_aliases(session: Session, lease: Lease) -> None:
    """Point confirmed remitters at the lease that now covers them.

    A remitter is confirmed before a payment history has established the lease,
    so the alias is stored without one. Once the lease exists, the alias is the
    record that the same person keeps paying this door.
    """
    key = _name_key(lease.tenant_name)
    if not key:
        return
    for alias in session.exec(
        select(TenantAlias).where(TenantAlias.property_id == lease.property_id)
    ).all():
        if alias.alias_key == key or _name_key(alias.tenant_name) == key:
            alias.lease_id = lease.id
            alias.tenant_name = lease.tenant_name
            session.add(alias)


def _create_leases_from_history(
    session: Session,
    history: Sequence[tuple[str | None, str, date, int]],
) -> list[str]:
    """Create a lease after two rent payments at the same amount on one door."""
    grouped: dict[tuple[str, str], list[tuple[date, int]]] = defaultdict(list)
    for tenant_name, property_id, day, amount in history:
        if not tenant_name:
            continue
        grouped[(tenant_name, property_id)].append((day, amount))

    created: list[str] = []
    for (tenant_name, property_id), payments in grouped.items():
        monthly_rent, occurrences = Counter(amount for _day, amount in payments).most_common(1)[0]
        lease = session.get(Lease, _lease_id(tenant_name, property_id))
        if lease is None:
            if occurrences < 2:
                continue
            lease = _upsert_lease(
                session,
                tenant_name=tenant_name,
                property_id=property_id,
                start_date=min(day for day, _amount in payments),
                monthly_rent_cents=monthly_rent,
            )
            if lease is not None:
                created.append(lease.id)
        if lease is not None:
            _link_aliases(session, lease)
    return created


def _pending_candidate_index(session: Session) -> dict[str, RentCandidate]:
    candidates = find_rent_candidates(session, include_all=True, max_scan=5000)
    return {candidate.transaction.id: candidate for candidate in candidates}


def apply_rent_selection(
    session: Session,
    transaction_ids: Sequence[str],
    *,
    property_id: str | None = None,
    category: str | None = None,
    client_confirmed: bool = False,
    user_attested_cpa_review: bool = False,
    dry_run: bool = False,
    actor_id: str = "rent-workbench",
    require_actionable: bool = True,
) -> RentApplyReport:
    """Post the payments a person selected, one outcome per id.

    ``property_id`` and ``category`` are the bulk choices: when they are given,
    every selected payment takes them, which is how a reviewer assigns one door
    to a set of payments the bank record does not attribute. Without them each
    payment uses its own proposal, and ``require_actionable`` refuses rows the
    evidence does not support. A mixed deposit-and-rent memo is always refused:
    its amounts are not stated separately.
    """
    door_override: Property | None = None
    if property_id:
        door_override = session.get(Property, property_id)
        if (
            door_override is None
            or door_override.property_type != "RENTAL_INVESTMENT"
            or not door_override.is_active
        ):
            raise ValueError("Choose an active property.")
    if category and category not in (_RENT_CATEGORY, _DEPOSIT_CATEGORY):
        raise ValueError("Category must be rent income or a tenant security deposit.")

    index = _pending_candidate_index(session)
    report = RentApplyReport()
    rent_history: list[tuple[str | None, str, date, int]] = []

    for txn_id in transaction_ids:
        candidate = index.get(txn_id)
        if candidate is None:
            report.outcomes.append(
                RentApplyOutcome(
                    transaction_id=txn_id,
                    status="SKIPPED",
                    reason="No longer pending in the rent queue",
                )
            )
            continue

        chosen_door = door_override or candidate.door
        chosen_category = category or candidate.category

        if (
            require_actionable
            and door_override is None
            and category is None
            and not candidate.is_actionable
        ):
            report.outcomes.append(
                RentApplyOutcome(
                    transaction_id=txn_id,
                    status="SKIPPED",
                    reason="; ".join(candidate.blockers) or "Needs a door and a category",
                    tenant_name=candidate.tenant_name,
                    amount_cents=candidate.amount_cents,
                )
            )
            continue
        if candidate.payment is not None and candidate.payment.is_mixed_deposit_and_rent:
            report.outcomes.append(
                RentApplyOutcome(
                    transaction_id=txn_id,
                    status="SKIPPED",
                    reason="Memo mixes rent and a deposit; split it manually",
                    tenant_name=candidate.tenant_name,
                    amount_cents=candidate.amount_cents,
                )
            )
            continue
        if chosen_door is None:
            report.outcomes.append(
                RentApplyOutcome(
                    transaction_id=txn_id,
                    status="SKIPPED",
                    reason="Choose the property",
                    tenant_name=candidate.tenant_name,
                    amount_cents=candidate.amount_cents,
                )
            )
            continue
        if chosen_category is None:
            report.outcomes.append(
                RentApplyOutcome(
                    transaction_id=txn_id,
                    status="SKIPPED",
                    reason="Choose rent income or a security deposit",
                    tenant_name=candidate.tenant_name,
                    amount_cents=candidate.amount_cents,
                )
            )
            continue

        outcome = _safe_post(
            session,
            candidate=candidate,
            prop=chosen_door,
            category=chosen_category,
            tenant_name=candidate.tenant_name,
            remitter=candidate.remitter,
            memo=candidate.payment.memo if candidate.payment else "",
            client_confirmed=client_confirmed,
            user_attested_cpa_review=user_attested_cpa_review,
            actor_id=actor_id,
            dry_run=dry_run,
        )
        report.outcomes.append(outcome)
        if (
            not dry_run
            and outcome.status == "POSTED"
            and outcome.category == _RENT_CATEGORY
        ):
            rent_history.append(
                (
                    outcome.tenant_name,
                    chosen_door.id,
                    candidate.transaction.transaction_date,
                    outcome.amount_cents,
                )
            )

    if not dry_run and rent_history:
        report.leases_created = _create_leases_from_history(session, rent_history)
        session.commit()
    return report


def apply_rent_candidates(
    session: Session,
    transaction_ids: Sequence[str],
    *,
    client_confirmed: bool = False,
    user_attested_cpa_review: bool = False,
    dry_run: bool = False,
    actor_id: str = "rent-workbench",
    require_actionable: bool = True,
) -> RentApplyReport:
    """Apply each payment's own proposal (no bulk override)."""
    return apply_rent_selection(
        session,
        transaction_ids,
        client_confirmed=client_confirmed,
        user_attested_cpa_review=user_attested_cpa_review,
        dry_run=dry_run,
        actor_id=actor_id,
        require_actionable=require_actionable,
    )


def apply_rent_row(
    session: Session,
    *,
    transaction_id: str,
    property_id: str,
    category: str,
    client_confirmed: bool = False,
    user_attested_cpa_review: bool = False,
    dry_run: bool = False,
    actor_id: str = "rent-workbench",
) -> RentApplyReport:
    """Classify one payment with a door and category a person chose."""
    txn = session.get(Transaction, transaction_id)
    if txn is None:
        raise ValueError("Transaction not found.")
    if txn.amount_cents <= 0:
        raise ValueError("Only inflows can be classified as rent received.")

    state = session.get(TransactionProcessingState, txn.id)
    if state is not None and (
        state.allocation_status == "FULLY_ALLOCATED" or state.posting_status == "POSTED"
    ):
        raise ValueError("This payment is already classified and posted.")

    prop = session.get(Property, property_id) if property_id else None
    if prop is None or prop.property_type != "RENTAL_INVESTMENT" or not prop.is_active:
        raise ValueError("Choose an active property.")
    if category not in (_RENT_CATEGORY, _DEPOSIT_CATEGORY):
        raise ValueError("Category must be rent income or a tenant security deposit.")

    payment = extract_tenant_payment(txn.raw_description)
    candidate = RentCandidate(
        transaction=txn,
        state=state,
        payment=payment,
        door=prop,
        tenant_name=payment.tenant_name if payment else None,
        remitter=payment.tenant_name if payment else None,
        category=category,
        confidence=1.0,
        reasons=("Confirmed by hand",),
    )
    report = RentApplyReport()
    report.outcomes.append(
        _safe_post(
            session,
            candidate=candidate,
            prop=prop,
            category=category,
            tenant_name=candidate.tenant_name,
            remitter=candidate.remitter,
            memo=payment.memo if payment else "",
            client_confirmed=client_confirmed,
            user_attested_cpa_review=user_attested_cpa_review,
            actor_id=actor_id,
            dry_run=dry_run,
        )
    )
    return report


def apply_automatic_rent_payments(
    session: Session,
    *,
    actor_id: str = "statement-import",
    limit: int = 200,
) -> RentApplyReport:
    """Classify payments the evidence already attributes unambiguously.

    Used when a checking statement is imported: only payments that say rent or
    deposit *and* carry a confirmed alias, lease match or memo address are
    posted. Anything else waits for a person in the rent workbench.
    """
    actionable = [
        candidate.transaction.id
        for candidate in find_rent_candidates(session)
        if candidate.is_actionable
        and candidate.payment is not None
        and (candidate.payment.is_rent or candidate.payment.is_security_deposit)
    ][:limit]
    if not actionable:
        return RentApplyReport()
    return apply_rent_candidates(session, actionable, actor_id=actor_id)
