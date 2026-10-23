"""The console: the super admin, and the fact that it is not a tenant.

The super admin is the platform's own backend operator -- outside the tenant model, not a
portfolio role, not a membership. These tests hold that line from both sides:

* **Nothing a tenant does can reach it.** Registering, signing in, accepting an invitation
  and administering a portfolio all leave `is_super_admin` at 0, a tenant session cannot
  reach `/admin`, and a tenant's credentials are refused at the console door.
* **The console is its own world.** It has its own sign-in, its own cookie path-scoped to
  `/admin`, no portfolio lease, and it reports counts and storage and never a balance.

The super admin itself is created the only way it can be: on the host, by
`create_super_admin` -- which is what `scripts/create_super_admin.py` calls.
"""

import re

import pytest
from sqlalchemy import text
from sqlmodel import Session
from starlette.testclient import TestClient

from proptally.db.system_models import ROLE_READ_ONLY
from proptally.web.auth import create_super_admin

OPERATOR_EMAIL = "super@example.com"
OPERATOR_PASSWORD = "operator-password-1"
TENANT_EMAIL = "tenant@example.com"
TENANT_PASSWORD = "owner-password-1"


def _cookie(client, path="/admin/login") -> str:
    """The CSRF token the page itself carries, the way a browser would send it."""
    match = re.search(r'name="csrf_token" value="([^"]+)"', client.get(path).text)
    assert match, f"no csrf token on {path}"
    return match.group(1)


class Console:
    """An instance with one tenant, and the means to sign in as the super admin."""

    def __init__(self, tenant):
        self.tenant = tenant
        self.app = tenant.app
        self.control_engine = tenant.control_engine
        self._started: list[TestClient] = []
        self._clients: list[TestClient] = []

    def client(self) -> TestClient:
        client = TestClient(self.app)
        client.__enter__()
        self._started.append(client)
        return client

    def sign_in(self):
        """Sign in at the console door. Returns the client and the sign-in response."""
        client = self.client()
        token = _cookie(client)
        response = client.post(
            "/admin/login",
            data={
                "email": OPERATOR_EMAIL,
                "password": OPERATOR_PASSWORD,
                "csrf_token": token,
            },
            follow_redirects=False,
        )
        assert response.status_code == 303, response.text[:300]
        self._clients.append(client)
        return client, response

    def admin(self) -> TestClient:
        return self.sign_in()[0]

    def close(self):
        for client in self._started:
            try:
                client.__exit__(None, None, None)
            except Exception:  # noqa: BLE001, S110 - teardown must not mask a test failure
                pass


@pytest.fixture(name="console")
def console_fixture(owner):
    """One tenant on the instance, plus a super admin created on the host."""
    tenant = owner(
        email=TENANT_EMAIL, password=TENANT_PASSWORD, portfolio_name="Tenant Portfolio"
    )
    create_super_admin(
        tenant.control_engine, email=OPERATOR_EMAIL, password=OPERATOR_PASSWORD
    )
    console = Console(tenant)
    yield console
    console.close()


def _super_admin_emails(console) -> set:
    with Session(console.control_engine) as session:
        rows = session.execute(text("SELECT email FROM users WHERE is_super_admin = 1")).all()
    return {row[0] for row in rows}


# --- The tenant can never reach the console ---------------------------------

def test_tenant_registration_confers_no_platform_authority(console):
    """The only super admin is the one created on the host."""
    assert _super_admin_emails(console) == {OPERATOR_EMAIL}


def test_a_tenant_session_cannot_reach_the_console(console):
    assert console.tenant.client.get("/admin").status_code == 404
    assert console.tenant.client.get("/admin/tenants").status_code == 404
    assert console.tenant.client.get("/admin/people").status_code == 404


