"""Navigation model for the PropTally application shell.

The shell's navigation is described here once, in Python, instead of being
duplicated as markup in the desktop sidebar and the mobile drawer. Doors come
from the database, so adding a property to the portfolio is a data change, not
a template change.

`Property.short_label` supplies the compact label used by the mobile quick
switcher and the door list; a door without one falls back to a derived short name
(street number plus street name, without the street-type suffix).
"""

from collections.abc import Iterable, Sequence
from contextvars import ContextVar
from dataclasses import dataclass

from sqlmodel import Session, select

from proptally.db.models import (
    Document,
    FinancialAccount,
    LoanAccount,
    Property,
    Statement,
    Transaction,
)
from proptally.db.system_models import Tenant, TenantMembership
from proptally.web.auth import current_context


@dataclass(frozen=True)
class PortfolioNavItem:
    tenant_id: str
    name: str
    role: str
    is_active: bool


@dataclass(frozen=True)
class NavItem:
    """One shell navigation entry."""

    label: str          # full label, desktop sidebar and drawer
    short: str          # compact label, bottom navigation
    href: str
    icon: str           # icon name understood by components/ui.html
    page: str | None = None   # matches `active_page` for the active state
    blank: bool = False          # opens in a new tab


@dataclass(frozen=True)
class NavSection:
    """A labelled group of navigation entries."""

    label: str
    items: Sequence[NavItem]


#: Doors are grouped ahead of the primary residence, as in the portfolio view.
#: Their compact labels come from `Property.short_label` when the owner set one,
#: and are otherwise derived from the street address, so the shell never needs a
#: list of a particular portfolio's properties.
_STREET_SUFFIXES = {
    "st", "street", "dr", "drive", "cir", "circle", "ave", "avenue",
    "rd", "road", "ln", "lane", "ct", "court", "blvd", "boulevard",
    "way", "pl", "place", "ter", "terrace",
}


def _derived_short_label(property_record: Property) -> str:
    """Street number plus street name, e.g. "12307 Larkspur St" -> "12307 Larkspur"."""
    tokens = (property_record.name or property_record.address_line1 or "Door").split()
    if len(tokens) > 1 and tokens[-1].lower().strip(".,") in _STREET_SUFFIXES:
        tokens = tokens[:-1]
    return " ".join(tokens) if tokens else property_record.id


def door_items(properties: Iterable[Property], active_property: str | None) -> list[dict]:
    """Builds the door navigation entries (rental doors first, then residences)."""
    ordered = sorted(
        properties,
        key=lambda p: (p.property_type != "RENTAL_INVESTMENT", p.name or ""),
    )
    return [
        {
            "id": prop.id,
            "label": prop.short_label or _derived_short_label(prop),
            "full_name": prop.name,
            "place": f"{prop.city}, {prop.state}",
            "is_primary": prop.property_type != "RENTAL_INVESTMENT",
            "href": f"/properties/{prop.id}",
            "is_active": prop.id == active_property,
        }
        for prop in ordered
    ]


def get_nav_doors(engine) -> list[Property]:
    """Active doors, ordered for navigation. One indexed read per page render."""
    try:
        with Session(engine) as session:
            statement = select(Property).where(Property.is_active == True)
            return list(session.exec(statement).all())
    except Exception:  # noqa: BLE001 - navigation must never break a page
        return []


def get_nav_portfolios(engine, user_id: str, active_tenant_id: str) -> list[PortfolioNavItem]:
    """Only the signed-in person's memberships, with the active workspace marked."""
    try:
        with Session(engine) as session:
            rows = session.exec(
                select(TenantMembership, Tenant)
                .join(Tenant, Tenant.id == TenantMembership.tenant_id)
                .where(TenantMembership.user_id == user_id)
                .order_by(TenantMembership.created_at)
            ).all()
    except Exception:  # noqa: BLE001 - a broken switcher must not blank the shell
        return []
    return [
        PortfolioNavItem(membership.tenant_id, tenant.name, membership.role,
                         tenant.id == active_tenant_id)
        for membership, tenant in rows
    ]


