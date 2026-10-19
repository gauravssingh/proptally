"""Transactions Register and Splitting web router."""

import math
import urllib.parse
from collections import defaultdict
from datetime import date, datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, or_
from sqlmodel import Session, select

from proptally.core.exceptions import GovernanceAuthorizationError
from proptally.db.models import (
    Document,
    FinancialAccount,
    Property,
    Statement,
    Transaction,
    TransactionProcessingState,
    TransactionSplit,
)
from proptally.domain.transactions.policy import (
    classification_status_for,
    evaluate_posting_policy,
)
from proptally.domain.transactions.splits import SplitItem, apply_transaction_splits
from proptally.web.auth import posting_actor_kwargs, validate_authorizations
from proptally.web.form_parsing import FormError
from proptally.web.form_parsing import cents as _cents
from proptally.web.helpers import safe_referer


def get_transactions_router(templates, get_session_dep):
    router = APIRouter()

    @router.get("/transactions", response_class=HTMLResponse)
    def view_transactions(
        request: Request,
        q: str = None,
        account_id: str = "ALL",
        status: str = "ALL",
        property_id: str = "ALL",
        direction: str = "ALL",
        per_page: str = "25",
        page: int = 1,
        session: Session = Depends(get_session_dep),
    ):
        # 1. Preload accounts and properties
        accounts = session.exec(select(FinancialAccount).order_by(FinancialAccount.account_name)).all()
        account_map = {a.id: a for a in accounts}

        properties = session.exec(select(Property).order_by(Property.name)).all()

        # 2. Headline counters are aggregates, not a full table scan in Python.
        total_recorded = session.exec(select(func.count()).select_from(Transaction)).one()
        global_posted = session.exec(
            select(func.count()).select_from(TransactionProcessingState).where(
                TransactionProcessingState.posting_status == "POSTED"
            )
        ).one()
        fully_allocated = session.exec(
            select(func.count()).select_from(TransactionProcessingState).where(
                TransactionProcessingState.allocation_status == "FULLY_ALLOCATED"
            )
        ).one()
        global_unallocated = max(0, total_recorded - fully_allocated)
        global_inflows = session.exec(
            select(func.count()).select_from(Transaction).where(Transaction.amount_cents > 0)
        ).one()
        global_outflows = session.exec(
            select(func.count()).select_from(Transaction).where(Transaction.amount_cents < 0)
        ).one()

        # 3. Build the filter in SQL so pagination happens in the database.
        q_clean = q.strip().lower() if q and q.strip() else None
        account_filter = account_id.strip() if account_id and account_id.strip() != "ALL" else None
        status_filter = status.strip().upper() if status and status.strip() != "ALL" else None
        property_filter = property_id.strip() if property_id and property_id.strip() != "ALL" else None
        direction_filter = direction.strip().upper() if direction and direction.strip() != "ALL" else None

        is_filtered = bool(q_clean or account_filter or status_filter or property_filter or direction_filter)

        query = select(Transaction)
        if q_clean:
            matching_accounts = select(FinancialAccount.id).where(
                func.lower(FinancialAccount.account_name).contains(q_clean)
            )
            query = query.where(or_(
                func.lower(Transaction.raw_description).contains(q_clean),
                Transaction.financial_account_id.in_(matching_accounts),
            ))
        if account_filter:
            query = query.where(Transaction.financial_account_id == account_filter)
        if direction_filter == "INFLOW":
            query = query.where(Transaction.amount_cents > 0)
        elif direction_filter == "OUTFLOW":
            query = query.where(Transaction.amount_cents < 0)
        if property_filter == "UNASSIGNED":
            tagged = select(TransactionSplit.transaction_id).where(
                TransactionSplit.property_id.is_not(None)
            )
            query = query.where(Transaction.id.not_in(tagged))
        elif property_filter:
            tagged = select(TransactionSplit.transaction_id).where(
                TransactionSplit.property_id == property_filter
            )
            query = query.where(Transaction.id.in_(tagged))
        if status_filter in {"POSTED", "FULLY_ALLOCATED", "PARTIALLY_ALLOCATED", "UNALLOCATED"}:
            query = query.join(
                TransactionProcessingState,
                TransactionProcessingState.transaction_id == Transaction.id,
            )
            if status_filter == "POSTED":
                query = query.where(TransactionProcessingState.posting_status == "POSTED")
            elif status_filter == "FULLY_ALLOCATED":
                query = query.where(
                    TransactionProcessingState.allocation_status == "FULLY_ALLOCATED",
                    TransactionProcessingState.posting_status != "POSTED",
                )
            else:
                query = query.where(
                    TransactionProcessingState.allocation_status == status_filter
                )

        ordered = query.order_by(Transaction.transaction_date.desc(), Transaction.id.desc())

        def _page_counts(total: int) -> tuple[int, int, int, int, int, str]:
            clean = per_page.strip() if per_page else "25"
            if clean.upper() == "ALL":
                size = max(total, 1)
            else:
                try:
                    size = max(int(clean), 1)
                except (ValueError, TypeError):
                    size = 25
                    clean = "25"
            pages = max(1, math.ceil(total / size))
            current = min(max(page, 1), pages)
            start = (current - 1) * size
            end = min(start + size, total)
            return size, pages, current, start, end, clean

        needs_attention = status_filter == "NEEDS_ATTENTION"
        if needs_attention:
            # Authorization policy is application logic, so this filter is the one
            # case that must inspect matching rows before paginating.
            rows = session.exec(ordered).all()
            ids = [row.id for row in rows]
            state_map = {
                state.transaction_id: state
                for state in session.exec(
                    select(TransactionProcessingState).where(
                        TransactionProcessingState.transaction_id.in_(ids)
                    )
                ).all()
            } if ids else {}
            splits_map: dict[str, list] = defaultdict(list)
            if ids:
                for split in session.exec(
                    select(TransactionSplit).where(TransactionSplit.transaction_id.in_(ids))
                ).all():
                    splits_map[split.transaction_id].append(split)
            filtered = []
            for txn in rows:
                st = state_map.get(txn.id)
                acct = account_map.get(txn.financial_account_id)
                splits = splits_map.get(txn.id, [])
                policy = evaluate_posting_policy(session, txn, splits, account=acct)
                if policy.requires_authorization or policy.should_offer_review:
                    filtered.append((txn, acct, st, splits))
            total_filtered = len(filtered)
            per_page_num, total_pages, page, start_idx, end_idx, per_page_clean = _page_counts(total_filtered)
            page_tuples = filtered[start_idx:end_idx]
        else:
            total_filtered = session.exec(
                select(func.count()).select_from(query.subquery())
            ).one()
            per_page_num, total_pages, page, start_idx, end_idx, per_page_clean = _page_counts(total_filtered)
            rows = session.exec(ordered.offset(start_idx).limit(per_page_num)).all()
            ids = [row.id for row in rows]
            state_map = {
                state.transaction_id: state
                for state in session.exec(
                    select(TransactionProcessingState).where(
                        TransactionProcessingState.transaction_id.in_(ids)
                    )
                ).all()
            } if ids else {}
            splits_map = defaultdict(list)
            if ids:
                for split in session.exec(
                    select(TransactionSplit).where(TransactionSplit.transaction_id.in_(ids))
                ).all():
                    splits_map[split.transaction_id].append(split)
            page_tuples = []
            for txn in rows:
                state = state_map.get(txn.id) or TransactionProcessingState(
                    transaction_id=txn.id,
                    allocation_status="UNALLOCATED",
                    allocated_cents=0,
                    residual_cents=abs(txn.amount_cents),
                )
                page_tuples.append((
                    txn,
                    account_map.get(txn.financial_account_id),
                    state,
                    splits_map.get(txn.id, []),
                ))

        # 6. Evaluate posting policy only for the current page slice
        transactions_data = []
        for txn, acct, st, splits in page_tuples:
            policy = evaluate_posting_policy(session, txn, splits, account=acct)
            transactions_data.append({
                "transaction": txn,
                "account_name": acct.account_name if acct else txn.financial_account_id,
                "state": st,
                "splits": splits,
                "policy": policy,
            })

        # 7. Build pagination URLs preserving query parameters
        def build_page_url(p: int) -> str:
            params = []
            if q_clean:
                params.append(f"q={urllib.parse.quote_plus(q.strip())}")
            if account_filter:
                params.append(f"account_id={urllib.parse.quote_plus(account_filter)}")
            if status_filter:
                params.append(f"status={urllib.parse.quote_plus(status_filter)}")
            if property_filter:
                params.append(f"property_id={urllib.parse.quote_plus(property_filter)}")
            if direction_filter:
                params.append(f"direction={urllib.parse.quote_plus(direction_filter)}")
            if per_page_clean != "25":
                params.append(f"per_page={urllib.parse.quote_plus(per_page_clean)}")
            params.append(f"page={p}")
            return f"/transactions?{'&'.join(params)}"

        prev_url = build_page_url(page - 1) if page > 1 else None
        next_url = build_page_url(page + 1) if page < total_pages else None
        start_item = start_idx + 1 if total_filtered > 0 else 0
        end_item = end_idx

        current_filters = {
            "q": q.strip() if q else "",
            "account_id": account_id or "ALL",
            "status": status or "ALL",
            "property_id": property_id or "ALL",
            "direction": direction or "ALL",
            "per_page": per_page_clean,
            "page": page,
            "is_filtered": is_filtered,
        }

        return templates.TemplateResponse(
            request=request,
            name="transactions.html",
            context={
                "active_page": "transactions",
                "transactions_data": transactions_data,
                "accounts": accounts,
                "properties": properties,
                "review_count": global_unallocated,
                "total_recorded": total_recorded,
                "global_posted": global_posted,
                "global_unallocated": global_unallocated,
                "global_inflows": global_inflows,
                "global_outflows": global_outflows,
                "current_filters": current_filters,
                "page": page,
                "total_pages": total_pages,
                "start_item": start_item,
                "end_item": end_item,
                "total_items": total_filtered,
                "prev_url": prev_url,
                "next_url": next_url,
            },
        )

    @router.post("/transactions/manual")
    def create_manual_transaction(
        request: Request,
        account_id: str = Form(...),
        transaction_date: str = Form(...),
        description: str = Form(...),
        amount_dollars: str = Form(...),
        direction: str = Form(...),
        counter_account_id: str = Form(None),
        session: Session = Depends(get_session_dep),
    ):
        try:
            cents = _cents(amount_dollars, "Amount")
        except FormError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if cents is None or cents <= 0:
            raise HTTPException(status_code=400, detail="Amount must be a positive money amount.")
        signed_cents = cents if direction == "INFLOW" else -cents

        # An optional counter account makes this a transfer. Choosing a credit
        # card here is what classifies the entry as a card settlement; no
        # description is inspected to decide that.
        counter = None
        if counter_account_id:
            counter = session.get(FinancialAccount, counter_account_id)
            if counter is None:
                raise HTTPException(status_code=400, detail="Counter account not found.")
            if counter.id == account_id:
                raise HTTPException(status_code=400, detail="A transfer cannot target its own account.")

        # Find or create a default manual statement
        stmt = session.exec(
            select(Statement).where(Statement.financial_account_id == account_id)
        ).first()
        if not stmt:
            doc = Document(
                id=str(uuid4()),
                file_sha256=f"manual_entry_{uuid4()}",
                original_filename="manual_user_entry",
                storage_path="/manual/entries",
                file_size_bytes=0,
            )
            session.add(doc)
            session.flush()
            stmt = Statement(
                id=f"stmt_manual_{account_id}",
                document_id=doc.id,
                financial_account_id=account_id,
                statement_type="MANUAL",
                active_parser_name="ManualEntry",
                active_parser_version="1.0",
                statement_date=datetime.now(timezone.utc).date(),
            )
            session.add(stmt)
            session.flush()

        txn = Transaction(
            id=str(uuid4()),
            statement_id=stmt.id,
            financial_account_id=account_id,
            transaction_date=date.fromisoformat(transaction_date),
            amount_cents=signed_cents,
            direction=direction,
            raw_description=description,
            counter_account_id=counter.id if counter else None,
        )
        session.add(txn)
        session.flush()

        state = TransactionProcessingState(
            transaction_id=txn.id,
            allocation_status="UNALLOCATED",
            allocated_cents=0,
            residual_cents=abs(signed_cents),
            is_reconciled=False,
        )
        session.add(state)
        session.commit()

        target_url = safe_referer(request, "/transactions")
        if not urllib.parse.urlsplit(target_url).path.startswith("/transactions"):
            target_url = "/transactions"
        return RedirectResponse(url=target_url, status_code=303)

    @router.post("/transactions/split")
    async def split_transaction(
        request: Request,
        session: Session = Depends(get_session_dep),
    ):
        form = await request.form()
        transaction_id = str(form.get("transaction_id"))
        property_ids = form.getlist("property_id[]")
        categories = form.getlist("category[]")
        amount_dollars_list = form.getlist("amount_dollars[]")
        post_flag = form.get("post_to_ledger") == "true"
        # Authorization comes only from the request. No route invents it.
        # The two authorizations belong to different roles, so they are validated here
        # rather than accepted from the form.
        client_confirmed, user_attested_cpa_review = validate_authorizations(
            client_confirmed=form.get("client_confirmed") == "true",
            user_attested_cpa_review=form.get("user_attested_cpa_review") == "true",
        )

        proc_state = session.get(TransactionProcessingState, transaction_id)
        already_posted = proc_state is not None and proc_state.posting_status == "POSTED"

        # The split status records classification review only. Whether posting is
        # permitted is decided by the domain policy, never by this field. A user
        # self-attestation is recorded as such and never as a CPA approval.
        tax_review_status = classification_status_for(client_confirmed, user_attested_cpa_review)

        splits: list[SplitItem] = []
        for prop_id, cat, amt_str in zip(property_ids, categories, amount_dollars_list):
            try:
                amt_cents = _cents(amt_str, "Split amount") or 0
            except FormError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            if amt_cents > 0:
                splits.append(
                    SplitItem(
                        property_id=str(prop_id) if prop_id else None,
                        category=str(cat),
                        amount_cents=amt_cents,
                        tax_review_status=tax_review_status,
                    )
                )

        if already_posted:
            # Reclassification of an already posted entry: safely reverse and repost
            from proptally.domain.transactions.splits import (
                reclassify_and_repost_transaction,
            )
            try:
                reclassify_and_repost_transaction(
                    session=session,
                    transaction_id=transaction_id,
                    new_splits=splits,
                    client_confirmed=client_confirmed,
                    user_attested_cpa_review=user_attested_cpa_review,
                    reversal_reason="User reclassified transaction splits via register",
                    **posting_actor_kwargs(),
                )
            except GovernanceAuthorizationError as exc:
                raise HTTPException(status_code=403, detail=str(exc))
        else:
            state = apply_transaction_splits(session, transaction_id, splits, allow_partial=True)
            if post_flag and state.allocation_status == "FULLY_ALLOCATED":
                from proptally.domain.transactions.splits import (
                    post_transaction_to_ledger,
                )
                try:
                    post_transaction_to_ledger(
                        session=session,
                        transaction_id=transaction_id,
                        client_confirmed=client_confirmed,
                        user_attested_cpa_review=user_attested_cpa_review,
                        **posting_actor_kwargs(),
                    )
                except GovernanceAuthorizationError as exc:
                    raise HTTPException(status_code=403, detail=str(exc))

        target_url = safe_referer(request, "/transactions")
        if not urllib.parse.urlsplit(target_url).path.startswith("/transactions"):
            target_url = "/transactions"
        return RedirectResponse(url=target_url, status_code=303)

    @router.post("/transactions/{transaction_id}/post")
    async def post_single_transaction(
        transaction_id: str,
        request: Request,
        session: Session = Depends(get_session_dep),
    ):
        form = await request.form()
        from proptally.domain.transactions.splits import post_transaction_to_ledger

        # The same role validation every other posting path uses: a MANAGER
        # cannot assert a client confirmation, nor a non-owner a CPA review.
        client_confirmed, user_attested_cpa_review = validate_authorizations(
            client_confirmed=form.get("client_confirmed") == "true",
            user_attested_cpa_review=form.get("user_attested_cpa_review") == "true",
        )
        try:
            post_transaction_to_ledger(
                session=session,
                transaction_id=transaction_id,
                client_confirmed=client_confirmed,
                user_attested_cpa_review=user_attested_cpa_review,
                **posting_actor_kwargs(),
            )
        except GovernanceAuthorizationError as exc:
            raise HTTPException(status_code=403, detail=str(exc))
        target_url = safe_referer(request, "/transactions")
        if not urllib.parse.urlsplit(target_url).path.startswith("/transactions"):
            target_url = "/transactions"
        return RedirectResponse(url=target_url, status_code=303)

    return router
