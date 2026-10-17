"""The document vault: finding, filing and adding documents.

The vault claims three things worth testing rather than asserting: that documents
can be reached by folder as well as by list, that a search finds a file wherever it
was filed, and that an upload says what happened to each file instead of reporting
one verdict for a batch. It also has to keep the property-upload path inside the
portfolio's own vault, which is where a second storage path used to escape.
"""

import re

import httpx
import pytest
from sqlmodel import Session, select
from starlette.testclient import TestClient

from proptally.db.models import Document
from proptally.db.system_models import TenantMembership, User
from proptally.web.app import create_app
from proptally.web.auth import hash_password

OWNER = {"email": "owner@example.com", "password": "owner-password-1", "display_name": "Owner", "portfolio_name": "Probe Portfolio"}
READER = {"email": "reader@example.com", "password": "reader-password-1", "display_name": "Reader"}

LEASE_PDF = b"%PDF-1.4\nlease agreement for the flat\n%%EOF\n"
POLICY_PDF = b"%PDF-1.4\ninsurance policy schedule\n%%EOF\n"
RECEIPT_PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDRdata"


@pytest.fixture(name="instance")
def instance_fixture(tmp_path, monkeypatch):
    monkeypatch.delenv("PROPTALLY_ALLOW_REGISTRATION", raising=False)
    monkeypatch.delenv("PROPTALLY_SECURE_COOKIES", raising=False)
    storage = tmp_path / "storage"
    app = create_app(system_db_url=f"sqlite:///{storage / 'system.db'}", storage_root=str(storage))
    app.state.storage_root = storage
    return app


def _csrf(client: TestClient) -> str:
    """The signed-in session's CSRF token, read from a rendered form."""
    match = re.search(r'name="csrf_token" value="([^"]+)"', client.get("/documents").text)
    assert match, "the vault must expose the session's CSRF token to its upload form"
    return match.group(1)


