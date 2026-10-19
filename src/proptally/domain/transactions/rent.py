"""Tenant rent extraction and bulk rent classification.

Bank descriptions are the only tenant evidence this portfolio has: no lease
documents were imported. A Zelle payment names the remitter and, when the payer
writes one, the memo says whether the money is rent or a deposit. This module
reads that evidence, attributes the payment to one rental door, allocates it to
rental income, and posts it through the same split/posting services the
reconciliation screen uses.

Deposits are deliberately not posted as income. A security deposit is a tenant
liability, not Schedule E rent, and the split vocabulary does not yet carry a
deposit category. They are reported as skipped so they remain visible rather
than being silently misclassified.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date

from sqlmodel import Session, select

from proptally.db.models import (
    Lease,
    Property,
    Transaction,
    TransactionProcessingState,
)
from proptally.db.tenant_manager import slugify
from proptally.domain.transactions.policy import classification_status_for
from proptally.domain.transactions.splits import (
    SplitItem,
    apply_transaction_splits,
    post_transaction_to_ledger,
)

_RENT_WORD = re.compile(r"\brent(?:al)?\b", re.I)
_DEPOSIT_WORD = re.compile(r"\bdeposit\b", re.I)
_ZELLE_FROM = re.compile(
    r"Zelle\s+payment\s+from\s+(?P<name>.+?)\s+for\s+\"(?P<memo>[^\"]*)\"",
    re.I,
)
_ADDRESS_HINT = re.compile(r"\b(\d{2,6}\s+[A-Za-z][A-Za-z0-9 .'-]{1,40})", re.I)


@dataclass(frozen=True)
class TenantPayment:
    """The tenant evidence a bank description carries."""

    tenant_name: str
    memo: str
    property_hint: str | None
    is_rent: bool
    is_security_deposit: bool

    @property
    def is_mixed_deposit_and_rent(self) -> bool:
        return self.is_rent and self.is_security_deposit


@dataclass
class RentBulkReport:
    """What a bulk run changed, and what it refused to guess."""

    applied: list[str] = field(default_factory=list)
    rent_cents: int = 0
    deposit_cents: int = 0
    tenants: set[str] = field(default_factory=set)
    leases_created: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    errors: list[tuple[str, str]] = field(default_factory=list)

    def skip(self, transaction_id: str, reason: str) -> None:
        self.skipped.append((transaction_id, reason))

    def fail(self, transaction_id: str, reason: str) -> None:
        self.errors.append((transaction_id, reason))


def _normalise_name(value: str) -> str:
    """Turn a bank's upper-case remitter into a display name without guessing."""
    cleaned = re.sub(r"\s+", " ", value or "").strip(" ,;")
    return cleaned.title()


def _property_hint(memo: str) -> str | None:
    match = _ADDRESS_HINT.search(memo or "")
    if not match:
        return None
    hint = re.sub(r"\s+", " ", match.group(1)).strip(" .,;")
    # Keep a street number and street name; drop trailing prose such as "pro-rent".
    tokens = hint.split()
    return " ".join(tokens[:3]) if len(tokens) >= 2 else None


def extract_tenant_payment(raw_description: str) -> TenantPayment | None:
    """Read tenant name, memo and rent/deposit intent from a Zelle description.

    Only an inflow that names a remitter and carries a quoted memo is accepted.
    ``Zelle payment to`` is an expense to someone else, and a bare transfer
    confirmation has no memo to classify.
    """
    description = " ".join((raw_description or "").split())
    match = _ZELLE_FROM.search(description)
    if not match:
        return None

    memo = match.group("memo").strip()
    if not memo:
        return None

    return TenantPayment(
        tenant_name=_normalise_name(match.group("name")),
        memo=memo,
        property_hint=_property_hint(memo),
        is_rent=bool(_RENT_WORD.search(memo)),
        is_security_deposit=bool(_DEPOSIT_WORD.search(memo)),
    )