@dataclass(frozen=True)
class NavState:
    """What the current portfolio actually contains, for journey-aware navigation.

    A brand-new portfolio should not open with Mortgages, Transactions, Review,
    Records and Tax: none of them can contain anything yet. Each section appears
    when the data that gives it meaning exists, so the left rail grows with the
    owner's work rather than showing an empty product on day one.
    """

    has_properties: bool = False
    has_accounts: bool = False
    has_mortgages: bool = False
    has_transactions: bool = False
    has_documents: bool = False
    has_statements: bool = False

    @property
    def has_any_data(self) -> bool:
        return any((
            self.has_properties, self.has_accounts, self.has_mortgages,
            self.has_transactions, self.has_documents, self.has_statements,
        ))


#: The shell renders the sidebar and the drawer in one request. Cache the small
#: set of existence checks so the journey is computed once per page.
_NAV_STATE_CACHE: ContextVar[tuple[str, NavState] | None] = ContextVar(
    "proptally_nav_state", default=None
)


def journey_state() -> NavState:
    """The current portfolio's progress, or an empty state outside a request."""
    context = current_context()
    if context is None:
        return NavState()
    cached = _NAV_STATE_CACHE.get()
    if cached is not None and cached[0] == context.tenant.id:
        return cached[1]
    try:
        with Session(context.engine) as session:
            def exists(statement) -> bool:
                return session.exec(statement.limit(1)).first() is not None

            state = NavState(
                has_properties=exists(
                    select(Property.id).where(Property.is_active.is_(True))
                ),
                has_accounts=exists(
                    select(FinancialAccount.id).where(FinancialAccount.is_active.is_(True))
                ),
                has_mortgages=exists(
                    select(LoanAccount.id).where(LoanAccount.is_active.is_(True))
                ),
                has_transactions=exists(select(Transaction.id)),
                has_documents=exists(select(Document.id)),
                has_statements=exists(select(Statement.id)),
            )
    except Exception:  # noqa: BLE001 - a provisioning tenant has no ledger yet
        state = NavState()
    _NAV_STATE_CACHE.set((context.tenant.id, state))
    return state


def primary_sections(
    state: NavState | None = None, portfolio_count: int = 1
) -> list[NavSection]:
    """The first steps of the journey: choose a portfolio, then orient in it.

    A single-portfolio account does not need an "All Portfolios" screen that
    lists the one portfolio it already has open. That entry appears only when
    the account actually reaches more than one portfolio; the workspace
    switcher then offers the same choice inline.

    The overview carries the two onboarding paths when the portfolio is empty,
    so setup and import do not need permanent entries in the left navigation.
    Administration and data management belong at the bottom, after the work the
    portfolio is used for.
    """
    sections: list[NavSection] = []
    if portfolio_count > 1:
        sections.append(NavSection(
            label="Portfolios",
            items=[
                NavItem(
                    label="All Portfolios",
                    short="Portfolios",
                    href="/portfolios",
                    icon="scale",
                    page="portfolios",
                ),
            ],
        ))
    if state is None:
        state = NavState()
    if state.has_properties:
        current_item = NavItem(
            label="Portfolio Overview",
            short="Overview",
            href="/",
            icon="home",
            page="dashboard",
        )
    else:
        current_item = NavItem(
            label="Portfolio Onboarding",
            short="Onboarding",
            href="/onboarding",
            icon="home",
            page="onboarding",
        )
    sections.append(NavSection(label="Current portfolio", items=[current_item]))
    return sections


