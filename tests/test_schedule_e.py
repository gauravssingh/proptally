"""Tests for IRS Form 1040 Schedule E tax workpaper engine and depreciation."""

from datetime import date
from fastapi.testclient import TestClient
from sqlmodel import Session

from proptally.db.connection import get_engine
from proptally.db.models import Property
from proptally.db.schema import init_schema
from proptally.domain.accounting.coa import seed_chart_of_accounts
from proptally.domain.tax.depreciation import calculate_annual_depreciation
from proptally.domain.tax.schedule_e import export_schedule_e_csv, generate_schedule_e_workpaper
from proptally.web.app import create_app


def test_macrs_27_5_depreciation():
    # 1. Rental property with building basis $340,000 (Alder)
    prop_rental = Property(
        id="prop_alder",
        name="12307 Larkspur St",
        address_line1="12307 Larkspur St",
        city="Westbrook",
        zip_code="60196",
        property_type="RENTAL_INVESTMENT",
        acquisition_date=date(2021, 6, 15),
        purchase_price_cents=42500000,
        land_basis_cents=8500000,
        building_basis_cents=34000000,  # $340,000
    )

    deprec_2026 = calculate_annual_depreciation(prop_rental, 2026)
    assert deprec_2026.is_depreciable is True
    # $340,000 / 27.5 = $12,363.6363... -> $12,363.64 (1236364 cents)
    assert deprec_2026.depreciation_cents == 1236364

    # 2. Mid-month convention in acquisition year (acquired in June = month 6)
    # Months in service = 12 - 6 + 0.5 = 6.5 months
    # 1236364 * (6.5 / 12) = 669697 cents ($6,696.97)
    deprec_2021 = calculate_annual_depreciation(prop_rental, 2021)
    assert deprec_2021.depreciation_cents == 669697

    # 3. Primary residence (Rowan) is non-depreciable
    prop_primary = Property(
        id="prop_rowan",
        name="3345 Elderberry",
        address_line1="3345 Elderberry",
        city="Cedar Grove",
        zip_code="60198",
        property_type="PRIMARY_RESIDENCE",
        building_basis_cents=52000000,
    )
    deprec_primary = calculate_annual_depreciation(prop_primary, 2026)
    assert deprec_primary.is_depreciable is False
    assert deprec_primary.depreciation_cents == 0


def test_schedule_e_workpaper_and_web_endpoints(tmp_path):
    db_file = tmp_path / "test_tax.db"
    db_uri = f"sqlite:///{db_file}"
    engine = get_engine(db_uri)
    init_schema(engine)

    with Session(engine) as session:
        seed_chart_of_accounts(session)
        larkspur = Property(
            id="prop_tinsley_huntley",
            name="12307 Larkspur St",
            address_line1="12307 Larkspur St",
            city="Westbrook",
            zip_code="60196",
            property_type="RENTAL_INVESTMENT",
            acquisition_date=date(2021, 6, 15),
            purchase_price_cents=42500000,
            land_basis_cents=8500000,
            building_basis_cents=34000000,
        )
        elderberry = Property(
            id="prop_burkey_algonquin",
            name="3345 Elderberry",
            address_line1="3345 Elderberry",
            city="Cedar Grove",
            zip_code="60198",
            property_type="PRIMARY_RESIDENCE",
            building_basis_cents=52000000,
        )
        session.add(larkspur)
        session.add(elderberry)
        session.commit()

        # Generate workpaper
        wp = generate_schedule_e_workpaper(session, tax_year=2026)
        assert len(wp.columns) == 2
        alder_col = [c for c in wp.columns if c.property_id == "prop_tinsley_huntley"][0]
        rowan_col = [c for c in wp.columns if c.property_id == "prop_burkey_algonquin"][0]

        assert alder_col.line_20_depreciation == 1236364  # $12,363.64
        assert rowan_col.line_20_depreciation == 0          # $0 (Primary)
        assert wp.total_expenses >= 1236364

        # Test CSV export
        csv_text = export_schedule_e_csv(wp)
        assert "IRS Line Item,Line Description" in csv_text
        assert "Line 20,Depreciation (27.5-Yr MACRS)" in csv_text
        assert "12363.64" in csv_text

    # Web endpoints
    app = create_app(db_uri)
    client = TestClient(app)

    resp_html = client.get("/tax/schedule-e?year=2026")
    assert resp_html.status_code == 200
    assert "IRS Schedule E Tax Workpaper" in resp_html.text
    assert "12307 Larkspur St" in resp_html.text
    assert "Line 20" in resp_html.text

    resp_csv = client.get("/tax/schedule-e/csv?year=2026")
    assert resp_csv.status_code == 200
    assert resp_csv.headers["content-type"] == "text/csv; charset=utf-8"
    assert "Schedule_E_2026_Workpaper.csv" in resp_csv.headers["content-disposition"]
