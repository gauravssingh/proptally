"""The asynchronous document pipeline and cross-document reconciliation.

One durable job per document means one corrupt PDF fails one job. The archive is
inventoried at upload time (bounded and synchronous, because the customer must be
told immediately whether the ZIP was accepted); classification, text extraction and
field extraction run later, in the worker, so closing the browser does not stop them.

Reconciliation is the second half of this module. Documents are grouped by a natural
key -- an address for properties, a loan number for mortgages, institution plus last
four for accounts -- and every value keeps its document, page and supporting line.
Genuine disagreements are recorded as conflicts; historical values (an outstanding
balance from an older statement) are kept as history rather than treated as a
contradiction.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, cast
from uuid import uuid4

from sqlalchemy import text
from sqlmodel import Session, select

from proptally.db.onboarding_models import (
    DECISION_ACCEPT,
    DOC_CLASSIFIED,
    DOC_DUPLICATE,
    DOC_EXTRACTED,
    DOC_EXTRACTING,
    DOC_FAILED,
    DOC_NEEDS_REVIEW,
    DOC_READY,
    DOC_UNSUPPORTED,
    ENTITY_ACCOUNT,
    ENTITY_LEASE,
    ENTITY_MORTGAGE,
    ENTITY_PROPERTY,
    JOB_DOCUMENT,
    JOB_DONE,
    JOB_FAILED,
    JOB_FINALIZE,
    JOB_PENDING,
    JOB_RUNNING,
    ONBOARDING_PARTIAL,
    ONBOARDING_PROCESSING,
    ONBOARDING_PROCESSING_FAILED,
    ONBOARDING_READY,
    ONBOARDING_REVIEWING,
    PROPOSAL_NEEDS_REVIEW,
    TERMINAL_DOCUMENT_STATES,
    OnboardingDocument,
    OnboardingExtraction,
    OnboardingFieldEvidence,
    OnboardingJob,
    OnboardingProposal,
    OnboardingReviewDecision,
    OnboardingSession,
    utc_now,
)
from proptally.onboarding.classify import SCHEMA_VERSION, classify
from proptally.onboarding.extraction import (
    DocumentUnreadable,
    ExtractionResult,
    UnsupportedDocument,
    extract_text,
    institution_from_filename,
    plausible_address,
)
from proptally.onboarding.providers import structured_extractor
from proptally.onboarding.store import OnboardingStore

# --- Serialisation helpers -------------------------------------------------


def jsonable(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, list):
        return [jsonable(item) for item in value]
    if isinstance(value, dict):
        return {key: jsonable(item) for key, item in value.items()}
    return value


def dumps(value: Any) -> str:
    return json.dumps(jsonable(value), ensure_ascii=False, sort_keys=True)


def loads(value: str | None, fallback: Any = None) -> Any:
    if not value:
        return {} if fallback is None else fallback
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return {} if fallback is None else fallback


def normalise_address(line1: str | None, city: str | None = None, zip_code: str | None = None) -> str:
    """A comparison key, not a display value.

    The substitutions collapse the abbreviation differences that make two spellings
    of one address look unrelated. A blank street address produces an empty key, so
    such records are never merged with each other on the strength of a city alone.
    """
    if not (line1 or "").strip():
        return ""
    parts = " ".join(part for part in (line1, city, zip_code) if part)
    lowered = parts.casefold()
    replacements = {
        r"\bstreet\b": "st", r"\bavenue\b": "ave", r"\broad\b": "rd",
        r"\bdrive\b": "dr", r"\blane\b": "ln", r"\bcourt\b": "ct",
        r"\bboulevard\b": "blvd", r"\bapartment\b": "apt", r"\bunit\b": "unit",
        r"\bnorth\b": "n", r"\bsouth\b": "s", r"\beast\b": "e", r"\bwest\b": "w",
    }
    for pattern, replacement in replacements.items():
        lowered = re.sub(pattern, replacement, lowered)
    lowered = re.sub(r"[^a-z0-9]+", " ", lowered)
    return " ".join(lowered.split())


# --- Session and jobs ------------------------------------------------------


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:16]}"


def enqueue(
    control_engine,
    onboarding_id: str,
    kind: str,
    *,
    document_id: str | None = None,
) -> OnboardingJob | None:
    """Add a job unless the same unit is already queued or claimed."""
    with Session(control_engine) as session:
        query = select(OnboardingJob).where(
            OnboardingJob.onboarding_id == onboarding_id,
            OnboardingJob.kind == kind,
            OnboardingJob.status.in_((JOB_PENDING, JOB_RUNNING)),
        )
        if document_id:
            query = query.where(OnboardingJob.document_id == document_id)
        else:
            query = query.where(OnboardingJob.document_id.is_(None))
        existing = session.exec(query).first()
        if existing is not None:
            return None
        job = OnboardingJob(
            id=new_id("job"),
            onboarding_id=onboarding_id,
            kind=kind,
            document_id=document_id,
        )
        session.add(job)
        session.commit()
        session.refresh(job)
        session.expunge(job)
        return job


def claim_next_job(control_engine) -> OnboardingJob | None:
    """Atomically take one pending job. Two workers cannot take the same row."""
    with Session(control_engine) as session:
        candidate = session.exec(
            select(OnboardingJob)
            .where(OnboardingJob.status == JOB_PENDING)
            .order_by(OnboardingJob.created_at)
            .limit(1)
        ).first()
        if candidate is None:
            return None
        claimed = session.execute(
            text(
                "UPDATE onboarding_jobs SET status=:running, attempts=attempts+1, "
                "started_at=:now WHERE id=:id AND status=:pending"
            ),
            {
                "running": JOB_RUNNING,
                "pending": JOB_PENDING,
                "now": utc_now().isoformat(),
                "id": candidate.id,
            },
        )
        if claimed.rowcount != 1:
            session.rollback()
            return None
        session.commit()
        job = session.get(OnboardingJob, candidate.id)
        session.refresh(job)
        session.expunge(job)
        return job


def _finish_job(control_engine, job_id: str, *, error: str | None = None) -> None:
    with Session(control_engine) as session:
        job = session.get(OnboardingJob, job_id)
        if job is None:
            return
        job.status = JOB_FAILED if error else JOB_DONE
        job.error = error
        job.finished_at = utc_now()
        session.add(job)
        session.commit()


# --- Document processing ---------------------------------------------------


def _save_extraction(session: Session, document: OnboardingDocument, result: ExtractionResult) -> None:
    existing = session.exec(
        select(OnboardingExtraction).where(OnboardingExtraction.document_id == document.id)
    ).first()
    payload = {
        "category": result.category,
        "fields": {
            name: {
                "value": jsonable(extracted.value),
                "page": extracted.page,
                "evidence": extracted.evidence,
                "method": extracted.method,
                "confidence": extracted.confidence,
            }
            for name, extracted in result.fields.items()
        },
        "method": result.method,
    }
    if existing is None:
        session.add(
            OnboardingExtraction(
                id=new_id("ext"),
                onboarding_id=document.onboarding_id,
                document_id=document.id,
                schema_name="onboarding.extraction",
                schema_version=SCHEMA_VERSION,
                payload_json=dumps(payload),
                method=result.method,
            )
        )
    else:
        existing.payload_json = dumps(payload)
        existing.method = result.method
        session.add(existing)


def process_document(control_engine, store: OnboardingStore, document_id: str) -> None:
    """Classify and extract one document. Raises nothing the caller must handle."""
    with Session(control_engine) as session:
        document = session.get(OnboardingDocument, document_id)
        if document is None or document.status in TERMINAL_DOCUMENT_STATES:
            return
        onboarding = session.get(OnboardingSession, document.onboarding_id)
        if onboarding is None or onboarding.status in {
            "CANCELLED", "EXPIRED", "COMPLETED", "PROVISIONING"
        }:
            return
        if document.duplicate_of_document_id:
            document.status = DOC_DUPLICATE
            document.error = "Identical to another file in this archive; retained once."
            document.processed_at = utc_now()
            session.add(document)
            session.commit()
            return
        document.status = DOC_EXTRACTING
        session.add(document)
        onboarding.status = ONBOARDING_PROCESSING
        onboarding.updated_at = utc_now()
        session.add(onboarding)
        session.commit()
        path = store.resolve(document.storage_path)
        file_format = document.format
        filename = document.original_filename
        onboarding_id = document.onboarding_id

    try:
        text_document = extract_text(path, file_format)
        pages = text_document.pages
        category = classify(filename, text_document.text)
        if category == "UNKNOWN" and file_format == "UNSUPPORTED":
            raise UnsupportedDocument("This file is not a supported document format.")

        extractor = structured_extractor()
        result = cast(ExtractionResult, extractor.extract(
            category=category, filename=filename, pages=pages
        ))
        result.category = category

        with Session(control_engine) as session:
            document = session.get(OnboardingDocument, document_id)
            if document is None:
                return
            document.category = category
            document.page_count = len(pages)
            document.text_chars = sum(len(page) for page in pages)
            document.extraction_method = text_document.method
            if document.format == "PDF":
                document.format = "SCANNED_PDF" if text_document.method.startswith("ocr:") else "NATIVE_PDF"
            document.status = DOC_READY if result.has_fields else DOC_NEEDS_REVIEW
            document.error = (
                None if result.has_fields
                else "No structured fields were recognised. The document is retained for review."
            )
            document.processed_at = utc_now()
            _save_extraction(session, document, result)
            session.add(document)
            session.commit()
    except UnsupportedDocument as exc:
        _fail_document(control_engine, document_id, DOC_UNSUPPORTED, str(exc))
    except DocumentUnreadable as exc:
        _fail_document(control_engine, document_id, DOC_FAILED, str(exc))
    except Exception as exc:  # noqa: BLE001 - one document must not fail the archive
        _fail_document(
            control_engine,
            document_id,
            DOC_FAILED,
            f"The document could not be processed: {str(exc)[:300]}",
        )
    finally:
        _maybe_finalize(control_engine, onboarding_id)


def _fail_document(control_engine, document_id: str, status: str, error: str) -> None:
    with Session(control_engine) as session:
        document = session.get(OnboardingDocument, document_id)
        if document is None:
            return
        document.status = status
        document.error = error[:500]
        document.processed_at = utc_now()
        session.add(document)
        session.commit()


def _maybe_finalize(control_engine, onboarding_id: str) -> None:
    with Session(control_engine) as session:
        documents = session.exec(
            select(OnboardingDocument).where(OnboardingDocument.onboarding_id == onboarding_id)
        ).all()
        if not documents or any(doc.status not in TERMINAL_DOCUMENT_STATES for doc in documents):
            return
        onboarding = session.get(OnboardingSession, onboarding_id)
        if onboarding is None or onboarding.status in {
            "READY_FOR_REVIEW", "PARTIALLY_PROCESSED", "REVIEWING", "APPROVED",
            "PROVISIONING", "COMPLETED", "CANCELLED", "EXPIRED",
        }:
            return
    enqueue(control_engine, onboarding_id, JOB_FINALIZE)


# --- Reconciliation --------------------------------------------------------


@dataclass
class Draft:
    """One proposed domain record while documents are being merged."""

    entity_type: str
    group_key: str
    title: str
    values: dict[str, Any] = field(default_factory=dict)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    conflicts: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    document_ids: set[str] = field(default_factory=set)
    history: list[dict[str, Any]] = field(default_factory=list)
    #: field name -> the statement date of the observation currently held.
    field_dates: dict[str, str] = field(default_factory=dict)
    #: the newest statement date that contributed a financial fact.
    latest_financial_date: str | None = None

    def add_evidence(
        self,
        name: str,
        value: Any,
        document_id: str,
        page: int,
        evidence: str,
        method: str,
        confidence: float,
    ) -> None:
        self.evidence.append({
            "field_name": name,
            "value": jsonable(value),
            "document_id": document_id,
            "page": page,
            "evidence": evidence,
            "method": method,
            "confidence": confidence,
        })
        self.document_ids.add(document_id)

    def merge(
        self,
        name: str,
        value: Any,
        document_id: str,
        page: int,
        evidence: str,
        method: str,
        confidence: float,
        *,
        conflict: bool = True,
    ) -> None:
        if value in (None, "", []):
            return
        self.add_evidence(name, value, document_id, page, evidence, method, confidence)
        current = self.values.get(name)
        if current in (None, "", []):
            # A key pre-seeded with an empty default is unset, not a value that
            # disagrees with the document.
            self.values[name] = value
            return
        if current == value:
            return
        if isinstance(current, list) and isinstance(value, list):
            merged = list(dict.fromkeys([*current, *value]))
            self.values[name] = merged
            return
        if not conflict:
            return
        current_evidence = next(
            (
                item for item in self.evidence
                if item["field_name"] == name and item["value"] == jsonable(current)
            ),
            None,
        )
        options = self.conflicts.setdefault(name, [{
            "value": jsonable(current),
            "document_id": (current_evidence or {}).get("document_id", document_id),
            "page": (current_evidence or {}).get("page", page),
            "evidence": (current_evidence or {}).get("evidence", evidence),
            "confidence": (current_evidence or {}).get("confidence", confidence),
        }])
        if not any(option["value"] == jsonable(value) for option in options):
            options.append({
                "value": jsonable(value),
                "document_id": document_id,
                "page": page,
                "evidence": evidence,
                "confidence": confidence,
            })


def _field_values(extraction: dict) -> dict[str, dict]:
    fields = extraction.get("fields") or {}
    return fields if isinstance(fields, dict) else {}


def _value(fields: dict, name: str) -> Any:
    item = fields.get(name) or {}
    return item.get("value") if isinstance(item, dict) else None


def _servicer_defaults(
    documents: list[OnboardingDocument],
    extractions: dict[str, dict],
) -> tuple[dict[str, str], dict[str, tuple[str, str, str, str]]]:
    """The one loan number and one property address per servicer, when unambiguous.

    A ZIP often contains a dozen statements for one loan and a 1098 that omits the
    full number. Filling the gap from the same servicer's other documents is
    reconciliation, not guessing: it applies only when the servicer has exactly one
    distinct value across the archive, and the review screen labels the value
    ``DERIVED``. A servicer with two loans or two high-confidence addresses gets no
    default, because those may be genuinely different records.
    """
    loan_sets: dict[str, set[str]] = {}
    address_sets: dict[str, dict[str, tuple[tuple[str, str, str, str], float]]] = {}
    for document in documents:
        payload = extractions.get(document.id)
        if payload is None:
            continue
        fields = _field_values(payload)
        lender = _value(fields, "lender") or institution_from_filename(document.original_filename)
        if not lender:
            continue
        servicer = str(lender).strip().casefold()
        if not servicer:
            continue
        loan = str(_value(fields, "loan_number") or "")
        if re.fullmatch(r"\d{6,24}", loan):
            loan_sets.setdefault(servicer, set()).add(loan)
        line1 = _value(fields, "address_line1")
        city = _value(fields, "city")
        if plausible_address(line1, city):
            state = str(_value(fields, "state") or "")
            zip_code = str(_value(fields, "zip_code") or "")
            # Group by the street line alone here. One statement may print the full
            # city/ZIP and another only the street; they are the same property, and
            # the most complete/highest-confidence version becomes the default.
            key = normalise_address(line1)
            confidence = float((fields.get("address_line1") or {}).get("confidence") or 0)
            parts = (str(line1), str(city), state, zip_code)
            score = (confidence, bool(city), bool(zip_code))
            existing = address_sets.setdefault(servicer, {}).get(key)
            if existing is None or score > existing[2]:
                address_sets[servicer][key] = (parts, confidence, score)

    default_loans = {
        servicer: next(iter(loans))
        for servicer, loans in loan_sets.items()
        if len(loans) == 1
    }
    default_addresses: dict[str, tuple[str, str, str, str]] = {}
    for servicer, addresses in address_sets.items():
        strong = {
            key: parts for key, (parts, confidence, _score) in addresses.items()
            if confidence >= 0.8
        }
        if len(strong) == 1:
            default_addresses[servicer] = next(iter(strong.values()))
    return default_loans, default_addresses


def _draft_from_document(
    document: OnboardingDocument,
    extraction: dict,
    drafts: dict[str, Draft],
    *,
    default_loans: dict[str, str] | None = None,
    default_addresses: dict[str, tuple[str, str, str, str]] | None = None,
) -> None:
    category = extraction.get("category") or document.category
    fields = dict(_field_values(extraction))
    default_loans = default_loans or {}
    default_addresses = default_addresses or {}

    def add(draft: Draft, name: str, *, conflict: bool = True) -> None:
        item = fields.get(name)
        if not isinstance(item, dict):
            return
        draft.merge(
            name,
            item.get("value"),
            document.id,
            int(item.get("page") or 0),
            item.get("evidence") or "",
            item.get("method") or "RULE",
            float(item.get("confidence") or 0.7),
            conflict=conflict,
        )

    def current_property_key() -> str:
        return normalise_address(
            _value(fields, "address_line1"), _value(fields, "city"), _value(fields, "zip_code")
        )

    property_key = current_property_key()

    def property_draft() -> Draft:
        key = f"property:{property_key}" if property_key else f"property:doc:{document.id}"
        draft = drafts.get(key)
        if draft is None:
            street = _value(fields, "address_line1") or ""
            city = _value(fields, "city") or ""
            title = street or f"Property from {document.original_filename}"
            draft = Draft(ENTITY_PROPERTY, key, title)
            draft.values = {
                "name": title,
                "address_line1": street,
                "address_line2": "",
                "city": city,
                "state": _value(fields, "state") or "",
                "zip_code": _value(fields, "zip_code") or "",
                "property_type": _value(fields, "property_type") or "RENTAL_INVESTMENT",
                "acquisition_date": None,
                "purchase_price_cents": None,
                "owner_names": [],
                "seller_names": [],
                "registration_number": None,
                "ownership_percentage": None,
                "short_label": None,
            }
            drafts[key] = draft
        for name in (
            "address_line1", "city", "state", "zip_code", "property_type",
            "acquisition_date", "purchase_price_cents", "owner_names",
            "seller_names", "registration_number", "ownership_percentage",
        ):
            add(draft, name)
        if not _value(fields, "property_type"):
            draft.add_evidence(
                "property_type", "RENTAL_INVESTMENT", document.id, 0,
                "Not found in the documents. PropTally defaults to rental investment; "
                "confirm before import.", "DEFAULT", 0.0,
            )
        if not _value(fields, "state"):
            draft.add_evidence(
                "state", "IL", document.id, 0,
                "Not found in the documents. PropTally's property form defaults to IL; "
                "correct it here if needed.", "DEFAULT", 0.0,
            )
        if draft.values.get("address_line1"):
            draft.title = draft.values["address_line1"]
            draft.values["name"] = draft.title
        return draft

    if category == "PROPERTY_DEED" or (
        category in {"PROPERTY_TAX", "INSURANCE", "VALUATION", "MAINTENANCE"}
        and property_key
    ):
        # A deed with no address is still a property that needs a human. A tax bill
        # or invoice with no address is supporting evidence only; it must not invent
        # a door of its own.
        property_draft()

    elif category in {"MORTGAGE_STATEMENT", "LOAN_AGREEMENT"}:
        lender = str(
            _value(fields, "lender")
            or institution_from_filename(document.original_filename)
            or ""
        ).strip()
        servicer_key = lender.casefold()

        # Fill identity from the one unambiguous value across this servicer's
        # documents. This is reconciliation, not invention: the value is sourced
        # from the same servicer's other statements and labelled DERIVED in review.
        loan_number = str(_value(fields, "loan_number") or "")
        if not re.fullmatch(r"\d{6,24}", loan_number):
            loan_number = default_loans.get(servicer_key, "")
            if loan_number:
                fields["loan_number"] = {
                    "value": loan_number,
                    "page": 0,
                    "evidence": (
                        f"Matched to the only loan number found on {lender or 'this servicer'}'s "
                        "other statements in this archive."
                    ),
                    "method": "DERIVED",
                    "confidence": 0.6,
                }
        if not loan_number:
            fields.pop("loan_number", None)
            loan_number = ""

        if not _value(fields, "lender") and lender:
            fields["lender"] = {
                "value": lender,
                "page": 0,
                "evidence": f"Archive folder: {lender}",
                "method": "DERIVED",
                "confidence": 0.7,
            }

        current_address = _value(fields, "address_line1")
        current_confidence = float(
            (fields.get("address_line1") or {}).get("confidence") or 0
        )
        if (
            (not plausible_address(current_address, _value(fields, "city")) or current_confidence < 0.7)
            and servicer_key in default_addresses
        ):
            line1, city, state, zip_code = default_addresses[servicer_key]
            for name, value in (
                ("address_line1", line1), ("city", city),
                ("state", state), ("zip_code", zip_code),
            ):
                fields[name] = {
                    "value": value,
                    "page": 0,
                    "evidence": (
                        f"Matched to the property address on {lender or 'this servicer'}'s "
                        "other statements in this archive."
                    ),
                    "method": "DERIVED",
                    "confidence": 0.7,
                }

        property_key = current_property_key()
        property = property_draft() if property_key else None

        masked = _value(fields, "account_number_masked")
        if not masked and loan_number:
            masked = "..." + loan_number[-4:]
            fields["account_number_masked"] = {
                "value": masked,
                "page": 0,
                "evidence": f"Derived from loan number {loan_number}.",
                "method": "DERIVED",
                "confidence": 0.7,
            }

        if loan_number:
            key = f"mortgage:loan:{loan_number}"
        elif lender and property_key:
            key = f"mortgage:{lender.casefold()}:{property_key}"
        elif lender:
            key = f"mortgage:{lender.casefold()}"
        else:
            key = f"mortgage:doc:{document.id}"

        draft = drafts.get(key)
        if draft is None:
            title = " ".join(part for part in (lender or "Mortgage", loan_number or "") if part)
            draft = Draft(ENTITY_MORTGAGE, key, title or "Mortgage")
            draft.values = {
                "lender": lender or "",
                "loan_number": loan_number or "",
                "account_number_masked": masked or "",
                "original_principal_cents": None,
                "outstanding_principal_cents": None,
                "interest_rate_bps": None,
                "term_months": None,
                "emi_amount_cents": None,
                "origination_date": None,
                "maturity_date": None,
                "statement_date": None,
                "escrow_balance_cents": None,
                "property_group_key": property.group_key if property else "",
                "property_address": _value(fields, "address_line1") or "",
                "property_city": _value(fields, "city") or "",
                "property_state": _value(fields, "state") or "",
                "property_zip": _value(fields, "zip_code") or "",
                "rate_type": "FIXED",
            }
            drafts[key] = draft

        for name in (
            "lender", "loan_number", "account_number_masked", "original_principal_cents",
            "term_months", "origination_date", "maturity_date",
        ):
            add(draft, name)

        # Payment, rate and balances are facts with dates. A statement from 2021
        # does not contradict a 2026 statement; the newest dated observation wins and
        # every observation remains in evidence.
        add(draft, "statement_date", conflict=False)
        for name in (
            "outstanding_principal_cents", "escrow_balance_cents",
            "emi_amount_cents", "interest_rate_bps",
        ):
            item = fields.get(name)
            if not isinstance(item, dict):
                continue
            balance_date = _value(fields, "statement_date")
            draft.add_evidence(
                name, item.get("value"), document.id, int(item.get("page") or 0),
                item.get("evidence") or "", item.get("method") or "RULE",
                float(item.get("confidence") or 0.7),
            )
            draft.history.append({
                "field": name,
                "value": jsonable(item.get("value")),
                "as_of": jsonable(balance_date),
                "document_id": document.id,
                "page": int(item.get("page") or 0),
                "evidence": item.get("evidence") or "",
            })
            # Each field follows its own date. A later statement of one servicer
            # must not be overwritten by an older statement processed later.
            current_field_date = draft.field_dates.get(name)
            if current_field_date is None or (
                balance_date and str(balance_date) >= current_field_date
            ):
                draft.values[name] = item.get("value")
                if balance_date:
                    draft.field_dates[name] = str(balance_date)
            if balance_date and (
                draft.latest_financial_date is None
                or str(balance_date) > draft.latest_financial_date
            ):
                draft.latest_financial_date = str(balance_date)
        if draft.latest_financial_date:
            draft.values["statement_date"] = draft.latest_financial_date

        if property is not None:
            draft.values["property_group_key"] = property.group_key
            draft.values["property_address"] = property.values.get("address_line1") or draft.values["property_address"]
            draft.values["property_city"] = property.values.get("city") or draft.values["property_city"]
            draft.values["property_state"] = property.values.get("state") or draft.values["property_state"]
            draft.values["property_zip"] = property.values.get("zip_code") or draft.values["property_zip"]

    elif category == "BANK_STATEMENT":
        institution = _value(fields, "institution_name") or ""
        masked = _value(fields, "account_number_masked") or ""
        account_type = _value(fields, "account_type") or "CHECKING"
        key = f"account:{institution.casefold()}:{masked}" if institution else f"account:doc:{document.id}"
        draft = drafts.get(key)
        if draft is None:
            draft = Draft(
                ENTITY_ACCOUNT,
                key,
                " ".join(part for part in (institution or "Account", account_type.replace("_", " ").title()) if part),
            )
            draft.values = {
                "institution_name": institution,
                "account_name": f"{institution} {account_type.replace('_', ' ').title()}".strip(),
                "account_type": account_type,
                "account_number_masked": masked,
                "currency": "USD",
                "opening_balance_cents": None,
                "closing_balance_cents": None,
                "statement_period_start": None,
                "statement_period_end": None,
            }
            drafts[key] = draft
        for name in ("institution_name", "account_number_masked", "account_type", "currency"):
            add(draft, name)
        for name in ("opening_balance_cents", "closing_balance_cents", "statement_period_start", "statement_period_end"):
            add(draft, name, conflict=False)
        if not _value(fields, "account_type"):
            draft.add_evidence(
                "account_type", "CHECKING", document.id, 0,
                "Not found in the document. Confirm the account type before import.",
                "DEFAULT", 0.0,
            )

    elif category == "RENTAL_AGREEMENT":
        property = property_draft()
        tenant = _value(fields, "tenant_name") or ""
        key = f"lease:{property.group_key}:{tenant.casefold()}"
        draft = drafts.get(key)
        if draft is None:
            draft = Draft(ENTITY_LEASE, key, f"Lease — {tenant}" if tenant else "Rental agreement")
            draft.values = {
                "property_group_key": property.group_key,
                "property_address": _value(fields, "address_line1") or property.values.get("address_line1") or "",
                "tenant_name": tenant,
                "landlord": _value(fields, "landlord") or "",
                "monthly_rent_cents": None,
                "security_deposit_cents": None,
                "lease_start": None,
                "lease_end": None,
                "payment_frequency": _value(fields, "payment_frequency") or "MONTHLY",
            }
            drafts[key] = draft
        for name in (
            "tenant_name", "landlord", "monthly_rent_cents", "security_deposit_cents",
            "lease_start", "lease_end", "payment_frequency",
        ):
            add(draft, name)
        if tenant:
            draft.title = f"Lease — {tenant}"

    else:
        # An unknown document is retained, never turned into a proposal.
        return


def _merge_partial_property_drafts(drafts: dict[str, Draft]) -> None:
    """Collapse a street-only proposal into the full address for the same street.

    One document may print the address over three lines and another may print only
    "1017 DR JOHN BURKEY DR". They are the same door. The merge is limited to drafts
    whose street line is identical, and only when at least one side lacks a city or
    ZIP; two complete but different addresses are left separate.
    """
    groups: dict[str, list[Draft]] = {}
    for draft in drafts.values():
        if draft.entity_type != ENTITY_PROPERTY:
            continue
        line1 = str(draft.values.get("address_line1") or "").strip()
        if line1:
            groups.setdefault(normalise_address(line1), []).append(draft)

    for group in groups.values():
        if len(group) < 2:
            continue
        canonical = max(
            group,
            key=lambda item: (
                bool(item.values.get("city")),
                bool(item.values.get("zip_code")),
                len(item.document_ids),
            ),
        )
        for other in list(group):
            if other is canonical:
                continue
            both_complete = (
                canonical.values.get("city") and canonical.values.get("zip_code")
                and other.values.get("city") and other.values.get("zip_code")
            )
            if both_complete and (
                str(other.values.get("city") or "").casefold(),
                str(other.values.get("zip_code") or "").casefold(),
            ) != (
                str(canonical.values.get("city") or "").casefold(),
                str(canonical.values.get("zip_code") or "").casefold(),
            ):
                # Two genuinely different complete addresses on the same street line.
                continue
            for name, value in other.values.items():
                if canonical.values.get(name) in (None, "", []) and value not in (None, "", []):
                    canonical.values[name] = value
            canonical.evidence.extend(other.evidence)
            canonical.document_ids |= other.document_ids
            for name, options in other.conflicts.items():
                existing = canonical.conflicts.setdefault(name, [])
                for option in options:
                    if not any(item["value"] == option["value"] for item in existing):
                        existing.append(option)
            for candidate in drafts.values():
                if candidate.values.get("property_group_key") == other.group_key:
                    candidate.values["property_group_key"] = canonical.group_key
            drafts.pop(other.group_key, None)


def build_proposals(control_engine, onboarding_id: str) -> tuple[int, int]:
    """Reconcile all terminal documents into staging proposals.

    Returns ``(proposal_count, conflict_count)``. Existing undecided proposals are
    replaced, which makes a re-run safe; accepted or rejected decisions stop a
    rebuild, because those are the customer's work and not extraction output.
    """
    with Session(control_engine) as session:
        decided = session.exec(
            select(OnboardingProposal).where(
                OnboardingProposal.onboarding_id == onboarding_id,
                OnboardingProposal.status != PROPOSAL_NEEDS_REVIEW,
            )
        ).first()
        # A field edit is customer work even before the record is accepted, so a
        # retry must not delete and rebuild the proposals underneath it.
        reviewed = session.exec(
            select(OnboardingReviewDecision).where(
                OnboardingReviewDecision.onboarding_id == onboarding_id
            )
        ).first()
        if decided is not None or reviewed is not None:
            proposals = session.exec(
                select(OnboardingProposal).where(OnboardingProposal.onboarding_id == onboarding_id)
            ).all()
            return len(proposals), sum(len(loads(p.conflicts_json)) for p in proposals)

        for proposal in session.exec(
            select(OnboardingProposal).where(OnboardingProposal.onboarding_id == onboarding_id)
        ).all():
            for evidence in session.exec(
                select(OnboardingFieldEvidence).where(
                    OnboardingFieldEvidence.proposal_id == proposal.id
                )
            ).all():
                session.delete(evidence)
            session.delete(proposal)
        session.commit()

        documents = session.exec(
            select(OnboardingDocument)
            .where(OnboardingDocument.onboarding_id == onboarding_id)
            .order_by(OnboardingDocument.original_filename)
        ).all()
        drafts: dict[str, Draft] = {}
        extractions: dict[str, dict] = {}
        for document in documents:
            if document.status not in {DOC_READY, DOC_NEEDS_REVIEW, DOC_EXTRACTED, DOC_CLASSIFIED}:
                continue
            extraction = session.exec(
                select(OnboardingExtraction).where(OnboardingExtraction.document_id == document.id)
            ).first()
            if extraction is None:
                continue
            extractions[document.id] = loads(extraction.payload_json)

        default_loans, default_addresses = _servicer_defaults(documents, extractions)
        for document in documents:
            payload = extractions.get(document.id)
            if payload is None:
                continue
            _draft_from_document(
                document, payload, drafts,
                default_loans=default_loans,
                default_addresses=default_addresses,
            )

        _merge_partial_property_drafts(drafts)

        # Attach mortgages and leases to a property when the association is
        # unambiguous. An ambiguous one is left marked, never guessed.
        property_drafts = [draft for draft in drafts.values() if draft.entity_type == ENTITY_PROPERTY]
        for draft in drafts.values():
            if draft.entity_type not in {ENTITY_MORTGAGE, ENTITY_LEASE}:
                continue
            target = draft.values.get("property_group_key")
            if target and target in drafts:
                draft.values["association_status"] = "MATCHED"
                continue
            if len(property_drafts) == 1:
                draft.values["property_group_key"] = property_drafts[0].group_key
                draft.values["association_status"] = "MATCHED_BY_ONLY_PROPERTY"
            elif property_drafts:
                draft.values["association_status"] = "AMBIGUOUS"
                draft.conflicts.setdefault("property_group_key", [
                    {
                        "value": item.group_key,
                        "document_id": min(draft.document_ids) if draft.document_ids else "",
                        "page": 0,
                        "evidence": item.title,
                        "confidence": 0.5,
                    }
                    for item in property_drafts
                ])
            else:
                draft.values["association_status"] = "MISSING"

        conflicts = 0
        for draft in drafts.values():
            proposal = OnboardingProposal(
                id=new_id("prp"),
                onboarding_id=onboarding_id,
                entity_type=draft.entity_type,
                status=PROPOSAL_NEEDS_REVIEW,
                group_key=draft.group_key,
                title=draft.title,
                original_payload_json=dumps(draft.values),
                payload_json=dumps(draft.values),
                conflicts_json=dumps(draft.conflicts),
                edited_fields_json="{}",
                source_document_ids_json=dumps(sorted(draft.document_ids)),
            )
            session.add(proposal)
            session.flush()
            for item in draft.evidence:
                session.add(
                    OnboardingFieldEvidence(
                        id=new_id("ev"),
                        onboarding_id=onboarding_id,
                        proposal_id=proposal.id,
                        field_name=item["field_name"],
                        value_json=dumps(item["value"]),
                        document_id=item["document_id"],
                        page_number=item["page"],
                        evidence_text=item["evidence"],
                        method=item["method"],
                        confidence=item["confidence"],
                    )
                )
            conflicts += len(draft.conflicts)
        session.commit()
        return len(drafts), conflicts


def finalize_onboarding(control_engine, onboarding_id: str) -> None:
    """Turn a fully processed archive into a review package."""
    proposals_count, conflicts = build_proposals(control_engine, onboarding_id)
    with Session(control_engine) as session:
        onboarding = session.get(OnboardingSession, onboarding_id)
        if onboarding is None:
            return
        documents = session.exec(
            select(OnboardingDocument).where(OnboardingDocument.onboarding_id == onboarding_id)
        ).all()
        failed = [doc for doc in documents if doc.status in {DOC_FAILED}]
        unsupported = [doc for doc in documents if doc.status == DOC_UNSUPPORTED]
        if proposals_count == 0:
            onboarding.status = ONBOARDING_PROCESSING_FAILED
            if failed or unsupported:
                onboarding.last_error = (
                    "No records could be proposed. Some documents failed or were "
                    "unsupported; review them below and retry."
                )
            else:
                onboarding.last_error = "No property, mortgage or account information was found."
        elif failed or unsupported:
            onboarding.status = ONBOARDING_PARTIAL
            onboarding.last_error = (
                f"{len(failed)} document(s) failed and {len(unsupported)} were unsupported. "
                "The rest are ready for review."
            )
        else:
            onboarding.status = ONBOARDING_READY
            onboarding.last_error = None
        if conflicts:
            onboarding.last_error = (
                (onboarding.last_error + " ") if onboarding.last_error else ""
            ) + f"{conflicts} field conflict(s) need your decision."
        onboarding.updated_at = utc_now()
        session.add(onboarding)
        session.commit()


# --- Job execution ---------------------------------------------------------


def run_job(control_engine, store: OnboardingStore, tenant_manager, job: OnboardingJob) -> None:
    try:
        if job.kind == JOB_DOCUMENT and job.document_id:
            process_document(control_engine, store, job.document_id)
            _finish_job(control_engine, job.id)
        elif job.kind == JOB_FINALIZE:
            finalize_onboarding(control_engine, job.onboarding_id)
            _finish_job(control_engine, job.id)
        else:
            from proptally.onboarding.provisioning import run_provisioning

            errors = run_provisioning(control_engine, tenant_manager, store, job.onboarding_id)
            _finish_job(control_engine, job.id, error="; ".join(errors) if errors else None)
    except Exception as exc:  # noqa: BLE001 - a job failure is recorded, not raised
        _finish_job(control_engine, job.id, error=str(exc)[:500])
        if job.document_id:
            _fail_document(control_engine, job.document_id, DOC_FAILED, str(exc)[:500])


def process_all(control_engine, store: OnboardingStore, tenant_manager, limit: int = 200) -> int:
    """Drain pending jobs. Used by the worker thread and by the test harness."""
    processed = 0
    while processed < limit:
        job = claim_next_job(control_engine)
        if job is None:
            break
        run_job(control_engine, store, tenant_manager, job)
        processed += 1
    return processed


# --- Review actions --------------------------------------------------------


def record_decision(
    control_engine,
    proposal_id: str,
    *,
    actor_user_id: str,
    decision: str,
    field_name: str | None = None,
    value: Any = None,
    note: str | None = None,
) -> OnboardingProposal:
    """Apply one customer decision, preserving what it replaced.

    A field edit changes only that field's value; the original extraction remains in
    ``original_payload_json`` and the prior value is written to the decision row.
    """
    with Session(control_engine) as session:
        proposal = session.get(OnboardingProposal, proposal_id)
        if proposal is None:
            raise ValueError("Proposed record not found.")
        onboarding = session.get(OnboardingSession, proposal.onboarding_id)
        if onboarding is None or onboarding.status in {"COMPLETED", "PROVISIONING", "CANCELLED", "EXPIRED"}:
            raise ValueError("This onboarding session is no longer open for review.")

        payload = loads(proposal.payload_json)
        previous = jsonable(payload.get(field_name)) if field_name else dumps(payload)
        if field_name:
            if decision in {"EDIT", "RESOLVE_CONFLICT", "ACCEPT"} and value is not None:
                payload[field_name] = value
            elif decision == "MARK_UNKNOWN":
                payload[field_name] = None
            proposal.payload_json = dumps(payload)
            if decision in {"EDIT", "RESOLVE_CONFLICT", "MARK_UNKNOWN"}:
                edited = loads(proposal.edited_fields_json)
                edited[field_name] = {
                    "original": loads(proposal.original_payload_json).get(field_name),
                    "revised": payload.get(field_name),
                    "decision": decision,
                }
                proposal.edited_fields_json = dumps(edited)
            conflicts = loads(proposal.conflicts_json)
            if decision in {"RESOLVE_CONFLICT", "ACCEPT", "EDIT", "MARK_UNKNOWN"}:
                conflicts.pop(field_name, None)
                proposal.conflicts_json = dumps(conflicts)
        else:
            if decision == DECISION_ACCEPT:
                conflicts = loads(proposal.conflicts_json)
                if conflicts:
                    raise ValueError(
                        "Resolve the conflicting fields before accepting this record."
                    )
                proposal.status = "ACCEPTED"
            elif decision == "REJECT":
                proposal.status = "REJECTED"
            elif decision == "DEFER":
                proposal.status = "DEFERRED"
            else:
                raise ValueError(f"Unknown decision {decision!r}.")
        proposal.updated_at = utc_now()
        session.add(proposal)
        session.add(
            OnboardingReviewDecision(
                id=new_id("dec"),
                onboarding_id=proposal.onboarding_id,
                proposal_id=proposal.id,
                field_name=field_name,
                decision=decision,
                previous_value_json=dumps(previous) if previous is not None else None,
                new_value_json=dumps(value) if value is not None else None,
                actor_user_id=actor_user_id,
                note=note,
            )
        )
        if onboarding.status == ONBOARDING_READY or onboarding.status == ONBOARDING_PARTIAL:
            onboarding.status = ONBOARDING_REVIEWING
        onboarding.updated_at = utc_now()
        session.add(onboarding)
        session.commit()
        session.refresh(proposal)
        session.expunge(proposal)
        return proposal


def review_counts(control_engine, onboarding_id: str) -> dict[str, int]:
    with Session(control_engine) as session:
        proposals = session.exec(
            select(OnboardingProposal).where(OnboardingProposal.onboarding_id == onboarding_id)
        ).all()
        return {
            "total": len(proposals),
            "accepted": sum(1 for p in proposals if p.status == "ACCEPTED"),
            "rejected": sum(1 for p in proposals if p.status == "REJECTED"),
            "deferred": sum(1 for p in proposals if p.status == "DEFERRED"),
            "needs_review": sum(1 for p in proposals if p.status == "NEEDS_REVIEW"),
            "imported": sum(1 for p in proposals if p.status == "IMPORTED"),
            "conflicts": sum(len(loads(p.conflicts_json)) for p in proposals),
        }
