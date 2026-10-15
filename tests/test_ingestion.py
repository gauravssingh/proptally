import json
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import event
from sqlmodel import Session, select

from proptally.db.connection import get_engine
from proptally.db.models import (
    Document,
    DocumentExtraction,
    EscrowAccount,
    FinancialAccount,
    FinancialInstitution,
    JournalEntry,
    LoanAccount,
    MortgagePeriod,
    Property,
    Statement,
)
from proptally.ingestion.parsers import parse_mortgage_pdf, validate_mortgage_extraction
from proptally.ingestion.service import (
    _sha256,
    approve_extraction,
    import_approved_extraction,
    ingest_folder,
    initialize_database,
    update_extraction,
)

VALID_TEXT = """PNC Bank Account Number 0700000002
Statement Date 10/01/2026
Payment Due Date 11/01/2026
Outstanding Principal $210,419.62
Interest Rate 2.500%
Principal $521.79
Interest $438.37
Escrow (Taxes and Insurance) $841.32
Total Amount Due $1,801.48
"""


def _write_pdf(path: Path, text: str = VALID_TEXT) -> None:
    import pymupdf

    pdf = pymupdf.open()
    page = pdf.new_page()
    page.insert_text((40, 50), text)
    pdf.save(path)
    pdf.close()


def _register_pnc_loan(engine) -> None:
    """Registers the loan these fixture statements are about.

    A statement is only ever booked against an account the portfolio already
    holds, so a test that expects an import registers the loan first -- the same
    thing an operator does before pointing the ingester at a new portfolio.
    """
    with Session(engine) as session:
        session.add(Property(
            id="prop_cimmaron_pingree", name="582 Foxglove Ct", address_line1="582 Foxglove Ct",
            city="Oakhaven", state="IL", zip_code="60199", property_type="RENTAL_INVESTMENT",
        ))
        session.add(FinancialInstitution(id="inst_pnc", name="PNC Bank"))
        session.flush()
        session.add(FinancialAccount(
            id="acct_pnc_0708", institution_id="inst_pnc", account_name="PNC Mortgage Loan",
            account_type="MORTGAGE", account_number_masked="...0708",
        ))
        session.flush()
        session.add(LoanAccount(
            id="loan_pnc_1000000002", financial_account_id="acct_pnc_0708",
            property_id="prop_cimmaron_pingree", loan_number="0700000002",
        ))
        session.commit()


def _bare_engine(tmp_path):
    """A portfolio database with nothing registered in it yet."""
    engine = get_engine(f"sqlite:///{tmp_path / 'proptally.db'}")
    initialize_database(engine)
    return engine


def _engine(tmp_path):
    engine = _bare_engine(tmp_path)
    _register_pnc_loan(engine)
    return engine


def test_parser_output_is_extracted_then_validated_but_not_approved():
    parsed = parse_mortgage_pdf(Path("PNC Oct 2026.pdf"), VALID_TEXT)
    assert parsed is not None
    assert parsed.status == "EXTRACTED"
    assert parsed.statement_date == date(2026, 10, 1)
    assert parsed.reported_interest_rate_milli_percent == 2500
    assert parsed.interest_cents == 43837

    validated = validate_mortgage_extraction(parsed)
    assert validated.status == "VALIDATED"
    assert "approval" not in validated.to_dict()


def test_missing_rate_and_inconsistent_amounts_require_review():
    missing_rate = VALID_TEXT.replace("Interest Rate 2.500%\n", "")
    result = validate_mortgage_extraction(parse_mortgage_pdf(Path("PNC Oct 2026.pdf"), missing_rate))
    assert result.status == "NEEDS_REVIEW"
    assert "Could not extract reported interest rate." in result.review_reasons

    inconsistent = VALID_TEXT.replace("Total Amount Due $1,801.48", "Total Amount Due $1,801.49")
    result = validate_mortgage_extraction(parse_mortgage_pdf(Path("PNC Oct 2026.pdf"), inconsistent))
    assert result.status == "NEEDS_REVIEW"
    assert "Principal + interest + escrow + fees does not equal total due." in result.review_reasons


