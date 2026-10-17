"""Property location persistence and validation."""

import re
from datetime import timedelta

from sqlalchemy import text
from sqlmodel import Session, select
from starlette.testclient import TestClient

from proptally.db.connection import get_engine
from proptally.db.models import Property, utc_now
from proptally.db.schema import (
    COLUMN_MIGRATIONS,
    SCHEMA_MIGRATIONS,
    _migrate_columns,
)
from proptally.db.system_models import ROLE_READ_ONLY, TenantMembership, User
from proptally.web.auth import cookie_name, hash_password, issue_session
from proptally.web.property_forms import (
    expire_stale_coordinates,
    fresh_coordinates,
    google_maps_url,
)

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
}


def _create(signed_in, **overrides):
    return signed_in.post(
        "/properties/create",
        data={**PROPERTY_FORM, **overrides},
        follow_redirects=False,
    )


def test_create_with_google_location(owner):
    signed_in = owner()
    response = _create(
        signed_in,
        google_place_id="ChIJ_test_place",
        latitude="30.2672",
        longitude="-97.7431",
        address_source="GOOGLE",
    )
    assert response.status_code == 303
    assert "error=" not in response.headers["location"]

    with Session(signed_in.engine) as session:
        prop = session.exec(select(Property)).one()
        assert prop.google_place_id == "ChIJ_test_place"
        assert prop.latitude == 30.2672
        assert prop.longitude == -97.7431
        assert prop.address_source == "GOOGLE"
        assert prop.location_cached_at is not None
        assert fresh_coordinates(prop) == (30.2672, -97.7431)


def test_create_without_google_location_is_manual(owner):
    signed_in = owner()
    response = _create(signed_in)
    assert response.status_code == 303

    with Session(signed_in.engine) as session:
        prop = session.exec(select(Property)).one()
        assert prop.google_place_id is None
        assert prop.latitude is None
        assert prop.longitude is None
        assert prop.address_source == "MANUAL"


def _property_id(signed_in) -> str:
    with Session(signed_in.engine) as session:
        return session.exec(select(Property)).one().id


