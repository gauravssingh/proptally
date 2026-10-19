import json
from datetime import timedelta
from urllib.parse import unquote_plus

from sqlmodel import Session, select

from proptally.db.models import (
    Document,
    DocumentExtraction,
    FinancialAccount,
    FinancialInstitution,
    Statement,
    utc_now,
)


def _extraction_data(account_id: str, institution_id: str) -> str:
    return json.dumps({
        "statement_type": "CHECKING",
        "statement_date": "2026-01-31",
        "period_start_date": "2026-01-01",
        "period_end_date": "2026-01-31",
        "financial_account_id": account_id,
        "institution_id": institution_id,
        "opening_balance_cents": 100_000,
        "closing_balance_cents": 120_000,
        "transactions": [],
    })


def test_bulk_import_skips_duplicate_periods(owner):
    signed_in = owner()
    base = utc_now()
    with Session(signed_in.engine) as session:
        session.add(FinancialInstitution(id="inst_bulk", name="Bulk Bank"))
        session.flush()
        session.add(FinancialAccount(
            id="acct_bulk",
            institution_id="inst_bulk",
            account_name="Bulk Checking",
            account_type="CHECKING",
            account_number_masked="...1234",
        ))
        for index in range(2):
            session.add(Document(
                id=f"doc_bulk_import_{index}",
                file_sha256=f"sha_bulk_import_{index}",
                original_filename=f"statement-{index}.pdf",
                storage_path=f"/tmp/statement-{index}.pdf",
                file_size_bytes=100,
                uploaded_at=base + timedelta(minutes=index),
            ))
            session.add(DocumentExtraction(
                id=f"ext_bulk_import_{index}",
                document_id=f"doc_bulk_import_{index}",
                parser_name="TestParser",
                parser_version="1",
                validation_status="VALIDATED",
                approval_status="APPROVED",
                extracted_data_json=_extraction_data("acct_bulk", "inst_bulk"),
            ))
        session.commit()

    response = signed_in.client.post(
        "/statements/import-approved",
        data={"csrf_token": signed_in.csrf()},
        follow_redirects=False,
    )
    assert response.status_code == 303
    message = unquote_plus(response.headers["location"])
    assert "Imported 1 statement" in message
    assert "Skipped 1 duplicate period" in message

    with Session(signed_in.engine) as session:
        statements = session.exec(select(Statement)).all()
        assert len(statements) == 1
        extractions = session.exec(select(DocumentExtraction)).all()
        assert sum(1 for item in extractions if item.imported_statement_id) == 1