def test_a_tenant_account_cannot_sign_in_at_the_console_door(console):
    """A portfolio account is not a weaker operator; it is not an operator."""
    client = console.client()
    response = client.post(
        "/admin/login",
        data={
            "email": TENANT_EMAIL,
            "password": TENANT_PASSWORD,
            "csrf_token": _cookie(client),
        },
        follow_redirects=False,
    )
    assert response.status_code == 200  # re-rendered with a verdict, not signed in
    assert "did not match" in response.text
    assert client.get("/admin").status_code == 404

    # ...and the tenant door still works for that account. Clear the already
    # signed-in session first: /auth/login redirects a signed-in visitor, so the
    # form token must come from a real pre-session page.
    console.tenant.client.cookies.clear()
    tenant_token = _cookie(console.tenant.client, "/auth/login")
    signed_in = console.tenant.client.post(
        "/auth/login",
        data={
            "email": TENANT_EMAIL,
            "password": TENANT_PASSWORD,
            "csrf_token": tenant_token,
        },
        follow_redirects=False,
    )
    assert signed_in.status_code == 303


def test_an_anonymous_visitor_is_not_told_the_console_exists(console):
    assert console.client().get("/admin").status_code == 404


# --- The console is its own world -------------------------------------------

def test_the_console_has_its_own_sign_in(console):
    page = console.client().get("/admin/login")
    assert page.status_code == 200
    assert "Backend console" in page.text
    assert "create_super_admin" in page.text  # it says how one is made, and that it cannot be made here


def test_the_super_admin_signs_in_at_the_console_door(console):
    client, _ = console.sign_in()
    assert client.get("/admin").status_code == 200
    assert "Tenant Portfolio" in client.get("/admin/tenants").text
    assert "tenant@example.com" in client.get("/admin/people").text


def test_the_console_session_never_travels_to_the_tenant_application(console):
    """The cookie is path-scoped to /admin, so a portfolio request cannot carry it."""
    client, response = console.sign_in()
    assert "Path=/admin" in response.headers.get("set-cookie", "")

    # With only the console cookie, the tenant application sees no session at all.
    dash = client.get("/", follow_redirects=False)
    assert dash.status_code == 303
    assert dash.headers["location"].startswith("/auth/login")


def test_the_console_sign_out_revokes_its_own_session(console):
    client = console.admin()
    signed_out = client.post(
        "/admin/logout", data={"csrf_token": _cookie(client, "/admin")}, follow_redirects=False
    )
    assert signed_out.status_code == 303
    assert signed_out.headers["location"] == "/admin/login"
    assert client.get("/admin").status_code == 404


def test_a_console_post_without_a_token_is_refused(console):
    client = console.admin()
    response = client.post(
        f"/admin/tenants/{console.tenant.tenant_id}/invites",
        data={"email": "x@example.com", "role": ROLE_READ_ONLY},
        follow_redirects=False,
    )
    assert response.status_code == 403
    with Session(console.control_engine) as session:
        count = session.execute(text("SELECT COUNT(*) FROM user_invites")).scalar()
    assert count == 0


# --- What the console reports, and what it must never report -----------------

def test_the_console_reports_counts_and_never_a_balance(console):
    detail = console.admin().get(f"/admin/tenants/{console.tenant.tenant_id}")
    assert detail.status_code == 200
    assert "What it holds" in detail.text
    assert "Doors" in detail.text
    # No currency anywhere: a portfolio's money is not the platform's business.
    assert "$" not in detail.text


# --- Operator actions -------------------------------------------------------

def test_the_super_admin_can_invite_and_withdraw(console):
    client = console.admin()
    token = _cookie(client, "/admin")
    invited = client.post(
        f"/admin/tenants/{console.tenant.tenant_id}/invites",
        data={"email": "guest@example.com", "role": ROLE_READ_ONLY, "csrf_token": token},
        follow_redirects=False,
    )
    assert invited.status_code == 303
    assert "invite_token=" in invited.headers["location"]
    assert "guest@example.com" in client.get(
        f"/admin/tenants/{console.tenant.tenant_id}"
    ).text

    with Session(console.control_engine) as session:
        invite_id = session.execute(
            text("SELECT id FROM user_invites WHERE email = 'guest@example.com'")
        ).scalar()
    withdrawn = client.post(
        f"/admin/invites/{invite_id}/revoke",
        data={"tenant_id": console.tenant.tenant_id, "csrf_token": token},
        follow_redirects=False,
    )
    assert withdrawn.status_code == 303
    assert "guest@example.com" not in client.get(
        f"/admin/tenants/{console.tenant.tenant_id}"
    ).text


