"""Reconciliation and Review Queue web router.

Governance is owned by the domain policy module. This router evaluates that
policy before mutating anything, so a request without the required
authorization changes nothing, and it passes the caller's authorizations
straight through to the domain posting service as a second line of defence.
"""

import math
from typing import Optional
from urllib.parse import urlencode
from uuid import uuid4

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func
from sqlmodel import Session, select

from proptally.core.exceptions import (
    DuplicatePostingError,
    GovernanceAuthorizationError,
    InvalidSplitSumError,
)
from proptally.db.models import (
    FinancialAccount,
    Property,
    Transaction,
    TransactionProcessingState,
    TransactionSplit,
    VendorRule,
)
from proptally.domain.accounting.controls import build_reconciliation_report
from proptally.domain.transactions.policy import (
    assert_posting_authorized,
    classification_status_for,
    evaluate_posting_policy,
)
from proptally.domain.transactions.rent import (
    RentApplyReport,
    apply_rent_row,
    apply_rent_selection,
    dismiss_rent_candidate,
    dismissed_rent_transactions,
    find_rent_candidates,
    restore_rent_candidate,
)
from proptally.domain.transactions.splits import (
    SplitItem,
    apply_transaction_splits,
    post_transaction_to_ledger,
)
from proptally.domain.transactions.suggest import suggest_split_for_transaction
from proptally.web.auth import posting_actor_kwargs, validate_authorizations


def find_matching_vendor_rule(rules: list[VendorRule], pattern_or_name: Optional[str]) -> Optional[VendorRule]:
    """Finds the most specific active vendor rule matching a query string.

    Lookup precedence:
    1. Exact match on rule.pattern (case-insensitive)
    2. Exact match on rule.vendor_name (case-insensitive)
    3. Substring match on rule.pattern (longest pattern first)
    4. Substring match on rule.vendor_name (longest name first)
    """
    if not pattern_or_name or not pattern_or_name.strip():
        return None
    clean = pattern_or_name.strip().upper()

    # 1. Exact match on pattern
    for r in rules:
        if r.pattern.strip().upper() == clean:
            return r

    # 2. Exact match on vendor_name
    for r in rules:
        if (r.vendor_name or "").strip().upper() == clean:
            return r

    # 3. Substring match on pattern: query contains pattern or pattern contains query
    sorted_by_pat = sorted(rules, key=lambda r: len(r.pattern), reverse=True)
    for r in sorted_by_pat:
        r_pat = r.pattern.strip().upper()
        if r_pat in clean or clean in r_pat:
            return r

    # 4. Substring match on vendor_name
    sorted_by_vnd = sorted(rules, key=lambda r: len(r.vendor_name or ""), reverse=True)
    for r in sorted_by_vnd:
        r_vnd = (r.vendor_name or "").strip().upper()
        if r_vnd and (r_vnd in clean or clean in r_vnd):
            return r

    return None


def _posting_http_error(exc: Exception) -> HTTPException:
    """Map a posting refusal onto the status the operator should see."""
    if isinstance(exc, GovernanceAuthorizationError):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, InvalidSplitSumError):
        return HTTPException(status_code=400, detail=str(exc))
    # DuplicatePostingError, "already POSTED", and any other allocation conflict:
    # the transaction cannot be posted as asked, and saying so is not a crash.
    return HTTPException(status_code=409, detail=str(exc))


def _queue_notice(notice: Optional[str], applied: Optional[int]) -> tuple[Optional[str], str]:
    """The toast a queue action leaves behind, derived from its redirect token."""
    count = applied or 0
    plural = "" if count == 1 else "s"
    if notice == "assign":
        return ("Assigned and posted to the ledger.", "success")
    if notice == "split":
        return ("Split saved and posted to the ledger.", "success")
    if notice == "suggestion":
        return ("Suggestion applied and posted to the ledger.", "success")
    if notice == "rule":
        return ("Vendor rule saved.", "success")
    if notice == "batch":
        if count:
            return (f"Vendor rule applied; posted {count} transaction{plural} to the ledger.", "success")
        return (
            "Vendor rule applied; matching transactions were classified and now need a sign-off.",
            "info",
        )
    return (None, "success")


