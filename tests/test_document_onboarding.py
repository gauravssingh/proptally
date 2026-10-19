"""Document-based onboarding: ZIP safety, extraction, reconciliation and provisioning.

The tests drive the real HTTP surface and read both databases directly. The point is
that a ZIP of ordinary documents becomes a portfolio only after explicit approval,
that every proposed value keeps its source, and that a failure in one file does not
take the archive down with it.
"""

import io
import json
import re
import zipfile
from datetime import UTC, datetime, timedelta
from urllib.parse import unquote_plus

import pymupdf
import pytest
from sqlmodel import Session, select
from synthetic_statements import bofa_checking_pdf

from proptally.db.models import (
    Document,
    DocumentExtraction,
    FinancialAccount,
    FinancialInstitution,
    JournalEntry,
    Lease,
    LoanAccount,
    PortfolioSetup,
    Property,
)
from proptally.db.onboarding_models import (
    OnboardingDocument,
    OnboardingFieldEvidence,
    OnboardingProposal,
    OnboardingReviewDecision,
    OnboardingSession,
)
from proptally.domain.setup_progress import evaluate
from proptally.onboarding.provisioning import _stage_statement_extractions, run_provisioning
from proptally.web.auth import cookie_name, issue_session, register_portfolio


@pytest.fixture(autouse=True)
def _disable_background_worker(monkeypatch):
    """Drive processing explicitly so a test never races the polling thread."""
    monkeypatch.setenv("PROPTALLY_ONBOARDING_WORKER", "0")


def _pdf(text: str) -> bytes:
    document = pymupdf.open()
    page = document.new_page()
    y = 72
    for line in text.splitlines():
        page.insert_text((72, y), line)
        y += 16
    return document.tobytes()


