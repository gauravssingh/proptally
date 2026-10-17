"""Sign-in, registration, invitations, password reset and the account screen.

These are the only routes that run without a portfolio context, so they are mounted
without the shared authentication dependency and each one states what it needs.
Their forms carry a pre-session CSRF token (see ``web.auth``) because no session
exists yet to lend them one.
"""

from __future__ import annotations

import urllib.parse

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from proptally.db.portfolio_lifecycle import portfolio_status
from proptally.db.system_models import (
    ADMIN_ROLES,
    CLIENT_CONFIRMATION_ROLES,
    CPA_REVIEW_ROLES,
    EXTERNAL_REVIEW_ROLES,
    INVITABLE_ROLES,
    ROLE_OWNER,
    WORK_ROLES,
)
from proptally.db.tenant_manager import TenantManager
from proptally.web.auth import (
    ROLE_CPA_REVIEWER,
    ROLE_DESCRIPTIONS,
    ROLE_PICKER_LABELS,
    ROLES,
    AuthenticationDependency,
    LoginRateLimiter,
    MembershipError,
    NoActivePortfolio,
    accept_invite,
    accept_onboarding_invite,
    authenticate,
    change_member_role,
    change_password,
    clear_session_cookie,
    consume_password_reset,
    cookie_name,
    create_invite,
    find_invite,
    find_onboarding_invite,
    find_reset_token,
    issue_pre_session_csrf,
    issue_session,
    memberships_for,
    pending_invites,
    portfolio_code_matches,
    portfolio_invite_code,
    pre_session_csrf_valid,
    register_portfolio,
    registration_is_open,
    registration_requires_code,
    remove_member,
    resolve_context,
    revoke_invite,
    revoke_session,
    set_pre_session_csrf_cookie,
    set_session_cookie,
    switch_active_tenant,
    tenant_for,
    tenant_members,
)
from proptally.web.routes.portfolios import DELETE_ROLES, EXPORT_ROLES

MIN_PASSWORD_LENGTH = 10

#: The administration screen's sections. Each one is a query parameter rather than
#: client-side state, so it is a real URL: bookmarkable, reloadable, and reachable with the
#: browser's back button (`docs/ui-ux/05_INTERACTION_STANDARDS.md` §5).
ACCOUNT_SECTION_PEOPLE = "people"
ACCOUNT_SECTION_PORTFOLIO = "portfolio"
ACCOUNT_SECTION_BACKUP = "backup"
ACCOUNT_SECTION_SIGNIN = "signin"
ACCOUNT_SECTIONS = (
    ACCOUNT_SECTION_PEOPLE,
    ACCOUNT_SECTION_PORTFOLIO,
    ACCOUNT_SECTION_BACKUP,
    ACCOUNT_SECTION_SIGNIN,
)

#: What each role may do, as capability rows: (capability, what it means, the roles that
#: hold it). Every row's role list is the same tuple the gate uses — `WORK_ROLES`,
#: `ADMIN_ROLES`, `CLIENT_CONFIRMATION_ROLES`, `CPA_REVIEW_ROLES`, `EXTERNAL_REVIEW_ROLES`
#: and the export/removal sets the portfolio routes enforce — so the screen cannot describe
#: authority the application does not carry out. The prose this replaces lived in
#: `ROLE_DESCRIPTIONS` and drifted from the tuples.
ROLE_CAPABILITIES = (
    (
        "Read the books",
        "The ledger, documents, statements and reports.",
        ROLES,
    ),
    (
        "Do the bookkeeping",
        "Import statements, allocate and post transactions, set up properties and accounts. "
        "A reviewer's posting is the entry that records their review.",
        WORK_ROLES,
    ),
    (
        "Tick the external-review control",
        "Record that a deduction was externally reviewed.",
        EXTERNAL_REVIEW_ROLES,
    ),
    (
        "Be the verified reviewer",
        "A reviewer's tick is recorded as a CPA's review; an owner's is recorded as the "
        "owner's own attestation, never as a CPA's.",
        CPA_REVIEW_ROLES,
    ),
    (
        "Manage people",
        "Invite, change roles, withdraw invitations and remove access.",
        ADMIN_ROLES,
    ),
    (
        "Authorize on the client's behalf",
        "CBD-1: the client's own confirmation. A manager cannot give it, however much work "
        "they do.",
        CLIENT_CONFIRMATION_ROLES,
    ),
    (
        "Export or remove the portfolio",
        "Download the whole portfolio, or delete it and its files.",
        tuple(role for role in ROLES if role in EXPORT_ROLES and role in DELETE_ROLES),
    ),
)


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _safe_next(next_url: str | None) -> str:
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


