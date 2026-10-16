"""A brand-new tenant: empty, and expected to work.

The application ships no sample data, so every new portfolio starts with nothing but
the standard chart of accounts and the portfolio-independent vendor rules. Everything a
person meets on their first visit therefore has to cope with an empty portfolio, and
this module is where that is proven rather than assumed:

* every screen renders without a portfolio to render;
* the figures are zero rather than absent, and notably not an error;
* nothing synthetic is present (no doors, transactions, documents or portfolio-specific
  rules), which is the property that makes it a live deployment rather than a demo;
* and the first real workflow — a door, its accounts, a mortgage, opening balances and
  the first posted rent — carries through to a Schedule E figure.
"""



from sqlmodel import Session, select

from proptally.db.models import (
    JournalEntryLine,
    Document,
    FinancialAccount,
    JournalEntry,
    LedgerAccount,
    LoanAccount,
    Property,
    Statement,
    Transaction,
    VendorRule,
)
from proptally.domain.accounting.coa import STANDARD_ACCOUNTS
from proptally.domain.accounting.metrics import (
    calculate_portfolio_equity,
    calculate_property_financials,
)
from proptally.domain.tax.schedule_e import generate_schedule_e_workpaper
from proptally.domain.transactions.suggest import DEFAULT_VENDOR_RULES

#: Every screen a signed-in owner can reach.
SCREENS = (
    "/",
    "/portfolios",
    "/settings",
    "/mortgages",
    "/transactions",
    "/reconciliation",
    "/tax/schedule-e",
    "/statements",
    "/account",
)


def test_every_screen_renders_for_a_brand_new_tenant(owner):
    """An empty portfolio must not break a single screen."""
    tenant = owner()
    for path in SCREENS:
        response = tenant.client.get(path)
        assert response.status_code == 200, f"{path} returned {response.status_code}"


def test_a_brand_new_tenant_holds_nothing_synthetic(owner):
    """No sample data of any kind: this is a live portfolio, not a demonstration."""
    tenant = owner()

    with Session(tenant.engine) as session:
        for model in (Property, FinancialAccount, LoanAccount, Transaction, Statement, Document, JournalEntry):
            assert session.exec(select(model)).all() == [], f"{model.__name__} should be empty"

        # The chart of accounts is exactly the portfolio-invariant one: an account per
        # entity is created only when the owner declares that entity.
        codes = {row.code for row in session.exec(select(LedgerAccount)).all()}
        assert codes == {spec["code"] for spec in STANDARD_ACCOUNTS}

        # Only the generic merchant rules are present, never a portfolio-specific one.
        rules = {row.id for row in session.exec(select(VendorRule)).all()}
        assert rules == {rule["id"] for rule in DEFAULT_VENDOR_RULES}


def test_empty_figures_are_zero_rather_than_errors(owner):
    """Zero is the honest answer for a portfolio with nothing in it."""
    tenant = owner()

    with Session(tenant.engine) as session:
        financials = calculate_property_financials(session)
        assert financials["rental_metrics_applicable"] is False
        for key in ("goi_cents", "noi_cents", "ocf_cents", "cads_cents", "total_debt_service_cents"):
            assert financials[key] == 0, key

        equity = calculate_portfolio_equity(session)
        assert equity["total_valuation_cents"] == 0
        assert equity["total_debt_cents"] == 0
        assert equity["net_equity_cents"] == 0
        assert equity["property_metrics"] == []

        workpaper = generate_schedule_e_workpaper(session, tax_year=2026)
        assert workpaper.columns == []
        assert workpaper.total_rents == 0
        assert workpaper.total_expenses == 0


def test_the_dashboard_offers_setup_and_nothing_else(owner):
    """The only thing an empty portfolio should invite is setting it up."""
    tenant = owner()
    dashboard = tenant.client.get("/")

    assert dashboard.status_code == 200
    assert "Set up your portfolio" in dashboard.text
    assert 'href="/settings"' in dashboard.text
    # No synthetic content on offer, and no portfolio figures to mislead with.
    assert "sample-portfolio" not in dashboard.text
    assert "Portfolio Financial Overview" not in dashboard.text

    settings = tenant.client.get("/settings")
    assert settings.status_code == 200
    assert tenant.client.get("/settings").text.count("name=") > 0, "the setup forms must render"


