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


def _sidecar_payload(*, opening=392695, closing=48586):
    result = _bank_result()
    result["fields"]["institution_name"] = "Bank of America"
    result["fields"]["opening_balance_cents"] = opening
    result["fields"]["closing_balance_cents"] = closing
    result["fields"]["statement_period_start"] = "2025-12-16"
    result["fields"]["statement_period_end"] = "2026-01-15"
    result["proptally_payload"]["statement"]["opening_balance_cents"] = opening
    result["proptally_payload"]["statement"]["closing_balance_cents"] = closing
    result["proptally_payload"]["statement"]["statement_date"] = "2026-01-15"
    result["proptally_payload"]["statement"]["period_start_date"] = "2025-12-16"
    result["proptally_payload"]["statement"]["period_end_date"] = "2026-01-15"
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

    def factory(*args, **kwargs):
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
    assert "queued for local language model extraction" in message

    with Session(signed_in.engine) as session:
        extractions = session.exec(select(DocumentExtraction)).all()
        assert len(extractions) == 1
        assert extractions[0].parser_name == "document_intelligence_lab"
        assert extractions[0].validation_status == "PROCESSING"
        assert extractions[0].approval_status == "PENDING"
        ext_id = extractions[0].id

    # Poll endpoint to finalize extraction
    poll_res = signed_in.client.get(
        f"/statements/extractions/{ext_id}/poll",
        headers={"X-Requested-With": "fetch", "Accept": "application/json"},
    )
    assert poll_res.status_code == 200
    poll_data = poll_res.json()
    assert poll_data["is_done"] is True
    assert poll_data["status"] == "VALIDATED"

    with Session(signed_in.engine) as session:
        extractions = session.exec(select(DocumentExtraction)).all()
        assert len(extractions) == 1
        assert extractions[0].parser_name == "document_intelligence_lab"
        assert extractions[0].validation_status == "VALIDATED"
        assert extractions[0].approval_status == "PENDING"
        stored = json.loads(extractions[0].extracted_data_json)
        assert stored["financial_account_id"] == "acct_amer_9456"
        assert len(stored["transactions"]) == 3
        assert stored["provenance"]["transactions"] == "builtin:BofaCheckingParser"
        assert stored["provenance"]["institution_name"] == "sidecar:doc_intel"
        assert session.exec(select(Statement)).all() == []


def test_hybrid_approved_extraction_imports_transactions_into_ledger(owner, monkeypatch, tmp_path):
    from synthetic_statements import bofa_checking_pdf
    from proptally.ingestion.service import approve_extraction, import_approved_extraction
    from proptally.db.models import Transaction, TransactionProcessingState

    signed_in = owner()
    _seed_checking(signed_in.engine)
    _install_sidecar(monkeypatch, _sidecar_payload())
    statement_pdf = bofa_checking_pdf(tmp_path / "checking.pdf")

    signed_in.post(
        "/statements/upload",
        {},
        files={"file": ("checking.pdf", statement_pdf.read_bytes(), "application/pdf")},
        follow_redirects=False,
    )

    # Visiting /statements finalizes pending background extractions
    signed_in.client.get("/statements")

    with Session(signed_in.engine) as session:
        extraction = session.exec(select(DocumentExtraction)).one()
        assert extraction.validation_status == "VALIDATED"
        approve_extraction(session, extraction.id, commit=True)
        stmt = import_approved_extraction(session, extraction.id, commit=True)

        assert stmt.ingestion_status == "COMMITTED"
        txs = session.exec(select(Transaction).where(Transaction.statement_id == stmt.id)).all()
        assert len(txs) == 3
        states = session.exec(select(TransactionProcessingState)).all()
        assert len(states) == 3
        for s in states:
            assert s.allocation_status == "UNALLOCATED"
            assert s.posting_status == "UNPOSTED"


