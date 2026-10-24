"""The property detail page's three-card financial overview."""

from datetime import date, timedelta

from sqlmodel import Session, select

from proptally.db.models import (
    Document,
    Lease,
    LoanAccount,
    MortgagePeriod,
    Property,
    PropertyValuation,
    Statement,
)

PROPERTY = {
    "name": "Overview Test Door",
    "address_line1": "1 Overview St",
    "address_line2": "",
    "city": "Austin",
    "state": "TX",
    "zip_code": "78701",
    "property_type": "RENTAL_INVESTMENT",
    "acquisition_date": "2020-01-01",
    "purchase_price": "400000.00",
    "land_basis": "80000.00",
    "building_basis": "320000.00",
    "current_valuation": "450000.00",
    "valuation_date": "2026-01-01",
    "short_label": "",
}


def _property_id(signed_in) -> str:
    with Session(signed_in.engine) as session:
        return session.exec(select(Property)).one().id


def test_reference_property_renders_the_three_overview_cards(owner):
    signed_in = owner(reference_portfolio=True)
    page = signed_in.client.get("/properties/prop_tinsley_huntley")
    assert page.status_code == 200
    assert "Property financial overview" in page.text
    assert "Mortgage" in page.text
    assert "Value &amp; position" in page.text
    assert "Estimated" in page.text
    assert "Principal paid down" in page.text


def test_mortgage_periods_populate_the_repayment_widget(owner):
    signed_in = owner(reference_portfolio=True)
    with Session(signed_in.engine) as session:
        loan = session.exec(
            select(LoanAccount).where(LoanAccount.property_id == "prop_tinsley_huntley")
        ).one()
        document = Document(
            id="doc_overview_stmt",
            file_sha256="overview-statement-hash",
            original_filename="mortgage.pdf",
            storage_path="/tmp/mortgage.pdf",
            file_size_bytes=1,
            document_type="MORTGAGE_STATEMENT",
            property_id="prop_tinsley_huntley",
        )
        statement = Statement(
            id="stmt_overview",
            document_id=document.id,
            financial_account_id=loan.financial_account_id,
            statement_type="MORTGAGE",
            active_parser_name="test",
            active_parser_version="1",
            statement_date=date(2026, 1, 1),
        )
        session.add(document)
        session.add(statement)
        session.flush()
        session.add(MortgagePeriod(
            id="mp_overview_1",
            statement_id=statement.id,
            loan_account_id=loan.id,
            payment_due_date=date(2026, 1, 1),
            statement_date=date(2026, 1, 1),
            principal_billed_cents=100000,
            interest_billed_cents=50000,
            escrow_billed_cents=20000,
            total_amount_billed_cents=170000,
            principal_applied_cents=100000,
            interest_applied_cents=50000,
            escrow_applied_cents=20000,
            total_amount_applied_cents=170000,
            outstanding_principal_cents=loan.original_principal_cents - 100000,
            interest_rate_bps=3125,
            status="FULLY_SETTLED",
        ))
        session.add(MortgagePeriod(
            id="mp_overview_2",
            statement_id=statement.id,
            loan_account_id=loan.id,
            payment_due_date=date(2026, 2, 1),
            statement_date=date(2026, 2, 1),
            principal_billed_cents=100000,
            interest_billed_cents=50000,
            escrow_billed_cents=20000,
            total_amount_billed_cents=170000,
            principal_applied_cents=100000,
            interest_applied_cents=50000,
            escrow_applied_cents=20000,
            total_amount_applied_cents=170000,
            outstanding_principal_cents=loan.original_principal_cents - 200000,
            interest_rate_bps=3125,
            status="FULLY_SETTLED",
        ))
        session.commit()

    page = signed_in.client.get("/properties/prop_tinsley_huntley")
    assert page.status_code == 200
    assert "Mortgage repayment" in page.text
    assert "Instalments paid" in page.text
    assert "Instalments remaining" in page.text
    assert "Last EMI recorded" in page.text
    assert "of scheduled instalments completed" in page.text


