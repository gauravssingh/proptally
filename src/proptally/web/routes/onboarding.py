"""Document-based onboarding: upload, status, review, approval and provisioning.

The routes are thin on purpose. The archive is inventoried by
:mod:`proptally.onboarding.archive`, processed by the durable worker, reconciled by
:mod:`proptally.onboarding.pipeline`, and only :mod:`proptally.onboarding.provisioning`
writes into a portfolio. What lives here is HTTP: ownership checks, form parsing and
the screens that let a customer see where every value came from.

Every route resolves a staging session through :func:`_owned_session`, which requires
the signed-in user *and* the active portfolio to match. A session id alone is never
authority to read another customer's documents.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from sqlmodel import Session, select

from proptally.db.onboarding_models import (
    DOC_FAILED,
    DOC_NEEDS_REVIEW,
    DOC_UNSUPPORTED,
    ENTITY_ACCOUNT,
    ENTITY_LEASE,
    ENTITY_MORTGAGE,
    ENTITY_PROPERTY,
    JOB_DOCUMENT,
    JOB_PROVISION,
    ONBOARDING_APPROVED,
    ONBOARDING_CANCELLED,
    ONBOARDING_COMPLETED,
    ONBOARDING_PARTIAL,
    ONBOARDING_PROCESSING,
    ONBOARDING_PROCESSING_FAILED,
    ONBOARDING_PROVISIONING,
    ONBOARDING_PROVISIONING_FAILED,
    ONBOARDING_READY,
    ONBOARDING_REVIEWING,
    ONBOARDING_UPLOADING,
    PROPOSAL_ACCEPTED,
    PROPOSAL_DEFERRED,
    PROPOSAL_IMPORTED,
    PROPOSAL_NEEDS_REVIEW,
    PROPOSAL_REJECTED,
    REVIEWABLE_STATES,
    TERMINAL_DOCUMENT_STATES,
    OnboardingDocument,
    OnboardingFieldEvidence,
    OnboardingJob,
    OnboardingProposal,
    OnboardingProvisionEntry,
    OnboardingSession,
    OnboardingUpload,
)
from proptally.onboarding.archive import (
    MAX_ARCHIVE_BYTES,
    MAX_EXTRACTED_BYTES,
    MAX_FILE_BYTES,
    MAX_FILES,
    SUPPORTED_SUFFIXES,
    ArchiveRejected,
    inventory_archive,
)
from proptally.onboarding.classify import category_label
from proptally.onboarding.fields import (
    ENTITY_LABELS,
    REQUIRED_FIELDS,
    display_value,
    editable,
    field_spec,
    missing_required,
    ordered_fields,
    parse_value,
)
from proptally.onboarding.pipeline import (
    enqueue,
    jsonable,
    loads,
    new_id,
    record_decision,
    review_counts,
)
from proptally.onboarding.store import OnboardingStore
from proptally.web.auth import AuthorizationError, current_context
from proptally.web.onboarding_resume import (
    SESSION_LABELS,
    SESSION_VARIANTS,
    journey_steps,
    latest_session,
    resume_context,
)

#: Staging is temporary. A month is long enough to finish a review and short enough
#: that an abandoned upload does not sit on the disk forever.
RETENTION_DAYS = 30

DOCUMENT_LABELS = {
    "DISCOVERED": "Queued",
    "CLASSIFIED": "Identified",
    "EXTRACTING": "Processing",
    "EXTRACTED": "Extracting information",
    "READY": "Ready",
    "NEEDS_REVIEW": "Needs attention",
    "UNSUPPORTED": "Unsupported",
    "CORRUPT": "Corrupt",
    "EXTRACTION_FAILED": "Failed",
    "DUPLICATE": "Duplicate",
}

DOCUMENT_VARIANTS = {
    "READY": "success",
    "NEEDS_REVIEW": "warning",
    "UNSUPPORTED": "neutral",
    "CORRUPT": "error",
    "EXTRACTION_FAILED": "error",
    "DUPLICATE": "neutral",
    "EXTRACTING": "info",
    "DISCOVERED": "neutral",
    "CLASSIFIED": "info",
    "EXTRACTED": "info",
}

PROPOSAL_LABELS = {
    PROPOSAL_NEEDS_REVIEW: "Needs review",
    PROPOSAL_ACCEPTED: "Accepted",
    PROPOSAL_REJECTED: "Rejected",
    PROPOSAL_DEFERRED: "Deferred",
    PROPOSAL_IMPORTED: "Imported",
}

PROPOSAL_VARIANTS = {
    PROPOSAL_NEEDS_REVIEW: "warning",
    PROPOSAL_ACCEPTED: "success",
    PROPOSAL_REJECTED: "neutral",
    PROPOSAL_DEFERRED: "info",
    PROPOSAL_IMPORTED: "success",
}

FORMAT_LABELS = {
    "NATIVE_PDF": "Text PDF",
    "SCANNED_PDF": "Scanned PDF",
    "IMAGE": "Image",
    "DOCX": "Word document",
    "XLSX": "Spreadsheet",
    "CSV": "CSV",
    "TEXT": "Text",
    "SUPPORTED": "Document",
    "UNSUPPORTED": "Unsupported",
    "UNKNOWN": "Unknown",
}

ENTITY_ORDER = (ENTITY_PROPERTY, ENTITY_MORTGAGE, ENTITY_ACCOUNT, ENTITY_LEASE)


def human_size(num_bytes: int | None) -> str:
    size = int(num_bytes or 0)
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.0f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def get_onboarding_router(templates, control_engine, tenant_manager, get_session_dep):
    router = APIRouter()
    store = OnboardingStore(tenant_manager.root)

    # --- ownership and views ----------------------------------------------

    def _owned_session(onboarding_id: str) -> OnboardingSession:
        context = current_context()
        if context is None:
            raise HTTPException(status_code=401, detail="Sign in first.")
        with Session(control_engine) as control:
            onboarding = control.get(OnboardingSession, onboarding_id)
            if (
                onboarding is None
                or onboarding.user_id != context.user.id
                or onboarding.tenant_id != context.tenant.id
            ):
                # 404 rather than 403: the existence of another customer's session
                # is not something this route should reveal.
                raise HTTPException(status_code=404, detail="Onboarding session not found.")
            control.expunge(onboarding)
            return onboarding

    def _require_work() -> None:
        context = current_context()
        if context is None or not context.can_work:
            raise AuthorizationError("This role may read the portfolio but not change it.")

    def _require_owner() -> None:
        context = current_context()
        if context is None or not context.can_confirm_client:
            raise AuthorizationError(
                "Only the portfolio owner can approve the import. It creates the "
                "portfolio's permanent records."
            )

    def _redirect(url: str, **params: str) -> RedirectResponse:
        query = urlencode({key: value for key, value in params.items() if value})
        return RedirectResponse(url=url + (f"?{query}" if query else ""), status_code=303)

    def _documents(onboarding_id: str) -> list[OnboardingDocument]:
        with Session(control_engine) as control:
            documents = control.exec(
                select(OnboardingDocument)
                .where(OnboardingDocument.onboarding_id == onboarding_id)
                .order_by(OnboardingDocument.original_filename)
            ).all()
            for document in documents:
                control.expunge(document)
            return documents

    def _proposals(onboarding_id: str) -> list[OnboardingProposal]:
        with Session(control_engine) as control:
            proposals = control.exec(
                select(OnboardingProposal)
                .where(OnboardingProposal.onboarding_id == onboarding_id)
                .order_by(OnboardingProposal.entity_type, OnboardingProposal.title)
            ).all()
            for proposal in proposals:
                control.expunge(proposal)
            return proposals

    def _proposal(onboarding_id: str, proposal_id: str) -> OnboardingProposal:
        with Session(control_engine) as control:
            proposal = control.get(OnboardingProposal, proposal_id)
            if proposal is None or proposal.onboarding_id != onboarding_id:
                raise HTTPException(status_code=404, detail="Proposed record not found.")
            control.expunge(proposal)
            return proposal

    def _evidence(proposal_id: str) -> dict[str, list[dict]]:
        with Session(control_engine) as control:
            rows = control.exec(
                select(OnboardingFieldEvidence)
                .where(OnboardingFieldEvidence.proposal_id == proposal_id)
                .order_by(OnboardingFieldEvidence.field_name, OnboardingFieldEvidence.page_number)
            ).all()
            grouped: dict[str, list[dict]] = {}
            for row in rows:
                grouped.setdefault(row.field_name, []).append({
                    "document_id": row.document_id,
                    "page": row.page_number,
                    "text": row.evidence_text,
                    "method": row.method,
                    "confidence": row.confidence,
                    "value": loads(row.value_json),
                    "value_display": display_value(row.field_name, loads(row.value_json)),
                })
            return grouped

    def _session_view(onboarding: OnboardingSession) -> dict:
        documents = _documents(onboarding.id)
        counts = review_counts(control_engine, onboarding.id)
        document_counts = Counter(document.status for document in documents)
        processed = sum(
            count for status, count in document_counts.items()
            if status in TERMINAL_DOCUMENT_STATES
        )
        with Session(control_engine) as control:
            failed_jobs = control.exec(
                select(OnboardingJob).where(
                    OnboardingJob.onboarding_id == onboarding.id,
                    OnboardingJob.status == "FAILED",
                )
            ).all()
        status = onboarding.status
        journey_by_status = {
            ONBOARDING_UPLOADING: (
                "clock", "Uploading the archive",
                "The archive is being received. Processing starts as soon as it is validated.",
            ),
            ONBOARDING_PROCESSING: (
                "clock", "Processing documents",
                "PropTally is reading the archive one document at a time. You can close this page and come back.",
            ),
            ONBOARDING_READY: (
                "check_circle", "Review what PropTally found",
                "PropTally has grouped the documents. Review every proposed record before creating the portfolio.",
            ),
            ONBOARDING_PARTIAL: (
                "alert_triangle", "Ready for review, with files needing attention",
                "Review what was found, or retry the documents that could not be read.",
            ),
            ONBOARDING_PROCESSING_FAILED: (
                "alert_triangle", "Processing needs attention",
                "No records could be proposed. Inspect the documents and retry.",
            ),
            ONBOARDING_REVIEWING: (
                "check_circle", "Review in progress",
                "Continue accepting, editing or deferring the proposed records.",
            ),
            ONBOARDING_APPROVED: (
                "check_circle", "Approved",
                "PropTally is ready to create the portfolio.",
            ),
            ONBOARDING_PROVISIONING: (
                "clock", "Creating the portfolio",
                "The approved records are being created. This continues in the background.",
            ),
            ONBOARDING_PROVISIONING_FAILED: (
                "alert_triangle", "The portfolio import needs a retry",
                "Records already created were kept. Retrying continues from where it stopped.",
            ),
            ONBOARDING_COMPLETED: (
                "check_circle", "Portfolio created",
                "The approved records are now part of this portfolio. Source documents were copied into its vault.",
            ),
            ONBOARDING_CANCELLED: (
                "file_text", "Import cancelled",
                "The staged documents were removed. The audit rows remain.",
            ),
            "EXPIRED": (
                "file_text", "Import expired",
                "The staged documents were removed after 30 days.",
            ),
        }
        journey_icon, journey_title, journey_text = journey_by_status.get(
            status,
            ("file_text", "Document import", "The import is ready to continue."),
        )
        return {
            "session": onboarding,
            "status_label": SESSION_LABELS.get(status, status),
            "status_variant": SESSION_VARIANTS.get(status, "neutral"),
            "journey_icon": journey_icon,
            "journey_title": journey_title,
            "journey_text": journey_text,
            "journey_steps": journey_steps(status),
            "documents": documents,
            "document_counts": document_counts,
            "proposal_counts": counts,
            "total_documents": len(documents),
            "processed_documents": processed,
            "progress": int(processed / len(documents) * 100) if documents else 0,
            "failed_jobs": failed_jobs,
            "is_processing": onboarding.status in {
                ONBOARDING_UPLOADING, ONBOARDING_PROCESSING,
                ONBOARDING_APPROVED, ONBOARDING_PROVISIONING,
            },
            "is_reviewable": onboarding.status in REVIEWABLE_STATES,
        }

    DOC_STATUS_FILTERS = {
        "all": set(),
        "ready": {"READY"},
        "duplicate": {"DUPLICATE"},
        "attention": {"NEEDS_REVIEW", "EXTRACTION_FAILED", "UNSUPPORTED", "CORRUPT"},
    }

    def _filter_documents(
        documents: list[OnboardingDocument], status_filter: str, query: str
    ) -> list[OnboardingDocument]:
        statuses = DOC_STATUS_FILTERS.get(status_filter, set())
        needle = query.casefold().strip()
        result = []
        for document in documents:
            if statuses and document.status not in statuses:
                continue
            if needle:
                haystack = " ".join((
                    document.original_filename,
                    category_label(document.category),
                    FORMAT_LABELS.get(document.format, document.format),
                    DOCUMENT_LABELS.get(document.status, document.status),
                    document.sha256,
                )).casefold()
                if needle not in haystack:
                    continue
            result.append(document)
        return result

    def _document_rows(documents: list[OnboardingDocument]) -> list[dict]:
        return [
            {
                "document": document,
                "label": DOCUMENT_LABELS.get(document.status, document.status),
                "variant": DOCUMENT_VARIANTS.get(document.status, "neutral"),
                "category_label": category_label(document.category),
                "format_label": FORMAT_LABELS.get(document.format, document.format.title()),
                "size": human_size(document.size_bytes),
                "can_preview": document.mime_type in {
                    "application/pdf", "image/png", "image/jpeg", "image/tiff",
                },
                "retryable": document.status in {DOC_FAILED, DOC_NEEDS_REVIEW},
            }
            for document in documents
        ]

    # --- entry -------------------------------------------------------------

    @router.get("/onboarding", response_class=HTMLResponse)
    def onboarding_choice(request: Request, session: Session = Depends(get_session_dep)):
        context = current_context()
        return templates.TemplateResponse(
            request=request,
            name="onboarding/choice.html",
            context={
                "active_page": "onboarding",
                "can_work": bool(context and context.can_work),
                "resume": resume_context(
                    latest_session(control_engine, context.user.id, context.tenant.id)
                ),
            },
        )

    @router.get("/onboarding/import", response_class=HTMLResponse)
    def upload_screen(request: Request, session: Session = Depends(get_session_dep)):
        context = current_context()
        return templates.TemplateResponse(
            request=request,
            name="onboarding/upload.html",
            context={
                "active_page": "onboarding",
                "can_work": bool(context and context.can_work),
                "max_archive": human_size(MAX_ARCHIVE_BYTES),
                "max_file": human_size(MAX_FILE_BYTES),
                "max_extracted": human_size(MAX_EXTRACTED_BYTES),
                "max_files": MAX_FILES,
                "extensions": ", ".join(sorted(SUPPORTED_SUFFIXES)),
                "resume": resume_context(
                    latest_session(control_engine, context.user.id, context.tenant.id)
                ),
                "upload_steps": journey_steps(None),
                "error": request.query_params.get("error"),
            },
        )

    @router.post("/onboarding/import/upload")
    async def upload_archive(
        request: Request,
        archive: UploadFile = File(...),
        session: Session = Depends(get_session_dep),
    ):
        _require_work()
        context = current_context()
        filename = Path(archive.filename or "documents.zip").name
        if not filename.lower().endswith(".zip"):
            return _redirect("/onboarding/import", error="Choose a .zip archive of your documents.")

        onboarding_id = new_id("onb")
        upload_id = new_id("upl")
        now = datetime.now(UTC)
        onboarding = OnboardingSession(
            id=onboarding_id,
            user_id=context.user.id,
            tenant_id=context.tenant.id,
            status=ONBOARDING_UPLOADING,
            expires_at=now + timedelta(days=RETENTION_DAYS),
            created_at=now,
            updated_at=now,
        )
        with Session(control_engine) as control:
            control.add(onboarding)
            control.commit()

        store.prepare(onboarding_id)
        relative = store.archive_relative(onboarding_id, upload_id)
        destination = store.resolve(relative)
        digest = hashlib.sha256()
        total = 0
        upload_row = OnboardingUpload(
            id=upload_id,
            onboarding_id=onboarding_id,
            original_filename=filename,
            storage_path=relative,
            status="RECEIVED",
        )
        try:
            with destination.open("wb") as target:
                while True:
                    chunk = await archive.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_ARCHIVE_BYTES:
                        raise ArchiveRejected(
                            f"The archive is larger than {human_size(MAX_ARCHIVE_BYTES)}."
                        )
                    digest.update(chunk)
                    target.write(chunk)
            if total == 0:
                raise ArchiveRejected("The uploaded file is empty.")
            upload_row.size_bytes = total
            upload_row.archive_sha256 = digest.hexdigest()

            entries = inventory_archive(destination, store, onboarding_id)
            upload_row.status = "EXTRACTED"
            upload_row.file_count = len(entries)
            upload_row.extracted_bytes = sum(entry.size_bytes for entry in entries)
            upload_row.completed_at = datetime.now(UTC)
        except ArchiveRejected as exc:
            upload_row.status = "FAILED"
            upload_row.error = str(exc)[:500]
            with Session(control_engine) as control:
                control.add(upload_row)
                onboarding = control.get(OnboardingSession, onboarding_id)
                onboarding.status = ONBOARDING_CANCELLED
                onboarding.last_error = str(exc)[:500]
                onboarding.updated_at = datetime.now(UTC)
                control.add(onboarding)
                control.commit()
            store.remove_session(onboarding_id)
            return _redirect("/onboarding/import", error=str(exc))
        finally:
            await archive.close()

        seen: dict[str, str] = {}
        document_rows: list[OnboardingDocument] = []
        for entry in entries:
            document_id = new_id("doc")
            duplicate_of = seen.get(entry.sha256)
            if duplicate_of is None:
                seen[entry.sha256] = document_id
            document = OnboardingDocument(
                id=document_id,
                onboarding_id=onboarding_id,
                upload_id=upload_id,
                original_filename=entry.original_filename,
                internal_filename=entry.internal_filename,
                storage_path=entry.storage_path,
                sha256=entry.sha256,
                size_bytes=entry.size_bytes,
                mime_type=entry.mime_type,
                category="UNKNOWN",
                format="UNSUPPORTED" if entry.unsupported else entry.format,
                status="DUPLICATE" if duplicate_of else "DISCOVERED",
                error=(
                    "Identical to another file in this archive; retained once."
                    if duplicate_of else None
                ),
                duplicate_of_document_id=duplicate_of,
            )
            document_rows.append(document)

        processing_ids = [doc.id for doc in document_rows if doc.status != "DUPLICATE"]
        with Session(control_engine) as control:
            control.add(upload_row)
            # Flush the batch before its documents: SQLModel declares the foreign
            # key but no ORM relationship, so insertion order is the order rows are
            # added and the parent must exist first.
            control.flush()
            for document in document_rows:
                control.add(document)
            onboarding = control.get(OnboardingSession, onboarding_id)
            onboarding.status = ONBOARDING_PROCESSING
            onboarding.updated_at = datetime.now(UTC)
            control.add(onboarding)
            control.commit()

        for document_id in processing_ids:
            enqueue(control_engine, onboarding_id, JOB_DOCUMENT, document_id=document_id)
        if not processing_ids:
            enqueue(control_engine, onboarding_id, "FINALIZE_ONBOARDING")

        return _redirect(f"/onboarding/import/{onboarding_id}")

    # --- status and retries ------------------------------------------------

    @router.get("/onboarding/import/{onboarding_id}", response_class=HTMLResponse)
    def onboarding_status(
        onboarding_id: str,
        request: Request,
        session: Session = Depends(get_session_dep),
    ):
        onboarding = _owned_session(onboarding_id)
        view = _session_view(onboarding)
        documents = view["documents"]
        status_filter = request.query_params.get("doc_status", "all")
        if status_filter not in DOC_STATUS_FILTERS:
            status_filter = "all"
        query = request.query_params.get("q", "").strip()
        filtered_documents = _filter_documents(documents, status_filter, query)
        filter_counts = {
            key: len(_filter_documents(documents, key, ""))
            for key in DOC_STATUS_FILTERS
        }
        with Session(control_engine) as control:
            entries = control.exec(
                select(OnboardingProvisionEntry).where(
                    OnboardingProvisionEntry.onboarding_id == onboarding_id,
                    OnboardingProvisionEntry.status == "DONE",
                )
            ).all()
            created = {
                "properties": sum(1 for entry in entries if entry.entity_type == ENTITY_PROPERTY),
                "mortgages": sum(1 for entry in entries if entry.entity_type == ENTITY_MORTGAGE),
                "accounts": sum(1 for entry in entries if entry.entity_type == ENTITY_ACCOUNT),
                "leases": sum(1 for entry in entries if entry.entity_type == ENTITY_LEASE),
            }
        return templates.TemplateResponse(
            request=request,
            name="onboarding/status.html",
            context={
                "active_page": "onboarding",
                "document_rows": _document_rows(filtered_documents),
                "created": created,
                "doc_status": status_filter,
                "doc_query": query,
                "filter_counts": filter_counts,
                "filtered_count": len(filtered_documents),
                "documents_url": f"/onboarding/import/{onboarding_id}",
                "message": request.query_params.get("message"),
                "error": request.query_params.get("error"),
                **view,
            },
        )

    @router.post("/onboarding/import/{onboarding_id}/retry")
    def retry_failed(onboarding_id: str, session: Session = Depends(get_session_dep)):
        _require_work()
        _owned_session(onboarding_id)
        queued = 0
        retry_ids: list[str] = []
        with Session(control_engine) as control:
            documents = control.exec(
                select(OnboardingDocument).where(
                    OnboardingDocument.onboarding_id == onboarding_id,
                    OnboardingDocument.status == DOC_FAILED,
                )
            ).all()
            retry_ids = [document.id for document in documents]
            for document in documents:
                document.status = "DISCOVERED"
                document.error = None
                document.processed_at = None
                control.add(document)
            onboarding = control.get(OnboardingSession, onboarding_id)
            if onboarding is not None:
                onboarding.status = ONBOARDING_PROCESSING
                onboarding.last_error = None
                onboarding.updated_at = datetime.now(UTC)
                control.add(onboarding)
            control.commit()
        for document_id in retry_ids:
            if enqueue(control_engine, onboarding_id, JOB_DOCUMENT, document_id=document_id):
                queued += 1
        return _redirect(
            f"/onboarding/import/{onboarding_id}",
            message=f"{queued} document(s) queued again." if queued else "There was nothing to retry.",
        )

    @router.post("/onboarding/import/{onboarding_id}/documents/{document_id}/retry")
    def retry_document(
        onboarding_id: str,
        document_id: str,
        session: Session = Depends(get_session_dep),
    ):
        _require_work()
        _owned_session(onboarding_id)
        with Session(control_engine) as control:
            document = control.get(OnboardingDocument, document_id)
            if document is None or document.onboarding_id != onboarding_id:
                raise HTTPException(status_code=404, detail="Document not found.")
            if document.status not in {DOC_FAILED, DOC_NEEDS_REVIEW}:
                return _redirect(
                    f"/onboarding/import/{onboarding_id}",
                    error="That document is not in a retryable state.",
                )
            document_name = document.original_filename
            document.status = "DISCOVERED"
            document.error = None
            document.processed_at = None
            control.add(document)
            onboarding = control.get(OnboardingSession, onboarding_id)
            if onboarding is not None:
                onboarding.status = ONBOARDING_PROCESSING
                onboarding.updated_at = datetime.now(UTC)
                control.add(onboarding)
            control.commit()
        enqueue(control_engine, onboarding_id, JOB_DOCUMENT, document_id=document_id)
        return _redirect(
            f"/onboarding/import/{onboarding_id}",
            message=f"{document_name} queued again.",
        )

    # --- review ------------------------------------------------------------

    @router.get("/onboarding/import/{onboarding_id}/review", response_class=HTMLResponse)
    def portfolio_review(
        onboarding_id: str,
        request: Request,
        session: Session = Depends(get_session_dep),
    ):
        onboarding = _owned_session(onboarding_id)
        if onboarding.status not in REVIEWABLE_STATES and onboarding.status not in {
            ONBOARDING_APPROVED, ONBOARDING_COMPLETED, ONBOARDING_PROVISIONING,
            ONBOARDING_PROVISIONING_FAILED,
        }:
            return _redirect(f"/onboarding/import/{onboarding_id}")
        proposals = _proposals(onboarding_id)
        groups: dict[str, list[dict]] = {entity: [] for entity in ENTITY_ORDER}
        for proposal in proposals:
            payload = loads(proposal.payload_json)
            conflicts = loads(proposal.conflicts_json)
            if proposal.entity_type == ENTITY_PROPERTY:
                summary = ", ".join(
                    part for part in (payload.get("city"), payload.get("state")) if part
                )
            elif proposal.entity_type == ENTITY_MORTGAGE:
                summary = " · ".join(
                    part for part in (
                        payload.get("lender"),
                        f"Loan {payload['loan_number']}" if payload.get("loan_number") else "",
                        payload.get("property_address"),
                    ) if part
                )
            elif proposal.entity_type == ENTITY_ACCOUNT:
                summary = " · ".join(
                    part for part in (
                        payload.get("account_number_masked"),
                        payload.get("account_type", "").replace("_", " ").title(),
                    ) if part
                )
            else:
                summary = payload.get("property_address") or ""
            groups.setdefault(proposal.entity_type, []).append({
                "proposal": proposal,
                "summary": summary or "No summary available",
                "status_label": PROPOSAL_LABELS.get(proposal.status, proposal.status),
                "status_variant": PROPOSAL_VARIANTS.get(proposal.status, "neutral"),
                "conflicts": sum(len(options) for options in conflicts.values()),
                "sources": len(loads(proposal.source_document_ids_json, [])),
                "association": payload.get("association_status"),
                "entity_name": ENTITY_LABELS.get(proposal.entity_type, proposal.entity_type).lower(),
                "can_accept": (
                    proposal.status == "NEEDS_REVIEW"
                    and payload.get("association_status") not in {"AMBIGUOUS", "MISSING"}
                    and _acceptance_problem(proposal) is None
                ),
            })
        counts = review_counts(control_engine, onboarding_id)
        documents = _documents(onboarding_id)
        return templates.TemplateResponse(
            request=request,
            name="onboarding/review.html",
            context={
                "active_page": "onboarding",
                "session": onboarding,
                "groups": groups,
                "group_order": ENTITY_ORDER,
                "entity_labels": ENTITY_LABELS,
                "counts": counts,
                "journey_steps": journey_steps(ONBOARDING_REVIEWING),
                "documents": _document_rows(documents),
                "attention_documents": _document_rows(
                    [doc for doc in documents if doc.status in {DOC_FAILED, DOC_UNSUPPORTED, DOC_NEEDS_REVIEW}]
                ),
                "can_approve": onboarding.status in REVIEWABLE_STATES and counts["accepted"] > 0,
                "message": request.query_params.get("message"),
                "error": request.query_params.get("error"),
                "status_label": SESSION_LABELS.get(onboarding.status, onboarding.status),
                "status_variant": SESSION_VARIANTS.get(onboarding.status, "neutral"),
            },
        )

    def _record_fields(
        proposal: OnboardingProposal,
        property_titles: dict[str, str] | None = None,
    ) -> tuple[list[dict], dict]:
        property_titles = property_titles or {}

        def shown(name: str, value) -> str:
            if name == "property_group_key" and value in property_titles:
                return property_titles[value]
            return display_value(name, value)

        payload = loads(proposal.payload_json)
        original = loads(proposal.original_payload_json)
        edited = loads(proposal.edited_fields_json)
        conflicts = loads(proposal.conflicts_json)
        evidence = _evidence(proposal.id)
        fields: list[dict] = []
        for name in ordered_fields(proposal.entity_type, payload.keys()):
            value = payload[name]
            spec = field_spec(name)
            if name == "association_status":
                continue
            options = conflicts.get(name, [])
            fields.append({
                "name": name,
                "label": spec["label"],
                "type": spec["type"],
                "value": value,
                "display": shown(name, value),
                "input_value": (
                    f"{int(value) / 100:.2f}" if spec["type"] == "money" and value is not None
                    else f"{int(value) / 1000:.3f}" if spec["type"] == "percentage_rate" and value is not None
                    else ", ".join(str(item) for item in value) if spec["type"] == "list" and isinstance(value, list)
                    else str(value)[:10] if spec["type"] == "date" and value
                    else "" if value is None else str(value)
                ),
                "editable": editable(name),
                "choices": spec.get("choices", ()),
                "required": name in REQUIRED_FIELDS.get(proposal.entity_type, ()),
                "edited": name in edited,
                "original_display": shown(name, original.get(name)),
                "conflict": bool(options),
                "conflict_options": [
                    {
                        "index": index,
                        "display": shown(name, option.get("value")),
                        "document_id": option.get("document_id"),
                        "page": option.get("page"),
                        "evidence": option.get("evidence"),
                        "confidence": option.get("confidence"),
                    }
                    for index, option in enumerate(options)
                ],
                "evidence": evidence.get(name, []),
            })
        # Evidence-only fields (a default, or a value the extractor did not put in
        # the payload) are still shown, because the customer is entitled to see it.
        present = {field["name"] for field in fields}
        for name in evidence:
            if name in present:
                continue
            fields.append({
                "name": name,
                "label": field_spec(name)["label"],
                "type": field_spec(name)["type"],
                "value": None,
                "display": "—",
                "input_value": "",
                "editable": False,
                "choices": (),
                "required": False,
                "edited": False,
                "original_display": "—",
                "conflict": False,
                "conflict_options": [],
                "evidence": evidence[name],
            })
        return fields, payload

    @router.get("/onboarding/import/{onboarding_id}/records/{proposal_id}", response_class=HTMLResponse)
    def record_review(
        onboarding_id: str,
        proposal_id: str,
        request: Request,
        session: Session = Depends(get_session_dep),
    ):
        onboarding = _owned_session(onboarding_id)
        proposal = _proposal(onboarding_id, proposal_id)
        documents = {doc.id: doc for doc in _documents(onboarding_id)}
        property_titles = {
            other.group_key: other.title
            for other in _proposals(onboarding_id)
            if other.entity_type == ENTITY_PROPERTY
        }
        fields, payload = _record_fields(proposal, property_titles)
        source_ids = loads(proposal.source_document_ids_json, [])
        selected_id = request.query_params.get("doc") or (source_ids[0] if source_ids else None)
        selected = documents.get(selected_id) if selected_id else None
        page = request.query_params.get("page") or "1"
        source_documents = [
            {
                "document": documents[doc_id],
                "category_label": category_label(documents[doc_id].category),
            }
            for doc_id in source_ids
            if doc_id in documents
        ]
        # Property association choices are the other property proposals in this
        # session, so a mortgage can be pointed at the right door.
        property_options = []
        with Session(control_engine) as control:
            for other in control.exec(
                select(OnboardingProposal).where(
                    OnboardingProposal.onboarding_id == onboarding_id,
                    OnboardingProposal.entity_type == ENTITY_PROPERTY,
                )
            ).all():
                property_options.append({
                    "value": other.group_key,
                    "label": other.title,
                })
        return templates.TemplateResponse(
            request=request,
            name="onboarding/record.html",
            context={
                "active_page": "onboarding",
                "session": onboarding,
                "proposal": proposal,
                "entity_label": ENTITY_LABELS.get(proposal.entity_type, proposal.entity_type),
                "fields": fields,
                "payload": payload,
                "source_documents": source_documents,
                "selected_document": selected,
                "selected_page": page,
                "selected_is_pdf": bool(selected and selected.mime_type == "application/pdf"),
                "selected_is_image": bool(selected and selected.mime_type.startswith("image/")),
                "property_options": property_options,
                "status_label": PROPOSAL_LABELS.get(proposal.status, proposal.status),
                "status_variant": PROPOSAL_VARIANTS.get(proposal.status, "neutral"),
                "can_review": onboarding.status in REVIEWABLE_STATES,
                "can_decide": (
                    onboarding.status in REVIEWABLE_STATES
                    and proposal.status not in {"ACCEPTED", "REJECTED", "IMPORTED"}
                ),
                "message": request.query_params.get("message"),
                "error": request.query_params.get("error"),
            },
        )

    def _acceptance_problem(proposal: OnboardingProposal) -> str | None:
        conflicts = loads(proposal.conflicts_json)
        if conflicts:
            names = ", ".join(field_spec(name)["label"] for name in conflicts)
            return f"Resolve these conflicting fields first: {names}."
        payload = loads(proposal.payload_json)
        missing = missing_required(proposal.entity_type, payload)
        if missing:
            return "These required fields are missing: " + ", ".join(missing) + "."
        return None

    @router.post("/onboarding/import/{onboarding_id}/records/{proposal_id}/decision")
    def decide_record(
        onboarding_id: str,
        proposal_id: str,
        decision: str = Form(...),
        return_to: str = Form(""),
        session: Session = Depends(get_session_dep),
    ):
        _require_work()
        onboarding = _owned_session(onboarding_id)
        record_url = f"/onboarding/import/{onboarding_id}/records/{proposal_id}"
        # A quick action on the review list returns there; the record screen keeps
        # the record open. Only a local path inside this import is accepted.
        import_prefix = f"/onboarding/import/{onboarding_id}"
        return_url = (
            return_to
            if not return_to.startswith("//")
            and return_to in {import_prefix, f"{import_prefix}/review", f"{import_prefix}/approve"}
            else record_url
        )
        if onboarding.status not in REVIEWABLE_STATES:
            return _redirect(
                return_url,
                error="This session is no longer open for review.",
            )
        proposal = _proposal(onboarding_id, proposal_id)
        if decision == "ACCEPT":
            problem = _acceptance_problem(proposal)
            if problem:
                return _redirect(return_url, error=problem)
        try:
            record_decision(
                control_engine,
                proposal_id,
                actor_user_id=current_context().user.id,
                decision=decision,
                note="Record disposition",
            )
        except ValueError as exc:
            return _redirect(return_url, error=str(exc))
        label = {
            "ACCEPT": "accepted",
            "REJECT": "rejected",
            "DEFER": "deferred",
        }.get(decision, "updated")
        return _redirect(return_url, message=f"Record {label}.")

    def _field_json(proposal: OnboardingProposal, field: str, message: str) -> JSONResponse:
        """The same field update, shaped for an in-place browser update."""
        payload = loads(proposal.payload_json)
        original = loads(proposal.original_payload_json)
        property_titles = {
            other.group_key: other.title
            for other in _proposals(proposal.onboarding_id)
            if other.entity_type == ENTITY_PROPERTY
        }

        def shown(value):
            if field == "property_group_key" and value in property_titles:
                return property_titles[value]
            return display_value(field, value)

        return JSONResponse({
            "ok": True,
            "field": field,
            "display": shown(payload.get(field)),
            "original_display": shown(original.get(field)),
            "edited": True,
            "message": message,
        })

    @router.post("/onboarding/import/{onboarding_id}/records/{proposal_id}/field")
    def edit_field(
        onboarding_id: str,
        proposal_id: str,
        request: Request,
        field: str = Form(...),
        action: str = Form("save"),
        value: str = Form(""),
        value_index: str = Form(""),
        session: Session = Depends(get_session_dep),
    ):
        _require_work()
        wants_json = (
            request.headers.get("x-requested-with") == "fetch"
            or "application/json" in request.headers.get("accept", "")
        )
        onboarding = _owned_session(onboarding_id)
        if onboarding.status not in REVIEWABLE_STATES:
            message = "This session is no longer open for review."
            if wants_json:
                return JSONResponse({"ok": False, "error": message}, status_code=409)
            return _redirect(
                f"/onboarding/import/{onboarding_id}/records/{proposal_id}",
                error=message,
            )
        proposal = _proposal(onboarding_id, proposal_id)
        conflicts = loads(proposal.conflicts_json)
        return_url = f"/onboarding/import/{onboarding_id}/records/{proposal_id}"
        try:
            if action == "unknown":
                updated = record_decision(
                    control_engine, proposal_id,
                    actor_user_id=current_context().user.id,
                    decision="MARK_UNKNOWN", field_name=field,
                    note="Marked unknown by customer",
                )
                message = f"{field_spec(field)['label']} marked unknown."
                if wants_json:
                    return _field_json(updated, field, message)
                return _redirect(return_url, message=message)
            if action == "resolve":
                options = conflicts.get(field) or []
                try:
                    index = int(value_index)
                    value = jsonable(options[index]["value"])
                except (ValueError, IndexError, KeyError) as exc:
                    raise ValueError("Choose one of the conflicting values.") from exc
                decision = "RESOLVE_CONFLICT"
            else:
                value = parse_value(field, value)
                decision = "EDIT"
            updated = record_decision(
                control_engine, proposal_id,
                actor_user_id=current_context().user.id,
                decision=decision, field_name=field, value=value,
                note="Value edited by customer" if decision != "RESOLVE_CONFLICT" else "Conflict resolved",
            )
        except ValueError as exc:
            if wants_json:
                return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
            return _redirect(return_url, error=str(exc))
        message = f"{field_spec(field)['label']} updated."
        if wants_json:
            return _field_json(updated, field, message)
        return _redirect(return_url, message=message)

    # --- documents ---------------------------------------------------------

    @router.get("/onboarding/import/{onboarding_id}/documents/{document_id}", response_class=HTMLResponse)
    def document_inspector(
        onboarding_id: str,
        document_id: str,
        request: Request,
        session: Session = Depends(get_session_dep),
    ):
        _owned_session(onboarding_id)
        next_path = request.query_params.get("next", "")
        back_href = (
            next_path
            if next_path.startswith("/") and not next_path.startswith("//")
            else f"/onboarding/import/{onboarding_id}"
        )
        back_label = "Back to record" if "/records/" in back_href else "Back to import"
        with Session(control_engine) as control:
            document = control.get(OnboardingDocument, document_id)
            if document is None or document.onboarding_id != onboarding_id:
                raise HTTPException(status_code=404, detail="Document not found.")
            control.expunge(document)
        with Session(control_engine) as control:
            evidence = control.exec(
                select(OnboardingFieldEvidence)
                .where(OnboardingFieldEvidence.document_id == document_id)
                .order_by(OnboardingFieldEvidence.page_number)
            ).all()
            evidence_rows = [
                {
                    "field": field_spec(row.field_name)["label"],
                    "display": display_value(row.field_name, loads(row.value_json)),
                    "page": row.page_number,
                    "text": row.evidence_text,
                }
                for row in evidence
            ]
        with Session(control_engine) as control:
            proposals = control.exec(
                select(OnboardingProposal).where(OnboardingProposal.onboarding_id == onboarding_id)
            ).all()
            related = [
                proposal for proposal in proposals
                if document_id in loads(proposal.source_document_ids_json, [])
            ]
        return templates.TemplateResponse(
            request=request,
            name="onboarding/document.html",
            context={
                "active_page": "onboarding",
                "session": _owned_session(onboarding_id),
                "document": document,
                "document_label": DOCUMENT_LABELS.get(document.status, document.status),
                "document_variant": DOCUMENT_VARIANTS.get(document.status, "neutral"),
                "category_label": category_label(document.category),
                "format_label": FORMAT_LABELS.get(document.format, document.format.title()),
                "evidence": evidence_rows,
                "related": related,
                "page": request.query_params.get("page") or "1",
                "is_pdf": document.mime_type == "application/pdf",
                "is_image": document.mime_type.startswith("image/"),
                "size": human_size(document.size_bytes),
                "back_href": back_href,
                "back_label": back_label,
            },
        )

    @router.get("/onboarding/import/{onboarding_id}/documents/{document_id}/file")
    def document_file(
        onboarding_id: str,
        document_id: str,
        session: Session = Depends(get_session_dep),
    ):
        _owned_session(onboarding_id)
        with Session(control_engine) as control:
            document = control.get(OnboardingDocument, document_id)
            if document is None or document.onboarding_id != onboarding_id:
                raise HTTPException(status_code=404, detail="Document not found.")
            path_value = document.storage_path
            mime_type = document.mime_type
            filename = document.original_filename
        try:
            path = store.resolve(path_value)
        except ValueError:
            raise HTTPException(status_code=404, detail="Document not found.") from None
        if not path.is_file():
            raise HTTPException(status_code=404, detail="Document not found.")
        safe_name = filename.replace('"', "").replace("\n", " ")
        return FileResponse(
            path,
            media_type=mime_type,
            headers={"Content-Disposition": f'inline; filename="{safe_name}"'},
        )

    # --- approval and provisioning ----------------------------------------

    def _approval_problems(proposals: list[OnboardingProposal]) -> list[str]:
        problems: list[str] = []
        accepted = [proposal for proposal in proposals if proposal.status == PROPOSAL_ACCEPTED]
        if not accepted:
            return ["Accept at least one property, mortgage or account before importing."]
        accepted_properties = [
            loads(proposal.payload_json).get("property_group_key") or proposal.group_key
            for proposal in accepted
            if proposal.entity_type == ENTITY_PROPERTY
        ]
        if not accepted_properties:
            problems.append("Accept at least one property; a portfolio needs a door.")
        for proposal in accepted:
            problem = _acceptance_problem(proposal)
            if problem:
                problems.append(f"{ENTITY_LABELS.get(proposal.entity_type, proposal.entity_type)} — {proposal.title}: {problem}")
                continue
            payload = loads(proposal.payload_json)
            if proposal.entity_type in {ENTITY_MORTGAGE, ENTITY_LEASE}:
                if payload.get("property_group_key") not in accepted_properties:
                    problems.append(
                        f"{ENTITY_LABELS.get(proposal.entity_type, proposal.entity_type)} — "
                        f"{proposal.title}: linked property is not an accepted property."
                    )
        return problems

    @router.get("/onboarding/import/{onboarding_id}/approve", response_class=HTMLResponse)
    def approve_screen(
        onboarding_id: str,
        request: Request,
        session: Session = Depends(get_session_dep),
    ):
        onboarding = _owned_session(onboarding_id)
        if onboarding.status not in REVIEWABLE_STATES:
            return _redirect(f"/onboarding/import/{onboarding_id}")
        proposals = _proposals(onboarding_id)
        counts = review_counts(control_engine, onboarding_id)
        problems = _approval_problems(proposals) if counts["accepted"] else []
        return templates.TemplateResponse(
            request=request,
            name="onboarding/approve.html",
            context={
                "active_page": "onboarding",
                "session": onboarding,
                "counts": counts,
                "problems": problems,
                "approved": [p for p in proposals if p.status == PROPOSAL_ACCEPTED],
                "rejected": [p for p in proposals if p.status == PROPOSAL_REJECTED],
                "deferred": [p for p in proposals if p.status == PROPOSAL_DEFERRED],
                "undecided": [p for p in proposals if p.status == PROPOSAL_NEEDS_REVIEW],
                "entity_labels": ENTITY_LABELS,
                "can_confirm": bool(
                    current_context() and current_context().can_confirm_client
                ),
                "can_approve": not problems and counts["accepted"] > 0,
                "error": request.query_params.get("error"),
            },
        )

    @router.post("/onboarding/import/{onboarding_id}/approve")
    def approve_import(
        onboarding_id: str,
        confirm: str = Form(""),
        session: Session = Depends(get_session_dep),
    ):
        _require_owner()
        onboarding = _owned_session(onboarding_id)
        if onboarding.status not in REVIEWABLE_STATES:
            return _redirect(f"/onboarding/import/{onboarding_id}")
        if confirm not in {"yes", "on", "true"}:
            return _redirect(
                f"/onboarding/import/{onboarding_id}/approve",
                error="Confirm that the accepted records should become the portfolio.",
            )
        proposals = _proposals(onboarding_id)
        problems = _approval_problems(proposals)
        if problems:
            return _redirect(
                f"/onboarding/import/{onboarding_id}/approve",
                error=problems[0],
            )
        context = current_context()
        with Session(control_engine) as control:
            stored = control.get(OnboardingSession, onboarding_id)
            stored.status = ONBOARDING_APPROVED
            stored.approved_at = datetime.now(UTC)
            stored.approved_by_user_id = context.user.id
            stored.updated_at = datetime.now(UTC)
            control.add(stored)
            control.commit()
        enqueue(control_engine, onboarding_id, JOB_PROVISION)
        return _redirect(
            f"/onboarding/import/{onboarding_id}",
            message="Approved. PropTally is creating the portfolio.",
        )

    @router.post("/onboarding/import/{onboarding_id}/provision/retry")
    def retry_provisioning(onboarding_id: str, session: Session = Depends(get_session_dep)):
        _require_owner()
        onboarding = _owned_session(onboarding_id)
        if onboarding.status != ONBOARDING_PROVISIONING_FAILED:
            return _redirect(f"/onboarding/import/{onboarding_id}")
        with Session(control_engine) as control:
            stored = control.get(OnboardingSession, onboarding_id)
            stored.status = ONBOARDING_APPROVED
            stored.updated_at = datetime.now(UTC)
            control.add(stored)
            control.commit()
        enqueue(control_engine, onboarding_id, JOB_PROVISION)
        return _redirect(
            f"/onboarding/import/{onboarding_id}",
            message="Retrying the import. Completed records are not created again.",
        )

    @router.post("/onboarding/import/{onboarding_id}/cancel")
    def cancel_onboarding(onboarding_id: str, session: Session = Depends(get_session_dep)):
        _require_work()
        onboarding = _owned_session(onboarding_id)
        if onboarding.status in {ONBOARDING_COMPLETED, ONBOARDING_PROVISIONING}:
            return _redirect(
                f"/onboarding/import/{onboarding_id}",
                error="This import is already provisioning or complete.",
            )
        with Session(control_engine) as control:
            stored = control.get(OnboardingSession, onboarding_id)
            stored.status = ONBOARDING_CANCELLED
            stored.updated_at = datetime.now(UTC)
            control.add(stored)
            for job in control.exec(
                select(OnboardingJob).where(
                    OnboardingJob.onboarding_id == onboarding_id,
                    OnboardingJob.status == "PENDING",
                )
            ).all():
                job.status = "FAILED"
                job.error = "Cancelled by the customer."
                control.add(job)
            control.commit()
        store.remove_session(onboarding_id)
        return _redirect("/onboarding", message="The import was cancelled and its documents removed.")

    return router