def financial_sections(state: NavState | None = None) -> list[NavSection]:
    """The working journey below the portfolio's doors, revealed by progress.

    Money first (what moved, what is owed, what needs review), then the records
    those figures come from, then tax, and finally the settings that change the
    portfolio itself rather than its books. A section with nothing behind it is
    omitted entirely.
    """
    state = state or journey_state()
    sections: list[NavSection] = []

    money: list[NavItem] = []
    if state.has_accounts or state.has_mortgages:
        money.append(NavItem(
            label="Mortgages & Escrow",
            short="Mortgages",
            href="/mortgages",
            icon="bank",
            page="mortgages",
        ))
    if state.has_transactions or state.has_statements:
        money.append(NavItem(
            label="Transactions Register",
            short="Transactions",
            href="/transactions",
            icon="card",
            page="transactions",
        ))
    if state.has_transactions:
        money.append(NavItem(
            label="Review Queue",
            short="Review",
            href="/reconciliation",
            icon="check_circle",
            page="reconciliation",
        ))
    if money:
        sections.append(NavSection(label="Money", items=money))

    records: list[NavItem] = []
    if state.has_documents:
        records.append(NavItem(
            label="Document Vault",
            short="Vault",
            href="/documents",
            icon="file_text",
            page="documents",
        ))
    if state.has_documents or state.has_statements:
        records.append(NavItem(
            label="Statements & Ingestion",
            short="Statements",
            href="/statements",
            icon="inbox",
            page="statements",
        ))
    if records:
        sections.append(NavSection(label="Records", items=records))

    if state.has_transactions:
        sections.append(NavSection(
            label="Tax",
            items=[NavItem(
                label="Tax & Schedule E",
                short="Tax / Sched E",
                href="/tax/schedule-e",
                icon="ledger",
                page="tax",
            )],
        ))

    settings: list[NavItem] = [
        NavItem(
            label="Manage Portfolio Data",
            short="Manage",
            href="/settings",
            icon="ledger",
            page="settings",
        ),
        NavItem(
            label="Controls & Reconciliation",
            short="Controls",
            href="/reconciliation/report",
            icon="scale",
            page="report",
        ),
        NavItem(
            label="Administration",
            short="Admin",
            href="/account",
            icon="user_check",
            page="account",
        ),
    ]
    sections.append(NavSection(label="Settings", items=settings))
    return sections


def console_sections() -> list[NavSection]:
    """The super admin's destinations, rendered in the console's own shell.

    Same `NavSection`/`NavItem` shape and the same shell markup as the workspace, so the
    console is the same product rather than a second one bolted on beside it. The
    destinations are platform ones: nothing here belongs to a tenant.
    """
    return [
        NavSection(
            label="Platform",
            items=[
                NavItem(
                    label="Overview",
                    short="Overview",
                    href="/admin",
                    icon="scale",
                    page="overview",
                ),
                NavItem(
                    label="Portfolios",
                    short="Portfolios",
                    href="/admin/tenants",
                    icon="bank",
                    page="tenants",
                ),
                NavItem(
                    label="People",
                    short="People",
                    href="/admin/people",
                    icon="user_check",
                    page="people",
                ),
                NavItem(
                    label="Onboarding",
                    short="Onboarding",
                    href="/admin/onboarding",
                    icon="tag",
                    page="onboarding",
                ),
                NavItem(
                    label="Document intelligence",
                    short="Doc intelligence",
                    href="/admin/document-intelligence",
                    icon="sparkline",
                    page="document_intelligence",
                ),
            ],
        ),
    ]


def instance_sections() -> list[NavSection]:
    """The super admin's console.

    Rendered only for the platform's super admin: it is administration of the application
    itself, outside the tenant model, and a super admin has no access to any portfolio's
    ledger. Kept separate from the workspace destinations so its presence never implies
    portfolio authority.
    """
    return [
        NavSection(
            label="Platform",
            items=[
                NavItem(
                    label="Backend Console",
                    short="Console",
                    href="/admin",
                    icon="shield",
                    page="admin",
                ),
            ],
        ),
    ]


def bottom_items(state: NavState | None = None) -> list[NavItem]:
    """Mobile bottom navigation: the highest-frequency destinations that exist."""
    state = state or journey_state()
    by_page = {
        item.page: item
        for section in primary_sections(state) + financial_sections(state)
        for item in section.items
    }
    # Setup and import are reached from the overview rather than the sidebar,
    # but they remain high-frequency destinations for a portfolio without data.
    by_page.setdefault(
        "setup",
        NavItem(label="Guided Setup", short="Setup", href="/setup",
                icon="sliders", page="setup"),
    )
    by_page.setdefault(
        "onboarding",
        NavItem(label="Import Documents", short="Import", href="/onboarding",
                icon="upload", page="onboarding"),
    )
    pages = ["dashboard"]
    if state.has_accounts or state.has_mortgages:
        pages.append("mortgages")
    elif state.has_documents:
        pages.append("documents")
    else:
        pages.append("onboarding")
    if state.has_transactions:
        pages.append("reconciliation")
    elif state.has_documents:
        pages.append("documents")
    else:
        pages.append("setup")
    if state.has_transactions:
        pages.append("tax")
    elif state.has_any_data:
        pages.append("settings")
    else:
        pages.append("account")
    items: list[NavItem] = []
    seen: set[str] = set()
    for page in pages:
        item = by_page.get(page)
        if item is not None and page not in seen:
            items.append(item)
            seen.add(page)
    return items[:4]