def test_acquisition_costs_and_arm_terms_are_persisted_and_shown(owner):
    signed_in = owner()
    payload = {
        **PROPERTY,
        "down_payment": "90000.00",
        "closing_costs": "12000.00",
        "furnishing_costs": "5000.00",
    }
    signed_in.post("/properties/create", data=payload, follow_redirects=False)
    property_id = _property_id(signed_in)

    mortgage = signed_in.post(
        "/mortgages/create",
        data={
            "property_id": property_id,
            "institution_name": "Test Lender",
            "account_name": "Test ARM Loan",
            "loan_number": "TEST-ARM-1",
            "original_principal": "300000.00",
            "opening_principal": "300000.00",
            "interest_rate": "6.125",
            "rate_type": "ARM",
            "term_months": "360",
            "origination_date": date.today().isoformat(),
        },
        follow_redirects=False,
    )
    assert mortgage.status_code == 303
    assert "error=" not in mortgage.headers["location"]

    with Session(signed_in.engine) as session:
        prop = session.get(Property, property_id)
        assert prop.down_payment_cents == 9_000_000
        assert prop.closing_costs_cents == 1_200_000
        assert prop.furnishing_costs_cents == 500_000
        loan = session.exec(select(LoanAccount)).one()
        assert loan.rate_type == "ARM"

    page = signed_in.client.get(f"/properties/{property_id}")
    assert page.status_code == 200
    assert "Adjustable (ARM)" in page.text
    assert "Month 1 of 360" in page.text
    assert "Down payment" in page.text
    assert "Closing costs" in page.text
    assert "Furnishing" in page.text
    assert "Cash invested" in page.text


def test_a_mortgage_free_property_shows_the_mortgage_free_state(owner):
    signed_in = owner()
    signed_in.post("/properties/create", data=PROPERTY, follow_redirects=False)
    page = signed_in.client.get(f"/properties/{_property_id(signed_in)}")
    assert page.status_code == 200
    assert "No mortgage recorded" in page.text
    assert "Estimated value" in page.text
    assert "Mortgage-free" in page.text


def test_a_property_without_a_valuation_states_what_is_missing(owner):
    signed_in = owner()
    payload = {**PROPERTY, "current_valuation": "", "valuation_date": ""}
    signed_in.post("/properties/create", data=payload, follow_redirects=False)
    page = signed_in.client.get(f"/properties/{_property_id(signed_in)}")
    assert page.status_code == 200
    assert "A current valuation is needed" in page.text
    assert "Valuation not recorded" in page.text


def test_statement_balances_show_principal_paid_down(owner):
    signed_in = owner(reference_portfolio=True)
    with Session(signed_in.engine) as session:
        loan = session.exec(
            select(LoanAccount).where(LoanAccount.property_id == "prop_tinsley_huntley")
        ).one()
        document = Document(
            id="doc_progress_stmt",
            file_sha256="progress-statement-hash",
            original_filename="progress.pdf",
            storage_path="/tmp/progress.pdf",
            file_size_bytes=1,
            document_type="MORTGAGE_STATEMENT",
            property_id="prop_tinsley_huntley",
        )
        statement = Statement(
            id="stmt_progress",
            document_id=document.id,
            financial_account_id=loan.financial_account_id,
            statement_type="MORTGAGE",
            active_parser_name="test",
            active_parser_version="1",
            statement_date=date(2026, 1, 1),
        )
        session.add(document)
        session.add(statement)
        session.flush()
        for index, (month, principal) in enumerate(((1, 30_000_000), (2, 29_000_000))):
            session.add(MortgagePeriod(
                id=f"mp_progress_{index}",
                statement_id=statement.id,
                loan_account_id=loan.id,
                payment_due_date=date(2026, month, 1),
                statement_date=date(2026, month, 1),
                principal_billed_cents=100_000,
                interest_billed_cents=50_000,
                escrow_billed_cents=20_000,
                total_amount_billed_cents=170_000,
                outstanding_principal_cents=principal,
                interest_rate_bps=3125,
                status="BILLED",
            ))
        session.commit()

    page = signed_in.client.get("/properties/prop_tinsley_huntley")
    assert page.status_code == 200
    assert "Based on servicer statement balances" in page.text
    assert "Paid down" in page.text
    assert "Current principal" in page.text
    assert "3.3%" in page.text
    assert "Mortgage Statements" in page.text
    assert "AWAITING BANK PAYMENT" in page.text
    assert "Mortgage Loan Detail" in page.text
    assert 'id="mortgage-loan-detail"' in page.text
    assert 'href="#mortgage-loan-detail"' in page.text
    assert "View full statement history" in page.text
    assert "UNPAID" in page.text