def test_the_console_cannot_leave_a_portfolio_without_an_owner(console):
    client = console.admin()
    with Session(console.control_engine) as session:
        membership_id = session.execute(
            text("SELECT id FROM tenant_memberships WHERE tenant_id = :t"),
            {"t": console.tenant.tenant_id},
        ).scalar()
    response = client.post(
        f"/admin/tenants/{console.tenant.tenant_id}/members/{membership_id}/role",
        data={"role": ROLE_READ_ONLY, "csrf_token": _cookie(client, "/admin")},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "error=" in response.headers["location"]
    with Session(console.control_engine) as session:
        role = session.execute(
            text("SELECT role FROM tenant_memberships WHERE id = :id"), {"id": membership_id}
        ).scalar()
    assert role == "OWNER"


def test_the_super_admin_issues_a_password_reset_that_works(console):
    """The operator can hand a tenant back their account without seeing their password."""
    client = console.admin()
    with Session(console.control_engine) as session:
        user_id = session.execute(
            text("SELECT id FROM users WHERE email = :e"), {"e": TENANT_EMAIL}
        ).scalar()

    issued = client.post(
        f"/admin/people/{user_id}/reset",
        data={"csrf_token": _cookie(client, "/admin")},
        follow_redirects=False,
    )
    assert issued.status_code == 303
    match = re.search(r"reset_token=([A-Za-z0-9_\-]+)", issued.headers["location"])
    assert match, issued.headers["location"]

    stranger = console.client()
    reset = stranger.post(
        f"/auth/reset/{match.group(1)}",
        data={"password": "tenant-new-password-1", "csrf_token": _cookie(stranger, "/auth/login")},
        follow_redirects=False,
    )
    assert reset.status_code == 303

    signed_in = console.tenant.client.post(
        "/auth/login",
        data={
            "email": TENANT_EMAIL,
            "password": "tenant-new-password-1",
            "csrf_token": _cookie(console.tenant.client, "/auth/login"),
        },
        follow_redirects=False,
    )
    assert signed_in.status_code == 303


# --- Onboarding codes: one code, one new portfolio ---------------------------

def _mint(console, *, email: str = "", portfolio_name: str = "") -> str:
    """Mints a code from the console and returns the plain token."""
    client = console.admin()
    minted = client.post(
        "/admin/onboarding",
        data={
            "email": email,
            "portfolio_name": portfolio_name,
            "csrf_token": _cookie(client, "/admin/onboarding"),
        },
        follow_redirects=False,
    )
    assert minted.status_code == 303, minted.text[:300]
    match = re.search(r"code=([A-Za-z0-9_\-]+)", minted.headers["location"])
    assert match, minted.headers["location"]
    return match.group(1)


def _redeem(console, code, *, email, password, name="New Person", portfolio="", client=None):
    """Redeems a code the way the person holding the link does."""
    person = client or console.client()
    path = f"/auth/join?code={code}"
    return person, person.post(
        "/auth/join",
        data={
            "code": code,
            "email": email,
            "password": password,
            "display_name": name,
            "portfolio_name": portfolio,
            "csrf_token": _cookie(person, path),
        },
        follow_redirects=False,
    )


def test_a_code_from_the_console_starts_exactly_one_portfolio(console):
    code = _mint(console, portfolio_name="Bluebonnet Holdings")

    # The link opens the redemption form, prefilled with the suggested name.
    person = console.client()
    page = person.get(f"/auth/join?code={code}")
    assert page.status_code == 200
    assert "Bluebonnet Holdings" in page.text

    # Ordinary registration is closed here — the fixture already has a tenant — yet the
    # code still opens the door, because the code is the permission, not registration.
    assert "Portfolio invitation code" in console.client().get("/auth/register").text

    _, created = _redeem(
        console, code, email="newcomer@example.com", password="newcomer-password-1",
        portfolio="Bluebonnet Holdings", client=person,
    )
    assert created.status_code == 303
    assert created.headers["location"] == "/"

    with Session(console.control_engine) as session:
        tenant = session.execute(
            text("SELECT status FROM tenants WHERE name = 'Bluebonnet Holdings'")
        ).scalar()
        super_admin = session.execute(
            text("SELECT is_super_admin FROM users WHERE email = 'newcomer@example.com'")
        ).scalar()
        role = session.execute(
            text(
                "SELECT m.role FROM tenant_memberships m JOIN users u ON u.id = m.user_id "
                "WHERE u.email = 'newcomer@example.com'"
            )
        ).scalar()
        spent = session.execute(
            text("SELECT COUNT(*) FROM tenant_onboarding_invites WHERE accepted_at IS NOT NULL")
        ).scalar()
    assert tenant == "ACTIVE"
    assert role == "OWNER"
    # Platform authority is not what a code confers.
    assert super_admin == 0
    assert spent == 1

    # The signed-in newcomer reaches their own portfolio.
    assert person.get("/").status_code == 200


def test_a_code_cannot_be_redeemed_twice(console):
    code = _mint(console)
    _, first = _redeem(console, code, email="first@example.com", password="first-password-1")
    assert first.status_code == 303

    _, second = _redeem(console, code, email="second@example.com", password="second-password-1")
    assert second.status_code == 200
    assert "already used" in second.text

    with Session(console.control_engine) as session:
        assert session.execute(
            text("SELECT COUNT(*) FROM users WHERE email = 'second@example.com'")
        ).scalar() == 0


def test_a_refused_redemption_does_not_spend_the_code(console):
    """A mistyped password must not burn the operator's link."""
    code = _mint(console)
    person = console.client()
    _, weak = _redeem(console, code, email="careless@example.com", password="short", client=person)
    assert weak.status_code == 200
    assert "10 characters" in weak.text

    page = console.client().get(f"/auth/join?code={code}")
    assert page.status_code == 200
    assert "Could not start your portfolio" not in page.text

    _, ok = _redeem(
        console, code, email="careless@example.com", password="careless-password-1", client=person
    )
    assert ok.status_code == 303


def test_a_pinned_code_rejects_another_address(console):
    code = _mint(console, email="invited@example.com")
    page = console.client().get(f"/auth/join?code={code}")
    assert "invited@example.com" in page.text

    _, wrong = _redeem(console, code, email="somebody-else@example.com", password="somebody-else-1")
    assert wrong.status_code == 200
    assert "invited@example.com" in wrong.text

    _, right = _redeem(console, code, email="invited@example.com", password="invited-password-1")
    assert right.status_code == 303


def test_a_withdrawn_code_stops_working(console):
    code = _mint(console)
    client = console.admin()
    with Session(console.control_engine) as session:
        invite_id = session.execute(
            text("SELECT id FROM tenant_onboarding_invites")
        ).scalar()

    revoked = client.post(
        f"/admin/onboarding/{invite_id}/revoke",
        data={"csrf_token": _cookie(client, "/admin/onboarding")},
        follow_redirects=False,
    )
    assert revoked.status_code == 303

    assert "invalid" in console.client().get(f"/auth/join?code={code}").text
    _, refused = _redeem(console, code, email="late@example.com", password="late-password-1")
    assert refused.status_code == 200
    assert "invalid" in refused.text


def test_a_tenant_cannot_mint_codes(console):
    """Minting is platform administration, so a portfolio session cannot reach it."""
    tenant_client = console.tenant.client
    assert tenant_client.get("/admin/onboarding").status_code == 404
    response = tenant_client.post(
        "/admin/onboarding", data={"portfolio_name": "Not mine"}, follow_redirects=False
    )
    assert response.status_code == 404
    with Session(console.control_engine) as session:
        assert session.execute(
            text("SELECT COUNT(*) FROM tenant_onboarding_invites")
        ).scalar() == 0


# --- The create-portfolio page: an established host asks for a code ---------

def test_an_established_host_asks_for_a_code_on_the_create_portfolio_page(console):
    """Once an operator exists, the door is theirs: the page asks, and it must be answered."""
    page = console.client().get("/auth/register")
    assert page.status_code == 200
    assert "Portfolio invitation code" in page.text


def test_a_code_minted_in_the_console_opens_the_create_portfolio_page(console):
    """The code typed on that page is the same single-use grant the console minted."""
    code = _mint(console, portfolio_name="Typed Fund")

    person = console.client()
    token = _cookie(person, "/auth/register")
    created = person.post(
        "/auth/register",
        data={
            "email": "typed@example.com",
            "password": "typed-password-1",
            "display_name": "Typed Person",
            "portfolio_name": "Typed Fund",
            "portfolio_code": code,
            "csrf_token": token,
        },
        follow_redirects=False,
    )
    assert created.status_code == 303, created.text[:300]
    assert created.headers["location"] == "/"

    with Session(console.control_engine) as session:
        assert session.execute(
            text("SELECT status FROM tenants WHERE name = 'Typed Fund'")
        ).scalar() == "ACTIVE"
        # The code is spent by this path too — it is one grant, not one grant per page.
        assert session.execute(
            text("SELECT COUNT(*) FROM tenant_onboarding_invites WHERE accepted_at IS NOT NULL")
        ).scalar() == 1


def test_the_create_portfolio_page_refuses_without_a_code(console):
    person = console.client()
    refused = person.post(
        "/auth/register",
        data={
            "email": "sneaky@example.com",
            "password": "sneaky-password-1",
            "display_name": "Sneaky",
            "portfolio_name": "Backdoor Holdings",
            "portfolio_code": "",
            "csrf_token": _cookie(person, "/auth/register"),
        },
        follow_redirects=False,
    )
    assert refused.status_code == 200
    assert "invitation code is not right" in refused.text

    with Session(console.control_engine) as session:
        assert session.execute(
            text("SELECT COUNT(*) FROM users WHERE email = 'sneaky@example.com'")
        ).scalar() == 0
        assert session.execute(
            text("SELECT COUNT(*) FROM tenants WHERE name = 'Backdoor Holdings'")
        ).scalar() == 0


def test_a_host_nobody_has_claimed_asks_for_no_code(tmp_path):
    """The bootstrap case: no operator yet, so there is no one to ask and nothing to mint."""
    from proptally.web.app import create_app
    from proptally.web.auth import registration_requires_code

    app = create_app(
        system_db_url=f"sqlite:///{tmp_path / 'system.db'}", storage_root=str(tmp_path)
    )
    with TestClient(app) as client:
        assert registration_requires_code(app.state.control_engine) is False

        page = client.get("/auth/register")
        assert page.status_code == 200
        assert "Portfolio invitation code" not in page.text

        created = client.post(
            "/auth/register",
            data={
                "email": "first@example.com",
                "password": "first-password-1",
                "display_name": "First",
                "portfolio_name": "First Portfolio",
                "csrf_token": re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1),
            },
            follow_redirects=False,
        )
        assert created.status_code == 303

    # ...and the first account is a tenant, not the platform's operator.
    with Session(app.state.control_engine) as session:
        assert session.execute(
            text("SELECT is_super_admin FROM users WHERE email = 'first@example.com'")
        ).scalar() == 0


