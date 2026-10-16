"""Local authentication, sessions, CSRF and portfolio authorisation.

Everything here is self-hosted and dependency-light by design: passwords are hashed
with Argon2id (the algorithm named by ``docs/architecture/v2/06_SECURITY_AND_DEPLOYMENT.md``
§3.1), session cookies carry a random token whose digest is the only thing stored,
and cross-site request forgery is stopped by a per-session double-submit token that
every unsafe request must echo. No cloud identity provider is involved, so no
account data leaves the host.

Authorisation is deliberately coarse and explicit:

* ``OWNER`` administers a portfolio.
* ``CPA_REVIEWER`` may record the external review a Schedule E deduction needs --
  that attestation is the role's entire purpose -- but may not administer.
* ``READ_ONLY`` may read and export, never mutate.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import socket
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import timedelta
from typing import AsyncIterator, Optional
from uuid import uuid4

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import HTTPException, Request
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlmodel import Session, select
from proptally.db.system_models import (
    ADMIN_ROLES,
    CLIENT_CONFIRMATION_ROLES,
    CPA_REVIEW_ROLES,
    EXTERNAL_REVIEW_ROLES,
    INVITABLE_ROLES,
    MUTATING_ROLES,
    ROLE_MANAGER,
    ROLES,
    WORK_ROLES,
    ROLE_CPA_REVIEWER,
    ROLE_OWNER,
    ROLE_READ_ONLY,
    SYSTEM_TABLES,  # noqa: F401  (re-exported for callers building the schema)
    PasswordResetToken,
    Tenant,
    TenantMembership,
    TENANT_ACTIVE,
    TENANT_FAILED,
    TENANT_PENDING,
    TENANT_PROVISIONING,
    User,
    UserInvite,
    UserSession,
    utc_now,
)
from proptally.db.tenant_manager import TenantManager, TenantPaths, slugify

logger = logging.getLogger(__name__)

#: Session cookie name. HTTP-only, SameSite=Lax, and Secure when the deployment
#: terminates TLS in front of the app.
COOKIE_NAME = "pt_session"

#: How long a session lives, and how long an unstretched "remember me" cookie lasts.
SESSION_LIFETIME = timedelta(hours=12)
INVITE_LIFETIME = timedelta(days=7)
RESET_LIFETIME = timedelta(hours=2)

#: Login throttling: this many failures per window, per (address, email) pair.
LOGIN_ATTEMPT_LIMIT = 5
LOGIN_ATTEMPT_WINDOW_SECONDS = 15 * 60

#: Requests that change state and therefore require a CSRF token.
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

_HASHER = PasswordHasher()


ROLE_DESCRIPTIONS = {
    ROLE_OWNER: (
        "Administer people and invitations, do the bookkeeping, authorize on the client's "
        "behalf, export the portfolio and remove it."
    ),
    ROLE_MANAGER: (
        "Do bookkeeping and portfolio setup: import statements, allocate and post "
        "transactions, and manage properties, accounts and loans. Cannot manage people or "
        "provide the client's own confirmation."
    ),
    ROLE_CPA_REVIEWER: (
        "Do bookkeeping as needed and record a verified external review. Cannot manage "
        "people or provide the client's own confirmation."
    ),
    ROLE_READ_ONLY: "Read portfolio data only; cannot change it or export the portfolio.",
}

#: Short, scannable labels for the role picker. Full descriptions are shown on the
#: invitation-acceptance page and the capabilities matrix.
ROLE_PICKER_LABELS = {
    ROLE_MANAGER: "MANAGER — Bookkeeping + setup",
    ROLE_CPA_REVIEWER: "CPA_REVIEWER — Bookkeeping + verified review",
    ROLE_READ_ONLY: "READ_ONLY — View only",
}


class AuthenticationRequired(Exception):
    """Raised when a request has no valid session. Rendered as a redirect to /auth/login."""



class NoActivePortfolio(RuntimeError):
    """A valid account has memberships, but none currently has usable storage state."""


class AuthorizationError(PermissionError):
    """Raised when the authenticated role may not perform the requested change."""


# --- Passwords -------------------------------------------------------------

def hash_password(password: str) -> str:
    """Argon2id hash of a password."""
    return _HASHER.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    """Constant-time password check. Never raises on a malformed hash."""
    try:
        return _HASHER.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def password_is_acceptable(password: str) -> tuple[bool, str]:
    """Minimum password policy for a self-hosted instance."""
    if len(password) < 10:
        return False, "Password must be at least 10 characters."
    if password.strip() != password:
        return False, "Password must not begin or end with whitespace."
    return True, ""


# --- Tokens ----------------------------------------------------------------

def new_token() -> str:
    """A high-entropy token for a session cookie, invite link or reset link."""
    return secrets.token_urlsafe(32)


def token_digest(token: str) -> str:
    """What is stored for a token. A stolen database yields no usable tokens."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def tokens_match(stored_digest: str, presented: str) -> bool:
    """Constant-time comparison of a stored digest against a presented token."""
    return hmac.compare_digest(stored_digest, token_digest(presented))


# --- Request context -------------------------------------------------------

