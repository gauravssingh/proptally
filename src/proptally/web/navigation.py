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
from dataclasses import dataclass
from typing import Optional

from sqlmodel import Session, select

from proptally.db.models import Property
from proptally.db.system_models import Tenant, TenantMembership


@dataclass(frozen=True)
class PortfolioNavItem:
    tenant_id: str
    name: str
    role: str
    is_active: bool


def get_nav_portfolios(engine, user_id: str, active_tenant_id: str) -> list[PortfolioNavItem]:
    """The signed-in person's portfolio switcher; never a process-global portfolio list."""
    with Session(engine) as session:
        memberships = session.exec(
            select(TenantMembership, Tenant)
            .join(Tenant, Tenant.id == TenantMembership.tenant_id)
            .where(TenantMembership.user_id == user_id)
            .order_by(TenantMembership.created_at)
        ).all()
    return [
        PortfolioNavItem(
            tenant_id=membership.tenant_id,
            name=tenant.name,
            role=membership.role,
            is_active=tenant.id == active_tenant_id,
        )
        for membership, tenant in memberships
    ]


@dataclass(frozen=True)
class NavItem:
    """One shell navigation entry."""

    label: str          # full label, desktop sidebar and drawer
    short: str          # compact label, bottom navigation
    href: str
    icon: str           # icon name understood by components/ui.html
    page: Optional[str] = None   # matches `active_page` for the active state
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


def door_items(properties: Iterable[Property], active_property: Optional[str]) -> list[dict]:
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
            statement = select(Property).where(Property.is_active == True)  # noqa: E712
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


def primary_sections() -> list[NavSection]:
    """Workspace destinations, then destinations scoped to the open portfolio."""
    return [
        NavSection(
            label="Workspaces",
            items=[
                NavItem(
                    label="All Portfolios",
                    short="Portfolios",
                    href="/portfolios",
                    icon="scale",
                    page="portfolios",
                ),
            ],
        ),
        NavSection(
            label="Current portfolio",
            items=[
                NavItem(
                    label="Portfolio Overview",
                    short="Overview",
                    href="/",
                    icon="home",
                    page="dashboard",
                ),
                NavItem(
                    label="Administration",
                    short="Admin",
                    href="/account",
                    icon="user_check",
                    page="account",
                ),
                NavItem(
                    label="Portfolio Setup",
                    short="Setup",
                    href="/settings",
                    icon="sliders",
                    page="settings",
                ),
            ],
        ),
    ]


def financial_sections() -> list[NavSection]:
    """Sidebar/drawer sections below the door list."""
    return [
        NavSection(
            label="Financial Core",
            items=[
                NavItem(
                    label="Mortgages & Escrow",
                    short="Mortgages",
                    href="/mortgages",
                    icon="bank",
                    page="mortgages",
                ),
                NavItem(
                    label="Transactions Register",
                    short="Transactions",
                    href="/transactions",
                    icon="card",
                    page="transactions",
                ),
                NavItem(
                    label="Review Queue",
                    short="Review",
                    href="/reconciliation",
                    icon="check_circle",
                    page="reconciliation",
                ),
                NavItem(
                    label="Tax & Schedule E",
                    short="Tax / Sched E",
                    href="/tax/schedule-e",
                    icon="ledger",
                    page="tax",
                ),
            ],
        ),
        NavSection(
            label="Documents",
            items=[
                NavItem(
                    label="Document Vault",
                    short="Vault",
                    href="/documents",
                    icon="file_text",
                    page="documents",
                ),
                NavItem(
                    label="Statements & Ingestion",
                    short="Statements",
                    href="/statements",
                    icon="inbox",
                    page="statements",
                ),
            ],
        ),
    ]


def bottom_items() -> list[NavItem]:
    """Mobile bottom navigation: the five highest-frequency destinations."""
    by_page = {
        item.page: item
        for section in primary_sections() + financial_sections()
        for item in section.items
    }
    return [
        by_page["dashboard"],
        by_page["mortgages"],
        by_page["reconciliation"],
        by_page["tax"],
    ]