# --- Document intelligence: the pipeline console -----------------------------

def test_the_pipeline_page_is_console_only(console):
    assert console.tenant.client.get("/admin/document-intelligence").status_code == 404


def test_the_pipeline_page_reports_an_unreachable_engine(console, monkeypatch):
    """A dead daemon is a state to render, never a 500."""
    monkeypatch.delenv("PROPTALLY_DOC_INTEL", raising=False)
    monkeypatch.setenv("PROPTALLY_DOC_INTEL_URL", "http://127.0.0.1:9")  # discard port

    page = console.admin().get("/admin/document-intelligence")

    assert page.status_code == 200
    assert "Document intelligence" in page.text
    assert "Unreachable" in page.text
    assert "PROPTALLY_DOC_INTEL OFF" in page.text
    assert "did not answer" in page.text


def test_the_pipeline_page_reports_profiles_jobs_and_staged_counts(console, monkeypatch):
    from proptally.db.models import Document, DocumentExtraction
    from proptally.ingestion.service import DOC_INTEL_PARSER_NAME
    from proptally.web.routes import admin as admin_routes

    monkeypatch.setattr(
        admin_routes,
        "_doc_intel_engine_status",
        lambda **kwargs: {
            "enabled": True,
            "base_url": "http://127.0.0.1:8765",
            "reachable": True,
            "error": None,
            "health": {
                "status": "healthy",
                "version": "0.1.0",
                "default_local_model": "qwen2.5:7b",
                "local_endpoint": "http://localhost:11434/v1",
                "corpus_documents_indexed": 311,
            },
            "profiles": [
                {
                    "id": "bank_statement",
                    "name": "Bank Account Statement",
                    "version": 1,
                    "fields": 9,
                    "required": 3,
                    "statement_type": "CHECKING",
                },
                {
                    "id": "rental_agreement",
                    "name": "Rental Agreement",
                    "version": 1,
                    "fields": 12,
                    "required": 4,
                    "statement_type": None,
                },
            ],
            "jobs": [
                {
                    "id": "job_090089433853",
                    "status": "completed",
                    "profile": "bank_statement",
                    "filename": "eStmt_2021-09-16.pdf",
                    "duration": 28.14,
                    "document": "doc_4e87cffefcd9caf6",
                    "error": None,
                },
                {
                    "id": "job_failed01",
                    "status": "failed",
                    "profile": "bank_statement",
                    "filename": "encrypted.pdf",
                    "duration": None,
                    "document": None,
                    "error": "PDF is encrypted",
                },
            ],
            "job_counts": [("completed", 1), ("failed", 1)],
        },
    )

    with Session(console.tenant.engine) as session:
        session.add(
            Document(
                id="doc_sidecar",
                file_sha256="a" * 64,
                original_filename="eStmt_2021-09-16.pdf",
                storage_path="/vault/a",
                file_size_bytes=1024,
            )
        )
        session.flush()
        session.add(
            DocumentExtraction(
                id="ext_sidecar",
                document_id="doc_sidecar",
                parser_name=DOC_INTEL_PARSER_NAME,
                parser_version="0.1.0",
                validation_status="NEEDS_REVIEW",
                approval_status="PENDING",
                extracted_data_json="{}",
            )
        )
        session.commit()

    page = console.admin().get("/admin/document-intelligence")

    assert page.status_code == 200
    assert "PROPTALLY_DOC_INTEL ON" in page.text
    assert "bank_statement" in page.text
    assert "CHECKING" in page.text
    assert "NOT USED HERE" in page.text
    assert "eStmt_2021-09-16.pdf" in page.text
    assert "28.1s" in page.text
    assert "PDF is encrypted" in page.text
    assert "Tenant Portfolio" in page.text