@dataclass(frozen=True)
class TenantContext:
    """Everything a request needs to act on exactly one portfolio."""

    user: User
    tenant: Tenant
    role: str
    session: UserSession
    engine: Engine
    paths: TenantPaths
    operation_id: Optional[str] = None

    @property
    def csrf_token(self) -> str:
        return self.session.csrf_token

    @property
    def is_owner(self) -> bool:
        return self.role == ROLE_OWNER

    @property
    def can_mutate(self) -> bool:
        """Whether this role may issue an unsafe request at all.

        The coarse gate the authentication dependency applies. It is deliberately wider
        than any single capability: a reviewer's one write is an attestation, and a
        manager's work is the bookkeeping, so each is narrowed again at the route.
        """
        return self.role in MUTATING_ROLES

    @property
    def can_work(self) -> bool:
        """Whether this role may do the bookkeeping: import, allocate, post, set up."""
        return self.role in WORK_ROLES

    @property
    def can_administer(self) -> bool:
        """Whether this role may administer the portfolio: people, roles, its fate."""
        return self.role in ADMIN_ROLES

    @property
    def can_confirm_client(self) -> bool:
        """CBD-1. Only the client authorizes on the client's own behalf."""
        return self.role in CLIENT_CONFIRMATION_ROLES

    @property
    def can_attest_cpa_review(self) -> bool:
        """Whether this role's review is recorded as a *verified* external review.

        Only a CPA reviewer's is. An owner may supply the same control, and the record
        shows that the owner attested their own deduction rather than that a CPA saw it.
        """
        return self.role in CPA_REVIEW_ROLES

    @property
    def can_supply_external_review(self) -> bool:
        """Whether this role may tick the external-review control at all."""
        return self.role in EXTERNAL_REVIEW_ROLES

    def require_mutation(self) -> None:
        if not self.can_mutate:
            raise AuthorizationError(
                f"Role {self.role} may read this portfolio but not change it."
            )

    def require_work(self) -> None:
        if not self.can_work:
            raise AuthorizationError(
                f"Role {self.role} may read this portfolio but not do its bookkeeping. "
                "An owner or a manager can."
            )

    def require_admin(self) -> None:
        if not self.can_administer:
            raise AuthorizationError(
                f"Role {self.role} may not administer this portfolio."
            )

    def require_client_confirmation(self) -> None:
        if not self.can_confirm_client:
            raise AuthorizationError(
                f"Role {self.role} cannot give the client's authorization. CBD-1 is the "
                "client's own decision, so only the portfolio owner can supply it."
            )

    def require_cpa_attestation(self) -> None:
        if not self.can_attest_cpa_review:
            raise AuthorizationError(
                f"Role {self.role} cannot record an external review. That is the CPA "
                "reviewer's role."
            )

    def actor_id(self) -> str:
        """Who to record as the author of an audited change."""
        return self.user.id

    def posting_actor(self) -> dict:
        """Keyword arguments naming this request as the actor of a posting.

        ``actor_verified_cpa_reviewer`` is True only for a member whose role is
        ``CPA_REVIEWER``. That role came from the control-plane membership the
        session resolved, which is what makes the sign-off verifiable: the domain
        service can then record a verified external review rather than an owner
        self-attestation, without ever trusting a value from the request body.
        """
        return {
            "actor_id": self.user.id,
            "actor_role": self.role,
            "actor_verified_cpa_reviewer": self.role == ROLE_CPA_REVIEWER,
        }


#: The context of the request being handled. Set by the dependency and read by
#: templates, so the shell can render the signed-in user without every route
#: having to thread it through its template context.
_current_context: ContextVar[Optional[TenantContext]] = ContextVar("proptally_context", default=None)


def current_context() -> Optional[TenantContext]:
    """The active request's portfolio context, or None outside a request."""
    return _current_context.get()


def set_current_context(context: Optional[TenantContext]) -> None:
    _current_context.set(context)


def posting_actor_kwargs() -> dict:
    """The actor keyword arguments for the active request's posting services.

    Falls back to the unauthenticated ``system`` actor outside a request (CLI,
    scripts, background jobs), so domain services keep their existing default.
    """
    context = current_context()
    if context is None:
        return {"actor_id": "system", "actor_role": None, "actor_verified_cpa_reviewer": False}
    return context.posting_actor()


# --- Login throttling ------------------------------------------------------

class LoginRateLimiter:
    """In-process throttle for password attempts.

    Keyed by address and email together, so one attacker cannot lock out an account
    by guessing at it from elsewhere, and a shared address cannot be used to spray
    many accounts. In-process state is appropriate here: this app is a single
    self-hosted service, and the limit is a speed bump, not an authorisation control.
    """

    def __init__(self, limit: int = LOGIN_ATTEMPT_LIMIT, window_seconds: int = LOGIN_ATTEMPT_WINDOW_SECONDS):
        self.limit = limit
        self.window_seconds = window_seconds
        self._failures: dict[tuple[str, str], list[float]] = {}

    def _prune(self, key: tuple[str, str], now: float) -> list[float]:
        recent = [t for t in self._failures.get(key, []) if now - t < self.window_seconds]
        if recent:
            self._failures[key] = recent
        else:
            self._failures.pop(key, None)
        return recent

    def is_blocked(self, ip_address: str, email: str) -> bool:
        now = time.monotonic()
        return len(self._prune((ip_address, email.lower()), now)) >= self.limit

    def record_failure(self, ip_address: str, email: str) -> None:
        now = time.monotonic()
        key = (ip_address, email.lower())
        recent = self._prune(key, now)
        recent.append(now)
        self._failures[key] = recent

    def clear(self, ip_address: str, email: str) -> None:
        self._failures.pop((ip_address, email.lower()), None)


