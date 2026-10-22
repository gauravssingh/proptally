"""Idempotent, local-only ingestion of statements into the PropTally vault."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from uuid import uuid4

from sqlalchemy import desc
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session, select

from proptally.db.models import (
    Document,
    DocumentExtraction,
    EscrowAccount,
    ExtractionVersion,
    FinancialAccount,
    FinancialInstitution,
    LoanAccount,
    MortgagePeriod,
    Property,
    Statement,
    Transaction,
    TransactionProcessingState,
    utc_now,
)
from proptally.db.schema import init_schema
from proptally.db.tenant_manager import slugify
from proptally.ingestion.bank_parsers import (
    BankStatementExtraction,
    BankTransaction,
    extract_bank_or_card_pdf,
)
from proptally.ingestion.doc_intel import DocIntelJob, DocumentIntelligenceClient
from proptally.ingestion.parsers import (
    PARSER_VERSION,
    MortgageExtraction,
    extract_pdf,
    validate_mortgage_extraction,
)

#: Statement types the bank/card parsers produce. Everything else this service
#: ingests is a servicer document attached to a mortgage loan.
BANK_STATEMENT_TYPES = ("CHECKING", "CREDIT_CARD")

#: Words that carry no identity in an institution's name; dropping them lets
#: "Bank of America" and "Bank of America, N.A." count as the same institution
#: without any list of known institutions existing anywhere.
_INSTITUTION_NOISE = frozenset({
    "bank", "card", "credit", "the", "of", "and", "n", "a", "na", "usa", "us",
    "inc", "llc", "national", "association", "company", "corp", "trust",
    "savings", "fsb", "ssb",
})

#: The mask a transfer description uses in front of another account's digits:
#: "Online payment from CHK 9456", "to card •••• 9306", "...0708".
_ACCOUNT_MENTION_PREFIX = r"(?:\*+|•+|x{2,}|\.{3}|chk\s+|ending\s+in\s+)"


class IngestionPersistenceError(RuntimeError):
    def __init__(self, message: str, orphaned_vault_file: Path | None = None):
        super().__init__(message)
        self.orphaned_vault_file = orphaned_vault_file


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _digits(value: object) -> str:
    """Digits only: statements and masks group numbers with spaces and dashes."""
    return re.sub(r"\D", "", "" if value is None else str(value))


def _institution_tokens(name: str | None) -> set[str]:
    return {
        token for token in re.findall(r"[a-z0-9]+", (name or "").lower())
        if token not in _INSTITUTION_NOISE
    }


def _names_agree(registered: str | None, observed: str | None) -> bool:
    """Whether two institution names plausibly name the same institution.

    Neither name is authoritative: the account number decides. This only breaks
    ties between accounts sharing a last-four, so a name absent on either side
    counts as agreement.
    """
    if not registered or not observed:
        return True
    return bool(_institution_tokens(registered) & _institution_tokens(observed))


def _institution_name(session: Session, account: FinancialAccount) -> str | None:
    institution = session.get(FinancialInstitution, account.institution_id)
    return institution.name if institution else None


def resolve_extraction_data(session: Session, data: dict) -> list[str]:
    """Records which of *this portfolio's* accounts an extraction belongs to.

    The parsers report only what a document said. This is the step that decides
    which registered account that is, using the loan numbers, masked account
    numbers and institution names the portfolio itself holds. It fills in
    ``financial_account_id`` / ``loan_account_id`` / ``property_id`` /
    ``institution_id``, and returns the review reasons to record when nothing
    registered matches: an unrecognised account is retained for a human, never
    guessed at and never invented.
    """
    if data.get("statement_type") in BANK_STATEMENT_TYPES:
        return _resolve_bank_data(session, data)
    return _resolve_mortgage_data(session, data)


def _resolve_mortgage_data(session: Session, data: dict) -> list[str]:
    chosen = data.get("loan_account_id")
    if chosen:
        loan = session.get(LoanAccount, chosen)
        if loan is None:
            return [f"No loan account {chosen} is registered in this portfolio; choose a registered loan."]
    else:
        loan = _match_loan(session, _digits(data.get("loan_number")), data.get("financial_account_id"))

    if loan is None:
        observed = _digits(data.get("loan_number")) or (data.get("account_last4") or "unknown")
        servicer = data.get("servicer_name")
        printed_at = f" at {servicer}" if servicer else ""
        for key in ("loan_account_id", "financial_account_id", "property_id"):
            data[key] = None
        reason = (
            f"No registered account matches loan number {observed}{printed_at}; "
            + "register the account and re-run, or choose a registered loan."
        )
        return [reason]

    data["loan_account_id"] = loan.id
    data["financial_account_id"] = loan.financial_account_id
    data["property_id"] = loan.property_id
    return []


def _match_loan(session: Session, loan_number: str, chosen_account_id: str | None) -> LoanAccount | None:
    """The portfolio's loan that a printed loan number refers to, if exactly one."""
    if loan_number:
        exact = session.exec(
            select(LoanAccount).where(LoanAccount.loan_number == loan_number)
        ).first()
        if exact is not None:
            return exact
        if len(loan_number) >= 4:
            # Notices often print only the tail of a loan number.
            tail = loan_number[-4:]
            candidates = [
                loan for loan in session.exec(select(LoanAccount)).all()
                if _digits(loan.loan_number).endswith(tail)
            ]
            if len(candidates) == 1:
                return candidates[0]
    if chosen_account_id:
        loans = session.exec(
            select(LoanAccount).where(LoanAccount.financial_account_id == chosen_account_id)
        ).all()
        if len(loans) == 1:
            return loans[0]
    return None


def _resolve_bank_data(session: Session, data: dict) -> list[str]:
    chosen = data.get("financial_account_id")
    if chosen:
        account = session.get(FinancialAccount, chosen)
        if account is None:
            return [f"No financial account {chosen} is registered in this portfolio; choose a registered account."]
        data["institution_id"] = account.institution_id
        return []

    account = _match_bank_account(session, data)
    if account is None:
        last4 = _digits(data.get("account_last4"))
        institution = data.get("institution_name") or "the statement"
        ending = f" ending {last4}" if last4 else ""
        data["financial_account_id"] = None
        data["institution_id"] = None
        reason = (
            f"No registered account matches {institution}{ending}; "
            + "register the account and re-run, or register it from this statement."
        )
        return [reason]

    data["financial_account_id"] = account.id
    data["institution_id"] = account.institution_id
    return []


