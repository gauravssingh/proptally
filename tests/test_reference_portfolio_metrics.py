"""Financial metrics measured on the synthetic reference portfolio.

The reference portfolio lives in the test suite (tests/reference_portfolio.py) because
the application ships no sample data: a tenant starts empty and is built by its owner.
This keeps a whole-portfolio check that the hand-computed figures in
tests/test_custom_portfolio.py complement.
"""

from sqlmodel import Session, select
from proptally.db.models import Property, LoanAccount, Transaction, TransactionProcessingState
from reference_portfolio import seed_reference_portfolio
from proptally.domain.accounting.metrics import calculate_portfolio_equity, calculate_property_financials


def test_reference_portfolio_metrics(session: Session):
    """The reference portfolio's entities are created and its metrics compute."""
    seed_reference_portfolio(session)

    # 1. Properties
    props = session.exec(select(Property)).all()
    assert len(props) == 3
    prop_ids = {p.id for p in props}
    assert "prop_tinsley_huntley" in prop_ids
    assert "prop_cimmaron_pingree" in prop_ids
    assert "prop_burkey_algonquin" in prop_ids

    # 2. Portfolio Equity
    equity = calculate_portfolio_equity(session)
    assert equity["total_valuation_cents"] == (46500000 + 39500000 + 71000000)  # $1,570,000
    assert equity["total_debt_cents"] > 0
    assert equity["net_equity_cents"] == equity["total_valuation_cents"] - equity["total_debt_cents"]

    # 3. Rental Financials
    fin_alder = calculate_property_financials(session, property_id="prop_tinsley_huntley")
    assert fin_alder["gross_rental_income_cents"] == 260000  # $2,600 rent
    assert fin_alder["repairs_cents"] == 25000              # $250 Home Depot repairs
    assert fin_alder["noi_cents"] == (260000 - 25000)       # $2,350 NOI
    assert fin_alder["mortgage_interest_cents"] == 66012    # $660.12 interest
    assert fin_alder["principal_paid_cents"] == 62000       # $620 principal
    assert fin_alder["escrow_funded_cents"] == 33800        # $338 escrow

    # 4. Review queue states (CBD-1 and CBD-2)
    atm_state = session.get(TransactionProcessingState, "txn_atm_deposit_cbd1_20260320")
    assert atm_state.allocation_status == "UNALLOCATED"
    assert atm_state.residual_cents == 150000

    apple_state = session.get(TransactionProcessingState, "txn_apple_store_cbd2_20260322")
    assert apple_state.allocation_status == "UNALLOCATED"
    assert apple_state.residual_cents == 12900