def test_parser_does_not_confuse_balance_or_ytd_fields_with_payment_amounts():
    text = """PNC Bank Account Number 0700000002
Statement Date 10/01/2026
Payment Due Date 11/01/2026
Principal Balance $999,999.99
Escrow Balance $12,345.67
Year-to-Date Principal $4,321.00
Year-to-Date Interest $3,210.00
Interest Rate 2.500%
Principal $521.79
Interest $438.37
Escrow (Taxes and Insurance) $841.32
Total Amount Due $1,801.48
"""
    result = validate_mortgage_extraction(parse_mortgage_pdf(Path("pnc.pdf"), text))
    assert result.status == "VALIDATED"
    assert result.principal_cents == 52179
    assert result.interest_cents == 43837
    assert result.escrow_cents == 84132
    assert result.outstanding_principal_cents == 99999999
    assert result.escrow_balance_cents == 1234567


def test_parser_accepts_reordered_payment_fields_and_optional_fee():
    text = """PNC Bank Account Number 0700000002
Payment Due Date 11/01/2026
Statement Date 10/01/2026
Total Amount Due $1,851.48
Fees $50.00
Escrow $841.32
Interest $438.37
Principal $521.79
Interest Rate 2.500%
Outstanding Principal $210,419.62
"""
    result = validate_mortgage_extraction(parse_mortgage_pdf(Path("pnc.pdf"), text))
    assert result.status == "VALIDATED"
    assert result.fees_cents == 5000
    assert result.total_due_cents == 185148


def test_parser_marks_duplicate_payment_labels_ambiguous():
    text = VALID_TEXT.replace(
        "Interest Rate 2.500%", "Interest Rate 2.500%\nFees $10.00\nFees $20.00"
    )
    result = validate_mortgage_extraction(parse_mortgage_pdf(Path("pnc.pdf"), text))
    assert result.status == "NEEDS_REVIEW"
    assert "Multiple candidate values found for fees." in result.review_reasons


def test_sha256_reads_content(tmp_path):
    source = tmp_path / "statement.pdf"
    source.write_bytes(b"synthetic fixture")
    assert len(_sha256(source)) == 64


def test_unrelated_account_digits_cannot_match_a_loan_number():
    result = parse_mortgage_pdf(Path("checking.pdf"), "Account 0764\nStatement 1293\nBalance 59.00")
    assert result is None


def test_loan_number_in_transaction_description_does_not_identify_statement():
    result = parse_mortgage_pdf(Path("checking.pdf"), "Mortgage ACH 0700000001 paid from checking")
    assert result is None


def test_unapproved_extraction_cannot_import_or_update_authoritative_terms(tmp_path):
    folder = tmp_path / "source"
    folder.mkdir()
    _write_pdf(folder / "pnc.pdf")
    engine = _engine(tmp_path)

    with Session(engine) as session:
        report = ingest_folder(folder, session, tmp_path / "vault", commit=True)
        extraction_id = report["items"][0]["extraction_id"]
        assert report["supported"] == 1
        assert report["items"][0]["status"] == "VALIDATED"
        assert session.exec(select(MortgagePeriod)).all() == []
        assert session.exec(select(Statement)).all() == []
        # The registered loan's terms are untouched until a statement is imported.
        assert session.get(LoanAccount, "loan_pnc_1000000002").current_interest_rate_bps is None
        with pytest.raises(ValueError, match="explicitly approved"):
            import_approved_extraction(session, extraction_id)

        record = session.get(DocumentExtraction, extraction_id)
        assert record.approval_status == "PENDING"
        approved = approve_extraction(session, extraction_id, commit=True)
        assert approved.approval_status == "APPROVED"
        # Approval alone books nothing and still leaves the terms alone.
        assert session.exec(select(MortgagePeriod)).all() == []
        assert session.get(LoanAccount, "loan_pnc_1000000002").current_interest_rate_bps is None

        statement = import_approved_extraction(session, extraction_id, commit=True)
        repeated = import_approved_extraction(session, extraction_id, commit=True)
        period = session.exec(select(MortgagePeriod)).one()
        loan = session.get(LoanAccount, "loan_pnc_1000000002")
        assert repeated.id == statement.id
        assert period.statement_date == date(2026, 10, 1)
        assert loan.current_interest_rate_bps == 2500
        assert session.exec(select(JournalEntry)).all() == []