def test_the_profile_detail_page_is_console_only(console):
    assert console.tenant.client.get(
        "/admin/document-intelligence/profiles/bank_statement"
    ).status_code == 404


def test_the_profile_detail_page_shows_the_field_contract_and_source(console, monkeypatch):
    from proptally.web.routes import admin as admin_routes

    monkeypatch.setattr(
        admin_routes,
        "_doc_intel_profile",
        lambda profile_id, **kwargs: {
            "profile_id": profile_id,
            "path": "/srv/document-intelligence-lab/config/profiles/bank_statement.yaml",
            "source": "profile_id: bank_statement\nversion: 1\nfields:\n  - key: institution_name\n",
            "statement_type": "CHECKING",
            "error": None,
            "profile": {
                "profile_id": profile_id,
                "version": 1,
                "display_name": "Bank Account Statement",
                "description": "Bank and card statements.",
                "fields": [
                    {
                        "key": "institution_name",
                        "label": "Financial Institution",
                        "type": "string",
                        "required": True,
                        "normalization": "trim_and_preserve_name",
                        "criticality": "critical",
                        "description": "Name of the bank issuing the statement.",
                        "aliases": ["bank", "institution"],
                    },
                    {
                        "key": "currency",
                        "label": "Currency",
                        "type": "string",
                        "required": False,
                        "normalization": "enum",
                        "criticality": "medium",
                        "description": None,
                        "aliases": [],
                    },
                ],
            },
        },
    )

    page = console.admin().get("/admin/document-intelligence/profiles/bank_statement")

    assert page.status_code == 200
    assert "Bank Account Statement" in page.text
    assert "institution_name" in page.text
    assert "trim_and_preserve_name" in page.text
    assert "REQUIRED" in page.text
    assert "also: bank, institution" in page.text
    # The YAML is shown verbatim, escaped.
    assert "profile_id: bank_statement" in page.text
    assert "read-only" in page.text.lower()


