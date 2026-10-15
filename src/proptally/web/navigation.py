"""Navigation model for the PropTally application shell.

The shell's navigation is described here once, in Python, instead of being
duplicated as markup in the desktop sidebar and the mobile drawer. Doors come
from the database, so adding a property to the portfolio is a data change, not
a template change.

`DOOR_SHORT_LABELS` supplies the compact label used by the mobile quick
switcher and the door list; a door that is not listed falls back to a derived
short name (street number plus street name, without the street-type suffix).
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Optional

from sqlmodel import Session, select

from proptally.db.models import Property


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


#: Doors grouped ahead of the primary residence, as in the portfolio view.
DOOR_SHORT_LABELS = {
    "prop_tinsley_huntley": "12307 Larkspur St",
    "prop_cimmaron_pingree": "582 Foxglove Ct",
    "prop_burkey_algonquin": "1017 Elderberry Dr",
}

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
            "label": DOOR_SHORT_LABELS.get(prop.id) or _derived_short_label(prop),
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


def primary_sections() -> list[NavSection]:
    """Sidebar/drawer sections above the door list."""
    return [
        NavSection(
            label="Portfolio",
            items=[
                NavItem(
                    label="Portfolio Overview",
                    short="Overview",
                    href="/",
                    icon="home",
                    page="dashboard",
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
