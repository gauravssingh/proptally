"""Duplicate entry guards for the manual onboarding data."""

from sqlmodel import Session, select

from proptally.db.models import FinancialAccount, Property, VendorRule

PROPERTY = {
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
}

ACCOUNT = {
    "institution_name": "Lone Star Bank",
    "account_name": "Operating Checking",
    "account_type": "CHECKING",
    "account_number_masked": "...1111",
    "mixed_use_status": "UNCLASSIFIED",
}


def test_duplicate_property_address_is_refused(owner):
    signed_in = owner()
    first = signed_in.post("/properties/create", data=PROPERTY, follow_redirects=False)
    assert first.status_code == 303
    assert "error=" not in first.headers["location"]

    second = signed_in.post(
        "/properties/create",
        data={**PROPERTY, "name": "Bluebonnet Duplicate"},
        follow_redirects=False,
    )
    assert second.status_code == 303
    assert "error=" in second.headers["location"]

    with Session(signed_in.engine) as session:
        assert len(session.exec(select(Property)).all()) == 1


def test_same_address_with_a_different_unit_is_allowed(owner):
    signed_in = owner()
    signed_in.post("/properties/create", data=PROPERTY, follow_redirects=False)
    unit = signed_in.post(
        "/properties/create",
        data={
            **PROPERTY,
            "name": "Bluebonnet Unit B",
            "address_line2": "Unit B",
        },
        follow_redirects=False,
    )
    assert unit.status_code == 303
    assert "error=" not in unit.headers["location"]
    with Session(signed_in.engine) as session:
        assert len(session.exec(select(Property)).all()) == 2


def test_duplicate_account_number_is_refused(owner):
    signed_in = owner()
    first = signed_in.post("/accounts/create", data=ACCOUNT, follow_redirects=False)
    assert first.status_code == 303
    assert "error=" not in first.headers["location"]

    second = signed_in.post(
        "/accounts/create",
        data={**ACCOUNT, "account_name": "Operating Checking 2"},
        follow_redirects=False,
    )
    assert second.status_code == 303
    assert "error=" in second.headers["location"]

    with Session(signed_in.engine) as session:
        assert len(session.exec(select(FinancialAccount)).all()) == 1


def test_duplicate_account_name_without_a_number_is_refused(owner):
    signed_in = owner()
    payload = {**ACCOUNT, "account_number_masked": ""}
    signed_in.post("/accounts/create", data=payload, follow_redirects=False)
    second = signed_in.post("/accounts/create", data=payload, follow_redirects=False)
    assert second.status_code == 303
    assert "error=" in second.headers["location"]


def test_a_different_account_number_is_allowed(owner):
    signed_in = owner()
    signed_in.post("/accounts/create", data=ACCOUNT, follow_redirects=False)
    second = signed_in.post(
        "/accounts/create",
        data={**ACCOUNT, "account_name": "Reserve Checking", "account_number_masked": "...2222"},
        follow_redirects=False,
    )
    assert second.status_code == 303
    assert "error=" not in second.headers["location"]
    with Session(signed_in.engine) as session:
        assert len(session.exec(select(FinancialAccount)).all()) == 2


def test_duplicate_vendor_rule_pattern_is_refused(owner):
    signed_in = owner()
    rule = {
        "pattern": "ACME PROPERTY",
        "vendor_name": "Acme",
        "service_type": "Management",
        "default_category": "MANAGEMENT",
        "default_property_id": "",
    }
    first = signed_in.post("/vendor-rules/create", data=rule, follow_redirects=False)
    assert first.status_code == 303
    assert "error=" not in first.headers["location"]

    second = signed_in.post(
        "/vendor-rules/create",
        data={**rule, "pattern": "acme property"},
        follow_redirects=False,
    )
    assert second.status_code == 303
    assert "error=" in second.headers["location"]

    with Session(signed_in.engine) as session:
        matches = [
            rule
            for rule in session.exec(select(VendorRule)).all()
            if rule.pattern.casefold() == "acme property"
        ]
        assert len(matches) == 1
