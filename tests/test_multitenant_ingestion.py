"""Ingestion for any portfolio, driven the way an operator drives it.

The statements here are synthetic and tiny, built in-process the way the rest of
this suite builds them. What is under test is the path a person without shell
access walks: upload a statement, have it matched against the accounts *this*
portfolio has registered, correct or register what does not match, then approve
and import. Nothing in that path knows any portfolio's account or loan numbers.
"""

import json
from datetime import date
from pathlib import Path

import pytest
from sqlmodel import Session, select
from starlette.testclient import TestClient

from proptally.db.models import (
    Document,
    DocumentExtraction,
    FinancialAccount,
    FinancialInstitution,
    LoanAccount,
    MortgagePeriod,
    Property,
    Statement,
    Transaction,
)
from proptally.ingestion.bank_parsers import BankStatementExtraction
from proptally.ingestion.service import (
    _resolve_counter_account,
    resolve_statement_extraction,
)
from proptally.web.routes.ingestion import _updates_from_form

#: A statement from a servicer no test has ever heard of, for a loan number
#: printed on the document itself.
STATEMENT_TEXT = """Northwind Mortgage Loan Number 0700000009
Statement Date 01/05/2027
Payment Due Date 02/01/2027
Outstanding Principal $180,000.00
Interest Rate 6.125%
Principal $250.00
Interest $918.75
Escrow $400.00
Total Amount Due $1,568.75
"""


def _write_pdf(path: Path, text: str = STATEMENT_TEXT) -> None:
    import pymupdf

    pdf = pymupdf.open()
    page = pdf.new_page()
    page.insert_text((40, 50), text)
    pdf.save(path)
    pdf.close()


def _write_pdf_pages(path: Path, pages: list[str]) -> None:
    import pymupdf

    pdf = pymupdf.open()
    for text in pages:
        page = pdf.new_page()
        page.insert_text((40, 50), text)
    pdf.save(path)
    pdf.close()


#: A financial institution's own template, not a portfolio's: the number on the
#: statement is read off the document, and which account it is is decided by the
#: portfolio that uploads it.
BANK_HEADER_TEXT = """Bank of America
Adv Plus Banking
Account Number 0029 1385 9456
for January 1, 2026 to January 15, 2026
Beginning balance on January 1, 2026 $1,000.00
Ending balance on January 15, 2026 $1,850.00
"""
BANK_ACTIVITY_TEXT = """Page 2 of 3
Deposits and other additions
01/05/26
DEPOSIT CARPENTERSVILLE ATM
$850.00
Total deposits and other additions $850.00
"""


@pytest.fixture(name="statement_pdf")
def statement_pdf_fixture(tmp_path) -> Path:
    path = tmp_path / "northwind-jan-2027.pdf"
    _write_pdf(path)
    return path


def _upload(signed_in, path: Path, filename: str = "northwind-jan-2027.pdf", *, with_csrf: bool = True):
    files = {"file": (filename, path.read_bytes(), "application/pdf")}
    data = {"csrf_token": signed_in.csrf()} if with_csrf else {}
    return signed_in.client.post(
        "/statements/upload", data=data, files=files, follow_redirects=False
    )


def _register_loan(engine, *, loan_number: str = "0700000009") -> None:
    """Registers the property, institution, account and loan a statement belongs to."""
    with Session(engine) as session:
        session.add(Property(
            id="prop_lakeview", name="88 Lakeview Ave", address_line1="88 Lakeview Ave",
            city="Springfield", state="IL", zip_code="62701", property_type="RENTAL_INVESTMENT",
        ))
        session.add(FinancialInstitution(id="inst_northwind", name="Northwind Mortgage"))
        session.flush()
        session.add(FinancialAccount(
            id="acct_northwind_0009", institution_id="inst_northwind",
            account_name="Northwind Mortgage Loan", account_type="MORTGAGE",
            account_number_masked="...0009",
        ))
        session.flush()
        session.add(LoanAccount(
            id="loan_northwind_0009", financial_account_id="acct_northwind_0009",
            property_id="prop_lakeview", loan_number=loan_number,
        ))
        session.commit()


