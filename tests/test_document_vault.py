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

from proptally.db.models import Document, FinancialAccount, FinancialInstitution, Statement
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
    assert "lease.pdf" in root and "receipt.png" in root      # and their files are listed beside them
    assert "pt-table__parent-row" not in root                 # root is top-level: no .. parent row

    # Inside the property: its own type folders, then its documents.
    inside = client.get(f"/documents?property={vault['property_id']}").text
    assert "Lease agreement" in inside        # the type folder inside this property
    assert "lease.pdf" in inside
    assert "receipt.png" not in inside        # that document lives in another folder
    assert "pt-breadcrumb__here" in inside and 'aria-current="page"' in inside
    assert "pt-tree__link is-active" in inside
    assert "pt-table__parent-row" in inside and ".." in inside  # .. row to move up to root

    # Inside the type folder: the documents, with .. row to move up to property.
    leaf = client.get(f"/documents?property={vault['property_id']}&type=LEASE_AGREEMENT").text
    assert "lease.pdf" in leaf
    assert "pt-table__parent-row" in leaf and ".." in leaf      # .. row to move up to property
    assert "1 document" in leaf                # the title bar counts what the folder holds

    # The other branch of the tree still holds the unfiled document.
    unfiled = client.get("/documents?property=unfiled").text
    assert "receipt.png" in unfiled and "lease.pdf" not in unfiled
    assert "pt-table__parent-row" in unfiled and ".." in unfiled


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


def test_simplified_document_listing_and_no_glance_panel(vault):
    client = vault["client"]
    _upload(client, [("lease.pdf", LEASE_PDF), ("receipt.png", RECEIPT_PNG)],
            document_type="LEASE_AGREEMENT", property_id=vault["property_id"])

    # 1. Without a selection: Vault at a glance panel is gone, giving 2-pane spacious view
    res = client.get("/documents")
    assert res.status_code == 200
    html = res.text
    assert "Vault at a glance" not in html
    assert "Which folder holds what" not in html

    # 2. Simplified columns are present
    assert ">Document<" in html
    assert ">Location<" in html
    assert ">Uploaded<" in html
    assert ">Size<" in html
    assert ">Status<" in html
    # Type is removed as a column
    assert "<th>Type</th>" not in html and ">Type<</th>" not in html

    # 3. Row clickability and checkboxes
    assert 'data-row-click="true"' in html
    assert 'data-doc-checkbox' in html
    assert 'id="doc-select-all"' in html
    assert 'id="doc-bulk-bar"' in html
    assert 'id="col-visibility-menu"' in html

    # 4. When a document is selected: inspector opens for it
    engine = vault["app"].state.tenant_manager.engine_for(vault["tenant_id"])
    with Session(engine) as session:
        doc = session.exec(select(Document).where(Document.original_filename == "lease.pdf")).first()
    assert doc is not None

    selected_res = client.get(f"/documents?doc={doc.id}")
    assert selected_res.status_code == 200
    assert "Preview of lease.pdf" in selected_res.text or f"/documents/{doc.id}/view" in selected_res.text
    assert "pt-explorer__aside pt-inspector" in selected_res.text


def test_bulk_download_documents(vault):
    import io
    import zipfile

    client = vault["client"]
    _upload(client, [("first.pdf", LEASE_PDF), ("second.pdf", POLICY_PDF)],
            document_type="LEASE_AGREEMENT", property_id=vault["property_id"])

    engine = vault["app"].state.tenant_manager.engine_for(vault["tenant_id"])
    with Session(engine) as session:
        docs = session.exec(select(Document)).all()
    assert len(docs) >= 2
    doc_ids = ",".join(d.id for d in docs)

    # POST bulk download
    resp = client.post("/documents/bulk/download", data={"doc_ids": doc_ids, "csrf_token": _csrf(client)})
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"
    assert "documents_export.zip" in resp.headers.get("content-disposition", "")

    # Inspect zip contents
    with zipfile.ZipFile(io.BytesIO(resp.content), "r") as zf:
        namelist = zf.namelist()
        assert "first.pdf" in namelist
        assert "second.pdf" in namelist
        assert zf.read("first.pdf") == LEASE_PDF
        assert zf.read("second.pdf") == POLICY_PDF


