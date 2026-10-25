"""Tenant rent evidence, candidate discovery and rent payment posting."""

from datetime import date
from uuid import uuid4

import pytest
from sqlmodel import Session, select

from proptally.db.models import (
    AuditLog,
    Document,
    FinancialAccount,
    FinancialInstitution,
    Lease,
    Property,
    Statement,
    TenantAlias,
    Transaction,
    TransactionProcessingState,
    TransactionSplit,
)
from proptally.domain.transactions.rent import (
    apply_rent_candidates,
    apply_rent_row,
    apply_rent_selection,
    dismiss_rent_candidate,
    extract_tenant_payment,
    find_rent_candidates,
    restore_rent_candidate,
)

# --- Fixtures -------------------------------------------------------------


def _checking_context(
    session: Session,
    *,
    tenant_name: str | None = "Tyler J Saxsma",
    monthly_rent_cents: int = 260000,
    property_name: str = "12307 TINSLEY ST",
    lease_start: date = date(2026, 1, 1),
    lease_end: date | None = None,
    lease_active: bool = True,
) -> dict:
    document = Document(
        id=str(uuid4()),
        file_sha256="rentworkbench" + uuid4().hex,
        original_filename="checking.pdf",
        storage_path="/tmp/checking.pdf",
        file_size_bytes=1,
    )
    institution = FinancialInstitution(id="inst_rent_bank", name="Bank of America")
    prop = Property(
        id="prop_tinsley",
        name=property_name,
        address_line1=property_name,
        city="HUNTLEY",
        state="IL",
        zip_code="60142",
        property_type="RENTAL_INVESTMENT",
    )
    session.add(document)
    session.add(institution)
    session.add(prop)
    session.flush()
    account = FinancialAccount(
        id="acct_rent_checking",
        institution_id=institution.id,
        account_name="Checking",
        account_type="CHECKING",
        account_number_masked="...9456",
    )
    session.add(account)
    session.flush()
    statement = Statement(
        id="stmt_rent_2026_01",
        document_id=document.id,
        financial_account_id=account.id,
        statement_type="CHECKING",
        active_parser_name="BofACheckingParser",
        active_parser_version="1.0.0",
        statement_date=date(2026, 1, 31),
    )
    session.add(statement)
    if tenant_name:
        session.add(
            Lease(
                id=f"lease_{uuid4().hex[:8]}",
                property_id=prop.id,
                tenant_name=tenant_name,
                start_date=lease_start,
                end_date=lease_end,
                monthly_rent_cents=monthly_rent_cents,
                is_active=lease_active,
            )
        )
    session.commit()
    return {
        "account_id": account.id,
        "statement_id": statement.id,
        "property_id": prop.id,
    }


def _add_txn(
    session: Session,
    context: dict,
    amount_cents: int,
    description: str,
    day: date = date(2026, 1, 12),
    *,
    requires_client_confirmation: bool = False,
) -> Transaction:
    txn = Transaction(
        id=str(uuid4()),
        statement_id=context["statement_id"],
        financial_account_id=context["account_id"],
        transaction_date=day,
        amount_cents=amount_cents,
        direction="INFLOW" if amount_cents > 0 else "OUTFLOW",
        raw_description=description,
        requires_client_confirmation=requires_client_confirmation,
    )
    session.add(txn)
    session.add(
        TransactionProcessingState(
            transaction_id=txn.id,
            allocation_status="UNALLOCATED",
            allocated_cents=0,
            residual_cents=abs(amount_cents),
            posting_status="UNPOSTED",
        )
    )
    session.commit()
    session.refresh(txn)
    return txn


def _mark_settled(session: Session, txn: Transaction) -> None:
    state = session.get(TransactionProcessingState, txn.id)
    state.allocation_status = "FULLY_ALLOCATED"
    state.allocated_cents = abs(txn.amount_cents)
    state.residual_cents = 0
    state.posting_status = "POSTED"
    session.add(state)
    session.commit()


# --- Evidence -------------------------------------------------------------