def _match_bank_account(session: Session, data: dict) -> FinancialAccount | None:
    """The portfolio's account that a statement belongs to, if exactly one."""
    last4 = _digits(data.get("account_last4"))
    observed_name = data.get("institution_name")
    accounts = [
        account for account in session.exec(select(FinancialAccount)).all()
        if account.account_type == data.get("statement_type")
    ]
    if last4:
        by_number = [a for a in accounts if _digits(a.account_number_masked).endswith(last4)]
        named = [a for a in by_number if _names_agree(_institution_name(session, a), observed_name)]
        if len(named) == 1:
            return named[0]
        if not named and len(by_number) == 1:
            return by_number[0]
        return None
    named = [a for a in accounts if _names_agree(_institution_name(session, a), observed_name)]
    return named[0] if len(named) == 1 else None


def register_observed_account(session: Session, data: dict, *, commit: bool = False) -> FinancialAccount:
    """Registers the bank account a statement names, from the statement's identity.

    This is the one place an entity is created from a document, and it is only
    reached for a bank or card statement whose institution and last four digits
    were observed: a human confirmed the statement belongs to this portfolio.
    Ids are derived from the observed name and digits, so no portfolio's account
    numbers appear in code.
    """
    statement_type = data.get("statement_type")
    if statement_type not in BANK_STATEMENT_TYPES:
        raise ValueError("Only bank and credit card statements carry the identity needed to register an account.")
    institution_name = (data.get("institution_name") or "").strip()
    last4 = _digits(data.get("account_last4"))
    if not institution_name or len(last4) < 4:
        raise ValueError("The statement does not name both an institution and the last four digits to register.")

    institution_id = data.get("institution_id") or f"inst_{slugify(institution_name)}"
    if session.get(FinancialInstitution, institution_id) is None:
        session.add(FinancialInstitution(id=institution_id, name=institution_name))

    account_id = f"acct_{slugify(institution_name)}_{last4}"
    account = session.get(FinancialAccount, account_id)
    if account is None:
        account = FinancialAccount(
            id=account_id,
            institution_id=institution_id,
            account_name=f"{institution_name} {statement_type.replace('_', ' ').title()} (...{last4})",
            account_type=statement_type,
            account_number_masked=f"...{last4}",
        )
        session.add(account)
    session.flush()

    data["institution_id"] = institution_id
    data["financial_account_id"] = account.id
    if commit:
        session.commit()
    return account


def resolve_statement_extraction(
    session: Session,
    extraction: MortgageExtraction | BankStatementExtraction,
) -> MortgageExtraction | BankStatementExtraction:
    """Attaches an extraction to this portfolio's accounts, or flags it for review."""
    data = extraction.to_dict()
    reasons = resolve_extraction_data(session, data)
    extraction.financial_account_id = data.get("financial_account_id")
    extraction.loan_account_id = data.get("loan_account_id")
    extraction.property_id = data.get("property_id")
    if isinstance(extraction, BankStatementExtraction):
        extraction.institution_id = data.get("institution_id")
    extraction.resolution_reasons = list(reasons)
    extraction.status = (
        "NEEDS_REVIEW" if (extraction.review_reasons or extraction.resolution_reasons) else "VALIDATED"
    )
    return extraction


def extract_and_resolve(
    path: Path,
    session: Session,
) -> MortgageExtraction | BankStatementExtraction | None:
    """Extracts a statement PDF and matches it against this portfolio's accounts.

    Returns None when the document is not a recognised statement format. A
    recognised statement for an account this portfolio does not have comes back
    as NEEDS_REVIEW with the reason, so it can be registered and re-run.
    """
    # Bank/card templates are more specific than the generic mortgage parser.
    # Trying the mortgage parser first made credit-card statements that print an
    # "account number" look like mortgages when their bank parser did not match.
    extraction = extract_bank_or_card_pdf(path)
    if extraction is None:
        extraction = extract_pdf(path)
    if extraction is None:
        return None
    return resolve_statement_extraction(session, extraction)


def _prepare_entities(session: Session, data: dict) -> None:
    """Attaches an approved mortgage extraction to the portfolio's own loan.

    Nothing is invented here: the loan must already be registered, and the
    account and property come from that loan rather than from the document. The
    only row created is the loan's escrow account, which is a record of an
    existing loan rather than a new entity.
    """
    loan_id = data.get("loan_account_id")
    loan = session.get(LoanAccount, loan_id) if loan_id else None
    if loan is None:
        raise ValueError(
            f"No loan account is registered for loan number {data.get('loan_number')!r}; "
            "register the loan and re-run."
        )
    account = session.get(FinancialAccount, loan.financial_account_id)
    if account is None:
        raise ValueError("The registered loan has no financial account to post against.")
    if session.get(Property, loan.property_id) is None:
        raise ValueError("The registered loan is not attached to a property in this portfolio.")

    # The registered loan is the authority on which account and property this
    # statement belongs to.
    data["financial_account_id"] = account.id
    data["property_id"] = loan.property_id

    needs_escrow = data.get("escrow_cents") is not None or data.get("escrow_balance_cents") is not None
    escrow = session.exec(
        select(EscrowAccount).where(EscrowAccount.loan_account_id == loan.id)
    ).first()
    if needs_escrow and escrow is None:
        session.add(EscrowAccount(
            id=f"escrow_{uuid4().hex[:12]}", loan_account_id=loan.id
        ))
    session.flush()


def _copy_to_vault(source: Path, digest: str, vault_dir: Path, suffix: str = ".pdf") -> tuple[Path, bool]:
    stored_path = vault_dir / digest[:2] / f"{digest}{suffix}"
    stored_path.parent.mkdir(parents=True, exist_ok=True)
    if stored_path.exists():
        if _sha256(stored_path) != digest:
            raise OSError(f"Vault file exists with unexpected content: {stored_path}")
        return stored_path, False

    staging_path = stored_path.with_name(f".{stored_path.name}.{uuid4().hex}.tmp")
    created = False
    try:
        shutil.copy2(source, staging_path)
        # The source may still be changing (an upload in flight); the vault name
        # must describe the bytes actually stored, not the bytes observed earlier.
        if _sha256(staging_path) != digest:
            raise OSError(
                "Source changed while being copied to the vault; refusing to store "
                "content under a digest it does not match."
            )
        try:
            os.link(staging_path, stored_path)
            created = True
        except FileExistsError:
            if _sha256(stored_path) != digest:
                raise OSError(f"Vault file exists with unexpected content: {stored_path}")
    finally:
        staging_path.unlink(missing_ok=True)
    return stored_path, created