def test_importing_one_pnc_statement_does_not_create_other_portfolio_entities(tmp_path):
    folder = tmp_path / "source"
    folder.mkdir()
    _write_pdf(folder / "pnc.pdf")
    engine = _engine(tmp_path)

    with Session(engine) as session:
        report = ingest_folder(folder, session, tmp_path / "vault", commit=True)
        extraction_id = report["items"][0]["extraction_id"]
        approve_extraction(session, extraction_id, commit=True)
        import_approved_extraction(session, extraction_id, commit=True)

        assert {item.id for item in session.exec(select(Property)).all()} == {"prop_cimmaron_pingree"}
        assert {item.id for item in session.exec(select(FinancialInstitution)).all()} == {"inst_pnc"}
        assert {item.id for item in session.exec(select(FinancialAccount)).all()} == {"acct_pnc_0708"}
        assert {item.id for item in session.exec(select(LoanAccount)).all()} == {"loan_pnc_1000000002"}
        assert {item.loan_account_id for item in session.exec(select(EscrowAccount)).all()} == {
            "loan_pnc_1000000002"
        }


def test_import_preserves_existing_client_entity_attributes(tmp_path):
    folder = tmp_path / "source"
    folder.mkdir()
    _write_pdf(folder / "pnc.pdf")
    engine = _bare_engine(tmp_path)

    with Session(engine) as session:
        session.add(Property(
            id="prop_cimmaron_pingree", name="Client Property Name",
            address_line1="Client Address", city="Client City", state="IL",
            zip_code="60197", property_type="PRIMARY_RESIDENCE",
        ))
        session.add(FinancialInstitution(id="inst_pnc", name="Client Institution Name"))
        session.flush()
        session.add(FinancialAccount(
            id="acct_pnc_0708", institution_id="inst_pnc",
            account_name="Client Account Name", account_type="MORTGAGE",
            account_number_masked="...0708",
        ))
        session.add(LoanAccount(
            id="loan_pnc_1000000002", financial_account_id="acct_pnc_0708",
            property_id="prop_cimmaron_pingree", loan_number="0700000002",
            original_principal_cents=25000000, current_interest_rate_bps=3500,
        ))
        session.flush()
        session.add(EscrowAccount(
            id="client_escrow", loan_account_id="loan_pnc_1000000002",
            opening_balance_cents=123456,
        ))
        session.commit()

        report = ingest_folder(folder, session, tmp_path / "vault", commit=True)
        extraction_id = report["items"][0]["extraction_id"]
        approve_extraction(session, extraction_id, commit=True)
        import_approved_extraction(session, extraction_id, commit=True)

        assert session.get(Property, "prop_cimmaron_pingree").name == "Client Property Name"
        assert session.get(FinancialInstitution, "inst_pnc").name == "Client Institution Name"
        assert session.get(FinancialAccount, "acct_pnc_0708").account_name == "Client Account Name"
        loan = session.get(LoanAccount, "loan_pnc_1000000002")
        assert loan.original_principal_cents == 25000000
        assert loan.current_interest_rate_bps == 2500
        assert session.get(EscrowAccount, "client_escrow").opening_balance_cents == 123456


