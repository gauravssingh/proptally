"""Write approved proposals into the portfolio, through the existing domain services.

This is the only module in the onboarding subsystem that touches a tenant database,
and it only ever sees proposals the customer accepted. It does not invent a second
property model, a second account model or a second ledger: the rows it creates are
the same rows the setup wizard creates, and the ledger accounts behind them are
allocated by :func:`proptally.domain.accounting.coa.ensure_entity_ledger_accounts`.

Idempotency is enforced twice, deliberately:

* every proposal records a :class:`OnboardingProvisionEntry`, and a retry skips it;
* every entity is also looked up by its natural key (address, loan number, account
  identity), so a failure between writing a row and writing its manifest entry
  cannot produce a duplicate on retry.

Partial failure is a supported state. Each entity commits before its manifest row;
a later failure leaves earlier entities in place and marks the session
``PROVISIONING_FAILED``, which is retryable.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from datetime import date
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlmodel import Session, select

from proptally.db.models import (
    Document,
    DocumentExtraction,
    EscrowAccount,
    FinancialAccount,
    FinancialInstitution,
    Lease,
    LoanAccount,
    Property,
    utc_now,
)
from proptally.db.onboarding_models import (
    ENTITY_ACCOUNT,
    ENTITY_LEASE,
    ENTITY_MORTGAGE,
    ENTITY_PROPERTY,
    ONBOARDING_COMPLETED,
    ONBOARDING_PROVISIONING,
    ONBOARDING_PROVISIONING_FAILED,
    PROPOSAL_ACCEPTED,
    PROPOSAL_IMPORTED,
    OnboardingDocument,
    OnboardingExtraction,
    OnboardingProposal,
    OnboardingProvisionEntry,
    OnboardingSession,
)
from proptally.domain.accounting.coa import ensure_entity_ledger_accounts
from proptally.domain.accounting.opening_balances import post_loan_opening_balances
from proptally.domain.setup_state import ensure as ensure_setup
from proptally.ingestion.parsers import PARSER_VERSION
from proptally.ingestion.service import extract_and_resolve
from proptally.onboarding.archive import BLOCKED_SUFFIXES
from proptally.onboarding.pipeline import normalise_address
from proptally.onboarding.store import OnboardingStore


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:12]}"


def _loads(value: str | None) -> dict:
    try:
        return json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}


def _day(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _masked(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    digits = re.sub(r"\D", "", text)
    return "..." + digits[-4:] if len(digits) >= 4 else ""


def _institution_for(session: Session, name: str) -> FinancialInstitution:
    """Find or create the institution, by the same name the setup form would use."""
    cleaned = " ".join((name or "").split()) or "Unknown institution"
    existing = session.exec(
        select(FinancialInstitution).where(
            FinancialInstitution.name.ilike(cleaned)  # type: ignore[attr-defined]
        )
    ).first()
    if existing is not None:
        return existing
    institution = FinancialInstitution(id=_id("inst"), name=cleaned)
    session.add(institution)
    session.flush()
    return institution


def _address_key_from(
    line1: Any, line2: Any, city: Any, state: Any, zip_code: Any
) -> tuple[str, str, str, str, str]:
    # Creation stores a blank state as IL. Lookup must use the same default or a
    # retry after a later failure inserts a duplicate door at the same address.
    state_value = " ".join(str(state or "").split()).casefold() or "il"
    return tuple(
        " ".join((str(value) if value is not None else "").split()).casefold()
        for value in (line1, line2, city, state_value, zip_code)
    )


def _address_key(prop: Property) -> tuple[str, str, str, str, str]:
    return _address_key_from(
        prop.address_line1, prop.address_line2, prop.city, prop.state, prop.zip_code
    )


def _find_property(session: Session, payload: dict) -> Property | None:
    target = _address_key_from(
        payload.get("address_line1"),
        payload.get("address_line2"),
        payload.get("city"),
        payload.get("state"),
        payload.get("zip_code"),
    )
    if not target[0]:
        return None
    for prop in session.exec(select(Property).where(Property.is_active == True)).all():
        if _address_key(prop) == target:
            return prop
    return None


def _create_property(session: Session, payload: dict) -> Property:
    existing = _find_property(session, payload)
    if existing is not None:
        return existing
    street = str(payload.get("address_line1") or "").strip()
    city = str(payload.get("city") or "").strip()
    if not street or not city:
        raise ValueError(
            "A property needs a street address and a city before it can be imported."
        )
    prop = Property(
        id=_id("prop"),
        name=str(payload.get("name") or street),
        address_line1=street,
        address_line2=str(payload.get("address_line2") or "") or None,
        city=city,
        state=str(payload.get("state") or "").strip() or "IL",
        zip_code=str(payload.get("zip_code") or "").strip(),
        property_type=str(payload.get("property_type") or "RENTAL_INVESTMENT"),
        acquisition_date=_day(payload.get("acquisition_date")),
        purchase_price_cents=payload.get("purchase_price_cents"),
        short_label=str(payload.get("short_label") or "") or None,
        # The address came from a document; it has not been confirmed against
        # Google Places, and the review screen says so. The customer can still
        # correct it on the property screen afterwards.
        address_source="MANUAL",
    )
    session.add(prop)
    session.flush()
    ensure_entity_ledger_accounts(session, commit=False)
    return prop


def _create_account(session: Session, payload: dict) -> FinancialAccount:
    institution = _institution_for(session, str(payload.get("institution_name") or ""))
    account_type = str(payload.get("account_type") or "CHECKING")
    masked = _masked(payload.get("account_number_masked"))
    for other in session.exec(
        select(FinancialAccount).where(
            FinancialAccount.institution_id == institution.id,
            FinancialAccount.account_type == account_type,
        )
    ).all():
        if masked and _masked(other.account_number_masked) == masked:
            return other
    account = FinancialAccount(
        id=_id("acct"),
        institution_id=institution.id,
        account_name=str(payload.get("account_name") or f"{institution.name} {account_type}"),
        account_type=account_type,
        account_number_masked=masked,
        mixed_use_status="UNCLASSIFIED",
        currency=str(payload.get("currency") or "USD"),
    )
    session.add(account)
    session.flush()
    ensure_entity_ledger_accounts(session, commit=False)
    return account


def _create_mortgage(session: Session, payload: dict, property_id: str) -> LoanAccount:
    loan_number = str(payload.get("loan_number") or "").strip()
    if not loan_number:
        raise ValueError("A mortgage needs a loan number before it can be imported.")
    existing = session.exec(
        select(LoanAccount).where(LoanAccount.loan_number == loan_number)
    ).first()
    if existing is not None:
        return existing

    institution = _institution_for(session, str(payload.get("lender") or ""))
    masked = _masked(payload.get("account_number_masked")) or (
        f"...{loan_number[-4:]}" if len(loan_number) >= 4 else loan_number
    )
    account = FinancialAccount(
        id=_id("acct"),
        institution_id=institution.id,
        account_name=f"{institution.name} loan {loan_number}",
        account_type="MORTGAGE",
        account_number_masked=masked,
        mixed_use_status="UNCLASSIFIED",
    )
    session.add(account)
    session.flush()
    loan = LoanAccount(
        id=_id("loan"),
        financial_account_id=account.id,
        property_id=property_id,
        loan_number=loan_number,
        origination_date=_day(payload.get("origination_date")),
        original_principal_cents=payload.get("original_principal_cents"),
        term_months=int(payload.get("term_months") or 360),
        rate_type=str(payload.get("rate_type") or "FIXED"),
        current_interest_rate_bps=payload.get("interest_rate_bps"),
    )
    session.add(loan)
    session.flush()
    escrow_cents = payload.get("escrow_balance_cents")
    if escrow_cents:
        session.add(
            EscrowAccount(
                id=_id("escrow"),
                loan_account_id=loan.id,
                opening_balance_cents=int(escrow_cents),
            )
        )
    ensure_entity_ledger_accounts(session, commit=False)
    # Onboarded loans must reach the ledger exactly like wizard-created ones,
    # under the same idempotency key so a retry cannot double the debt.
    post_loan_opening_balances(
        session,
        loan,
        payload.get("outstanding_principal_cents") or payload.get("original_principal_cents"),
        escrow_cents,
        commit=False,
    )
    return loan


def _create_lease(session: Session, payload: dict, property_id: str) -> Lease:
    tenant = str(payload.get("tenant_name") or "").strip()
    start = _day(payload.get("lease_start"))
    rent = payload.get("monthly_rent_cents")
    if not tenant:
        raise ValueError("A lease needs a tenant name before it can be imported.")
    if start is None or rent is None:
        raise ValueError("A lease needs a start date and a monthly rent before it can be imported.")
    existing = session.exec(
        select(Lease).where(
            Lease.property_id == property_id,
            Lease.tenant_name == tenant,
            Lease.start_date == start,
        )
    ).first()
    if existing is not None:
        return existing
    lease = Lease(
        id=_id("lease"),
        property_id=property_id,
        tenant_name=tenant,
        start_date=start,
        end_date=_day(payload.get("lease_end")),
        monthly_rent_cents=int(rent),
        security_deposit_cents=int(payload.get("security_deposit_cents") or 0),
    )
    session.add(lease)
    session.flush()
    return lease


DOCUMENT_TYPE_FOR_CATEGORY = {
    "MORTGAGE_STATEMENT": "MORTGAGE_STATEMENT",
    "BANK_STATEMENT": "CHECKING_STATEMENT",
    "RENTAL_AGREEMENT": "LEASE_AGREEMENT",
    "INSURANCE": "INSURANCE_POLICY",
    "PROPERTY_TAX": "PROPERTY_TAX",
}


def _staged_documents(control_engine, onboarding_id: str) -> list[dict]:
    """The staged documents and the property group each one supports."""
    with Session(control_engine) as control:
        documents = control.exec(
            select(OnboardingDocument).where(OnboardingDocument.onboarding_id == onboarding_id)
        ).all()
        extractions = {
            item.document_id: item
            for item in control.exec(
                select(OnboardingExtraction).where(
                    OnboardingExtraction.onboarding_id == onboarding_id
                )
            ).all()
        }
        # A property proposal is the authority on which documents belong to it.
        # Recomputing from address fields would detach a document when the address
        # was edited during review.
        proposal_groups: dict[str, str] = {}
        for proposal in control.exec(
            select(OnboardingProposal).where(
                OnboardingProposal.onboarding_id == onboarding_id,
                OnboardingProposal.entity_type == ENTITY_PROPERTY,
            )
        ).all():
            for document_id in _loads(proposal.source_document_ids_json or "[]"):
                proposal_groups[str(document_id)] = proposal.group_key
        result = []
        for document in documents:
            group = proposal_groups.get(document.id, "")
            extraction = extractions.get(document.id)
            if not group and extraction is not None:
                fields = (_loads(extraction.payload_json).get("fields") or {})
                street = (fields.get("address_line1") or {}).get("value") or ""
                city = (fields.get("city") or {}).get("value") or ""
                zip_code = (fields.get("zip_code") or {}).get("value") or ""
                key = normalise_address(street, city, zip_code)
                group = f"property:{key}" if key else ""
            result.append({
                "id": document.id,
                "storage_path": document.storage_path,
                "sha256": document.sha256,
                "original_filename": document.original_filename,
                "size_bytes": document.size_bytes,
                "mime_type": document.mime_type,
                "category": document.category,
                "property_group": group,
            })
        return result


def _copy_documents(
    tenant: Session,
    documents: list[dict],
    store: OnboardingStore,
    vault_root,
    property_by_group: dict[str, str],
) -> list[tuple[str, str]]:
    """Retain source documents in the portfolio's own vault.

    Files become ordinary vault documents, linked to the property where the evidence
    supports it. SHA-256 is the uniqueness key the vault already uses, so retrying
    copies nothing twice. Unsupported or unreadable files are copied too: they are
    what the customer uploaded, and the audit trail points at them.
    """
    copied: list[tuple[str, str]] = []
    for staged in documents:
        source = store.resolve(staged["storage_path"])
        if not source.is_file():
            continue
        suffix = source.suffix.lower()
        if suffix in BLOCKED_SUFFIXES:
            # An archive's executable or script is never retained as a portfolio
            # document, even if the customer uploaded it inside a ZIP.
            continue
        digest = staged["sha256"] or hashlib.sha256(source.read_bytes()).hexdigest()
        existing = tenant.exec(
            select(Document).where(Document.file_sha256 == digest)
        ).first()
        if existing is not None:
            copied.append((staged["id"], existing.id))
            continue

        # Content-addressable layout, matching ingestion / vault readers.
        destination = vault_root / digest[:2] / f"{digest}{suffix}"
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.exists():
            shutil.copyfile(source, destination)
        document = Document(
            id=_id("doc"),
            file_sha256=digest,
            original_filename=staged["original_filename"],
            storage_path=str(destination),
            file_size_bytes=staged["size_bytes"],
            mime_type=staged["mime_type"],
            document_type=DOCUMENT_TYPE_FOR_CATEGORY.get(staged["category"], "OTHER"),
            property_id=property_by_group.get(staged["property_group"]),
            uploaded_at=utc_now(),
        )
        tenant.add(document)
        tenant.flush()
        copied.append((staged["id"], document.id))
    return copied


def _stage_statement_extractions(
    tenant: Session,
    documents: list[dict],
    copied: list[tuple[str, str]],
) -> None:
    """Stage retained statement PDFs for the Statements & Ingestion screen.

    Onboarding creates the properties, loans and accounts first; this turns both
    mortgage and bank/card PDFs into the same extraction records an upload
    through ``/statements`` would create. Using the shared extractor keeps the
    bank/card-first parser order, so a credit-card statement is never read as a
    mortgage merely because it prints an account number. A parsing failure is
    local to that document: the portfolio is still complete, and the document
    remains available to reprocess later.
    """
    tenant_document_ids = dict(copied)
    for staged in documents:
        if staged.get("category") not in {"MORTGAGE_STATEMENT", "BANK_STATEMENT"}:
            continue
        tenant_document_id = tenant_document_ids.get(staged["id"])
        if not tenant_document_id:
            continue
        if tenant.exec(
            select(DocumentExtraction.id).where(
                DocumentExtraction.document_id == tenant_document_id
            )
        ).first() is not None:
            continue
        document = tenant.get(Document, tenant_document_id)
        if document is None:
            continue
        path = Path(document.storage_path)
        if not path.is_file():
            continue
        try:
            extraction = extract_and_resolve(path, tenant)
        except Exception:  # noqa: BLE001 - one unreadable PDF must not fail provisioning
            continue
        if extraction is None:
            continue
        document.document_type = {
            "MORTGAGE": "MORTGAGE_STATEMENT",
            "CHECKING": "CHECKING_STATEMENT",
            "CREDIT_CARD": "CREDIT_CARD_STATEMENT",
        }.get(extraction.statement_type, document.document_type)
        tenant.add(document)
        tenant.add(DocumentExtraction(
            id=_id("ext"),
            document_id=document.id,
            parser_name=extraction.parser_name,
            parser_version=PARSER_VERSION,
            validation_status=extraction.status,
            approval_status="PENDING",
            extracted_data_json=json.dumps(extraction.to_dict(), sort_keys=True),
        ))
    tenant.flush()


def _mark_entry(
    control: Session,
    onboarding_id: str,
    proposal: OnboardingProposal,
    *,
    entity_id: str | None,
    error: str | None = None,
) -> None:
    entry = control.exec(
        select(OnboardingProvisionEntry).where(
            OnboardingProvisionEntry.onboarding_id == onboarding_id,
            OnboardingProvisionEntry.proposal_id == proposal.id,
        )
    ).first()
    if entry is None:
        entry = OnboardingProvisionEntry(
            id=_id("prv"),
            onboarding_id=onboarding_id,
            proposal_id=proposal.id,
            entity_type=proposal.entity_type,
        )
    entry.status = "FAILED" if error else "DONE"
    entry.error = error
    entry.tenant_entity_id = entity_id
    entry.completed_at = utc_now() if not error else None
    control.add(entry)


def run_provisioning(control_engine, tenant_manager, store: OnboardingStore, onboarding_id: str) -> list[str]:
    """Provision every accepted proposal. Returns human-readable errors, if any.

    Safe to call again after a failure: completed manifest entries are skipped, and
    natural-key lookups catch anything written without one.
    """
    with Session(control_engine) as control:
        onboarding = control.get(OnboardingSession, onboarding_id)
        if onboarding is None:
            return ["Onboarding session not found."]
        if onboarding.status == ONBOARDING_COMPLETED:
            return []
        if onboarding.status not in {
            "APPROVED", "PROVISIONING_FAILED", ONBOARDING_PROVISIONING,
        }:
            return ["This onboarding session has not been approved for provisioning."]
        proposals = control.exec(
            select(OnboardingProposal)
            .where(
                OnboardingProposal.onboarding_id == onboarding_id,
                OnboardingProposal.status.in_((PROPOSAL_ACCEPTED, PROPOSAL_IMPORTED)),
            )
            .order_by(OnboardingProposal.created_at)
        ).all()
        completed = {
            entry.proposal_id
            for entry in control.exec(
                select(OnboardingProvisionEntry).where(
                    OnboardingProvisionEntry.onboarding_id == onboarding_id,
                    OnboardingProvisionEntry.status == "DONE",
                )
            ).all()
        }
        rows = [
            (proposal.id, proposal.entity_type, proposal.group_key, proposal.payload_json)
            for proposal in proposals
            if proposal.id not in completed
        ]
        # Properties must exist before the mortgages and leases that reference
        # them; created_at order can put a later-sorted document's mortgage first.
        _order = {"PROPERTY": 0, "ACCOUNT": 1, "MORTGAGE": 2, "LEASE": 3}
        rows.sort(key=lambda row: _order.get(row[1], 99))
        if not rows and not any(p.status == PROPOSAL_ACCEPTED for p in proposals):
            # Everything accepted has already been imported; finish the session.
            onboarding.status = ONBOARDING_COMPLETED
            onboarding.completed_at = utc_now()
            onboarding.updated_at = utc_now()
            control.add(onboarding)
            control.commit()
            return []
        tenant_id = onboarding.tenant_id
        paths = tenant_manager.paths(tenant_id)
        onboarding.status = ONBOARDING_PROVISIONING
        onboarding.updated_at = utc_now()
        control.add(onboarding)
        control.commit()

    staged_documents = _staged_documents(control_engine, onboarding_id)
    engine = tenant_manager.engine_for(tenant_id)
    errors: list[str] = []
    property_by_group: dict[str, str] = {}

    with Session(engine) as tenant:
        for proposal_id, entity_type, group_key, payload_json in rows:
            payload = _loads(payload_json)
            try:
                if entity_type == ENTITY_PROPERTY:
                    entity = _create_property(tenant, payload)
                    entity_id = entity.id
                    # The proposal's own durable key, not a recalculation from
                    # possibly edited payload fields: approval validated this key.
                    property_by_group[group_key] = entity_id
                elif entity_type == ENTITY_ACCOUNT:
                    entity_id = _create_account(tenant, payload).id
                elif entity_type == ENTITY_MORTGAGE:
                    property_id = (
                        property_by_group.get(str(payload.get("property_group_key") or ""))
                        or _property_id_for(tenant, payload)
                    )
                    if property_id is None:
                        raise ValueError(
                            "This mortgage is not linked to an accepted property. "
                            "Resolve its property association in review first."
                        )
                    entity_id = _create_mortgage(tenant, payload, property_id).id
                elif entity_type == ENTITY_LEASE:
                    property_id = (
                        property_by_group.get(str(payload.get("property_group_key") or ""))
                        or _property_id_for(tenant, payload)
                    )
                    if property_id is None:
                        raise ValueError(
                            "This lease is not linked to an accepted property. "
                            "Resolve its property association in review first."
                        )
                    entity_id = _create_lease(tenant, payload, property_id).id
                else:
                    raise ValueError(f"Unknown proposal type {entity_type!r}.")
                ensure_setup(tenant)
                tenant.commit()
            except Exception as exc:  # noqa: BLE001 - one record's failure is reported
                tenant.rollback()
                errors.append(f"{entity_type.title()}: {exc}")
                with Session(control_engine) as control:
                    proposal = control.get(OnboardingProposal, proposal_id)
                    if proposal is not None:
                        _mark_entry(control, onboarding_id, proposal, entity_id=None, error=str(exc)[:400])
                        control.commit()
                continue

            # The entity is committed; record its identity in the control plane.
            with Session(control_engine) as control:
                proposal = control.get(OnboardingProposal, proposal_id)
                if proposal is not None:
                    proposal.status = PROPOSAL_IMPORTED
                    proposal.updated_at = utc_now()
                    _mark_entry(control, onboarding_id, proposal, entity_id=entity_id)
                    control.add(proposal)
                    control.commit()

        try:
            copied = _copy_documents(tenant, staged_documents, store, paths.vault, property_by_group)
            tenant.commit()
        except Exception as exc:  # noqa: BLE001
            tenant.rollback()
            copied = []
            errors.append(f"Source documents: {exc}")
        if copied:
            with Session(control_engine) as control:
                for staged_id, tenant_document_id in copied:
                    staged = control.get(OnboardingDocument, staged_id)
                    if staged is not None:
                        staged.tenant_document_id = tenant_document_id
                        control.add(staged)
                control.commit()
            try:
                _stage_statement_extractions(tenant, staged_documents, copied)
                tenant.commit()
            except Exception as exc:  # noqa: BLE001 - the portfolio is already written
                tenant.rollback()
                errors.append(f"Statement staging: {exc}")

    with Session(control_engine) as control:
        onboarding = control.get(OnboardingSession, onboarding_id)
        if onboarding is not None:
            if errors:
                onboarding.status = ONBOARDING_PROVISIONING_FAILED
                onboarding.last_error = "; ".join(errors)[:1000]
            else:
                onboarding.status = ONBOARDING_COMPLETED
                onboarding.completed_at = utc_now()
                onboarding.last_error = None
            onboarding.updated_at = utc_now()
            control.add(onboarding)
            control.commit()

    if not errors:
        # Every accepted document now lives in the portfolio's CAS vault, so the
        # staged bytes (and the staging directory) are retained no longer.
        store.remove_session(onboarding_id)
    return errors


def _property_id_for(session: Session, payload: dict) -> str | None:
    """Resolve by address fields carried on the mortgage or lease payload."""
    candidate = Property(
        id="",
        name="",
        address_line1=str(payload.get("property_address") or ""),
        address_line2="",
        city=str(payload.get("property_city") or payload.get("city") or ""),
        state=str(payload.get("property_state") or payload.get("state") or ""),
        zip_code=str(payload.get("property_zip") or payload.get("zip_code") or ""),
    )
    if not candidate.address_line1:
        return None
    for prop in session.exec(select(Property).where(Property.is_active == True)).all():
        if _address_key(prop) == _address_key(candidate):
            return prop.id
    return None
