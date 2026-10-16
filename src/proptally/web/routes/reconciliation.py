"""Reconciliation and Review Queue web router.

Governance is owned by the domain policy module. This router evaluates that
policy before mutating anything, so a request without the required
authorization changes nothing, and it passes the caller's authorizations
straight through to the domain posting service as a second line of defence.
"""

import math
from typing import Optional
from uuid import uuid4
from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlmodel import Session, select

from proptally.core.exceptions import GovernanceAuthorizationError
from proptally.db.models import (
    FinancialAccount,
    Property,
    Transaction,
    TransactionProcessingState,
    TransactionSplit,
    VendorRule,
)
from proptally.domain.transactions.policy import (
    assert_posting_authorized,
    classification_status_for,
    evaluate_posting_policy,
)
from proptally.domain.transactions.splits import (
    SplitItem,
    apply_transaction_splits,
    post_transaction_to_ledger,
)
from proptally.domain.transactions.suggest import suggest_split_for_transaction
from proptally.web.auth import validate_authorizations
from proptally.web.auth import posting_actor_kwargs


def get_reconciliation_router(templates, get_session_dep):
    router = APIRouter()

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
        session: Session = Depends(get_session_dep),
    ):
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
        properties = session.exec(select(Property).where(Property.is_active == True)).all()

        all_review_items = []
        vendor_match_counts = {}

        for state in unallocated_states:
            txn = session.get(Transaction, state.transaction_id)
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

            # Vendor Pattern Filter
            if vendor_pattern_upper:
                if vendor_pattern_upper not in desc_upper and (not sugg or vendor_pattern_upper not in sugg.rule_name.upper()):
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

        # 7. Batch Action Metadata (if vendor pattern is active and matching items exist)
        batch_action_info = None
        if vendor_pattern_upper and filtered_items:
            # Find matching vendor rule
            matching_rule = next((r for r in vendor_rules if r.pattern.upper() == vendor_pattern_upper or vendor_pattern_upper in r.pattern.upper()), None)
            batch_action_info = {
                "pattern": vendor_pattern,
                "count": filtered_count,
                "rule": matching_rule,
                "suggested_category": matching_rule.default_category if matching_rule else "PERSONAL",
                "suggested_property_id": matching_rule.default_property_id if matching_rule else None,
            }

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
                "top_vendors": top_vendors,
                "vendor_rules": vendor_rules,
                "batch_action_info": batch_action_info,
                "applied_count": applied,
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

        policy = evaluate_posting_policy(session, txn, splits)
        try:
            assert_posting_authorized(
                policy, client_confirmed=client_confirmed, user_attested_cpa_review=user_attested_cpa_review
            )
        except GovernanceAuthorizationError as exc:
            raise HTTPException(status_code=403, detail=str(exc))

        apply_transaction_splits(session, txn.id, splits)
        try:
            post_transaction_to_ledger(
                session=session,
                transaction_id=txn.id,
                client_confirmed=client_confirmed,
                user_attested_cpa_review=user_attested_cpa_review,
                **posting_actor_kwargs(),
            )
        except GovernanceAuthorizationError as exc:
            raise HTTPException(status_code=403, detail=str(exc))

        return RedirectResponse(url="/reconciliation", status_code=303)

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

        state = apply_transaction_splits(session, txn.id, splits, allow_partial=True)

        if post_if_fully_allocated and state.allocation_status == "FULLY_ALLOCATED":
            policy = evaluate_posting_policy(session, txn, splits)
            try:
                assert_posting_authorized(
                    policy, client_confirmed=client_confirmed, user_attested_cpa_review=user_attested_cpa_review
                )
                post_transaction_to_ledger(
                    session=session,
                    transaction_id=txn.id,
                    client_confirmed=client_confirmed,
                    user_attested_cpa_review=user_attested_cpa_review,
                    **posting_actor_kwargs(),
                )
            except GovernanceAuthorizationError as exc:
                raise HTTPException(status_code=403, detail=str(exc))

        return RedirectResponse(url="/reconciliation", status_code=303)

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

        policy = evaluate_posting_policy(session, txn, splits)
        try:
            assert_posting_authorized(
                policy, client_confirmed=client_confirmed, user_attested_cpa_review=user_attested_cpa_review
            )
        except GovernanceAuthorizationError as exc:
            raise HTTPException(status_code=403, detail=str(exc))

        apply_transaction_splits(session, txn.id, splits)
        post_transaction_to_ledger(
            session=session,
            transaction_id=txn.id,
            client_confirmed=client_confirmed,
            user_attested_cpa_review=user_attested_cpa_review,
            **posting_actor_kwargs(),
        )
        return RedirectResponse(url="/reconciliation", status_code=303)

    @router.post("/reconciliation/batch-apply-rule")
    def batch_apply_vendor_rule(
        vendor_pattern: str = Form(...),
        category: str = Form(...),
        property_id: Optional[str] = Form(None),
        client_confirmed: bool = Form(False),
        user_attested_cpa_review: bool = Form(False),
        session: Session = Depends(get_session_dep),
    ):
        """Batch-classifies and posts all unallocated transactions matching a vendor pattern."""
        unallocated_states = session.exec(
            select(TransactionProcessingState)
            .where(
                (TransactionProcessingState.allocation_status != "FULLY_ALLOCATED")
                | (TransactionProcessingState.posting_status != "POSTED")
            )
        ).all()

        pattern_upper = vendor_pattern.strip().upper()
        prop_target = property_id if property_id and property_id.strip() else None
        applied_count = 0

        for state in unallocated_states:
            txn = session.get(Transaction, state.transaction_id)
            if not txn:
                continue

            if pattern_upper in txn.raw_description.upper():
                splits = [
                    SplitItem(
                        property_id=prop_target,
                        category=category,
                        amount_cents=abs(txn.amount_cents),
                        tax_review_status=classification_status_for(*validate_authorizations(
                    client_confirmed, user_attested_cpa_review)),
                        notes=f"Batch Vendor Rule: {vendor_pattern}",
                    )
                ]

                policy = evaluate_posting_policy(session, txn, splits)
                try:
                    assert_posting_authorized(
                        policy, client_confirmed=client_confirmed, user_attested_cpa_review=user_attested_cpa_review
                    )
                except GovernanceAuthorizationError:
                    # Skip transactions that fail authorization gates rather than aborting batch
                    continue

                apply_transaction_splits(session, txn.id, splits)
                try:
                    post_transaction_to_ledger(
                        session=session,
                        transaction_id=txn.id,
                        client_confirmed=client_confirmed,
                        user_attested_cpa_review=user_attested_cpa_review,
                        **posting_actor_kwargs(),
                    )
                    applied_count += 1
                except Exception:
                    continue

        return RedirectResponse(url=f"/reconciliation?applied={applied_count}", status_code=303)

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
                client_confirmed=requires_client_confirmation,
                user_attested_cpa_review=requires_cpa_review,
                session=session,
            )

        return RedirectResponse(url="/reconciliation", status_code=303)

    return router