def _register(client: TestClient, payload: dict) -> None:
    client.get("/auth/login")
    response = client.post(
        "/auth/register",
        data={**payload, "csrf_token": client.cookies.get("pt_csrf")},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text[:400]
    # A 303 to the sign-in page means registration was closed: without this the
    # test would carry on as an anonymous visitor and fail somewhere confusing.
    assert response.headers["location"] != "/auth/login", "registration was refused"


def _sign_in(client: TestClient, email: str, password: str) -> None:
    client.get("/auth/login")
    response = client.post(
        "/auth/login",
        data={"email": email, "password": password, "csrf_token": client.cookies.get("pt_csrf")},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text[:400]


@pytest.fixture(name="owner")
def owner_fixture(instance):
    # The control-plane schema is created in the app's lifespan, so the client has
    # to be entered as a context manager before anything can be registered.
    with TestClient(instance) as client:
        _register(client, OWNER)
        yield client


@pytest.fixture(name="vault")
def vault_fixture(instance, owner):
    """The owner's portfolio with one property, so documents can be filed against it."""
    from proptally.db.models import Property

    tenant_id = _tenant_id(instance, "Probe Portfolio")
    engine = instance.state.tenant_manager.engine_for(tenant_id)
    with Session(engine) as session:
        session.add(Property(
            id="prop_probe", name="4821 Larkspur Ave", address_line1="4821 Larkspur Ave",
            city="Westbrook", zip_code="60191", property_type="RENTAL_INVESTMENT",
        ))
        session.commit()
    return {"client": owner, "app": instance, "tenant_id": tenant_id, "property_id": "prop_probe"}


def _tenant_id(app, name: str) -> str:
    from sqlalchemy import text

    with Session(app.state.control_engine) as session:
        rows = session.execute(text("SELECT id FROM tenants WHERE name = :n"), {"n": name}).all()
    assert rows, f"no portfolio named {name}"
    return rows[0][0]


def _upload(client: TestClient, files: list[tuple[str, bytes]], **fields) -> httpx.Response:
    payload = [("files", (name, body, "application/pdf")) for name, body in files]
    return client.post(
        "/documents/upload",
        data={"csrf_token": _csrf(client), **fields},
        files=payload,
        follow_redirects=True,
    )


def _document_count(app, tenant_id: str) -> int:
    engine = app.state.tenant_manager.engine_for(tenant_id)
    with Session(engine) as session:
        return len(session.exec(select(Document)).all())


# --- the list, the folders and the search --------------------------------


def test_the_vault_files_documents_by_property_and_reports_what_is_inside(vault):
    client, app = vault["client"], vault["app"]
    _upload(client, [("lease.pdf", LEASE_PDF)], document_type="LEASE_AGREEMENT", property_id=vault["property_id"])
    _upload(client, [("receipt.png", RECEIPT_PNG)], document_type="OTHER")

    html = client.get("/documents").text

    assert "4821 Larkspur Ave" in html          # the property folder
    assert "Unfiled" in html                    # the folder the second document landed in
    assert "lease.pdf" in html and "receipt.png" in html
    assert "2 documents" in html                # the summary line counts both
    assert _document_count(app, vault["tenant_id"]) == 2


def test_opening_a_folder_shows_what_is_inside_it(vault):
    """Folders are opened, not filtered: each level lists its subfolders and its files."""
    client = vault["client"]
    _upload(client, [("lease.pdf", LEASE_PDF)], document_type="LEASE_AGREEMENT", property_id=vault["property_id"])
    _upload(client, [("receipt.png", RECEIPT_PNG)], document_type="")   # unfiled and unclassified

    root = client.get("/documents").text
    assert "pt-tree__link is-active" in root                  # the vault itself is the open folder
    assert "4821 Larkspur Ave" in root and "Unfiled" in root  # the property level of the tree
    assert "pt-folders" in root                               # folders are what you open
    assert "lease.pdf" in root and "receipt.png" in root      # and their files are listed beside them

    # Inside the property: its own type folders, then its documents.
    inside = client.get(f"/documents?property={vault['property_id']}").text
    assert "Lease agreement" in inside        # the type folder inside this property
    assert "lease.pdf" in inside
    assert "receipt.png" not in inside        # that document lives in another folder
    assert "pt-breadcrumb__here" in inside and 'aria-current="page"' in inside
    assert "pt-tree__link is-active" in inside

    # Inside the type folder: the documents, with no folders left to open.
    leaf = client.get(f"/documents?property={vault['property_id']}&type=LEASE_AGREEMENT").text
    assert "lease.pdf" in leaf
    assert "pt-folders" not in leaf
    assert "1 document" in leaf                # the title bar counts what the folder holds

    # The other branch of the tree still holds the unfiled document.
    unfiled = client.get("/documents?property=unfiled").text
    assert "receipt.png" in unfiled and "lease.pdf" not in unfiled


def test_the_uploader_opens_on_demand_rather_than_sitting_on_the_page(vault):
    """The screen is a file browser: the uploader is behind the header action, not always open."""
    client = vault["client"]
    _upload(client, [("lease.pdf", LEASE_PDF)], document_type="LEASE_AGREEMENT", property_id=vault["property_id"])

    page = client.get("/documents").text

    # The header offers it, the dialog is closed until it is used, and the form is inside it.
    assert 'data-modal-open="upload-documents"' in page
    dialog_at = page.index('id="upload-documents" class="pt-modal" hidden')
    form_at = page.index('action="/documents/upload"')
    assert form_at > dialog_at, "the upload form must live inside the dialog"
    assert page.count('action="/documents/upload"') == 1, "exactly one upload form"
    assert 'id="add-documents"' not in page, "the permanent upload panel is gone"

    # A form that posts must carry its own token: reading one from anywhere on the page
    # passes while the browser's own upload is refused.
    upload_form = page[form_at:page.index("</form>", form_at)]
    assert 'name="csrf_token"' in upload_form, "the upload form must carry the session's token"


def test_search_finds_a_file_wherever_it_was_filed(vault):
    client = vault["client"]
    _upload(client, [("lease.pdf", LEASE_PDF)], document_type="LEASE_AGREEMENT", property_id=vault["property_id"])
    _upload(client, [("policy.pdf", POLICY_PDF)], document_type="INSURANCE_POLICY")

    # Searching from inside a folder still looks everywhere: the search is the finder and
    # it leaves the folder behind rather than hiding matches that live elsewhere.
    html = client.get("/documents?property=unfiled&q=policy").text
    assert "policy.pdf" in html
    assert "lease.pdf" not in html
    assert "search “policy”" in html            # the path says what you are looking at

    # The folder tree answers "where is it?" by counting the matches in their folder,
    # the root row included, so the tree never claims more than the search found.
    assert "Insurance policy" in html
    assert re.search(r'Whole vault</span>\s*<span class="pt-tree__count">1</span>', html)

    # No match at all is an empty state, not an error.
    empty = client.get("/documents?q=zzzz").text
    assert "Nothing here matches" in empty


def test_an_uploaded_image_is_stored_and_described_as_an_image(vault):
    """The media type follows the file: a PNG is not a PDF that happens to be named .png."""
    client, app = vault["client"], vault["app"]
    _upload(client, [("receipt.png", RECEIPT_PNG)], document_type="OTHER",
            property_id=vault["property_id"])

    engine = app.state.tenant_manager.engine_for(vault["tenant_id"])
    with Session(engine) as session:
        document = session.exec(select(Document)).one()
    assert document.mime_type == "image/png"

    # Serving it: rendered in place, because that is what "view" means; downloading it
    # keeps the portfolio's own filename.
    viewed = client.get(f"/documents/{document.id}/view")
    assert viewed.headers["content-type"].startswith("image/png")
    assert "attachment" not in viewed.headers.get("content-disposition", "")
    assert "attachment" in client.get(f"/documents/{document.id}/download").headers["content-disposition"]

    page = client.get("/documents").text
    assert "PNG" in page            # the file is listed as the kind of file it is
    assert "Images" in page         # and the folder offers the chip for it


def test_pagination_walks_the_documents(vault):
    client = vault["client"]
    _upload(client, [("one.pdf", LEASE_PDF), ("two.pdf", POLICY_PDF), ("three.png", RECEIPT_PNG)])

    first = client.get("/documents?per_page=2").text
    assert "Page 1 of 2" in first
    assert "Showing <strong>1</strong>–<strong>2</strong> of <strong>3</strong>" in first

    second = client.get("/documents?per_page=2&page=2").text
    assert "Page 2 of 2" in second
    assert "Showing <strong>3</strong>–<strong>3</strong> of <strong>3</strong>" in second


# --- adding documents ----------------------------------------------------


def test_upload_stores_several_files_and_reports_each_outcome(vault):
    client, app = vault["client"], vault["app"]

    response = _upload(client, [("lease.pdf", LEASE_PDF), ("policy.pdf", POLICY_PDF)])

    assert response.status_code == 200
    assert "2 stored" in response.text
    assert "lease.pdf" in response.text and "policy.pdf" in response.text
    assert _document_count(app, vault["tenant_id"]) == 2

    # The same bytes again: recognised, reported, and not stored a second time.
    again = _upload(client, [("lease-again.pdf", LEASE_PDF)])
    assert "already in the vault" in again.text
    assert _document_count(app, vault["tenant_id"]) == 2


def test_upload_keeps_the_files_it_can_and_names_the_one_it_cannot(vault):
    client, app = vault["client"], vault["app"]

    response = client.post(
        "/documents/upload",
        data={"csrf_token": _csrf(client)},
        files=[
            ("files", ("lease.pdf", LEASE_PDF, "application/pdf")),
            ("files", ("notes.csv", b"a,b\n1,2\n", "text/csv")),
        ],
        follow_redirects=True,
    )

    assert "1 stored" in response.text
    assert "notes.csv" in response.text                 # the rejected file is named
    assert "only PDF and image documents can be stored" in response.text
    assert _document_count(app, vault["tenant_id"]) == 1


def test_re_uploading_known_content_can_still_file_it(vault):
    client = vault["client"]
    _upload(client, [("lease.pdf", LEASE_PDF)], document_type="")

    # The same bytes, now with the door and the type chosen: the upload is what an
    # owner does when they realise a document should have been filed somewhere.
    again = _upload(client, [("lease.pdf", LEASE_PDF)], document_type="LEASE_AGREEMENT",
                    property_id=vault["property_id"])
    assert "already in the vault" in again.text

    engine = vault["app"].state.tenant_manager.engine_for(vault["tenant_id"])
    with Session(engine) as session:
        documents = session.exec(select(Document)).all()
    assert len(documents) == 1, "the content must not be stored twice"
    assert documents[0].property_id == vault["property_id"]
    assert documents[0].document_type == "LEASE_AGREEMENT"


def test_an_upload_with_no_files_says_so(vault):
    client, app = vault["client"], vault["app"]
    response = client.post("/documents/upload", data={"csrf_token": _csrf(client)}, follow_redirects=True)
    assert "Choose at least one file" in response.text
    assert _document_count(app, vault["tenant_id"]) == 0


def test_a_read_only_member_can_look_but_not_add(vault):
    app = vault["app"]
    # A read-only member of the same portfolio, created the way an invitation would.
    with Session(app.state.control_engine) as session:
        session.add(User(
            id="usr_reader", email=READER["email"],
            password_hash=hash_password(READER["password"]), display_name=READER["display_name"],
        ))
        session.flush()
        session.add(TenantMembership(
            id="mem_reader", user_id="usr_reader", tenant_id=vault["tenant_id"], role="READ_ONLY",
        ))
        session.commit()

    with TestClient(app) as reader:
        _sign_in(reader, READER["email"], READER["password"])

        assert reader.get("/documents").status_code == 200
        listed = _upload(reader, [("sneaky.pdf", LEASE_PDF)], follow_redirects=False)
        assert listed.status_code == 403, "a read-only member must not be able to store documents"
        assert _document_count(app, vault["tenant_id"]) == 0


# --- opening and copying a file ------------------------------------------


def test_a_document_downloads_with_its_own_name_and_bytes(vault):
    client = vault["client"]
    _upload(client, [("lease.pdf", LEASE_PDF)])

    document_id = _only_document_id(vault)
    view = client.get(f"/documents/{document_id}/view")
    assert view.status_code == 200
    assert view.content == LEASE_PDF

    download = client.get(f"/documents/{document_id}/download")
    assert download.status_code == 200
    assert download.content == LEASE_PDF
    assert "lease.pdf" in download.headers["content-disposition"]


def test_another_portfolios_document_is_not_reachable_by_id(vault, monkeypatch):
    client, app = vault["client"], vault["app"]
    _upload(client, [("lease.pdf", LEASE_PDF)])
    document_id = _only_document_id(vault)

    # A second portfolio on the same instance. Its owner is registered, because a
    # portfolio created behind the account's back would not have a session to test.
    monkeypatch.setenv("PROPTALLY_ALLOW_REGISTRATION", "true")
    with TestClient(app) as other:
        _register(other, {"email": "other@example.com", "password": "other-password-1",
                          "display_name": "Other", "portfolio_name": "Other Portfolio"})

        # A signed-in owner of another portfolio reaches none of these by id.
        assert other.get(f"/documents/{document_id}/view", follow_redirects=False).status_code == 404
        assert other.get(f"/documents/{document_id}/download", follow_redirects=False).status_code == 404
        assert "lease.pdf" not in other.get("/documents").text

    # An anonymous visitor is sent to sign in rather than served the file.
    with TestClient(app) as anonymous:
        assert anonymous.get(f"/documents/{document_id}/view", follow_redirects=False).status_code in (303, 307)


def test_the_property_upload_lands_in_the_portfolios_own_vault(vault):
    """The second storage path used to write outside the tenant's vault."""
    client, app = vault["client"], vault["app"]

    response = client.post(
        f"/properties/{vault['property_id']}/documents/upload",
        data={"csrf_token": _csrf(client), "document_type": "LEASE_AGREEMENT"},
        files={"file": ("lease.pdf", LEASE_PDF, "application/pdf")},
        follow_redirects=False,
    )
    assert response.status_code == 303

    document_id = _only_document_id(vault)
    engine = app.state.tenant_manager.engine_for(vault["tenant_id"])
    with Session(engine) as session:
        stored_path = session.get(Document, document_id).storage_path

    expected_vault = app.state.storage_root / "tenants" / vault["tenant_id"] / "vault"
    assert str(expected_vault) in stored_path, f"stored outside the portfolio vault: {stored_path}"


def _only_document_id(vault) -> str:
    engine = vault["app"].state.tenant_manager.engine_for(vault["tenant_id"])
    with Session(engine) as session:
        documents = session.exec(select(Document)).all()
    assert len(documents) == 1, f"expected one document, found {len(documents)}"
    return documents[0].id
