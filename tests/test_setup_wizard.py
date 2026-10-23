"""Guided portfolio setup: resume, skip, completion and derived progress."""

import re

from sqlmodel import Session, select
from starlette.testclient import TestClient

from proptally.db.models import FinancialAccount, PortfolioSetup, Property
from proptally.db.system_models import ROLE_READ_ONLY, TenantMembership, User
from proptally.domain.setup_progress import evaluate
from proptally.web.auth import cookie_name, hash_password, issue_session

PROPERTY_FORM = {
    "name": "4409 Bluebonnet Ln",
    "address_line1": "4409 Bluebonnet Ln",
    "address_line2": "",
    "city": "Austin",
    "state": "TX",
    "zip_code": "78704",
    "property_type": "RENTAL_INVESTMENT",
    "acquisition_date": "",
    "purchase_price": "",
    "land_basis": "",
    "building_basis": "",
    "current_valuation": "",
    "valuation_date": "",
    "short_label": "",
    "return_to": "/setup",
}

ACCOUNT_FORM = {
    "institution_name": "Lone Star Bank",
    "account_name": "Operating Checking",
    "account_type": "CHECKING",
    "account_number_masked": "...1111",
    "mixed_use_status": "UNCLASSIFIED",
    "return_to": "/setup",
}


def _ids(signed_in):
    with Session(signed_in.engine) as session:
        prop = session.exec(select(Property)).one()
        account = session.exec(select(FinancialAccount)).one()
        return prop.id, account.id


def _progress(signed_in):
    with Session(signed_in.engine) as session:
        return evaluate(session)


def test_fresh_portfolio_starts_the_guided_flow(owner):
    signed_in = owner()
    dashboard = signed_in.client.get("/")
    assert "Start guided setup" in dashboard.text
    assert 'href="/setup"' in dashboard.text

    setup = signed_in.client.get("/setup", follow_redirects=False)
    assert setup.status_code == 303
    assert setup.headers["location"].startswith("/setup/property")

    step = signed_in.client.get("/setup/property")
    assert step.status_code == 200
    assert "Add your first property" in step.text
    assert 'name="return_to" value="/setup"' in step.text
    assert "data-location-picker" in step.text


def test_saving_a_property_advances_and_resumes_at_the_next_step(owner):
    signed_in = owner()
    saved = signed_in.post("/properties/create", data=PROPERTY_FORM, follow_redirects=False)
    assert saved.status_code == 303
    assert saved.headers["location"].startswith("/setup")

    resumed = signed_in.client.get("/setup", follow_redirects=False)
    assert resumed.status_code == 303
    assert resumed.headers["location"].startswith("/setup/accounts")

    progress = _progress(signed_in)
    assert progress.percent == 20
    assert progress.done_count == 1
    assert progress.next_step == "accounts"


def test_skipping_the_mortgage_removes_it_from_the_total(owner):
    signed_in = owner()
    signed_in.post("/properties/create", data=PROPERTY_FORM, follow_redirects=False)
    signed_in.post("/accounts/create", data=ACCOUNT_FORM, follow_redirects=False)

    skipped = signed_in.post("/setup/mortgage/skip", data={}, follow_redirects=False)
    assert skipped.status_code == 303
    assert skipped.headers["location"] == "/setup"

    progress = _progress(signed_in)
    assert progress.total == 4
    assert progress.percent == 50
    assert progress.next_step == "opening-balances"

    page = signed_in.client.get("/setup/mortgage")
    assert page.status_code == 200
    assert "Marked as not applicable" in page.text


