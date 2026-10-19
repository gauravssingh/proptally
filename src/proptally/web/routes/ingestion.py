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
from urllib.parse import quote, urlencode
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
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
    reprocess_document,
    reprocess_unrecognized_documents,
    update_extraction,
)
from proptally.onboarding.archive import ArchiveRejected, inventory_archive
from proptally.onboarding.store import OnboardingStore
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

#: The states an extraction can be in, and the words the screen shows for them.
STATUS_LABELS = {"VALIDATED": "Validated", "NEEDS_REVIEW": "Needs review", "UNSUPPORTED": "Unsupported"}

#: The queue's facets, in the order the rail shows them. A row's `action` — the same
#: value that decides its one next step — decides the facet it belongs to, so the rail's
#: counts add up to the queue and no facet claims a row it would not show.
STATUS_FACETS: tuple[tuple[str, str, str], ...] = (
    ("all", "All", "inbox"),
    ("review", "Needs review", "alert_triangle"),
    ("approve", "Ready to approve", "check_circle"),
    ("approved", "Approved", "check"),
    ("imported", "Imported", "ledger"),
)

#: A row's next step, as the URL names the facet that holds it.
FACET_FOR_ACTION = {"review": "review", "approve": "approve", "import": "approved", "done": "imported"}

SORTS: tuple[tuple[str, str], ...] = (
    ("received", "Received"),
    ("name", "Document"),
    ("type", "Type"),
    ("amount", "Amount / Due"),
)

INSPECTOR_TABS: tuple[tuple[str, str], ...] = (
    ("preview", "Preview"),
    ("figures", "Figures"),
    ("statement", "Statement"),
)

#: How the figures the parser read are named.
MONEY_LABELS: dict[str, str] = {
    "opening_balance": "Opening balance",
    "closing_balance": "Closing balance",
    "principal": "Principal",
    "interest": "Interest",
    "escrow": "Escrow",
    "fees": "Fees",
    "total_due": "Total due",
    "outstanding_principal": "Outstanding principal",
    "escrow_balance": "Escrow balance",
}


def _dollars(cents: int | None) -> str:
    return "" if cents is None else f"{Decimal(cents) / 100:.2f}"


def _percent(milli_percent: int | None) -> str:
    return "" if milli_percent is None else f"{Decimal(milli_percent) / 1000:.3f}"


def _redirect(message: str | None = None) -> RedirectResponse:
    target = "/statements" if not message else f"/statements?message={quote(message)}"
    return RedirectResponse(url=target, status_code=303)


def _statements_url(*, status: str | None = None, sort: str | None = None,
                    doc: str | None = None, tab: str | None = None,
                    q: str | None = None, page: int | None = None,
                    per_page: str | None = None) -> str:
    """One address for one view of the queue, so the template never assembles a URL."""
    params: dict[str, str] = {}
    if status and status != "all":
        params["status"] = status
    if sort and sort != "received":
        params["sort"] = sort
    if q and q.strip():
        params["q"] = q.strip()
    if per_page and per_page != "25":
        params["per_page"] = per_page
    if page and page > 1:
        params["page"] = str(page)
    if doc:
        params["doc"] = doc
        # A tab only means anything with a statement chosen, so it hangs off `doc`.
        if tab and tab != "preview":
            params["tab"] = tab
    return "/statements" + (f"?{urlencode(params)}" if params else "")


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


def _statement_document_type(extraction) -> str | None:
    if extraction is None:
        return None
    return {
        "MORTGAGE": "MORTGAGE_STATEMENT",
        "CHECKING": "CHECKING_STATEMENT",
        "CREDIT_CARD": "CREDIT_CARD_STATEMENT",
    }.get(getattr(extraction, "statement_type", None))


def _account_tail(value: str | None) -> str:
    digits = "".join(character for character in str(value or "") if character.isdigit())
    if len(digits) >= 4:
        return digits[-4:]
    return str(value or "").strip().lstrip(".").casefold()