def _persist_document(
    path: Path,
    digest: str,
    vault_dir: Path,
    session: Session,
    extraction: MortgageExtraction | BankStatementExtraction | None,
    *,
    original_filename: str | None = None,
    document_type: str | None = None,
    property_id: str | None = None,
    suffix: str = ".pdf",
) -> tuple[Document, str | None]:
    stored_path, created_vault_file = _copy_to_vault(path, digest, vault_dir, suffix)
    document = Document(
        id=str(uuid4()), file_sha256=digest, original_filename=original_filename or path.name,
        storage_path=str(stored_path), file_size_bytes=path.stat().st_size,
        # The media type follows the file, not the model's default: a PNG came in as an
        # image, and the vault serves, previews and describes it as one.
        mime_type=VAULT_SUFFIX_TYPES.get(suffix, "application/octet-stream"),
        document_type=document_type, property_id=property_id,
    )
    extraction_id = str(uuid4()) if extraction else None
    try:
        with session.begin_nested():
            session.add(document)
            session.flush()
            if extraction and extraction_id:
                session.add(DocumentExtraction(
                    id=extraction_id,
                    document_id=document.id,
                    parser_name=extraction.parser_name,
                    parser_version=PARSER_VERSION,
                    validation_status=extraction.status,
                    approval_status="PENDING",
                    extracted_data_json=json.dumps(extraction.to_dict(), sort_keys=True),
                ))
                session.flush()
        session.commit()
    except Exception as exc:
        session.rollback()
        # This transaction failed, but a concurrent ingest may have committed a
        # Document row pointing at the same content-addressed file. Only remove
        # the file when no committed document references that digest.
        still_referenced = False
        if created_vault_file:
            bind = session.get_bind()
            engine = getattr(bind, "engine", bind)
            try:
                with Session(engine) as fresh:
                    still_referenced = fresh.exec(
                        select(Document).where(Document.file_sha256 == digest)
                    ).first() is not None
            except Exception:
                # Unsure means keep: a stale file is recoverable, a deleted live
                # document is not.
                still_referenced = True
            if not still_referenced:
                try:
                    stored_path.unlink(missing_ok=True)
                except OSError:
                    pass
        orphan_path = stored_path if stored_path.exists() else None
        raise IngestionPersistenceError(str(exc), orphan_path) from exc
    return document, extraction_id


def reprocess_document(
    session: Session, document: Document, *, commit: bool = False
) -> DocumentExtraction:
    """Re-run the current parsers over a document already in the vault.

    Parser fixes otherwise only help files uploaded after the fix. This keeps the
    original document and extraction history, and adds a new extraction for the
    review queue. Imported statements are refused: their authoritative version
    is already recorded, and a second extraction would compete with it.
    """
    if not document.storage_path:
        raise ValueError("The document has no stored file to reprocess.")
    if session.exec(
        select(DocumentExtraction).where(
            DocumentExtraction.document_id == document.id,
            DocumentExtraction.imported_statement_id.is_not(None),
        )
    ).first():
        raise ValueError("This document has already been imported; reprocessing would create a competing extraction.")

    path = Path(document.storage_path)
    if not path.is_file():
        raise ValueError("The stored file for this document is missing from the vault.")
    if path.suffix.lower() != ".pdf":
        raise ValueError("Only PDF statements can be reprocessed.")

    extraction = extract_and_resolve(path, session)
    if extraction is None:
        raise ValueError("No supported statement format was recognized in this PDF.")

    record = DocumentExtraction(
        id=str(uuid4()),
        document_id=document.id,
        parser_name=extraction.parser_name,
        parser_version=PARSER_VERSION,
        validation_status=extraction.status,
        approval_status="PENDING",
        extracted_data_json=json.dumps(extraction.to_dict(), sort_keys=True),
    )
    session.add(record)
    document.document_type = {
        "MORTGAGE": "MORTGAGE_STATEMENT",
        "CHECKING": "CHECKING_STATEMENT",
        "CREDIT_CARD": "CREDIT_CARD_STATEMENT",
    }.get(extraction.statement_type, document.document_type)
    session.add(document)
    session.flush()
    if commit:
        session.commit()
    return record


def reprocess_unrecognized_documents(session: Session, *, commit: bool = True) -> dict:
    """Retry every vaulted document that has no extraction yet.

    This is the repair path for a parser release: onboarding copies documents
    before the statement queue sees them, and a re-upload would be skipped as a
    duplicate SHA-256. The report separates parser misses from real errors so a
    bulk run never hides an unreadable file behind a success count.
    """
    extracted = 0
    unsupported = 0
    errors = 0
    recognized_ids: list[str] = []
    unsupported_ids: list[str] = []
    error_details: list[tuple[str, str]] = []

    documents = session.exec(
        select(Document)
        .where(~Document.id.in_(select(DocumentExtraction.document_id)))
        .order_by(Document.uploaded_at, Document.id)
    ).all()
    for document in documents:
        try:
            record = reprocess_document(session, document, commit=commit)
        except ValueError as exc:
            unsupported += 1
            unsupported_ids.append(document.id)
            error_details.append((document.id, str(exc)))
        except Exception as exc:  # noqa: BLE001 - one document must not stop the batch
            errors += 1
            error_details.append((document.id, str(exc)))
        else:
            extracted += 1
            recognized_ids.append(record.id)

    return {
        "scanned": len(documents),
        "extracted": extracted,
        "unsupported": unsupported,
        "errors": errors,
        "extraction_ids": recognized_ids,
        "unsupported_document_ids": unsupported_ids,
        "error_details": error_details,
    }


