"""The property detail page's three-card financial overview."""

from datetime import date

from sqlmodel import Session, select

from proptally.db.models import (
    Document,
    LoanAccount,
    MortgagePeriod,
    Property,
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
    assert "No servicer payments recorded yet" in page.text


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
