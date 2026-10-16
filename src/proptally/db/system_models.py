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

#: Portfolio roles. They answer three separate questions, and keeping them separate is
#: what lets a team share a portfolio without one person's authority leaking to another:
#:
#: * OWNER     -- the client. Owns the portfolio, administers it, and is the only role
#:                that can authorize on the owner's own behalf.
#: * MANAGER   -- does the work: imports statements, allocates and posts, maintains the
#:                portfolio's setup. Cannot administer people, export or delete, and
#:                cannot give the client's authorization.
#: * CPA_REVIEWER -- an accountant. Reads everything and can record the external review a
#:                Schedule E deduction requires. Does not do the bookkeeping.
#: * READ_ONLY -- sees everything, changes nothing.
ROLE_OWNER = "OWNER"
ROLE_MANAGER = "MANAGER"
ROLE_CPA_REVIEWER = "CPA_REVIEWER"
ROLE_READ_ONLY = "READ_ONLY"
ROLES = (ROLE_OWNER, ROLE_MANAGER, ROLE_CPA_REVIEWER, ROLE_READ_ONLY)

#: Roles that may change a portfolio's *content*: the bookkeeping itself. A reviewer is
#: included because their attestation is delivered by posting the entry that needs it --
#: without that, a verified CPA review could never be recorded. What separates the roles
#: is authority: only an owner authorizes on the client's behalf and administers the
#: portfolio, and only a reviewer can be recorded as a verified external review.
WORK_ROLES = (ROLE_OWNER, ROLE_MANAGER, ROLE_CPA_REVIEWER)

#: Roles that may administer a portfolio: its people, its role assignments, its export
#: and its removal. Deliberately narrower than doing the work.
ADMIN_ROLES = (ROLE_OWNER,)

#: Roles permitted to issue any unsafe request. A superset of the two above, plus the
#: reviewer, whose one write is recording an attestation -- without this the attestation
#: could not be submitted at all.
MUTATING_ROLES = (ROLE_OWNER, ROLE_MANAGER, ROLE_CPA_REVIEWER)

#: CBD-1: authorization on the client's own behalf. Only the client can give it, so a
#: manager cannot, however much work they do.
CLIENT_CONFIRMATION_ROLES = (ROLE_OWNER,)

#: CBD-2: whose review is recorded as a *verified* external review.
CPA_REVIEW_ROLES = (ROLE_CPA_REVIEWER,)

#: CBD-2: who may tick the external-review control. A reviewer's tick is recorded as a
#: verified review; an owner's is recorded as the owner's own attestation, never as a
#: CPA's -- the posting record distinguishes them by role. A manager can do neither,
#: which is the point of the role.
EXTERNAL_REVIEW_ROLES = (ROLE_OWNER, ROLE_CPA_REVIEWER)

#: Roles that may be handed out by invitation. Ownership is not given away by link: an
#: invited person joins in one of these and is promoted afterwards, deliberately, by an
#: owner who can see who they are adding.
INVITABLE_ROLES = (ROLE_MANAGER, ROLE_CPA_REVIEWER, ROLE_READ_ONLY)


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
    created_at: datetime = Field(default_factory=utc_now)


class TenantMembership(SQLModel, table=True):
    """Which users may open which portfolio, and in what role."""

    __tablename__ = "tenant_memberships"
    __table_args__ = (
        CheckConstraint(
            "role IN ('OWNER', 'MANAGER', 'CPA_REVIEWER', 'READ_ONLY')",
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
            "role IN ('MANAGER', 'CPA_REVIEWER', 'READ_ONLY')",
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


class AccessEvent(SQLModel, table=True):
    """One change to a portfolio's access list, append-only.

    Membership changes are the one thing a client cannot reconstruct from their own
    books, so they are recorded here rather than only in an application log: who was
    invited, promoted, removed or allowed to leave, by whom, and when.
    """

    __tablename__ = "access_events"

    id: str = Field(primary_key=True)
    tenant_id: str = Field(foreign_key="tenants.id", index=True)
    actor_user_id: Optional[str] = Field(default=None, foreign_key="users.id")
    #: 'INVITED', 'INVITATION_WITHDRAWN', 'JOINED', 'ROLE_CHANGED', 'REMOVED', 'LEFT',
    #: 'RENAMED', 'PORTFOLIO_DELETED'
    event: str
    #: The person the event is about, by email, since their account may be gone later.
    subject_email: Optional[str] = Field(default=None)
    #: Free-form context: the old and new role, for instance.
    detail: Optional[str] = Field(default=None)
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
    AccessEvent.__table__,
)
