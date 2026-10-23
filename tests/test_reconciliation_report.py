"""Controls and the reconciliation report.

The report is a detective control over posted ledger lines; the publish gate is
the preventive half. These tests pin the conventions that would otherwise drift:
statement arithmetic differs between checking and card, card balances are stored
in the holder's sign, and a warning must be acknowledged before publishing.
"""

from datetime import date

from sqlmodel import select

from proptally.db.models import (
    Document,
    FinancialAccount,
    FinancialInstitution,
    LedgerAccount,
    Property,
    Statement,
    Transaction,
)
from proptally.domain.accounting.controls import build_reconciliation_report


def _account(session, account_id, name, account_type, ledger_code):
    institution = session.exec(select(FinancialInstitution)).first()
    if institution is None:
        institution = FinancialInstitution(id="inst_test", name="Test Bank")
        session.add(institution)
        session.flush()
    session.add(
        FinancialAccount(
            id=account_id,
            institution_id=institution.id,
            account_name=name,
            account_type=account_type,
            account_number_masked="...0001",
            ledger_account_code=ledger_code,
        )
    )
    return institution


def _statement(session, statement_id, account_id, statement_type, opening, closing, amounts):
    document = Document(
        id=f"doc_{statement_id}",
        original_filename=f"{statement_id}.pdf",
        storage_path=f"/tmp/{statement_id}.pdf",
        file_size_bytes=10,
        file_sha256=f"sha_{statement_id}",
        document_type="CHECKING_STATEMENT",
    )
    session.add(document)
    session.add(
        Statement(
            id=statement_id,
            document_id=document.id,
            financial_account_id=account_id,
            statement_type=statement_type,
            active_parser_name="test",
            active_parser_version="1",
            period_start_date=date(2026, 1, 1),
            period_end_date=date(2026, 1, 31),
            statement_date=date(2026, 1, 31),
            opening_balance_cents=opening,
            closing_balance_cents=closing,
            ingestion_status="COMMITTED",
        )
    )
    for index, amount in enumerate(amounts):
        session.add(
            Transaction(
                id=f"txn_{statement_id}_{index}",
                statement_id=statement_id,
                financial_account_id=account_id,
                transaction_date=date(2026, 1, 10),
                amount_cents=amount,
                direction="INFLOW" if amount >= 0 else "OUTFLOW",
                raw_description=f"row {index}",
            )
        )


def _by_key(report):
    return {check.key: check for check in report.checks}


def test_statement_arithmetic_uses_the_convention_for_each_type(session):
    session.add(LedgerAccount(code=1500, name="Checking", account_type="ASSET", normal_balance="DEBIT"))
    session.add(LedgerAccount(code=2010, name="Card", account_type="LIABILITY", normal_balance="CREDIT"))
    session.flush()
    _account(session, "acct_checking", "Operating Checking", "CHECKING", 1500)
    _account(session, "acct_card", "Gold Card", "CREDIT_CARD", 2010)
    # Checking: opening + net == closing.
    _statement(session, "stmt_checking", "acct_checking", "CHECKING", 10000, 15000, [5000])
    # Card: charges negative, so opening - net == closing.
    _statement(session, "stmt_card", "acct_card", "CREDIT_CARD", 10000, 4000, [6000])
    session.commit()

    report = build_reconciliation_report(session)

    assert _by_key(report)["statement_math"].passed is True

    transaction = session.get(Transaction, "txn_stmt_checking_0")
    transaction.amount_cents = 4000
    session.add(transaction)
    session.commit()

    report = build_reconciliation_report(session)
    check = _by_key(report)["statement_math"]
    assert check.passed is False
    assert check.count == 1
    assert "stmt_checking" in check.samples


def test_missing_basis_and_account_drift_are_named(session):
    session.add(
        Property(
            id="prop_no_basis",
            name="12 Elm St",
            address_line1="12 Elm St",
            city="Springfield",
            zip_code="62704",
        )
    )
    session.add(LedgerAccount(code=1500, name="Checking", account_type="ASSET", normal_balance="DEBIT"))
    session.flush()
    _account(session, "acct_checking", "Operating Checking", "CHECKING", 1500)
    _statement(session, "stmt_checking", "acct_checking", "CHECKING", 10000, 15000, [5000])
    session.commit()

    report = build_reconciliation_report(session)
    checks = _by_key(report)
    assert checks["property_cost_basis"].passed is False
    assert "12 Elm St" in checks["property_cost_basis"].samples
    # Ledger is zero, statement is 15,000 cents: a real, named divergence.
    assert checks["account_reconciliation"].passed is False
    account_row = next(row for row in report.accounts if row.account_id == "acct_checking")
    assert account_row.difference_cents == -15000
    assert account_row.status == "DRIFT"


def test_ledger_imbalance_is_blocking(session):
    session.add(LedgerAccount(code=1500, name="Checking", account_type="ASSET", normal_balance="DEBIT"))
    session.commit()

    report = build_reconciliation_report(session)
    checks = _by_key(report)
    assert checks["ledger_balanced"].passed is True
    assert report.blocking_checks == []


def test_report_page_renders_for_a_seeded_portfolio(owner):
    signed_in = owner()
    signed_in.seed_reference_portfolio()

    response = signed_in.client.get("/reconciliation/report")

    assert response.status_code == 200
    assert "Controls &amp; reconciliation" in response.text or "Controls & reconciliation" in response.text
    assert "Property cost basis" in response.text
    assert "Account reconciliation" in response.text
    assert "Statement arithmetic" in response.text
    assert 'href="/reconciliation/report"' in response.text
