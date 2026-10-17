import re
import urllib.parse
from datetime import date
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from sqlmodel import Session, select

from proptally.db.models import (
    Document,
    EscrowAccount,
    EscrowMovement,
    FinancialAccount,
    FinancialInstitution,
    JournalEntry,
    JournalEntryLine,
    Lease,
    LoanAccount,
    MortgagePeriod,
    Property,
    PropertyPhoto,
    PropertyValuation,
    Transaction,
    TransactionProcessingState,
)
from proptally.domain.accounting.metrics import calculate_property_financials
from proptally.domain.transactions.suggest import suggest_split_for_transaction
from proptally.ingestion.service import MAX_UPLOAD_BYTES, store_document
from proptally.web.auth import current_context
from proptally.web.helpers import format_cents, period_label


def _tenant_photos_dir() -> Path:
    """The signed-in portfolio's photo store.

    Photos are financial records, so they live inside the portfolio's own directory
    and are served through an authenticated route; there is no process-wide
    photo directory that any request could reach.
    """
    context = current_context()
    if context is None:
        raise HTTPException(status_code=503, detail="No portfolio context for this request.")
    return context.paths.photos


def get_google_maps_url(prop: Property) -> str:
    """Return configured or auto-detected Google Maps URL for property."""
    if prop.google_maps_url and prop.google_maps_url.strip():
        return prop.google_maps_url.strip()
    parts = [prop.address_line1]
    if prop.address_line2:
        parts.append(prop.address_line2)
    parts.append(f"{prop.city}, {prop.state} {prop.zip_code}")
    full_address = ", ".join(p for p in parts if p)
    return f"https://www.google.com/maps/search/?api=1&query={urllib.parse.quote_plus(full_address)}"


#: Split categories that represent rent received on a door.
RENT_INFLOW_CATEGORIES = ("RENT_INCOME", "RENT", "LATE_FEE", "FEE")


def _counterparty_named_by_bank(raw_description: str) -> str:
    """The remitter a bank record actually names, or "" when it names nobody.

    Only explicit remitter wording is accepted (``ZELLE TRANSFER FROM JANE DOE``).
    An ATM deposit or a card settlement names no tenant, and reporting one would be
    a claim the bank record does not support.
    """
    match = re.search(r"\bFROM\s+([A-Z][A-Z'.\- ]{2,40})", raw_description.upper())
    return match.group(1).strip(" .-") if match else ""


def infer_unverified_tenant(session: Session, property_id: str, lease: Lease | None) -> dict | None:
    """Summarises rent-like inflows on a door that have no lease behind them.

    Returns ``None`` when a lease is registered (the lease is then the authority) or
    when the door has no unconfirmed rent-like inflow. Which inflows belong to this
    door comes from the same classification engine the review queue uses, so a
    portfolio's own vendor rules decide it; nothing here is tied to a particular
    client, tenant or deposit pattern. The counterparty is whatever the bank record
    says, and is left empty when the record names nobody.
    """
    if lease is not None:
        return None

    rows = session.exec(
        select(Transaction, TransactionProcessingState)
        .join(
            TransactionProcessingState,
            TransactionProcessingState.transaction_id == Transaction.id,
        )
        .where(Transaction.amount_cents > 0)
        .where(TransactionProcessingState.posting_status != "POSTED")
        .order_by(Transaction.transaction_date)
    ).all()

    matched = []
    for txn, state in rows:
        if state.allocation_status == "FULLY_ALLOCATED":
            continue
        suggestion = suggest_split_for_transaction(
            txn.raw_description, txn.amount_cents, session=session
        )
        if suggestion is None or suggestion.property_id != property_id:
            continue
        if str(suggestion.category).upper() not in RENT_INFLOW_CATEGORIES:
            continue
        matched.append(txn)

    if not matched:
        return None

    total_cents = sum(txn.amount_cents for txn in matched)
    named = next(
        (name for name in (_counterparty_named_by_bank(t.raw_description) for t in matched) if name),
        "",
    )
    sources = []
    for txn in matched:
        if txn.raw_description not in sources:
            sources.append(txn.raw_description)

    return {
        "name": named,
        "status": "UNVERIFIED",
        "is_unverified": True,
        "monthly_rent_cents": None,
        "payment_summary": f"{len(matched)} unconfirmed rent deposit(s), {format_cents(total_cents)} total",
        "source": "Unconfirmed rent-like inflows: " + "; ".join(sources),
    }


