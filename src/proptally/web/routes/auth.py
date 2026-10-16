"""Sign-in, registration, invitations, password reset and the account screen.

These are the only routes that run without a portfolio context, so they are mounted
without the shared authentication dependency and each one states what it needs.
Their forms carry a pre-session CSRF token (see ``web.auth``) because no session
exists yet to lend them one.
"""

from __future__ import annotations

import urllib.parse
from typing import Optional

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlmodel import Session, select

from proptally.db.system_models import ROLE_OWNER
from proptally.db.tenant_manager import TenantManager
from proptally.web.auth import (
    ROLES,
    AuthenticationDependency,
    LoginRateLimiter,
    MembershipError,
    ROLE_CPA_REVIEWER,
    ROLE_DESCRIPTIONS,
    accept_invite,
    change_member_role,
    pending_invites,
    remove_member,
    revoke_invite,
    tenant_members,
    authenticate,
    change_password,
    create_invite,
    find_invite,
    issue_password_reset,
    issue_pre_session_csrf,
    issue_session,
    clear_session_cookie,
    consume_password_reset,
    cookie_name,
    memberships_for,
    pre_session_csrf_valid,
    register_portfolio,
    registration_is_open,
    resolve_context,
    revoke_session,
    set_pre_session_csrf_cookie,
    set_session_cookie,
    switch_active_tenant,
    tenant_for,
)

MIN_PASSWORD_LENGTH = 10


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _safe_next(next_url: Optional[str]) -> str:
    """Only ever redirect to a path on this host."""
    if next_url and next_url.startswith("/") and not next_url.startswith("//"):
        return next_url
    return "/"


def _render(templates, request: Request, name: str, context: dict, *, csrf_token: str, set_cookie: bool):
    """Renders an unauthenticated page, issuing the pre-session CSRF cookie once."""
    response = templates.TemplateResponse(
        request=request, name=name, context={"csrf_token": csrf_token, **context}
    )
    if set_cookie:
        set_pre_session_csrf_cookie(response, csrf_token, request)
    return response


