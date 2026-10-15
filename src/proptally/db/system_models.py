"""Control-plane entities: accounts, portfolios, memberships and sessions.

These live in their own database (``storage/system.db``), separate from every
portfolio's ledger. That separation is the isolation model: a portfolio's data sits
in its own SQLite file, and this database holds only the identity and membership
facts needed to decide which file a request may open.

The tables are declared with the same SQLModel metadata as the domain models,
so they are created explicitly by :func:`init_system_schema` rather than by a
blanket ``create_all`` -- otherwise the control-plane database would also get
empty copies of every ledger table.
"""

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import CheckConstraint
from sqlmodel import Field, SQLModel

#: Portfolio roles. OWNER administers the portfolio; CPA_REVIEWER may record the
#: external review a Schedule E deduction needs; READ_ONLY may not mutate anything.
ROLE_OWNER = "OWNER"
ROLE_CPA_REVIEWER = "CPA_REVIEWER"
ROLE_READ_ONLY = "READ_ONLY"
ROLES = (ROLE_OWNER, ROLE_CPA_REVIEWER, ROLE_READ_ONLY)

#: Roles permitted to change state. CPA_REVIEWER is deliberately included: the
#: external-review attestation is that role's whole purpose.
MUTATING_ROLES = (ROLE_OWNER, ROLE_CPA_REVIEWER)


def utc_now() -> datetime:
    """Return timezone-aware UTC datetime."""
    return datetime.now(timezone.utc)


class User(SQLModel, table=True):
    """One person with a login on this instance."""

    __tablename__ = "users"

    id: str = Field(primary_key=True)  # e.g. "usr_9f2c..."
    email: str = Field(unique=True, index=True)  # stored lowercased
    password_hash: str
    display_name: str
    #: Instance administration is separate from portfolio ownership: an instance
    #: admin manages accounts and invites but has no implicit access to a ledger.
    is_instance_admin: bool = Field(default=False)
    is_active: bool = Field(default=True)
    password_changed_at: Optional[datetime] = Field(default=None)
    created_at: datetime = Field(default_factory=utc_now)


class Tenant(SQLModel, table=True):
    """One portfolio: the unit of data isolation."""

    __tablename__ = "tenants"

    id: str = Field(primary_key=True)  # e.g. "tnt_5c81..."
    name: str
    slug: str = Field(unique=True, index=True)
    is_demo: bool = Field(default=False)
    created_at: datetime = Field(default_factory=utc_now)


class TenantMembership(SQLModel, table=True):
    """Which users may open which portfolio, and in what role."""

    __tablename__ = "tenant_memberships"
    __table_args__ = (
        CheckConstraint(
            "role IN ('OWNER', 'CPA_REVIEWER', 'READ_ONLY')",
            name="check_membership_role",
        ),
    )

    id: str = Field(primary_key=True)
    user_id: str = Field(foreign_key="users.id", index=True)
    tenant_id: str = Field(foreign_key="tenants.id", index=True)
    role: str = Field(default=ROLE_OWNER)
    created_at: datetime = Field(default_factory=utc_now)


class UserSession(SQLModel, table=True):
    """A logged-in browser session.

    The cookie carries a random token; only its SHA-256 digest is stored, so a
    leaked database does not yield usable sessions. ``csrf_token`` is the
    double-submit value every state-changing request must echo.
    """

    __tablename__ = "user_sessions"

    id: str = Field(primary_key=True)  # sha256 hex of the cookie token
    user_id: str = Field(foreign_key="users.id", index=True)
    active_tenant_id: Optional[str] = Field(default=None, foreign_key="tenants.id")
    csrf_token: str
    ip_address: Optional[str] = Field(default=None)
    user_agent: Optional[str] = Field(default=None)
    expires_at: datetime
    revoked_at: Optional[datetime] = Field(default=None)
    created_at: datetime = Field(default_factory=utc_now)


class UserInvite(SQLModel, table=True):
    """A single-use invitation to join an existing portfolio.

    The only route by which a second person joins a portfolio: self-registration
    creates a new portfolio, never access to somebody else's.
    """

    __tablename__ = "user_invites"
    __table_args__ = (
        CheckConstraint(
            "role IN ('CPA_REVIEWER', 'READ_ONLY')",
            name="check_invite_role",
        ),
    )

    id: str = Field(primary_key=True)
    tenant_id: str = Field(foreign_key="tenants.id", index=True)
    email: str = Field(index=True)  # stored lowercased
    role: str = Field(default=ROLE_READ_ONLY)
    token_hash: str = Field(unique=True, index=True)
    invited_by_user_id: str = Field(foreign_key="users.id")
    expires_at: datetime
    accepted_at: Optional[datetime] = Field(default=None)
    created_at: datetime = Field(default_factory=utc_now)


class PasswordResetToken(SQLModel, table=True):
    """A single-use password reset token, delivered out of band by the operator."""

    __tablename__ = "password_reset_tokens"

    id: str = Field(primary_key=True)
    user_id: str = Field(foreign_key="users.id", index=True)
    token_hash: str = Field(unique=True, index=True)
    expires_at: datetime
    used_at: Optional[datetime] = Field(default=None)
    created_at: datetime = Field(default_factory=utc_now)


#: Control-plane tables, in dependency order. ``init_system_schema`` creates only
#: these, so a portfolio's ledger tables never appear in the control plane.
SYSTEM_TABLES = (
    User.__table__,
    Tenant.__table__,
    TenantMembership.__table__,
    UserSession.__table__,
    UserInvite.__table__,
    PasswordResetToken.__table__,
)