def test_sorting_columns_and_direction(vault):
    client = vault["client"]
    _upload(client, [("alpha.pdf", LEASE_PDF), ("beta.pdf", POLICY_PDF)],
            document_type="LEASE_AGREEMENT", property_id=vault["property_id"])

    # Sort by document name ascending & descending
    res_asc = client.get("/documents?sort=name&dir=asc")
    assert res_asc.status_code == 200
    pos_alpha = res_asc.text.find("alpha.pdf")
    pos_beta = res_asc.text.find("beta.pdf")
    assert pos_alpha != -1 and pos_beta != -1
    assert pos_alpha < pos_beta

    res_desc = client.get("/documents?sort=name&dir=desc")
    assert res_desc.status_code == 200
    pos_alpha_desc = res_desc.text.find("alpha.pdf")
    pos_beta_desc = res_desc.text.find("beta.pdf")
    assert pos_beta_desc < pos_alpha_desc

    # Sort by location and status
    res_loc = client.get("/documents?sort=location&dir=asc")
    assert res_loc.status_code == 200
    res_status = client.get("/documents?sort=status&dir=desc")
    assert res_status.status_code == 200


def test_inspector_navigation_and_resizer(vault):
    client = vault["client"]
    DOC_C = b"%PDF-1.4\nthird distinct document content\n%%EOF\n"
    _upload(client, [("doc_a.pdf", LEASE_PDF), ("doc_b.pdf", POLICY_PDF), ("doc_c.pdf", DOC_C)],
            document_type="LEASE_AGREEMENT", property_id=vault["property_id"])

    engine = vault["app"].state.tenant_manager.engine_for(vault["tenant_id"])
    with Session(engine) as session:
        docs = session.exec(select(Document).order_by(Document.original_filename)).all()
    assert len(docs) >= 3
    doc_a = next(d for d in docs if d.original_filename == "doc_a.pdf")
    doc_b = next(d for d in docs if d.original_filename == "doc_b.pdf")
    doc_c = next(d for d in docs if d.original_filename == "doc_c.pdf")

    # Select middle document on page 2 with per_page=1, sorting by name asc
    res = client.get(f"/documents?sort=name&dir=asc&per_page=1&page=2&doc={doc_b.id}")
    assert res.status_code == 200
    html = res.text

    # Resizer element present
    assert 'id="explorer-resizer"' in html
    assert 'class="pt-explorer__resizer"' in html

    # Position counter
    assert 'id="inspector-position"' in html
    # Previous and Next buttons with proper hrefs
    assert f"doc={doc_a.id}" in html
    assert f"doc={doc_c.id}" in html
    assert 'id="inspector-prev-btn"' in html
    assert 'id="inspector-next-btn"' in html

    # Close button preserves page, per_page, and sorting
    assert 'id="inspector-close-btn"' in html
    assert "page=2" in html
    assert "sort=name" in html

    # Column preferences: reset button & required Document column
    assert 'id="col-reset-btn"' in html
    assert "(Required)" in html