def test_memo_names_remitter_intent_and_address():
    payment = extract_tenant_payment(
        'Zelle payment from  TYLER J SAXSMA for "partial Rental Deposit + pro-rent 12307 Tinsley"; Conf# 99c1mpk1l'
    )

    assert payment is not None
    assert payment.tenant_name == "Tyler J Saxsma"
    assert payment.memo == "partial Rental Deposit + pro-rent 12307 Tinsley"
    assert payment.property_hint == "12307 Tinsley"
    assert payment.is_rent is True
    assert payment.is_security_deposit is True
    assert payment.is_mixed_deposit_and_rent is True
    assert payment.names_remitter_only is False


@pytest.mark.parametrize(
    ("description", "expected"),
    [
        ("Zelle Transfer Conf# 999x9udhw; LEAH A SWANSON", "Leah A Swanson"),
        ("Zelle payment from  TIFFANY P DZITKOWSKI Conf# 99agnx4t6", "Tiffany P Dzitkowski"),
        ("Zelle Transfer Conf# hzw21nibs; FRANKLIN, JOSE", "Jose Franklin"),
    ],
)
def test_remitter_only_forms_are_evidence_but_not_a_classification(description, expected):
    payment = extract_tenant_payment(description)

    assert payment is not None
    assert payment.tenant_name == expected
    assert payment.is_rent is False
    assert payment.is_security_deposit is False
    assert payment.names_remitter_only is True


def test_outgoing_transfers_are_not_tenant_evidence():
    assert extract_tenant_payment(
        'Zelle payment to  SEETHU BABU for "Aug rent 1 of 2 22627"; Conf# xsci9pv33'
    ) is None


def test_a_decimal_amount_is_not_read_as_a_street_address():
    payment = extract_tenant_payment(
        'Zelle payment from  TIMOTHY TANG for "$204.12 for lunch, $44.02 for pastries"; Conf# 1'
    )

    assert payment is not None
    assert payment.property_hint is None


# --- Discovery ------------------------------------------------------------


def test_a_lease_matches_the_remitter_despite_the_middle_initial(session: Session):
    context = _checking_context(session)
    _add_txn(session, context, 260000, 'Zelle payment from  TYLER J SAXSMA for "rent"; Conf# 1')

    [candidate] = find_rent_candidates(session)

    assert candidate.door is not None and candidate.door.id == context["property_id"]
    assert candidate.category == "RENT_INCOME"
    assert candidate.confidence == pytest.approx(0.90)
    assert candidate.is_actionable is True
    assert any("lease" in reason for reason in candidate.reasons)


def test_a_confirmed_remitter_alias_attributes_a_memo_without_the_word_rent(session: Session):
    context = _checking_context(session, tenant_name=None)
    session.add(
        TenantAlias(
            id="alias_peter",
            alias_key="peter dzitkowski",
            display_name="Peter Dzitkowski",
            tenant_name="Peter Dzitkowski",
            property_id=context["property_id"],
        )
    )
    session.commit()
    _add_txn(
        session,
        context,
        280000,
        'Zelle payment from  PETER DZITKOWSKI for "June."; Conf# 2',
        date(2026, 5, 31),
    )

    [candidate] = find_rent_candidates(session)

    assert candidate.door is not None and candidate.door.id == context["property_id"]
    assert candidate.category == "RENT_INCOME"
    assert candidate.confidence == pytest.approx(0.95)
    assert candidate.is_actionable is True


def test_a_family_member_with_the_same_surname_does_not_match_someone_elses_lease(session: Session):
    context = _checking_context(session, tenant_name="Peter Dzitkowski")
    _add_txn(
        session,
        context,
        280000,
        'Zelle payment from  TIFFANY P DZITKOWSKI for "June."; Conf# 3',
        date(2026, 5, 31),
    )

    assert find_rent_candidates(session) == []


def test_a_rent_memo_with_no_door_asks_for_one_instead_of_guessing(session: Session):
    context = _checking_context(session, tenant_name=None)
    _add_txn(
        session,
        context,
        250000,
        'Zelle payment from  PETER DZITKOWSKI for "Rent"; Conf# 4',
        date(2026, 7, 1),
    )

    [candidate] = find_rent_candidates(session)

    assert candidate.category == "RENT_INCOME"
    assert candidate.door is None
    assert candidate.is_actionable is False
    assert "Choose the property" in candidate.blockers