def get_auth_router(templates, control_engine, tenant_manager: TenantManager, rate_limiter: LoginRateLimiter, auth_dep: AuthenticationDependency):
    router = APIRouter()

    # --- Sign in ---------------------------------------------------------

    @router.get("/auth/login", response_class=HTMLResponse)
    def login_form(request: Request, next: Optional[str] = None):
        context = resolve_context(
            control_engine, tenant_manager, request.cookies.get(cookie_name())
        )
        if context is not None:
            return RedirectResponse(url=_safe_next(next), status_code=303)
        csrf_token, set_cookie = issue_pre_session_csrf(request)
        return _render(
            templates, request, "login.html",
            {"error": None, "email": "", "next_url": _safe_next(next),
             "registration_open": registration_is_open(control_engine)},
            csrf_token=csrf_token, set_cookie=set_cookie,
        )

    @router.post("/auth/login")
    def login_submit(
        request: Request,
        email: str = Form(...),
        password: str = Form(...),
        csrf_token: str = Form(None),
        next: str = Form("/"),
    ):
        csrf_token, set_cookie = issue_pre_session_csrf(request)
        if not pre_session_csrf_valid(request, csrf_token):
            return _render(
                templates, request, "login.html",
                {"error": "Your sign-in form expired. Please try again.", "email": email,
                 "next_url": _safe_next(next), "registration_open": registration_is_open(control_engine)},
                csrf_token=csrf_token, set_cookie=set_cookie,
            )

        ip = _client_ip(request)
        if rate_limiter.is_blocked(ip, email):
            return _render(
                templates, request, "login.html",
                {"error": "Too many failed attempts. Wait a few minutes and try again.",
                 "email": email, "next_url": _safe_next(next),
                 "registration_open": registration_is_open(control_engine)},
                csrf_token=csrf_token, set_cookie=set_cookie,
            )

        user = authenticate(control_engine, email, password)
        if user is None:
            rate_limiter.record_failure(ip, email)
            return _render(
                templates, request, "login.html",
                {"error": "Email or password is incorrect.", "email": email,
                 "next_url": _safe_next(next), "registration_open": registration_is_open(control_engine)},
                csrf_token=csrf_token, set_cookie=set_cookie,
            )

        rate_limiter.clear(ip, email)
        memberships = memberships_for(control_engine, user.id)
        if not memberships:
            return _render(
                templates, request, "login.html",
                {"error": "This account is not a member of any portfolio.",
                 "email": email, "next_url": "/", "registration_open": registration_is_open(control_engine)},
                csrf_token=csrf_token, set_cookie=set_cookie,
            )

        token, _ = issue_session(
            control_engine, user, memberships[0].tenant_id,
            ip_address=ip, user_agent=request.headers.get("user-agent"),
        )
        response = RedirectResponse(url=_safe_next(next), status_code=303)
        set_session_cookie(response, token, request)
        return response

    @router.post("/auth/logout")
    def logout(request: Request, csrf_token: str = Form(None)):
        token = request.cookies.get(cookie_name())
        # Require the token so a hostile page cannot sign the user out silently.
        context = resolve_context(control_engine, tenant_manager, token)
        if context is not None:
            presented = csrf_token or request.headers.get("x-csrf-token")
            if presented and presented == context.csrf_token:
                revoke_session(control_engine, token)
        elif token:
            revoke_session(control_engine, token)
        response = RedirectResponse(url="/auth/login", status_code=303)
        clear_session_cookie(response)
        return response

    # --- Registration ----------------------------------------------------

    @router.get("/auth/register", response_class=HTMLResponse)
    def register_form(request: Request):
        if not registration_is_open(control_engine):
            return RedirectResponse(url="/auth/login", status_code=303)
        csrf_token, set_cookie = issue_pre_session_csrf(request)
        return _render(
            templates, request, "register.html",
            {"error": None, "email": "", "display_name": "", "portfolio_name": "",
             "min_password_length": MIN_PASSWORD_LENGTH},
            csrf_token=csrf_token, set_cookie=set_cookie,
        )

    @router.post("/auth/register")
    def register_submit(
        request: Request,
        email: str = Form(...),
        password: str = Form(...),
        display_name: str = Form(""),
        portfolio_name: str = Form(""),
        csrf_token: str = Form(None),
    ):
        csrf_token, set_cookie = issue_pre_session_csrf(request)

        def failure(message: str):
            return _render(
                templates, request, "register.html",
                {"error": message, "email": email, "display_name": display_name,
                 "portfolio_name": portfolio_name, "min_password_length": MIN_PASSWORD_LENGTH},
                csrf_token=csrf_token, set_cookie=set_cookie,
            )

        if not registration_is_open(control_engine):
            return RedirectResponse(url="/auth/login", status_code=303)
        if not pre_session_csrf_valid(request, csrf_token):
            return failure("Your form expired. Please try again.")

        try:
            user, tenant, _ = register_portfolio(
                control_engine, tenant_manager,
                email=email, password=password,
                display_name=display_name, portfolio_name=portfolio_name,
            )
        except ValueError as exc:
            return failure(str(exc))

        token, _ = issue_session(
            control_engine, user, tenant.id,
            ip_address=_client_ip(request), user_agent=request.headers.get("user-agent"),
        )
        response = RedirectResponse(url="/", status_code=303)
        set_session_cookie(response, token, request)
        return response

    # --- Invitations -----------------------------------------------------

    @router.get("/auth/invite/{token}", response_class=HTMLResponse)
    def invite_form(token: str, request: Request):
        invite = find_invite(control_engine, token)
        if invite is None:
            return _render(
                templates, request, "login.html",
                {"error": "This invitation is invalid, already used, or has expired.",
                 "email": "", "next_url": "/", "registration_open": registration_is_open(control_engine)},
                csrf_token=issue_pre_session_csrf(request)[0],
                set_cookie=issue_pre_session_csrf(request)[1],
            )
        tenant = tenant_for(control_engine, invite.tenant_id)
        csrf_token, set_cookie = issue_pre_session_csrf(request)
        return _render(
            templates, request, "invite.html",
            {"error": None, "token": token, "invite_email": invite.email, "invite_role": invite.role,
             "invite_tenant_name": tenant.name if tenant else "this portfolio",
             "display_name": "", "min_password_length": MIN_PASSWORD_LENGTH},
            csrf_token=csrf_token, set_cookie=set_cookie,
        )

    @router.post("/auth/invite/{token}")
    def invite_submit(
        token: str,
        request: Request,
        password: str = Form(...),
        display_name: str = Form(""),
        csrf_token: str = Form(None),
    ):
        csrf_token, set_cookie = issue_pre_session_csrf(request)
        invite = find_invite(control_engine, token)
        if invite is None:
            return _render(
                templates, request, "login.html",
                {"error": "This invitation is invalid, already used, or has expired.",
                 "email": "", "next_url": "/", "registration_open": registration_is_open(control_engine)},
                csrf_token=csrf_token, set_cookie=set_cookie,
            )
        if not pre_session_csrf_valid(request, csrf_token):
            return _render(
                templates, request, "invite.html",
                {"error": "Your form expired. Please try again.", "token": token,
                 "invite_email": invite.email, "invite_role": invite.role,
                 "invite_tenant_name": "", "display_name": display_name,
                 "min_password_length": MIN_PASSWORD_LENGTH},
                csrf_token=csrf_token, set_cookie=set_cookie,
            )
        try:
            user, tenant, _ = accept_invite(
                control_engine, tenant_manager, token=token,
                password=password, display_name=display_name,
            )
        except ValueError as exc:
            return _render(
                templates, request, "invite.html",
                {"error": str(exc), "token": token, "invite_email": invite.email,
                 "invite_role": invite.role, "invite_tenant_name": "",
                 "display_name": display_name, "min_password_length": MIN_PASSWORD_LENGTH},
                csrf_token=csrf_token, set_cookie=set_cookie,
            )
        session_token, _ = issue_session(
            control_engine, user, tenant.id,
            ip_address=_client_ip(request), user_agent=request.headers.get("user-agent"),
        )
        response = RedirectResponse(url="/", status_code=303)
        set_session_cookie(response, session_token, request)
        return response

    # --- Password reset --------------------------------------------------

    @router.get("/auth/reset", response_class=HTMLResponse)
    def reset_form(request: Request):
        csrf_token, set_cookie = issue_pre_session_csrf(request)
        return _render(
            templates, request, "reset.html",
            {"error": None, "issued_token": None, "submitted": False},
            csrf_token=csrf_token, set_cookie=set_cookie,
        )

    @router.post("/auth/reset")
    def reset_submit(request: Request, email: str = Form(...), csrf_token: str = Form(None)):
        csrf_token, set_cookie = issue_pre_session_csrf(request)
        if not pre_session_csrf_valid(request, csrf_token):
            return _render(
                templates, request, "reset.html",
                {"error": "Your form expired. Please try again.", "issued_token": None, "submitted": False},
                csrf_token=csrf_token, set_cookie=set_cookie,
            )
        issued = issue_password_reset(control_engine, email)
        if issued is not None:
            # This deployment has no mail transport, so the token is surfaced to the
            # operator rather than silently dropped. It is still only shown here; the
            # response for an unknown address is identical in shape.
            return _render(
                templates, request, "reset.html",
                {"error": None, "issued_token": issued, "submitted": True},
                csrf_token=csrf_token, set_cookie=set_cookie,
            )
        return _render(
            templates, request, "reset.html",
            {"error": None, "issued_token": None, "submitted": True},
            csrf_token=csrf_token, set_cookie=set_cookie,
        )

    @router.get("/auth/reset/{token}", response_class=HTMLResponse)
    def reset_password_form(token: str, request: Request):
        csrf_token, set_cookie = issue_pre_session_csrf(request)
        return _render(
            templates, request, "reset_password.html",
            {"error": None, "token": token, "min_password_length": MIN_PASSWORD_LENGTH},
            csrf_token=csrf_token, set_cookie=set_cookie,
        )

    @router.post("/auth/reset/{token}")
    def reset_password_submit(
        token: str, request: Request,
        password: str = Form(...), csrf_token: str = Form(None),
    ):
        csrf_token, set_cookie = issue_pre_session_csrf(request)
        if not pre_session_csrf_valid(request, csrf_token):
            return _render(
                templates, request, "reset_password.html",
                {"error": "Your form expired. Please try again.", "token": token,
                 "min_password_length": MIN_PASSWORD_LENGTH},
                csrf_token=csrf_token, set_cookie=set_cookie,
            )
        try:
            consume_password_reset(control_engine, token, password)
        except ValueError as exc:
            return _render(
                templates, request, "reset_password.html",
                {"error": str(exc), "token": token, "min_password_length": MIN_PASSWORD_LENGTH},
                csrf_token=csrf_token, set_cookie=set_cookie,
            )
        response = RedirectResponse(url="/auth/login", status_code=303)
        clear_session_cookie(response)
        return response

    # --- Signed-in account screen ---------------------------------------

    @router.get("/account", response_class=HTMLResponse)
    def account_screen(request: Request, context=Depends(auth_dep)):
        """The signed-in account: its portfolios, who can reach this one, and its access list."""
        memberships = []
        for membership in memberships_for(control_engine, context.user.id):
            tenant = tenant_for(control_engine, membership.tenant_id)
            if tenant is not None:
                memberships.append({"tenant_id": tenant.id, "name": tenant.name, "role": membership.role})

        members = tenant_members(control_engine, context.tenant.id)
        invites = pending_invites(control_engine, context.tenant.id)
        owners = [m for m in members if m["role"] == ROLE_OWNER]

        return templates.TemplateResponse(
            request=request,
            name="account.html",
            context={
                "active_page": "account",
                "context": context,
                "memberships": memberships,
                "members": members,
                "pending_invites": invites,
                "roles": ROLES,
                "role_descriptions": ROLE_DESCRIPTIONS,
                # Surfaced so the screen can explain why a control is unavailable rather
                # than let it fail when used: the portfolio must keep an owner.
                "owner_count": len(owners),
                "invite_token": request.query_params.get("invite_token"),
                "message": request.query_params.get("message"),
                "error": request.query_params.get("error"),
                "csrf_token": context.csrf_token,
                "min_password_length": MIN_PASSWORD_LENGTH,
            },
        )

    @router.post("/account/password")
    def account_change_password(
        request: Request,
        current_password: str = Form(...),
        new_password: str = Form(...),
        context=Depends(auth_dep),
    ):
        context.require_mutation()
        try:
            change_password(control_engine, context.user, current_password, new_password)
        except ValueError as exc:
            return RedirectResponse(url=f"/account?error={exc}", status_code=303)
        response = RedirectResponse(
            url="/auth/login?next=/account", status_code=303
        )
        clear_session_cookie(response)
        return response

    @router.post("/account/invites")
    def account_create_invite(
        request: Request,
        email: str = Form(...),
        role: str = Form(ROLE_CPA_REVIEWER),
        context=Depends(auth_dep),
    ):
        context.require_admin()
        try:
            _, token = create_invite(
                control_engine,
                tenant_id=context.tenant.id,
                email=email,
                role=role,
                invited_by_user_id=context.user.id,
            )
        except ValueError as exc:
            return RedirectResponse(url=f"/account?error={exc}", status_code=303)
        return RedirectResponse(url=f"/account?invite_token={token}", status_code=303)

    @router.post("/account/members/{membership_id}/role")
    def account_change_member_role(
        membership_id: str,
        role: str = Form(...),
        context=Depends(auth_dep),
    ):
        """Changes what a member may do in this portfolio. Owners only."""
        context.require_admin()
        try:
            change_member_role(
                control_engine, membership_id, role, tenant_id=context.tenant.id
            )
        except MembershipError as exc:
            return RedirectResponse(url=f"/account?error={urllib.parse.quote(str(exc))}", status_code=303)
        return RedirectResponse(
            url=f"/account?message={urllib.parse.quote('Role updated.')}", status_code=303
        )

    @router.post("/account/members/{membership_id}/remove")
    def account_remove_member(membership_id: str, context=Depends(auth_dep)):
        """Removes somebody's access to this portfolio. Their account survives."""
        context.require_admin()
        try:
            remove_member(control_engine, membership_id, tenant_id=context.tenant.id)
        except MembershipError as exc:
            return RedirectResponse(url=f"/account?error={urllib.parse.quote(str(exc))}", status_code=303)
        return RedirectResponse(
            url=f"/account?message={urllib.parse.quote('Access removed.')}", status_code=303
        )

    @router.post("/account/invites/{invite_id}/revoke")
    def account_revoke_invite(invite_id: str, context=Depends(auth_dep)):
        """Withdraws an unaccepted invitation, so its link stops working."""
        context.require_admin()
        try:
            revoke_invite(control_engine, invite_id, tenant_id=context.tenant.id)
        except MembershipError as exc:
            return RedirectResponse(url=f"/account?error={urllib.parse.quote(str(exc))}", status_code=303)
        return RedirectResponse(
            url=f"/account?message={urllib.parse.quote('Invitation withdrawn.')}", status_code=303
        )

    @router.post("/auth/switch-tenant")
    def switch_tenant(
        request: Request,
        tenant_id: str = Form(...),
        context=Depends(auth_dep),
    ):
        allowed = {m.tenant_id for m in memberships_for(control_engine, context.user.id)}
        if tenant_id not in allowed:
            # A user may only switch to a portfolio they are a member of; never
            # let the posted value choose which database to open.
            return RedirectResponse(url="/account?error=Not a member of that portfolio", status_code=303)
        switch_active_tenant(control_engine, request.cookies.get(cookie_name()), tenant_id)
        return RedirectResponse(url="/", status_code=303)

    return router