def _only_extraction(engine) -> DocumentExtraction:
    with Session(engine) as session:
        return session.exec(select(DocumentExtraction)).one()


def test_uploaded_statement_matches_the_portfolios_own_loan_and_imports(owner, statement_pdf):
    signed_in = owner()
    other = owner(email="someone.else@example.com", portfolio_name="Another Portfolio")
    _register_loan(signed_in.engine)

    response = _upload(signed_in, statement_pdf)
    assert response.status_code == 303

    extraction = _only_extraction(signed_in.engine)
    data = json.loads(extraction.extracted_data_json)
    assert extraction.validation_status == "VALIDATED"
    assert data["servicer_name"] == "Northwind Mortgage"
    assert data["loan_number"] == "0700000009"
    # Identified from this portfolio's own records, not from the document.
    assert data["loan_account_id"] == "loan_northwind_0009"
    assert data["financial_account_id"] == "acct_northwind_0009"
    assert data["property_id"] == "prop_lakeview"

    with Session(signed_in.engine) as session:
        # Staged only: nothing has been booked as authoritative yet.
        assert session.exec(select(MortgagePeriod)).all() == []
        assert session.exec(select(Statement)).all() == []
        assert session.get(Document, extraction.document_id).original_filename == "northwind-jan-2027.pdf"

    assert signed_in.post(f"/statements/extractions/{extraction.id}/approve", {}).status_code == 200
    assert signed_in.post(f"/statements/extractions/{extraction.id}/import", {}).status_code == 200

    with Session(signed_in.engine) as session:
        period = session.exec(select(MortgagePeriod)).one()
        statement = session.exec(select(Statement)).one()
        assert period.loan_account_id == "loan_northwind_0009"
        assert period.statement_date == date(2027, 1, 5)
        assert period.payment_due_date == date(2027, 2, 1)
        assert period.principal_billed_cents == 25000
        assert period.interest_billed_cents == 91875
        assert period.escrow_billed_cents == 40000
        assert period.total_amount_billed_cents == 156875
        assert period.outstanding_principal_cents == 18000000
        assert period.interest_rate_bps == 6125
        assert statement.financial_account_id == "acct_northwind_0009"
        assert statement.document_id == extraction.document_id
        assert session.get(LoanAccount, "loan_northwind_0009").current_interest_rate_bps == 6125
        # Nothing beyond what was already registered was invented.
        assert {record.id for record in session.exec(select(Property)).all()} == {"prop_lakeview"}
        assert {record.id for record in session.exec(select(LoanAccount)).all()} == {"loan_northwind_0009"}
        assert {record.id for record in session.exec(select(FinancialAccount)).all()} == {"acct_northwind_0009"}

    # The other portfolio's database is untouched: no document, no statement, no
    # period, and no account invented from this statement's identity.
    with Session(other.engine) as session:
        assert session.exec(select(Document)).all() == []
        assert session.exec(select(DocumentExtraction)).all() == []
        assert session.exec(select(Statement)).all() == []
        assert session.exec(select(MortgagePeriod)).all() == []
        assert session.exec(select(FinancialAccount)).all() == []