def test_bulk_download_all_matching_scope(vault):
    import io
    import zipfile

    client = vault["client"]
    _upload(client, [("matching_1.pdf", LEASE_PDF), ("matching_2.pdf", POLICY_PDF)],
            document_type="LEASE_AGREEMENT", property_id=vault["property_id"])

    resp = client.post(
        "/documents/bulk/download",
        data={
            "scope": "all_matching",
            "csrf_token": _csrf(client),
            "kind": "all",
        },
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"
    assert "documents_export.zip" in resp.headers.get("content-disposition", "")

    with zipfile.ZipFile(io.BytesIO(resp.content), "r") as zf:
        names = zf.namelist()
        assert "matching_1.pdf" in names
        assert "matching_2.pdf" in names


def test_dedicated_financial_accounts_section_in_vault(vault):
    import io
    import zipfile
    from datetime import date

    client = vault["client"]
    engine = vault["app"].state.tenant_manager.engine_for(vault["tenant_id"])

    # 1. Setup Financial Accounts
    with Session(engine) as session:
        inst = FinancialInstitution(id="inst_bofa", name="Bank of America")
        session.add(inst)
        session.commit()

        chk = FinancialAccount(
            id="fa_bofa_chk",
            institution_id=inst.id,
            account_name="Bank of America Checking (...9456)",
            account_type="CHECKING",
            account_number_masked="...9456",
        )
        cc = FinancialAccount(
            id="fa_apple_card",
            institution_id=inst.id,
            account_name="Apple Card (...1234)",
            account_type="CREDIT_CARD",
            account_number_masked="...1234",
        )
        session.add(chk)
        session.add(cc)
        session.commit()

    # 2. Upload: 1 property doc, 1 checking stmt, 1 cc stmt, 1 misc unclassified doc
    BOFA_PDF = b"%PDF-1.4\nBank of America Checking Statement content\n%%EOF\n"
    APPLE_PDF = b"%PDF-1.4\nApple Card Statement content\n%%EOF\n"
    MISC_PDF = b"%PDF-1.4\nMiscellaneous unclassified content\n%%EOF\n"

    _upload(client, [("lease.pdf", LEASE_PDF)], document_type="LEASE_AGREEMENT", property_id=vault["property_id"])
    _upload(client, [("bofa_jan.pdf", BOFA_PDF)], document_type="CHECKING_STATEMENT")
    _upload(client, [("apple_jan.pdf", APPLE_PDF)], document_type="CREDIT_CARD_STATEMENT")
    _upload(client, [("misc.pdf", MISC_PDF)], document_type="")

    # Link checking and card statements to accounts in Statement table
    with Session(engine) as session:
        bofa_doc = session.exec(select(Document).where(Document.original_filename == "bofa_jan.pdf")).one()
        bofa_doc_id = bofa_doc.id
        session.add(
            Statement(
                id="stmt_bofa_jan",
                financial_account_id="fa_bofa_chk",
                document_id=bofa_doc_id,
                statement_type="CHECKING",
                active_parser_name="bofa_checking",
                active_parser_version="1.0",
                statement_date=date(2025, 1, 31),
                opening_balance_cents=10000,
                closing_balance_cents=20000,
            )
        )
        apple_doc = session.exec(select(Document).where(Document.original_filename == "apple_jan.pdf")).one()
        session.add(
            Statement(
                id="stmt_apple_jan",
                financial_account_id="fa_apple_card",
                document_id=apple_doc.id,
                statement_type="CREDIT_CARD",
                active_parser_name="apple_card",
                active_parser_version="1.0",
                statement_date=date(2025, 1, 31),
                opening_balance_cents=0,
                closing_balance_cents=5000,
            )
        )
        session.commit()

    # 3. Whole vault view
    res = client.get("/documents")
    assert res.status_code == 200
    html = res.text

    # Three rail sections present
    assert "Properties" in html
    assert "Financial Accounts" in html
    assert "Unfiled" in html

    # Property tree has Larkspur
    assert "4821 Larkspur Ave" in html

    # Financial Accounts tree has Checking Accounts and Credit Cards
    assert "Checking Accounts" in html
    assert "Bank of America Checking (...9456)" in html
    assert "Credit Cards" in html
    assert "Apple Card (...1234)" in html

    # Location column in document table shows account name, not 'Unfiled'
    assert "Bank of America Checking (...9456) / Bank statement" in html
    assert "Apple Card (...1234) / Credit card statement" in html
    assert "4821 Larkspur Ave / Lease agreement" in html
    assert "Unfiled / Unclassified" in html

    # 4. Inspector tags on checking statement
    doc_res = client.get(f"/documents?doc={bofa_doc_id}")
    assert doc_res.status_code == 200
    # Inspector tags have the account name, not 'Unfiled'
    assert "Bank of America Checking (...9456)" in doc_res.text
    assert "Bank statement" in doc_res.text

    # 5. Open Checking Account folder
    acct_res = client.get("/documents?account=fa_bofa_chk")
    assert acct_res.status_code == 200
    acct_html = acct_res.text
    assert "bofa_jan.pdf" in acct_html
    assert "apple_jan.pdf" not in acct_html
    assert "lease.pdf" not in acct_html
    assert "misc.pdf" not in acct_html
    assert "Bank of America Checking (...9456)" in acct_html
    # Parent navigation points to Checking Accounts group
    assert "account_group=checking" in acct_html

    # 6. Open Unfiled folder
    unfiled_res = client.get("/documents?property=unfiled")
    assert unfiled_res.status_code == 200
    unfiled_html = unfiled_res.text
    assert "misc.pdf" in unfiled_html
    assert "bofa_jan.pdf" not in unfiled_html
    assert "apple_jan.pdf" not in unfiled_html
    assert "lease.pdf" not in unfiled_html

    # 7. Bulk download matching within financial account
    dl_res = client.post(
        "/documents/bulk/download",
        data={
            "scope": "all_matching",
            "csrf_token": _csrf(client),
            "account": "fa_bofa_chk",
            "kind": "all",
        },
    )
    assert dl_res.status_code == 200
    with zipfile.ZipFile(io.BytesIO(dl_res.content), "r") as zf:
        zf_names = zf.namelist()
        assert "bofa_jan.pdf" in zf_names
        assert "apple_jan.pdf" not in zf_names
        assert "lease.pdf" not in zf_names
        assert "misc.pdf" not in zf_names