def test_a_mixed_deposit_and_rent_memo_is_blocked_for_manual_splitting(session: Session):
    context = _checking_context(session)
    _add_txn(
        session,
        context,
        200000,
        'Zelle payment from  TYLER J SAXSMA for "partial Rental Deposit + pro-rent 12307 Tinsley"; Conf# 5',
    )

    [candidate] = find_rent_candidates(session)

    assert candidate.is_actionable is False
    assert any("split it manually" in blocker for blocker in candidate.blockers)


def test_an_unrelated_inflow_appears_only_when_searched(session: Session):
    context = _checking_context(session)
    _add_txn(session, context, 2500, 'Zelle payment from  STEVEN D CLESCERI for "jbl"; Conf# 6')

    assert find_rent_candidates(session) == []

    [candidate] = find_rent_candidates(session, q="jbl")

    assert candidate.door is None
    assert candidate.is_actionable is False
    assert "The bank record does not say rent or deposit" in candidate.blockers


def test_settled_payments_are_history_and_not_default_candidates(session: Session):
    context = _checking_context(session)
    txn = _add_txn(session, context, 260000, 'Zelle payment from  TYLER J SAXSMA for "rent"; Conf# 7')
    _mark_settled(session, txn)

    assert find_rent_candidates(session) == []

    [candidate] = find_rent_candidates(session, include_settled=True)
    assert candidate.is_settled is True


# --- Posting --------------------------------------------------------------


def test_two_equal_rent_payments_post_create_the_lease_and_learn_the_remitter(session: Session):
    context = _checking_context(session, tenant_name=None)
    january = _add_txn(
        session,
        context,
        260000,
        'Zelle payment from  TYLER J SAXSMA for "12307 Tinsley rent"; Conf# 8',
        date(2026, 1, 12),
    )
    february = _add_txn(
        session,
        context,
        260000,
        'Zelle payment from  TYLER J SAXSMA for "12307 Tinsley rent feb pt1"; Conf# 9',
        date(2026, 2, 2),
    )

    report = apply_rent_candidates(session, [january.id, february.id])

    assert [outcome.status for outcome in report.outcomes] == ["POSTED", "POSTED"]
    assert report.rent_cents == 520000
    assert report.deposit_cents == 0
    assert report.errors == []

    states = session.exec(
        select(TransactionProcessingState).where(
            TransactionProcessingState.transaction_id.in_([january.id, february.id])
        )
    ).all()
    assert all(state.posting_status == "POSTED" for state in states)
    assert all(state.allocation_status == "FULLY_ALLOCATED" for state in states)

    splits = session.exec(
        select(TransactionSplit).where(
            TransactionSplit.transaction_id.in_([january.id, february.id])
        )
    ).all()
    assert {split.category for split in splits} == {"RENT_INCOME"}
    assert {split.schedule_e_line for split in splits} == {3}
    assert {split.property_id for split in splits} == {context["property_id"]}

    lease = session.exec(select(Lease)).one()
    assert lease.tenant_name == "Tyler J Saxsma"
    assert lease.property_id == context["property_id"]
    assert lease.monthly_rent_cents == 260000

    alias = session.exec(select(TenantAlias)).one()
    assert alias.alias_key == "tyler saxsma"
    assert alias.property_id == context["property_id"]
    assert alias.lease_id == lease.id


def test_a_security_deposit_posts_to_the_liability_not_rent_income(session: Session):
    context = _checking_context(session, tenant_name=None)
    deposit = _add_txn(
        session,
        context,
        200000,
        'Zelle payment from  TYLER J SAXSMA for "Security Deposit"; Conf# 10',
        date(2026, 1, 12),
    )

    report = apply_rent_row(
        session,
        transaction_id=deposit.id,
        property_id=context["property_id"],
        category="SECURITY_DEPOSIT",
    )

    assert [outcome.status for outcome in report.outcomes] == ["POSTED"]
    assert report.deposit_cents == 200000
    split = session.exec(
        select(TransactionSplit).where(TransactionSplit.transaction_id == deposit.id)
    ).one()
    assert split.category == "SECURITY_DEPOSIT"
    assert split.schedule_e_line is None