def ingest_folder(folder: Path, session: Session, vault_dir: Path, *, commit: bool = False) -> dict:
    """Scan PDFs independently; persist each only in commit mode."""
    files = sorted(path for path in folder.rglob("*") if path.is_file() and path.suffix.lower() == ".pdf")
    report = {
        "scanned": len(files), "duplicates": 0, "supported": 0, "needs_review": 0,
        "unsupported": 0, "imported": 0, "errors": 0, "items": [],
    }
    for path in files:
        try:
            digest = _sha256(path)
            if session.exec(select(Document).where(Document.file_sha256 == digest)).first():
                report["duplicates"] += 1
                report["items"].append({"file": path.name, "status": "DUPLICATE_SKIPPED"})
                continue

            extraction = extract_and_resolve(path, session)

            if extraction is None:
                report["unsupported"] += 1
                item = {"file": path.name, "status": "UNSUPPORTED"}
            else:
                if extraction.status == "VALIDATED":
                    report["supported"] += 1
                else:
                    report["needs_review"] += 1
                item = {
                    "file": path.name,
                    "status": extraction.status,
                    "kind": extraction.statement_type,
                    "reasons": extraction.review_reasons,
                    "extraction": extraction.to_dict(),
                }

            if commit:
                document, extraction_id = _persist_document(path, digest, vault_dir, session, extraction)
                item["document_id"] = document.id
                if extraction_id:
                    item["extraction_id"] = extraction_id
                report["imported"] += 1
            report["items"].append(item)
        except (OSError, SQLAlchemyError, TypeError, ValueError, RuntimeError) as exc:
            report["errors"] += 1
            item = {"file": path.name, "status": "ERROR", "error": str(exc)}
            if isinstance(exc, IngestionPersistenceError) and exc.orphaned_vault_file:
                item["orphaned_vault_file"] = str(exc.orphaned_vault_file)
            report["items"].append(item)
    return report


#: One upload's limit, shared by every screen that accepts a file.
MAX_UPLOAD_BYTES = 50 * 1024 * 1024

#: What the vault will accept, and the media type each suffix is served as.
VAULT_SUFFIX_TYPES = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}


@dataclass
class StoredDocument:
    """The outcome of putting one uploaded file into the vault."""

    document: Document
    created: bool
    extraction_id: str | None = None
    note: str | None = None


def store_document(
    session: Session,
    vault_dir: Path,
    *,
    payload: bytes,
    original_filename: str,
    document_type: str | None = None,
    property_id: str | None = None,
    parse: bool = True,
) -> StoredDocument:
    """Put an uploaded file in this portfolio's vault, and record it.

    Unlike the statement path, this needs no parser to succeed: a lease, an insurance
    policy or a photograph of a receipt has no extraction, and the vault is where it
    belongs either way. Content already stored is recognised by its SHA-256 and its
    existing row comes back with ``created=False`` rather than being written twice.

    A PDF a parser recognises is staged for review, so an upload from the vault and an
    upload from the statements screen behave the same way.
    """
    name = Path(original_filename or "document").name
    suffix = Path(name).suffix.lower()
    if suffix not in VAULT_SUFFIX_TYPES:
        raise ValueError(
            f"{name}: only PDF and image documents can be stored "
            f"({', '.join(sorted(VAULT_SUFFIX_TYPES))})."
        )
    if not payload:
        raise ValueError(f"{name}: the file is empty.")

    digest = hashlib.sha256(payload).hexdigest()
    existing = session.exec(select(Document).where(Document.file_sha256 == digest)).first()
    if existing is not None:
        return StoredDocument(document=existing, created=False, note="already in the vault")

    vault_dir.mkdir(parents=True, exist_ok=True)
    staging = vault_dir / f".upload-{uuid4().hex}{suffix}"
    staging.write_bytes(payload)
    try:
        extraction: MortgageExtraction | BankStatementExtraction | None = None
        note: str | None = None
        if parse and suffix == ".pdf":
            try:
                extraction = extract_and_resolve(staging, session)
            except Exception as exc:  # noqa: BLE001 - an unreadable PDF is the uploader's file
                note = f"stored without extraction: {exc}"
        document, extraction_id = _persist_document(
            staging,
            digest,
            vault_dir,
            session,
            extraction,
            original_filename=name,
            document_type=document_type,
            property_id=property_id,
            suffix=suffix,
        )
    finally:
        staging.unlink(missing_ok=True)
    return StoredDocument(document=document, created=True, extraction_id=extraction_id, note=note)


def update_extraction(session: Session, extraction_id: str, updates: dict, *, commit: bool = False) -> DocumentExtraction:
    """Update extracted fields on an unimported extraction and re-evaluate validation status."""
    record = session.get(DocumentExtraction, extraction_id)
    if record is None:
        raise ValueError(f"Unknown extraction: {extraction_id}")
    if record.imported_statement_id:
        raise ValueError("Cannot update an already-imported extraction.")

    data = json.loads(record.extracted_data_json)
    for key, value in updates.items():
        data[key] = value

    # Editing a value invalidates any prior human approval of the old values.
    record.approval_status = "PENDING"
    record.approved_at = None

    # If field ambiguities are resolved by user updates, clear them. Stored labels
    # use spaces ("total due") while updates use columns ("total_due_cents").
    field_ambiguities = data.get("field_ambiguities", [])
    if field_ambiguities:
        update_keys = {
            key.replace(" ", "_").removesuffix("_cents") for key in updates
        }
        resolved = [
            f for f in field_ambiguities
            if f.replace(" ", "_").removesuffix("_cents") not in update_keys
        ]
        data["field_ambiguities"] = resolved

    if data.get("statement_type") in BANK_STATEMENT_TYPES:
        updated = _bank_extraction_from_data(record, data)
    else:
        updated = MortgageExtraction(
            parser_name=record.parser_name,
            statement_type=data.get("statement_type", "MORTGAGE"),
            servicer_name=data.get("servicer_name"),
            loan_number=data.get("loan_number"),
            account_last4=data.get("account_last4"),
            statement_date=date.fromisoformat(data["statement_date"]) if data.get("statement_date") else None,
            payment_due_date=date.fromisoformat(data["payment_due_date"]) if data.get("payment_due_date") else None,
            principal_cents=data.get("principal_cents"),
            interest_cents=data.get("interest_cents"),
            escrow_cents=data.get("escrow_cents"),
            fees_cents=data.get("fees_cents"),
            total_due_cents=data.get("total_due_cents"),
            outstanding_principal_cents=data.get("outstanding_principal_cents"),
            reported_interest_rate_milli_percent=data.get("reported_interest_rate_milli_percent"),
            escrow_balance_cents=data.get("escrow_balance_cents"),
            field_ambiguities=data.get("field_ambiguities", []),
        )
        updated = validate_mortgage_extraction(updated)

    # Corrected identity (or a chosen account) is matched against this
    # portfolio's own accounts again, so a correction can clear a review reason
    # and a stale match never survives a change of account.
    updated = resolve_statement_extraction(session, updated)

    record.validation_status = updated.status
    if updated.status != "VALIDATED":
        record.approval_status = "PENDING"
        record.approved_at = None

    record.extracted_data_json = json.dumps(updated.to_dict(), sort_keys=True)
    session.add(record)
    session.flush()
    if commit:
        session.commit()
    return record