def test_the_profile_detail_page_reports_an_unreachable_daemon(console, monkeypatch):
    from proptally.web.routes import admin as admin_routes

    monkeypatch.setattr(
        admin_routes,
        "_doc_intel_profile",
        lambda profile_id, **kwargs: {
            "profile_id": profile_id,
            "path": None,
            "source": "",
            "profile": None,
            "statement_type": None,
            "error": "ConnectError: [Errno 61] Connection refused",
        },
    )

    page = console.admin().get("/admin/document-intelligence/profiles/bank_statement")

    assert page.status_code == 200
    assert "could not be read" in page.text
    assert "Connection refused" in page.text


def test_doc_intel_engine_status_integrates_with_client_methods(monkeypatch):
    import httpx
    from proptally.web.routes import admin as admin_routes
    from proptally.ingestion.doc_intel import DocumentIntelligenceClient

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "healthy", "version": "0.1.0"})
        if request.url.path == "/api/v1/profiles":
            return httpx.Response(200, json={
                "profiles": {
                    "bank_statement": {
                        "display_name": "Bank Account Statement",
                        "version": 1,
                        "fields": {"account_type": {"required": True}},
                    }
                }
            })
        if request.url.path == "/api/v1/jobs":
            return httpx.Response(200, json=[
                {
                    "job_id": "job_01",
                    "status": "completed",
                    "profile_name": "bank_statement",
                }
            ])
        if request.url.path == "/api/v1/profiles/bank_statement":
            return httpx.Response(200, json={
                "profile_id": "bank_statement",
                "path": "/path/to/bank_statement.yaml",
                "source": "yaml_content",
                "profile": {"profile_id": "bank_statement", "fields": []},
                "error": None,
            })
        return httpx.Response(404, json={"detail": "not found"})

    def mock_client_factory(*args, **kwargs):
        return DocumentIntelligenceClient(
            base_url="http://127.0.0.1:8765",
            http_client=httpx.Client(
                base_url="http://127.0.0.1:8765", transport=httpx.MockTransport(handler)
            ),
        )

    monkeypatch.setattr(admin_routes, "DocumentIntelligenceClient", mock_client_factory)

    status = admin_routes._doc_intel_engine_status()
    assert status["reachable"] is True
    assert status["error"] is None
    assert len(status["profiles"]) == 1
    assert status["profiles"][0]["id"] == "bank_statement"
    assert status["profiles"][0]["statement_type"] == "CHECKING"
    assert len(status["jobs"]) == 1
    assert status["jobs"][0]["id"] == "job_01"

    profile_data = admin_routes._doc_intel_profile("bank_statement")
    assert profile_data["error"] is None
    assert profile_data["profile_id"] == "bank_statement"
    assert profile_data["source"] == "yaml_content"