# --- Accounts, portfolios and memberships ----------------------------------

def normalise_email(email: str) -> str:
    return email.strip().lower()


def find_user(engine: Engine, email: str) -> Optional[User]:
    with Session(engine) as session:
        return session.exec(select(User).where(User.email == normalise_email(email))).first()


def count_users(engine: Engine) -> int:
    with Session(engine) as session:
        return len(session.exec(select(User)).all())


def memberships_for(engine: Engine, user_id: str) -> list[TenantMembership]:
    with Session(engine) as session:
        return list(
            session.exec(
                select(TenantMembership)
                .where(TenantMembership.user_id == user_id)
                .order_by(TenantMembership.created_at)
            ).all()
        )


def tenant_for(engine: Engine, tenant_id: str) -> Optional[Tenant]:
    with Session(engine) as session:
        return session.get(Tenant, tenant_id)


# --- Who has access to a portfolio -----------------------------------------

class MembershipError(RuntimeError):
    """Raised when a change to a portfolio's access list is refused."""


def tenant_members(control_engine: Engine, tenant_id: str) -> list[dict]:
    """Everyone with access to this portfolio, oldest membership first.

    Returns plain rows rather than ORM objects so a template cannot accidentally walk
    into another portfolio's data through a relationship.
    """
    with Session(control_engine) as session:
        memberships = session.exec(
            select(TenantMembership)
            .where(TenantMembership.tenant_id == tenant_id)
            .order_by(TenantMembership.created_at)
        ).all()
        rows = []
        for membership in memberships:
            user = session.get(User, membership.user_id)
            rows.append({
                "membership_id": membership.id,
                "user_id": membership.user_id,
                "email": user.email if user else "(deleted account)",
                "display_name": user.display_name if user else "",
                "is_active": bool(user.is_active) if user else False,
                "role": membership.role,
                "joined_at": membership.created_at,
            })
        return rows


def pending_invites(control_engine: Engine, tenant_id: str) -> list[UserInvite]:
    """Invitations that have been issued and not yet accepted."""
    with Session(control_engine) as session:
        return list(session.exec(
            select(UserInvite).where(
                UserInvite.tenant_id == tenant_id,
                UserInvite.accepted_at.is_(None),
            ).order_by(UserInvite.created_at)
        ).all())


def _owner_count(session: Session, tenant_id: str, excluding: Optional[str] = None) -> int:
    """Owners of the portfolio, optionally ignoring one membership."""
    owners = session.exec(
        select(TenantMembership).where(
            TenantMembership.tenant_id == tenant_id,
            TenantMembership.role == ROLE_OWNER,
        )
    ).all()
    return len([m for m in owners if m.id != excluding])


def change_member_role(
    control_engine: Engine, membership_id: str, new_role: str, *, tenant_id: str
) -> None:
    """Changes what a member may do. The portfolio must keep at least one owner.

    A portfolio with no owner cannot be administered by anyone: no role changes, no
    invitations, no removal of its data. Demoting the last owner is therefore refused
    rather than performed.
    """
    if new_role not in ROLES:
        raise MembershipError(f"{new_role!r} is not a role.")

    with Session(control_engine) as session:
        membership = session.get(TenantMembership, membership_id)
        # The membership must belong to the portfolio the caller administers: the id
        # alone must never be enough to reach another portfolio's access list.
        if membership is None or membership.tenant_id != tenant_id:
            raise MembershipError("That member is not in this portfolio.")
        if membership.role == new_role:
            return
        if membership.role == ROLE_OWNER and new_role != ROLE_OWNER:
            if _owner_count(session, tenant_id, excluding=membership.id) == 0:
                raise MembershipError(
                    "This is the portfolio's only owner, so it cannot be demoted. "
                    "Make somebody else an owner first."
                )
        membership.role = new_role
        session.add(membership)
        session.commit()


def remove_member(control_engine: Engine, membership_id: str, *, tenant_id: str) -> None:
    """Removes somebody's access to a portfolio.

    Their account survives: they may still belong to other portfolios. The portfolio
    must keep at least one owner, so removing the last one is refused.
    """
    with Session(control_engine) as session:
        membership = session.get(TenantMembership, membership_id)
        if membership is None or membership.tenant_id != tenant_id:
            raise MembershipError("That member is not in this portfolio.")
        if membership.role == ROLE_OWNER and _owner_count(session, tenant_id, excluding=membership.id) == 0:
            raise MembershipError(
                "This is the portfolio's only owner. Transfer ownership before removing "
                "it, or remove the portfolio itself."
            )
        session.delete(membership)
        session.commit()

    logger.info(
        "Portfolio access removed: tenant=%s membership=%s user=%s",
        tenant_id, membership_id, membership.user_id,
    )