def test_statement_for_an_unregistered_loan_is_retained_with_an_actionable_reason(owner, statement_pdf):
    signed_in = owner()
    other = owner(email="someone.else@example.com", portfolio_name="Another Portfolio")

    assert _upload(signed_in, statement_pdf).status_code == 303

    extraction = _only_extraction(signed_in.engine)
    data = json.loads(extraction.extracted_data_json)
    reasons = data["resolution_reasons"]
    assert extraction.validation_status == "NEEDS_REVIEW"
    assert extraction.approval_status == "PENDING"
    assert any("no registered account matches" in reason.lower() for reason in reasons)
    assert any("register the account and re-run" in reason.lower() for reason in reasons)
    assert data["loan_account_id"] is None
    assert data["financial_account_id"] is None

    with Session(signed_in.engine) as session:
        # Nothing was imported, and no entity was invented at a fixed id.
        assert session.exec(select(Statement)).all() == []
        assert session.exec(select(MortgagePeriod)).all() == []
        assert session.exec(select(Property)).all() == []
        assert session.exec(select(LoanAccount)).all() == []
        assert session.exec(select(FinancialAccount)).all() == []
        assert session.get(Document, extraction.document_id) is not None

    # An unvalidated extraction cannot be approved, so it cannot be imported.
    assert signed_in.post(f"/statements/extractions/{extraction.id}/approve", {}).status_code == 400

    # The reason's instruction works from the browser: register the loan, choose
    # it on the statement, and the same extraction becomes importable.
    _register_loan(signed_in.engine)
    updated = signed_in.post(
        f"/statements/extractions/{extraction.id}/update",
        {"loan_account_id": "loan_northwind_0009"},
    )
    assert updated.status_code == 200

    refreshed = _only_extraction(signed_in.engine)
    assert refreshed.validation_status == "VALIDATED"
    assert json.loads(refreshed.extracted_data_json)["resolution_reasons"] == []

    assert signed_in.post(f"/statements/extractions/{extraction.id}/approve", {}).status_code == 200
    assert signed_in.post(f"/statements/extractions/{extraction.id}/import", {}).status_code == 200
    with Session(signed_in.engine) as session:
        assert session.exec(select(MortgagePeriod)).one().loan_account_id == "loan_northwind_0009"

    with Session(other.engine) as session:
        assert session.exec(select(Document)).all() == []
        assert session.exec(select(MortgagePeriod)).all() == []


def test_corrected_amounts_are_revalidated_rather_than_trusted(owner, statement_pdf):
    signed_in = owner()
    _register_loan(signed_in.engine)
    assert _upload(signed_in, statement_pdf).status_code == 303
    extraction = _only_extraction(signed_in.engine)
    assert extraction.validation_status == "VALIDATED"

    # A corrected total that no longer matches principal + interest + escrow is
    # caught, and the stale approval cannot stand.
    assert signed_in.post(f"/statements/extractions/{extraction.id}/approve", {}).status_code == 200
    corrected = signed_in.post(
        f"/statements/extractions/{extraction.id}/update", {"total_due": "9999.00"}
    )
    assert corrected.status_code == 200
    refreshed = _only_extraction(signed_in.engine)
    assert refreshed.validation_status == "NEEDS_REVIEW"
    assert refreshed.approval_status == "PENDING"
    assert any(
        "does not equal total due" in reason
        for reason in json.loads(refreshed.extracted_data_json)["review_reasons"]
    )


def test_non_pdf_upload_is_rejected_before_anything_is_stored(owner, tmp_path):
    signed_in = owner()

    not_a_pdf = tmp_path / "notes.txt"
    not_a_pdf.write_text("a statement this instance cannot read")
    rejected = _upload(signed_in, not_a_pdf, filename="notes.txt")
    assert rejected.status_code == 400
    assert "%PDF-" in rejected.text

    # Extension is not evidence either: a file named .pdf must still carry the header.
    liar = tmp_path / "liar.pdf"
    liar.write_bytes(b"this is not a pdf")
    assert _upload(signed_in, liar, filename="liar.pdf").status_code == 400

    with Session(signed_in.engine) as session:
        assert session.exec(select(Document)).all() == []
        assert session.exec(select(DocumentExtraction)).all() == []


def test_upload_requires_a_session_and_a_csrf_token(owner, statement_pdf):
    signed_in = owner()
    anonymous = TestClient(signed_in.app)

    unauthenticated = anonymous.post(
        "/statements/upload",
        files={"file": ("northwind-jan-2027.pdf", statement_pdf.read_bytes(), "application/pdf")},
        follow_redirects=False,
    )
    assert unauthenticated.status_code == 303
    assert unauthenticated.headers["location"].startswith("/auth/login")

    no_token = _upload(signed_in, statement_pdf, with_csrf=False)
    assert no_token.status_code == 403

    with Session(signed_in.engine) as session:
        assert session.exec(select(Document)).all() == []
        assert session.exec(select(DocumentExtraction)).all() == []