def test_hybrid_upload_detects_transaction_balance_mismatch_and_requires_review(
    owner, monkeypatch, tmp_path
):
    from urllib.parse import unquote_plus
    from synthetic_statements import bofa_checking_pdf

    signed_in = owner()
    _seed_checking(signed_in.engine)
    # Give sidecar an ending balance of 999999 which does not tie out to transactions
    _install_sidecar(monkeypatch, _sidecar_payload(closing=999999))
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
    assert "queued for local language model extraction" in message

    with Session(signed_in.engine) as session:
        extractions = session.exec(select(DocumentExtraction)).all()
        assert len(extractions) == 1
        assert extractions[0].validation_status == "PROCESSING"
        ext_id = extractions[0].id

    # Poll endpoint to finalize
    poll_res = signed_in.client.get(
        f"/statements/extractions/{ext_id}/poll",
        headers={"X-Requested-With": "fetch", "Accept": "application/json"},
    )
    assert poll_res.status_code == 200
    poll_data = poll_res.json()
    assert poll_data["is_done"] is True
    assert poll_data["status"] == "NEEDS_REVIEW"
    assert any("does not equal closing balance" in r for r in poll_data["reasons"])

    with Session(signed_in.engine) as session:
        extractions = session.exec(select(DocumentExtraction)).all()
        assert len(extractions) == 1
        assert extractions[0].validation_status == "NEEDS_REVIEW"
        stored = json.loads(extractions[0].extracted_data_json)
        assert len(stored["transactions"]) == 3
        assert any("does not equal closing balance" in r for r in stored["review_reasons"])


def test_hybrid_upload_extracts_unrecognised_pdf_via_sidecar(owner, monkeypatch, tmp_path):
    from urllib.parse import unquote_plus
    import pymupdf

    signed_in = owner()
    _seed_checking(signed_in.engine)

    # An unknown bank PDF layout not recognized by built-in regex parsers
    unrecognised_pdf = tmp_path / "unknown_bank.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text(pymupdf.Point(50, 50), "Regional Community Bank\nChecking Account ...9456\nBeginning Balance: $1,000.00\nEnding Balance: $1,500.00")
    doc.save(unrecognised_pdf)
    doc.close()

    result = _bank_result()
    result["fields"]["institution_name"] = "Bank of America"
    result["fields"]["opening_balance_cents"] = 100000
    result["fields"]["closing_balance_cents"] = 150000
    _install_sidecar(monkeypatch, result)

    response = signed_in.post(
        "/statements/upload",
        {},
        files={"file": ("unknown_bank.pdf", unrecognised_pdf.read_bytes(), "application/pdf")},
        follow_redirects=False,
    )

    assert response.status_code == 303
    message = unquote_plus(response.headers["location"])
    assert "local language model" in message
    assert "queued for local language model extraction" in message

    # Finalize via /statements
    signed_in.client.get("/statements")

    with Session(signed_in.engine) as session:
        extractions = session.exec(select(DocumentExtraction)).all()
        assert len(extractions) == 1
        assert extractions[0].parser_name == "document_intelligence_lab"
        assert extractions[0].validation_status == "VALIDATED"


def test_cancel_statement_extraction_reverts_to_local_parser(owner, monkeypatch, tmp_path):
    from synthetic_statements import bofa_checking_pdf

    cancelled_jobs = []

    import proptally.web.routes.ingestion as ingestion_routes

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "healthy"})
        if request.url.path == "/api/v1/jobs/extract/upload":
            return httpx.Response(
                202,
                json={"job_id": "job_cancel_test", "status": "queued", "profile_name": "bank_statement"},
            )
        if request.url.path == "/api/v1/jobs/job_cancel_test/cancel":
            cancelled_jobs.append("job_cancel_test")
            return httpx.Response(200, json={"cancelled": True})
        return httpx.Response(404, json={"detail": "not found"})

    def factory(*args, **kwargs):
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

    signed_in = owner()
    _seed_checking(signed_in.engine)
    statement_pdf = bofa_checking_pdf(tmp_path / "checking.pdf")

    signed_in.post(
        "/statements/upload",
        {},
        files={"file": ("checking.pdf", statement_pdf.read_bytes(), "application/pdf")},
        follow_redirects=False,
    )

    with Session(signed_in.engine) as session:
        ext = session.exec(select(DocumentExtraction)).one()
        assert ext.validation_status == "PROCESSING"

    # Cancel the in-flight extraction
    cancel_res = signed_in.post(f"/statements/extractions/{ext.id}/cancel", {}, follow_redirects=False)
    assert cancel_res.status_code == 303
    assert "job_cancel_test" in cancelled_jobs

    with Session(signed_in.engine) as session:
        ext = session.exec(select(DocumentExtraction)).one()
        assert ext.validation_status != "PROCESSING"
        stored = json.loads(ext.extracted_data_json)
        assert any("cancelled" in r.lower() for r in stored["review_reasons"])
        # Built-in transactions preserved from local extraction
        assert len(stored["transactions"]) == 3


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