def _bank_transaction_from_data(item: dict) -> BankTransaction:
    return BankTransaction(
        transaction_date=date.fromisoformat(item["transaction_date"]),
        posting_date=date.fromisoformat(item["posting_date"]) if item.get("posting_date") else None,
        amount_cents=item["amount_cents"],
        direction=item["direction"],
        raw_description=item["raw_description"],
        reference_number=item.get("reference_number"),
        requires_client_confirmation=item.get("requires_client_confirmation", False),
        requires_cpa_review=item.get("requires_cpa_review", False),
    )


def _bank_extraction_from_data(record: DocumentExtraction, data: dict) -> BankStatementExtraction:
    """Rebuilds a bank extraction from stored JSON so it can be re-resolved."""
    return BankStatementExtraction(
        parser_name=record.parser_name,
        statement_type=data.get("statement_type", "CHECKING"),
        institution_name=data.get("institution_name"),
        account_last4=data.get("account_last4"),
        statement_date=date.fromisoformat(data["statement_date"]),
        period_start_date=date.fromisoformat(data["period_start_date"]) if data.get("period_start_date") else None,
        period_end_date=date.fromisoformat(data["period_end_date"]) if data.get("period_end_date") else None,
        opening_balance_cents=data.get("opening_balance_cents"),
        closing_balance_cents=data.get("closing_balance_cents"),
        institution_id=data.get("institution_id"),
        financial_account_id=data.get("financial_account_id"),
        transactions=[_bank_transaction_from_data(item) for item in data.get("transactions", [])],
        status=data.get("status", "EXTRACTED"),
        review_reasons=list(data.get("review_reasons", [])),
    )


def approve_extraction(session: Session, extraction_id: str, *, commit: bool = False) -> DocumentExtraction:
    """Record explicit human approval for a structurally valid extraction."""
    extraction = session.get(DocumentExtraction, extraction_id)
    if extraction is None:
        raise ValueError(f"Unknown extraction: {extraction_id}")
    if extraction.validation_status != "VALIDATED":
        raise ValueError("Only VALIDATED extractions can be approved.")
    if extraction.approval_status != "APPROVED":
        extraction.approval_status = "APPROVED"
        extraction.approved_at = utc_now()
        session.add(extraction)
    session.flush()
    if commit:
        session.commit()
    return extraction


def _prepare_bank_entities(session: Session, data: dict) -> None:
    """Ensures the institution and account an approved bank statement belongs to.

    The account is normally already registered, having been matched when the
    statement was extracted. When the extraction instead carries only the
    identity the document printed -- institution plus last four digits -- the
    account is registered from that identity, which is the one case where a
    statement creates an account rather than attaching to one.
    """
    institution = session.get(FinancialInstitution, data.get("institution_id")) if data.get("institution_id") else None
    account = session.get(FinancialAccount, data.get("financial_account_id")) if data.get("financial_account_id") else None
    if institution is None or account is None:
        account = register_observed_account(session, data)
    if account.institution_id != data.get("institution_id"):
        raise ValueError("The extraction's account belongs to a different institution.")
    session.flush()


def _resolve_counter_account(session: Session, description: str, own_account_id: str) -> str | None:
    """The portfolio account a transfer's description names, if exactly one does.

    "Online payment from CHK 9456" and "ROCKET MORTGAGE" each name another of
    this portfolio's own accounts. Exactly one match links the two; no match, or
    several, leaves the transaction unlinked rather than guessing.

    A bare institution mention is not a transfer: "BANK OF AMERICA ATM
    WITHDRAWAL" must remain an expense, not a card settlement.
    """
    lowered = (description or "").lower()
    if not lowered:
        return None
    if not re.search(r"\b(payment|autopay|pymt|transfer|paid|pay)\b", lowered):
        return None
    matches: list[str] = []
    for account in session.exec(select(FinancialAccount)).all():
        if account.id == own_account_id:
            continue
        last4 = _digits(account.account_number_masked)
        if last4 and re.search(rf"{_ACCOUNT_MENTION_PREFIX}{re.escape(last4)}(?!\d)", lowered):
            matches.append(account.id)
            continue
        if account.account_type != "CREDIT_CARD":
            # An institution-name mention only identifies a card payment; other
            # account types need their account number to be named explicitly.
            continue
        institution = session.get(FinancialInstitution, account.institution_id)
        name = (institution.name if institution else "").lower()
        if len(name) >= 4 and name in lowered:
            matches.append(account.id)
    unique = list(dict.fromkeys(matches))
    return unique[0] if len(unique) == 1 else None


def _import_bank_statement(session: Session, record: DocumentExtraction, data: dict, *, commit: bool = False) -> Statement:
    statement_date = date.fromisoformat(data["statement_date"])
    period_start_date = date.fromisoformat(data["period_start_date"]) if data.get("period_start_date") else None
    period_end_date = date.fromisoformat(data["period_end_date"]) if data.get("period_end_date") else statement_date
    account_id = data["financial_account_id"]

    existing_statement = session.exec(
        select(Statement).where(
            Statement.financial_account_id == account_id,
            Statement.statement_date == statement_date,
        )
    ).first()
    if existing_statement:
        if existing_statement.document_id != record.document_id:
            # Same account and statement date but different bytes: this is a
            # corrected or duplicate statement, and its transactions must not be
            # silently discarded by claiming the extraction was imported.
            raise ValueError(
                f"A different statement for {account_id} dated {statement_date} is "
                "already imported. Review or replace that statement explicitly."
            )
        record.imported_statement_id = existing_statement.id
        session.add(record)
        if commit:
            session.commit()
        return existing_statement

    _prepare_bank_entities(session, data)

    statement = Statement(
        id=str(uuid4()),
        document_id=record.document_id,
        financial_account_id=account_id,
        statement_type=data["statement_type"],
        active_parser_name=record.parser_name,
        active_parser_version=record.parser_version,
        statement_date=statement_date,
        period_start_date=period_start_date,
        period_end_date=period_end_date,
        opening_balance_cents=data.get("opening_balance_cents"),
        closing_balance_cents=data.get("closing_balance_cents"),
        ingestion_status="COMMITTED",
    )
    session.add(statement)
    session.flush()

    session.add(ExtractionVersion(
        id=str(uuid4()),
        statement_id=statement.id,
        parser_name=record.parser_name,
        parser_version=record.parser_version,
        extracted_data_json=record.extracted_data_json,
        is_active=True,
    ))

    # Persist transactions
    for idx, tx_data in enumerate(data.get("transactions", [])):
        tx_id = f"tx_{statement.id[:8]}_{idx:04d}"
        tx_date = date.fromisoformat(tx_data["transaction_date"])
        post_date = date.fromisoformat(tx_data["posting_date"]) if tx_data.get("posting_date") else tx_date
        counter_acct = _resolve_counter_account(session, tx_data.get("raw_description", ""), account_id)

        tx = Transaction(
            id=tx_id,
            statement_id=statement.id,
            financial_account_id=account_id,
            transaction_date=tx_date,
            posting_date=post_date,
            amount_cents=tx_data["amount_cents"],
            direction=tx_data["direction"],
            raw_description=tx_data["raw_description"],
            reference_number=tx_data.get("reference_number"),
            counter_account_id=counter_acct,
            requires_client_confirmation=tx_data.get("requires_client_confirmation", False),
            requires_cpa_review=tx_data.get("requires_cpa_review", False),
        )
        session.add(tx)
        session.flush()

        state = TransactionProcessingState(
            transaction_id=tx.id,
            allocation_status="UNALLOCATED",
            allocated_cents=0,
            residual_cents=abs(tx.amount_cents),
            posting_status="UNPOSTED",
        )
        session.add(state)

    record.imported_statement_id = statement.id
    session.add(record)
    session.flush()
    if commit:
        session.commit()
    return statement


