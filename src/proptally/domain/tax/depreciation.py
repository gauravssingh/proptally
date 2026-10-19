"""MACRS 27.5-year straight line depreciation service for residential rental real estate."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

from proptally.db.models import Property


@dataclass
class DepreciationResult:
    property_id: str
    property_name: str
    property_type: str
    is_depreciable: bool
    purchase_price_cents: Optional[int]
    land_basis_cents: Optional[int]
    building_basis_cents: Optional[int]
    depreciation_method: str
    useful_life_years: float
    tax_year: int
    depreciation_cents: int
    notes: str


def calculate_annual_depreciation(
    prop: Property,
    tax_year: int,
) -> DepreciationResult:
    """Computes annual MACRS depreciation for a single property and tax year."""
    if prop.property_type != "RENTAL_INVESTMENT":
        return DepreciationResult(
            property_id=prop.id,
            property_name=prop.name,
            property_type=prop.property_type,
            is_depreciable=False,
            purchase_price_cents=prop.purchase_price_cents,
            land_basis_cents=prop.land_basis_cents,
            building_basis_cents=prop.building_basis_cents,
            depreciation_method="NONE",
            useful_life_years=0.0,
            tax_year=tax_year,
            depreciation_cents=0,
            notes="Primary residence is not eligible for MACRS rental depreciation (IRC §168).",
        )

    building_basis = prop.building_basis_cents or 0
    if building_basis <= 0:
        return DepreciationResult(
            property_id=prop.id,
            property_name=prop.name,
            property_type=prop.property_type,
            is_depreciable=True,
            purchase_price_cents=prop.purchase_price_cents,
            land_basis_cents=prop.land_basis_cents,
            building_basis_cents=0,
            depreciation_method="MACRS_27_5_SL",
            useful_life_years=27.5,
            tax_year=tax_year,
            depreciation_cents=0,
            notes="Depreciable building basis is not recorded. Awaiting closing disclosure / ALTA statement.",
        )

    # Check acquisition date for mid-month convention in year of acquisition
    acq_date = prop.acquisition_date
    if acq_date and acq_date.year > tax_year:
        # Not yet placed in service
        return DepreciationResult(
            property_id=prop.id,
            property_name=prop.name,
            property_type=prop.property_type,
            is_depreciable=True,
            purchase_price_cents=prop.purchase_price_cents,
            land_basis_cents=prop.land_basis_cents,
            building_basis_cents=building_basis,
            depreciation_method="MACRS_27_5_SL",
            useful_life_years=27.5,
            tax_year=tax_year,
            depreciation_cents=0,
            notes=f"Property acquired {acq_date.isoformat()} after tax year {tax_year}.",
        )

    # Full-year depreciation: basis / 27.5, in exact decimal cents so a .5
    # boundary can never be changed by binary floating point.
    full_year_annual = int(
        (Decimal(building_basis) / Decimal("27.5")).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    )

    if acq_date and acq_date.year == tax_year:
        # IRS Mid-Month convention: month of acquisition counts as 0.5 month.
        # Months in service = 12 - month + 0.5
        months_in_service = Decimal(12 - acq_date.month) + Decimal("0.5")
        deprec_cents = int(
            (Decimal(full_year_annual) * months_in_service / Decimal(12)).quantize(
                Decimal("1"), rounding=ROUND_HALF_UP
            )
        )
        note = f"First year mid-month convention ({months_in_service:.1f} months in service)."
    else:
        deprec_cents = full_year_annual
        note = "Full year MACRS 27.5-year straight line."

    return DepreciationResult(
        property_id=prop.id,
        property_name=prop.name,
        property_type=prop.property_type,
        is_depreciable=True,
        purchase_price_cents=prop.purchase_price_cents,
        land_basis_cents=prop.land_basis_cents,
        building_basis_cents=building_basis,
        depreciation_method="MACRS_27_5_SL",
        useful_life_years=27.5,
        tax_year=tax_year,
        depreciation_cents=deprec_cents,
        notes=note,
    )