def test_next_payment_due_picks_upcoming_period_not_oldest_historical(owner):
    signed_in = owner(reference_portfolio=True)
    with Session(signed_in.engine) as session:
        loan = session.exec(
            select(LoanAccount).where(LoanAccount.property_id == "prop_tinsley_huntley")
        ).one()
        document = Document(
            id="doc_history_stmt",
            file_sha256="history-hash",
            original_filename="history.pdf",
            storage_path="/tmp/history.pdf",
            file_size_bytes=1,
            document_type="MORTGAGE_STATEMENT",
            property_id="prop_tinsley_huntley",
        )
        statement = Statement(
            id="stmt_history",
            document_id=document.id,
            financial_account_id=loan.financial_account_id,
            statement_type="MORTGAGE",
            active_parser_name="test",
            active_parser_version="1",
            statement_date=date(2021, 6, 22),
        )
        session.add(document)
        session.add(statement)
        session.flush()
        # Old unsettled period from 2021
        session.add(MortgagePeriod(
            id="mp_2021_old",
            statement_id=statement.id,
            loan_account_id=loan.id,
            payment_due_date=date(2021, 7, 1),
            statement_date=date(2021, 6, 22),
            principal_billed_cents=50_000,
            interest_billed_cents=50_000,
            escrow_billed_cents=20_000,
            total_amount_billed_cents=120_000,
            outstanding_principal_cents=loan.original_principal_cents,
            interest_rate_bps=3125,
            status="BILLED",
        ))
        # Upcoming unsettled period
        session.add(MortgagePeriod(
            id="mp_future_period",
            statement_id=statement.id,
            loan_account_id=loan.id,
            payment_due_date=date(2028, 11, 1),
            statement_date=date(2028, 10, 1),
            principal_billed_cents=60_000,
            interest_billed_cents=40_000,
            escrow_billed_cents=20_000,
            total_amount_billed_cents=120_000,
            outstanding_principal_cents=loan.original_principal_cents - 100_000,
            interest_rate_bps=3125,
            status="BILLED",
        ))
        session.commit()

    page = signed_in.client.get("/properties/prop_tinsley_huntley")
    assert page.status_code == 200
    # Must NOT show the 2021 past-due payment
    assert "Jul 01, 2021 · past due" not in page.text
    # Must show the upcoming due date
    assert "Nov 01, 2028" in page.text
    assert "past due" not in page.text


def test_edit_loan_terms_modal_and_update_from_property_page(owner):
    signed_in = owner(reference_portfolio=True)
    property_id = "prop_tinsley_huntley"

    with Session(signed_in.engine) as session:
        loan = session.exec(
            select(LoanAccount).where(LoanAccount.property_id == property_id)
        ).one()
        loan_id = loan.id
        loan.origination_date = None
        session.add(loan)
        session.commit()

    # Initial view: origination date is not recorded
    page = signed_in.client.get(f"/properties/{property_id}")
    assert page.status_code == 200
    assert "Edit loan terms" in page.text
    assert "edit-loan-modal" in page.text
    assert "Start month not recorded" in page.text

    # Submit updated terms with origination date and return_to
    res = signed_in.post(
        f"/mortgages/{loan_id}/edit",
        data={
            "property_id": property_id,
            "return_to": f"/properties/{property_id}",
            "institution_name": "Rocket Mortgage",
            "account_name": "Rocket Primary Servicing",
            "loan_number": "1350010708",
            "account_number_masked": "...0708",
            "origination_date": "2021-06-01",
            "term_months": "360",
            "interest_rate": "2.500",
            "original_principal": "240000.00",
            "rate_type": "FIXED",
        },
        follow_redirects=False,
    )
    assert res.status_code == 303
    assert res.headers["location"].startswith(f"/properties/{property_id}")

    # View property page again - origination date is now active
    page = signed_in.client.get(f"/properties/{property_id}")
    assert page.status_code == 200
    assert "Jun 01, 2021" in page.text
    assert "Start month not recorded" not in page.text
    assert "Month " in page.text



def test_equity_share_is_rendered_once_in_the_asset_equity_card(owner):
    signed_in = owner(reference_portfolio=True)
    page = signed_in.client.get("/properties/prop_tinsley_huntley")
    assert page.status_code == 200
    card = page.text.split("pt-overview-card--primary", 1)[1].split(
        "pt-overview-card--mortgage", 1
    )[0]
    assert card.count("<span>Equity share</span>") == 1
    assert "<dt>Equity share</dt>" not in card


def test_unverified_lease_badge_tracks_the_signed_contract(owner):
    signed_in = owner(reference_portfolio=True)
    page = signed_in.client.get("/properties/prop_tinsley_huntley")
    assert "UNVERIFIED LEASE" in page.text
    assert "TENANTED" not in page.text

    with Session(signed_in.engine) as session:
        document = Document(
            id="doc_lease_contract",
            file_sha256="lease-contract-hash",
            original_filename="lease.pdf",
            storage_path="/tmp/lease.pdf",
            file_size_bytes=1,
            document_type="LEASE_AGREEMENT",
            property_id="prop_tinsley_huntley",
        )
        session.add(document)
        session.flush()
        lease = session.exec(
            select(Lease).where(Lease.property_id == "prop_tinsley_huntley")
        ).one()
        lease.document_id = document.id
        session.add(lease)
        session.commit()

    page = signed_in.client.get("/properties/prop_tinsley_huntley")
    assert "TENANTED" in page.text
    assert "UNVERIFIED LEASE" not in page.text


