"""Tests for mobile-responsive web interface and PWA manifest."""

import pytest
from starlette.testclient import TestClient

from proptally.web.app import create_app


@pytest.fixture
def client(tmp_path):
    db_file = tmp_path / "test_mobile.db"
    app = create_app(f"sqlite:///{db_file}")
    with TestClient(app) as test_client:
        yield test_client


def test_manifest_json_endpoint(client):
    res = client.get("/manifest.json")
    assert res.status_code == 200
    data = res.json()
    assert data["name"] == "PropTally"
    assert data["short_name"] == "PropTally"
    assert data["display"] == "standalone"
    assert data["theme_color"] == "#020617"
    assert len(data["icons"]) > 0


def test_mobile_meta_tags_and_pwa_headers(client):
    res = client.get("/")
    assert res.status_code == 200
    html = res.text

    # Viewport fit cover for iOS notch/safe areas
    assert "viewport-fit=cover" in html
    # Standalone PWA theme tags
    assert '<meta name="theme-color" content="#020617">' in html
    assert '<meta name="apple-mobile-web-app-capable" content="yes">' in html
    assert '<meta name="apple-mobile-web-app-title" content="PropTally">' in html
    assert '<link rel="manifest" href="/static/manifest.json">' in html


def test_mobile_navigation_components(client):
    res = client.get("/")
    assert res.status_code == 200
    html = res.text

    # 1. Mobile top app bar with door switcher and drawer trigger
    assert 'id="mobileMenuBtn"' in html
    assert "All Doors (3)" in html
    assert "12307 Larkspur" in html
    assert "2718 Foxglove" in html
    assert "3345 Elderberry" in html

    # 2. Slide-over drawer
    assert 'id="mobileDrawer"' in html
    assert 'id="mobileDrawerClose"' in html
    assert 'id="mobileDrawerOverlay"' in html

    # 3. Mobile bottom navigation bar with primary touch targets
    assert 'aria-label="Mobile Bottom Navigation"' in html
    assert "Overview" in html
    assert "Doors" in html
    assert "Mortgages" in html
    assert "Review" in html
    assert "Tax / Sched E" in html


def test_mobile_endpoints_health(client):
    endpoints = [
        "/",
        "/mortgages",
        "/reconciliation",
        "/tax/schedule-e",
        "/properties/prop_tinsley_huntley",
        "/properties/prop_cimmaron_pingree",
        "/properties/prop_burkey_algonquin",
        "/static/icons/icon.svg",
    ]
    for endpoint in endpoints:
        res = client.get(endpoint)
        assert res.status_code == 200, f"Failed at {endpoint}: status {res.status_code}"