def _relink_pending_account_statements(session: Session, account: FinancialAccount) -> int:
    """Link other pending statements that name the account just registered."""
    institution = session.get(FinancialInstitution, account.institution_id)
    institution_name = (institution.name if institution else "").strip().casefold()
    account_tail = _account_tail(account.account_number_masked)
    if not institution_name or not account_tail:
        return 0
    linked = 0
    pending = session.exec(
        select(DocumentExtraction).where(
            DocumentExtraction.approval_status == "PENDING",
            DocumentExtraction.imported_statement_id.is_(None),
        )
    ).all()
    for other in pending:
        data = json.loads(other.extracted_data_json)
        if data.get("financial_account_id"):
            continue
        if data.get("statement_type") not in BANK_STATEMENT_TYPES:
            continue
        if (data.get("institution_name") or "").strip().casefold() != institution_name:
            continue
        if _account_tail(data.get("account_last4")) != account_tail:
            continue
        try:
            update_extraction(
                session, other.id, {"financial_account_id": account.id}, commit=False
            )
            linked += 1
        except ValueError:
            continue
    if linked:
        session.commit()
    return linked


def _store_single_statement(payload: bytes, filename: str, session: Session, context) -> RedirectResponse:
    digest = hashlib.sha256(payload).hexdigest()
    if session.exec(select(Document).where(Document.file_sha256 == digest)).first():
        return _redirect("That exact file is already in this portfolio's vault; review it below.")

    # Stage outside the watched inbox: the hot-folder watcher must never move
    # or process a file while this request is still reading it.
    staging = context.paths.root / "staging" / f"upload-{uuid4().hex}.pdf"
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
            original_filename=Path(filename or "statement.pdf").name,
            document_type=_statement_document_type(extraction),
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