def _post_form(tenant, path: str, data: dict, *, follow_redirects: bool = False):
    """Posts a form the way the browser does, with the session's CSRF token."""
    return tenant.post(path, data, follow_redirects=follow_redirects)


def test_the_first_workflow_produces_a_reportable_portfolio(owner):
    """A door, its accounts, a mortgage, opening balances and the first posted rent.

    This is the journey a new owner takes, driven through the same forms they use, and
    it ends at a Schedule E figure rather than at "the request succeeded".
    """
    tenant = owner()

    # 1. The first door.
    created = _post_form(tenant, "/properties/create", {
        "name": "First Duplex",
        "address_line1": "1 First St",
        "city": "Austin",
        "state": "TX",
        "zip_code": "78701",
        "property_type": "RENTAL_INVESTMENT",
        "acquisition_date": "2024-01-15",
        "purchase_price": "400000.00",
        "land_basis": "80000.00",
        "building_basis": "320000.00",
    })
    assert created.status_code == 303 and "error=" not in created.headers["location"], created.headers["location"]

    with Session(tenant.engine) as session:
        prop = session.exec(select(Property)).one()
        assert prop.asset_ledger_code is not None, "the door owns a ledger account"

    # 2. Its operating account and a purpose-limited rental card.
    for payload in (
        {"institution_name": "Lone Star Bank", "account_name": "Operating Checking",
         "account_type": "CHECKING", "account_number_masked": "...1111",
         "mixed_use_status": "UNCLASSIFIED"},
        {"institution_name": "Cedar Bank", "account_name": "Rental Card",
         "account_type": "CREDIT_CARD", "account_number_masked": "...2222",
         "mixed_use_status": "DEDICATED_RENTAL"},
    ):
        response = _post_form(tenant, "/accounts/create", payload)
        assert response.status_code == 303 and "error=" not in response.headers["location"], response.headers["location"]

    with Session(tenant.engine) as session:
        checking = session.exec(
            select(FinancialAccount).where(FinancialAccount.account_type == "CHECKING")
        ).one()
        assert checking.ledger_account_code is not None

    # 3. A mortgage on the door.
    mortgage = _post_form(tenant, "/mortgages/create", {
        "property_id": prop.id,
        "institution_name": "Spruce Mortgage",
        "account_name": "Spruce Loan",
        "loan_number": "SP-1",
        "original_principal": "350000.00",
        "opening_principal": "350000.00",
        "escrow_opening_balance": "2000.00",
        "interest_rate": "3.125",
        "term_months": "360",
    })
    assert mortgage.status_code == 303 and "error=" not in mortgage.headers["location"], mortgage.headers["location"]

    with Session(tenant.engine) as session:
        loan = session.exec(select(LoanAccount)).one()
        assert (loan.principal_ledger_code, loan.clearing_ledger_code, loan.escrow_ledger_code) != (None, None, None)

    # 4. The dated baseline covers the door's cost basis and the cash on hand. The
    #    loan's own principal and escrow were stated on the loan (step 3) and are
    #    deliberately not offered here, so neither figure can be counted twice.
    opening = _post_form(tenant, "/opening-balances", {
        "as_of": "2026-01-01",
        f"asset_{prop.id}": "400000.00",
        f"amount_{checking.id}": "5000.00",
    })
    assert opening.status_code == 303 and "error=" not in opening.headers["location"], opening.headers["location"]

    # The dashboard now reports a real portfolio rather than the setup panel.
    dashboard = tenant.client.get("/")
    assert dashboard.status_code == 200
    assert "Portfolio Financial Overview" in dashboard.text
    assert "Set up your portfolio" not in dashboard.text

    with Session(tenant.engine) as session:
        equity = calculate_portfolio_equity(session)
        assert equity["net_equity_cents"] == 400_000_00 - 350_000_00
        assert equity["debt_fully_verified"] is True

    # 5. The first rent: entered manually, allocated to the door, and posted.
    manual = _post_form(tenant, "/transactions/manual", {
        "account_id": checking.id,
        "transaction_date": "2026-02-01",
        "description": "RENT DEPOSIT FEBRUARY",
        "amount_dollars": "2500.00",
        "direction": "INFLOW",
    })
    assert manual.status_code == 303

    with Session(tenant.engine) as session:
        rent = session.exec(
            select(Transaction).where(Transaction.amount_cents == 250_000)
        ).one()

    split = _post_form(tenant, "/transactions/split", {
        "transaction_id": rent.id,
        "property_id[]": prop.id,
        "category[]": "RENT_INCOME",
        "amount_dollars[]": "2500.00",
        "post_to_ledger": "true",
        "client_confirmed": "true",
    })
    assert split.status_code == 303

    with Session(tenant.engine) as session:
        posted = session.exec(
            select(JournalEntry).where(JournalEntry.status == "POSTED")
        ).all()
        assert posted, "the rent must reach the ledger"

        financials = calculate_property_financials(session, year=2026)
        assert financials["gross_rental_income_cents"] == 250_000
        assert financials["cash_rent_collected_cents"] == 250_000

        workpaper = generate_schedule_e_workpaper(session, tax_year=2026)
        column = next(c for c in workpaper.columns if c.property_id == prop.id)
        assert column.line_3_rents == 250_000
        assert workpaper.total_rents == 250_000

    # 6. And it is visible as posted on the register.
    register = tenant.client.get("/transactions")
    assert register.status_code == 200
    assert "RENT DEPOSIT FEBRUARY" in register.text


