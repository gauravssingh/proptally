import io
import json
import zipfile
from urllib.parse import unquote_plus

from sqlmodel import Session, select
from synthetic_statements import bofa_checking_pdf, bofa_credit_card_pdf

from proptally.db.models import Document, DocumentExtraction, FinancialAccount, FinancialInstitution


def test_zip_upload_parses_and_matches_bank_and_card_statements(owner, tmp_path):
    signed_in = owner()
    checking = bofa_checking_pdf(tmp_path / "checking.pdf")
    card = bofa_credit_card_pdf(tmp_path / "card.pdf")
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        bundle.write(checking, "statements/checking.pdf")
        bundle.write(card, "statements/card.pdf")

    with Session(signed_in.engine) as session:
        session.add(FinancialInstitution(id="inst_bofa_zip", name="Bank of America"))
        session.flush()
        session.add(FinancialAccount(
            id="acct_zip_checking",
            institution_id="inst_bofa_zip",
            account_name="BofA Checking",
            account_type="CHECKING",
            account_number_masked="...9456",
        ))
        session.add(FinancialAccount(
            id="acct_zip_card",
            institution_id="inst_bofa_zip",
            account_name="BofA Visa",
            account_type="CREDIT_CARD",
            account_number_masked="...9306",
        ))
        session.commit()

    response = signed_in.post(
        "/statements/upload",
        {},
        files={"file": ("statements.zip", archive.getvalue(), "application/zip")},
        follow_redirects=False,
    )
    assert response.status_code == 303
    message = unquote_plus(response.headers["location"])
    assert "ZIP processed" in message
    assert "2 matched" in message

    with Session(signed_in.engine) as session:
        documents = session.exec(select(Document)).all()
        assert len(documents) == 2
        extractions = session.exec(select(DocumentExtraction)).all()
        assert len(extractions) == 2
        assert all(item.validation_status == "VALIDATED" for item in extractions)
        assert {item.document_id for item in extractions} == {item.id for item in documents}


def test_zip_identifies_new_account_and_relinks_matching_statements(owner, tmp_path):
    signed_in = owner()
    first = bofa_checking_pdf(
        tmp_path / "jan.pdf",
        period_start="December 16, 2025",
        period_end="January 15, 2026",
    )
    second = bofa_checking_pdf(
        tmp_path / "feb.pdf",
        period_start="January 16, 2026",
        period_end="February 15, 2026",
    )
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        bundle.write(first, "jan.pdf")
        bundle.write(second, "feb.pdf")

    response = signed_in.post(
        "/statements/upload",
        {},
        files={"file": ("unmatched.zip", archive.getvalue(), "application/zip")},
        follow_redirects=False,
    )
    assert response.status_code == 303
    message = unquote_plus(response.headers["location"])
    assert "2 need review" in message
    assert "new account" in message
    assert "Bank of America ...9456" in message

    with Session(signed_in.engine) as session:
        extraction = session.exec(select(DocumentExtraction)).first()
        extraction_id = extraction.id

    registered = signed_in.post(
        f"/statements/extractions/{extraction_id}/update",
        {"register_observed_account": "true"},
        follow_redirects=False,
    )
    assert registered.status_code == 303
    assert "Linked 1 matching statement" in unquote_plus(registered.headers["location"])

    with Session(signed_in.engine) as session:
        extractions = session.exec(select(DocumentExtraction)).all()
        assert len(extractions) == 2
        assert all(item.validation_status == "VALIDATED" for item in extractions)
        account_ids = {json.loads(item.extracted_data_json).get("financial_account_id") for item in extractions}
        assert len(account_ids) == 1
        assert None not in account_ids