def import_approved_extraction(session: Session, extraction_id: str, *, commit: bool = False) -> Statement:
    """Import an approved mortgage or banking extraction as authoritative, without posting a journal entry."""
    record = session.get(DocumentExtraction, extraction_id)
    if record is None:
        raise ValueError(f"Unknown extraction: {extraction_id}")
    if record.validation_status != "VALIDATED" or record.approval_status != "APPROVED":
        raise ValueError("Only explicitly approved VALIDATED extractions can be imported.")
    if record.imported_statement_id:
        statement = session.get(Statement, record.imported_statement_id)
        if statement is None:
            raise RuntimeError("Extraction references a missing imported statement.")
        return statement

    data = json.loads(record.extracted_data_json)
    if data.get("statement_type") in ("CHECKING", "CREDIT_CARD"):
        return _import_bank_statement(session, record, data, commit=commit)

    statement_date = date.fromisoformat(data["statement_date"])
    payment_due_date = date.fromisoformat(data["payment_due_date"])
    loan_account_id = data["loan_account_id"]
    rate = data["reported_interest_rate_milli_percent"]
    if not all(data.get(key) is not None for key in (
        "financial_account_id", "loan_account_id", "principal_cents", "interest_cents",
        "escrow_cents", "total_due_cents", "outstanding_principal_cents",
        "reported_interest_rate_milli_percent",
    )):
        raise ValueError("Validated extraction is missing a required mortgage field.")

    existing_period = session.exec(
        select(MortgagePeriod).where(
            MortgagePeriod.loan_account_id == loan_account_id,
            MortgagePeriod.statement_date == statement_date,
        )
    ).first()
    if existing_period:
        raise ValueError("A mortgage period already exists for this loan and statement date.")

    _prepare_entities(session, data)
    statement = Statement(
        id=str(uuid4()), document_id=record.document_id,
        financial_account_id=data["financial_account_id"], statement_type=data["statement_type"],
        active_parser_name=record.parser_name, active_parser_version=record.parser_version,
        statement_date=statement_date, ingestion_status="COMMITTED",
    )
    session.add(statement)
    session.flush()
    session.add(ExtractionVersion(
        id=str(uuid4()), statement_id=statement.id, parser_name=record.parser_name,
        parser_version=record.parser_version,
        extracted_data_json=record.extracted_data_json, is_active=True,
    ))
    session.add(MortgagePeriod(
        id=str(uuid4()), statement_id=statement.id, loan_account_id=loan_account_id,
        payment_due_date=payment_due_date, statement_date=statement_date,
        principal_billed_cents=data["principal_cents"], interest_billed_cents=data["interest_cents"],
        escrow_billed_cents=data["escrow_cents"], fees_billed_cents=data.get("fees_cents") or 0,
        total_amount_billed_cents=data["total_due_cents"],
        outstanding_principal_cents=data["outstanding_principal_cents"],
        interest_rate_bps=rate, reported_escrow_balance_cents=data.get("escrow_balance_cents"),
    ))
    loan = session.get(LoanAccount, loan_account_id)
    latest_period = session.exec(
        select(MortgagePeriod)
        .where(MortgagePeriod.loan_account_id == loan_account_id)
        .order_by(desc(MortgagePeriod.statement_date))
    ).first()
    if loan and latest_period and latest_period.statement_date == statement_date:
        loan.current_interest_rate_bps = rate
        session.add(loan)
    record.imported_statement_id = statement.id
    session.add(record)
    session.flush()
    if commit:
        session.commit()
    return statement


def initialize_database(engine) -> None:
    init_schema(engine)


# --- Document Intelligence sidecar -----------------------------------------
#
# The sidecar (``document-intelligence-lab``) reads a PDF and returns structured
# fields plus a ``proptally_payload`` of proposed rows. It is an *extractor*, not
# a writer: nothing here trusts its account ids. Its payload is converted into the
# same extraction dictionary the local parsers produce, resolved against this
# portfolio's own accounts, and staged as a ``DocumentExtraction`` -- the existing
# review queue. Only ``approve_extraction`` + ``import_approved_extraction``, both
# driven by a person, ever create a ``Statement``.

#: Where the sidecar daemon is expected to listen. Override with
#: ``PROPTALLY_DOC_INTEL_URL`` when it runs elsewhere.
DOC_INTEL_DEFAULT_URL = "http://127.0.0.1:8765"

#: Set to ``1``/``true``/``yes`` to extract uploads with the sidecar. Off by
#: default: it runs a local language model and takes tens of seconds per file.
DOC_INTEL_ENV_FLAG = "PROPTALLY_DOC_INTEL"

#: ``DocumentExtraction.parser_name`` recorded for every sidecar extraction, and
#: the value the admin console counts to report what the extractor has staged.
DOC_INTEL_PARSER_NAME = "document_intelligence_lab"

