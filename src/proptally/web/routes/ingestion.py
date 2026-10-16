"""Statement ingestion screens: upload, review, approve and import.

Everything here works for any portfolio. A statement is matched against the
accounts *this* portfolio has registered (``proptally.ingestion.service``); a
recognised statement for an account that is not registered is kept as
NEEDS_REVIEW with the reason, and the operator resolves it on this screen. No
account number, loan number or institution name is hardcoded anywhere in the
path from upload to imported statement.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import desc
from sqlmodel import Session, select

from proptally.db.models import (
    Document,
    DocumentExtraction,
    FinancialAccount,
    FinancialInstitution,
    LoanAccount,
    Property,
    Statement,
)
from proptally.ingestion.service import (
    BANK_STATEMENT_TYPES,
    MAX_UPLOAD_BYTES,
    _persist_document,
    approve_extraction,
    extract_and_resolve,
    import_approved_extraction,
    register_observed_account,
    update_extraction,
)
from proptally.web.auth import current_context

#: A statement is a PDF; the size limit is shared with the document vault.
PDF_MAGIC = b"%PDF-"

#: Fields an operator may correct. Amounts are entered in dollars and rates in
#: percent, and are stored the way the ledger stores them: cents and
#: thousandths of a percent.
_MONEY_FIELDS = (
    "principal", "interest", "escrow", "fees", "total_due", "outstanding_principal",
    "escrow_balance", "opening_balance", "closing_balance",
)
_DATE_FIELDS = ("statement_date", "payment_due_date", "period_start_date", "period_end_date")
#: What the document itself said about which account it is.
_IDENTITY_FIELDS = ("servicer_name", "loan_number", "account_last4", "institution_name")
#: Which of the portfolio's own records it belongs to.
_CHOICE_FIELDS = ("loan_account_id", "financial_account_id")


def _dollars(cents: int | None) -> str:
    return "" if cents is None else f"{Decimal(cents) / 100:.2f}"


def _percent(milli_percent: int | None) -> str:
    return "" if milli_percent is None else f"{Decimal(milli_percent) / 1000:.3f}"


def _redirect(message: str | None = None) -> RedirectResponse:
    target = "/statements" if not message else f"/statements?message={quote(message)}"
    return RedirectResponse(url=target, status_code=303)


def _updates_from_form(form) -> dict:
    """The corrections an operator submitted, in the units the ledger stores.

    Empty inputs are left out, so submitting the form untouched changes nothing.
    """
    updates: dict = {}
    for field in _MONEY_FIELDS:
        raw = (form.get(field) or "").strip().replace(",", "").replace("$", "")
        if not raw:
            continue
        try:
            updates[f"{field}_cents"] = int((Decimal(raw) * 100).quantize(Decimal(1)))
        except InvalidOperation as exc:
            raise HTTPException(status_code=400, detail=f"{field.replace('_', ' ').title()} must be an amount.") from exc

    raw_rate = (form.get("interest_rate_percent") or "").strip()
    if raw_rate:
        try:
            updates["reported_interest_rate_milli_percent"] = int((Decimal(raw_rate) * 1000).quantize(Decimal(1)))
        except InvalidOperation as exc:
            raise HTTPException(status_code=400, detail="Interest rate must be a percentage.") from exc

    for field in _DATE_FIELDS:
        raw = (form.get(field) or "").strip()
        if not raw:
            continue
        try:
            updates[field] = date.fromisoformat(raw).isoformat()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"{field.replace('_', ' ').title()} must be a date.") from exc

    for field in _IDENTITY_FIELDS + _CHOICE_FIELDS:
        raw = (form.get(field) or "").strip()
        if raw:
            updates[field] = raw
    return updates


def _extraction_row(session: Session, document: Document, extraction: DocumentExtraction | None) -> dict:
    """One document with everything the screen shows about its extraction."""
    data = json.loads(extraction.extracted_data_json) if extraction else {}
    statement = (
        session.get(Statement, extraction.imported_statement_id)
        if extraction and extraction.imported_statement_id
        else None
    )
    account_id = data.get("financial_account_id")
    loan_id = data.get("loan_account_id")
    return {
        "document": document,
        "extraction": extraction,
        "data": data,
        "status": extraction.validation_status if extraction else "UNSUPPORTED",
        "approval_status": extraction.approval_status if extraction else None,
        "reasons": list(data.get("review_reasons") or []) + list(data.get("resolution_reasons") or []),
        "statement": statement,
        "account": session.get(FinancialAccount, account_id) if account_id else None,
        "loan": session.get(LoanAccount, loan_id) if loan_id else None,
        "is_bank": data.get("statement_type") in BANK_STATEMENT_TYPES,
        # Offered only when the statement names its own institution and last four
        # digits but no such account is registered yet: the operator's one-click
        # way to register it without shell access.
        "can_register": (
            extraction is not None
            and not account_id
            and data.get("statement_type") in BANK_STATEMENT_TYPES
            and bool(data.get("institution_name"))
            and len(str(data.get("account_last4") or "")) >= 4
        ),
        "money": {field: _dollars(data.get(f"{field}_cents")) for field in _MONEY_FIELDS},
        "rate_percent": _percent(data.get("reported_interest_rate_milli_percent")),
    }


def get_ingestion_router(templates, get_session_dep):
    router = APIRouter()

    @router.get("/statements", response_class=HTMLResponse)
    def view_statements(
        request: Request,
        message: str | None = Query(None),
        session: Session = Depends(get_session_dep),
    ):
        documents = session.exec(select(Document).order_by(desc(Document.uploaded_at))).all()

        # The most recent extraction of each document is the one on screen; older
        # ones stay in the table as history under the same document.
        latest: dict[str, DocumentExtraction] = {}
        for extraction in session.exec(select(DocumentExtraction)).all():
            current = latest.get(extraction.document_id)
            if current is None or extraction.created_at >= current.created_at:
                latest[extraction.document_id] = extraction

        rows = [_extraction_row(session, document, latest.get(document.id)) for document in documents]

        loan_options = []
        for loan in session.exec(select(LoanAccount)).all():
            property_record = session.get(Property, loan.property_id)
            where = property_record.name if property_record else "no property registered"
            loan_options.append({"value": loan.id, "label": f"Loan {loan.loan_number} · {where}"})

        account_options = []
        for account in session.exec(select(FinancialAccount)).all():
            institution = session.get(FinancialInstitution, account.institution_id)
            lender = institution.name if institution else "unknown institution"
            account_options.append({
                "value": account.id,
                "label": f"{account.account_name} ({account.account_type}) · {lender}",
            })

        return templates.TemplateResponse(
            request=request,
            name="statements.html",
            context={
                "active_page": "statements",
                "message": message,
                "rows": rows,
                "loan_options": loan_options,
                "account_options": account_options,
                "max_upload_mb": MAX_UPLOAD_BYTES // (1024 * 1024),
            },
        )

    @router.post("/statements/upload")
    async def upload_statement(
        file: UploadFile = File(...),
        session: Session = Depends(get_session_dep),
    ):
        """Stores a statement PDF in this portfolio's vault and stages its extraction."""
        payload = await file.read(MAX_UPLOAD_BYTES + 1)
        if not payload:
            raise HTTPException(status_code=400, detail="No file was uploaded.")
        if len(payload) > MAX_UPLOAD_BYTES:
            raise HTTPException(
                status_code=400,
                detail=f"Statement PDFs are limited to {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.",
            )
        if not payload.startswith(PDF_MAGIC):
            raise HTTPException(
                status_code=400,
                detail="Only PDF statements are accepted: the uploaded file does not begin with %PDF-.",
            )

        context = current_context()
        digest = hashlib.sha256(payload).hexdigest()
        if session.exec(select(Document).where(Document.file_sha256 == digest)).first():
            return _redirect("That exact file is already in this portfolio's vault; review it below.")

        staging = context.paths.inbox / f"upload-{uuid4().hex}.pdf"
        staging.parent.mkdir(parents=True, exist_ok=True)
        staging.write_bytes(payload)
        try:
            try:
                extraction = extract_and_resolve(staging, session)
            except Exception as exc:
                # A malformed PDF is the uploader's problem, not a server fault.
                raise HTTPException(status_code=400, detail=f"The statement could not be read: {exc}") from exc
            _persist_document(
                staging, digest, context.paths.vault, session, extraction,
                original_filename=Path(file.filename or "statement.pdf").name,
            )
        finally:
            staging.unlink(missing_ok=True)

        if extraction is None:
            return _redirect(
                "The document is stored in the vault, but it is not a statement format this "
                "instance recognises, so nothing was extracted."
            )
        if extraction.status == "VALIDATED":
            return _redirect("Statement extracted and matched to a registered account; approve and import it below.")
        return _redirect("Statement retained for review: " + " ".join(extraction.review_reasons + extraction.resolution_reasons))

    @router.post("/statements/extractions/{extraction_id}/update")
    async def update_statement_extraction(
        extraction_id: str,
        request: Request,
        session: Session = Depends(get_session_dep),
    ):
        """Applies corrections and/or a chosen account, then re-validates."""
        record = session.get(DocumentExtraction, extraction_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Unknown extraction.")

        form = await request.form()
        updates = _updates_from_form(form)
        try:
            if form.get("register_observed_account"):
                data = json.loads(record.extracted_data_json)
                data.update(updates)
                account = register_observed_account(session, data)
                updates["financial_account_id"] = account.id
            extraction = update_extraction(session, extraction_id, updates, commit=True)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _redirect(f"Extraction re-validated: {extraction.validation_status}.")

    @router.post("/statements/extractions/{extraction_id}/approve")
    async def approve_statement_extraction(
        extraction_id: str,
        session: Session = Depends(get_session_dep),
    ):
        try:
            extraction = approve_extraction(session, extraction_id, commit=True)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _redirect(f"Extraction approved: {extraction.approval_status}.")

    @router.post("/statements/extractions/{extraction_id}/import")
    async def import_statement_extraction(
        extraction_id: str,
        session: Session = Depends(get_session_dep),
    ):
        try:
            statement = import_approved_extraction(session, extraction_id, commit=True)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _redirect(f"Statement imported as {statement.statement_type} on {statement.statement_date}.")

    return router