def revoke_invite(control_engine: Engine, invite_id: str, *, tenant_id: str) -> None:
    """Withdraws an invitation that has not been accepted, so its link stops working."""
    with Session(control_engine) as session:
        invite = session.get(UserInvite, invite_id)
        if invite is None or invite.tenant_id != tenant_id:
            raise MembershipError("That invitation is not for this portfolio.")
        if invite.accepted_at is not None:
            raise MembershipError("That invitation has already been accepted.")
        session.delete(invite)
        session.commit()


def unique_slug(engine: Engine, name: str) -> str:
    """A portfolio slug that is not already taken."""
    base = slugify(name)
    with Session(engine) as session:
        taken = {row for row in session.exec(select(Tenant.slug)).all()}
    if base not in taken:
        return base
    for suffix in range(2, 1000):
        candidate = f"{base}-{suffix}"
        if candidate not in taken:
            return candidate
    return f"{base}-{secrets.token_hex(3)}"



class TenantProvisioningError(ValueError):
    """The portfolio account exists but its tenant storage is not usable yet."""


def provision_tenant(
    control_engine: Engine,
    tenant_manager: TenantManager,
    tenant_id: str,
    *,
    recover_stale: bool = False,
) -> Tenant:
    """Provision or retry one tenant, activating it only after SQLite verifies cleanly."""
    with Session(control_engine) as session:
        tenant = session.get(Tenant, tenant_id)
        if tenant is None:
            raise TenantProvisioningError("Portfolio not found.")
        if tenant.status == TENANT_ACTIVE:
            tenant_manager.ensure_tenant_storage(tenant_id)
            return tenant
        allowed = (TENANT_PENDING, TENANT_FAILED)
        if recover_stale:
            allowed += (TENANT_PROVISIONING,)
        placeholders = ", ".join(f":s{i}" for i in range(len(allowed)))
        params = {f"s{i}": value for i, value in enumerate(allowed)}
        params.update({"tenant_id": tenant_id, "provisioning": TENANT_PROVISIONING})
        claimed = session.execute(text(
            f"UPDATE tenants SET status=:provisioning WHERE id=:tenant_id "
            f"AND status IN ({placeholders})"
        ), params)
        if claimed.rowcount != 1:
            raise TenantProvisioningError(
                f"Portfolio provisioning is already in progress or unavailable (state {tenant.status})."
            )
        session.commit()

    try:
        tenant_manager.provision_tenant_storage(tenant_id)
    except Exception as exc:
        with Session(control_engine) as session:
            session.execute(text(
                "UPDATE tenants SET status=:failed WHERE id=:tenant_id "
                "AND status=:provisioning"
            ), {"failed": TENANT_FAILED, "tenant_id": tenant_id,
                "provisioning": TENANT_PROVISIONING})
            session.commit()
        raise TenantProvisioningError(
            f"Portfolio storage provisioning failed and is safe to retry: {exc}"
        ) from exc

    with Session(control_engine) as session:
        activated = session.execute(text(
            "UPDATE tenants SET status=:active WHERE id=:tenant_id "
            "AND status=:provisioning"
        ), {"active": TENANT_ACTIVE, "tenant_id": tenant_id,
            "provisioning": TENANT_PROVISIONING})
        if activated.rowcount != 1:
            session.rollback()
            raise TenantProvisioningError(
                "Portfolio state changed during provisioning; storage remains inactive."
            )
        session.commit()
        tenant = session.get(Tenant, tenant_id)
        session.expunge(tenant)
        return tenant

def retry_incomplete_provisioning(
    control_engine: Engine, tenant_manager: TenantManager
) -> None:
    """Resume retryable PENDING/FAILED tenants before serving requests."""
    with Session(control_engine) as session:
        tenant_ids = session.exec(
            select(Tenant.id).where(Tenant.status.in_((TENANT_PENDING, TENANT_FAILED)))
        ).all()
    for tenant_id in tenant_ids:
        try:
            provision_tenant(control_engine, tenant_manager, tenant_id)
        except TenantProvisioningError:
            logger.error("Tenant provisioning remains incomplete: tenant=%s", tenant_id)
