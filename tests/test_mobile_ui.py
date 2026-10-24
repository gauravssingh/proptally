"""Tests for mobile-responsive web interface and PWA manifest."""

import pytest


@pytest.fixture
def client(owner):
    """A signed-in owner's client; every page below requires a session."""
    return owner(reference_portfolio=True).client


def test_manifest_json_endpoint(client):
    res = client.get("/manifest.json")
    assert res.status_code == 200
    data = res.json()
    assert data["name"] == "PropTally"
    assert data["short_name"] == "PropTally"
    assert data["display"] == "standalone"
    assert data["theme_color"] == "#020617"
    assert len(data["icons"]) > 0


def test_mobile_meta_tags_and_pwa_headers(client, parse_html):
    res = client.get("/")
    assert res.status_code == 200
    soup = parse_html(res)

    # Viewport fit cover for iOS notch/safe areas
    viewport = soup.find("meta", attrs={"name": "viewport"})
    assert viewport and "viewport-fit=cover" in viewport.get("content", "")

    # Standalone PWA theme tags
    theme_color = soup.find("meta", attrs={"name": "theme-color"})
    assert theme_color and theme_color.get("content") == "#020617"
    assert soup.find("meta", attrs={"name": "apple-mobile-web-app-capable", "content": "yes"}) is not None
    assert soup.find("meta", attrs={"name": "apple-mobile-web-app-title", "content": "PropTally"}) is not None
    assert soup.find("link", attrs={"rel": "manifest", "href": "/static/manifest.json"}) is not None


def test_mobile_navigation_components(client, parse_html):
    res = client.get("/")
    assert res.status_code == 200
    soup = parse_html(res)

    # 1. Mobile top app bar with door switcher and drawer trigger
    assert soup.find(id="mobileMenuBtn") is not None
    page_text = soup.get_text()
    assert "All properties (3)" in page_text
    assert "12307 Larkspur" in page_text
    assert "2718 Foxglove" in page_text
    assert "3345 Elderberry" in page_text

    # 2. Slide-over drawer
    assert soup.find(id="mobileDrawer") is not None
    assert soup.find(id="mobileDrawerClose") is not None
    assert soup.find(id="mobileDrawerOverlay") is not None

    # 3. Mobile bottom navigation bar with primary touch targets
    bottom_nav = soup.find(attrs={"aria-label": "Mobile Bottom Navigation"})
    assert bottom_nav is not None
    bottom_text = bottom_nav.get_text()
    for item in ["Overview", "Properties", "Mortgages", "Review", "Tax / Sched E"]:
        assert item in bottom_text


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


def test_document_vault_and_statements_fluid_containers(client, parse_html):
    # Document vault uses fluid full-width shell
    res_docs = client.get("/documents")
    assert res_docs.status_code == 200
    soup_docs = parse_html(res_docs)
    assert soup_docs.find(class_="pt-content--fluid") is not None
    assert soup_docs.find(class_="pt-explorer") is not None

    # Statement ingestion uses fluid full-width shell and has no hardcoded inline max-height
    res_stmts = client.get("/statements")
    assert res_stmts.status_code == 200
    soup_stmts = parse_html(res_stmts)
    assert soup_stmts.find(class_="pt-content--fluid") is not None
    assert soup_stmts.find(class_="pt-statements-workspace") is not None
    assert 'style="max-height: 28rem;"' not in res_stmts.text


def test_high_res_fluid_css_rules():
    from pathlib import Path
    css_path = Path(__file__).resolve().parent.parent / "src" / "proptally" / "web" / "static" / "css" / "components.css"
    content = css_path.read_text(encoding="utf-8")

    # Fluid container rule
    assert ".pt-content--fluid" in content

    # High-resolution fluid explorer column rules
    assert "min-width: 1440px" in content
    assert "min-width: 1920px" in content
    assert "min-width: 2560px" in content
    assert ".pt-explorer--railed.pt-explorer--inspecting" in content
    assert "clamp(" in content

    # High-resolution fluid statements workspace split rules
    assert ".pt-statements-workspace--split" in content
    assert "pt-evidence-list--selectable" in content

    # Full height utilization rules
    assert "100dvh" in content
    assert ".pt-explorer__main > .pt-table-wrap" in content
    assert ".pt-explorer__main .pt-table-scroll" in content
    assert ".pt-statements-workspace--split .pt-statement-table" in content