def test_statements_page_lists_status_approval_and_imported_link(owner, statement_pdf):
    signed_in = owner()
    _register_loan(signed_in.engine)
    assert _upload(signed_in, statement_pdf).status_code == 303

    page = signed_in.client.get("/statements")
    assert page.status_code == 200
    assert "northwind-jan-2027.pdf" in page.text
    assert "VALIDATED" in page.text

    extraction = _only_extraction(signed_in.engine)
    signed_in.post(f"/statements/extractions/{extraction.id}/approve", {})
    signed_in.post(f"/statements/extractions/{extraction.id}/import", {})

    imported = signed_in.client.get("/statements")
    assert imported.status_code == 200
    assert "APPROVED" in imported.text
    with Session(signed_in.engine) as session:
        statement = session.exec(select(Statement)).one()
    assert statement.id in imported.text

    # A second upload of identical bytes is refused with a message rather than
    # stored twice, and the page still shows the one extraction.
    duplicate = _upload(signed_in, statement_pdf)
    assert duplicate.status_code == 303
    assert duplicate.headers["location"].startswith("/statements?message=")
    with Session(signed_in.engine) as session:
        assert len(session.exec(select(Document)).all()) == 1
        assert len(session.exec(select(DocumentExtraction)).all()) == 1


def test_bank_statement_is_matched_by_identity_and_registered_from_the_browser(owner, tmp_path):
    """A bank statement for an account this portfolio has never seen.

    It is recognised by the institution's own template, unmatched because the
    portfolio holds no such account, and then registered from the statement
    itself through the review screen -- no shell, no code change.
    """
    path = tmp_path / "bofa-checking-jan-2026.pdf"
    _write_pdf_pages(path, [BANK_HEADER_TEXT, "Statement continued", BANK_ACTIVITY_TEXT])
    signed_in = owner()

    assert _upload(signed_in, path, filename="bofa-checking-jan-2026.pdf").status_code == 303

    extraction = _only_extraction(signed_in.engine)
    data = json.loads(extraction.extracted_data_json)
    assert extraction.validation_status == "NEEDS_REVIEW"
    assert data["statement_type"] == "CHECKING"
    assert data["institution_name"] == "Bank of America"
    assert data["account_last4"] == "9456"
    reason = " ".join(data["resolution_reasons"]).lower()
    assert "bank of america" in reason and "9456" in reason
    with Session(signed_in.engine) as session:
        assert session.exec(select(FinancialAccount)).all() == []

    # The screen offers the one-click way out of that reason.
    review_page = signed_in.client.get("/statements")
    assert review_page.status_code == 200
    assert "NEEDS REVIEW" in review_page.text
    assert "Register Bank of America account ending 9456" in review_page.text

    registered = signed_in.post(
        f"/statements/extractions/{extraction.id}/update",
        {"register_observed_account": "true"},
    )
    assert registered.status_code == 200

    refreshed = _only_extraction(signed_in.engine)
    refreshed_data = json.loads(refreshed.extracted_data_json)
    assert refreshed.validation_status == "VALIDATED"
    with Session(signed_in.engine) as session:
        account = session.get(FinancialAccount, refreshed_data["financial_account_id"])
        assert account.account_number_masked == "...9456"
        assert account.account_type == "CHECKING"
        assert session.get(FinancialInstitution, account.institution_id).name == "Bank of America"

    assert signed_in.post(f"/statements/extractions/{extraction.id}/approve", {}).status_code == 200
    assert signed_in.post(f"/statements/extractions/{extraction.id}/import", {}).status_code == 200

    with Session(signed_in.engine) as session:
        statement = session.exec(select(Statement)).one()
        assert statement.financial_account_id == account.id
        assert statement.statement_date == date(2026, 1, 15)
        transaction = session.exec(select(Transaction)).one()
        assert transaction.amount_cents == 85000
        assert transaction.direction == "INFLOW"
        # A branch ATM cash deposit names no tenant, so it stays flagged for the
        # client rather than being attributed to a door automatically.
        assert transaction.requires_client_confirmation is True
        assert transaction.counter_account_id is None