#: Sidecar profile -> the statement type this portfolio records for it.
DOC_INTEL_PROFILES = {
    "bank_statement": "CHECKING",
    "credit_card_statement": "CREDIT_CARD",
    "mortgage_statement": "MORTGAGE",
}

#: Statement type -> the sidecar profile that extracts it, for an already
#: classified document (see ``DOC_INTEL_PROFILES`` for the reverse).
DOC_INTEL_PROFILE_FOR_TYPE = {value: key for key, value in DOC_INTEL_PROFILES.items()}

_DOC_INTEL_DOCUMENT_TYPES = {
    "CHECKING": "CHECKING_STATEMENT",
    "CREDIT_CARD": "CREDIT_CARD_STATEMENT",
    "MORTGAGE": "MORTGAGE_STATEMENT",
}

#: The fields ``import_approved_extraction`` needs before a mortgage statement can
#: be imported. The sidecar returns an amount due and a principal balance but not
#: the payment's principal/interest/escrow split, so a mortgage extraction is
#: staged for review and completed by a person.
_MORTGAGE_REVIEW_FIELDS = (
    ("principal_cents", "principal billed"),
    ("interest_cents", "interest billed"),
    ("escrow_cents", "escrow billed"),
    ("total_due_cents", "total amount due"),
    ("outstanding_principal_cents", "outstanding principal"),
    ("reported_interest_rate_milli_percent", "interest rate"),
)


class DocIntelStagingError(RuntimeError):
    """The sidecar returned a job this portfolio cannot stage as an extraction."""


@dataclass
class DocIntelStaged:
    """The outcome of staging one completed sidecar job for review."""

    job: DocIntelJob
    document: Document
    extraction: DocumentExtraction
    created: bool
    data: dict


