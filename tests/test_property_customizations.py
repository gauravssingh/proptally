"""Tests for property personalization: narratives, Google Maps links, and photo management."""

import io

import pytest
from sqlmodel import Session, select

from proptally.db.connection import get_engine
from proptally.db.models import Property, PropertyPhoto
from proptally.web.routes.properties import get_google_maps_url


@pytest.fixture
def custom_env(tmp_path, monkeypatch, owner):
    db_file = tmp_path / "test_customizations.db"
    db_url = f"sqlite:///{db_file}"
    signed_in = owner(db_url=db_url, reference_portfolio=True)
    # Photos live in the portfolio's own store, not a process-wide directory.
    photos_dir = signed_in.app.state.tenant_manager.paths(signed_in.tenant_id).photos
    yield signed_in, db_url, photos_dir


def test_google_maps_url_helper():
    prop = Property(
        id="prop_test",
        name="Test Villa",
        address_line1="123 Ocean Drive",
        address_line2="Suite 4B",
        city="Miami",
        state="FL",
        zip_code="33139",
    )
    # Default auto-detection
    url = get_google_maps_url(prop)
    assert url.startswith("https://www.google.com/maps/search/?api=1&query=")
    assert "123+Ocean+Drive" in url or "123%20Ocean%20Drive" in url or "123+Ocean+Drive" in url
    assert "Miami" in url

    # Custom override
    prop.google_maps_url = "https://maps.google.com/?cid=987654321"
    assert get_google_maps_url(prop) == "https://maps.google.com/?cid=987654321"


def test_property_page_renders_maps_and_narrative_sections(custom_env):
    signed_in, _, _ = custom_env
    res = signed_in.client.get("/properties/prop_tinsley_huntley")
    assert res.status_code == 200
    html = res.text

    assert "Google Maps" in html
    assert "https://www.google.com/maps/search/?api=1" in html
    assert "Property Notes" in html
    assert "Property Photos" in html
    assert "id=\"edit-details-modal\"" in html
    assert "id=\"upload-photo-modal\"" in html
    assert "id=\"lightbox-modal\"" in html


def test_update_property_narrative_and_maps_url(custom_env):
    signed_in, db_url, _ = custom_env

    # 1. Update narrative and custom maps URL
    custom_story = "Acquired in 2021 as a cornerstone rental asset with strong cash flows."
    custom_map = "https://maps.google.com/?q=Custom+Westbrook+Pin"

    res = signed_in.post(
        "/properties/prop_tinsley_huntley/update-details",
        data={"description": custom_story, "google_maps_url": custom_map},
        follow_redirects=True,
    )
    assert res.status_code == 200
    assert custom_story in res.text
    assert custom_map in res.text

    # Verify DB persistence
    engine = get_engine(db_url)
    with Session(engine) as session:
        prop = session.get(Property, "prop_tinsley_huntley")
        assert prop.description == custom_story
        assert prop.google_maps_url == custom_map

    # 2. Reset back to empty/auto-detect
    res = signed_in.post(
        "/properties/prop_tinsley_huntley/update-details",
        data={"description": "", "google_maps_url": ""},
        follow_redirects=True,
    )
    assert res.status_code == 200
    with Session(engine) as session:
        prop = session.get(Property, "prop_tinsley_huntley")
        assert prop.description is None
        assert prop.google_maps_url is None


def test_photo_upload_cover_and_delete_lifecycle(custom_env):
    signed_in, db_url, photos_dir = custom_env
    prop_id = "prop_tinsley_huntley"

    # 1. Upload first photo (should automatically become cover)
    img_data_1 = b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"fake_jpeg_data_1"
    res1 = signed_in.post(
        f"/properties/{prop_id}/photos/upload",
        files={"photo": ("front_elevation.jpg", io.BytesIO(img_data_1), "image/jpeg")},
        data={"caption": "Front Elevation 2024", "is_cover": "false"},
        follow_redirects=True,
    )
    assert res1.status_code == 200
    assert "Front Elevation 2024" in res1.text
    assert "Cover Photo" in res1.text

    engine = get_engine(db_url)
    with Session(engine) as session:
        photos = session.exec(select(PropertyPhoto).where(PropertyPhoto.property_id == prop_id)).all()
        assert len(photos) == 1
        photo1 = photos[0]
        assert photo1.is_cover is True
        assert photo1.caption == "Front Elevation 2024"
        photo1_id = photo1.id

        # Verify file exists on local disk
        stored_file = photos_dir / prop_id / photo1.storage_path.split("/")[-1]
        assert stored_file.exists()
        assert stored_file.read_bytes() == img_data_1

    # 2. Verify serving via /photos static mount
    res_static = signed_in.client.get(photo1.storage_path)
    assert res_static.status_code == 200
    assert res_static.content == img_data_1

    # 3. Upload second photo as cover
    img_data_2 = b"\x89PNG\r\n\x1a\n" + b"fake_png_data_2"
    res2 = signed_in.post(
        f"/properties/{prop_id}/photos/upload",
        files={"photo": ("renovated_kitchen.png", io.BytesIO(img_data_2), "image/png")},
        data={"caption": "Renovated Kitchen", "is_cover": "true"},
        follow_redirects=True,
    )
    assert res2.status_code == 200
    assert "Renovated Kitchen" in res2.text

    with Session(engine) as session:
        p1 = session.get(PropertyPhoto, photo1_id)
        assert p1.is_cover is False
        photos = session.exec(select(PropertyPhoto).where(PropertyPhoto.property_id == prop_id)).all()
        assert len(photos) == 2
        p2 = next(p for p in photos if p.id != photo1_id)
        assert p2.is_cover is True
        assert p2.caption == "Renovated Kitchen"
        photo2_id = p2.id

    # 4. Switch cover photo back to photo 1
    res_cover = signed_in.post(
        f"/properties/{prop_id}/photos/{photo1_id}/cover",
        data={},
        follow_redirects=True,
    )
    assert res_cover.status_code == 200
    with Session(engine) as session:
        p1 = session.get(PropertyPhoto, photo1_id)
        p2 = session.get(PropertyPhoto, photo2_id)
        assert p1.is_cover is True
        assert p2.is_cover is False

    # 5. Delete cover photo 1 (photo 2 should now promote to cover)
    res_del = signed_in.post(
        f"/properties/{prop_id}/photos/{photo1_id}/delete",
        data={},
        follow_redirects=True,
    )
    assert res_del.status_code == 200
    with Session(engine) as session:
        photos_remaining = session.exec(select(PropertyPhoto).where(PropertyPhoto.property_id == prop_id)).all()
        assert len(photos_remaining) == 1
        assert photos_remaining[0].id == photo2_id
        assert photos_remaining[0].is_cover is True

    # Check photo 1 file was deleted from disk
    stored_file1 = photos_dir / prop_id / f"{photo1_id}.jpg"
    assert not stored_file1.exists()