def _zip(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in files.items():
            archive.writestr(name, payload)
    return buffer.getvalue()


def _csrf(signed_in) -> str:
    return re.search(
        r'name="csrf_token" value="([^"]+)"', signed_in.client.get("/account").text
    ).group(1)


def _upload(signed_in, archive: bytes, filename: str = "documents.zip"):
    return signed_in.client.post(
        "/onboarding/import/upload",
        files={"archive": (filename, archive, "application/zip")},
        data={"csrf_token": _csrf(signed_in)},
        follow_redirects=False,
    )


def _start(signed_in, archive: bytes) -> str:
    response = _upload(signed_in, archive)
    assert response.status_code == 303, response.text[:800]
    location = response.headers["location"]
    assert "/onboarding/import/onb_" in location, location
    signed_in.app.state.onboarding_worker.drain()
    return location.rsplit("/", 1)[-1]


def _proposals(signed_in, onboarding_id: str) -> list[OnboardingProposal]:
    with Session(signed_in.app.state.control_engine) as control:
        rows = control.exec(
            select(OnboardingProposal).where(
                OnboardingProposal.onboarding_id == onboarding_id
            )
        ).all()
        for row in rows:
            control.expunge(row)
        return rows


def _documents(signed_in, onboarding_id: str) -> list[OnboardingDocument]:
    with Session(signed_in.app.state.control_engine) as control:
        rows = control.exec(
            select(OnboardingDocument).where(
                OnboardingDocument.onboarding_id == onboarding_id
            )
        ).all()
        for row in rows:
            control.expunge(row)
        return rows


def _accept(signed_in, onboarding_id: str, proposal_id: str):
    return signed_in.client.post(
        f"/onboarding/import/{onboarding_id}/records/{proposal_id}/decision",
        data={"decision": "ACCEPT", "csrf_token": _csrf(signed_in)},
        follow_redirects=False,
    )


DEED = """SALE DEED
Property Address: 123 Main St, Springfield, IL 62704
Purchase Price: $800,000.00
Sale Date: 06/15/2021
Buyer: Jane Doe
Seller: John Smith
"""

MORTGAGE = """ROCKET MORTGAGE
Lender: Rocket Mortgage
Loan Number: 0700000001
Property Address: 123 Main St, Springfield, IL 62704
Original Principal: $640,000.00
Outstanding Principal: $590,000.00
Interest Rate: 3.125%
Loan Term: 30 years
Monthly Payment: $3,200.00
Statement Date: 01/31/2026
"""

BANK = """FIRST NATIONAL BANK
Institution: First National Bank
Account Number: 1234567890
Account Type: Checking
Statement Period: 01/01/2026 to 01/31/2026
Opening Balance: $10,000.00
Closing Balance: $12,000.00
"""

LEASE = """RENTAL AGREEMENT
Property Address: 123 Main St, Springfield, IL 62704
Tenant: Riley Alvarez
Monthly Rent: $2,600.00
Security Deposit: $2,600.00
Lease Start: 01/01/2026
Lease End: 12/31/2026
"""


def test_end_to_end_import_creates_the_portfolio_only_after_approval(owner):
    signed_in = owner()
    onboarding_id = _start(
        signed_in,
        _zip({
            "sale_deed.pdf": _pdf(DEED),
            "mortgage_statement.pdf": _pdf(MORTGAGE),
            "bank_statement.pdf": _pdf(BANK),
            "lease.pdf": _pdf(LEASE),
        }),
    )

    with Session(signed_in.app.state.control_engine) as control:
        session = control.get(OnboardingSession, onboarding_id)
        assert session.status == "READY_FOR_REVIEW", session.last_error

    documents = _documents(signed_in, onboarding_id)
    assert {doc.category for doc in documents} == {
        "PROPERTY_DEED", "MORTGAGE_STATEMENT", "BANK_STATEMENT", "RENTAL_AGREEMENT",
    }
    assert all(doc.status == "READY" for doc in documents)

    proposals = _proposals(signed_in, onboarding_id)
    assert {proposal.entity_type for proposal in proposals} == {
        "PROPERTY", "MORTGAGE", "ACCOUNT", "LEASE",
    }
    property_proposal = next(p for p in proposals if p.entity_type == "PROPERTY")
    mortgage_proposal = next(p for p in proposals if p.entity_type == "MORTGAGE")
    assert '"purchase_price_cents": 80000000' in property_proposal.payload_json
    with Session(signed_in.app.state.control_engine) as control:
        location_evidence = control.exec(
            select(OnboardingFieldEvidence).where(
                OnboardingFieldEvidence.proposal_id == property_proposal.id,
                OnboardingFieldEvidence.field_name == "purchase_price_cents",
            )
        ).first()
        assert location_evidence is not None
        location = json.loads(location_evidence.source_location_json)
        assert location["status"] == "AVAILABLE"
        assert location["page"] == 1
        assert location["rects"]
    assert '"property_group_key": "property:123 main st springfield 62704"' in mortgage_proposal.payload_json
    assert '"association_status": "MATCHED"' in mortgage_proposal.payload_json

    # Nothing has been written into the portfolio yet.
    with Session(signed_in.engine) as tenant:
        assert tenant.exec(select(Property)).all() == []

    review = signed_in.client.get(f"/onboarding/import/{onboarding_id}/review")
    assert review.status_code == 200
    assert "Review what PropTally found" in review.text
    assert "123 Main St" in review.text
    assert "Review property" in review.text
    assert "Review mortgage" in review.text
    assert f'href="/onboarding/import/{onboarding_id}/records/{property_proposal.id}"' in review.text

    record = signed_in.client.get(
        f"/onboarding/import/{onboarding_id}/records/{property_proposal.id}"
    )
    assert record.status_code == 200
    assert "Source documents" in record.text
    assert "Purchase Price" in record.text or "Purchase price" in record.text
    assert "sale_deed.pdf" in record.text
    # Field editing keeps save, mark-unknown and cancel together; unknown is text.
    assert "Mark unknown" in record.text
    assert 'data-record-field-cancel' in record.text
    assert "pt-field-cancel-icon" not in record.text

    for proposal in proposals:
        accepted = _accept(signed_in, onboarding_id, proposal.id)
        assert accepted.status_code == 303
        assert "error=" not in accepted.headers["location"], accepted.headers["location"]

    review_ready = signed_in.client.get(f"/onboarding/import/{onboarding_id}/review").text
    assert "Finish" in review_ready
    assert "pt-onboarding-approve-bar" not in review_ready

    approve = signed_in.client.get(f"/onboarding/import/{onboarding_id}/approve")
    assert approve.status_code == 200
    assert "Create the portfolio" in approve.text

    response = signed_in.client.post(
        f"/onboarding/import/{onboarding_id}/approve",
        data={"confirm": "yes", "csrf_token": _csrf(signed_in)},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "error=" not in response.headers["location"]
    signed_in.app.state.onboarding_worker.drain()

    with Session(signed_in.app.state.control_engine) as control:
        assert control.get(OnboardingSession, onboarding_id).status == "COMPLETED"

    with Session(signed_in.engine) as tenant:
        properties = tenant.exec(select(Property)).all()
        assert len(properties) == 1
        assert properties[0].address_line1 == "123 Main St"
        assert properties[0].city == "Springfield"
        assert properties[0].purchase_price_cents == 80_000_000
        assert properties[0].acquisition_date.isoformat() == "2021-06-15"

        loans = tenant.exec(select(LoanAccount)).all()
        assert len(loans) == 1
        assert loans[0].loan_number == "0700000001"
        assert loans[0].original_principal_cents == 64_000_000
        assert loans[0].current_interest_rate_bps == 3125
        assert loans[0].property_id == properties[0].id

        accounts = tenant.exec(select(FinancialAccount)).all()
        assert any(
            a.account_type == "CHECKING" and a.account_number_masked == "...7890"
            for a in accounts
        )

        leases = tenant.exec(select(Lease)).all()
        assert len(leases) == 1
        assert leases[0].tenant_name == "Riley Alvarez"
        assert leases[0].monthly_rent_cents == 260_000

        documents = tenant.exec(select(Document)).all()
        assert len(documents) == 4
        assert all(doc.property_id == properties[0].id for doc in documents
                   if doc.document_type in {"MORTGAGE_STATEMENT", "LEASE_AGREEMENT"})

        setup = tenant.get(PortfolioSetup, "portfolio")
        assert setup is not None and setup.published_at is None
        progress = evaluate(tenant)
        assert progress.steps[0].done  # property
        assert progress.steps[1].done  # accounts
        assert progress.steps[2].done  # mortgage

    # The setup wizard resumes at the first thing the documents could not supply.
    setup_response = signed_in.client.get("/setup", follow_redirects=False)
    assert setup_response.status_code == 303
    assert setup_response.headers["location"].startswith("/setup/opening-balances")

    # A retry after success does nothing and cannot duplicate a record.
    errors = run_provisioning(
        signed_in.app.state.control_engine,
        signed_in.app.state.tenant_manager,
        signed_in.app.state.onboarding_store,
        onboarding_id,
    )
    assert errors == []
    with Session(signed_in.engine) as tenant:
        assert len(tenant.exec(select(Property)).all()) == 1
        assert len(tenant.exec(select(LoanAccount)).all()) == 1


def test_review_cards_have_explicit_actions_and_quick_accept_returns_to_review(owner):
    signed_in = owner()
    onboarding_id = _start(signed_in, _zip({"sale_deed.pdf": _pdf(DEED)}))
    property_proposal = next(
        p for p in _proposals(signed_in, onboarding_id) if p.entity_type == "PROPERTY"
    )
    review_url = f"/onboarding/import/{onboarding_id}/review"
    decision_url = f"/onboarding/import/{onboarding_id}/records/{property_proposal.id}/decision"

    page = signed_in.client.get(review_url)
    assert "Review property" in page.text
    assert 'name="return_to"' in page.text
    assert "Accept at least one record to continue." in page.text
    assert "Finish" in page.text
    assert "disabled" in page.text

    accepted = signed_in.client.post(
        decision_url,
        data={
            "decision": "ACCEPT",
            "return_to": review_url,
            "csrf_token": _csrf(signed_in),
        },
        follow_redirects=False,
    )
    assert accepted.status_code == 303
    assert accepted.headers["location"].startswith(review_url)
    with Session(signed_in.app.state.control_engine) as control:
        assert control.get(OnboardingProposal, property_proposal.id).status == "ACCEPTED"

    detail = signed_in.client.get(
        f"/onboarding/import/{onboarding_id}/records/{property_proposal.id}"
    )
    assert "ACCEPTED" in detail.text.upper()
    assert "Accept record" not in detail.text
    assert "Defer" not in detail.text
    assert "Reject" not in detail.text

    unsafe = signed_in.client.post(
        f"/onboarding/import/{onboarding_id}/records/{property_proposal.id}/decision",
        data={
            "decision": "DEFER",
            "return_to": "https://example.com/phish",
            "csrf_token": _csrf(signed_in),
        },
        follow_redirects=False,
    )
    assert unsafe.status_code == 303
    assert unsafe.headers["location"].startswith(
        f"/onboarding/import/{onboarding_id}/records/{property_proposal.id}"
    )

    # Deferred is not final: the record still needs review, so decisions remain.
    deferred_page = signed_in.client.get(unsafe.headers["location"])
    assert "Accept record" in deferred_page.text
    assert "Defer" in deferred_page.text
    assert "Reject" in deferred_page.text


def test_conflicting_values_must_be_resolved_before_acceptance(owner):
    signed_in = owner()
    second_deed = DEED.replace("SALE DEED", "TITLE DEED").replace(
        "Purchase Price: $800,000.00", "Purchase Price: $850,000.00"
    ).replace("Sale Date: 06/15/2021", "Sale Date: 07/01/2021")
    onboarding_id = _start(
        signed_in,
        _zip({"deed_one.pdf": _pdf(DEED), "deed_two.pdf": _pdf(second_deed)}),
    )

    property_proposal = next(
        p for p in _proposals(signed_in, onboarding_id) if p.entity_type == "PROPERTY"
    )
    assert '"purchase_price_cents"' in property_proposal.conflicts_json
    assert "80000000" in property_proposal.conflicts_json
    assert "85000000" in property_proposal.conflicts_json

    refused = _accept(signed_in, onboarding_id, property_proposal.id)
    assert "error=" in refused.headers["location"]
    assert "Resolve these conflicting fields" in unquote_plus(refused.headers["location"])

    # Resolve both disagreements by choosing the second source's values.
    for field in ("purchase_price_cents", "acquisition_date"):
        resolved = signed_in.client.post(
            f"/onboarding/import/{onboarding_id}/records/{property_proposal.id}/field",
            data={
                "field": field,
                "action": "resolve",
                "value_index": "1",
                "csrf_token": _csrf(signed_in),
            },
            follow_redirects=False,
        )
        assert resolved.status_code == 303
        assert "error=" not in resolved.headers["location"], resolved.headers["location"]

    accepted = _accept(signed_in, onboarding_id, property_proposal.id)
    assert "error=" not in accepted.headers["location"], accepted.headers["location"]

    with Session(signed_in.app.state.control_engine) as control:
        stored = control.get(OnboardingProposal, property_proposal.id)
        assert stored.status == "ACCEPTED"
        assert '"purchase_price_cents": 85000000' in stored.payload_json
        # The original extraction is never overwritten.
        assert '"purchase_price_cents": 80000000' in stored.original_payload_json
        decisions = control.exec(
            select(OnboardingReviewDecision).where(
                OnboardingReviewDecision.proposal_id == property_proposal.id
            )
        ).all()
        assert any(decision.decision == "RESOLVE_CONFLICT" for decision in decisions)


def test_archive_path_traversal_is_rejected_and_nothing_is_staged(owner):
    signed_in = owner()
    archive = _zip({"../escaped.pdf": _pdf(DEED)})
    response = _upload(signed_in, archive)

    assert response.status_code == 303
    assert response.headers["location"].startswith("/onboarding/import?error=")
    with Session(signed_in.app.state.control_engine) as control:
        sessions = control.exec(select(OnboardingSession)).all()
        assert len(sessions) == 1 and sessions[0].status == "CANCELLED"
        assert control.exec(select(OnboardingDocument)).all() == []
    assert not (signed_in.app.state.onboarding_store.root / ".." / "escaped.pdf").exists()


def test_archive_with_too_many_files_is_rejected(owner):
    signed_in = owner()
    archive = _zip({f"note_{index}.txt": b"hello" for index in range(201)})
    response = _upload(signed_in, archive)
    assert response.status_code == 303
    assert "error=" in response.headers["location"]
    assert "more than 200 files" in unquote_plus(response.headers["location"])


def test_one_unreadable_document_does_not_fail_the_archive(owner):
    signed_in = owner()
    onboarding_id = _start(
        signed_in,
        _zip({
            "sale_deed.pdf": _pdf(DEED),
            "broken.pdf": b"%PDF-1.4\nthis is not a real pdf",
            "tool.exe": b"MZ\x90\x00 not a document",
        }),
    )

    documents = {doc.original_filename: doc for doc in _documents(signed_in, onboarding_id)}
    assert documents["sale_deed.pdf"].status == "READY"
    assert documents["broken.pdf"].status == "EXTRACTION_FAILED"
    assert documents["tool.exe"].status == "UNSUPPORTED"
    # The unsupported file is retained, not discarded.
    assert (signed_in.app.state.onboarding_store.resolve(
        documents["tool.exe"].storage_path
    )).is_file()

    with Session(signed_in.app.state.control_engine) as control:
        session = control.get(OnboardingSession, onboarding_id)
        assert session.status == "PARTIALLY_PROCESSED"
        assert "failed" in session.last_error.lower()

    retried = signed_in.client.post(
        f"/onboarding/import/{onboarding_id}/retry",
        data={"csrf_token": _csrf(signed_in)},
        follow_redirects=False,
    )
    assert retried.status_code == 303
    signed_in.app.state.onboarding_worker.drain()
    after = {doc.original_filename: doc for doc in _documents(signed_in, onboarding_id)}
    assert after["sale_deed.pdf"].status == "READY"
    assert after["broken.pdf"].status == "EXTRACTION_FAILED"


def _second_customer(signed_in, email: str, portfolio_name: str):
    """A second signed-in customer on the same instance, without the closed door.

    Self-registration closes once the first portfolio exists, which is deliberate.
    The test creates the account and session directly, then drives the same HTTP
    routes a person would.
    """
    from starlette.testclient import TestClient

    user, tenant, _membership = register_portfolio(
        signed_in.app.state.control_engine,
        signed_in.app.state.tenant_manager,
        email=email,
        password="second-password-1",
        display_name="Second Owner",
        portfolio_name=portfolio_name,
    )
    token, _session = issue_session(
        signed_in.app.state.control_engine, user, tenant.id
    )
    client = TestClient(signed_in.app)
    client.__enter__()
    client.cookies.set(cookie_name(), token)
    return client, tenant


def test_another_customer_cannot_read_a_staged_session(owner):
    signed_in = owner(email="first@example.com", portfolio_name="First Portfolio")
    onboarding_id = _start(signed_in, _zip({"sale_deed.pdf": _pdf(DEED)}))

    other_client, other_tenant = _second_customer(
        signed_in, "second@example.com", "Second Portfolio"
    )
    try:
        assert other_client.get(f"/onboarding/import/{onboarding_id}").status_code == 404
        assert other_client.get(f"/onboarding/import/{onboarding_id}/review").status_code == 404
        document = _documents(signed_in, onboarding_id)[0]
        assert other_client.get(
            f"/onboarding/import/{onboarding_id}/documents/{document.id}/file"
        ).status_code == 404
        assert other_tenant.id != signed_in.tenant_id
    finally:
        other_client.__exit__(None, None, None)


def test_non_zip_upload_is_refused(owner):
    signed_in = owner()
    response = _upload(signed_in, b"not a zip", filename="documents.txt")
    assert response.status_code == 303
    assert "error=" in response.headers["location"]
    assert "zip" in response.headers["location"].lower()


def test_field_edit_preserves_the_original_and_records_the_decision(owner):
    signed_in = owner()
    onboarding_id = _start(signed_in, _zip({"sale_deed.pdf": _pdf(DEED)}))
    property_proposal = next(
        p for p in _proposals(signed_in, onboarding_id) if p.entity_type == "PROPERTY"
    )
    csrf = _csrf(signed_in)

    edited = signed_in.client.post(
        f"/onboarding/import/{onboarding_id}/records/{property_proposal.id}/field",
        data={
            "field": "purchase_price_cents",
            "action": "save",
            "value": "825000.00",
            "csrf_token": csrf,
        },
        follow_redirects=False,
    )
    assert edited.status_code == 303
    assert "error=" not in edited.headers["location"]

    unknown = signed_in.client.post(
        f"/onboarding/import/{onboarding_id}/records/{property_proposal.id}/field",
        data={
            "field": "acquisition_date",
            "action": "unknown",
            "csrf_token": csrf,
        },
        follow_redirects=False,
    )
    assert unknown.status_code == 303

    with Session(signed_in.app.state.control_engine) as control:
        stored = control.get(OnboardingProposal, property_proposal.id)
        assert '"purchase_price_cents": 82500000' in stored.payload_json
        assert '"acquisition_date": null' in stored.payload_json
        # The extraction remains exactly what the document said.
        assert '"purchase_price_cents": 80000000' in stored.original_payload_json
        assert '"acquisition_date": "2021-06-15"' in stored.original_payload_json
        decisions = control.exec(
            select(OnboardingReviewDecision)
            .where(OnboardingReviewDecision.proposal_id == property_proposal.id)
            .order_by(OnboardingReviewDecision.created_at)
        ).all()
        assert [decision.decision for decision in decisions] == ["EDIT", "MARK_UNKNOWN"]
        assert decisions[0].actor_user_id

    # The browser form posts the edited text under `value`, not under the field's
    # own name. If those drift apart, a save looks successful but stores null.
    record_url = f"/onboarding/import/{onboarding_id}/records/{property_proposal.id}"
    page = signed_in.client.get(record_url).text
    assert 'id="purchase_price_cents"' in page
    assert 'name="value"' in page
    assert 'name="purchase_price_cents"' not in page

    labelled = signed_in.client.post(
        f"{record_url}/field",
        data={
            "field": "short_label",
            "action": "save",
            "value": "Bluebonnet",
            "csrf_token": csrf,
        },
        follow_redirects=False,
    )
    assert labelled.status_code == 303
    with Session(signed_in.app.state.control_engine) as control:
        stored = control.get(OnboardingProposal, property_proposal.id)
        assert '"short_label": "Bluebonnet"' in stored.payload_json
    assert "Bluebonnet" in signed_in.client.get(record_url).text

    saved_page = signed_in.client.get(record_url + "?message=Short+label+updated.")
    assert "Short label updated." in saved_page.text
    assert "pt-toast--success" in saved_page.text
    assert "pt-alert--success" not in saved_page.text

    failed_page = signed_in.client.get(record_url + "?error=Cannot+save+that+value.")
    assert "Cannot save that value." in failed_page.text
    assert "pt-toast--error" in failed_page.text

    # The record screen updates a field in place with this response; no reload.
    in_place = signed_in.client.post(
        f"{record_url}/field",
        data={
            "field": "short_label",
            "action": "save",
            "value": "Larkspur",
            "csrf_token": csrf,
        },
        headers={"accept": "application/json"},
        follow_redirects=False,
    )
    assert in_place.status_code == 200
    body = in_place.json()
    assert body["ok"] is True
    assert body["display"] == "Larkspur"
    assert body["message"] == "Short label updated."
    assert body["edited"] is True

    unknown_json = signed_in.client.post(
        f"{record_url}/field",
        data={
            "field": "acquisition_date",
            "action": "unknown",
            "csrf_token": csrf,
        },
        headers={"accept": "application/json"},
        follow_redirects=False,
    )
    assert unknown_json.status_code == 200
    assert unknown_json.json()["display"] == "—"

    invalid_json = signed_in.client.post(
        f"{record_url}/field",
        data={
            "field": "purchase_price_cents",
            "action": "save",
            "value": "not money",
            "csrf_token": csrf,
        },
        headers={"accept": "application/json"},
        follow_redirects=False,
    )
    assert invalid_json.status_code == 400
    assert invalid_json.json()["ok"] is False


def test_document_inspector_returns_to_the_record_it_was_opened_from(owner):
    signed_in = owner()
    onboarding_id = _start(signed_in, _zip({"sale_deed.pdf": _pdf(DEED)}))
    property_proposal = next(
        p for p in _proposals(signed_in, onboarding_id) if p.entity_type == "PROPERTY"
    )
    document = _documents(signed_in, onboarding_id)[0]
    record_url = f"/onboarding/import/{onboarding_id}/records/{property_proposal.id}"

    record_page = signed_in.client.get(record_url).text
    assert "Open inspector" in record_page
    assert "next=" in record_page

    with Session(signed_in.app.state.control_engine) as control:
        evidence = control.exec(
            select(OnboardingFieldEvidence).where(
                OnboardingFieldEvidence.document_id == document.id
            )
        ).first()
        assert evidence is not None
        evidence_id = evidence.id
    location = signed_in.client.get(
        f"/onboarding/import/{onboarding_id}/documents/{document.id}/evidence/{evidence_id}/location"
    )
    assert location.status_code == 200
    assert location.json()["status"] in {"AVAILABLE", "AMBIGUOUS", "UNAVAILABLE"}
    assert signed_in.client.get(
        f"/onboarding/import/{onboarding_id}/documents/{document.id}/evidence/ev_missing/location"
    ).status_code == 404

    inspector = signed_in.client.get(
        f"/onboarding/import/{onboarding_id}/documents/{document.id}",
        params={"next": f"{record_url}?doc={document.id}&page=1"},
    )
    assert inspector.status_code == 200
    assert "Back to record" in inspector.text
    assert f'href="{record_url}' in inspector.text

    # An off-site return path is ignored rather than becoming an open redirect.
    unsafe = signed_in.client.get(
        f"/onboarding/import/{onboarding_id}/documents/{document.id}",
        params={"next": "https://example.com/phish"},
    )
    assert f'href="/onboarding/import/{onboarding_id}"' in unsafe.text


def test_provisioning_reuses_an_existing_property_at_the_same_address(owner):
    signed_in = owner()
    onboarding_id = _start(signed_in, _zip({"sale_deed.pdf": _pdf(DEED)}))
    property_proposal = next(
        p for p in _proposals(signed_in, onboarding_id) if p.entity_type == "PROPERTY"
    )

    # The customer already typed this door into the setup wizard. Importing the deed
    # must recognise it rather than create a second property at the same address.
    with Session(signed_in.engine) as tenant:
        existing = Property(
            id="prop_already_there",
            name="123 Main St",
            address_line1="123 Main St",
            city="Springfield",
            state="IL",
            zip_code="62704",
        )
        tenant.add(existing)
        tenant.commit()

    assert "error=" not in _accept(signed_in, onboarding_id, property_proposal.id).headers["location"]
    response = signed_in.client.post(
        f"/onboarding/import/{onboarding_id}/approve",
        data={"confirm": "yes", "csrf_token": _csrf(signed_in)},
        follow_redirects=False,
    )
    assert response.status_code == 303
    signed_in.app.state.onboarding_worker.drain()

    with Session(signed_in.engine) as tenant:
        properties = tenant.exec(select(Property)).all()
        assert [prop.id for prop in properties] == ["prop_already_there"]


def test_statement_layouts_do_not_turn_prose_or_dates_into_identity():
    from proptally.onboarding.extraction import (
        RuleBasedExtractor,
        parse_loan_number,
        parse_rate_bps,
        plausible_address,
    )

    assert parse_loan_number("Account Number ends in 0708") is None
    assert parse_loan_number("10/14/2025") is None
    assert parse_loan_number("Loan Number 1350010708") == "1350010708"
    assert parse_rate_bps("Interest Rate (Until 10/01/2032)") is None
    assert parse_rate_bps("4.990%") == 4990
    assert plausible_address("securing", "") is False
    assert plausible_address("582 CIMMARON CIR", "CRYSTAL LAKE") is True

    pnc = """PNC Mortgage
Account Number
1350010708
Statement Date
04/24/2026
Interest Rate (Until 10/01/2032)
4.990%
Regular Monthly Payment
$1,801.48
Outstanding Principal
$209,897.83
Property Address
582 CIMMARON CIR
CRYSTAL LAKE, IL 60012
"""
    result = RuleBasedExtractor().extract(
        category="MORTGAGE_STATEMENT",
        filename="PNC Mortgage/statement.pdf",
        pages=[pnc],
    )
    assert result.fields["lender"].value == "PNC Mortgage"
    assert result.fields["loan_number"].value == "1350010708"
    assert result.fields["interest_rate_bps"].value == 4990
    assert result.fields["emi_amount_cents"].value == 180_148
    assert result.fields["address_line1"].value == "582 CIMMARON CIR"
    assert result.fields["city"].value == "CRYSTAL LAKE"
    assert result.fields["zip_code"].value == "60012"

    tax_form = """SUBSTITUTE FORM 1098
Account number (see instructions)
Recipient/Lender's TIN
Property Address
securing mortgage
"""
    result = RuleBasedExtractor().extract(
        category="MORTGAGE_STATEMENT",
        filename="PNC Mortgage/tax_2025.pdf",
        pages=[tax_form],
    )
    assert "loan_number" not in result.fields
    assert "outstanding_principal_cents" not in result.fields
    assert "emi_amount_cents" not in result.fields
    assert "address_line1" not in result.fields


def test_historical_statements_reconcile_to_one_loan_and_latest_values(owner):
    signed_in = owner()
    # A year of statements plus a 1098 for the same PNC loan. The 1098 omits the
    # full account number and reports no current balance.
    statement_2025 = """PNC Mortgage
Account Number
1350010708
Statement Date
01/31/2025
Interest Rate
2.500%
Regular Monthly Payment
$1,800.00
Outstanding Principal
$210,000.00
Property Address
582 CIMMARON CIR
CRYSTAL LAKE, IL 60012
"""
    statement_2026 = """PNC Mortgage
Account Number
1350010708
Statement Date
09/25/2026
Interest Rate
2.500%
Regular Monthly Payment
$1,769.08
Outstanding Principal
$207,272.56
Property Address
582 CIMMARON CIR
CRYSTAL LAKE, IL 60012
"""
    tax_form = """SUBSTITUTE FORM 1098
Account number (see instructions)
Recipient/Lender's TIN
Property Address
securing mortgage
"""
    onboarding_id = _start(
        signed_in,
        _zip({
            "PNC Mortgage/statement_2025.pdf": _pdf(statement_2025),
            "PNC Mortgage/statement_2026.pdf": _pdf(statement_2026),
            "PNC Mortgage/tax_2025.pdf": _pdf(tax_form),
        }),
    )

    proposals = _proposals(signed_in, onboarding_id)
    properties = [p for p in proposals if p.entity_type == "PROPERTY"]
    mortgages = [p for p in proposals if p.entity_type == "MORTGAGE"]
    assert len(properties) == 1
    assert len(mortgages) == 1
    mortgage = mortgages[0]
    assert mortgage.status == "NEEDS_REVIEW"
    assert '"loan_number": "1350010708"' in mortgage.payload_json
    assert '"statement_date": "2026-09-25"' in mortgage.payload_json
    assert '"outstanding_principal_cents": 20727256' in mortgage.payload_json
    assert '"emi_amount_cents": 176908' in mortgage.payload_json
    assert '"interest_rate_bps": 2500' in mortgage.payload_json
    assert mortgage.conflicts_json == "{}"
    assert '"property_group_key": "property:582 cimmaron cir crystal lake 60012"' in mortgage.payload_json


def test_upload_page_starts_at_the_first_step_when_no_import_exists(owner):
    signed_in = owner()
    page = signed_in.client.get("/onboarding/import")
    assert page.status_code == 200
    assert "Upload documents" in page.text
    # The dropzone is the action; a second "Choose a ZIP" control is redundant.
    assert "Choose a ZIP" not in page.text
    assert "Upload and process" in page.text
    # The top card is a short brief, and the next steps are a five-stage flow.
    assert "pt-upload-brief" in page.text
    assert "Put your deeds, mortgage statements" in page.text
    assert page.text.count('class="pt-import-steps__item') == 3
    assert "What happens next" in page.text
    assert page.text.count('class="pt-flow__step"') == 5


def test_status_page_filters_and_search_the_document_table(owner):
    signed_in = owner()
    deed = _pdf(DEED)
    onboarding_id = _start(
        signed_in,
        _zip({
            "sale_deed.pdf": deed,
            "sale_deed_copy.pdf": deed,
        }),
    )

    duplicates = signed_in.client.get(
        f"/onboarding/import/{onboarding_id}?doc_status=duplicate"
    )
    assert duplicates.status_code == 200
    assert "Showing 1 of 2 files" in duplicates.text
    assert "sale_deed_copy.pdf" in duplicates.text
    assert "sale_deed.pdf" not in duplicates.text

    search = signed_in.client.get(
        f"/onboarding/import/{onboarding_id}?q=sale_deed_copy"
    )
    assert search.status_code == 200
    assert "Showing 1 of 2 files" in search.text
    assert "sale_deed_copy.pdf" in search.text


def test_edited_property_and_mortgage_sorted_first_still_provision(owner):
    """A mortgage can be created before its property, and an edited address must
    not detach it: provisioning orders properties first and links by group_key."""
    signed_in = owner()
    onboarding_id = _start(
        signed_in,
        _zip({
            "sale_deed.pdf": _pdf(DEED),
            "mortgage_statement.pdf": _pdf(MORTGAGE),
            "bank_statement.pdf": _pdf(BANK),
            "lease.pdf": _pdf(LEASE),
        }),
    )
    proposals = _proposals(signed_in, onboarding_id)
    prop = next(p for p in proposals if p.entity_type == "PROPERTY")
    mortgage = next(p for p in proposals if p.entity_type == "MORTGAGE")

    # Edit the property address in review. Its durable group_key stays the one the
    # mortgage payload names.
    edited = signed_in.client.post(
        f"/onboarding/import/{onboarding_id}/records/{prop.id}/field",
        data={
            "field": "address_line1", "action": "save",
            "value": "456 Edited Ave", "csrf_token": _csrf(signed_in),
        },
        follow_redirects=False,
    )
    assert edited.status_code == 303, edited.text[:400]

    # Make the mortgage strictly older than the property, so created_at ordering
    # would process it first without the two-pass sort.
    with Session(signed_in.app.state.control_engine) as control:
        property_row = control.get(OnboardingProposal, prop.id)
        mortgage_row = control.get(OnboardingProposal, mortgage.id)
        property_row.created_at = datetime.now(UTC)
        mortgage_row.created_at = datetime.now(UTC) - timedelta(days=1)
        control.add(property_row)
        control.add(mortgage_row)
        control.commit()

    for proposal in _proposals(signed_in, onboarding_id):
        accepted = _accept(signed_in, onboarding_id, proposal.id)
        assert accepted.status_code == 303
        assert "error=" not in accepted.headers["location"], accepted.headers["location"]

    response = signed_in.client.post(
        f"/onboarding/import/{onboarding_id}/approve",
        data={"confirm": "yes", "csrf_token": _csrf(signed_in)},
        follow_redirects=False,
    )
    assert response.status_code == 303
    signed_in.app.state.onboarding_worker.drain()

    with Session(signed_in.app.state.control_engine) as control:
        assert control.get(OnboardingSession, onboarding_id).status == "COMPLETED"

    with Session(signed_in.engine) as tenant:
        properties = tenant.exec(select(Property)).all()
        assert len(properties) == 1
        assert properties[0].address_line1 == "456 Edited Ave"
        loans = tenant.exec(select(LoanAccount)).all()
        assert len(loans) == 1
        assert loans[0].property_id == properties[0].id
        # The mortgage opening balance reached the ledger under its own key.
        opening = tenant.exec(
            select(JournalEntry).where(
                JournalEntry.idempotency_key == f"MORTGAGE_OPENING:{loans[0].id}"
            )
        ).first()
        assert opening is not None and opening.status == "POSTED"


def test_onboarding_stages_a_supported_bank_statement_for_ingestion(owner, tmp_path):
    """Bank and card PDFs reach the same extraction queue as an upload."""
    signed_in = owner()
    pdf = bofa_checking_pdf(tmp_path / "bank.pdf")
    with Session(signed_in.engine) as tenant:
        tenant.add(FinancialInstitution(id="inst_onboarding_bofa", name="Bank of America"))
        tenant.flush()
        tenant.add(FinancialAccount(
            id="acct_onboarding_bofa_9456",
            institution_id="inst_onboarding_bofa",
            account_name="Bank of America Checking (...9456)",
            account_type="CHECKING",
            account_number_masked="...9456",
        ))
        tenant.flush()
        document = Document(
            id="doc_onboarding_bank",
            file_sha256="onboarding-bank-sha",
            original_filename="bank_statement.pdf",
            storage_path=str(pdf),
            file_size_bytes=pdf.stat().st_size,
            mime_type="application/pdf",
            document_type="CHECKING_STATEMENT",
        )
        tenant.add(document)
        tenant.commit()

        _stage_statement_extractions(
            tenant,
            [{"id": "stage_bank", "category": "BANK_STATEMENT"}],
            [("stage_bank", document.id)],
        )
        tenant.commit()

        extraction = tenant.exec(
            select(DocumentExtraction).where(
                DocumentExtraction.document_id == document.id
            )
        ).one()
        data = json.loads(extraction.extracted_data_json)
        assert data["statement_type"] == "CHECKING"
        assert data["financial_account_id"] == "acct_onboarding_bofa_9456"


def test_onboarding_stages_mortgage_statements_for_ingestion(owner):
    """A mortgage PDF copied by onboarding is parsed and linked to the new loan,
    so it appears in Statements & Ingestion without a second upload."""
    signed_in = owner()
    onboarding_id = _start(
        signed_in,
        _zip({
            "sale_deed.pdf": _pdf(DEED),
            "mortgage_statement.pdf": _pdf(MORTGAGE),
        }),
    )
    for proposal in _proposals(signed_in, onboarding_id):
        accepted = _accept(signed_in, onboarding_id, proposal.id)
        assert accepted.status_code == 303
        assert "error=" not in accepted.headers["location"], accepted.headers["location"]

    response = signed_in.client.post(
        f"/onboarding/import/{onboarding_id}/approve",
        data={"confirm": "yes", "csrf_token": _csrf(signed_in)},
        follow_redirects=False,
    )
    assert response.status_code == 303
    signed_in.app.state.onboarding_worker.drain()

    with Session(signed_in.app.state.control_engine) as control:
        assert control.get(OnboardingSession, onboarding_id).status == "COMPLETED"

    with Session(signed_in.engine) as tenant:
        loan = tenant.exec(select(LoanAccount)).one()
        document = tenant.exec(
            select(Document).where(Document.original_filename == "mortgage_statement.pdf")
        ).one()
        extraction = tenant.exec(
            select(DocumentExtraction).where(DocumentExtraction.document_id == document.id)
        ).one()
        data = json.loads(extraction.extracted_data_json)
        assert data["statement_type"] == "MORTGAGE"
        assert data["loan_account_id"] == loan.id
        assert data["property_id"] == loan.property_id
        assert extraction.approval_status == "PENDING"
        assert extraction.validation_status in {"VALIDATED", "NEEDS_REVIEW"}

    page = signed_in.client.get("/statements")
    assert page.status_code == 200
    assert "mortgage_statement.pdf" in page.text


def test_document_previews_are_allowed_in_same_origin_frames(owner):
    signed_in = owner()
    page = signed_in.client.get("/onboarding/import")
    assert page.status_code == 200
    assert page.headers["X-Frame-Options"] == "SAMEORIGIN"
    csp = page.headers["Content-Security-Policy"]
    assert "frame-ancestors 'self'" in csp
    assert "frame-src 'self'" in csp