def test_editing_the_address_clears_a_stale_place(owner):
    signed_in = owner()
    _create(
        signed_in,
        google_place_id="ChIJ_test_place",
        latitude="30.2672",
        longitude="-97.7431",
        address_source="GOOGLE",
    )
    property_id = _property_id(signed_in)

    response = signed_in.post(
        f"/properties/{property_id}/edit",
        data={
            **PROPERTY_FORM,
            "address_line1": "1 New Street",
            "google_place_id": "ChIJ_test_place",
            "latitude": "30.2672",
            "longitude": "-97.7431",
            "address_source": "GOOGLE",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "error=" not in response.headers["location"]

    with Session(signed_in.engine) as session:
        prop = session.get(Property, property_id)
        assert prop.google_place_id is None
        assert prop.latitude is None
        assert prop.longitude is None
        assert prop.address_source == "MANUAL"


def test_address_only_edit_clears_the_pin(owner):
    signed_in = owner()
    _create(
        signed_in,
        google_place_id="ChIJ_test_place",
        latitude="30.2672",
        longitude="-97.7431",
        address_source="GOOGLE",
    )
    property_id = _property_id(signed_in)

    response = signed_in.post(
        f"/properties/{property_id}/edit",
        data={**PROPERTY_FORM, "address_line1": "1 New Street"},
        follow_redirects=False,
    )
    assert response.status_code == 303

    with Session(signed_in.engine) as session:
        prop = session.get(Property, property_id)
        assert prop.google_place_id is None
        assert prop.address_source == "MANUAL"


def test_clearing_a_location_keeps_the_address(owner):
    signed_in = owner()
    _create(
        signed_in,
        google_place_id="ChIJ_test_place",
        latitude="30.2672",
        longitude="-97.7431",
        address_source="GOOGLE",
    )
    property_id = _property_id(signed_in)

    response = signed_in.post(
        f"/properties/{property_id}/edit",
        data={
            **PROPERTY_FORM,
            "google_place_id": "",
            "clear_location": "1",
            "address_source": "MANUAL",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    with Session(signed_in.engine) as session:
        prop = session.get(Property, property_id)
        assert prop.address_line1 == PROPERTY_FORM["address_line1"]
        assert prop.google_place_id is None
        assert prop.address_source == "MANUAL"


def test_notes_only_update_preserves_the_location(owner):
    signed_in = owner()
    _create(
        signed_in,
        google_place_id="ChIJ_test_place",
        latitude="30.2672",
        longitude="-97.7431",
        address_source="GOOGLE",
    )
    property_id = _property_id(signed_in)

    response = signed_in.post(
        f"/properties/{property_id}/update-details",
        data={"description": "Roof replaced in 2025."},
        follow_redirects=False,
    )
    assert response.status_code == 303

    with Session(signed_in.engine) as session:
        prop = session.get(Property, property_id)
        assert prop.description == "Roof replaced in 2025."
        assert prop.google_place_id == "ChIJ_test_place"


def test_one_sided_coordinates_are_refused(owner):
    signed_in = owner()
    response = _create(signed_in, latitude="30.2672")
    assert response.status_code == 303
    assert "error=" in response.headers["location"]

    with Session(signed_in.engine) as session:
        assert session.exec(select(Property)).all() == []


def test_settings_renders_manual_fields_without_a_key(owner):
    signed_in = owner()
    html = signed_in.client.get("/settings").text
    assert "data-location-picker" in html
    assert 'name="address_line1"' in html
    assert "Address search is off" in html
    assert "maps/api/js" not in html


def test_settings_renders_search_when_a_key_is_configured(monkeypatch, owner):
    monkeypatch.setenv("PROPTALLY_GOOGLE_MAPS_BROWSER_KEY", "test-browser-key")
    signed_in = owner()
    html = signed_in.client.get("/settings").text
    assert 'data-maps-key="test-browser-key"' in html
    assert "data-location-search" in html
    assert "property-location.js" in html


def test_detail_page_renders_the_embed_and_place_link(monkeypatch, owner):
    monkeypatch.setenv("PROPTALLY_GOOGLE_MAPS_BROWSER_KEY", "test-browser-key")
    signed_in = owner()
    _create(
        signed_in,
        google_place_id="ChIJ_test_place",
        latitude="30.2672",
        longitude="-97.7431",
        address_source="GOOGLE",
    )
    html = signed_in.client.get(f"/properties/{_property_id(signed_in)}").text
    assert "https://www.google.com/maps/embed/v1/place" in html
    assert "place_id:ChIJ_test_place" in html
    assert "query_place_id=ChIJ_test_place" in html


def test_manual_detail_page_has_no_embed(monkeypatch, owner):
    monkeypatch.setenv("PROPTALLY_GOOGLE_MAPS_BROWSER_KEY", "test-browser-key")
    signed_in = owner()
    _create(signed_in)
    html = signed_in.client.get(f"/properties/{_property_id(signed_in)}").text
    assert "maps/embed/v1/place" not in html
    assert "No pinned location" in html


def test_google_maps_url_prefers_the_place_id():
    prop = Property(
        id="p",
        name="P",
        address_line1="1 Main St",
        city="Austin",
        state="TX",
        zip_code="78701",
        google_place_id="ChIJ_test_place",
    )
    url = google_maps_url(prop)
    assert "query_place_id=ChIJ_test_place" in url


def test_expired_coordinates_are_cleared(engine):
    with Session(engine) as session:
        prop = Property(
            id="prop_stale",
            name="Stale",
            address_line1="1 Main St",
            city="Austin",
            state="TX",
            zip_code="78701",
            google_place_id="ChIJ_test_place",
            latitude=1.0,
            longitude=2.0,
            location_cached_at=utc_now() - timedelta(days=31),
        )
        session.add(prop)
        session.commit()

        assert expire_stale_coordinates(session) == 1
        session.commit()
        session.refresh(prop)
        assert prop.latitude is None
        assert prop.longitude is None
        assert prop.location_cached_at is None
        assert prop.google_place_id == "ChIJ_test_place"


def test_location_columns_are_registered(engine):
    versions = {version for version, _, _ in SCHEMA_MIGRATIONS}
    assert 105 in versions
    columns = {(table, column) for table, column, _ in COLUMN_MIGRATIONS}
    assert ("properties", "google_place_id") in columns
    with engine.connect() as connection:
        names = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(properties)")}
    assert {"google_place_id", "address_source", "latitude", "longitude", "location_cached_at"} <= names


def _read_only_client(signed_in) -> TestClient:
    with Session(signed_in.control_engine) as session:
        user = User(
            id="usr_readonly_location",
            email="readonly-location@example.com",
            password_hash=hash_password("read-only-password-1"),
            display_name="Read Only",
        )
        session.add(user)
        session.flush()
        session.add(TenantMembership(
            id="mem_readonly_location",
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


def test_read_only_member_cannot_write_a_location(owner):
    signed_in = owner()
    read_only = _read_only_client(signed_in)
    token = re.search(r'name="csrf_token" value="([^"]+)"', read_only.get("/account").text)
    assert token

    response = read_only.post(
        "/properties/create",
        data={
            **PROPERTY_FORM,
            "google_place_id": "ChIJ_test_place",
            "latitude": "30.2672",
            "longitude": "-97.7431",
            "csrf_token": token.group(1),
        },
        follow_redirects=False,
    )
    assert response.status_code == 403
    with Session(signed_in.engine) as session:
        assert session.exec(select(Property)).all() == []


def test_location_stays_in_the_tenant_database(owner):
    signed_in = owner()
    _create(
        signed_in,
        google_place_id="ChIJ_test_place",
        latitude="30.2672",
        longitude="-97.7431",
        address_source="GOOGLE",
    )
    with Session(signed_in.engine) as session:
        prop = session.exec(select(Property)).one()
        assert prop.google_place_id == "ChIJ_test_place"

    with signed_in.control_engine.connect() as connection:
        tables = {
            row[0]
            for row in connection.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    assert "properties" not in tables


def test_migration_adds_location_columns_to_an_existing_table(tmp_path):
    engine = get_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE properties (id VARCHAR PRIMARY KEY, name VARCHAR)"))
        _migrate_columns(connection)
        names = {
            row[1]
            for row in connection.execute(text("PRAGMA table_info(properties)"))
        }
    assert {
        "google_place_id",
        "address_source",
        "latitude",
        "longitude",
        "location_cached_at",
    } <= names


def test_coordinates_need_a_cache_timestamp():
    prop = Property(
        id="p",
        name="P",
        address_line1="A",
        city="C",
        zip_code="Z",
        latitude=1.0,
        longitude=2.0,
    )
    assert fresh_coordinates(prop) is None