def test_newest_approved_statement_controls_rate_and_duplicates_are_idempotent(tmp_path):
    folder = tmp_path / "source"
    folder.mkdir()
    newest = VALID_TEXT
    oldest = VALID_TEXT.replace("10/01/2026", "09/01/2026").replace("11/01/2026", "10/01/2026").replace("2.500%", "3.125%")
    _write_pdf(folder / "a-new.pdf", newest)
    _write_pdf(folder / "z-old.pdf", oldest)
    engine = _engine(tmp_path)

    with Session(engine) as session:
        report = ingest_folder(folder, session, tmp_path / "vault", commit=True)
        assert report["imported"] == 2
        assert session.exec(select(MortgagePeriod)).all() == []
        extraction_ids = {item["file"]: item["extraction_id"] for item in report["items"]}
        approve_extraction(session, extraction_ids["a-new.pdf"], commit=True)
        import_approved_extraction(session, extraction_ids["a-new.pdf"], commit=True)
        approve_extraction(session, extraction_ids["z-old.pdf"], commit=True)
        import_approved_extraction(session, extraction_ids["z-old.pdf"], commit=True)
        loan = session.get(LoanAccount, "loan_pnc_1000000002")
        periods = session.exec(select(MortgagePeriod)).all()
        repeated = ingest_folder(folder, session, tmp_path / "vault", commit=True)

    assert loan.current_interest_rate_bps == 2500
    assert len(periods) == 2
    assert repeated["duplicates"] == 2
    assert repeated["imported"] == 0


def test_missing_statement_date_is_persisted_for_future_review(tmp_path):
    folder = tmp_path / "source"
    folder.mkdir()
    _write_pdf(folder / "missing-date.pdf", VALID_TEXT.replace("Statement Date 10/01/2026\n", ""))
    engine = _engine(tmp_path)

    with Session(engine) as session:
        report = ingest_folder(folder, session, tmp_path / "vault", commit=True)
        record = session.get(DocumentExtraction, report["items"][0]["extraction_id"])
        data = json.loads(record.extracted_data_json)
        assert report["needs_review"] == 1
        assert report["items"][0]["status"] == "NEEDS_REVIEW"
        assert data["statement_date"] is None
        assert data["reported_interest_rate_milli_percent"] == 2500
        assert session.exec(select(Statement)).all() == []
        assert session.exec(select(MortgagePeriod)).all() == []


def test_corrupt_pdf_does_not_abort_following_files(tmp_path):
    folder = tmp_path / "source"
    folder.mkdir()
    (folder / "a-corrupt.pdf").write_bytes(b"not a PDF")
    _write_pdf(folder / "b-valid.pdf")
    engine = _engine(tmp_path)

    with Session(engine) as session:
        report = ingest_folder(folder, session, tmp_path / "vault", commit=True)
        documents = session.exec(select(Document)).all()

    error = next(item for item in report["items"] if item["file"] == "a-corrupt.pdf")
    assert report["errors"] == 1
    assert "a-corrupt.pdf" == error["file"]
    assert error["error"]
    assert report["imported"] == 1
    assert [document.original_filename for document in documents] == ["b-valid.pdf"]


def test_dry_run_makes_no_database_or_vault_changes_and_preserves_source(tmp_path):
    folder = tmp_path / "source"
    folder.mkdir()
    source = folder / "pnc.pdf"
    _write_pdf(source)
    original = source.read_bytes()
    vault = tmp_path / "vault"
    engine = _engine(tmp_path)

    with Session(engine) as session:
        report = ingest_folder(folder, session, vault, commit=False)
        assert session.exec(select(Document)).all() == []
        assert session.exec(select(DocumentExtraction)).all() == []

    assert report["items"][0]["status"] == "VALIDATED"
    assert not vault.exists()
    assert source.read_bytes() == original


def test_failed_persistence_cleans_up_or_reports_vault_file(tmp_path):
    folder = tmp_path / "source"
    folder.mkdir()
    _write_pdf(folder / "pnc.pdf")
    vault = tmp_path / "vault"
    engine = _engine(tmp_path)

    def fail_document_extraction_insert(mapper, connection, target):
        raise RuntimeError("simulated extraction persistence failure")

    event.listen(DocumentExtraction, "before_insert", fail_document_extraction_insert)
    try:
        with Session(engine) as session:
            report = ingest_folder(folder, session, vault, commit=True)
            assert session.exec(select(Document)).all() == []
            assert session.exec(select(DocumentExtraction)).all() == []
    finally:
        event.remove(DocumentExtraction, "before_insert", fail_document_extraction_insert)

    item = report["items"][0]
    assert report["errors"] == 1
    assert item["status"] == "ERROR"
    assert "simulated extraction persistence failure" in item["error"]
    assert not list(vault.rglob("*.pdf"))
    assert "orphaned_vault_file" not in item