def register_portfolio(
    control_engine: Engine,
    tenant_manager: TenantManager,
    *,
    email: str,
    password: str,
    display_name: str,
    portfolio_name: str,
) -> tuple[User, Tenant, TenantMembership]:
    """Creates an account and its first portfolio, in that order.

    The first account on a fresh instance also becomes the instance administrator,
    which is what makes a self-hosted deployment bootstrapable without shipping a
    default password.
    """
    ok, reason = password_is_acceptable(password)
    if not ok:
        raise ValueError(reason)

    email = normalise_email(email)
    if not email or "@" not in email:
        raise ValueError("A valid email address is required.")

    with Session(control_engine) as session:
        if session.exec(select(User).where(User.email == email)).first():
            raise ValueError("An account with that email already exists.")

        is_first_user = len(session.exec(select(User)).all()) == 0
        user = User(
            id=f"usr_{uuid4().hex[:16]}",
            email=email,
            password_hash=hash_password(password),
            display_name=display_name.strip() or email,
            is_instance_admin=is_first_user,
        )
        session.add(user)

        tenant = Tenant(
            id=f"tnt_{uuid4().hex[:16]}",
            name=portfolio_name.strip() or f"{user.display_name}'s portfolio",
            slug=unique_slug(control_engine, portfolio_name),
            status=TENANT_PENDING,
        )
        session.add(tenant)
        session.flush()

        membership = TenantMembership(
            id=f"mem_{uuid4().hex[:16]}",
            user_id=user.id,
            tenant_id=tenant.id,
            role=ROLE_OWNER,
        )
        session.add(membership)
        session.commit()
        session.refresh(user)
        session.refresh(tenant)
        session.refresh(membership)

    # The control-plane rows remain unusable until storage and financial schema verify.
    tenant = provision_tenant(control_engine, tenant_manager, tenant.id)
    return user, tenant, membership


def authenticate(control_engine: Engine, email: str, password: str) -> Optional[User]:
    """The user for these credentials, or None. Never reveals which part failed."""
    with Session(control_engine) as session:
        user = session.exec(select(User).where(User.email == normalise_email(email))).first()
        if user is None or not user.is_active:
            # Still hash something, so a missing account is not obviously faster.
            verify_password(_HASHER.hash("timing-equalisation"), password)
            return None
        if not verify_password(user.password_hash, password):
            return None
        return user


def change_password(control_engine: Engine, user: User, current_password: str, new_password: str) -> None:
    """Changes a password and revokes every other session.

    Changing a password is how a user responds to suspected compromise, so it must
    also cut off sessions that were opened with the old one.
    """
    ok, reason = password_is_acceptable(new_password)
    if not ok:
        raise ValueError(reason)
    with Session(control_engine) as session:
        stored = session.get(User, user.id)
        if stored is None or not verify_password(stored.password_hash, current_password):
            raise ValueError("Current password is incorrect.")
        stored.password_hash = hash_password(new_password)
        stored.password_changed_at = utc_now()
        session.add(stored)
        for row in session.exec(select(UserSession).where(UserSession.user_id == user.id)).all():
            if row.revoked_at is None:
                row.revoked_at = utc_now()
                session.add(row)
        session.commit()


def create_invite(
    control_engine: Engine,
    *,
    tenant_id: str,
    email: str,
    role: str,
    invited_by_user_id: str,
) -> tuple[UserInvite, str]:
    """Issues a single-use invitation. Returns the row and the plain token."""
    if role not in INVITABLE_ROLES:
        raise ValueError(
            f"Cannot invite somebody as {role!r}. Invite them as "
            f"{' or '.join(INVITABLE_ROLES)}, then promote them if they should own it."
        )

    token = new_token()
    with Session(control_engine) as session:
        invite = UserInvite(
            id=f"inv_{uuid4().hex[:16]}",
            tenant_id=tenant_id,
            email=normalise_email(email),
            role=role,
            token_hash=token_digest(token),
            invited_by_user_id=invited_by_user_id,
            expires_at=utc_now() + INVITE_LIFETIME,
        )
        session.add(invite)
        session.commit()
        session.refresh(invite)
    return invite, token


def find_invite(control_engine: Engine, token: str) -> Optional[UserInvite]:
    with Session(control_engine) as session:
        invite = session.exec(
            select(UserInvite).where(UserInvite.token_hash == token_digest(token))
        ).first()
        if invite is None or invite.accepted_at is not None:
            return None
        if invite.expires_at <= utc_now():
            return None
        return invite


def accept_invite(
    control_engine: Engine,
    tenant_manager: TenantManager,
    *,
    token: str,
    password: str,
    display_name: str = "",
) -> tuple[User, Tenant, TenantMembership]:
    """Atomically claim a live invitation and create its membership."""
    preview = find_invite(control_engine, token)
    if preview is None:
        raise ValueError("This invitation is invalid, already used, or has expired.")
    tenant_manager.ensure_tenant_storage(preview.tenant_id)

    ok, reason = password_is_acceptable(password)
    if not ok:
        raise ValueError(reason)
    password_hash = hash_password(password)
    now = utc_now().isoformat()

    try:
        with Session(control_engine) as session:
            claimed = session.execute(text(
                "UPDATE user_invites SET accepted_at=:accepted_at "
                "WHERE token_hash=:token_hash AND accepted_at IS NULL "
                "AND julianday(expires_at) > julianday(:now) "
                "AND EXISTS (SELECT 1 FROM tenants WHERE tenants.id=user_invites.tenant_id "
                "AND tenants.status=:active)"
            ), {"accepted_at": now, "token_hash": token_digest(token),
                "now": now, "active": TENANT_ACTIVE})
            if claimed.rowcount != 1:
                raise ValueError("This invitation is invalid, already used, or has expired.")

            invite = session.exec(
                select(UserInvite).where(UserInvite.token_hash == token_digest(token))
            ).one()
            user = session.exec(select(User).where(User.email == invite.email)).first()
            if user is None:
                user = User(
                    id=f"usr_{uuid4().hex[:16]}",
                    email=invite.email,
                    password_hash=password_hash,
                    display_name=display_name.strip() or invite.email,
                )
                session.add(user)
                session.flush()
            elif not verify_password(user.password_hash, password):
                raise ValueError(
                    "An account already exists for this email; sign in to accept the invitation."
                )

            existing = session.exec(
                select(TenantMembership).where(
                    TenantMembership.user_id == user.id,
                    TenantMembership.tenant_id == invite.tenant_id,
                )
            ).first()
            if existing is not None:
                raise ValueError("This account is already a member of that portfolio.")

            membership = TenantMembership(
                id=f"mem_{uuid4().hex[:16]}",
                user_id=user.id,
                tenant_id=invite.tenant_id,
                role=invite.role,
            )
            session.add(membership)
            session.commit()
            session.refresh(user)
            session.refresh(membership)
            tenant = session.get(Tenant, invite.tenant_id)
    except ValueError:
        raise
    except Exception as exc:
        from sqlalchemy.exc import IntegrityError

        if isinstance(exc, IntegrityError):
            raise ValueError(
                "This invitation was already accepted or the account is already a member."
            ) from exc
        raise

    return user, tenant, membership