def test_a_second_payment_from_a_confirmed_remitter_needs_no_lease(session: Session):
    context = _checking_context(session, tenant_name=None)
    first = _add_txn(
        session,
        context,
        250000,
        'Zelle payment from  PETER DZITKOWSKI for "Rent"; Conf# 11',
        date(2026, 7, 1),
    )
    apply_rent_row(
        session,
        transaction_id=first.id,
        property_id=context["property_id"],
        category="RENT_INCOME",
    )
    _add_txn(
        session,
        context,
        280000,
        'Zelle payment from  PETER DZITKOWSKI for "June."; Conf# 12',
        date(2026, 5, 31),
    )

    [candidate] = find_rent_candidates(session)

    assert candidate.confidence == pytest.approx(0.95)
    assert candidate.is_actionable is True
    assert candidate.category == "RENT_INCOME"


def test_a_gated_payment_is_classified_and_left_unposted(session: Session):
    context = _checking_context(session)
    txn = _add_txn(
        session,
        context,
        260000,
        'Zelle payment from  TYLER J SAXSMA for "rent"; Conf# 13',
        requires_client_confirmation=True,
    )

    report = apply_rent_candidates(session, [txn.id])

    assert [outcome.status for outcome in report.outcomes] == ["ALLOCATED"], report.outcomes
    state = session.get(TransactionProcessingState, txn.id)
    assert state.allocation_status == "FULLY_ALLOCATED"
    assert state.posting_status == "UNPOSTED"
    session.refresh(txn)
    assert txn.requires_client_confirmation is True


def test_a_supplied_client_authorization_posts_a_gated_payment(session: Session):
    context = _checking_context(session)
    txn = _add_txn(
        session,
        context,
        260000,
        'Zelle payment from  TYLER J SAXSMA for "rent"; Conf# 14',
        requires_client_confirmation=True,
    )

    report = apply_rent_candidates(session, [txn.id], client_confirmed=True)

    assert [outcome.status for outcome in report.outcomes] == ["POSTED"], report.outcomes
    assert session.get(TransactionProcessingState, txn.id).posting_status == "POSTED"


def test_a_mixed_memo_is_refused_by_a_batch_run(session: Session):
    context = _checking_context(session)
    mixed = _add_txn(
        session,
        context,
        200000,
        'Zelle payment from  TYLER J SAXSMA for "Rent Security Deposit"; Conf# 15',
    )

    report = apply_rent_candidates(session, [mixed.id])

    assert [outcome.status for outcome in report.outcomes] == ["SKIPPED"]
    assert "split it manually" in report.outcomes[0].reason
    assert session.get(TransactionProcessingState, mixed.id).posting_status == "UNPOSTED"


def test_a_row_apply_rejects_an_outflow_and_a_non_rental_door(session: Session):
    context = _checking_context(session)
    outflow = _add_txn(session, context, -5000, "HERTZ RENT-A-CAR", date(2026, 1, 3))

    with pytest.raises(ValueError, match="Only inflows"):
        apply_rent_row(
            session,
            transaction_id=outflow.id,
            property_id=context["property_id"],
            category="RENT_INCOME",
        )
    with pytest.raises(ValueError, match="Only inflows"):
        dismiss_rent_candidate(session, outflow.id)

    session.add(
        Property(
            id="prop_home",
            name="Home",
            address_line1="1 MAIN ST",
            city="HUNTLEY",
            state="IL",
            zip_code="60142",
            property_type="PRIMARY_RESIDENCE",
        )
    )
    session.commit()
    inflow = _add_txn(session, context, 260000, 'Zelle payment from  TYLER J SAXSMA for "rent"; Conf# 16')

    with pytest.raises(ValueError, match="active property"):
        apply_rent_row(
            session,
            transaction_id=inflow.id,
            property_id="prop_home",
            category="RENT_INCOME",
        )


# --- Web ------------------------------------------------------------------


