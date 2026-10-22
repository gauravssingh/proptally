"""Staging a Document Intelligence job never writes a Statement by itself."""

import json

import httpx
import pytest
from sqlmodel import Session, select

from proptally.db.models import (
    Document,
    DocumentExtraction,
    FinancialAccount,
    FinancialInstitution,
    Statement,
)
from proptally.ingestion.doc_intel import DocumentIntelligenceClient
from proptally.ingestion.service import (
    DocIntelStagingError,
    StatementIngestionService,
    doc_intel_extraction_data,
)

PDF_BYTES = b"%PDF-1.4 test statement"

MASKED_OK = "...9456"


def _bank_result(*, masked=MASKED_OK, valid=True):
    return {
        "document_id": "doc_3182d6096ff329ba",
        "profile_id": "bank_statement",
        "fields": {
            "institution_name": "Bank of America, N.A.",
            "account_type": "CHECKING",
            "account_number_masked": masked,
            "opening_balance_cents": 341227,
            "closing_balance_cents": 414736,
            "statement_period_start": "04/17/2025",
            "statement_period_end": "05/15/2025",
        },
        "cross_field_issues": []
        if valid
        else [{"field": "closing_balance_cents", "message": "Closing balance does not tie."}],
        "cross_field_valid": valid,
        "proptally_payload": {
            "financial_account": {
                "id": "acct_bofa_chk_9456",
                "institution_id": "bofa",
                "account_name": "Bank of America Checking ...9456",
                "account_type": "CHECKING",
                "account_number_masked": MASKED_OK,
            },
            "statement": {
                "id": "stmt_doc_3182d6096ff329ba",
                "statement_type": "CHECKING",
                "active_parser_name": "document_intelligence_lab",
                "active_parser_version": "0.1.0",
                "period_start_date": "2025-04-17",
                "period_end_date": "2025-05-15",
                "statement_date": "2025-05-15",
                "opening_balance_cents": 341227,
                "closing_balance_cents": 414736,
                "ingestion_status": "VALIDATED_READY",
            },
            "loan_account": None,
            "escrow_account": None,
            "metadata": {},
        },
    }