def get_properties_router(templates, get_session_dep):
    router = APIRouter()

    @router.get("/properties")
    def list_properties():
        return RedirectResponse(url="/", status_code=303)

    @router.get("/properties/{property_id}", response_class=HTMLResponse)
    def view_property_detail(property_id: str, request: Request, session: Session = Depends(get_session_dep)):
        prop = session.get(Property, property_id)
        if not prop:
            raise HTTPException(status_code=404, detail=f"Property {property_id} not found")

        val = session.exec(
            select(PropertyValuation)
            .where(PropertyValuation.property_id == property_id)
            .order_by(PropertyValuation.valuation_date.desc())
        ).first()

        loan = session.exec(
            select(LoanAccount).where(LoanAccount.property_id == property_id, LoanAccount.is_active == True)
        ).first()

        escrow = None
        institution_name = "Mortgage Servicer"
        mortgage_periods = []
        latest_period = None
        servicer_reported_principal_cents = None
        ledger_calculated_principal_cents = None
        reported_escrow_balance_cents = None
        calculated_escrow_balance_cents = 0

        if loan:
            fin_acct = session.get(FinancialAccount, loan.financial_account_id) if loan.financial_account_id else None
            inst = session.get(FinancialInstitution, fin_acct.institution_id) if fin_acct else None
            if inst:
                institution_name = inst.name

            escrow = session.exec(
                select(EscrowAccount).where(EscrowAccount.loan_account_id == loan.id)
            ).first()

            mortgage_periods = session.exec(
                select(MortgagePeriod)
                .where(MortgagePeriod.loan_account_id == loan.id)
                .order_by(MortgagePeriod.statement_date.desc())
            ).all()

            if mortgage_periods:
                latest_period = mortgage_periods[0]
                servicer_reported_principal_cents = latest_period.outstanding_principal_cents
                reported_escrow_balance_cents = latest_period.reported_escrow_balance_cents

            # Ledger-calculated principal from POSTED journal lines
            code = loan.principal_ledger_code
            if code is not None:
                posted_lines = session.exec(
                    select(JournalEntryLine)
                    .join(JournalEntry, JournalEntryLine.journal_entry_id == JournalEntry.id)
                    .where(JournalEntry.status == "POSTED")
                    .where(JournalEntryLine.ledger_account_code == code)
                ).all()
                if posted_lines:
                    ledger_calculated_principal_cents = max(
                        0, sum(l.credit_cents for l in posted_lines) - sum(l.debit_cents for l in posted_lines)
                    )

            # Escrow calculated balance
            movements = []
            if escrow:
                movements = session.exec(
                    select(EscrowMovement)
                    .where(EscrowMovement.escrow_account_id == escrow.id)
                    .order_by(EscrowMovement.movement_date.desc())
                ).all()
            if movements:
                calculated_escrow_balance_cents = movements[0].running_calculated_balance_cents
            elif escrow:
                calculated_escrow_balance_cents = escrow.opening_balance_cents

        authoritative_principal_cents = (
            servicer_reported_principal_cents
            if servicer_reported_principal_cents is not None
            else ledger_calculated_principal_cents
        )

        net_equity_cents = None
        ltv_percent = None
        if val and val.estimated_value_cents:
            if authoritative_principal_cents is not None:
                net_equity_cents = val.estimated_value_cents - authoritative_principal_cents
                if val.estimated_value_cents > 0:
                    ltv_percent = round((authoritative_principal_cents / val.estimated_value_cents) * 100, 1)
            else:
                net_equity_cents = val.estimated_value_cents
                ltv_percent = 0.0

        lease = session.exec(
            select(Lease).where(Lease.property_id == property_id, Lease.is_active == True)
        ).first()

        financials = calculate_property_financials(session, property_id=property_id)

        unallocated_states = session.exec(
            select(TransactionProcessingState).where(
                TransactionProcessingState.allocation_status != "FULLY_ALLOCATED"
            )
        ).all()
        review_count = len(unallocated_states)

        maps_url = get_google_maps_url(prop)
        photos = session.exec(
            select(PropertyPhoto)
            .where(PropertyPhoto.property_id == property_id)
            .order_by(PropertyPhoto.is_cover.desc(), PropertyPhoto.created_at.desc())
        ).all()

        cover_photo = None
        if prop.cover_photo_id:
            cover_photo = next((p for p in photos if p.id == prop.cover_photo_id), None)
        if not cover_photo and photos:
            cover_photo = next((p for p in photos if p.is_cover), photos[0])

        property_documents = session.exec(
            select(Document)
            .where(Document.property_id == property_id)
            .order_by(Document.uploaded_at.desc())
        ).all()

        lease_document = None
        if lease and lease.document_id:
            lease_document = session.get(Document, lease.document_id)
        if not lease_document and property_documents:
            lease_document = next((d for d in property_documents if d.document_type == "LEASE_AGREEMENT"), None)

        # An unverified tenant is inferred from this door's own bank activity,
        # never assumed from a fixture. See infer_unverified_tenant.
        detected_tenant = infer_unverified_tenant(session, property_id, lease)

        return templates.TemplateResponse(
            request=request,
            name="property_detail.html",
            context={
                "active_page": "properties",
                "active_property": property_id,
                "property": prop,
                "valuation": val,
                "loan": loan,
                "institution_name": institution_name,
                "escrow": escrow,
                "latest_period": latest_period,
                "mortgage_periods": mortgage_periods,
                "servicer_reported_principal_cents": servicer_reported_principal_cents,
                "ledger_calculated_principal_cents": ledger_calculated_principal_cents,
                "authoritative_principal_cents": authoritative_principal_cents,
                "reported_escrow_balance_cents": reported_escrow_balance_cents,
                "calculated_escrow_balance_cents": calculated_escrow_balance_cents,
                "net_equity_cents": net_equity_cents,
                "ltv_percent": ltv_percent,
                "lease": lease,
                "lease_document": lease_document,
                "property_documents": property_documents,
                "detected_tenant": detected_tenant,
                "financials": financials,
                "review_count": review_count,
                "period_label": period_label(financials),
                "google_maps_url": maps_url,
                "photos": photos,
                "cover_photo": cover_photo,
            },
        )

    @router.post("/properties/{property_id}/update-details")
    def update_property_details(
        property_id: str,
        description: str = Form(None),
        google_maps_url: str = Form(None),
        session: Session = Depends(get_session_dep),
    ):
        prop = session.get(Property, property_id)
        if not prop:
            raise HTTPException(status_code=404, detail="Property not found")
        prop.description = description.strip() if description and description.strip() else None
        prop.google_maps_url = google_maps_url.strip() if google_maps_url and google_maps_url.strip() else None
        session.add(prop)
        session.commit()
        return RedirectResponse(url=f"/properties/{property_id}", status_code=303)

    @router.post("/properties/{property_id}/photos/upload")
    async def upload_property_photo(
        property_id: str,
        photo: UploadFile = File(...),
        caption: str = Form(None),
        is_cover: bool = Form(False),
        session: Session = Depends(get_session_dep),
    ):
        prop = session.get(Property, property_id)
        if not prop:
            raise HTTPException(status_code=404, detail="Property not found")

        if not photo.filename:
            raise HTTPException(status_code=400, detail="No file selected")

        ext = Path(photo.filename).suffix.lower()
        if ext not in (".jpg", ".jpeg", ".png", ".webp", ".heic", ".avif", ".gif"):
            raise HTTPException(status_code=400, detail="Only image files are permitted (JPEG, PNG, WebP, HEIC).")

        prop_photos_dir = _tenant_photos_dir() / property_id
        prop_photos_dir.mkdir(parents=True, exist_ok=True)

        photo_id = f"photo_{uuid4().hex[:12]}"
        safe_name = f"{photo_id}{ext}"
        destination = prop_photos_dir / safe_name

        content = await photo.read()
        if len(content) > 15 * 1024 * 1024:  # 15 MB limit
            raise HTTPException(status_code=400, detail="Photo file too large (max 15MB).")

        with destination.open("wb") as buffer:
            buffer.write(content)

        existing_photos = session.exec(
            select(PropertyPhoto).where(PropertyPhoto.property_id == property_id)
        ).all()
        should_be_cover = is_cover or len(existing_photos) == 0

        if should_be_cover:
            for p in existing_photos:
                p.is_cover = False
                session.add(p)

        photo_record = PropertyPhoto(
            id=photo_id,
            property_id=property_id,
            filename=photo.filename,
            storage_path=f"/photos/{property_id}/{safe_name}",
            caption=caption.strip() if caption and caption.strip() else None,
            is_cover=should_be_cover,
        )
        session.add(photo_record)
        if should_be_cover:
            prop.cover_photo_id = photo_id
            session.add(prop)

        session.commit()
        return RedirectResponse(url=f"/properties/{property_id}", status_code=303)

    @router.post("/properties/{property_id}/photos/{photo_id}/cover")
    def set_cover_photo(
        property_id: str,
        photo_id: str,
        session: Session = Depends(get_session_dep),
    ):
        prop = session.get(Property, property_id)
        if not prop:
            raise HTTPException(status_code=404, detail="Property not found")

        photos = session.exec(
            select(PropertyPhoto).where(PropertyPhoto.property_id == property_id)
        ).all()
        target = next((p for p in photos if p.id == photo_id), None)
        if not target:
            raise HTTPException(status_code=404, detail="Photo not found")

        for p in photos:
            p.is_cover = (p.id == photo_id)
            session.add(p)

        prop.cover_photo_id = photo_id
        session.add(prop)
        session.commit()
        return RedirectResponse(url=f"/properties/{property_id}", status_code=303)

    @router.post("/properties/{property_id}/photos/{photo_id}/delete")
    def delete_photo(
        property_id: str,
        photo_id: str,
        session: Session = Depends(get_session_dep),
    ):
        prop = session.get(Property, property_id)
        photo = session.get(PropertyPhoto, photo_id)
        if photo and photo.property_id == property_id:
            relative_name = photo.storage_path.removeprefix("/photos/")
            disk_path = _tenant_photos_dir() / relative_name
            disk_path.unlink(missing_ok=True)

            session.delete(photo)
            if prop and prop.cover_photo_id == photo_id:
                prop.cover_photo_id = None
                remaining = session.exec(
                    select(PropertyPhoto)
                    .where(PropertyPhoto.property_id == property_id, PropertyPhoto.id != photo_id)
                    .order_by(PropertyPhoto.created_at.desc())
                ).first()
                if remaining:
                    remaining.is_cover = True
                    prop.cover_photo_id = remaining.id
                    session.add(remaining)
                session.add(prop)
            session.commit()
        return RedirectResponse(url=f"/properties/{property_id}", status_code=303)

    @router.get("/documents/{document_id}/view")
    def view_document(
        document_id: str,
        session: Session = Depends(get_session_dep),
    ):
        doc = session.get(Document, document_id)
        if not doc or not Path(doc.storage_path).exists():
            raise HTTPException(status_code=404, detail="Document not found")
        # Viewing, not downloading: the browser renders it in place, which is what the
        # inspector's preview and the "Open" action both rely on.
        return FileResponse(
            path=doc.storage_path,
            media_type=doc.mime_type or "application/pdf",
            content_disposition_type="inline",
        )

    @router.get("/documents/{document_id}/download")
    def download_document(
        document_id: str,
        session: Session = Depends(get_session_dep),
    ):
        doc = session.get(Document, document_id)
        if not doc or not Path(doc.storage_path).exists():
            raise HTTPException(status_code=404, detail="Document not found")
        return FileResponse(
            path=doc.storage_path,
            media_type="application/octet-stream",
            filename=doc.original_filename,
        )

    @router.post("/properties/{property_id}/documents/upload")
    async def upload_property_document(
        property_id: str,
        file: UploadFile = File(...),
        document_type: str = Form("OTHER"),
        notes: str = Form(None),
        tenant_name: str = Form(None),
        monthly_rent_dollars: str = Form(None),
        security_deposit_dollars: str = Form(None),
        start_date: str = Form(None),
        end_date: str = Form(None),
        session: Session = Depends(get_session_dep),
    ):
        prop = session.get(Property, property_id)
        if not prop:
            raise HTTPException(status_code=404, detail="Property not found")

        if not file.filename:
            raise HTTPException(status_code=400, detail="No file selected")

        content = await file.read(MAX_UPLOAD_BYTES + 1)
        try:
            stored = store_document(
                session,
                current_context().paths.vault,
                payload=content,
                original_filename=file.filename,
                document_type=document_type,
                property_id=property_id,
                parse=False,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        doc = stored.document
        if not stored.created:
            # The bytes are already in this portfolio's vault; the upload still files
            # them against this door rather than storing the same content twice.
            if not doc.property_id:
                doc.property_id = property_id
            if not doc.document_type or doc.document_type == "OTHER":
                doc.document_type = document_type
            session.add(doc)

        if document_type == "LEASE_AGREEMENT":
            existing_lease = session.exec(
                select(Lease).where(Lease.property_id == property_id, Lease.is_active == True)
            ).first()
            if existing_lease:
                existing_lease.document_id = doc.id
                if tenant_name and tenant_name.strip():
                    existing_lease.tenant_name = tenant_name.strip()
                if monthly_rent_dollars and monthly_rent_dollars.strip():
                    try:
                        existing_lease.monthly_rent_cents = int(round(float(monthly_rent_dollars.strip().replace("$", "").replace(",", "")) * 100))
                    except ValueError:
                        pass
                if security_deposit_dollars and security_deposit_dollars.strip():
                    try:
                        existing_lease.security_deposit_cents = int(round(float(security_deposit_dollars.strip().replace("$", "").replace(",", "")) * 100))
                    except ValueError:
                        pass
                if start_date and start_date.strip():
                    try:
                        existing_lease.start_date = date.fromisoformat(start_date.strip())
                    except ValueError:
                        pass
                if end_date and end_date.strip():
                    try:
                        existing_lease.end_date = date.fromisoformat(end_date.strip())
                    except ValueError:
                        pass
                session.add(existing_lease)
            elif tenant_name and tenant_name.strip():
                try:
                    rent_cents = int(round(float(monthly_rent_dollars.strip().replace("$", "").replace(",", "")) * 100)) if monthly_rent_dollars else 0
                    dep_cents = int(round(float(security_deposit_dollars.strip().replace("$", "").replace(",", "")) * 100)) if security_deposit_dollars else rent_cents
                    s_date = date.fromisoformat(start_date.strip()) if start_date and start_date.strip() else date(2026, 1, 1)
                    e_date = date.fromisoformat(end_date.strip()) if end_date and end_date.strip() else None
                    new_lease = Lease(
                        id=str(uuid4()),
                        property_id=property_id,
                        tenant_name=tenant_name.strip(),
                        start_date=s_date,
                        end_date=e_date,
                        monthly_rent_cents=rent_cents,
                        security_deposit_cents=dep_cents,
                        document_id=doc.id,
                        is_active=True,
                    )
                    session.add(new_lease)
                except Exception:
                    pass

        session.commit()
        return RedirectResponse(url=f"/properties/{property_id}#documents-vault", status_code=303)

    @router.post("/properties/{property_id}/lease/update")
    def update_property_lease(
        property_id: str,
        tenant_name: str = Form(...),
        monthly_rent_dollars: str = Form(...),
        security_deposit_dollars: str = Form(None),
        start_date: str = Form(...),
        end_date: str = Form(None),
        is_active: bool = Form(True),
        session: Session = Depends(get_session_dep),
    ):
        prop = session.get(Property, property_id)
        if not prop:
            raise HTTPException(status_code=404, detail="Property not found")

        try:
            rent_cents = int(round(float(monthly_rent_dollars.strip().replace("$", "").replace(",", "")) * 100))
            dep_cents = (
                int(round(float(security_deposit_dollars.strip().replace("$", "").replace(",", "")) * 100))
                if security_deposit_dollars and security_deposit_dollars.strip()
                else rent_cents
            )
            s_date = date.fromisoformat(start_date.strip())
            e_date = date.fromisoformat(end_date.strip()) if end_date and end_date.strip() else None
        except ValueError as err:
            raise HTTPException(status_code=400, detail=f"Invalid lease values: {err}")

        existing_lease = session.exec(
            select(Lease).where(Lease.property_id == property_id, Lease.is_active == True)
        ).first()

        if existing_lease:
            existing_lease.tenant_name = tenant_name.strip()
            existing_lease.monthly_rent_cents = rent_cents
            existing_lease.security_deposit_cents = dep_cents
            existing_lease.start_date = s_date
            existing_lease.end_date = e_date
            existing_lease.is_active = is_active
            session.add(existing_lease)
        else:
            new_lease = Lease(
                id=str(uuid4()),
                property_id=property_id,
                tenant_name=tenant_name.strip(),
                start_date=s_date,
                end_date=e_date,
                monthly_rent_cents=rent_cents,
                security_deposit_cents=dep_cents,
                is_active=is_active,
            )
            session.add(new_lease)

        session.commit()
        return RedirectResponse(url=f"/properties/{property_id}#tenant-lease", status_code=303)

    return router