def test_the_rent_page_lists_the_evidence_and_posts_ready_payments(owner, parse_html):
    signed_in = owner()
    with Session(signed_in.engine) as session:
        context = _checking_context(session)
        txn = _add_txn(
            session,
            context,
            260000,
            'Zelle payment from  TYLER J SAXSMA for "rent"; Conf# 17',
        )
        txn_id = txn.id

    queue = signed_in.client.get("/reconciliation").text
    assert "Rent received" in queue

    page = parse_html(signed_in.client.get("/reconciliation/rent").text)
    assert page.find("h1").get_text(strip=True).startswith("Rent Received")
    assert "Tyler J Saxsma" in page.get_text()
    assert page.select_one("#rentBulkForm") is not None
    assert page.select_one("input[data-bulk-pick]") is not None
    assert page.select_one("[data-select-ready]") is not None

    response = signed_in.post(
        "/reconciliation/rent/apply",
        {"transaction_id": [txn_id]},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "posted=1" in response.headers["location"]

    with Session(signed_in.engine) as session:
        assert session.get(TransactionProcessingState, txn_id).posting_status == "POSTED"

    followed = parse_html(signed_in.client.get(response.headers["location"]).text)
    assert "Rent received: posted 1 payment." in followed.get_text()
    assert followed.select_one("[data-toast-region]") is not None


def test_a_remitter_only_payment_can_be_classified_by_hand(owner, parse_html):
    signed_in = owner()
    with Session(signed_in.engine) as session:
        context = _checking_context(session, tenant_name=None)
        txn = _add_txn(
            session,
            context,
            250000,
            "Zelle Transfer Conf# 999abc; PETER DZITKOWSKI",
            date(2026, 7, 1),
        )
        txn_id = txn.id

    page = parse_html(signed_in.client.get("/reconciliation/rent?q=dzitkowski").text)
    assert "Peter Dzitkowski" in page.get_text()

    response = signed_in.post(
        "/reconciliation/rent/row",
        {
            "transaction_id": txn_id,
            "property_id": context["property_id"],
            "category": "RENT_INCOME",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    with Session(signed_in.engine) as session:
        assert session.get(TransactionProcessingState, txn_id).posting_status == "POSTED"
        assert session.exec(select(TenantAlias)).one().alias_key == "peter dzitkowski"


def test_queue_actions_confirm_with_a_toast(owner, parse_html):
    signed_in = owner()
    with Session(signed_in.engine) as session:
        context = _checking_context(session)
        txn = _add_txn(
            session,
            context,
            260000,
            'Zelle payment from  TYLER J SAXSMA for "rent"; Conf# 18',
        )
        txn_id = txn.id

    response = signed_in.post(
        "/reconciliation/assign",
        {
            "transaction_id": txn_id,
            "property_id": context["property_id"],
            "category": "RENT_INCOME",
            "client_confirmed": "true",
        },
    )

    assert response.status_code == 200
    page = parse_html(response.text)
    assert "Assigned and posted to the ledger." in page.get_text()
    assert page.select_one("[data-toast-region]") is not None


# --- Not rent -------------------------------------------------------------


def test_an_amount_equal_to_the_rent_is_not_rent_evidence_by_itself(session: Session):
    """A company's own transfer can equal the rent; the amount alone proves nothing."""
    context = _checking_context(
        session,
        tenant_name="Erick",
        monthly_rent_cents=300000,
        lease_start=date(2023, 1, 1),
    )
    _add_txn(
        session,
        context,
        300000,
        "Zelle Transfer Conf# 999vd5ha3; MUMMY LOGIC AND ART LLC",
        date(2023, 4, 17),
    )

    assert find_rent_candidates(session) == []

    [candidate] = find_rent_candidates(session, q="mummy")

    assert candidate.door is not None and candidate.door.id == context["property_id"]
    assert candidate.category is None
    assert candidate.is_actionable is False
    assert any("not evidence by itself" in reason for reason in candidate.reasons)


def test_dismissing_a_candidate_keeps_it_out_until_restored(session: Session):
    context = _checking_context(session)
    txn = _add_txn(session, context, 260000, 'Zelle payment from  TYLER J SAXSMA for "rent"; Conf# 20')

    assert len(find_rent_candidates(session)) == 1
    assert dismiss_rent_candidate(session, txn.id, reason="Company transfer") is True
    assert find_rent_candidates(session) == []
    assert len(find_rent_candidates(session, include_dismissed=True)) == 1
    assert dismiss_rent_candidate(session, txn.id) is False

    assert restore_rent_candidate(session, txn.id) is True
    assert len(find_rent_candidates(session)) == 1

    actions = [
        row.action
        for row in session.exec(
            select(AuditLog).where(AuditLog.entity_id == txn.id)
        ).all()
    ]
    assert "RENT_DISMISSED" in actions
    assert "RENT_RESTORED" in actions


def test_a_dismissed_payment_is_not_posted_by_a_batch(session: Session):
    context = _checking_context(session)
    txn = _add_txn(session, context, 260000, 'Zelle payment from  TYLER J SAXSMA for "rent"; Conf# 21')
    dismiss_rent_candidate(session, txn.id, reason="Not rent")

    report = apply_rent_candidates(session, [txn.id])

    assert [outcome.status for outcome in report.outcomes] == ["SKIPPED"]
    assert session.get(TransactionProcessingState, txn.id).posting_status == "UNPOSTED"


def test_a_not_rent_decision_can_be_reversed_from_the_page(owner, parse_html):
    signed_in = owner()
    with Session(signed_in.engine) as session:
        context = _checking_context(session)
        txn = _add_txn(session, context, 260000, 'Zelle payment from  TYLER J SAXSMA for "rent"; Conf# 22')
        txn_id = txn.id

    response = signed_in.post(
        "/reconciliation/rent/dismiss", {"transaction_id": txn_id}
    )
    assert response.status_code == 200
    assert "Removed from rent candidates" in response.text

    page = parse_html(signed_in.client.get("/reconciliation/rent").text)
    assert "Tyler J Saxsma" not in page.get_text()

    ruled_out = parse_html(signed_in.client.get("/reconciliation/rent?state=dismissed").text)
    assert "Tyler J Saxsma" in ruled_out.get_text()
    assert "Restore" in ruled_out.get_text()

    restored = signed_in.post("/reconciliation/rent/restore", {"transaction_id": txn_id})
    assert restored.status_code == 200
    assert "Restored to rent candidates" in restored.text

    page = parse_html(signed_in.client.get("/reconciliation/rent").text)
    assert "Tyler J Saxsma" in page.get_text()


def test_a_company_transfer_equal_to_rent_is_not_listed_as_rent(owner, parse_html):
    signed_in = owner()
    with Session(signed_in.engine) as session:
        context = _checking_context(
            session,
            tenant_name="Erick",
            monthly_rent_cents=300000,
            lease_start=date(2023, 1, 1),
        )
        _add_txn(
            session,
            context,
            300000,
            "Zelle Transfer Conf# 999vd5ha3; MUMMY LOGIC AND ART LLC",
            date(2023, 4, 17),
        )

    page = parse_html(signed_in.client.get("/reconciliation/rent").text)
    assert "Mummy Logic And Art Llc" not in page.get_text()

    searched = parse_html(signed_in.client.get("/reconciliation/rent?q=mummy").text)
    assert "Mummy Logic And Art Llc" in searched.get_text()
    assert "not evidence by itself" in searched.get_text()


def test_a_bulk_selection_takes_one_door_and_category(session: Session):
    context = _checking_context(session, tenant_name=None)
    first = _add_txn(
        session,
        context,
        250000,
        "Zelle Transfer Conf# 999aaa; PETER DZITKOWSKI",
        date(2026, 7, 1),
    )
    second = _add_txn(
        session,
        context,
        280000,
        "Zelle Transfer Conf# 999bbb; PETER DZITKOWSKI",
        date(2026, 8, 1),
    )
    assert find_rent_candidates(session) == []

    report = apply_rent_selection(
        session,
        [first.id, second.id],
        property_id=context["property_id"],
        category="RENT_INCOME",
    )

    assert [outcome.status for outcome in report.outcomes] == ["POSTED", "POSTED"]
    assert report.errors == []
    with_alias = session.exec(select(TenantAlias)).one()
    assert with_alias.alias_key == "peter dzitkowski"
    assert with_alias.property_id == context["property_id"]


def test_bulk_apply_posts_the_selected_payments(owner):
    signed_in = owner()
    with Session(signed_in.engine) as session:
        context = _checking_context(session, tenant_name=None)
        first = _add_txn(
            session,
            context,
            250000,
            "Zelle Transfer Conf# 999ccc; PETER DZITKOWSKI",
            date(2026, 7, 1),
        )
        second = _add_txn(
            session,
            context,
            280000,
            "Zelle Transfer Conf# 999ddd; PETER DZITKOWSKI",
            date(2026, 8, 1),
        )
        first_id, second_id = first.id, second.id

    response = signed_in.post(
        "/reconciliation/rent/apply",
        {
            "transaction_id": [first_id, second_id],
            "property_id": context["property_id"],
            "category": "RENT_INCOME",
        },
    )

    assert response.status_code == 200
    assert "posted 2 payments" in response.text
    with Session(signed_in.engine) as session:
        states = session.exec(
            select(TransactionProcessingState).where(
                TransactionProcessingState.transaction_id.in_([first_id, second_id])
            )
        ).all()
        assert all(state.posting_status == "POSTED" for state in states)


# --- Lease periods ---------------------------------------------------------


def _add_lease(session: Session, context: dict, tenant: str, start: date, end: date | None, cents: int) -> None:
    session.add(
        Lease(
            id=f"lease_{uuid4().hex[:8]}",
            property_id=context["property_id"],
            tenant_name=tenant,
            start_date=start,
            end_date=end,
            monthly_rent_cents=cents,
        )
    )
    session.commit()


def test_a_lease_attributes_only_payments_inside_its_term(session: Session):
    """Erick's lease starts 2026-10-01; Polynt's earlier payments are not his."""
    context = _checking_context(
        session,
        tenant_name="Erick",
        monthly_rent_cents=300000,
        lease_start=date(2026, 10, 1),
        lease_end=date(2027, 9, 30),
    )
    _add_txn(
        session,
        context,
        300000,
        "POLYNT COMPOSITE DES:PAYMENT    ID:20260825215007  INDN:JAIDEEP MALHOTRA",
        date(2026, 8, 26),
    )

    [candidate] = find_rent_candidates(session, q="polynt")

    assert candidate.door is None
    assert candidate.tenant_name is None
    assert all("Amount equals the rent" not in reason for reason in candidate.reasons)


def test_a_lease_attributes_an_atm_deposit_inside_its_term(session: Session):
    context = _checking_context(
        session,
        tenant_name="Erick",
        monthly_rent_cents=300000,
        lease_start=date(2026, 10, 1),
        lease_end=date(2027, 9, 30),
    )
    _add_txn(session, context, 300000, "BKOFAMERICA ATM 11/02 DEPOSIT", date(2026, 11, 2))

    [candidate] = find_rent_candidates(session, q="deposit")

    assert candidate.door is not None and candidate.door.id == context["property_id"]
    assert any("Amount equals the rent" in reason for reason in candidate.reasons)


def test_a_previous_tenant_is_not_reassigned_to_the_new_lease(session: Session):
    context = _checking_context(
        session,
        tenant_name="Erick",
        monthly_rent_cents=300000,
        lease_start=date(2026, 10, 1),
        lease_end=date(2027, 9, 30),
    )
    _add_lease(session, context, "Polynt Composite", date(2025, 1, 1), date(2026, 9, 30), 300000)

    previous = _add_txn(
        session,
        context,
        300000,
        "Zelle Transfer Conf# 999poly; POLYNT COMPOSITE",
        date(2025, 6, 25),
    )
    late = _add_txn(
        session,
        context,
        300000,
        "Zelle Transfer Conf# 999late; POLYNT COMPOSITE",
        date(2026, 10, 15),
    )

    previous_candidate = next(
        candidate
        for candidate in find_rent_candidates(session, q="polynt")
        if candidate.transaction.id == previous.id
    )
    assert previous_candidate.door is not None
    assert previous_candidate.tenant_name == "Polynt Composite"
    assert previous_candidate.confidence == pytest.approx(0.90)

    late_candidate = next(
        candidate
        for candidate in find_rent_candidates(session, q="polynt")
        if candidate.transaction.id == late.id
    )
    assert late_candidate.door is None
    assert any("does not cover this payment date" in blocker for blocker in late_candidate.blockers)
    assert late_candidate.tenant_name == "Polynt Composite"


def test_an_ended_lease_still_attributes_its_own_period(session: Session):
    context = _checking_context(
        session,
        tenant_name="Polynt Composite",
        monthly_rent_cents=300000,
        lease_start=date(2025, 1, 1),
        lease_end=date(2026, 9, 30),
        lease_active=False,
    )
    _add_txn(
        session,
        context,
        300000,
        "Zelle Transfer Conf# 999old; POLYNT COMPOSITE",
        date(2025, 6, 25),
    )

    [candidate] = find_rent_candidates(session)

    assert candidate.door is not None and candidate.door.id == context["property_id"]
    assert candidate.tenant_name == "Polynt Composite"