def _client(result, *, status="completed", error=None):
    """A real client answering a fixed job lifecycle over a mock transport."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/jobs/extract/upload":
            return httpx.Response(
                202,
                json={
                    "job_id": "job_test",
                    "status": "queued",
                    "profile_name": "bank_statement",
                    "filename": "statement.pdf",
                },
            )
        if request.url.path == "/api/v1/jobs/job_test":
            return httpx.Response(
                200,
                json={
                    "job_id": "job_test",
                    "status": status,
                    "profile_name": "bank_statement",
                    "filename": "statement.pdf",
                    "document_id": result.get("document_id") if result else None,
                    "error_message": error,
                    "result": result,
                },
            )
        return httpx.Response(404, json={"detail": "not found"})

    return DocumentIntelligenceClient(
        base_url="http://127.0.0.1:8000",
        http_client=httpx.Client(
            base_url="http://127.0.0.1:8000", transport=httpx.MockTransport(handler)
        ),
    )


def _register_bofa_checking(session):
    session.add(FinancialInstitution(id="inst_bank_of_america", name="Bank of America"))
    session.flush()
    session.add(
        FinancialAccount(
            id="acct_bank_of_america_9456",
            institution_id="inst_bank_of_america",
            account_name="Bank of America Checking (...9456)",
            account_type="CHECKING",
            account_number_masked=MASKED_OK,
        )
    )
    session.commit()


def _stage(session, tmp_path, result, *, status="completed", error=None, profile="bank_statement"):
    service = StatementIngestionService(client=_client(result, status=status, error=error))
    return service.extract_and_stage(
        session,
        tmp_path / "vault",
        payload=PDF_BYTES,
        original_filename="statement.pdf",
        profile_name=profile,
        timeout=5.0,
    )


def test_extraction_data_uses_iso_dates_and_integer_cents():
    result = _bank_result()
    data = doc_intel_extraction_data(
        "bank_statement", result["fields"], result["proptally_payload"]
    )

    assert data["statement_type"] == "CHECKING"
    assert data["statement_date"] == "2025-05-15"
    assert data["period_start_date"] == "2025-04-17"
    assert data["opening_balance_cents"] == 341227
    assert data["account_last4"] == "9456"
    # Identity is resolved against this portfolio, never taken from the sidecar.
    assert data["financial_account_id"] is None


def test_extraction_data_rejects_an_unknown_profile_and_a_missing_date():
    with pytest.raises(DocIntelStagingError):
        doc_intel_extraction_data("insurance_policy", {}, {"statement": {}})
    with pytest.raises(DocIntelStagingError):
        doc_intel_extraction_data("bank_statement", {}, {"statement": {}})


def test_staging_attaches_a_known_account_and_writes_no_statement(session, tmp_path):
    _register_bofa_checking(session)

    staged = _stage(session, tmp_path, _bank_result())

    assert staged.created is True
    assert staged.extraction.validation_status == "VALIDATED"
    assert staged.extraction.approval_status == "PENDING"
    stored = json.loads(staged.extraction.extracted_data_json)
    assert stored["financial_account_id"] == "acct_bank_of_america_9456"
    assert stored["institution_id"] == "inst_bank_of_america"
    assert stored["period_start_date"] == "2025-04-17"
    assert stored["source_document_id"] == "doc_3182d6096ff329ba"
    assert staged.document.storage_path.endswith(".pdf")
    assert session.exec(select(Statement)).all() == []


def test_staging_an_unknown_account_lands_in_review(session, tmp_path):
    staged = _stage(session, tmp_path, _bank_result())

    stored = json.loads(staged.extraction.extracted_data_json)
    assert staged.extraction.validation_status == "NEEDS_REVIEW"
    assert stored["financial_account_id"] is None
    assert "Bank of America" in stored["resolution_reasons"][0]


def test_a_failed_cross_field_check_lands_in_review(session, tmp_path):
    _register_bofa_checking(session)

    staged = _stage(session, tmp_path, _bank_result(valid=False))

    stored = json.loads(staged.extraction.extracted_data_json)
    assert staged.extraction.validation_status == "NEEDS_REVIEW"
    assert "Closing balance" in stored["review_reasons"][0]


def test_resubmitting_the_same_document_stages_one_extraction(session, tmp_path):
    _register_bofa_checking(session)

    first = _stage(session, tmp_path, _bank_result())
    second = _stage(session, tmp_path, _bank_result())

    assert first.created is True
    assert second.created is False
    assert second.extraction.id == first.extraction.id
    assert len(session.exec(select(Document)).all()) == 1
    assert len(session.exec(select(DocumentExtraction)).all()) == 1


def test_sidecar_output_supersedes_a_local_parser_extraction(session, tmp_path):
    from proptally.ingestion.service import store_document

    _register_bofa_checking(session)
    stored = store_document(
        session,
        tmp_path / "vault",
        payload=PDF_BYTES,
        original_filename="statement.pdf",
        parse=False,
    )
    session.add(
        DocumentExtraction(
            id="ext_local",
            document_id=stored.document.id,
            parser_name="legacy_bank_parser",
            parser_version="1",
            validation_status="VALIDATED",
            extracted_data_json=json.dumps({"statement_type": "CHECKING"}),
        )
    )
    session.commit()

    staged = _stage(session, tmp_path, _bank_result())

    assert staged.created is True
    assert staged.extraction.parser_name == "document_intelligence_lab"
    assert len(session.exec(select(DocumentExtraction)).all()) == 2


def test_a_failed_job_raises_and_stores_nothing(session, tmp_path):
    service = StatementIngestionService(
        client=_client(None, status="failed", error="PDF is encrypted")
    )

    with pytest.raises(DocIntelStagingError, match="PDF is encrypted"):
        service.extract_and_stage(
            session,
            tmp_path / "vault",
            payload=PDF_BYTES,
            original_filename="statement.pdf",
            profile_name="bank_statement",
            timeout=5.0,
        )

    assert session.exec(select(Document)).all() == []


def test_a_job_without_a_payload_is_refused(session, tmp_path):
    result = _bank_result()
    result["proptally_payload"] = None

    with pytest.raises(DocIntelStagingError, match="proptally_payload"):
        _stage(session, tmp_path, result)


def test_a_mortgage_without_a_payment_split_is_staged_for_review(session, tmp_path):
    result = {
        "document_id": "doc_mtg",
        "profile_id": "mortgage_statement",
        "fields": {
            "lender": "Rocket Mortgage",
            "loan_number": "1000000002",
            "outstanding_principal_cents": 21041962,
            "interest_rate_bps": 2500,
            "statement_date": "2026-10-01",
            "emi_amount_cents": 180148,
        },
        "cross_field_issues": [],
        "cross_field_valid": True,
        "proptally_payload": {
            "financial_account": {
                "account_type": "MORTGAGE",
                "account_number_masked": "...0002",
            },
            "statement": {
                "statement_type": "MORTGAGE",
                "statement_date": "2026-10-01",
                "active_parser_name": "document_intelligence_lab",
                "active_parser_version": "0.1.0",
                "ingestion_status": "VALIDATED_READY",
            },
            "metadata": {},
        },
    }

    staged = _stage(session, tmp_path, result, profile="mortgage_statement")

    stored = json.loads(staged.extraction.extracted_data_json)
    assert staged.extraction.validation_status == "NEEDS_REVIEW"
    assert any("principal billed" in reason for reason in stored["review_reasons"])
    assert stored["total_due_cents"] == 180148
    assert stored["reported_interest_rate_milli_percent"] == 2500


def _sidecar_payload():
    result = _bank_result()
    result["fields"]["institution_name"] = "Bank of America"
    result["proptally_payload"]["statement"]["statement_date"] = "2021-09-16"
    result["proptally_payload"]["statement"]["period_start_date"] = "2021-08-18"
    result["proptally_payload"]["statement"]["period_end_date"] = "2021-09-16"
    return result


def _install_sidecar(monkeypatch, result=None, *, healthy=True):
    """Point the web route at a real service answering from a mock transport."""
    import proptally.web.routes.ingestion as ingestion_routes

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            body = {"status": "healthy"} if healthy else {"status": "degraded"}
            return httpx.Response(200 if healthy else 503, json=body)
        if request.url.path == "/api/v1/jobs/extract/upload":
            return httpx.Response(
                202,
                json={"job_id": "job_web", "status": "queued", "profile_name": "bank_statement"},
            )
        if request.url.path == "/api/v1/jobs/job_web":
            return httpx.Response(
                200,
                json={
                    "job_id": "job_web",
                    "status": "completed",
                    "profile_name": "bank_statement",
                    "result": result,
                },
            )
        return httpx.Response(404, json={"detail": "not found"})

    def factory():
        return StatementIngestionService(
            client=DocumentIntelligenceClient(
                base_url="http://127.0.0.1:8765",
                http_client=httpx.Client(
                    base_url="http://127.0.0.1:8765", transport=httpx.MockTransport(handler)
                ),
            )
        )

    monkeypatch.setenv("PROPTALLY_DOC_INTEL", "1")
    monkeypatch.setattr(ingestion_routes, "_doc_intel_service", factory)


def _seed_checking(engine):
    with Session(engine) as session:
        session.add(FinancialInstitution(id="inst_amer", name="Bank of America"))
        session.flush()
        session.add(
            FinancialAccount(
                id="acct_amer_9456",
                institution_id="inst_amer",
                account_name="Bank of America Checking (...9456)",
                account_type="CHECKING",
                account_number_masked="...9456",
            )
        )
        session.commit()


def test_upload_uses_the_sidecar_when_enabled_and_stages_for_review(owner, monkeypatch, tmp_path):
    from urllib.parse import unquote_plus

    from synthetic_statements import bofa_checking_pdf

    signed_in = owner()
    _seed_checking(signed_in.engine)
    _install_sidecar(monkeypatch, _sidecar_payload())
    statement_pdf = bofa_checking_pdf(tmp_path / "checking.pdf")

    response = signed_in.post(
        "/statements/upload",
        {},
        files={"file": ("checking.pdf", statement_pdf.read_bytes(), "application/pdf")},
        follow_redirects=False,
    )

    assert response.status_code == 303
    message = unquote_plus(response.headers["location"])
    assert "local language model" in message
    assert "matched to a registered account" in message

    with Session(signed_in.engine) as session:
        extractions = session.exec(select(DocumentExtraction)).all()
        assert len(extractions) == 1
        assert extractions[0].parser_name == "document_intelligence_lab"
        assert extractions[0].validation_status == "VALIDATED"
        assert extractions[0].approval_status == "PENDING"
        stored = json.loads(extractions[0].extracted_data_json)
        assert stored["financial_account_id"] == "acct_amer_9456"
        assert session.exec(select(Statement)).all() == []


def test_upload_falls_back_to_the_builtin_parser_when_the_sidecar_is_down(owner, monkeypatch, tmp_path):
    from urllib.parse import unquote_plus

    from synthetic_statements import bofa_checking_pdf

    signed_in = owner()
    _seed_checking(signed_in.engine)
    _install_sidecar(monkeypatch, _sidecar_payload(), healthy=False)
    statement_pdf = bofa_checking_pdf(tmp_path / "checking.pdf")

    response = signed_in.post(
        "/statements/upload",
        {},
        files={"file": ("checking.pdf", statement_pdf.read_bytes(), "application/pdf")},
        follow_redirects=False,
    )

    assert response.status_code == 303
    message = unquote_plus(response.headers["location"])
    assert "local language model" not in message
    with Session(signed_in.engine) as session:
        extractions = session.exec(select(DocumentExtraction)).all()
        assert len(extractions) == 1
        assert extractions[0].parser_name != "document_intelligence_lab"


def test_a_person_can_approve_and_import_a_staged_extraction(session, tmp_path):
    from proptally.ingestion.service import approve_extraction, import_approved_extraction

    _register_bofa_checking(session)
    staged = _stage(session, tmp_path, _bank_result())

    approve_extraction(session, staged.extraction.id, commit=True)
    statement = import_approved_extraction(session, staged.extraction.id, commit=True)

    assert statement.ingestion_status == "COMMITTED"
    assert statement.statement_date.isoformat() == "2025-05-15"
    assert statement.opening_balance_cents == 341227
    assert statement.closing_balance_cents == 414736
    assert statement.financial_account_id == "acct_bank_of_america_9456"