def test_extraction_data_handles_full_account_number_and_textual_dates():
    from proptally.ingestion.service import _iso_date

    assert _iso_date("August 18, 2021") == "2021-08-18"
    assert _iso_date("August 18th, 2021") == "2021-08-18"
    assert _iso_date("18-May-2025") == "2025-05-18"
    assert _iso_date("2025-05-15 00:00:00") == "2025-05-15"
    assert _iso_date("2025-05-15T12:00:00Z") == "2025-05-15"

    result = _bank_result(masked="402918239456")
    result["fields"]["statement_period_start"] = "August 18, 2021"
    result["fields"]["statement_period_end"] = "September 16, 2021"
    result["proptally_payload"]["statement"]["statement_date"] = "September 16, 2021"
    result["proptally_payload"]["statement"]["period_start_date"] = "August 18, 2021"
    result["proptally_payload"]["statement"]["period_end_date"] = "September 16, 2021"

    data = doc_intel_extraction_data(
        "bank_statement", result["fields"], result["proptally_payload"]
    )
    assert data["account_last4"] == "9456"
    assert data["statement_date"] == "2021-09-16"
    assert data["period_start_date"] == "2021-08-18"


def test_resubmitting_same_document_updates_unimported_extraction(session, tmp_path):
    from proptally.ingestion.service import approve_extraction

    _register_bofa_checking(session)

    res1 = _bank_result()
    res1["fields"]["opening_balance_cents"] = 100000
    res1["proptally_payload"]["statement"]["opening_balance_cents"] = 100000
    first = _stage(session, tmp_path, res1)
    assert json.loads(first.extraction.extracted_data_json)["opening_balance_cents"] == 100000

    approve_extraction(session, first.extraction.id, commit=True)
    assert first.extraction.approval_status == "APPROVED"

    res2 = _bank_result()
    res2["fields"]["opening_balance_cents"] = 200000
    res2["proptally_payload"]["statement"]["opening_balance_cents"] = 200000
    second = _stage(session, tmp_path, res2)
    assert second.created is False
    assert second.extraction.id == first.extraction.id
    assert json.loads(second.extraction.extracted_data_json)["opening_balance_cents"] == 200000
    assert second.extraction.approval_status == "PENDING"
    assert second.extraction.approved_at is None


def test_client_wait_for_job_retries_transient_polling_errors_and_cancels_on_timeout():
    polls = 0
    cancelled = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal polls
        if request.url.path == "/api/v1/jobs/job_retry/cancel":
            cancelled.append(request.url.path)
            return httpx.Response(200, json={"job_id": "job_retry", "status": "cancelled"})
        if request.url.path == "/api/v1/jobs/job_retry":
            polls += 1
            if polls == 1:
                raise httpx.ConnectError("connection blip")
            if polls == 2:
                return httpx.Response(503, json={"detail": "temporarily unavailable"})
            return httpx.Response(
                200,
                json={
                    "job_id": "job_retry",
                    "status": "processing",
                    "profile_name": "bank_statement",
                },
            )
        return httpx.Response(404, json={"detail": "not found"})

    client = DocumentIntelligenceClient(
        base_url="http://127.0.0.1:8000",
        http_client=httpx.Client(
            base_url="http://127.0.0.1:8000", transport=httpx.MockTransport(handler)
        ),
    )

    with pytest.raises(TimeoutError):
        client.wait_for_job("job_retry", timeout=0.05, poll_interval=0.01)

    assert polls >= 1
    assert len(cancelled) == 1


