"""End-to-end tie-out probes promoted from the independent review.

These assert the properties a CPA actually relies on: NOI includes every
operating account, a utility allocation reaches its own ledger code, the
Schedule E CSV foots to Line 20, and staged splits do not leak across tax years.
"""

import csv
import io
from datetime import date

from sqlmodel import select

from proptally.db.models import (
    Document,
    FinancialAccount,
    FinancialInstitution,
    JournalEntryLine,
    Property,
    Statement,
    Transaction,
)
from proptally.domain.accounting.coa import UTILITIES_CODE
from proptally.domain.accounting.metrics import calculate_property_financials
from proptally.domain.tax.schedule_e import (
    export_schedule_e_csv,
    generate_schedule_e_workpaper,
)
from proptally.domain.transactions.splits import (
    SplitItem,
    apply_transaction_splits,
    post_transaction_to_ledger,
)


def _setup(session):
    inst = FinancialInstitution(id="i1", name="Bank")
    prop = Property(
        id="p1",
        name="Door",
        address_line1="1 A St",
        city="C",
        state="IL",
        zip_code="1",
        property_type="RENTAL_INVESTMENT",
    )
    acct = FinancialAccount(
        id="a1",
        institution_id="i1",
        account_name="Checking",
        account_type="CHECKING",
        account_number_masked="...1111",
    )
    doc = Document(
        id="d1",
        file_sha256="x",
        original_filename="x",
        storage_path="x",
        file_size_bytes=1,
    )
    stmt = Statement(
        id="s1",
        document_id="d1",
        financial_account_id="a1",
        statement_type="CHECKING",
        active_parser_name="x",
        active_parser_version="1",
        statement_date=date(2026, 1, 1),
    )
    session.add(inst)
    session.flush()
    session.add(prop)
    session.flush()
    session.add(acct)
    session.flush()
    session.add(doc)
    session.flush()
    session.add(stmt)
    session.flush()
    return prop, acct, stmt


def _post(session, acct, pid, category, cents, tid, *, day=1, post=True):
    txn = Transaction(
        id=tid,
        statement_id="s1",
        financial_account_id=acct.id,
        transaction_date=date(2026, 3, day),
        amount_cents=-cents,
        direction="OUTFLOW",
        raw_description=category,
    )
    session.add(txn)
    session.flush()
    apply_transaction_splits(
        session,
        txn.id,
        [
            SplitItem(
                property_id=pid,
                category=category,
                amount_cents=cents,
                tax_review_status="USER_CLASSIFIED",
            )
        ],
    )
    if post:
        post_transaction_to_ledger(session, txn.id)
    return txn


def test_noi_includes_management_commissions_and_hoa(session):
    prop, acct, _stmt = _setup(session)
    session.commit()
    _post(session, acct, prop.id, "MANAGEMENT", 20000, "t1")
    _post(session, acct, prop.id, "COMMISSIONS", 150000, "t2")
    _post(session, acct, prop.id, "HOA", 15000, "t3")

    fin = calculate_property_financials(session, property_id=prop.id, year=2026)
    assert fin["total_operating_expenses_cents"] == 185000
    assert fin["other_operating_expense_cents"] == 185000
    assert fin["noi_cents"] == -185000
    assert fin["economic_net_income_cents"] == -185000


def test_utilities_post_to_the_utilities_account_not_personal_draw(session):
    prop, acct, _stmt = _setup(session)
    session.commit()
    _post(session, acct, prop.id, "UTILITIES", 12345, "t1")

    codes = {
        line.ledger_account_code
        for line in session.exec(
            select(JournalEntryLine).where(JournalEntryLine.property_id == prop.id)
        ).all()
    }
    assert UTILITIES_CODE in codes
    assert 5090 not in codes

    fin = calculate_property_financials(session, property_id=prop.id, year=2026)
    assert fin["total_operating_expenses_cents"] == 12345
    assert fin["noi_cents"] == -12345

    workpaper = generate_schedule_e_workpaper(session, 2026)
    column = next(c for c in workpaper.columns if c.property_id == prop.id)
    assert column.line_17_utilities == 12345
    assert column.line_20_total_expenses == 12345


def test_schedule_e_csv_foots_to_line_20(session):
    prop, acct, _stmt = _setup(session)
    session.commit()
    _post(session, acct, prop.id, "MANAGEMENT", 20000, "t1")
    _post(session, acct, prop.id, "COMMISSIONS", 150000, "t2")
    _post(session, acct, prop.id, "HOA", 15000, "t3")

    workpaper = generate_schedule_e_workpaper(session, 2026)
    column = next(c for c in workpaper.columns if c.property_id == prop.id)
    assert column.line_11_management == 20000
    assert column.line_8_commissions == 150000
    assert column.line_19_other == 15000
    assert column.line_20_total_expenses == 185000

    rows = list(csv.reader(io.StringIO(export_schedule_e_csv(workpaper))))
    by_line = {row[0]: row for row in rows if row and row[0].startswith("Line ")}
    total_rental = int(round(float(by_line["Line 20"][-1]) * 100))
    expense_sum = 0
    for label, row in by_line.items():
        if label in {"Line 3", "Line 20", "Line 21"}:
            continue
        expense_sum += int(round(float(row[-1]) * 100))
    assert total_rental == 185000
    assert expense_sum == total_rental


def test_staged_splits_do_not_leak_across_tax_years(session):
    prop, acct, _stmt = _setup(session)
    session.commit()

    txn = Transaction(
        id="staged_2025",
        statement_id="s1",
        financial_account_id=acct.id,
        transaction_date=date(2025, 12, 31),
        amount_cents=-99999,
        direction="OUTFLOW",
        raw_description="Staged 2025",
    )
    session.add(txn)
    session.flush()
    apply_transaction_splits(
        session,
        txn.id,
        [
            SplitItem(
                property_id=prop.id,
                category="MANAGEMENT",
                amount_cents=99999,
                tax_review_status="CLIENT_CONFIRMED",
            )
        ],
    )
    session.commit()

    wp2025 = generate_schedule_e_workpaper(session, 2025)
    fy2025 = next(
        c for c in wp2025.columns if c.property_id == prop.id
    )
    fy2026 = next(
        c for c in generate_schedule_e_workpaper(session, 2026).columns
        if c.property_id == prop.id
    )
    assert fy2025.line_11_management == 0
    assert wp2025.unposted_split_count == 1
    assert wp2025.unposted_provisional_cents == 99999
    assert fy2026.line_11_management == 0
    assert fy2026.line_20_total_expenses == 0

    # A posted transaction is not counted again from its staged split.
    _post(session, acct, prop.id, "MANAGEMENT", 5000, "posted_2026")
    session.commit()
    fy2026 = next(
        c for c in generate_schedule_e_workpaper(session, 2026).columns
        if c.property_id == prop.id
    )
    assert fy2026.line_11_management == 5000
