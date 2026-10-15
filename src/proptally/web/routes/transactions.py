"""Transactions Register and Splitting web router."""

from collections import defaultdict
from datetime import date
import math
import urllib.parse
from uuid import uuid4

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
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
from proptally.web.auth import posting_actor_kwargs


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
        prop_map = {p.id: p for p in properties}

        # 2. Preload processing states and splits in batch (prevents N+1 queries)
        all_states = session.exec(select(TransactionProcessingState)).all()
        state_map = {s.transaction_id: s for s in all_states}

        all_splits = session.exec(select(TransactionSplit)).all()
        splits_map = defaultdict(list)
        for sp in all_splits:
            splits_map[sp.transaction_id].append(sp)

        # 3. Fetch all recorded transactions
        txns = session.exec(
            select(Transaction).order_by(Transaction.transaction_date.desc(), Transaction.id.desc())
        ).all()

        total_recorded = len(txns)
        global_posted = 0
        global_unallocated = 0
        global_inflows = 0
        global_outflows = 0

        for txn in txns:
            st = state_map.get(txn.id)
            if st and st.posting_status == "POSTED":
                global_posted += 1
            if not st or st.allocation_status != "FULLY_ALLOCATED":
                global_unallocated += 1
            if txn.amount_cents > 0:
                global_inflows += 1
            elif txn.amount_cents < 0:
                global_outflows += 1

        # 4. Multi-dimensional filtering
        q_clean = q.strip().lower() if q and q.strip() else None
        account_filter = account_id.strip() if account_id and account_id.strip() != "ALL" else None
        status_filter = status.strip().upper() if status and status.strip() != "ALL" else None
        property_filter = property_id.strip() if property_id and property_id.strip() != "ALL" else None
        direction_filter = direction.strip().upper() if direction and direction.strip() != "ALL" else None

        is_filtered = bool(q_clean or account_filter or status_filter or property_filter or direction_filter)

        filtered_tuples = []
        for txn in txns:
            acct = account_map.get(txn.financial_account_id)
            st = state_map.get(txn.id)
            if not st:
                st = TransactionProcessingState(
                    transaction_id=txn.id,
                    allocation_status="UNALLOCATED",
                    allocated_cents=0,
                    residual_cents=abs(txn.amount_cents),
                )
            splits = splits_map.get(txn.id, [])

            # Filter: Text search (merchant description or account name)
            if q_clean:
                desc = (txn.raw_description or "").lower()
                acct_str = (acct.account_name if acct else "").lower()
                if q_clean not in desc and q_clean not in acct_str:
                    continue

            # Filter: Account
            if account_filter and txn.financial_account_id != account_filter:
                continue

            # Filter: Cash flow direction
            if direction_filter:
                if direction_filter == "INFLOW" and txn.amount_cents <= 0:
                    continue
                elif direction_filter == "OUTFLOW" and txn.amount_cents >= 0:
                    continue

            # Filter: Property / Door
            if property_filter:
                if property_filter == "UNASSIGNED":
                    if splits and any(s.property_id for s in splits):
                        continue
                else:
                    if not any(s.property_id == property_filter for s in splits):
                        continue

            # Filter: Allocation & Posting Status
            if status_filter:
                if status_filter == "POSTED":
                    if st.posting_status != "POSTED":
                        continue
                elif status_filter == "FULLY_ALLOCATED":
                    if st.allocation_status != "FULLY_ALLOCATED" or st.posting_status == "POSTED":
                        continue
                elif status_filter == "PARTIALLY_ALLOCATED":
                    if st.allocation_status != "PARTIALLY_ALLOCATED":
                        continue
                elif status_filter == "UNALLOCATED":
                    if st.allocation_status != "UNALLOCATED":
                        continue
                elif status_filter == "NEEDS_ATTENTION":
                    pol = evaluate_posting_policy(session, txn, splits, account=acct)
                    if not (pol.requires_authorization or pol.should_offer_review):
                        continue

            filtered_tuples.append((txn, acct, st, splits))

        total_filtered = len(filtered_tuples)

        # 5. Pagination calculation
        per_page_clean = per_page.strip() if per_page else "25"
        if per_page_clean.upper() == "ALL":
            per_page_num = max(total_filtered, 1)
        else:
            try:
                per_page_num = max(int(per_page_clean), 1)
            except (ValueError, TypeError):
                per_page_num = 25
                per_page_clean = "25"

        total_pages = max(1, math.ceil(total_filtered / per_page_num))
        if page < 1:
            page = 1
        elif page > total_pages:
            page = total_pages

        start_idx = (page - 1) * per_page_num
        end_idx = min(start_idx + per_page_num, total_filtered)
        page_tuples = filtered_tuples[start_idx:end_idx]

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
        amount_dollars: float = Form(...),
        direction: str = Form(...),
        counter_account_id: str = Form(None),
        session: Session = Depends(get_session_dep),
    ):
        cents = round(amount_dollars * 100)
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
                statement_date=date.today(),
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

        referer = request.headers.get("referer")
        target_url = referer if referer and "/transactions" in referer else "/transactions"
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
        client_confirmed = form.get("client_confirmed") == "true"
        user_attested_cpa_review = form.get("user_attested_cpa_review") == "true"

        proc_state = session.get(TransactionProcessingState, transaction_id)
        already_posted = proc_state is not None and proc_state.posting_status == "POSTED"

        # The split status records classification review only. Whether posting is
        # permitted is decided by the domain policy, never by this field. A user
        # self-attestation is recorded as such and never as a CPA approval.
        tax_review_status = classification_status_for(client_confirmed, user_attested_cpa_review)

        splits: list[SplitItem] = []
        for prop_id, cat, amt_str in zip(property_ids, categories, amount_dollars_list):
            amt_float = float(amt_str) if amt_str else 0.0
            amt_cents = round(amt_float * 100)
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

        referer = request.headers.get("referer")
        target_url = referer if referer and "/transactions" in referer else "/transactions"
        return RedirectResponse(url=target_url, status_code=303)

    @router.post("/transactions/{transaction_id}/post")
    async def post_single_transaction(
        transaction_id: str,
        request: Request,
        session: Session = Depends(get_session_dep),
    ):
        form = await request.form()
        from proptally.domain.transactions.splits import post_transaction_to_ledger
        try:
            post_transaction_to_ledger(
                session=session,
                transaction_id=transaction_id,
                client_confirmed=form.get("client_confirmed") == "true",
                user_attested_cpa_review=form.get("user_attested_cpa_review") == "true",
                **posting_actor_kwargs(),
            )
        except GovernanceAuthorizationError as exc:
            raise HTTPException(status_code=403, detail=str(exc))
        referer = request.headers.get("referer")
        target_url = referer if referer and "/transactions" in referer else "/transactions"
        return RedirectResponse(url=target_url, status_code=303)

    return router