def _normalise_address(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()


def find_property_for_hint(session: Session, hint: str | None, *, property_id: str | None = None) -> Property | None:
    """Resolve an address fragment to exactly one active rental door."""
    if property_id:
        prop = session.get(Property, property_id)
        if prop is None or prop.property_type != "RENTAL_INVESTMENT" or not prop.is_active:
            return None
        if hint:
            wanted = _normalise_address(hint)
            haystack = _normalise_address(f"{prop.name} {prop.address_line1}")
            if wanted and wanted not in haystack:
                return None
        return prop
    if not hint:
        return None

    wanted = _normalise_address(hint)
    if not wanted:
        return None
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


def _property_for_tenant_lease(session: Session, tenant_name: str) -> Property | None:
    """A tenant with exactly one active lease gives a safe property fallback."""
    wanted = tenant_name.strip().casefold()
    property_ids = {
        lease.property_id
        for lease in session.exec(select(Lease).where(Lease.is_active == True)).all()  # noqa: E712
        if lease.tenant_name.strip().casefold() == wanted
    }
    if len(property_ids) != 1:
        return None
    prop = session.get(Property, next(iter(property_ids)))
    if prop is None or prop.property_type != "RENTAL_INVESTMENT" or not prop.is_active:
        return None
    return prop


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
    existing = session.get(Lease, lease_id)
    if existing is not None:
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


def bulk_apply_rent_payments(
    session: Session,
    *,
    property_id: str | None = None,
    tenant_name: str | None = None,
    dry_run: bool = False,
    create_leases: bool = True,
    actor_id: str = "bulk-rent",
) -> RentBulkReport:
    """Classify and post rent inflows already extracted from bank statements.

    The function is intentionally conservative:

    * a payment is attributed only when the memo names one rental address, or
      the caller supplies ``property_id``;
    * a memo that says both "deposit" and "rent" is skipped for manual splitting,
      because the amounts are not stated separately;
    * a deposit-only memo is posted to the tenant-deposit liability, never to
      rental income;
    * a payment that is already fully allocated or posted is left unchanged.
    """
    report = RentBulkReport()
    wanted_tenant = tenant_name.strip().casefold() if tenant_name else None
    lease_history: dict[tuple[str, str], list[tuple[date, int]]] = defaultdict(list)

    transactions = session.exec(
        select(Transaction)
        .where(Transaction.amount_cents > 0)
        .order_by(Transaction.transaction_date, Transaction.id)
    ).all()

    for txn in transactions:
        state = session.get(TransactionProcessingState, txn.id)
        if state is not None and (state.allocation_status == "FULLY_ALLOCATED" or state.posting_status == "POSTED"):
            continue

        payment = extract_tenant_payment(txn.raw_description)
        if payment is None or not (payment.is_rent or payment.is_security_deposit):
            continue
        if wanted_tenant and payment.tenant_name.casefold() != wanted_tenant:
            continue
        if payment.is_mixed_deposit_and_rent:
            report.skip(txn.id, f"memo mixes deposit and rent; split manually ({payment.memo})")
            continue

        prop = find_property_for_hint(session, payment.property_hint, property_id=property_id)
        if prop is None and property_id is None:
            prop = _property_for_tenant_lease(session, payment.tenant_name)
        if prop is None:
            hint = payment.property_hint or "no address in memo"
            report.skip(txn.id, f"cannot attribute {payment.tenant_name} to exactly one rental door ({hint})")
            continue

        amount = abs(txn.amount_cents)
        category = "SECURITY_DEPOSIT" if payment.is_security_deposit else "RENT_INCOME"
        if category == "RENT_INCOME":
            lease_history[(payment.tenant_name, prop.id)].append((txn.transaction_date, amount))

        if dry_run:
            report.applied.append(txn.id)
            if category == "RENT_INCOME":
                report.rent_cents += amount
            else:
                report.deposit_cents += amount
            report.tenants.add(payment.tenant_name)
            continue

        split = SplitItem(
            property_id=prop.id,
            category=category,
            amount_cents=amount,
            schedule_e_line=3 if category == "RENT_INCOME" else None,
            tax_review_status=classification_status_for(True, False),
            notes=f"Tenant: {payment.tenant_name}; memo: {payment.memo}",
        )
        try:
            apply_transaction_splits(
                session, txn.id, [split], allow_partial=False, commit=False, actor_id=actor_id
            )
            post_transaction_to_ledger(
                session=session,
                transaction_id=txn.id,
                client_confirmed=True,
                commit=False,
                actor_id=actor_id,
            )
            session.commit()
        except Exception as exc:  # noqa: BLE001 - one bad payment must not stop the batch
            session.rollback()
            report.fail(txn.id, str(exc))
            continue

        report.applied.append(txn.id)
        if category == "RENT_INCOME":
            report.rent_cents += amount
        else:
            report.deposit_cents += amount
        report.tenants.add(payment.tenant_name)

    if create_leases and not dry_run:
        for (payment_tenant, prop_id), history in lease_history.items():
            amounts = Counter(amount for _day, amount in history)
            monthly_rent, occurrences = amounts.most_common(1)[0]
            if occurrences < 2:
                report.skip(
                    f"{payment_tenant}/{prop_id}",
                    "payment history does not establish a recurring monthly rent",
                )
                continue
            lease = _upsert_lease(
                session,
                tenant_name=payment_tenant,
                property_id=prop_id,
                start_date=min(day for day, _amount in history),
                monthly_rent_cents=monthly_rent,
            )
            if lease is not None:
                report.leases_created.append(lease.id)
        session.commit()

    return report
