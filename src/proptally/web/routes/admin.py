"""The console: the super admin's surface, outside the tenant model.

The super admin is the application's own backend operator. It is not a portfolio role,
not a membership, and not a participant in the platform's data -- it administers the
software and the tenants that live on it. Three consequences shape every route here:

* **It is created on the host.** ``scripts/create_super_admin.py``; no route in this
  module (or anywhere else) can mint one, and tenant registration never confers it.
* **It has its own door.** ``/admin/login`` with a cookie path-scoped to ``/admin``, so
  the console's session never travels with a portfolio request and a tenant session can
  never reach the console.
* **It never reads a ledger.** Every figure here is a control-plane fact or a
  count-only summary from ``portfolio_status()`` -- properties, documents, storage,
  members. Never a balance.

Everything except the sign-in mounts :class:`~proptally.web.auth.AdminDependency`:
a session belonging to an active super admin, or 404, so the console is not advertised
to the tenants on this platform.
"""

from __future__ import annotations

import urllib.parse
from collections import Counter
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func
from sqlmodel import Session, select

from proptally.db.portfolio_lifecycle import portfolio_status
from proptally.db.system_models import (
    INVITABLE_ROLES,
    ROLES,
    TENANT_STATES,
    LegacyMigration,
    Tenant,
    TenantMembership,
    TenantOperation,
    User,
    UserInvite,
    UserSession,
)
from proptally.db.tenant_manager import TenantManager
from proptally.web.auth import (
    ROLE_PICKER_LABELS,
    MembershipError,
    authenticate_super_admin,
    change_member_role,
    clear_admin_cookie,
    create_invite,
    create_onboarding_invite,
    issue_password_reset,
    issue_pre_session_csrf,
    issue_session,
    onboarding_invites,
    pending_invites,
    pre_session_csrf_valid,
    remove_member,
    resolve_admin_context,
    revoke_invite,
    revoke_onboarding_invite,
    revoke_session,
    set_admin_cookie,
    set_pre_session_csrf_cookie,
    super_admin_cookie_name,
    tenant_members,
    utc_now,
)


def _count(session: Session, model, *criteria) -> int:
    statement = select(func.count()).select_from(model)
    for criterion in criteria:
        statement = statement.where(criterion)
    return session.exec(statement).one()


def _control_plane_path(control_engine) -> Path | None:
    database = control_engine.url.database
    if database:
        return Path(database)
    return None


def _file_size(path: Path | None) -> int:
    try:
        return path.stat().st_size if path and path.is_file() else 0
    except OSError:
        return 0


def _instance_overview(control_engine) -> dict:
    """Instance-wide counts. Nothing here names a portfolio's contents."""
    with Session(control_engine) as session:
        users_total = _count(session, User)
        users_active = _count(session, User, User.is_active == True)
        admins = _count(session, User, User.is_super_admin == True)
        tenants = list(session.exec(select(Tenant).order_by(Tenant.created_at)).all())
        sessions_live = _count(
            session,
            UserSession,
            UserSession.revoked_at.is_(None),
            UserSession.expires_at > utc_now(),
        )
        memberships = _count(session, TenantMembership)
        invites_waiting = _count(session, UserInvite, UserInvite.accepted_at.is_(None))

    by_status = Counter(tenant.status for tenant in tenants)
    return {
        "users_total": users_total,
        "users_active": users_active,
        "super_admins": admins,
        "tenant_accounts": users_total - admins,
        "tenants_total": len(tenants),
        "tenants_active": by_status.get("ACTIVE", 0),
        "by_status": [(status, by_status.get(status, 0)) for status in TENANT_STATES],
        "sessions_live": sessions_live,
        "memberships": memberships,
        "invites_waiting": invites_waiting,
    }