def issue_password_reset(control_engine: Engine, email: str) -> Optional[str]:
    """Creates a reset token for the local operator to deliver out of band.

    Returns ``None`` when no such account exists. This helper must only be called from an
    operator-controlled path (currently ``scripts/issue_password_reset.py``), never from an
    unauthenticated web endpoint: the returned token can change the account's password.
    """
    with Session(control_engine) as session:
        user = session.exec(select(User).where(User.email == normalise_email(email))).first()
        if user is None:
            return None
        token = new_token()
        session.add(PasswordResetToken(
            id=f"rst_{uuid4().hex[:16]}",
            user_id=user.id,
            token_hash=token_digest(token),
            expires_at=utc_now() + RESET_LIFETIME,
        ))
        session.commit()
    return token


def consume_password_reset(control_engine: Engine, token: str, new_password: str) -> User:
    """Sets a new password from a single-use reset token and revokes all sessions."""
    ok, reason = password_is_acceptable(new_password)
    if not ok:
        raise ValueError(reason)

    with Session(control_engine) as session:
        row = session.exec(
            select(PasswordResetToken).where(PasswordResetToken.token_hash == token_digest(token))
        ).first()
        if row is None or row.used_at is not None or row.expires_at <= utc_now():
            raise ValueError("This reset link is invalid, already used, or has expired.")
        user = session.get(User, row.user_id)
        if user is None:
            raise ValueError("This reset link is invalid, already used, or has expired.")
        user.password_hash = hash_password(new_password)
        user.password_changed_at = utc_now()
        session.add(user)
        row.used_at = utc_now()
        session.add(row)
        for existing in session.exec(select(UserSession).where(UserSession.user_id == user.id)).all():
            if existing.revoked_at is None:
                existing.revoked_at = utc_now()
                session.add(existing)
        session.commit()
        session.refresh(user)
        return user


# --- Sessions --------------------------------------------------------------

def issue_session(
    control_engine: Engine,
    user: User,
    tenant_id: str,
    *,
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None,
) -> tuple[str, UserSession]:
    """Opens a session and returns the cookie token with its stored row."""
    token = new_token()
    with Session(control_engine) as session:
        row = UserSession(
            id=token_digest(token),
            user_id=user.id,
            active_tenant_id=tenant_id,
            csrf_token=new_token(),
            ip_address=ip_address,
            user_agent=(user_agent or "")[:255] or None,
            expires_at=utc_now() + SESSION_LIFETIME,
        )
        session.add(row)
        session.commit()
        session.refresh(row)
    return token, row


def revoke_session(control_engine: Engine, token: str) -> None:
    with Session(control_engine) as session:
        row = session.get(UserSession, token_digest(token))
        if row is not None and row.revoked_at is None:
            row.revoked_at = utc_now()
            session.add(row)
            session.commit()


def switch_active_tenant(control_engine: Engine, token: str, tenant_id: str) -> None:
    """Points a session at another portfolio the user is a member of."""
    with Session(control_engine) as session:
        row = session.get(UserSession, token_digest(token))
        if row is not None:
            row.active_tenant_id = tenant_id
            session.add(row)
            session.commit()


