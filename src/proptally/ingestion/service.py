"""Idempotent, local-only ingestion of statements into the PropTally vault."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from datetime import date
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
from proptally.ingestion.bank_parsers import (
    BankStatementExtraction,
    extract_bank_or_card_pdf,
)
from proptally.ingestion.parsers import (
    PARSER_VERSION,
    MortgageExtraction,
    extract_pdf,
    validate_mortgage_extraction,
)

PROPERTY_DEFAULTS = {
    "prop_tinsley_huntley": ("12307 Larkspur St", "12307 Larkspur St", "Westbrook", "60196", "RENTAL_INVESTMENT"),
    "prop_cimmaron_pingree": ("2718 Foxglove Ct", "2718 Foxglove Ct", "Oakhaven", "60199", "RENTAL_INVESTMENT"),
    "prop_burkey_algonquin": ("3345 Elderberry Dr", "3345 Elderberry Dr", "Cedar Grove", "60198", "PRIMARY_RESIDENCE"),
}
LOANS = {
    "loan_rocket_1000000001": ("Rocket Mortgage", "acct_rocket_5035", "0700000001", "prop_tinsley_huntley"),
    "loan_pnc_1000000002": ("PNC Mortgage", "acct_pnc_0708", "0700000002", "prop_cimmaron_pingree"),
    "loan_ccm_1000000003": ("CrossCountry Mortgage", "acct_ccm_9359", "0700000003", "prop_burkey_algonquin"),
}


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


def _prepare_entities(session: Session, data: dict) -> None:
    loan_id = data["loan_account_id"]
    account_id = data["financial_account_id"]
    property_id = data["property_id"]
    loan_details = LOANS.get(loan_id)
    if loan_details is None:
        raise ValueError(f"Unsupported loan identifier in extraction: {loan_id}")
    institution_name, expected_account_id, loan_number, expected_property_id = loan_details
    if account_id != expected_account_id or property_id != expected_property_id:
        raise ValueError("Extraction entity identifiers do not match the identified loan.")

    property_defaults = PROPERTY_DEFAULTS.get(property_id)
    if property_defaults is None:
        raise ValueError(f"Unsupported property identifier in extraction: {property_id}")
    property_record = session.get(Property, property_id)
    if property_record is None:
        name, address, city, zip_code, property_type = property_defaults
        session.add(Property(
            id=property_id, name=name, address_line1=address, city=city,
            state="IL", zip_code=zip_code, property_type=property_type,
        ))

    institution_id = {
        "Rocket Mortgage": "inst_rocket",
        "PNC Mortgage": "inst_pnc",
        "CrossCountry Mortgage": "inst_ccm",
    }[institution_name]
    institution = session.get(FinancialInstitution, institution_id)
    if institution is None:
        session.add(FinancialInstitution(id=institution_id, name=institution_name))

    account = session.get(FinancialAccount, account_id)
    if account is None:
        session.add(FinancialAccount(
            id=account_id, institution_id=institution_id,
            account_name=institution_name, account_type="MORTGAGE",
            account_number_masked=f"...{loan_number[-4:]}",
        ))
    elif account.institution_id != institution_id:
        raise ValueError("Existing financial account is linked to a different institution.")

    loan = session.get(LoanAccount, loan_id)
    if loan is None:
        session.add(LoanAccount(
            id=loan_id, financial_account_id=account_id,
            property_id=property_id, loan_number=loan_number,
        ))
    elif loan.financial_account_id != account_id or loan.property_id != property_id:
        raise ValueError("Existing loan is linked to a different account or property.")

    needs_escrow = data.get("escrow_cents") is not None or data.get("escrow_balance_cents") is not None
    escrow = session.exec(
        select(EscrowAccount).where(EscrowAccount.loan_account_id == loan_id)
    ).first()
    if needs_escrow and escrow is None:
        session.add(EscrowAccount(
            id=f"escrow_{loan_id.removeprefix('loan_')}", loan_account_id=loan_id
        ))
    session.flush()


def _copy_to_vault(source: Path, digest: str, vault_dir: Path) -> tuple[Path, bool]:
    stored_path = vault_dir / digest[:2] / f"{digest}.pdf"
    stored_path.parent.mkdir(parents=True, exist_ok=True)
    if stored_path.exists():
        if _sha256(stored_path) != digest:
            raise OSError(f"Vault file exists with unexpected content: {stored_path}")
        return stored_path, False

    staging_path = stored_path.with_name(f".{stored_path.name}.{uuid4().hex}.tmp")
    created = False
    try:
        shutil.copy2(source, staging_path)
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
) -> tuple[Document, str | None]:
    stored_path, created_vault_file = _copy_to_vault(path, digest, vault_dir)
    document = Document(
        id=str(uuid4()), file_sha256=digest, original_filename=path.name,
        storage_path=str(stored_path), file_size_bytes=path.stat().st_size,
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
        if created_vault_file:
            try:
                stored_path.unlink(missing_ok=True)
            except OSError:
                pass
        orphan_path = stored_path if stored_path.exists() else None
        raise IngestionPersistenceError(str(exc), orphan_path) from exc
    return document, extraction_id


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

            extraction = extract_pdf(path)
            if extraction is None:
                extraction = extract_bank_or_card_pdf(path)

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

    # If field ambiguities are resolved by user updates, clear them
    field_ambiguities = data.get("field_ambiguities", [])
    if field_ambiguities:
        resolved = [f for f in field_ambiguities if f not in updates and f"{f}_cents" not in updates]
        data["field_ambiguities"] = resolved

    stmt_date = date.fromisoformat(data["statement_date"]) if data.get("statement_date") else None
    due_date = date.fromisoformat(data["payment_due_date"]) if data.get("payment_due_date") else None

    extraction = MortgageExtraction(
        parser_name=record.parser_name,
        statement_type=data.get("statement_type", "MORTGAGE"),
        financial_account_id=data.get("financial_account_id"),
        loan_account_id=data.get("loan_account_id"),
        property_id=data.get("property_id"),
        statement_date=stmt_date,
        payment_due_date=due_date,
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
    validated = validate_mortgage_extraction(extraction)

    record.validation_status = validated.status
    if validated.status != "VALIDATED":
        record.approval_status = "PENDING"
        record.approved_at = None

    record.extracted_data_json = json.dumps(validated.to_dict(), sort_keys=True)
    session.add(record)
    session.flush()
    if commit:
        session.commit()
    return record


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
    inst_id = data["institution_id"]
    account_id = data["financial_account_id"]
    statement_type = data["statement_type"]

    inst = session.get(FinancialInstitution, inst_id)
    if inst is None:
        name = "Bank of America" if inst_id == "inst_bofa" else "Apple Card / Goldman Sachs"
        session.add(FinancialInstitution(id=inst_id, name=name))

    acct = session.get(FinancialAccount, account_id)
    if acct is None:
        if account_id == "acct_bofa_chk_9456":
            name = "Bank of America Advantage Plus Checking"
            masked = "...9456"
        elif account_id == "acct_bofa_cc_9306":
            name = "Bank of America Visa Signature"
            masked = "...9306"
        elif account_id == "acct_apple_card":
            name = "Apple Card"
            masked = "...AppleCard"
        else:
            name = account_id
            masked = "...0000"
        session.add(FinancialAccount(
            id=account_id,
            institution_id=inst_id,
            account_name=name,
            account_type=statement_type,
            account_number_masked=masked,
            mixed_use_status="UNCLASSIFIED",
        ))
    session.flush()


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
        counter_acct = tx_data.get("counter_account_id")
        if counter_acct and session.get(FinancialAccount, counter_acct) is None:
            counter_acct = None

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