def test_photo_upload_disallows_invalid_extensions(custom_env):
    signed_in, _, _ = custom_env
    res = signed_in.post(
        "/properties/prop_tinsley_huntley/photos/upload",
        files={"photo": ("malicious_script.sh", io.BytesIO(b"echo 123"), "text/plain")},
        data={"caption": "Bad file"},
    )
    assert res.status_code == 400
    assert "Only image files are permitted" in res.json()["detail"]


def test_property_document_upload_and_vaulting(custom_env, tmp_path, monkeypatch):
    from proptally.db.models import Document, Lease

    vault_dir = tmp_path / "vault_docs"
    vault_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("PROPTALLY_VAULT_DIR", str(vault_dir))

    signed_in, db_uri, _ = custom_env
    engine = get_engine(db_uri)
    prop_id = "prop_tinsley_huntley"

    fake_pdf = b"%PDF-1.4 test lease content with Tyler Saxsma contract details"
    res_upload = signed_in.post(
        f"/properties/{prop_id}/documents/upload",
        files={"file": ("Tyler_Saxsma_Lease_2026.pdf", io.BytesIO(fake_pdf), "application/pdf")},
        data={
            "document_type": "LEASE_AGREEMENT",
            "notes": "Executed 2026 Residential Lease Agreement",
            "tenant_name": "Tyler J. Saxsma",
            "monthly_rent_dollars": "2600.00",
            "security_deposit_dollars": "2000.00",
            "start_date": "2026-01-01",
            "end_date": "2026-12-31",
        },
        follow_redirects=True,
    )
    assert res_upload.status_code == 200
    assert "Tyler_Saxsma_Lease_2026.pdf" in res_upload.text
    assert "Signed Lease Contract Vaulted &amp; Verified" in res_upload.text or "Signed Lease Contract Vaulted & Verified" in res_upload.text

    # Verify DB records
    with Session(engine) as session:
        doc = session.exec(select(Document).where(Document.original_filename == "Tyler_Saxsma_Lease_2026.pdf")).first()
        assert doc is not None
        assert doc.property_id == prop_id
        assert doc.document_type == "LEASE_AGREEMENT"
        doc_id = doc.id

        lease = session.exec(select(Lease).where(Lease.property_id == prop_id, Lease.is_active == True)).first()
        assert lease is not None
        assert lease.tenant_name == "Tyler J. Saxsma"
        assert lease.monthly_rent_cents == 260000
        assert lease.security_deposit_cents == 200000
        assert lease.document_id == doc.id

    # Test view and download endpoints
    res_view = signed_in.client.get(f"/documents/{doc_id}/view")
    assert res_view.status_code == 200
    assert res_view.headers["content-type"] == "application/pdf"
    assert res_view.content == fake_pdf

    res_dl = signed_in.client.get(f"/documents/{doc_id}/download")
    assert res_dl.status_code == 200
    assert res_dl.content == fake_pdf


def test_property_lease_update_endpoint(custom_env):
    from proptally.db.models import Lease

    signed_in, db_uri, _ = custom_env
    engine = get_engine(db_uri)
    prop_id = "prop_cimmaron_pingree"

    res_lease = signed_in.post(
        f"/properties/{prop_id}/lease/update",
        data={
            "tenant_name": "Riley Smith",
            "monthly_rent_dollars": "2100.00",
            "security_deposit_dollars": "2100.00",
            "start_date": "2026-02-01",
            "end_date": "2027-01-31",
            "is_active": "true",
        },
        follow_redirects=True,
    )
    assert res_lease.status_code == 200
    assert "Riley Smith" in res_lease.text

    with Session(engine) as session:
        lease = session.exec(select(Lease).where(Lease.property_id == prop_id, Lease.is_active == True)).first()
        assert lease is not None
        assert lease.tenant_name == "Riley Smith"
        assert lease.monthly_rent_cents == 210000