def resolve_context(
    control_engine: Engine,
    tenant_manager: TenantManager,
    token: Optional[str],
) -> Optional[TenantContext]:
    """The portfolio context for a session cookie, or None if it is not usable."""
    if not token:
        return None

    with Session(control_engine) as session:
        row = session.get(UserSession, token_digest(token))
        if row is None or row.revoked_at is not None or row.expires_at <= utc_now():
            return None
        user = session.get(User, row.user_id)
        if user is None or not user.is_active:
            return None

        memberships = list(
            session.exec(
                select(TenantMembership)
                .where(TenantMembership.user_id == user.id)
                .order_by(TenantMembership.created_at)
            ).all()
        )
        if not memberships:
            return None

        active_memberships = []
        for membership in memberships:
            tenant = session.get(Tenant, membership.tenant_id)
            if tenant is not None and tenant.status == TENANT_ACTIVE:
                active_memberships.append((membership, tenant))
        if not active_memberships:
            raise NoActivePortfolio(
                "This account has no active portfolio. Tenant provisioning or deletion "
                "must be resolved before financial data is available."
            )

        membership, tenant = next(
            (pair for pair in active_memberships if pair[1].id == row.active_tenant_id),
            active_memberships[0],
        )
        session.expunge(user)
        session.expunge(row)
        session.expunge(tenant)

    tenant_manager.ensure_tenant_storage(tenant.id)
    return TenantContext(
        user=user,
        tenant=tenant,
        role=membership.role,
        session=row,
        engine=tenant_manager.engine_for(tenant.id),
        paths=tenant_manager.paths(tenant.id),
    )


# --- Request dependencies --------------------------------------------------

def cookie_name() -> str:
    return os.environ.get("PROPTALLY_SESSION_COOKIE", COOKIE_NAME)


def cookie_is_secure(request) -> bool:
    """Whether to mark cookies Secure, decided from how the request actually arrived.

    A Secure cookie is never sent back over plain HTTP, so marking one Secure on an
    http:// deployment locks the user out of their own app. The flag therefore
    follows the scheme: HTTPS (including behind a reverse proxy that sets
    X-Forwarded-Proto) gets Secure cookies, plain HTTP does not.
    ``PROPTALLY_SECURE_COOKIES`` overrides this in either direction.
    """
    override = os.environ.get("PROPTALLY_SECURE_COOKIES", "").strip().lower()
    if override in {"1", "true", "yes"}:
        return True
    if override in {"0", "false", "no"}:
        return False
    forwarded = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
    return forwarded == "https" or request.url.scheme == "https"


def validate_authorizations(
    client_confirmed: bool, user_attested_cpa_review: bool
) -> tuple[bool, bool]:
    """Checks the authorizations a request claims, and returns what may be recorded.

    The two gates are the client's own authorization and the external review of a
    deduction, so they belong to different roles. A request that claims one it may not
    give is refused rather than quietly downgraded: dropping it silently would post the
    entry as though nobody had authorized it, and the user would be told about a policy
    gate instead of about their role.

    Called by every route that reads the controls, so the rule cannot differ between the
    register and the review queue.
    """
    context = current_context()
    if context is None:
        raise AuthorizationError("No portfolio context for this request.")

    if client_confirmed and not context.can_confirm_client:
        raise AuthorizationError(
            f"Role {context.role} cannot give the client's authorization. Confirming a "
            "deposit on the owner's behalf is the owner's decision; ask them to confirm it."
        )
    if user_attested_cpa_review and not context.can_supply_external_review:
        raise AuthorizationError(
            f"Role {context.role} cannot record an external review. That is the CPA "
            "reviewer's role, or the owner's own attestation."
        )
    return client_confirmed, user_attested_cpa_review


def csrf_token_valid(context: TenantContext, presented: Optional[str]) -> bool:
    """Double-submit check: the request must echo the session's own token."""
    if not presented:
        return False
    return hmac.compare_digest(context.csrf_token, presented)


# --- Pre-session CSRF ------------------------------------------------------
#
# Sign-in, registration, invitation and password-reset forms have no session yet,
# so their token cannot come from one. They use the same double-submit idea with a
# cookie issued when the form is rendered: an attacker's page can make the browser
# send the cookie, but cannot read it to put the matching value in the form.

PRE_SESSION_COOKIE = "pt_csrf"


def issue_pre_session_csrf(request) -> tuple[str, bool]:
    """The token for an unauthenticated form, and whether the cookie must be set."""
    existing = request.cookies.get(PRE_SESSION_COOKIE)
    if existing:
        return existing, False
    return new_token(), True


def pre_session_csrf_valid(request, presented: Optional[str]) -> bool:
    """Whether an unauthenticated post echoes the pre-session cookie."""
    cookie = request.cookies.get(PRE_SESSION_COOKIE)
    if not cookie or not presented:
        return False
    return hmac.compare_digest(cookie, presented)


def set_pre_session_csrf_cookie(response, token: str, request) -> None:
    response.set_cookie(
        PRE_SESSION_COOKIE,
        token,
        httponly=True,
        samesite="lax",
        secure=cookie_is_secure(request),
        path="/",
    )


def set_session_cookie(response, token: str, request) -> None:
    """Sets the session cookie with the flags the security spec requires."""
    response.set_cookie(
        cookie_name(),
        token,
        httponly=True,
        samesite="lax",
        secure=cookie_is_secure(request),
        max_age=int(SESSION_LIFETIME.total_seconds()),
        path="/",
    )


def clear_session_cookie(response) -> None:
    response.delete_cookie(cookie_name(), path="/")


def registration_is_open(control_engine: Engine) -> bool:
    """Whether self-registration is permitted right now.

    Open when the operator enables it, and also while the instance has no accounts
    at all: a self-hosted deployment has to be bootstrapable without shipping a
    default password, and the very first account becomes the instance administrator.
    """
    flag = os.environ.get("PROPTALLY_ALLOW_REGISTRATION", "").lower() in {"1", "true", "yes"}
    return flag or count_users(control_engine) == 0