def test_completion_is_derived_from_the_portfolio(owner):
    signed_in = owner()
    signed_in.post("/properties/create", data=PROPERTY_FORM, follow_redirects=False)
    signed_in.post("/accounts/create", data=ACCOUNT_FORM, follow_redirects=False)
    signed_in.post("/setup/mortgage/skip", data={}, follow_redirects=False)
    property_id, account_id = _ids(signed_in)

    opening = signed_in.post(
        "/opening-balances",
        data={
            "as_of": "2026-01-01",
            f"asset_{property_id}": "400000.00",
            f"amount_{account_id}": "5000.00",
            "return_to": "/setup",
        },
        follow_redirects=False,
    )
    assert opening.status_code == 303

    progress = _progress(signed_in)
    assert progress.publishable is True
    assert progress.published is False
    assert progress.percent == 75

    home = signed_in.client.get("/setup", follow_redirects=False)
    assert home.status_code == 200
    assert "Portfolio essentials complete" in home.text
    assert "Publish portfolio" in home.text

    review = signed_in.post("/setup/publish", data={}, follow_redirects=False)
    assert review.status_code == 303
    assert review.headers["location"] == "/setup/publish/review"
    assert _progress(signed_in).published is False

    page = signed_in.client.get("/setup/publish/review")
    assert page.status_code == 200
    assert "Review before publishing" in page.text
    assert "Property cost basis" in page.text

    published = signed_in.post(
        "/setup/publish", data={"acknowledge_gaps": "1"}, follow_redirects=False
    )
    assert published.status_code == 303
    assert published.headers["location"] == "/setup/done"
    assert _progress(signed_in).published is True

    manual = signed_in.post(
        "/transactions/manual",
        data={
            "account_id": account_id,
            "transaction_date": "2026-02-01",
            "description": "RENT DEPOSIT FEBRUARY",
            "amount_dollars": "2500.00",
            "direction": "INFLOW",
        },
        follow_redirects=False,
    )
    assert manual.status_code == 303

    progress = _progress(signed_in)
    assert progress.complete is True
    assert progress.percent == 100

    done = signed_in.client.get("/setup/done")
    assert done.status_code == 200
    assert "Portfolio is live" in done.text

    dashboard = signed_in.client.get("/")
    assert "Portfolio Financial Overview" in dashboard.text
    assert "Finish portfolio setup" not in dashboard.text


def test_revisiting_a_done_step_adds_another_property(owner):
    signed_in = owner()
    signed_in.post("/properties/create", data=PROPERTY_FORM, follow_redirects=False)

    revisited = signed_in.client.get("/setup/property")
    assert revisited.status_code == 200
    assert "Already in this portfolio" in revisited.text

    second = signed_in.post(
        "/properties/create",
        data={
            **PROPERTY_FORM,
            "name": "Second Door",
            "address_line1": "2 Second St",
        },
        follow_redirects=False,
    )
    assert second.status_code == 303
    with Session(signed_in.engine) as session:
        assert len(session.exec(select(Property)).all()) == 2
    assert _progress(signed_in).percent == 20


def test_publish_requires_the_essentials(owner):
    signed_in = owner()
    response = signed_in.post("/setup/publish", data={}, follow_redirects=False)
    assert response.status_code == 303
    assert "/setup?error=" in response.headers["location"]
    assert _progress(signed_in).published is False
    assert "DRAFT" in signed_in.client.get("/").text


def test_every_step_renders(owner):
    signed_in = owner()
    assert "Add a bank or card account" in signed_in.client.get("/setup/accounts").text

    signed_in.post("/properties/create", data=PROPERTY_FORM, follow_redirects=False)
    assert "Add a mortgage loan" in signed_in.client.get("/setup/mortgage").text
    assert "Record opening balances" in signed_in.client.get("/setup/opening-balances").text
    assert "Bring in your first records" in signed_in.client.get("/setup/records").text


def _read_only_client(signed_in) -> TestClient:
    with Session(signed_in.control_engine) as session:
        user = User(
            id="usr_readonly_setup",
            email="readonly-setup@example.com",
            password_hash=hash_password("read-only-password-1"),
            display_name="Read Only",
        )
        session.add(user)
        session.flush()
        session.add(TenantMembership(
            id="mem_readonly_setup",
            user_id=user.id,
            tenant_id=signed_in.tenant_id,
            role=ROLE_READ_ONLY,
        ))
        session.commit()
        token, _ = issue_session(signed_in.control_engine, user, signed_in.tenant_id)
    client = TestClient(signed_in.app)
    client.__enter__()
    client.cookies.set(cookie_name(), token)
    return client