def test_finalize_stale_extraction_with_local_fallback(session, tmp_path):
    from datetime import datetime, timezone, timedelta
    from uuid import uuid4
    from proptally.ingestion.service import finalize_stale_extraction

    doc = Document(
        id=str(uuid4()),
        original_filename="statement.pdf",
        file_sha256="0" * 64,
        file_size_bytes=100,
        mime_type="application/pdf",
        storage_path="vault/statement.pdf",
    )
    session.add(doc)
    session.flush()

    ext = DocumentExtraction(
        id=str(uuid4()),
        document_id=doc.id,
        parser_name="document_intelligence_lab",
        parser_version="1.0.0",
        validation_status="PROCESSING",
        approval_status="PENDING",
        created_at=datetime.now(timezone.utc) - timedelta(seconds=120),
        extracted_data_json=json.dumps({
            "job_id": "job_stale_1",
            "local_extraction": {
                "status": "VALIDATED",
                "statement_type": "bank_checking",
                "transactions": [],
                "review_reasons": [],
            },
        }),
    )
    session.add(ext)
    session.commit()

    updated = finalize_stale_extraction(session, ext, max_age_seconds=60)
    assert updated.validation_status == "VALIDATED"
    assert updated.approval_status == "PENDING"
    data = json.loads(updated.extracted_data_json)
    assert any("timed out" in r for r in data.get("review_reasons", []))


def test_finalize_stale_extraction_without_local_fallback(session):
    from datetime import datetime, timezone, timedelta
    from uuid import uuid4
    from proptally.ingestion.service import finalize_stale_extraction

    doc = Document(
        id=str(uuid4()),
        original_filename="statement2.pdf",
        file_sha256="1" * 64,
        file_size_bytes=100,
        mime_type="application/pdf",
        storage_path="vault/statement2.pdf",
    )
    session.add(doc)
    session.flush()

    ext = DocumentExtraction(
        id=str(uuid4()),
        document_id=doc.id,
        parser_name="document_intelligence_lab",
        parser_version="1.0.0",
        validation_status="PROCESSING",
        approval_status="PENDING",
        created_at=datetime.now(timezone.utc) - timedelta(seconds=120),
        extracted_data_json=json.dumps({
            "job_id": "job_stale_2",
            "review_reasons": [],
        }),
    )
    session.add(ext)
    session.commit()

    updated = finalize_stale_extraction(session, ext, max_age_seconds=60)
    assert updated.validation_status == "NEEDS_REVIEW"
    assert updated.approval_status == "PENDING"
    data = json.loads(updated.extracted_data_json)
    assert any("timed out" in r for r in data.get("review_reasons", []))


def test_reupload_unimported_document_requeues_for_sidecar(owner, monkeypatch, tmp_path):
    from urllib.parse import unquote_plus
    from sqlalchemy import desc
    from synthetic_statements import bofa_checking_pdf

    signed_in = owner()
    _seed_checking(signed_in.engine)
    statement_pdf = bofa_checking_pdf(tmp_path / "checking.pdf")

    # Step 1: Upload with sidecar disabled -> ingested via built-in parser
    monkeypatch.setenv("PROPTALLY_DOC_INTEL", "0")
    res1 = signed_in.post(
        "/statements/upload",
        {},
        files={"file": ("checking.pdf", statement_pdf.read_bytes(), "application/pdf")},
        follow_redirects=False,
    )
    assert res1.status_code == 303
    with Session(signed_in.engine) as session:
        extractions = session.exec(select(DocumentExtraction)).all()
        assert len(extractions) == 1
        assert extractions[0].parser_name == "BofaCheckingParser"
        assert extractions[0].validation_status == "VALIDATED"
        doc_id = extractions[0].document_id

    # Step 2: Now enable sidecar and re-upload the same file -> re-queues for sidecar extraction
    _install_sidecar(monkeypatch, _sidecar_payload())
    res2 = signed_in.post(
        "/statements/upload",
        {},
        files={"file": ("checking.pdf", statement_pdf.read_bytes(), "application/pdf")},
        follow_redirects=False,
    )
    assert res2.status_code == 303
    message = unquote_plus(res2.headers["location"])
    assert "re-queued for local language model extraction" in message

    with Session(signed_in.engine) as session:
        extractions = session.exec(
            select(DocumentExtraction)
            .where(DocumentExtraction.document_id == doc_id)
            .order_by(desc(DocumentExtraction.created_at))
        ).all()
        assert len(extractions) == 2
        latest = extractions[0]
        assert latest.parser_name == "document_intelligence_lab"
        assert latest.validation_status == "PROCESSING"

    # Step 3: Poll to finalize
    poll_res = signed_in.client.get(
        f"/statements/extractions/{latest.id}/poll",
        headers={"X-Requested-With": "fetch", "Accept": "application/json"},
    )
    assert poll_res.status_code == 200
    assert poll_res.json()["is_done"] is True
    assert poll_res.json()["status"] == "VALIDATED"