def _acquire_tenant_operation(
    control_engine: Engine, tenant_id: str, user_id: str, session_id: str, role: str
) -> Optional[str]:
    """Atomically lease an active tenant only while its session and membership remain valid."""
    operation_id = f"op_{uuid4().hex}"
    now = utc_now().isoformat()
    with Session(control_engine) as session:
        result = session.execute(text(
            "INSERT INTO tenant_operations "
            "(id, tenant_id, actor_id, user_id, session_id, process_id, host_name, started_at) "
            "SELECT :operation_id, t.id, :actor_id, u.id, s.id, :process_id, :host_name, :now "
            "FROM tenants AS t "
            "JOIN tenant_memberships AS m ON m.tenant_id=t.id AND m.user_id=:user_id "
            "JOIN users AS u ON u.id=m.user_id AND u.is_active=1 "
            "JOIN user_sessions AS s ON s.id=:session_id AND s.user_id=u.id "
            "WHERE t.id=:tenant_id AND t.status=:active AND m.role=:role "
            "AND s.revoked_at IS NULL AND julianday(s.expires_at)>julianday(:now)"
        ), {"operation_id": operation_id, "tenant_id": tenant_id,
            "actor_id": user_id, "process_id": os.getpid(), "host_name": socket.gethostname(),
            "user_id": user_id, "session_id": session_id, "role": role, "now": now,
            "active": TENANT_ACTIVE})
        if result.rowcount != 1:
            session.rollback()
            return None
        session.commit()
    return operation_id


def _release_tenant_operation(
    control_engine: Engine, tenant_manager: TenantManager, operation_id: str, tenant_id: str
) -> None:
    try:
        with Session(control_engine) as session:
            session.execute(text("DELETE FROM tenant_operations WHERE id=:id"), {"id": operation_id})
            session.commit()
    finally:
        tenant_manager.end_operation(tenant_id, operation_id)


@contextmanager
def tenant_operation_scope(
    control_engine: Engine,
    tenant_manager: TenantManager,
    *,
    context: TenantContext,
    tenant_id: str,
    role: str,
):
    """Lease a member-selected tenant while a cross-portfolio operation uses it."""
    operation_id = _acquire_tenant_operation(
        control_engine, tenant_id, context.user.id, context.session.id, role
    )
    if operation_id is None:
        raise AuthenticationRequired()
    tenant_manager.begin_operation(tenant_id, operation_id)
    try:
        paths = tenant_manager.ensure_tenant_storage(tenant_id)
        yield tenant_manager.engine_for(tenant_id), paths, operation_id
    finally:
        _release_tenant_operation(control_engine, tenant_manager, operation_id, tenant_id)

class AuthenticationDependency:
    """Resolve a tenant context and hold a request lease until its response ends."""

    def __init__(
        self,
        control_engine: Engine,
        tenant_manager: TenantManager,
        *,
        require_mutating_role: bool = True,
    ):
        self.control_engine = control_engine
        self.tenant_manager = tenant_manager
        self.require_mutating_role = require_mutating_role

    async def __call__(self, request: Request) -> AsyncIterator[TenantContext]:
        token = request.cookies.get(cookie_name())
        context = resolve_context(self.control_engine, self.tenant_manager, token)
        if context is None:
            raise AuthenticationRequired()

        operation_id = _acquire_tenant_operation(
            self.control_engine,
            context.tenant.id,
            context.user.id,
            context.session.id,
            context.role,
        )
        if operation_id is None:
            raise AuthenticationRequired()
        self.tenant_manager.begin_operation(context.tenant.id, operation_id)
        context = replace(context, operation_id=operation_id)
        context_token = None
        try:
            if request.method in UNSAFE_METHODS:
                presented = request.headers.get("x-csrf-token")
                if not presented:
                    presented = await _form_csrf_token(request)
                if not csrf_token_valid(context, presented):
                    raise HTTPException(
                        status_code=403,
                        detail=(
                            "Missing or invalid CSRF token. Reload the page and try again; "
                            "if the problem persists, sign in again."
                        ),
                    )
                if self.require_mutating_role and not context.can_mutate:
                    raise AuthorizationError(
                        f"Role {context.role} may read this portfolio but not change it."
                    )

            context_token = _current_context.set(context)
            yield context
        finally:
            if context_token is not None:
                _current_context.reset(context_token)
            _release_tenant_operation(
                self.control_engine, self.tenant_manager, operation_id, context.tenant.id
            )


async def _form_csrf_token(request: Request) -> Optional[str]:
    """The CSRF token from the request's form body, if it has one.

    Starlette caches the parsed form on the request, so parsing it here is not
    destructive: the route handler that reads the body afterwards gets the same
    object rather than an exhausted stream.
    """
    content_type = request.headers.get("content-type", "")
    if "form" not in content_type:
        return None
    try:
        form = await request.form()
    except Exception:  # noqa: BLE001 - a malformed body must not become a 500
        return None
    value = form.get("csrf_token")
    return value if isinstance(value, str) else None