def test_bank_statement_identity_is_matched_and_registered_from_the_statement(owner):
    """A bank statement names its own institution and last four; the portfolio decides."""
    signed_in = owner()
    with Session(signed_in.engine) as session:
        session.add(FinancialInstitution(id="inst_bofa", name="Bank of America"))
        session.flush()
        session.add(FinancialAccount(
            id="acct_bofa_chk_9456", institution_id="inst_bofa",
            account_name="Bank of America Advantage Plus Checking",
            account_type="CHECKING", account_number_masked="...9456",
        ))
        session.add(FinancialAccount(
            id="acct_bofa_visa_9306", institution_id="inst_bofa",
            account_name="Bank of America Visa Signature",
            account_type="CREDIT_CARD", account_number_masked="...9306",
            mixed_use_status="MIXED_USE",
        ))
        session.commit()

        extracted = BankStatementExtraction(
            parser_name="BofaCheckingParser", statement_type="CHECKING",
            institution_name="Bank of America", account_last4="9456",
            statement_date=date(2026, 1, 15), period_start_date=date(2026, 1, 1),
            period_end_date=date(2026, 1, 15), opening_balance_cents=100000,
            closing_balance_cents=100000,
        )
        resolve_statement_extraction(session, extracted)
        assert extracted.financial_account_id == "acct_bofa_chk_9456"
        assert extracted.institution_id == "inst_bofa"
        assert extracted.resolution_reasons == []

        # A statement whose account is not registered is flagged, not guessed at,
        # even though another account at the same institution exists.
        unmatched = BankStatementExtraction(
            parser_name="BofaCreditCardParser", statement_type="CREDIT_CARD",
            institution_name="Bank of America", account_last4="1111",
            statement_date=date(2026, 1, 8), period_start_date=date(2025, 12, 9),
            period_end_date=date(2026, 1, 8), opening_balance_cents=1000,
            closing_balance_cents=1000,
        )
        resolve_statement_extraction(session, unmatched)
        assert unmatched.financial_account_id is None
        assert "no registered account matches" in unmatched.resolution_reasons[0].lower()

        # A transfer description names another of the portfolio's own accounts.
        assert _resolve_counter_account(session, "Online payment from CHK 9456", "acct_bofa_visa_9306") == "acct_bofa_chk_9456"
        assert _resolve_counter_account(session, "ROCKET MORTGAGE PAYMENT", "acct_bofa_chk_9456") is None
        assert _resolve_counter_account(session, "Card purchase", "acct_bofa_chk_9456") is None


def test_updates_from_form_ignores_untouched_inputs_and_parses_money(owner):
    """The correction form submits dollars; the ledger stores cents."""
    updates = _updates_from_form({
        "principal": "521.79",
        "interest": "438.37",
        "escrow": "",
        "total_due": "1,801.48",
        "interest_rate_percent": "2.5",
        "statement_date": "2026-10-01",
        "loan_number": "0700000002",
    })
    assert updates["principal_cents"] == 52179
    assert updates["interest_cents"] == 43837
    assert updates["total_due_cents"] == 180148
    assert updates["reported_interest_rate_milli_percent"] == 2500
    assert updates["statement_date"] == "2026-10-01"
    assert updates["loan_number"] == "0700000002"
    assert "escrow_cents" not in updates
    assert "loan_account_id" not in updates


# --- one row, one next step, one dialog -----------------------------------