def test_restaged_old_row_does_not_timeout_prematurely(session, tmp_path):
    from datetime import datetime, timezone, timedelta
    from uuid import uuid4
    from proptally.ingestion.doc_intel import DocIntelJob
    from proptally.ingestion.service import StatementIngestionService

    doc = Document(
        id=str(uuid4()),
        original_filename="statement_old.pdf",
        file_sha256="2" * 64,
        file_size_bytes=100,
        mime_type="application/pdf",
        storage_path="vault/statement_old.pdf",
    )
    session.add(doc)
    session.flush()

    two_hours_ago = datetime.now(timezone.utc) - timedelta(hours=2)
    ext = DocumentExtraction(
        id=str(uuid4()),
        document_id=doc.id,
        parser_name="document_intelligence_lab",
        parser_version="1.0.0",
        validation_status="PROCESSING",
        approval_status="PENDING",
        created_at=two_hours_ago,
        extracted_data_json=json.dumps({"job_id": "job_old", "submitted_at": two_hours_ago.isoformat()}),
    )
    session.add(ext)
    session.commit()

    # Re-stage with a fresh job (simulating re-upload or re-process)
    fresh_job = DocIntelJob(job_id="job_fresh", status="processing", profile_name="bank_statement")
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/jobs/job_fresh":
            return httpx.Response(200, json={"job_id": "job_fresh", "status": "processing", "profile_name": "bank_statement"})
        return httpx.Response(404, json={"detail": "not found"})

    client = DocumentIntelligenceClient(
        base_url="http://127.0.0.1:8765",
        http_client=httpx.Client(base_url="http://127.0.0.1:8765", transport=httpx.MockTransport(handler)),
    )
    service = StatementIngestionService(client=client)

    updated_ext = service.stage_pending(
        session,
        tmp_path,
        job=fresh_job,
        payload=b"%PDF-1.4 mock",
        original_filename="statement_old.pdf",
        profile_name="bank_statement",
        commit=True,
    )

    # Calling finalize_extraction must NOT trigger staleness timeout despite created_at being 2 hours old
    finalized = service.finalize_extraction(session, updated_ext, commit=True, max_age_seconds=300)
    assert finalized.validation_status == "PROCESSING"
    data = json.loads(finalized.extracted_data_json)
    assert "timed out" not in " ".join(data.get("review_reasons", []))


def test_evidence_coordinates_retained_in_extraction_data():
    from proptally.ingestion.service import doc_intel_extraction_data

    fields = {
        "institution_name": {
            "raw_value": "Bank of America",
            "normalized_value": "Bank of America",
            "evidence": [
                {
                    "quote": "Bank of America",
                    "page_index": 0,
                    "resolution_status": "exact",
                    "boxes": [[10.0, 20.0, 100.0, 40.0]],
                }
            ],
        },
        "opening_balance_cents": {
            "raw_value": "$1,000.00",
            "normalized_value": 100000,
            "evidence": [
                {
                    "quote": "$1,000.00",
                    "page_index": 0,
                    "resolution_status": "exact",
                    "boxes": [[150.0, 200.0, 220.0, 215.0]],
                }
            ],
        },
        "statement_period_start": "2025-12-16",
        "statement_period_end": "2026-01-15",
    }
    payload = {
        "statement": {
            "statement_date": "2026-01-15",
            "opening_balance_cents": 100000,
            "closing_balance_cents": 200000,
        },
        "financial_account": {
            "account_number_masked": "...9456",
        },
    }

    data = doc_intel_extraction_data("bank_statement", fields, payload)
    assert "evidence" in data
    assert "institution_name" in data["evidence"]
    assert isinstance(data["evidence"]["institution_name"], list)
    assert data["evidence"]["institution_name"][0]["quote"] == "Bank of America"
    assert data["evidence"]["institution_name"][0]["boxes"] == [[10.0, 20.0, 100.0, 40.0]]
    assert "opening_balance_cents" in data["evidence"]
    assert isinstance(data["evidence"]["opening_balance_cents"], list)
    assert data["evidence"]["opening_balance_cents"][0]["quote"] == "$1,000.00"


