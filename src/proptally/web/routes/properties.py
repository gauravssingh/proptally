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
from proptally.web.form_parsing import cents as _cents
from proptally.web.helpers import format_cents, period_label
from proptally.web.property_forms import (
    FormError,
    address_tuple,
    apply_address,
    apply_property_location,
    safe_http_url,
)
from proptally.web.property_forms import (
    google_maps_url as get_google_maps_url,
)


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

        today = date.today()
        valuation_age_months = None
        if val and val.valuation_date:
            valuation_age_months = (
                (today.year - val.valuation_date.year) * 12
                + today.month
                - val.valuation_date.month
            )
            if today.day < val.valuation_date.day:
                valuation_age_months -= 1
            valuation_age_months = max(0, valuation_age_months)
        valuation_is_stale = (
            valuation_age_months is not None and valuation_age_months >= 12
        )

        equity_percent = None
        if val and val.estimated_value_cents and net_equity_cents is not None:
            equity_percent = round(
                net_equity_cents / val.estimated_value_cents * 100, 1
            )

        appreciation_cents = None
        appreciation_percent = None
        if val and prop.purchase_price_cents:
            appreciation_cents = (
                val.estimated_value_cents - prop.purchase_price_cents
            )
            if prop.purchase_price_cents > 0:
                appreciation_percent = round(
                    appreciation_cents / prop.purchase_price_cents * 100, 1
                )

        principal_repaid_cents = None
        principal_repaid_percent = None
        if loan and loan.original_principal_cents and authoritative_principal_cents is not None:
            principal_repaid_cents = max(
                0, loan.original_principal_cents - authoritative_principal_cents
            )
            principal_repaid_percent = round(
                principal_repaid_cents / loan.original_principal_cents * 100, 1
            )

        # Without an original principal, statement balances still show how much
        # principal has been retired since the first recorded statement.
        balance_periods = [
            period for period in mortgage_periods
            if period.outstanding_principal_cents is not None
        ]
        principal_progress_percent = None
        principal_paid_down_cents = None
        principal_start_cents = None
        principal_start_date = None
        principal_latest_date = None
        if balance_periods:
            first_period = min(balance_periods, key=lambda period: period.statement_date)
            last_period = max(balance_periods, key=lambda period: period.statement_date)
            principal_start_cents = first_period.outstanding_principal_cents
            principal_latest_date = last_period.statement_date
            principal_start_date = first_period.statement_date
            if principal_start_cents and principal_start_cents > 0:
                principal_paid_down_cents = max(
                    0, principal_start_cents - last_period.outstanding_principal_cents
                )
                principal_progress_percent = round(
                    principal_paid_down_cents / principal_start_cents * 100, 1
                )

        monthly_payment_cents = (
            latest_period.total_amount_billed_cents if latest_period else None
        )
        settled_periods = [
            period for period in mortgage_periods if period.status == "FULLY_SETTLED"
        ]
        last_payment_period = (
            max(settled_periods, key=lambda period: period.statement_date)
            if settled_periods
            else None
        )
        last_payment_cents = (
            last_payment_period.total_amount_applied_cents
            if last_payment_period
            else None
        )
        last_payment_date = (
            last_payment_period.payment_due_date if last_payment_period else None
        )
        unsettled_periods = [
            period for period in mortgage_periods if period.status != "FULLY_SETTLED"
        ]
        next_period = (
            min(unsettled_periods, key=lambda period: period.payment_due_date)
            if unsettled_periods
            else None
        )
        next_payment_due = next_period.payment_due_date if next_period else None
        next_payment_overdue = bool(
            next_payment_due is not None and next_payment_due < today
        )
        payments_recorded = len(settled_periods)

        instalments_paid = payments_recorded
        instalments_total = loan.term_months if loan else None
        instalments_remaining = (
            max(0, instalments_total - instalments_paid)
            if instalments_total
            else None
        )
        instalment_progress_percent = None
        if instalments_total and instalments_total > 0:
            instalment_progress_percent = min(
                100.0, round(instalments_paid / instalments_total * 100, 1)
            )
        instalments_over_term = bool(
            instalments_total and instalments_paid > instalments_total
        )

        rate_type = (loan.rate_type or "FIXED") if loan else None
        rate_type_label = (
            "Fixed" if rate_type == "FIXED"
            else "Adjustable (ARM)" if rate_type == "ARM"
            else rate_type
        )
        loan_start_month = loan.origination_date if loan else None
        loan_months_elapsed = None
        loan_month_number = None
        loan_months_remaining = None
        loan_schedule_percent = None
        if loan and loan.origination_date and loan.term_months:
            elapsed = (
                (today.year - loan.origination_date.year) * 12
                + today.month
                - loan.origination_date.month
            )
            if today.day < loan.origination_date.day:
                elapsed -= 1
            elapsed = max(0, min(loan.term_months, elapsed))
            loan_months_elapsed = elapsed
            loan_month_number = min(loan.term_months, elapsed + 1)
            loan_months_remaining = max(0, loan.term_months - elapsed)
            loan_schedule_percent = round(elapsed / loan.term_months * 100, 1)

        down_payment_cents = prop.down_payment_cents
        closing_costs_cents = prop.closing_costs_cents
        furnishing_costs_cents = prop.furnishing_costs_cents
        acquisition_costs_recorded = any(
            value is not None
            for value in (
                down_payment_cents,
                closing_costs_cents,
                furnishing_costs_cents,
            )
        )
        acquisition_cash_cents = (
            sum(
                value
                for value in (
                    down_payment_cents,
                    closing_costs_cents,
                    furnishing_costs_cents,
                )
                if value is not None
            )
            if acquisition_costs_recorded
            else None
        )
        total_acquisition_cost_cents = None
        if prop.purchase_price_cents is not None:
            total_acquisition_cost_cents = (
                prop.purchase_price_cents
                + (closing_costs_cents or 0)
                + (furnishing_costs_cents or 0)
            )

        address_full = ", ".join(
            part
            for part in (
                prop.address_line1,
                prop.address_line2,
                f"{prop.city}, {prop.state} {prop.zip_code}".strip(),
            )
            if part
        )
        address_short = ", ".join(
            part for part in (prop.address_line1, prop.city) if part
        )
        property_type_label = (
            "Rental investment"
            if prop.property_type == "RENTAL_INVESTMENT"
            else "Primary residence"
        )

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

        document_type_counts: dict[str, int] = {}
        for document in property_documents:
            key = document.document_type or "OTHER"
            document_type_counts[key] = document_type_counts.get(key, 0) + 1
        vault_filters = []
        for doc_type, label, icon in (
            ("", "All documents", "folder"),
            ("LEASE_AGREEMENT", "Lease agreements", "file_text"),
            ("MORTGAGE_STATEMENT", "Mortgage statements", "bank"),
            ("INSURANCE_POLICY", "Insurance policies", "shield"),
            ("PROPERTY_TAX", "Property tax", "file_text"),
            ("HOA_STATEMENT", "HOA documents", "file_text"),
        ):
            count = len(property_documents) if not doc_type else document_type_counts.get(doc_type, 0)
            if doc_type and not count:
                continue
            href = f"/documents?property={property_id}"
            if doc_type:
                href += f"&type={doc_type}"
            vault_filters.append({
                "type": doc_type,
                "label": label,
                "icon": icon,
                "count": count,
                "href": href,
            })

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
                "equity_percent": equity_percent,
                "valuation_age_months": valuation_age_months,
                "valuation_is_stale": valuation_is_stale,
                "appreciation_cents": appreciation_cents,
                "appreciation_percent": appreciation_percent,
                "principal_repaid_cents": principal_repaid_cents,
                "principal_repaid_percent": principal_repaid_percent,
                "principal_progress_percent": principal_progress_percent,
                "principal_paid_down_cents": principal_paid_down_cents,
                "principal_start_cents": principal_start_cents,
                "principal_start_date": principal_start_date,
                "principal_latest_date": principal_latest_date,
                "principal_statement_count": len(balance_periods),
                "monthly_payment_cents": monthly_payment_cents,
                "last_payment_cents": last_payment_cents,
                "last_payment_date": last_payment_date,
                "next_payment_due": next_payment_due,
                "next_payment_overdue": next_payment_overdue,
                "payments_recorded": payments_recorded,
                "instalments_paid": instalments_paid,
                "instalments_total": instalments_total,
                "instalments_remaining": instalments_remaining,
                "instalment_progress_percent": instalment_progress_percent,
                "instalments_over_term": instalments_over_term,
                "rate_type": rate_type,
                "rate_type_label": rate_type_label,
                "loan_start_month": loan_start_month,
                "loan_month_number": loan_month_number,
                "loan_months_elapsed": loan_months_elapsed,
                "loan_months_remaining": loan_months_remaining,
                "loan_schedule_percent": loan_schedule_percent,
                "down_payment_cents": down_payment_cents,
                "closing_costs_cents": closing_costs_cents,
                "furnishing_costs_cents": furnishing_costs_cents,
                "acquisition_costs_recorded": acquisition_costs_recorded,
                "acquisition_cash_cents": acquisition_cash_cents,
                "total_acquisition_cost_cents": total_acquisition_cost_cents,
                "is_mortgage_free": loan is None,
                "address_full": address_full,
                "address_short": address_short,
                "property_type_label": property_type_label,
                "lease": lease,
                "lease_document": lease_document,
                "property_documents": property_documents,
                "vault_filters": vault_filters,
                "detected_tenant": detected_tenant,
                "financials": financials,
                "review_count": review_count,
                "period_label": period_label(financials),
                "google_maps_url": maps_url,
                "photos": photos,
                "cover_photo": cover_photo,
                "error": request.query_params.get("error"),
            },
        )

    @router.post("/properties/{property_id}/update-details")
    async def update_property_details(
        property_id: str,
        request: Request,
        session: Session = Depends(get_session_dep),
    ):
        prop = session.get(Property, property_id)
        if not prop:
            raise HTTPException(status_code=404, detail="Property not found")

        form = await request.form()
        previous_address = address_tuple(prop)
        previous_place_id = prop.google_place_id
        try:
            # Parse and validate the whole form before changing the row, so a bad
            # address or location cannot leave the notes half-updated. A form that
            # carries only notes (an older caller) leaves address and location alone.
            if "address_line1" in form:
                apply_address(prop, form)
            apply_property_location(
                prop,
                form,
                previous_address=previous_address,
                previous_place_id=previous_place_id,
            )
            if any(
                field in form
                for field in ("down_payment", "closing_costs", "furnishing_costs")
            ):
                prop.down_payment_cents = _cents(
                    form.get("down_payment"), "Down payment"
                )
                prop.closing_costs_cents = _cents(
                    form.get("closing_costs"), "Closing costs"
                )
                prop.furnishing_costs_cents = _cents(
                    form.get("furnishing_costs"), "Furnishing costs"
                )
            description = (form.get("description") or "").strip()
            custom_maps_url = (form.get("google_maps_url") or "").strip()
            if custom_maps_url and safe_http_url(custom_maps_url) is None:
                raise FormError("Google Maps link must start with http:// or https://.")
        except FormError as exc:
            session.rollback()
            return RedirectResponse(
                url=f"/properties/{property_id}?error={urllib.parse.quote(str(exc))}",
                status_code=303,
            )

        prop.description = description or None
        prop.google_maps_url = custom_maps_url or None
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
                    parsed = _cents(monthly_rent_dollars, "Monthly rent")
                    if parsed is not None:
                        existing_lease.monthly_rent_cents = parsed
                if security_deposit_dollars and security_deposit_dollars.strip():
                    existing_lease.security_deposit_cents = _cents(
                        security_deposit_dollars, "Security deposit"
                    )
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
                rent_cents = _cents(monthly_rent_dollars, "Monthly rent") or 0
                dep_cents = _cents(security_deposit_dollars, "Security deposit")
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
            rent_cents = _cents(monthly_rent_dollars, "Monthly rent")
            if rent_cents is None:
                raise FormError("Monthly rent is required.")
            dep_cents = _cents(security_deposit_dollars, "Security deposit")
            s_date = date.fromisoformat(start_date.strip())
            e_date = date.fromisoformat(end_date.strip()) if end_date and end_date.strip() else None
        except (ValueError, FormError) as err:
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