def _rent_notice(params) -> tuple[Optional[str], str]:
    """The toast a rent-classification run leaves behind."""
    notice = (params.get("notice") or "").strip()
    if notice == "dismissed":
        return (
            "Removed from rent candidates; the transaction stays in the review queue.",
            "success",
        )
    if notice == "restored":
        return ("Restored to rent candidates.", "success")

    try:
        posted = int(params.get("posted") or 0)
        allocated = int(params.get("allocated") or 0)
        skipped = int(params.get("skipped") or 0)
        errors = int(params.get("error") or 0)
    except (TypeError, ValueError):
        return (None, "success")
    if not any((posted, allocated, skipped, errors)):
        return (None, "success")

    parts = []
    if posted:
        parts.append(f"posted {posted} payment{'' if posted == 1 else 's'}")
    if allocated:
        parts.append(f"allocated {allocated} awaiting a sign-off")
    if skipped:
        parts.append(f"left {skipped} in the queue")
    if errors:
        parts.append(f"{errors} failed")
    variant = "error" if errors and not posted else ("warning" if not posted else "success")
    return ("Rent received: " + "; ".join(parts) + ".", variant)


def _rent_redirect(report: RentApplyReport):
    params = urlencode({
        "posted": len(report.posted),
        "allocated": len(report.allocated),
        "skipped": len(report.skipped),
        "error": len(report.errors),
    })
    return RedirectResponse(url=f"/reconciliation/rent?{params}", status_code=303)