def test_read_only_member_can_view_but_not_skip(owner):
    signed_in = owner()
    read_only = _read_only_client(signed_in)
    page = read_only.get("/setup/property")
    assert page.status_code == 200
    assert "Read-only role" in page.text

    token = re.search(r'name="csrf_token" value="([^"]+)"', read_only.get("/account").text)
    assert token
    refused = read_only.post(
        "/setup/mortgage/skip",
        data={"csrf_token": token.group(1)},
        follow_redirects=False,
    )
    assert refused.status_code == 403
    with Session(signed_in.engine) as session:
        setup = session.get(PortfolioSetup, "portfolio")
        assert setup is None or "mortgage" not in (setup.skipped_steps or "")


def test_a_legacy_portfolio_is_enrolled_only_when_setup_is_opened(owner):
    signed_in = owner()
    with Session(signed_in.engine) as session:
        setup = session.get(PortfolioSetup, "portfolio")
        if setup is not None:
            session.delete(setup)
            session.commit()

    signed_in.post(
        "/properties/create",
        data={**PROPERTY_FORM, "return_to": ""},
        follow_redirects=False,
    )
    dashboard = signed_in.client.get("/")
    assert "Finish portfolio setup" not in dashboard.text

    setup = signed_in.client.get("/setup", follow_redirects=False)
    assert setup.status_code == 303
    assert setup.headers["location"].startswith("/setup/accounts")

    with Session(signed_in.engine) as session:
        assert session.get(PortfolioSetup, "portfolio") is not None


def test_deleting_data_reopens_the_checklist(owner):
    signed_in = owner()
    signed_in.post("/properties/create", data=PROPERTY_FORM, follow_redirects=False)
    signed_in.post("/accounts/create", data=ACCOUNT_FORM, follow_redirects=False)
    assert _progress(signed_in).percent == 40

    with Session(signed_in.engine) as session:
        account = session.exec(select(FinancialAccount)).one()
        session.delete(account)
        session.commit()

    progress = _progress(signed_in)
    assert progress.percent == 20
    assert progress.next_step == "accounts"


def test_opening_balances_offer_statement_figures(owner):
    """A statement's opening balance is offered per account, with its own date."""
    from datetime import date

    from proptally.db.models import Document, FinancialAccount, Statement

    signed_in = owner()
    signed_in.post("/accounts/create", data=ACCOUNT_FORM, follow_redirects=False)
    with Session(signed_in.engine) as session:
        account = session.exec(select(FinancialAccount)).one()
        account_id = account.id
        session.add(Document(
            id="doc_open", file_sha256="o" * 64, original_filename="stmt.pdf",
            storage_path="/vault/stmt.pdf", file_size_bytes=10, mime_type="application/pdf",
        ))
        session.add(Statement(
            id="stmt_open", document_id="doc_open", financial_account_id=account_id,
            statement_type="CHECKING", active_parser_name="test", active_parser_version="1",
            period_start_date=date(2021, 8, 18), period_end_date=date(2021, 9, 16),
            statement_date=date(2021, 9, 16),
            opening_balance_cents=334473, closing_balance_cents=602828,
            ingestion_status="COMMITTED",
        ))
        session.commit()

    page = signed_in.client.get("/setup/opening-balances")
    assert page.status_code == 200
    assert "From your imported statements" in page.text
    assert f'data-opening-fill="amount_{account_id}"' in page.text
    assert 'data-opening-amount="3344.73"' in page.text
    assert 'data-opening-date="2021-08-18"' in page.text
    # Cost basis has no statement source.
    assert "data-opening-fill=\"asset_" not in page.text