def _store_statement_archive(payload: bytes, session: Session, context) -> RedirectResponse:
    store = OnboardingStore(context.paths.root)
    batch_id = f"stmtzip_{uuid4().hex}"
    archive_path = context.paths.root / "staging" / f"{batch_id}.zip"
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    archive_path.write_bytes(payload)
    validated = review = duplicates = unsupported = failed = 0
    new_accounts: set[str] = set()
    try:
        try:
            entries = inventory_archive(archive_path, store, batch_id)
        except ArchiveRejected as exc:
            return _redirect(f"Archive rejected: {exc}")

        for entry in entries:
            if entry.format != "PDF":
                unsupported += 1
                continue
            path = store.resolve(entry.storage_path)
            if session.exec(
                select(Document).where(Document.file_sha256 == entry.sha256)
            ).first():
                duplicates += 1
                continue
            try:
                extraction = extract_and_resolve(path, session)
            except Exception:  # noqa: BLE001 - one bad file must not stop the archive
                extraction = None
                failed += 1
            try:
                _persist_document(
                    path,
                    entry.sha256,
                    context.paths.vault,
                    session,
                    extraction,
                    original_filename=entry.original_filename,
                    document_type=_statement_document_type(extraction),
                )
            except Exception:  # noqa: BLE001 - keep processing the rest of the archive
                failed += 1
                continue
            if extraction is None:
                unsupported += 1
                continue
            if extraction.status == "VALIDATED":
                validated += 1
            else:
                review += 1
            data = extraction.to_dict()
            if (
                data.get("statement_type") in BANK_STATEMENT_TYPES
                and not data.get("financial_account_id")
            ):
                institution = data.get("institution_name") or "Unknown institution"
                last4 = data.get("account_last4") or "unknown"
                new_accounts.add(f"{institution} ...{last4}")

        parts = []
        if validated:
            parts.append(f"{validated} matched")
        if review:
            parts.append(f"{review} need review")
        if new_accounts:
            shown = "; ".join(sorted(new_accounts)[:4])
            more = len(new_accounts) - 4
            parts.append(
                f"{len(new_accounts)} new account{'' if len(new_accounts) == 1 else 's'}: "
                f"{shown}{f' and {more} more' if more > 0 else ''}"
            )
        if duplicates:
            parts.append(f"{duplicates} already in the vault")
        if unsupported:
            parts.append(f"{unsupported} unsupported")
        if failed:
            parts.append(f"{failed} could not be read")
        message = (
            "ZIP processed: " + "; ".join(parts) + "."
            if parts
            else "The ZIP contained no statement PDFs."
        )
        return _redirect(message)
    finally:
        archive_path.unlink(missing_ok=True)
        store.remove_session(batch_id)


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
    status = extraction.validation_status if extraction else "UNSUPPORTED"
    approval_status = extraction.approval_status if extraction else None
    mime = document.mime_type or ""
    return {
        "document": document,
        "extraction": extraction,
        "data": data,
        "status": status,
        "status_label": STATUS_LABELS.get(status, status),
        "approval_status": approval_status,
        "reasons": list(data.get("review_reasons") or []) + list(data.get("resolution_reasons") or []),
        "statement": statement,
        "account": session.get(FinancialAccount, account_id) if account_id else None,
        "loan": session.get(LoanAccount, loan_id) if loan_id else None,
        "is_bank": data.get("statement_type") in BANK_STATEMENT_TYPES,
        "type_label": data.get("statement_type") or "Unrecognised",
        "can_preview": mime == "application/pdf" or mime.startswith("image/"),
        # The single next step for this row, decided once here so that the row's button,
        # its dialog and the rail's counts cannot drift apart. Importing ends the chain,
        # so an imported statement is never offered again.
        "action": (
            "done" if statement
            else "import" if approval_status == "APPROVED"
            else "approve" if status == "VALIDATED"
            else "review"
        ),
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
        status: str = "all",
        sort: str = "received",
        doc: str | None = None,
        tab: str = "preview",
        q: str | None = Query(None),
        per_page: str = "25",
        page: int | None = None,
        message: str | None = Query(None),
        session: Session = Depends(get_session_dep),
    ):
        """One facet of the queue, the statement chosen in it, and the one next step for it."""
        facet_keys = {key for key, _, _ in STATUS_FACETS}
        status = status if status in facet_keys else "all"
        sort = sort if sort in dict(SORTS) else "received"
        tab = tab if tab in dict(INSPECTOR_TABS) else "preview"

        documents = session.exec(
            select(Document).order_by(desc(Document.uploaded_at), desc(Document.id))
        ).all()

        # The most recent extraction of each document is the one on screen; older
        # ones stay in the table as history under the same document.
        latest: dict[str, DocumentExtraction] = {}
        for extraction in session.exec(select(DocumentExtraction)).all():
            current = latest.get(extraction.document_id)
            if current is None or extraction.created_at >= current.created_at:
                latest[extraction.document_id] = extraction

        all_rows = [_extraction_row(session, document, latest.get(document.id)) for document in documents]

        needle = (q or "").strip().lower()
        if needle:
            all_rows = [
                row for row in all_rows
                if needle in row["document"].original_filename.lower()
                or needle in (row["data"].get("servicer_name") or "").lower()
                or needle in (row["data"].get("institution_name") or "").lower()
                or needle in (row["data"].get("loan_number") or "").lower()
                or needle in str(row["data"].get("account_last4") or "").lower()
                or needle in (row["type_label"] or "").lower()
            ]

        # The rail counts the queue, from the rows matching search: each row is
        # in exactly one facet, the one its own next step names.
        counts = {key: 0 for key in facet_keys}
        counts["all"] = len(all_rows)
        for row in all_rows:
            row["facet"] = FACET_FOR_ACTION[row["action"]]
            counts[row["facet"]] += 1

        rows = [row for row in all_rows if status == "all" or row["facet"] == status]

        def _amount_key(row):
            m = row.get("money", {})
            v = m.get("closing_balance") if row.get("is_bank") else m.get("total_due")
            try:
                return float(str(v).replace(",", "")) if v else 0.0
            except (ValueError, TypeError):
                return 0.0

        ordered = sorted(
            rows,
            key={
                "received": lambda row: row["document"].uploaded_at,
                "name": lambda row: row["document"].original_filename.lower(),
                "type": lambda row: (row["type_label"], row["document"].original_filename.lower()),
                "amount": _amount_key,
            }[sort],
            reverse=(sort in ("received", "amount")),
        )

        # The queue is paged before it is rendered. The internal table scroll handles the
        # rows on this page; the pager moves between pages, so a long queue never becomes
        # one endless table.
        per_page_clean = (per_page or "25").strip()
        if per_page_clean.upper() == "ALL":
            page_size = max(len(ordered), 1)
            per_page_clean = "ALL"
        else:
            try:
                page_size = max(1, min(250, int(per_page_clean)))
            except (TypeError, ValueError):
                page_size = 25
                per_page_clean = "25"
            else:
                per_page_clean = str(page_size)

        total_items = len(ordered)
        total_pages = max(1, -(-total_items // page_size))

        # The inspector: the one statement being looked at, and its tab. A chosen
        # statement opens on its own page when no page is named, so a direct document
        # link does not silently move the row out from under the reader. An explicit
        # page always wins, so the pager can move while an inspector stays open.
        selected = next((row for row in ordered if row["document"].id == doc), None)
        if selected is None and doc:
            selected = next((row for row in all_rows if row["document"].id == doc), None)

        if page is None:
            selected_index = next(
                (index for index, row in enumerate(ordered) if row["document"].id == selected["document"].id),
                None,
            ) if selected is not None else None
            page = selected_index // page_size + 1 if selected_index is not None else 1

        page = min(max(page, 1), total_pages)
        start_index = (page - 1) * page_size
        end_index = min(start_index + page_size, total_items)
        page_rows = ordered[start_index:end_index]

        # Selection is navigation: every row carries the address that shows it, so the
        # pick, the name and the row's own menu item are the same link. The page travels
        # with the link, because that is where the row is.
        for index, row in enumerate(ordered):
            row_page = index // page_size + 1
            row["select_href"] = _statements_url(
                status=status, sort=sort, doc=row["document"].id, q=q,
                per_page=per_page_clean, page=row_page,
            )

        # A selected statement from another facet can still be open in the inspector;
        # its dialogs must exist even when it is not a row on this page.
        dialog_rows = list(page_rows)
        if selected is not None and not any(row is selected for row in dialog_rows):
            dialog_rows.append(selected)

        inspector_tabs: list[dict] = []
        if selected is not None:
            inspector_tabs = [
                {"key": key, "label": label, "href": _statements_url(
                    status=status, sort=sort, doc=selected["document"].id, tab=key, q=q,
                    per_page=per_page_clean, page=page)}
                for key, label in INSPECTOR_TABS
            ]

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

        def _page_url(target_page: int) -> str:
            return _statements_url(
                status=status, sort=sort, q=q, per_page=per_page_clean, page=target_page,
                doc=selected["document"].id if selected else None,
                tab=tab if selected else None,
            )

        start_item = start_index + 1 if total_items else 0
        end_item = end_index

        return templates.TemplateResponse(
            request=request,
            name="statements.html",
            context={
                "active_page": "statements",
                "message": message,
                "message_variant": "info",
                "status": status,
                "q": q or "",
                "is_searching": bool(needle),
                "status_facets": [
                    {"key": key, "label": label, "icon": icon, "count": counts[key],
                     "href": _statements_url(status=key, sort=sort, q=q, per_page=per_page_clean),
                     "active": key == status}
                    for key, label, icon in STATUS_FACETS
                ],
                "sort": sort,
                "sort_urls": {
                    key: _statements_url(
                        status=status, sort=key, tab=tab, q=q, per_page=per_page_clean,
                        doc=selected["document"].id if selected else None)
                    for key, _ in SORTS
                },
                "sort_chips": [
                    {"label": label, "active": key == sort, "href": _statements_url(
                        status=status, sort=key, tab=tab, q=q, per_page=per_page_clean,
                        doc=selected["document"].id if selected else None)}
                    for key, label in SORTS
                ],
                "rows": page_rows,
                "dialog_rows": dialog_rows,
                "total_documents": len(all_rows),
                "total_items": total_items,
                "start_item": start_item,
                "end_item": end_item,
                "page": page,
                "total_pages": total_pages,
                "per_page": per_page_clean,
                "prev_url": _page_url(page - 1) if page > 1 else None,
                "next_url": _page_url(page + 1) if page < total_pages else None,
                "awaiting_review": counts["review"],
                "unsupported_count": sum(1 for row in all_rows if row["status"] == "UNSUPPORTED"),
                "ready_to_approve": counts["approve"],
                "approved_ready": counts["approved"],
                "ready_to_import": counts["approve"] + counts["approved"],
                "imported_total": counts["imported"],
                "selected": selected,
                "inspector_tabs": inspector_tabs,
                "active_tab": tab,
                "inspector_close_href": _statements_url(
                    status=status, sort=sort, q=q, per_page=per_page_clean, page=page),
                "money_labels": MONEY_LABELS,
                "loan_options": loan_options,
                "account_options": account_options,
                "max_upload_mb": MAX_UPLOAD_BYTES // (1024 * 1024),
            },
        )

    @router.post("/statements/approve-ready")
    def approve_ready_statements(
        status: str = Form("all"),
        sort: str = Form("received"),
        q: str = Form(""),
        page: int = Form(1),
        per_page: str = Form("25"),
        session: Session = Depends(get_session_dep),
    ):
        """Approve every validated statement that is still pending approval.

        Approval records the human check; it does not import the statement or
        write to the ledger. Statements in NEEDS_REVIEW are deliberately excluded.
        """
        facet_keys = {key for key, _, _ in STATUS_FACETS}
        status = status if status in facet_keys else "all"
        sort = sort if sort in dict(SORTS) else "received"
        ready = session.exec(
            select(DocumentExtraction).where(
                DocumentExtraction.validation_status == "VALIDATED",
                DocumentExtraction.approval_status == "PENDING",
                DocumentExtraction.imported_statement_id.is_(None),
            )
        ).all()
        for extraction in ready:
            approve_extraction(session, extraction.id, commit=False)
        session.commit()

        approved = len(ready)
        message = (
            f"Approved {approved} statement{'' if approved == 1 else 's'}. "
            "Import them when ready."
            if approved
            else "No statements were waiting for approval."
        )
        target = _statements_url(
            status="approved" if status == "approve" else status,
            sort=sort, q=q, page=page, per_page=per_page,
        )
        separator = "&" if "?" in target else "?"
        return RedirectResponse(
            url=f"{target}{separator}{urlencode({'message': message})}",
            status_code=303,
        )

    @router.post("/statements/import-approved")
    def import_approved_statements(
        status: str = Form("all"),
        sort: str = Form("received"),
        q: str = Form(""),
        page: int = Form(1),
        per_page: str = Form("25"),
        session: Session = Depends(get_session_dep),
    ):
        """Import approved statements, skipping duplicate loan/account periods.

        Import is the step that makes a statement authoritative. It deliberately
        does not post a journal entry; payment settlement remains a separate ledger
        action. Each row runs in its own savepoint so one bad or duplicate statement
        cannot roll back the rest of the batch.
        """
        facet_keys = {key for key, _, _ in STATUS_FACETS}
        status = status if status in facet_keys else "all"
        sort = sort if sort in dict(SORTS) else "received"
        rows = session.exec(
            select(DocumentExtraction)
            .join(Document, Document.id == DocumentExtraction.document_id)
            .where(
                DocumentExtraction.validation_status == "VALIDATED",
                DocumentExtraction.approval_status == "APPROVED",
                DocumentExtraction.imported_statement_id.is_(None),
            )
            .order_by(desc(Document.uploaded_at), desc(DocumentExtraction.created_at))
        ).all()

        imported = duplicates = incomplete = failed = 0
        first_error = ""
        for extraction in rows:
            try:
                with session.begin_nested():
                    import_approved_extraction(session, extraction.id, commit=False)
                imported += 1
            except ValueError as exc:
                detail = str(exc).lower()
                if "already" in detail or "period already exists" in detail:
                    duplicates += 1
                elif "missing" in detail:
                    incomplete += 1
                else:
                    failed += 1
                    first_error = first_error or str(exc)
            except Exception as exc:  # noqa: BLE001 - one row must not stop the batch
                failed += 1
                first_error = first_error or str(exc)
        session.commit()

        parts = []
        if imported:
            parts.append(f"Imported {imported} statement{'' if imported == 1 else 's'}")
        if duplicates:
            parts.append(f"Skipped {duplicates} duplicate period{'' if duplicates == 1 else 's'}")
        if incomplete:
            parts.append(f"{incomplete} incomplete statement{'' if incomplete == 1 else 's'}")
        if failed:
            parts.append(f"{failed} failed")
        message = ". ".join(parts) + "." if parts else "No approved statements were waiting for import."
        if first_error and failed:
            message += f" First error: {first_error[:180]}"

        target = _statements_url(
            status="imported" if status == "approved" else status,
            sort=sort, q=q, page=page, per_page=per_page,
        )
        separator = "&" if "?" in target else "?"
        return RedirectResponse(
            url=f"{target}{separator}{urlencode({'message': message})}",
            status_code=303,
        )

    @router.get("/statements/extractions/{extraction_id}", response_class=RedirectResponse)
    def view_extraction_redirect(
        extraction_id: str,
        session: Session = Depends(get_session_dep),
    ):
        """Redirects direct extraction URLs to the statement in the queue."""
        extraction = session.get(DocumentExtraction, extraction_id)
        if extraction and extraction.document_id:
            return RedirectResponse(url=f"/statements?doc={extraction.document_id}", status_code=303)
        return RedirectResponse(url="/statements", status_code=303)

    @router.post("/statements/documents/{document_id}/reprocess")
    def reprocess_statement_document(
        document_id: str,
        session: Session = Depends(get_session_dep),
    ):
        """Re-runs the current parsers on a document already in the vault."""
        document = session.get(Document, document_id)
        if document is None:
            raise HTTPException(status_code=404, detail="Unknown document.")
        try:
            extraction = reprocess_document(session, document, commit=True)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _redirect(
            f"Reprocessed {document.original_filename}: {extraction.validation_status}."
        )

    @router.post("/statements/reprocess-unrecognized")
    def reprocess_unrecognized_statements(
        session: Session = Depends(get_session_dep),
    ):
        """Retry every vaulted PDF that has no extraction yet."""
        report = reprocess_unrecognized_documents(session, commit=True)
        message = (
            f"Retried {report['scanned']} unrecognized document"
            f"{'' if report['scanned'] == 1 else 's'}: "
            f"{report['extracted']} extracted, {report['unsupported']} still unsupported"
        )
        if report["errors"]:
            message += f", {report['errors']} failed with errors"
        return _redirect(message + ".")

    @router.post("/statements/upload")
    async def upload_statement(
        file: UploadFile = File(...),
        session: Session = Depends(get_session_dep),
    ):
        """Stores statement PDFs, or a ZIP of them, and stages their extractions."""
        payload = await file.read(MAX_UPLOAD_BYTES + 1)
        if not payload:
            raise HTTPException(status_code=400, detail="No file was uploaded.")
        if len(payload) > MAX_UPLOAD_BYTES:
            raise HTTPException(
                status_code=400,
                detail=f"Statement uploads are limited to {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.",
            )

        context = current_context()
        if payload.startswith(PDF_MAGIC):
            return _store_single_statement(
                payload, Path(file.filename or "statement.pdf").name, session, context
            )
        if payload.startswith(b"PK\x03\x04"):
            return _store_statement_archive(payload, session, context)
        raise HTTPException(
            status_code=400,
            detail="Upload a PDF statement or a ZIP archive of statement PDFs.",
        )

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
        registered_account = None
        try:
            if form.get("register_observed_account"):
                data = json.loads(record.extracted_data_json)
                data.update(updates)
                registered_account = register_observed_account(session, data)
                updates["financial_account_id"] = registered_account.id
            extraction = update_extraction(session, extraction_id, updates, commit=True)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        linked = (
            _relink_pending_account_statements(session, registered_account)
            if registered_account is not None
            else 0
        )
        message = f"Extraction re-validated: {extraction.validation_status}."
        if linked:
            message += f" Linked {linked} matching statement{'' if linked == 1 else 's'} to the new account."
        return _redirect(message)

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

        rent_note = ""
        if statement.statement_type == "CHECKING":
            # Bank imports are where rent arrives. Classify only payments the
            # extractor can attribute to one door (memo address or a single
            # active lease); anything ambiguous stays in the review queue.
            from proptally.domain.transactions.rent import bulk_apply_rent_payments

            report = bulk_apply_rent_payments(session, create_leases=True)
            if report.applied:
                rent_note = (
                    f" Auto-classified {len(report.applied)} tenant payment"
                    f"{'' if len(report.applied) == 1 else 's'}."
                )
        return _redirect(
            f"Statement imported as {statement.statement_type} on {statement.statement_date}.{rent_note}"
        )

    return router