def test_one_figure_has_one_home_so_a_balance_cannot_be_counted_twice(owner):
    """The mortgage balance is stated on the loan, and only there.

    The loan screen records the loan's opening principal and escrow reserve; the dated
    baseline records the portfolio's cost basis and cash. Offering the principal in both
    places meant an owner who filled in both reported twice the debt they owed, which is
    silent and looks like a data problem rather than a form problem.
    """
    tenant = owner()
    _post_form(tenant, "/properties/create", {
        "name": "One Home Duplex", "address_line1": "1 Home St", "city": "Austin",
        "state": "TX", "zip_code": "78701", "property_type": "RENTAL_INVESTMENT",
        "acquisition_date": "2024-01-15", "purchase_price": "400000.00",
        "land_basis": "80000.00", "building_basis": "320000.00",
    })
    with Session(tenant.engine) as session:
        prop = session.exec(select(Property)).one()

    _post_form(tenant, "/accounts/create", {
        "institution_name": "Lone Star Bank", "account_name": "Operating",
        "account_type": "CHECKING", "account_number_masked": "...3333",
        "mixed_use_status": "UNCLASSIFIED",
    })
    _post_form(tenant, "/mortgages/create", {
        "property_id": prop.id, "institution_name": "Spruce Mortgage",
        "account_name": "Spruce Loan", "loan_number": "SP-9",
        "original_principal": "300000.00", "opening_principal": "300000.00",
        "escrow_opening_balance": "1500.00", "interest_rate": "3.5", "term_months": "360",
    })

    settings = tenant.client.get("/settings").text
    # The baseline form does not offer the loan's principal or escrow at all.
    with Session(tenant.engine) as session:
        loan = session.exec(select(LoanAccount)).one()
    assert f"principal_{loan.id}" not in settings
    assert f"escrow_{loan.id}" not in settings

    # The debt is the principal, once, and the escrow reserve is on the ledger too.
    with Session(tenant.engine) as session:
        equity = calculate_portfolio_equity(session)
        assert equity["total_debt_cents"] == 300_000_00

        escrow_lines = session.exec(
            select(JournalEntryLine).where(
                JournalEntryLine.ledger_account_code == loan.escrow_ledger_code
            )
        ).all()
        assert sum(line.debit_cents for line in escrow_lines) == 1_500_00

    # A dated baseline for cash does not disturb the loan balance.
    with Session(tenant.engine) as session:
        checking = session.exec(
            select(FinancialAccount).where(FinancialAccount.account_type == "CHECKING")
        ).one()
    _post_form(tenant, "/opening-balances", {
        "as_of": "2026-01-01", f"amount_{checking.id}": "5000.00",
    })
    with Session(tenant.engine) as session:
        assert calculate_portfolio_equity(session)["total_debt_cents"] == 300_000_00