def get_auth_router(
    templates,
    control_engine,
    tenant_manager: TenantManager,
    rate_limiter: LoginRateLimiter,
    auth_dep: AuthenticationDependency,
    session_dep: AuthenticationDependency = None,
):
    router = APIRouter()
    # Operations about the signed-in person themselves -- changing a password, choosing a
    # portfolio -- authenticate and check CSRF but do not require a role in the portfolio:
    # a read-only member still owns their own account.
    session_scope = session_dep or auth_dep

    # --- Sign in ---------------------------------------------------------

    def _render_login(
        request: Request,
        *,
        error_kind: str | None = None,
        reset_error: str | None = None,
        pane: str | None = None,
        email: str = "",
        next_url: str = "/",
    ):
        """Render the sign-in box, optionally with a verdict on the last attempt.

        The route names what happened (``error_kind``); the template owns the words and
        the recovery actions, so a failure reads the same wherever it is raised.
        """
        csrf_token, set_cookie = issue_pre_session_csrf(request)
        return _render(
            templates, request, "login.html",
            {
                "error_kind": error_kind,
                "reset_error": reset_error,
                "pane": pane,
                "email": email,
                "next_url": next_url,
                "registration_open": registration_is_open(control_engine),
                "code_required": registration_requires_code(control_engine),
            },
            csrf_token=csrf_token, set_cookie=set_cookie,
        )

    @router.get("/auth/login", response_class=HTMLResponse)
    def login_form(request: Request, next: str | None = None):
        try:
            context = resolve_context(
                control_engine, tenant_manager, request.cookies.get(cookie_name())
            )
        except NoActivePortfolio:
            context = None
        if context is not None:
            return RedirectResponse(url=_safe_next(next), status_code=303)
        return _render_login(request, next_url=_safe_next(next))

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
            return _render_login(
                request, error_kind="expired", email=email, next_url=_safe_next(next)
            )

        ip = _client_ip(request)
        if rate_limiter.is_blocked(ip, email):
            return _render_login(
                request, error_kind="throttled", email=email, next_url=_safe_next(next)
            )

        user = authenticate(control_engine, email, password)
        if user is None:
            rate_limiter.record_failure(ip, email)
            return _render_login(
                request, error_kind="credentials", email=email, next_url=_safe_next(next)
            )

        rate_limiter.clear(ip, email)
        memberships = memberships_for(control_engine, user.id)
        if not memberships:
            return _render_login(request, error_kind="nomember", email=email)

        token, _ = issue_session(
            control_engine, user, memberships[0].tenant_id,
            ip_address=ip, user_agent=request.headers.get("user-agent"),
        )
        destination = _safe_next(next)
        if len(memberships) > 1:
            # Never silently drop a multi-portfolio user into the oldest portfolio.
            # The authenticated list lets them choose, then returns to the original
            # local destination in the newly selected session context.
            destination = "/portfolios?" + urllib.parse.urlencode(
                {"choose": "1", "next": destination}
            )
        response = RedirectResponse(url=destination, status_code=303)
        set_session_cookie(response, token, request)
        return response

    @router.post("/auth/logout")
    def logout(request: Request, csrf_token: str = Form(None)):
        token = request.cookies.get(cookie_name())
        # Require the token so a hostile page cannot sign the user out silently.
        try:
            context = resolve_context(control_engine, tenant_manager, token)
        except NoActivePortfolio:
            context = None
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

    def _render_register(
        request: Request,
        *,
        csrf_token: str,
        set_cookie: bool,
        error: str | None = None,
        email: str = "",
        display_name: str = "",
        portfolio_name: str = "",
    ):
        return _render(
            templates, request, "register.html",
            {"error": error, "email": email, "display_name": display_name,
             "portfolio_name": portfolio_name, "min_password_length": MIN_PASSWORD_LENGTH,
             "portfolio_code_required": registration_requires_code(control_engine)},
            csrf_token=csrf_token, set_cookie=set_cookie,
        )

    def _door_is_reachable() -> bool:
        """Whether this page is the way in at all.

        True while ordinary registration is open, and also when only a code opens the
        door — because this is the page that code is typed on. Closed only when neither
        applies, which is a host that is running and has closed its door.
        """
        return registration_is_open(control_engine) or registration_requires_code(control_engine)

    @router.get("/auth/register", response_class=HTMLResponse)
    def register_form(request: Request):
        if not _door_is_reachable():
            return RedirectResponse(url="/auth/login", status_code=303)
        csrf_token, set_cookie = issue_pre_session_csrf(request)
        return _render_register(request, csrf_token=csrf_token, set_cookie=set_cookie)

    @router.post("/auth/register")
    def register_submit(
        request: Request,
        email: str = Form(...),
        password: str = Form(...),
        display_name: str = Form(""),
        portfolio_name: str = Form(""),
        portfolio_code: str = Form(""),
        csrf_token: str = Form(None),
    ):
        csrf_token, set_cookie = issue_pre_session_csrf(request)

        def failure(message: str):
            return _render_register(
                request, csrf_token=csrf_token, set_cookie=set_cookie, error=message,
                email=email, display_name=display_name, portfolio_name=portfolio_name,
            )

        if not _door_is_reachable():
            return RedirectResponse(url="/auth/login", status_code=303)
        if not pre_session_csrf_valid(request, csrf_token):
            return failure("Your form expired. Please try again.")

        # A code is either a single-use one minted in the console or the older
        # single-secret gate, and the two are interchangeable to the person typing it.
        # Where a code is required, accepting neither is a refusal; where none is
        # required, an empty field is simply an empty field.
        invite = find_onboarding_invite(control_engine, portfolio_code)
        global_code_ok = bool(portfolio_invite_code()) and portfolio_code_matches(portfolio_code)
        if registration_requires_code(control_engine) and invite is None and not global_code_ok:
            return failure("That portfolio invitation code is not right.")

        try:
            if invite is not None:
                user, tenant, _ = accept_onboarding_invite(
                    control_engine, tenant_manager,
                    token=portfolio_code, email=email, password=password,
                    display_name=display_name, portfolio_name=portfolio_name,
                )
            else:
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

    # --- Onboarding codes (a new portfolio) ------------------------------

    def _render_join(
        request: Request,
        *,
        code: str = "",
        error: str | None = None,
        email: str = "",
        display_name: str = "",
        portfolio_name: str = "",
        hint: str | None = None,
        pinned_email: str | None = None,
    ):
        """Render the redemption form. The code rides in a hidden field so a failure
        keeps the holder on the same page rather than sending them back to retype it."""
        csrf_token, set_cookie = issue_pre_session_csrf(request)
        return _render(
            templates, request, "join.html",
            {
                "code": code,
                "error": error,
                "email": pinned_email or email,
                "pinned_email": pinned_email,
                "display_name": display_name,
                "portfolio_name": portfolio_name or (hint or ""),
                "min_password_length": MIN_PASSWORD_LENGTH,
            },
            csrf_token=csrf_token, set_cookie=set_cookie,
        )

    @router.get("/auth/join", response_class=HTMLResponse)
    def join_form(request: Request, code: str = ""):
        """Where an onboarding code starts a portfolio.

        Deliberately not gated by ``registration_is_open``: the code *is* the permission,
        which is what lets an operator hand out one portfolio at a time on a live
        instance that has long since closed ordinary registration.

        With no code in the link this is still a usable page — the field is shown rather
        than an error — so a person who was told "you have an invitation code" has
        somewhere to put it.
        """
        invite = find_onboarding_invite(control_engine, code) if code.strip() else None
        if invite is None:
            return _render_join(
                request,
                code=code,
                error=("That invitation code is invalid, already used, or has expired."
                       if code.strip() else None),
            )
        return _render_join(
            request, code=code, hint=invite.portfolio_name_hint, pinned_email=invite.email
        )

    @router.post("/auth/join")
    def join_submit(
        request: Request,
        code: str = Form(""),
        email: str = Form(""),
        password: str = Form(""),
        display_name: str = Form(""),
        portfolio_name: str = Form(""),
        csrf_token: str = Form(None),
    ):
        invite = find_onboarding_invite(control_engine, code)
        if invite is None:
            return _render_join(
                request, code=code,
                error="That invitation code is invalid, already used, or has expired.",
            )
        if not pre_session_csrf_valid(request, csrf_token):
            return _render_join(
                request, code=code, hint=invite.portfolio_name_hint,
                pinned_email=invite.email, display_name=display_name,
                portfolio_name=portfolio_name, error="Your form expired. Please try again.",
            )
        try:
            user, tenant, _ = accept_onboarding_invite(
                control_engine, tenant_manager,
                token=code, email=email, password=password,
                display_name=display_name, portfolio_name=portfolio_name,
            )
        except ValueError as exc:
            # A refusal never spends the code: assert that here rather than by comment.
            return _render_join(
                request, code=code, hint=invite.portfolio_name_hint,
                pinned_email=invite.email, email=email, display_name=display_name,
                portfolio_name=portfolio_name, error=str(exc),
            )

        token, _ = issue_session(
            control_engine, user, tenant.id,
            ip_address=_client_ip(request), user_agent=request.headers.get("user-agent"),
        )
        response = RedirectResponse(url="/", status_code=303)
        set_session_cookie(response, token, request)
        return response

    # --- Invitations -----------------------------------------------------

    def _render_invite(
        request: Request,
        token: str,
        invite,
        *,
        error: str | None = None,
        display_name: str = "",
    ):
        """Render the invitation as its holder sees it.

        Everything the page says about the invitation is read from the row here, so a
        re-render after a refused attempt cannot lose the portfolio it is for.
        """
        tenant = tenant_for(control_engine, invite.tenant_id)
        csrf_token, set_cookie = issue_pre_session_csrf(request)
        return _render(
            templates, request, "invite.html",
            {"error": error, "token": token, "invite_email": invite.email,
             "invite_role": invite.role,
             "invite_role_description": ROLE_DESCRIPTIONS.get(invite.role, ""),
             "invite_tenant_name": tenant.name if tenant else "this portfolio",
             "invite_expires_at": invite.expires_at,
             "display_name": display_name, "min_password_length": MIN_PASSWORD_LENGTH},
            csrf_token=csrf_token, set_cookie=set_cookie,
        )

    def _invite_is_gone(request: Request):
        """A spent, withdrawn or expired token lands on sign-in with the reason stated.

        Rendered through _render_login so an unusable invitation reads like every other
        failure, and so the pre-session CSRF cookie is issued exactly once: issuing it
        twice on a cookie-less request renders one token beside a different cookie, and
        the reader's next submission is then refused as expired.
        """
        return _render_login(request, error_kind="invite_dead")

    @router.get("/auth/invite/{token}", response_class=HTMLResponse)
    def invite_form(token: str, request: Request):
        invite = find_invite(control_engine, token)
        if invite is None:
            return _invite_is_gone(request)
        return _render_invite(request, token, invite)

    @router.post("/auth/invite/{token}")
    def invite_submit(
        token: str,
        request: Request,
        password: str = Form(...),
        display_name: str = Form(""),
        csrf_token: str = Form(None),
    ):
        csrf_token, _ = issue_pre_session_csrf(request)
        invite = find_invite(control_engine, token)
        if invite is None:
            return _invite_is_gone(request)
        if not pre_session_csrf_valid(request, csrf_token):
            return _render_invite(
                request, token, invite,
                error="Your form expired. Please try again.",
                display_name=display_name,
            )
        try:
            user, tenant, _ = accept_invite(
                control_engine, tenant_manager, token=token,
                password=password, display_name=display_name,
            )
        except ValueError as exc:
            return _render_invite(
                request, token, invite, error=str(exc), display_name=display_name
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
        """Explains how to get a reset token, for a reader who arrived here directly.

        Reset tokens are never issued to an unauthenticated web requester: this
        self-hosted deployment has no mailer, so the super admin creates the
        one-time link locally and hands it to the account holder out of band.
        """
        return templates.TemplateResponse(request=request, name="reset.html", context={})

    @router.post("/auth/reset/token", response_class=HTMLResponse)
    def reset_token_submit(
        request: Request,
        token: str = Form(""),
        csrf_token: str = Form(None),
    ):
        """Check a pasted reset token, then hand the holder to the change-password screen.

        A form cannot build a path segment, so the token arrives as a field: a live one
        is forwarded to ``/auth/reset/<token>``, a dead one keeps the reader on the step
        with the reason. Neither answer says anything about any account.
        """
        if not pre_session_csrf_valid(request, csrf_token):
            return _render_login(
                request,
                pane="forgot",
                reset_error="Your form expired. Please try again.",
            )

        presented = token.strip()
        if presented and find_reset_token(control_engine, presented) is not None:
            return RedirectResponse(
                url="/auth/reset/" + urllib.parse.quote(presented, safe=""),
                status_code=303,
            )
        return _render_login(
            request,
            pane="forgot",
            reset_error=(
                "That token is not valid." if presented
                else "Enter the token your super admin gave you."
            ),
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

    # --- Administration screen ------------------------------------------

    @router.get("/account", response_class=HTMLResponse)
    def account_screen(
        request: Request,
        section: str = ACCOUNT_SECTION_PEOPLE,
        context=Depends(auth_dep),
    ):
        """Administration for the open portfolio: its people, its status and its backup.

        The section is a query parameter rather than client-side state, so every section is
        a real URL that can be bookmarked, reloaded and reached with the browser's back
        button, and the segmented navigation is ordinary links.
        """
        if section not in ACCOUNT_SECTIONS:
            section = ACCOUNT_SECTION_PEOPLE

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
                "section": section,
                "account_sections": [
                    (ACCOUNT_SECTION_PEOPLE, "People"),
                    (ACCOUNT_SECTION_PORTFOLIO, "Portfolio status"),
                    (ACCOUNT_SECTION_BACKUP, "Backup"),
                    (ACCOUNT_SECTION_SIGNIN, "Sign-in"),
                ],
                "memberships": memberships,
                "members": members,
                "pending_invites": invites,
                "roles": ROLES,
                # Both option lists are built here, from the model's own tuples, so a
                # template never has to assemble them and the invite form cannot offer a
                # role the invitation endpoint would refuse.
                "role_options": [{"value": role, "label": role} for role in ROLES],
                "invitable_role_options": [
                    {"value": role, "label": ROLE_PICKER_LABELS[role]}
                    for role in ROLES if role in INVITABLE_ROLES
                ],
                "role_capabilities": ROLE_CAPABILITIES,
                # Surfaced so the screen can explain why a control is unavailable rather
                # than let it fail when used: the portfolio must keep an owner.
                "owner_count": len(owners),
                "invite_token": request.query_params.get("invite_token"),
                "message": request.query_params.get("message"),
                "error": request.query_params.get("error"),
                "csrf_token": context.csrf_token,
                "min_password_length": MIN_PASSWORD_LENGTH,
                # Only the two sections that report it read the portfolio's database and
                # stat its files, so People and Sign-in do not pay for that work.
                "portfolio_status": (
                    portfolio_status(control_engine, tenant_manager, context.tenant.id)
                    if section in (ACCOUNT_SECTION_PORTFOLIO, ACCOUNT_SECTION_BACKUP)
                    else None
                ),
                # Each action's visibility mirrors the role set its route enforces, so the
                # screen never offers a control that would be refused (`04` §E: no dead
                # affordances). They are the same set today, and stay correct if they diverge.
                "can_export": context.role in EXPORT_ROLES,
                "can_remove_portfolio": context.role in DELETE_ROLES,
            },
        )

    @router.post("/account/password")
    def account_change_password(
        request: Request,
        current_password: str = Form(...),
        new_password: str = Form(...),
        context=Depends(session_scope),
    ):
        # Deliberately no role check: changing your own password is about you, not about
        # the portfolio, so a read-only member must be able to do it.
        try:
            change_password(control_engine, context.user, current_password, new_password)
        except ValueError as exc:
            return RedirectResponse(
                url=f"/account?section={ACCOUNT_SECTION_SIGNIN}&error={exc}", status_code=303
            )
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
            return RedirectResponse(
                url=f"/account?section={ACCOUNT_SECTION_PEOPLE}&error={exc}", status_code=303
            )
        return RedirectResponse(
            url=f"/account?section={ACCOUNT_SECTION_PEOPLE}&invite_token={token}", status_code=303
        )

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
            return RedirectResponse(
                url=f"/account?section={ACCOUNT_SECTION_PEOPLE}&error={urllib.parse.quote(str(exc))}",
                status_code=303,
            )
        return RedirectResponse(
            url=f"/account?section={ACCOUNT_SECTION_PEOPLE}&message={urllib.parse.quote('Role updated.')}",
            status_code=303,
        )

    @router.post("/account/members/{membership_id}/remove")
    def account_remove_member(membership_id: str, context=Depends(auth_dep)):
        """Removes somebody's access to this portfolio. Their account survives."""
        context.require_admin()
        try:
            remove_member(control_engine, membership_id, tenant_id=context.tenant.id)
        except MembershipError as exc:
            return RedirectResponse(
                url=f"/account?section={ACCOUNT_SECTION_PEOPLE}&error={urllib.parse.quote(str(exc))}",
                status_code=303,
            )
        return RedirectResponse(
            url=f"/account?section={ACCOUNT_SECTION_PEOPLE}&message={urllib.parse.quote('Access removed.')}",
            status_code=303,
        )

    @router.post("/account/invites/{invite_id}/revoke")
    def account_revoke_invite(invite_id: str, context=Depends(auth_dep)):
        """Withdraws an unaccepted invitation, so its link stops working."""
        context.require_admin()
        try:
            revoke_invite(control_engine, invite_id, tenant_id=context.tenant.id)
        except MembershipError as exc:
            return RedirectResponse(
                url=f"/account?section={ACCOUNT_SECTION_PEOPLE}&error={urllib.parse.quote(str(exc))}",
                status_code=303,
            )
        return RedirectResponse(
            url=f"/account?section={ACCOUNT_SECTION_PEOPLE}&message={urllib.parse.quote('Invitation withdrawn.')}",
            status_code=303,
        )

    @router.post("/auth/switch-tenant")
    def switch_tenant(
        request: Request,
        tenant_id: str = Form(...),
        return_to: str = Form("/"),
        context=Depends(session_scope),
    ):
        allowed = {m.tenant_id for m in memberships_for(control_engine, context.user.id)}
        if tenant_id not in allowed:
            # A user may only switch to a portfolio they belong to; never let the posted
            # value choose which database to open.
            return RedirectResponse(
                url="/portfolios?error=Not%20a%20member%20of%20that%20portfolio", status_code=303
            )
        switch_active_tenant(control_engine, request.cookies.get(cookie_name()), tenant_id)
        return RedirectResponse(url=_safe_next(return_to), status_code=303)

    return router