def get_reconciliation_router(templates, get_session_dep):
    router = APIRouter()

    @router.get("/reconciliation/report", response_class=HTMLResponse)
    def view_reconciliation_report(
        request: Request,
        session: Session = Depends(get_session_dep),
    ):
        report = build_reconciliation_report(session)
        review_count = session.exec(
            select(func.count(TransactionProcessingState.transaction_id)).where(
                TransactionProcessingState.allocation_status == "UNALLOCATED"
            )
        ).one()
        return templates.TemplateResponse(
            request=request,
            name="reconciliation_report.html",
            context={
                "active_page": "report",
                "report": report,
                "review_count": int(review_count or 0),
            },
        )

    @router.get("/reconciliation", response_class=HTMLResponse)
    def view_reconciliation(
        request: Request,
        q: Optional[str] = Query(None),
        account_id: Optional[str] = Query(None),
        vendor_pattern: Optional[str] = Query(None),
        gate: Optional[str] = Query(None),
        direction: Optional[str] = Query(None),
        page: int = Query(default=1, ge=1),
        per_page: int = Query(default=25, ge=1, le=500),
        applied: Optional[int] = Query(None),
        notice: Optional[str] = Query(None),
        posted: Optional[int] = Query(None),
        held: Optional[int] = Query(None),
        skipped: Optional[int] = Query(None),
        session: Session = Depends(get_session_dep),
    ):
        notice_message, notice_variant = _queue_notice(notice, applied)
        if notice == "bulk":
            posted_n, held_n, skipped_n = posted or 0, held or 0, skipped or 0
            parts = []
            if posted_n:
                parts.append(f"posted {posted_n}")
            if held_n:
                parts.append(f"held {held_n} for sign-off")
            if skipped_n:
                parts.append(f"skipped {skipped_n}")
            notice_message = (
                "Bulk classification: " + ", ".join(parts) + "."
                if parts
                else "Bulk classification: nothing changed."
            )
            notice_variant = (
                "error" if skipped_n and not posted_n
                else ("warning" if held_n and not posted_n else "success")
            )
        # 1. Fetch unallocated states
        unallocated_states = session.exec(
            select(TransactionProcessingState)
            .where(
                (TransactionProcessingState.allocation_status != "FULLY_ALLOCATED")
                | (TransactionProcessingState.posting_status != "POSTED")
            )
        ).all()

        # 2. Pre-fetch financial accounts and vendor rules to avoid N+1 queries
        accounts_map = {a.id: a for a in session.exec(select(FinancialAccount)).all()}
        vendor_rules = session.exec(select(VendorRule).where(VendorRule.is_active == True)).all()
        all_properties = session.exec(select(Property)).all()
        properties_by_id = {p.id: p for p in all_properties}
        properties = [p for p in all_properties if p.is_active]
        state_ids = [state.transaction_id for state in unallocated_states]
        transactions_map = {
            txn.id: txn
            for txn in session.exec(
                select(Transaction).where(Transaction.id.in_(state_ids))
            ).all()
        } if state_ids else {}

        all_review_items = []
        vendor_match_counts = {}

        for state in unallocated_states:
            txn = transactions_map.get(state.transaction_id)
            if not txn:
                continue

            acct = accounts_map.get(txn.financial_account_id)
            suggestion = suggest_split_for_transaction(
                txn.raw_description,
                txn.amount_cents,
                rules_cache=vendor_rules,
            )

            # Track top vendor frequencies for quick filter chips
            if suggestion and suggestion.vendor_name:
                matched_label = suggestion.vendor_name
                vendor_match_counts[matched_label] = vendor_match_counts.get(matched_label, 0) + 1

            all_review_items.append({
                "transaction": txn,
                "state": state,
                "account": acct,
                "account_name": acct.account_name if acct else txn.financial_account_id,
                "suggestion": suggestion,
            })

        # Top vendors list sorted by transaction count
        top_vendors = sorted(
            [{"name": k, "count": v} for k, v in vendor_match_counts.items()],
            key=lambda x: x["count"],
            reverse=True,
        )[:12]

        # 3. Apply Filters
        filtered_items = []
        q_upper = q.strip().upper() if q and q.strip() else None
        vendor_pattern_upper = vendor_pattern.strip().upper() if vendor_pattern and vendor_pattern.strip() else None

        for item in all_review_items:
            txn = item["transaction"]
            acct = item["account"]
            sugg = item["suggestion"]
            desc_upper = txn.raw_description.upper()

            # Text Search (description, account name, vendor name, service type)
            if q_upper:
                match_desc = q_upper in desc_upper
                match_acct = acct and q_upper in acct.account_name.upper()
                match_vendor = sugg and (
                    (sugg.vendor_name and q_upper in sugg.vendor_name.upper())
                    or (sugg.service_type and q_upper in sugg.service_type.upper())
                )
                if not (match_desc or match_acct or match_vendor):
                    continue

            # Account Filter
            if account_id and txn.financial_account_id != account_id:
                continue

            # Vendor Pattern Filter: a chip carries the display vendor name
            # ("Retail / Dining"), which is not always a substring of the rule
            # description or the bank text, so match the suggestion's vendor too.
            if vendor_pattern_upper:
                match_desc = vendor_pattern_upper in desc_upper
                match_sugg = bool(sugg) and (
                    vendor_pattern_upper in (sugg.rule_name or "").upper()
                    or vendor_pattern_upper in (sugg.vendor_name or "").upper()
                )
                if not (match_desc or match_sugg):
                    continue

            # Governance Gate Filter
            if gate:
                if gate == "CBD1":
                    # CBD-1 client confirmation required
                    if not (sugg and sugg.requires_client_confirmation):
                        continue
                elif gate == "CBD2":
                    # CPA review required
                    is_mixed_card = acct and acct.mixed_use_status == "MIXED_USE"
                    needs_cpa = (sugg and sugg.requires_cpa_review) or is_mixed_card
                    if not needs_cpa:
                        continue
                elif gate == "SUGGESTED":
                    if not sugg:
                        continue
                elif gate == "UNSUGGESTED":
                    if sugg:
                        continue

            # Amount Direction Filter
            if direction:
                if direction == "DEBIT" and txn.amount_cents > 0:
                    continue
                elif direction == "CREDIT" and txn.amount_cents < 0:
                    continue
                elif direction == "LARGE" and abs(txn.amount_cents) < 50000:
                    continue

            filtered_items.append(item)

        # 4. Calculate Aggregate Metrics
        total_unallocated_count = len(all_review_items)
        total_pending_cents = sum(abs(i["transaction"].amount_cents) for i in all_review_items)
        filtered_count = len(filtered_items)
        filtered_cents = sum(abs(i["transaction"].amount_cents) for i in filtered_items)
        suggested_count = sum(1 for i in all_review_items if i.get("suggestion"))

        # 5. Paginate Filtered Results
        total_pages = max(1, math.ceil(filtered_count / per_page))
        current_page = max(1, min(page, total_pages))
        start_idx = (current_page - 1) * per_page
        end_idx = start_idx + per_page
        page_slice = filtered_items[start_idx:end_idx]

        # 6. Detailed policy and splits only for the current page slice
        final_page_items = []
        for item in page_slice:
            txn = item["transaction"]
            acct = item["account"]
            splits = session.exec(
                select(TransactionSplit).where(TransactionSplit.transaction_id == txn.id)
            ).all()
            policy = evaluate_posting_policy(session, txn, splits, account=acct)

            final_page_items.append({
                "transaction": txn,
                "state": item["state"],
                "account_name": item["account_name"],
                "account": acct,
                "policy": policy,
                "suggestion": item["suggestion"],
                "splits": splits,
            })

        prop_name_lookup = {door.id: door.name for door in properties}

        return templates.TemplateResponse(
            request=request,
            name="reconciliation.html",
            context={
                "active_page": "reconciliation",
                "review_items": final_page_items,
                "review_count": total_unallocated_count,
                "filtered_count": filtered_count,
                "total_unallocated_count": total_unallocated_count,
                "total_pending_cents": total_pending_cents,
                "filtered_cents": filtered_cents,
                "suggested_count": suggested_count,
                "accounts": list(accounts_map.values()),
                "properties": properties,
                "properties_by_id": properties_by_id,
                "top_vendors": top_vendors,
                "vendor_rules": vendor_rules,
                "prop_name_lookup": prop_name_lookup,
                "message": notice_message,
                "message_variant": notice_variant,
                "pagination": {
                    "page": current_page,
                    "per_page": per_page,
                    "total_pages": total_pages,
                    "total_items": filtered_count,
                    "start_item": start_idx + 1 if filtered_count > 0 else 0,
                    "end_item": min(end_idx, filtered_count),
                    "has_prev": current_page > 1,
                    "has_next": current_page < total_pages,
                    "prev_page": current_page - 1,
                    "next_page": current_page + 1,
                },
                "active_filters": {
                    "q": q or "",
                    "account_id": account_id or "",
                    "vendor_pattern": vendor_pattern or "",
                    "gate": gate or "ALL",
                    "direction": direction or "ALL",
                    "per_page": per_page,
                },
            },
        )

    @router.post("/reconciliation/assign")
    def assign_review_item(
        transaction_id: str = Form(...),
        property_id: str = Form(None),
        category: str = Form(...),
        client_confirmed: bool = Form(False),
        user_attested_cpa_review: bool = Form(False),
        session: Session = Depends(get_session_dep),
    ):
        txn = session.get(Transaction, transaction_id)
        if not txn:
            raise HTTPException(status_code=404, detail="Transaction not found.")

        target_amount = abs(txn.amount_cents)
        splits = [
            SplitItem(
                property_id=property_id if property_id else None,
                category=category,
                amount_cents=target_amount,
                tax_review_status=classification_status_for(*validate_authorizations(
                    client_confirmed, user_attested_cpa_review)),
            )
        ]

        if txn.amount_cents == 0:
            raise HTTPException(
                status_code=400,
                detail="This bank record carries no amount; there is nothing to allocate.",
            )

        policy = evaluate_posting_policy(session, txn, splits)
        try:
            assert_posting_authorized(
                policy, client_confirmed=client_confirmed, user_attested_cpa_review=user_attested_cpa_review
            )
            # One unit of work: a rejected posting must not leave a rewritten
            # allocation behind.
            apply_transaction_splits(session, txn.id, splits, commit=False)
            post_transaction_to_ledger(
                session=session,
                transaction_id=txn.id,
                client_confirmed=client_confirmed,
                user_attested_cpa_review=user_attested_cpa_review,
                commit=False,
                **posting_actor_kwargs(),
            )
            session.commit()
        except (GovernanceAuthorizationError, DuplicatePostingError, InvalidSplitSumError, ValueError) as exc:
            session.rollback()
            raise _posting_http_error(exc) from exc

        return RedirectResponse(url="/reconciliation?notice=assign", status_code=303)

    @router.get("/reconciliation/rent", response_class=HTMLResponse)
    def view_rent_received(
        request: Request,
        q: Optional[str] = Query(None),
        property_id: Optional[str] = Query(None),
        state: Optional[str] = Query(None),
        session: Session = Depends(get_session_dep),
    ):
        """Rent received: every inflow with the evidence for or against calling it rent."""
        view = (state or "pending").strip().lower()
        dismissed = dismissed_rent_transactions(session)
        if view == "dismissed":
            candidates = [
                candidate
                for candidate in find_rent_candidates(
                    session,
                    q=q,
                    property_id=(property_id or "").strip() or None,
                    include_settled=True,
                    include_dismissed=True,
                    include_all=True,
                    max_scan=5000,
                )
                if candidate.transaction.id in dismissed
            ]
        else:
            candidates = find_rent_candidates(
                session,
                q=q,
                property_id=(property_id or "").strip() or None,
                include_settled=view == "all",
            )
        rows = []
        for candidate in candidates:
            policy = None
            if not candidate.is_settled and candidate.category:
                policy = evaluate_posting_policy(
                    session,
                    candidate.transaction,
                    [
                        SplitItem(
                            property_id=candidate.door.id if candidate.door else None,
                            category=candidate.category,
                            amount_cents=candidate.amount_cents,
                        )
                    ],
                )
            rows.append({
                "candidate": candidate,
                "policy": policy,
                "dismissed": candidate.transaction.id in dismissed,
            })

        doors = session.exec(
            select(Property)
            .where(
                Property.property_type == "RENTAL_INVESTMENT",
                Property.is_active == True,  # noqa: E712
            )
            .order_by(Property.name)
        ).all()
        ready_ids = [
            candidate.transaction.id
            for candidate in candidates
            if candidate.is_actionable
            and not candidate.is_settled
            and candidate.transaction.id not in dismissed
        ]
        selectable_count = sum(
            1
            for candidate in candidates
            if not candidate.is_settled and candidate.transaction.id not in dismissed
        )
        message, message_variant = _rent_notice(request.query_params)

        return templates.TemplateResponse(
            request=request,
            name="rent.html",
            context={
                "active_page": "reconciliation",
                "rows": rows,
                "properties": doors,
                "candidate_count": len(candidates),
                "ready_count": len(ready_ids),
                "selectable_count": selectable_count,
                "needs_door_count": sum(
                    1 for candidate in candidates
                    if not candidate.is_settled and candidate.door is None
                ),
                "active_filters": {
                    "q": q or "",
                    "property_id": property_id or "",
                    "state": view if view in ("all", "dismissed") else "pending",
                },
                "message": message,
                "message_variant": message_variant,
            },
        )

    @router.post("/reconciliation/rent/apply")
    def apply_selected_rent_payments(
        transaction_ids: str = Form(""),
        transaction_id: list[str] = Form(default=[]),
        property_id: Optional[str] = Form(None),
        category: Optional[str] = Form(None),
        client_confirmed: bool = Form(False),
        user_attested_cpa_review: bool = Form(False),
        session: Session = Depends(get_session_dep),
    ):
        """Apply one door and category to the selected payments, or each one's proposal."""
        ids = [txn_id.strip() for txn_id in transaction_ids.split(",") if txn_id.strip()]
        ids.extend(txn_id.strip() for txn_id in transaction_id if txn_id.strip())
        unique: list[str] = []
        for txn_id in ids:
            if txn_id and txn_id not in unique:
                unique.append(txn_id)
        if not unique:
            raise HTTPException(status_code=400, detail="Select at least one payment.")

        try:
            report = apply_rent_selection(
                session,
                unique,
                property_id=(property_id or "").strip() or None,
                category=(category or "").strip() or None,
                client_confirmed=client_confirmed,
                user_attested_cpa_review=user_attested_cpa_review,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _rent_redirect(report)

    @router.post("/reconciliation/rent/row")
    def classify_rent_row(
        transaction_id: str = Form(...),
        property_id: str = Form(...),
        category: str = Form(...),
        client_confirmed: bool = Form(False),
        user_attested_cpa_review: bool = Form(False),
        session: Session = Depends(get_session_dep),
    ):
        """Classify one inflow with the door and category a person chose."""
        try:
            report = apply_rent_row(
                session,
                transaction_id=transaction_id.strip(),
                property_id=property_id.strip(),
                category=category.strip(),
                client_confirmed=client_confirmed,
                user_attested_cpa_review=user_attested_cpa_review,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _rent_redirect(report)

    @router.post("/reconciliation/rent/dismiss")
    def dismiss_rent_row(
        transaction_id: str = Form(...),
        session: Session = Depends(get_session_dep),
    ):
        """Rule a payment out as rent; it stays in the review queue."""
        try:
            dismiss_rent_candidate(session, transaction_id.strip(), reason="Not rent (owner)")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RedirectResponse(url="/reconciliation/rent?notice=dismissed", status_code=303)

    @router.post("/reconciliation/rent/restore")
    def restore_rent_row(
        transaction_id: str = Form(...),
        session: Session = Depends(get_session_dep),
    ):
        """Undo a "not rent" decision."""
        restore_rent_candidate(session, transaction_id.strip())
        return RedirectResponse(
            url="/reconciliation/rent?state=dismissed&notice=restored", status_code=303
        )

    @router.post("/reconciliation/split")
    def split_review_item(
        transaction_id: str = Form(...),
        split_property_id: list[str] = Form(...),
        split_category: list[str] = Form(...),
        split_amount_cents: list[int] = Form(...),
        client_confirmed: bool = Form(False),
        user_attested_cpa_review: bool = Form(False),
        post_if_fully_allocated: bool = Form(True),
        session: Session = Depends(get_session_dep),
    ):
        txn = session.get(Transaction, transaction_id)
        if not txn:
            raise HTTPException(status_code=404, detail="Transaction not found.")

        splits = []
        for prop_id, cat, amt in zip(split_property_id, split_category, split_amount_cents):
            if amt > 0:
                splits.append(SplitItem(
                    property_id=prop_id if prop_id else None,
                    category=cat,
                    amount_cents=amt,
                    tax_review_status=classification_status_for(*validate_authorizations(
                    client_confirmed, user_attested_cpa_review)),
                ))

        if not splits:
            raise HTTPException(status_code=400, detail="At least one positive split required.")

        try:
            state = apply_transaction_splits(
                session, txn.id, splits, allow_partial=True, commit=False
            )
            if post_if_fully_allocated and state.allocation_status == "FULLY_ALLOCATED":
                policy = evaluate_posting_policy(session, txn, splits)
                assert_posting_authorized(
                    policy, client_confirmed=client_confirmed, user_attested_cpa_review=user_attested_cpa_review
                )
                post_transaction_to_ledger(
                    session=session,
                    transaction_id=txn.id,
                    client_confirmed=client_confirmed,
                    user_attested_cpa_review=user_attested_cpa_review,
                    commit=False,
                    **posting_actor_kwargs(),
                )
            session.commit()
        except (GovernanceAuthorizationError, DuplicatePostingError, InvalidSplitSumError, ValueError) as exc:
            session.rollback()
            raise _posting_http_error(exc) from exc

        return RedirectResponse(url="/reconciliation?notice=split", status_code=303)

    @router.post("/reconciliation/apply-suggestion")
    def apply_suggested_classification(
        transaction_id: str = Form(...),
        client_confirmed: bool = Form(False),
        user_attested_cpa_review: bool = Form(False),
        session: Session = Depends(get_session_dep),
    ):
        txn = session.get(Transaction, transaction_id)
        if not txn:
            raise HTTPException(status_code=404, detail="Transaction not found.")

        sugg = suggest_split_for_transaction(txn.raw_description, txn.amount_cents, session=session)
        if not sugg:
            raise HTTPException(status_code=400, detail="No auto-suggestion available for this transaction.")

        splits = [
            SplitItem(
                property_id=sugg.property_id,
                category=sugg.category,
                amount_cents=abs(txn.amount_cents),
                tax_review_status=classification_status_for(*validate_authorizations(
                    client_confirmed, user_attested_cpa_review)),
                notes=f"Auto-applied: {sugg.rule_name}",
            )
        ]

        if txn.amount_cents == 0:
            raise HTTPException(
                status_code=400,
                detail="This bank record carries no amount; there is nothing to allocate.",
            )

        policy = evaluate_posting_policy(session, txn, splits)
        try:
            assert_posting_authorized(
                policy, client_confirmed=client_confirmed, user_attested_cpa_review=user_attested_cpa_review
            )
            apply_transaction_splits(session, txn.id, splits, commit=False)
            post_transaction_to_ledger(
                session=session,
                transaction_id=txn.id,
                client_confirmed=client_confirmed,
                user_attested_cpa_review=user_attested_cpa_review,
                commit=False,
                **posting_actor_kwargs(),
            )
            session.commit()
        except (GovernanceAuthorizationError, DuplicatePostingError, InvalidSplitSumError, ValueError) as exc:
            session.rollback()
            raise _posting_http_error(exc) from exc
        return RedirectResponse(url="/reconciliation?notice=suggestion", status_code=303)

    @router.post("/reconciliation/batch-apply-rule")
    def batch_apply_vendor_rule(
        vendor_pattern: str = Form(...),
        category: str = Form(...),
        property_id: Optional[str] = Form(None),
        client_confirmed: bool = Form(False),
        user_attested_cpa_review: bool = Form(False),
        rule_id: Optional[str] = Form(None),
        session: Session = Depends(get_session_dep),
    ):
        """Classifies matching transactions; posts only when no gate is required.

        Requirement flags on a rule are policy, never authorization. A rule that
        needs CBD-1/CBD-2 allocates the transaction and leaves it UNPOSTED so a
        human can supply the actual confirmation.
        """
        clean_pattern = vendor_pattern.strip().upper()
        rule = None
        if rule_id and rule_id.strip():
            rule = session.get(VendorRule, rule_id.strip())

        all_rules = session.exec(select(VendorRule).where(VendorRule.is_active == True)).all()
        if rule is None:
            rule = find_matching_vendor_rule(all_rules, vendor_pattern)

        required_client = bool(rule.requires_client_confirmation) if rule else False
        required_cpa = bool(rule.requires_cpa_review) if rule else False

        unallocated_states = session.exec(
            select(TransactionProcessingState)
            .where(
                (TransactionProcessingState.allocation_status != "FULLY_ALLOCATED")
                | (TransactionProcessingState.posting_status != "POSTED")
            )
        ).all()

        prop_target = property_id if property_id and property_id.strip() else None
        applied_count = 0

        patterns_to_check = [clean_pattern]
        if rule:
            patterns_to_check.append(rule.pattern.strip().upper())
            if rule.vendor_name:
                patterns_to_check.append(rule.vendor_name.strip().upper())

        for state in unallocated_states:
            txn = session.get(Transaction, state.transaction_id)
            if not txn or txn.amount_cents == 0:
                # A record that moved no money has nothing to allocate.
                continue

            desc_upper = txn.raw_description.upper()
            matches = any(p and p in desc_upper for p in patterns_to_check)
            sugg = None
            if not matches:
                sugg = suggest_split_for_transaction(txn.raw_description, txn.amount_cents, rules_cache=all_rules)
                if sugg:
                    if rule and sugg.rule_id == rule.id:
                        matches = True
                    elif sugg.vendor_name and (
                        sugg.vendor_name.upper() == clean_pattern
                        or sugg.vendor_name.upper() in clean_pattern
                        or clean_pattern in sugg.vendor_name.upper()
                    ):
                        matches = True
                    elif clean_pattern in sugg.rule_name.upper():
                        matches = True

            if matches:
                # Requirement flags are policy, never authorization.
                txn_req_client = required_client
                txn_req_cpa = required_cpa
                if not (txn_req_client and txn_req_cpa):
                    if not sugg:
                        sugg = suggest_split_for_transaction(txn.raw_description, txn.amount_cents, rules_cache=all_rules)
                    if sugg and (rule is None or sugg.rule_id == rule.id):
                        txn_req_client = txn_req_client or bool(sugg.requires_client_confirmation)
                        txn_req_cpa = txn_req_cpa or bool(sugg.requires_cpa_review)

                splits = [
                    SplitItem(
                        property_id=prop_target,
                        category=category,
                        amount_cents=abs(txn.amount_cents),
                        tax_review_status=classification_status_for(
                            client_confirmed, user_attested_cpa_review
                        ),
                        notes=f"Batch Vendor Rule: {rule.vendor_name if rule else vendor_pattern}",
                    )
                ]

                if txn_req_client or txn_req_cpa:
                    # Apply the allocation, record the gates, and leave posting
                    # to an explicit, attributable human action.
                    txn.requires_client_confirmation = (
                        txn.requires_client_confirmation or txn_req_client
                    )
                    txn.requires_cpa_review = txn.requires_cpa_review or txn_req_cpa
                    session.add(txn)
                    apply_transaction_splits(session, txn.id, splits)
                    applied_count += 1
                    continue

                policy = evaluate_posting_policy(session, txn, splits)
                try:
                    assert_posting_authorized(
                        policy, client_confirmed=client_confirmed,
                        user_attested_cpa_review=user_attested_cpa_review,
                    )
                except GovernanceAuthorizationError:
                    # Skip transactions that fail authorization gates rather
                    # than aborting the batch.
                    continue

                try:
                    apply_transaction_splits(session, txn.id, splits, commit=False)
                    post_transaction_to_ledger(
                        session=session,
                        transaction_id=txn.id,
                        client_confirmed=client_confirmed,
                        user_attested_cpa_review=user_attested_cpa_review,
                        commit=False,
                        **posting_actor_kwargs(),
                    )
                    session.commit()
                    applied_count += 1
                except (
                    GovernanceAuthorizationError,
                    DuplicatePostingError,
                    InvalidSplitSumError,
                    ValueError,
                ):
                    # One refused row must not abort the batch, and must not
                    # leave its allocation rewritten.
                    session.rollback()
                    continue

        return RedirectResponse(
            url=f"/reconciliation?applied={applied_count}&notice=batch", status_code=303
        )

    @router.post("/reconciliation/bulk-classify")
    def bulk_classify_selected(
        transaction_id: list[str] = Form(default=[]),
        property_id: Optional[str] = Form(None),
        category: str = Form(...),
        client_confirmed: bool = Form(False),
        user_attested_cpa_review: bool = Form(False),
        session: Session = Depends(get_session_dep),
    ):
        """Classify every selected transaction with one door and category.

        A row whose policy needs an authorization that was not supplied is
        allocated and left unposted, exactly as the batch-rule path does, so one
        gated item cannot block a bulk action or post without its gate.
        """
        ids = [txn_id.strip() for txn_id in transaction_id if txn_id.strip()]
        if not ids:
            raise HTTPException(status_code=400, detail="Select at least one transaction.")

        prop_target = property_id if property_id and property_id.strip() else None
        posted_count = held_count = skipped_count = 0

        for txn_id in ids:
            txn = session.get(Transaction, txn_id)
            if not txn or txn.amount_cents == 0:
                skipped_count += 1
                continue

            splits = [
                SplitItem(
                    property_id=prop_target,
                    category=category,
                    amount_cents=abs(txn.amount_cents),
                    tax_review_status=classification_status_for(
                        client_confirmed, user_attested_cpa_review
                    ),
                )
            ]
            policy = evaluate_posting_policy(session, txn, splits)
            unmet_gate = (
                policy.requires_client_confirmation and not client_confirmed
            ) or (policy.requires_cpa_review and not user_attested_cpa_review)

            try:
                apply_transaction_splits(session, txn.id, splits, commit=False)
                if unmet_gate:
                    txn.requires_client_confirmation = (
                        txn.requires_client_confirmation or policy.requires_client_confirmation
                    )
                    txn.requires_cpa_review = (
                        txn.requires_cpa_review or policy.requires_cpa_review
                    )
                    session.add(txn)
                    session.commit()
                    held_count += 1
                    continue

                assert_posting_authorized(
                    policy,
                    client_confirmed=client_confirmed,
                    user_attested_cpa_review=user_attested_cpa_review,
                )
                post_transaction_to_ledger(
                    session=session,
                    transaction_id=txn.id,
                    client_confirmed=client_confirmed,
                    user_attested_cpa_review=user_attested_cpa_review,
                    commit=False,
                    **posting_actor_kwargs(),
                )
                session.commit()
                posted_count += 1
            except (
                GovernanceAuthorizationError,
                DuplicatePostingError,
                InvalidSplitSumError,
                ValueError,
            ):
                session.rollback()
                skipped_count += 1

        return RedirectResponse(
            url=(
                "/reconciliation?notice=bulk"
                f"&posted={posted_count}&held={held_count}&skipped={skipped_count}"
            ),
            status_code=303,
        )

    @router.post("/reconciliation/vendor-rules/create")
    def create_vendor_rule(
        pattern: str = Form(...),
        vendor_name: str = Form(...),
        service_type: str = Form(...),
        default_category: str = Form("PERSONAL"),
        default_property_id: Optional[str] = Form(None),
        confidence: float = Form(0.90),
        requires_client_confirmation: bool = Form(False),
        requires_cpa_review: bool = Form(False),
        apply_to_existing: bool = Form(False),
        session: Session = Depends(get_session_dep),
    ):
        """Creates a new vendor classification rule and optionally applies it immediately."""
        clean_pattern = pattern.strip().upper()
        rule_id = f"vrule_{clean_pattern.lower().replace(' ', '_')[:20]}_{uuid4().hex[:6]}"
        prop_target = default_property_id if default_property_id and default_property_id.strip() else None

        rule_record = VendorRule(
            id=rule_id,
            pattern=clean_pattern,
            vendor_name=vendor_name.strip(),
            service_type=service_type.strip(),
            default_category=default_category,
            default_property_id=prop_target,
            confidence=confidence,
            requires_client_confirmation=requires_client_confirmation,
            requires_cpa_review=requires_cpa_review,
            is_active=True,
        )
        session.add(rule_record)
        session.commit()

        if apply_to_existing:
            # Re-use batch apply logic
            return batch_apply_vendor_rule(
                vendor_pattern=clean_pattern,
                category=default_category,
                property_id=prop_target,
                # The rule's requirements are policy, not a client/CPA grant.
                client_confirmed=False,
                user_attested_cpa_review=False,
                # This is an internal call, not an HTTP form. Without an explicit
                # None the route default is a FastAPI Form object, not a string.
                rule_id=None,
                session=session,
            )

        return RedirectResponse(url="/reconciliation?notice=rule", status_code=303)

    return router