def _tenant_rows(control_engine, tenant_manager: TenantManager) -> list[dict]:
    """Every portfolio with its control-plane facts and count-only summary."""
    with Session(control_engine) as session:
        tenants = list(session.exec(select(Tenant).order_by(Tenant.created_at)).all())
        memberships = list(session.exec(select(TenantMembership)).all())
        invites = list(session.exec(select(UserInvite)).all())
        operations = list(session.exec(select(TenantOperation)).all())
        migrations = list(session.exec(select(LegacyMigration)).all())
        users = {user.id: user for user in session.exec(select(User)).all()}

    members_by_tenant: dict[str, list] = {}
    for membership in memberships:
        members_by_tenant.setdefault(membership.tenant_id, []).append(membership)
    invites_by_tenant = Counter(
        invite.tenant_id for invite in invites if invite.accepted_at is None
    )
    leases_by_tenant = Counter(operation.tenant_id for operation in operations)
    latest_migration: dict[str, LegacyMigration] = {}
    for migration in sorted(migrations, key=lambda m: m.updated_at or m.created_at):
        latest_migration[migration.tenant_id] = migration

    rows = []
    for tenant in tenants:
        tenant_memberships = members_by_tenant.get(tenant.id, [])
        owner_emails = [
            users[m.user_id].email
            for m in tenant_memberships
            if m.role == "OWNER" and m.user_id in users
        ]
        summary = portfolio_status(control_engine, tenant_manager, tenant.id)
        rows.append({
            "id": tenant.id,
            "name": tenant.name,
            "slug": tenant.slug,
            "status": tenant.status,
            "created_at": tenant.created_at,
            "member_count": len(tenant_memberships),
            "owner_emails": owner_emails,
            "pending_invites": invites_by_tenant.get(tenant.id, 0),
            "leases": leases_by_tenant.get(tenant.id, 0),
            "migration": latest_migration.get(tenant.id),
            "status_detail": summary,
            "counts": summary.summary if summary else None,
            "readable": bool(summary and summary.summary.readable),
        })
    return rows


def _people_rows(control_engine) -> list[dict]:
    """Every account with membership and live-session counts."""
    with Session(control_engine) as session:
        users = list(session.exec(select(User).order_by(User.created_at)).all())
        memberships = list(session.exec(select(TenantMembership)).all())
        tenants = {tenant.id: tenant for tenant in session.exec(select(Tenant)).all()}
        sessions = list(
            session.exec(
                select(UserSession).where(
                    UserSession.revoked_at.is_(None),
                    UserSession.expires_at > utc_now(),
                )
            ).all()
        )

    membership_rows: dict[str, list] = {}
    for membership in memberships:
        membership_rows.setdefault(membership.user_id, []).append(membership)
    session_counts = Counter(row.user_id for row in sessions)

    rows = []
    for user in users:
        user_memberships = membership_rows.get(user.id, [])
        rows.append({
            "id": user.id,
            "email": user.email,
            "display_name": user.display_name,
            "is_super_admin": user.is_super_admin,
            "is_active": user.is_active,
            "created_at": user.created_at,
            "membership_count": len(user_memberships),
            "session_count": session_counts.get(user.id, 0),
            "portfolios": [
                {
                    "tenant_id": membership.tenant_id,
                    "name": tenants[membership.tenant_id].name if membership.tenant_id in tenants else "(removed)",
                    "role": membership.role,
                }
                for membership in user_memberships
            ],
        })
    return rows


def _person(control_engine, user_id: str) -> dict | None:
    with Session(control_engine) as session:
        user = session.get(User, user_id)
        if user is None:
            return None
        memberships = list(
            session.exec(
                select(TenantMembership).where(TenantMembership.user_id == user_id)
            ).all()
        )
        tenants = {tenant.id: tenant for tenant in session.exec(select(Tenant)).all()}
        sessions = list(
            session.exec(
                select(UserSession)
                .where(UserSession.user_id == user_id)
                .order_by(UserSession.created_at.desc())
            ).all()
        )
        session.expunge(user)
        for row in sessions:
            session.expunge(row)

    now = utc_now()
    return {
        "id": user.id,
        "email": user.email,
        "display_name": user.display_name,
        "is_super_admin": user.is_super_admin,
        "is_active": user.is_active,
        "created_at": user.created_at,
        "password_changed_at": user.password_changed_at,
        "portfolios": [
            {
                "tenant_id": membership.tenant_id,
                "name": tenants[membership.tenant_id].name if membership.tenant_id in tenants else "(removed)",
                "role": membership.role,
                "joined_at": membership.created_at,
            }
            for membership in memberships
        ],
        "sessions": [
            {
                "id": row.id[:12],
                "ip_address": row.ip_address or "—",
                "user_agent": (row.user_agent or "")[:120] or "—",
                "created_at": row.created_at,
                "expires_at": row.expires_at,
                "live": row.revoked_at is None and row.expires_at > now,
            }
            for row in sessions
        ],
    }