def test_real_data_app_mode_skips_synthetic_demo_seed(tmp_path, monkeypatch, owner):
    database_url = f"sqlite:///{tmp_path / 'real-data.db'}"
    monkeypatch.setenv("PROPTALLY_DEMO_SEED", "false")
    signed_in = owner(db_url=database_url)
    assert signed_in.client.get("/mortgages").status_code == 200

    with Session(signed_in.engine) as session:
        assert session.exec(select(Property)).all() == []


def test_section_scoped_parsing_resolves_ytd_table_ambiguity():
    text = """PNC Bank Account Number 0700000002
Statement Date 03/02/2026
Payment Due Date 04/01/2026
Interest Rate 2.500%
Outstanding Principal $210,940.32
Explanation of Amount Due
Principal $520.70
Interest $439.46
Escrow (Taxes and Insurance) $841.32
Total Amount Due $1,801.48
Past Payments Breakdown
Paid Since Last Statement
Principal $519.62
Paid Year to Date
Principal $1,038.16
Interest $882.16
"""
    result = validate_mortgage_extraction(parse_mortgage_pdf(Path("pnc.pdf"), text))
    assert result.status == "VALIDATED"
    assert result.principal_cents == 52070
    assert result.interest_cents == 43946
    assert result.escrow_cents == 84132
    assert result.total_due_cents == 180148
    assert result.outstanding_principal_cents == 21094032


def test_annual_escrow_account_disclosure_is_classified_as_escrow_analysis():
    text = """Rocket Mortgage Loan number 0700000001
Annual escrow account disclosure statement
Statement Date 09/18/2026
Projected minimum balance: $1,347.52
"""
    result = validate_mortgage_extraction(parse_mortgage_pdf(Path("rocket.pdf"), text))
    assert result.statement_type == "ESCROW_ANALYSIS"
    assert result.status == "NEEDS_REVIEW"
    assert "Auxiliary tax/escrow documents are retained for review, not booked as monthly activity." in result.review_reasons


def test_update_extraction_corrects_needs_review_and_allows_import(tmp_path):
    folder = tmp_path / "source"
    folder.mkdir()
    # Inconsistent total due forces NEEDS_REVIEW
    bad_text = VALID_TEXT.replace("Total Amount Due $1,801.48", "Total Amount Due $1,801.99")
    _write_pdf(folder / "pnc_bad.pdf", bad_text)
    vault = tmp_path / "vault"
    engine = _engine(tmp_path)

    with Session(engine) as session:
        report = ingest_folder(folder, session, vault, commit=True)
        extraction_id = report["items"][0]["extraction_id"]
        assert report["items"][0]["status"] == "NEEDS_REVIEW"

        # Correcting the total promotes it to VALIDATED
        updated = update_extraction(session, extraction_id, {"total_due_cents": 180148}, commit=True)
        assert updated.validation_status == "VALIDATED"
        assert updated.approval_status == "PENDING"

        # Now it can be approved and imported
        approved = approve_extraction(session, extraction_id, commit=True)
        assert approved.approval_status == "APPROVED"

        statement = import_approved_extraction(session, extraction_id, commit=True)
        assert statement.ingestion_status == "COMMITTED"

        # Updating an imported extraction must be rejected
        with pytest.raises(ValueError, match="Cannot update an already-imported extraction"):
            update_extraction(session, extraction_id, {"total_due_cents": 180148})


def test_update_extraction_invalidating_resets_approval(tmp_path):
    folder = tmp_path / "source"
    folder.mkdir()
    _write_pdf(folder / "pnc.pdf", VALID_TEXT)
    vault = tmp_path / "vault"
    engine = _engine(tmp_path)

    with Session(engine) as session:
        report = ingest_folder(folder, session, vault, commit=True)
        extraction_id = report["items"][0]["extraction_id"]
        approve_extraction(session, extraction_id, commit=True)

        # Breaking the balance by changing principal
        updated = update_extraction(session, extraction_id, {"principal_cents": 99999}, commit=True)
        assert updated.validation_status == "NEEDS_REVIEW"
        assert updated.approval_status == "PENDING"
        assert updated.approved_at is None

