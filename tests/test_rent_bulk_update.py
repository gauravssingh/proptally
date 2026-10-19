"""Tenant extraction and bulk rent classification from bank descriptions."""

from datetime import date
from uuid import uuid4

from sqlmodel import Session, select

from proptally.db.models import (
    Document,
    FinancialAccount,
    FinancialInstitution,
    Lease,
    Property,
    Statement,
    Transaction,
    TransactionProcessingState,
    TransactionSplit,
)
from proptally.domain.transactions.rent import (
    bulk_apply_rent_payments,
    extract_tenant_payment,
)


def test_extract_tenant_payment_reads_remitter_memo_and_address():
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


def test_extract_tenant_payment_ignores_outgoing_or_uncaptioned_transfers():
    assert extract_tenant_payment(
        'Zelle payment to  SEETHU BABU for "Aug rent 1 of 2 22627"; Conf# xsci9pv33'
    ) is None
    assert extract_tenant_payment("Zelle Transfer Conf# jr4gx3ko5; BOGAT, ROBERT") is None


def _rent_context(session: Session) -> dict:
    document = Document(
        id=str(uuid4()),
        file_sha256="rentbulk" + uuid4().hex,
        original_filename="checking.pdf",
        storage_path="/tmp/checking.pdf",
        file_size_bytes=1,
    )
    institution = FinancialInstitution(id="inst_rent_bank", name="Bank of America")
    prop = Property(
        id="prop_tinsley",
        name="12307 TINSLEY ST",
        address_line1="12307 TINSLEY ST",
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
    session.commit()
    return {"account_id": account.id, "statement_id": statement.id, "property_id": prop.id}


def _add_txn(session: Session, context: dict, amount: int, description: str, day: date) -> Transaction:
    txn = Transaction(
        id=str(uuid4()),
        statement_id=context["statement_id"],
        financial_account_id=context["account_id"],
        transaction_date=day,
        amount_cents=amount,
        direction="INFLOW",
        raw_description=description,
    )
    session.add(txn)
    session.add(TransactionProcessingState(
        transaction_id=txn.id,
        allocation_status="UNALLOCATED",
        allocated_cents=0,
        residual_cents=abs(amount),
        posting_status="UNPOSTED",
    ))
    session.commit()
    session.refresh(txn)
    return txn


def test_bulk_rent_allocates_posts_and_creates_tenant_record(session: Session):
    context = _rent_context(session)
    jan = _add_txn(
        session,
        context,
        260000,
        'Zelle payment from  TYLER J SAXSMA for "rent"; Conf# 99c1nxf3o',
        date(2026, 1, 12),
    )
    feb = _add_txn(
        session,
        context,
        260000,
        'Zelle payment from  TYLER J SAXSMA for "Tinsley rent feb pt1"; Conf# 99c44x19h',
        date(2026, 2, 2),
    )
    mixed = _add_txn(
        session,
        context,
        200000,
        'Zelle payment from  TYLER J SAXSMA for "partial Rental Deposit + pro-rent 12307 Tinsley"; Conf# 99c1mpk1l',
        date(2026, 1, 12),
    )
    deposit = _add_txn(
        session,
        context,
        200000,
        'Zelle payment from  TYLER J SAXSMA for "Security Deposit"; Conf# 99c1zzzzz',
        date(2026, 1, 12),
    )
    trash = _add_txn(
        session,
        context,
        7150,
        'Zelle payment from  TYLER J SAXSMA for "MDC trash Tinsley"; Conf# 99cfqt495',
        date(2026, 5, 4),
    )

    report = bulk_apply_rent_payments(
        session,
        property_id=context["property_id"],
        tenant_name="Tyler J Saxsma",
    )

    assert set(report.applied) == {jan.id, feb.id, deposit.id}
    assert report.rent_cents == 520000
    assert report.deposit_cents == 200000
    assert report.errors == []
    assert {txn_id for txn_id, _reason in report.skipped} == {mixed.id}

    splits = session.exec(
        select(TransactionSplit).where(
            TransactionSplit.transaction_id.in_([jan.id, feb.id, deposit.id])
        )
    ).all()
    assert len(splits) == 3
    rent_splits = [split for split in splits if split.category == "RENT_INCOME"]
    deposit_split = next(split for split in splits if split.category == "SECURITY_DEPOSIT")
    assert len(rent_splits) == 2
    assert deposit_split.amount_cents == 200000
    assert all(split.property_id == context["property_id"] for split in splits)

    states = session.exec(
        select(TransactionProcessingState).where(
            TransactionProcessingState.transaction_id.in_([jan.id, feb.id, deposit.id])
        )
    ).all()
    assert all(state.posting_status == "POSTED" for state in states)
    assert all(state.allocation_status == "FULLY_ALLOCATED" for state in states)

    # The unclassified-looking trash reimbursement is not rent and stays pending.
    trash_state = session.get(TransactionProcessingState, trash.id)
    assert trash_state is not None
    assert trash_state.posting_status == "UNPOSTED"

    leases = session.exec(select(Lease)).all()
    assert len(leases) == 1
    assert leases[0].tenant_name == "Tyler J Saxsma"
    assert leases[0].property_id == context["property_id"]
    assert leases[0].monthly_rent_cents == 260000


def test_bulk_rent_route_classifies_and_posts_for_the_signed_in_owner(owner):
    signed_in = owner()
    with Session(signed_in.engine) as session:
        context = _rent_context(session)
        jan = _add_txn(
            session,
            context,
            260000,
            'Zelle payment from  TYLER J SAXSMA for "rent"; Conf# 99c1nxf3o',
            date(2026, 1, 12),
        )
        feb = _add_txn(
            session,
            context,
            260000,
            'Zelle payment from  TYLER J SAXSMA for "Tinsley rent feb pt1"; Conf# 99c44x19h',
            date(2026, 2, 2),
        )
        jan_id, feb_id = jan.id, feb.id

    page = signed_in.client.get("/reconciliation").text
    assert "Bulk Classify Rent" in page

    response = signed_in.post(
        "/reconciliation/bulk-rent",
        {
            "property_id": context["property_id"],
            "tenant_name": "Tyler J Saxsma",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    with Session(signed_in.engine) as session:
        states = session.exec(
            select(TransactionProcessingState).where(
                TransactionProcessingState.transaction_id.in_([jan_id, feb_id])
            )
        ).all()
        assert all(state.posting_status == "POSTED" for state in states)
        lease = session.exec(select(Lease)).one()
        assert lease.tenant_name == "Tyler J Saxsma"