def _dialog_block(html: str, dialog_id: str) -> str:
    """The markup from a dialog's opening tag to the end of the form inside it.

    A page of stacked forms is what this screen replaced, so "the form is inside a
    closed dialog" is the behaviour under test and has to be readable from the markup.
    """
    start = html.index(f'id="{dialog_id}" class="pt-modal"')
    end = html.index("</form>", start) + len("</form>")
    return html[start:end]


def test_a_validated_statement_offers_approval_in_a_dialog_and_then_import(owner, statement_pdf):
    signed_in = owner()
    _register_loan(signed_in.engine)
    assert _upload(signed_in, statement_pdf).status_code == 303
    extraction = _only_extraction(signed_in.engine)

    page = signed_in.client.get("/statements").text

    # The row's single next step is approval, behind a dialog that opens on its own id.
    assert f'data-modal-open="approve-{extraction.id}"' in page
    assert f'data-modal-open="import-{extraction.id}"' not in page   # nothing to import yet
    assert f'id="approve-{extraction.id}" class="pt-modal" hidden' in page
    assert f'action="/statements/extractions/{extraction.id}/approve"' in _dialog_block(page, f"approve-{extraction.id}")

    # The correction form is in a closed dialog too, and exists exactly once: no inline
    # copy of it is left on the page beside the row.
    review = _dialog_block(page, f"review-{extraction.id}")
    assert f'id="review-{extraction.id}" class="pt-modal" hidden' in page
    assert f'action="/statements/extractions/{extraction.id}/update"' in review
    assert page.count(f'action="/statements/extractions/{extraction.id}/update"') == 1

    # Approving moves the row on to importing; importing ends it, with nothing left to open.
    signed_in.post(f"/statements/extractions/{extraction.id}/approve", {})
    approved = signed_in.client.get("/statements").text
    assert f'data-modal-open="import-{extraction.id}"' in approved
    assert f'data-modal-open="approve-{extraction.id}"' not in approved
    assert f'action="/statements/extractions/{extraction.id}/import"' in _dialog_block(approved, f"import-{extraction.id}")

    signed_in.post(f"/statements/extractions/{extraction.id}/import", {})
    imported = signed_in.client.get("/statements").text
    assert f'data-modal-open="import-{extraction.id}"' not in imported
    assert f'data-modal-open="review-{extraction.id}"' not in imported


def test_a_statement_that_matched_no_account_offers_exactly_one_way_in(owner, statement_pdf):
    signed_in = owner()
    assert _upload(signed_in, statement_pdf).status_code == 303   # no loan registered

    extraction = _only_extraction(signed_in.engine)
    assert extraction.validation_status == "NEEDS_REVIEW"
    page = signed_in.client.get("/statements").text

    # One next step — reviewing it — and the fields to correct are in that dialog.
    assert f'data-modal-open="review-{extraction.id}"' in page
    assert f'data-modal-open="approve-{extraction.id}"' not in page
    assert f'data-modal-open="import-{extraction.id}"' not in page
    assert f'action="/statements/extractions/{extraction.id}/update"' in _dialog_block(page, f"review-{extraction.id}")


def test_a_bank_statement_with_no_registered_account_registers_it_from_the_dialog(owner, tmp_path):
    signed_in = owner()
    path = tmp_path / "boa-jan-2026.pdf"
    _write_pdf_pages(path, [BANK_HEADER_TEXT, BANK_ACTIVITY_TEXT])
    assert _upload(signed_in, path, filename="boa-jan-2026.pdf").status_code == 303

    extraction = _only_extraction(signed_in.engine)
    page = signed_in.client.get("/statements").text

    # The one way forward is the dialog that explains the missing account and offers
    # to register it, so the operator never has to leave the screen for a shell.
    assert f'data-modal-open="review-{extraction.id}"' in page
    dialog = _dialog_block(page, f"review-{extraction.id}")
    assert 'name="register_observed_account"' in dialog
    assert f'action="/statements/extractions/{extraction.id}/update"' in dialog