def get_admin_router(
    templates,
    control_engine,
    tenant_manager: TenantManager,
    admin_dep,
    rate_limiter,
) -> APIRouter:
    router = APIRouter(prefix="/admin")

    def _admin_nav() -> list[dict]:
        return [
            {"key": "overview", "label": "Overview", "href": "/admin"},
            {"key": "tenants", "label": "Portfolios", "href": "/admin/tenants"},
            {"key": "people", "label": "People", "href": "/admin/people"},
        ]

    def _render(request, name, context, active):
        return templates.TemplateResponse(
            request=request,
            name=name,
            context={"admin_nav": _admin_nav(), "active_page": active, **context},
        )

    def _render_login(request: Request, *, error_kind: str | None = None, email: str = ""):
        """Render the console sign-in, issuing the pre-session CSRF cookie once.

        Unauthenticated, so the token cannot come from a session: it is the pre-session
        double-submit cookie the tenant sign-in uses, for the same reason (P17).
        """
        csrf_token, set_cookie = issue_pre_session_csrf(request)
        response = templates.TemplateResponse(
            request=request,
            name="admin/login.html",
            context={"csrf_token": csrf_token, "error_kind": error_kind, "email": email},
        )
        if set_cookie:
            set_pre_session_csrf_cookie(response, csrf_token, request)
        return response

    def _client_ip(request: Request) -> str:
        return request.client.host if request.client else "unknown"

    # --- The super admin's own door --------------------------------------

    @router.get("/login", response_class=HTMLResponse)
    def admin_login_form(request: Request):
        """The console's sign-in. Separate from ``/auth/login`` on purpose.

        The super admin is not a tenant: it does not sign in through the portfolio door,
        and a portfolio session is no use here.
        """
        if resolve_admin_context(
            control_engine, request.cookies.get(super_admin_cookie_name())
        ) is not None:
            return RedirectResponse(url="/admin", status_code=303)
        return _render_login(request)

    @router.post("/login")
    def admin_login_submit(
        request: Request,
        email: str = Form(...),
        password: str = Form(...),
        csrf_token: str = Form(None),
    ):
        if not pre_session_csrf_valid(request, csrf_token):
            return _render_login(request, error_kind="expired", email=email)

        ip = _client_ip(request)
        if rate_limiter.is_blocked(ip, email):
            return _render_login(request, error_kind="throttled", email=email)

        user = authenticate_super_admin(control_engine, email, password)
        if user is None:
            rate_limiter.record_failure(ip, email)
            return _render_login(request, error_kind="credentials", email=email)

        rate_limiter.clear(ip, email)
        # No portfolio: the super admin has no tenant, and the session says so.
        token, _ = issue_session(
            control_engine,
            user,
            None,
            ip_address=ip,
            user_agent=request.headers.get("user-agent"),
        )
        response = RedirectResponse(url="/admin", status_code=303)
        set_admin_cookie(response, token, request)
        return response

    @router.post("/logout")
    def admin_logout(request: Request, csrf_token: str = Form(None)):
        token = request.cookies.get(super_admin_cookie_name())
        context = resolve_admin_context(control_engine, token)
        if context is not None:
            presented = csrf_token or request.headers.get("x-csrf-token")
            if presented and presented == context.csrf_token:
                revoke_session(control_engine, token)
        elif token:
            revoke_session(control_engine, token)
        response = RedirectResponse(url="/admin/login", status_code=303)
        clear_admin_cookie(response)
        return response

    # --- Overview --------------------------------------------------------

    @router.get("", response_class=HTMLResponse)
    @router.get("/", response_class=HTMLResponse)
    def overview(request: Request, admin=Depends(admin_dep)):
        return _render(
            request,
            "admin/overview.html",
            {
                "admin": admin,
                "csrf_token": admin.csrf_token,
                "overview": _instance_overview(control_engine),
                "control_plane": _control_plane_path(control_engine),
                "control_plane_bytes": _file_size(_control_plane_path(control_engine)),
                "storage_root": tenant_manager.root,
            },
            "overview",
        )

    # --- Portfolios ------------------------------------------------------

    @router.get("/tenants", response_class=HTMLResponse)
    def tenants_list(request: Request, admin=Depends(admin_dep)):
        return _render(
            request,
            "admin/tenants.html",
            {
                "admin": admin,
                "csrf_token": admin.csrf_token,
                "tenants": _tenant_rows(control_engine, tenant_manager),
                "message": request.query_params.get("message"),
                "error": request.query_params.get("error"),
            },
            "tenants",
        )

    @router.get("/tenants/{tenant_id}", response_class=HTMLResponse)
    def tenant_detail(request: Request, tenant_id: str, admin=Depends(admin_dep)):
        with Session(control_engine) as session:
            tenant = session.get(Tenant, tenant_id)
            if tenant is None:
                raise HTTPException(status_code=404, detail="No such portfolio")
            session.expunge(tenant)
            migration = session.exec(
                select(LegacyMigration)
                .where(LegacyMigration.tenant_id == tenant_id)
                .order_by(LegacyMigration.updated_at.desc())
            ).first()
        return _render(
            request,
            "admin/tenant_detail.html",
            {
                "admin": admin,
                "csrf_token": admin.csrf_token,
                "tenant": tenant,
                "status_detail": portfolio_status(control_engine, tenant_manager, tenant_id),
                "members": tenant_members(control_engine, tenant_id),
                "pending_invites": pending_invites(control_engine, tenant_id),
                "migration": migration,
                "role_options": [{"value": role, "label": role} for role in ROLES],
                "invitable_role_options": [
                    {"value": role, "label": ROLE_PICKER_LABELS.get(role, role)}
                    for role in INVITABLE_ROLES
                ],
                "invite_token": request.query_params.get("invite_token"),
                "message": request.query_params.get("message"),
                "error": request.query_params.get("error"),
            },
            "tenants",
        )

    # --- People ----------------------------------------------------------

    @router.get("/people", response_class=HTMLResponse)
    def people_list(request: Request, admin=Depends(admin_dep)):
        return _render(
            request,
            "admin/people.html",
            {
                "admin": admin,
                "csrf_token": admin.csrf_token,
                "people": _people_rows(control_engine),
                "message": request.query_params.get("message"),
                "error": request.query_params.get("error"),
            },
            "people",
        )

    @router.get("/people/{user_id}", response_class=HTMLResponse)
    def person_detail(request: Request, user_id: str, admin=Depends(admin_dep)):
        person = _person(control_engine, user_id)
        if person is None:
            raise HTTPException(status_code=404, detail="No such account")
        return _render(
            request,
            "admin/person_detail.html",
            {
                "admin": admin,
                "csrf_token": admin.csrf_token,
                "person": person,
                "reset_token": request.query_params.get("reset_token"),
                "message": request.query_params.get("message"),
                "error": request.query_params.get("error"),
            },
            "people",
        )

    # --- Onboarding codes (starting a new portfolio) ---------------------

    @router.get("/onboarding", response_class=HTMLResponse)
    def onboarding_page(request: Request, admin=Depends(admin_dep)):
        """Mint, review and withdraw the codes that start new portfolios.

        The platform-level counterpart of a portfolio invitation: this one creates a
        tenant, that one joins an existing one.
        """
        return _render(
            request,
            "admin/onboarding.html",
            {
                "admin": admin,
                "csrf_token": admin.csrf_token,
                "invites": onboarding_invites(control_engine),
                "code": request.query_params.get("code"),
                "now": utc_now(),
                "message": request.query_params.get("message"),
                "error": request.query_params.get("error"),
            },
            "onboarding",
        )

    @router.post("/onboarding")
    def onboarding_mint(
        request: Request,
        email: str = Form(""),
        portfolio_name: str = Form(""),
        admin=Depends(admin_dep),
    ):
        try:
            _, token = create_onboarding_invite(
                control_engine,
                created_by_user_id=admin.user.id,
                email=email,
                portfolio_name_hint=portfolio_name,
            )
        except ValueError as exc:
            return RedirectResponse(
                url="/admin/onboarding?error=" + urllib.parse.quote(str(exc)),
                status_code=303,
            )
        return RedirectResponse(
            url="/admin/onboarding?code=" + urllib.parse.quote(token), status_code=303
        )

    @router.post("/onboarding/{invite_id}/revoke")
    def onboarding_revoke(invite_id: str, admin=Depends(admin_dep)):
        try:
            revoke_onboarding_invite(control_engine, invite_id)
        except MembershipError as exc:
            return RedirectResponse(
                url="/admin/onboarding?error=" + urllib.parse.quote(str(exc)),
                status_code=303,
            )
        return RedirectResponse(
            url="/admin/onboarding?message=" + urllib.parse.quote("Code withdrawn."),
            status_code=303,
        )

    # --- Operator actions ------------------------------------------------
    #
    # Each wraps an existing, already-tested primitive. The operator acts on a
    # portfolio's behalf, so the guard rails the primitive already carries -- the
    # invitable-role set, the "a portfolio keeps an owner" rule -- still apply.

    @router.post("/people/{user_id}/reset")
    def issue_reset(user_id: str, admin=Depends(admin_dep)):
        with Session(control_engine) as session:
            user = session.get(User, user_id)
            email = user.email if user else None
        if email is None:
            raise HTTPException(status_code=404, detail="No such account")
        token = issue_password_reset(control_engine, email)
        if token is None:
            return RedirectResponse(
                url=f"/admin/people/{user_id}?error="
                + urllib.parse.quote("No usable account for that address."),
                status_code=303,
            )
        return RedirectResponse(
            url=f"/admin/people/{user_id}?reset_token={urllib.parse.quote(token)}",
            status_code=303,
        )

    @router.post("/tenants/{tenant_id}/invites")
    def invite_member(
        tenant_id: str,
        email: str = Form(...),
        role: str = Form("READ_ONLY"),
        admin=Depends(admin_dep),
    ):
        try:
            _, token = create_invite(
                control_engine,
                tenant_id=tenant_id,
                email=email,
                role=role,
                invited_by_user_id=admin.user.id,
            )
        except ValueError as exc:
            return RedirectResponse(
                url=f"/admin/tenants/{tenant_id}?error=" + urllib.parse.quote(str(exc)),
                status_code=303,
            )
        return RedirectResponse(
            url=f"/admin/tenants/{tenant_id}?invite_token={urllib.parse.quote(token)}",
            status_code=303,
        )

    @router.post("/invites/{invite_id}/revoke")
    def withdraw_invite(
        invite_id: str,
        tenant_id: str = Form(...),
        admin=Depends(admin_dep),
    ):
        try:
            revoke_invite(control_engine, invite_id, tenant_id=tenant_id)
        except MembershipError as exc:
            return RedirectResponse(
                url=f"/admin/tenants/{tenant_id}?error=" + urllib.parse.quote(str(exc)),
                status_code=303,
            )
        return RedirectResponse(
            url=f"/admin/tenants/{tenant_id}?message="
            + urllib.parse.quote("Invitation withdrawn."),
            status_code=303,
        )

    @router.post("/tenants/{tenant_id}/members/{membership_id}/role")
    def change_role(
        tenant_id: str,
        membership_id: str,
        role: str = Form(...),
        admin=Depends(admin_dep),
    ):
        try:
            change_member_role(control_engine, membership_id, role, tenant_id=tenant_id)
        except MembershipError as exc:
            return RedirectResponse(
                url=f"/admin/tenants/{tenant_id}?error=" + urllib.parse.quote(str(exc)),
                status_code=303,
            )
        return RedirectResponse(
            url=f"/admin/tenants/{tenant_id}?message=" + urllib.parse.quote("Role updated."),
            status_code=303,
        )

    @router.post("/tenants/{tenant_id}/members/{membership_id}/remove")
    def remove_membership(
        tenant_id: str,
        membership_id: str,
        admin=Depends(admin_dep),
    ):
        try:
            remove_member(control_engine, membership_id, tenant_id=tenant_id)
        except MembershipError as exc:
            return RedirectResponse(
                url=f"/admin/tenants/{tenant_id}?error=" + urllib.parse.quote(str(exc)),
                status_code=303,
            )
        return RedirectResponse(
            url=f"/admin/tenants/{tenant_id}?message=" + urllib.parse.quote("Access removed."),
            status_code=303,
        )

    return router