def _iso_date(value: object) -> str | None:
    """An ISO date string, from either ISO or a US-style printed date."""
    if value in (None, ""):
        return None
    text = str(value).strip()
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        pass
    for pattern in ("%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(text, pattern).date().isoformat()
        except ValueError:
            continue
    return None


def _cents(value: object) -> int | None:
    """Integer cents from the sidecar's normalized money value, or None."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None


def _field_value(val: object) -> object:
    """Unwraps a FieldPrediction dictionary if present, returning its normalized or raw value."""
    if isinstance(val, dict) and ("normalized_value" in val or "raw_value" in val):
        nv = val.get("normalized_value")
        return nv if nv is not None else val.get("raw_value")
    return val


def doc_intel_extraction_data(profile_name: str, fields: dict, payload: dict) -> dict:
    """Convert a sidecar payload into this portfolio's extraction dictionary.

    The result has the same shape ``BankStatementExtraction.to_dict()`` and
    ``MortgageExtraction.to_dict()`` produce, so the review screen, the correction
    form and the import path treat a sidecar extraction exactly like a parsed one.
    Identity fields are left unresolved: ``resolve_extraction_data`` attaches them
    to accounts this portfolio already holds.
    """
    fields = {k: _field_value(v) for k, v in (fields or {}).items()}
    profile = (profile_name or "").strip().lower()
    if profile not in DOC_INTEL_PROFILES:
        raise DocIntelStagingError(
            f"Unsupported extraction profile {profile_name!r}; expected one of "
            f"{', '.join(sorted(DOC_INTEL_PROFILES))}."
        )
    statement = payload.get("statement") or {}
    account = payload.get("financial_account") or {}
    metadata = payload.get("metadata") or {}
    statement_type = DOC_INTEL_PROFILES[profile]

    statement_date = _iso_date(
        statement.get("statement_date") or fields.get("statement_date")
        or statement.get("period_end_date") or fields.get("statement_period_end")
    )
    if statement_date is None:
        raise DocIntelStagingError(
            "The extraction returned no usable statement date; the document cannot be "
            "staged against a period."
        )

    masked = fields.get("account_number_masked") or account.get("account_number_masked")
    institution_name = str(fields.get("institution_name") or "").strip() or None
    servicer_name = str(
        fields.get("servicer_name") or fields.get("lender") or fields.get("institution_name") or ""
    ).strip() or None

    data: dict = {
        "statement_type": statement_type,
        "statement_date": statement_date,
        "period_start_date": _iso_date(
            statement.get("period_start_date") or fields.get("statement_period_start")
        ),
        "period_end_date": _iso_date(
            statement.get("period_end_date") or fields.get("statement_period_end")
        ),
        "opening_balance_cents": _cents(statement.get("opening_balance_cents")),
        "closing_balance_cents": _cents(statement.get("closing_balance_cents")),
        "institution_name": institution_name,
        "servicer_name": servicer_name,
        "account_last4": _digits(masked) or None,
        "institution_id": None,
        "financial_account_id": None,
        "transactions": [],
        "field_ambiguities": [],
        "review_reasons": [],
        "resolution_reasons": [],
    }

    if statement_type == "MORTGAGE":
        data.update({
            "loan_number": _digits(fields.get("loan_number")) or None,
            "loan_account_id": None,
            "property_id": None,
            "principal_cents": _cents(fields.get("principal_cents")),
            "interest_cents": _cents(fields.get("interest_cents")),
            "escrow_cents": _cents(fields.get("escrow_cents")),
            "fees_cents": _cents(fields.get("fees_cents")),
            "total_due_cents": _cents(
                fields.get("total_due_cents") or fields.get("emi_amount_cents")
                or metadata.get("total_amount_due_cents")
            ),
            "outstanding_principal_cents": _cents(fields.get("outstanding_principal_cents")),
            "reported_interest_rate_milli_percent": _cents(
                fields.get("interest_rate_bps") or metadata.get("rate_bps")
            ),
            "escrow_balance_cents": _cents(
                fields.get("escrow_balance_cents") or metadata.get("escrow_balance_cents")
            ),
            "payment_due_date": _iso_date(fields.get("payment_due_date")),
        })
        if data["account_last4"] is None:
            data["account_last4"] = _digits(fields.get("loan_number")) or None

    return data


def _doc_intel_review_reasons(data: dict, result: dict) -> list[str]:
    """Everything that keeps an extraction out of VALIDATED, in review-screen words."""
    reasons: list[str] = []
    for issue in result.get("cross_field_issues") or []:
        message = str(issue.get("message") or issue.get("reason") or "").strip()
        if message:
            reasons.append(message)
    if data["statement_type"] == "MORTGAGE":
        for key, label in _MORTGAGE_REVIEW_FIELDS:
            if data.get(key) is None:
                reasons.append(f"The statement does not state its {label}; enter it to import.")
    return reasons


logger = logging.getLogger("proptally.ingestion.doc_intel")


class StatementIngestionService:
    """Submits statements to the Document Intelligence sidecar and stages the result.

    ``submit`` returns a job immediately; ``extract`` waits for one; ``stage`` turns
    a completed job into a reviewable ``DocumentExtraction``. Staging never writes a
    ``Statement``, so no ingested document can move a balance without a person
    approving and importing it.
    """

    def __init__(
        self,
        base_url: str | None = None,
        client: DocumentIntelligenceClient | None = None,
    ):
        self.base_url = base_url or os.environ.get("PROPTALLY_DOC_INTEL_URL", DOC_INTEL_DEFAULT_URL)
        self._client = client

    @property
    def client(self) -> DocumentIntelligenceClient:
        if self._client is None:
            self._client = DocumentIntelligenceClient(base_url=self.base_url)
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()

    def __enter__(self) -> StatementIngestionService:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def healthy(self) -> bool:
        """Whether the sidecar answers its own health endpoint."""
        return bool(self.client.health().get("status") in ("ok", "healthy"))

    def available(self, timeout: float = 2.0) -> bool:
        """Whether the sidecar can be reached right now, without waiting out a call.

        A caller that has another way to extract uses this to choose between engines;
        a refused connection, a timeout or an unhealthy body all mean "not available"
        rather than an error.
        """
        try:
            return bool(self.client.health(timeout=timeout).get("status") in ("ok", "healthy"))
        except Exception:  # noqa: BLE001 - an unreachable sidecar is a fallback, not a fault
            return False

    def submit(
        self,
        *,
        payload: bytes,
        original_filename: str,
        profile_name: str,
        property_id: str = "",
    ) -> DocIntelJob:
        """Queue an extraction and return at once, with the job the daemon assigned."""
        return self.client.submit_extraction(
            file=payload,
            filename=original_filename,
            profile_name=profile_name,
            property_id=property_id,
            map_to_proptally=True,
        )

    def extract(
        self,
        *,
        payload: bytes,
        original_filename: str,
        profile_name: str,
        property_id: str = "",
        timeout: float = 180.0,
        on_progress=None,
    ) -> DocIntelJob:
        """Queue an extraction and wait for the completed job."""
        job = self.submit(
            payload=payload,
            original_filename=original_filename,
            profile_name=profile_name,
            property_id=property_id,
        )
        return self.client.wait_for_job(job.job_id, timeout=timeout, on_progress=on_progress)

    def stage(
        self,
        session: Session,
        vault_dir: Path,
        *,
        job: DocIntelJob,
        payload: bytes,
        original_filename: str,
        profile_name: str,
        commit: bool = False,
    ) -> DocIntelStaged:
        """Store the file and stage its extraction for review.

        The bytes go into this portfolio's vault under their SHA-256, so submitting
        the same statement twice returns the extraction already staged for it
        instead of creating a second one. Nothing is written to the ledger.
        """
        if not job.is_success or not job.result:
            raise DocIntelStagingError(
                f"Extraction job {job.job_id} did not complete successfully "
                f"({job.status}): {job.error_message or 'no detail returned'}."
            )
        result = job.result
        proptally_payload = result.get("proptally_payload")
        if not isinstance(proptally_payload, dict):
            raise DocIntelStagingError(
                "The extraction job returned no proptally_payload; submit it with "
                "map_to_proptally=True."
            )

        data = doc_intel_extraction_data(profile_name, result.get("fields") or {}, proptally_payload)
        review_reasons = _doc_intel_review_reasons(data, result)
        resolution_reasons = list(resolve_extraction_data(session, data))
        data["review_reasons"] = review_reasons
        data["resolution_reasons"] = resolution_reasons
        data["status"] = "NEEDS_REVIEW" if (review_reasons or resolution_reasons) else "VALIDATED"
        data["source_document_id"] = result.get("document_id")
        data["source_job_id"] = job.job_id

        stored = store_document(
            session,
            vault_dir,
            payload=payload,
            original_filename=original_filename,
            document_type=_DOC_INTEL_DOCUMENT_TYPES[data["statement_type"]],
            property_id=data.get("property_id"),
            parse=False,
        )
        statement = proptally_payload.get("statement") or {}
        parser_name = str(statement.get("active_parser_name") or DOC_INTEL_PARSER_NAME)
        existing = session.exec(
            select(DocumentExtraction)
            .where(DocumentExtraction.document_id == stored.document.id)
            .where(DocumentExtraction.parser_name == parser_name)
            .order_by(desc(DocumentExtraction.created_at))
        ).first()
        if existing is not None:
            logger.info(
                "DocIntel job %s already staged as extraction %s (document %s)",
                job.job_id, existing.id, stored.document.id,
            )
            return DocIntelStaged(
                job=job, document=stored.document, extraction=existing, created=False, data=data
            )

        extraction = DocumentExtraction(
            id=str(uuid4()),
            document_id=stored.document.id,
            parser_name=parser_name,
            parser_version=str(statement.get("active_parser_version") or "unknown"),
            validation_status=data["status"],
            approval_status="PENDING",
            extracted_data_json=json.dumps(data, sort_keys=True),
        )
        session.add(extraction)
        session.flush()
        if commit:
            session.commit()
        logger.info(
            "Staged DocIntel job %s as extraction %s (status: %s, file: %s)",
            job.job_id, extraction.id, data["status"], original_filename,
        )
        return DocIntelStaged(
            job=job, document=stored.document, extraction=extraction, created=True, data=data
        )

    def extract_and_stage(
        self,
        session: Session,
        vault_dir: Path,
        *,
        payload: bytes,
        original_filename: str,
        profile_name: str,
        property_id: str = "",
        timeout: float = 180.0,
        on_progress=None,
        commit: bool = False,
    ) -> DocIntelStaged:
        """Submit, wait, and stage in one call. The file is stored only on success."""
        logger.info(
            "Extracting and staging '%s' (%s bytes) via Document Intelligence (profile: %s)",
            original_filename, len(payload), profile_name,
        )
        job = self.extract(
            payload=payload,
            original_filename=original_filename,
            profile_name=profile_name,
            property_id=property_id,
            timeout=timeout,
            on_progress=on_progress,
        )
        return self.stage(
            session,
            vault_dir,
            job=job,
            payload=payload,
            original_filename=original_filename,
            profile_name=profile_name,
            commit=commit,
        )