def test_valuation_badge_uses_the_recorded_source(owner):
    signed_in = owner(reference_portfolio=True)
    page = signed_in.client.get("/properties/prop_tinsley_huntley")
    value_card = page.text.split("pt-overview-card--value", 1)[1].split("</article>", 1)[0]
    assert "ESTIMATED" in value_card
    assert "APPRAISED" not in value_card

    with Session(signed_in.engine) as session:
        valuation = session.exec(
            select(PropertyValuation).where(
                PropertyValuation.property_id == "prop_tinsley_huntley"
            )
        ).one()
        valuation.valuation_source = "APPRAISAL"
        session.add(valuation)
        session.commit()

    page = signed_in.client.get("/properties/prop_tinsley_huntley")
    value_card = page.text.split("pt-overview-card--value", 1)[1].split("</article>", 1)[0]
    assert "APPRAISED" in value_card
    assert "ESTIMATED" not in value_card


def test_cads_discloses_zero_recognized_debt_service(owner):
    signed_in = owner()
    signed_in.post("/properties/create", data=PROPERTY, follow_redirects=False)
    property_id = _property_id(signed_in)
    signed_in.post(
        "/mortgages/create",
        data={
            "property_id": property_id,
            "institution_name": "Test Lender",
            "account_name": "Test Loan",
            "loan_number": "TEST-CADS-1",
            "original_principal": "300000.00",
            "opening_principal": "300000.00",
            "interest_rate": "6.125",
            "rate_type": "FIXED",
            "term_months": "360",
            "origination_date": date.today().isoformat(),
        },
        follow_redirects=False,
    )

    page = signed_in.client.get(f"/properties/{property_id}")
    assert page.status_code == 200
    assert "$0.00 debt service settled" in page.text


def test_missing_basis_discloses_depreciation_and_styles_the_action(owner):
    signed_in = owner()
    payload = {
        **PROPERTY,
        "purchase_price": "",
        "land_basis": "",
        "building_basis": "",
    }
    signed_in.post("/properties/create", data=payload, follow_redirects=False)

    page = signed_in.client.get(f"/properties/{_property_id(signed_in)}")
    assert page.status_code == 200
    assert "Schedule E Line 18 depreciation is $0.00" in page.text
    marker = page.text.index("Add acquisition costs")
    button = page.text[page.text.rfind("<button", 0, marker):marker]
    assert "pt-btn--secondary" in button


def test_lease_progress_bar_tracks_the_lease_term(owner):
    signed_in = owner(reference_portfolio=True)
    today = date.today()

    start = today - timedelta(days=100)
    end = today + timedelta(days=100)
    with Session(signed_in.engine) as session:
        lease = session.exec(
            select(Lease).where(Lease.property_id == "prop_tinsley_huntley")
        ).one()
        lease.start_date = start
        lease.end_date = end
        session.add(lease)
        session.commit()

    total_months = (end.year - start.year) * 12 + end.month - start.month
    if end.day > start.day:
        total_months += 1
    months_elapsed = (today.year - start.year) * 12 + today.month - start.month
    if today.day < start.day:
        months_elapsed -= 1
    current_month = min(total_months, max(1, months_elapsed + 1))

    page = signed_in.client.get("/properties/prop_tinsley_huntley")
    assert page.status_code == 200
    assert "Lease progress" in page.text
    assert "50.0%" in page.text
    assert f"Month {current_month} of {total_months}" in page.text
    assert "100 days remaining" in page.text


def test_primary_residence_operating_statement_card_styling(owner):
    signed_in = owner()
    payload = {
        **PROPERTY,
        "name": "Owner Primary Home",
        "property_type": "PRIMARY_RESIDENCE",
    }
    signed_in.post("/properties/create", data=payload, follow_redirects=False)
    prop_id = _property_id(signed_in)

    page = signed_in.client.get(f"/properties/{prop_id}")
    assert page.status_code == 200
    assert "Owner-Occupied Primary Residence" in page.text
    assert "Owner-Occupied Primary Residence Policy" in page.text

    # Operating Statement section body should not have pt-section__body--flush for primary residence
    op_stmt_marker = page.text.index("Operating Statement (P&L)")
    op_stmt_block = page.text[op_stmt_marker:op_stmt_marker + 1000]
    assert "pt-section__body--flush" not in op_stmt_block
    assert "pt-section__body" in op_stmt_block

