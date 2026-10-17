"""Property fields and location persistence shared by every property form.

There is one parser and one writer for a property. The portfolio setup screen,
the property detail editor and the guided setup wizard all call these functions,
so none of them can drift into a weaker validation path.

Nothing here commits. The caller owns the transaction and can roll it back when
a submission is refused.
"""

from __future__ import annotations

import re
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from sqlmodel import Session, select

from proptally.db.models import Property, PropertyValuation, utc_now
from proptally.web.form_parsing import (
    FormError,
    cents,
    choice,
    clean,
    day,
    flag,
    new_id,
    required,
)

#: Property classes the owner may choose between. A rental door is a Schedule E
#: asset and a primary residence is not, and the reporting engine treats them
#: differently everywhere, so the choice is explicit rather than defaulted.
PROPERTY_TYPE_OPTIONS = [
    {"value": "RENTAL_INVESTMENT", "label": "Rental investment property (Schedule E)"},
    {"value": "PRIMARY_RESIDENCE", "label": "Primary residence (non-operating)"},
]
PROPERTY_TYPES = tuple(option["value"] for option in PROPERTY_TYPE_OPTIONS)

#: Google place IDs are opaque, URL-safe identifiers (usually starting with ChIJ).
PLACE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,512}$")

#: Google's Places terms permit caching latitude/longitude for 30 consecutive
#: calendar days. The map is rendered from the place ID, so expiry never removes
#: a property's pinned location from the UI.
COORDINATE_CACHE_DAYS = 30

def address_tuple(prop: Property) -> tuple[str, str, str, str, str]:
    """The address as one comparable value, for detecting a manual edit."""
    return (
        prop.address_line1 or "",
        prop.address_line2 or "",
        prop.city or "",
        prop.state or "",
        prop.zip_code or "",
    )


def _normalised(values) -> tuple[str, ...]:
    return tuple(" ".join((value or "").split()) for value in values)


def apply_address(prop: Property, form) -> None:
    """Applies the address fields. Raises before anything is written."""
    address_line1 = required(form.get("address_line1"), "Street address")
    city = required(form.get("city"), "City")
    address_line2 = clean(form.get("address_line2")) or None
    state = clean(form.get("state")) or "IL"
    zip_code = clean(form.get("zip_code"))

    prop.address_line1 = address_line1
    prop.address_line2 = address_line2
    prop.city = city
    prop.state = state
    prop.zip_code = zip_code


def _address_key(prop: Property) -> tuple[str, ...]:
    """A case- and whitespace-insensitive key for the physical address."""
    return tuple(
        " ".join((value or "").split()).casefold()
        for value in address_tuple(prop)
    )


def property_from_form(session: Session, form, prop: Property) -> Property:
    """Applies every field of the property form, raising before any write."""
    name = required(form.get("name"), "Property name")
    property_type = choice(form.get("property_type"), PROPERTY_TYPES, "Property type")
    acquisition_date = day(form.get("acquisition_date"), "Acquisition date")
    purchase_price_cents = cents(form.get("purchase_price"), "Purchase price")
    land_basis_cents = cents(form.get("land_basis"), "Land basis")
    building_basis_cents = cents(form.get("building_basis"), "Building basis")
    down_payment_cents = cents(form.get("down_payment"), "Down payment")
    closing_costs_cents = cents(form.get("closing_costs"), "Closing costs")
    furnishing_costs_cents = cents(form.get("furnishing_costs"), "Furnishing costs")
    # Valuation is parsed here too, so a bad value cannot be discovered after the
    # property has already been committed by the allocator.
    cents(form.get("current_valuation"), "Current valuation")
    day(form.get("valuation_date"), "Valuation date")

    # Parses and writes the address only after every other value above parsed.
    apply_address(prop, form)

    # A second door at the same physical address is almost always a duplicate.
    # Address line 2 is part of the key, so separate units in one building remain
    # distinct. Inactive properties do not block a new one.
    for other in session.exec(
        select(Property).where(
            Property.id != prop.id,
            Property.is_active == True,
        )
    ).all():
        if _address_key(other) == _address_key(prop):
            raise FormError(
                "A property at this address is already in this portfolio. "
                "Use Address line 2 to distinguish a separate unit."
            )

    prop.name = name
    prop.property_type = property_type
    prop.acquisition_date = acquisition_date
    prop.purchase_price_cents = purchase_price_cents
    prop.land_basis_cents = land_basis_cents
    prop.building_basis_cents = building_basis_cents
    prop.down_payment_cents = down_payment_cents
    prop.closing_costs_cents = closing_costs_cents
    prop.furnishing_costs_cents = furnishing_costs_cents
    prop.short_label = clean(form.get("short_label")) or None
    return prop


def record_property_valuation(session: Session, prop: Property, form) -> None:
    """Records the owner's stated current value as a manual valuation."""
    value_cents = cents(form.get("current_valuation"), "Current valuation")
    if value_cents is None:
        return
    valuation_day = day(form.get("valuation_date"), "Valuation date") or date.today()
    latest = session.exec(
        select(PropertyValuation)
        .where(PropertyValuation.property_id == prop.id)
        .order_by(PropertyValuation.valuation_date.desc())
    ).first()
    if latest is not None and latest.valuation_date == valuation_day:
        latest.estimated_value_cents = value_cents
        latest.valuation_source = "CLIENT_MANUAL"
        session.add(latest)
        return
    session.add(PropertyValuation(
        id=new_id("val"),
        property_id=prop.id,
        valuation_date=valuation_day,
        estimated_value_cents=value_cents,
        valuation_source="CLIENT_MANUAL",
        notes="Owner-declared value from the portfolio setup screen",
    ))


def _coordinate(value, label: str, limit: int) -> float | None:
    text = clean(value)
    if not text:
        return None
    try:
        number = Decimal(text)
    except InvalidOperation:
        raise FormError(f"{label}: {value!r} is not a coordinate.")
    if not number.is_finite() or abs(number) > limit:
        raise FormError(f"{label} must be between -{limit} and {limit}.")
    return float(number)


def _clear_location(prop: Property) -> None:
    prop.google_place_id = None
    prop.address_source = "MANUAL"
    prop.latitude = None
    prop.longitude = None
    prop.location_cached_at = None


def apply_property_location(
    prop: Property,
    form,
    *,
    previous_address: tuple[str, str, str, str, str] | None = None,
    previous_place_id: str | None = None,
) -> None:
    """Applies the Google location fields, or clears them.

    A form that does not manage location at all leaves the stored location alone.
    A form that manages it always decides: a place ID pins the location, an empty
    place ID (or the explicit clear control) reverts the property to manual entry.

    ``previous_address`` and ``previous_place_id`` let the server catch a stale
    marker even if the browser failed to clear it: if the address changed while
    the same place ID was submitted, the location is dropped.
    """
    manages_location = any(
        field in form
        for field in ("google_place_id", "clear_location", "latitude", "longitude", "address_source")
    )
    if not manages_location:
        # An address-only edit still invalidates a marker: the owner changed the
        # address without selecting a new place.
        if (
            "address_line1" in form
            and previous_address is not None
            and _normalised(address_tuple(prop)) != _normalised(previous_address)
        ):
            _clear_location(prop)
        return

    place_id = clean(form.get("google_place_id")) or None
    clear_location = flag(form.get("clear_location"))
    latitude = _coordinate(form.get("latitude"), "Latitude", 90)
    longitude = _coordinate(form.get("longitude"), "Longitude", 180)
    source = clean(form.get("address_source")).upper()
    if source not in {"", "MANUAL", "GOOGLE"}:
        raise FormError("Location source must be MANUAL or GOOGLE.")
    if place_id is not None and not PLACE_ID_PATTERN.match(place_id):
        raise FormError("That Google place identifier is not valid.")
    if (latitude is None) != (longitude is None):
        raise FormError("Latitude and longitude must be provided together.")

    if clear_location or place_id is None:
        _clear_location(prop)
        return

    if (
        previous_address is not None
        and previous_place_id == place_id
        and _normalised(address_tuple(prop)) != _normalised(previous_address)
    ):
        # The address was edited without selecting a new place. Keeping the old
        # marker would display a location the owner no longer claims.
        _clear_location(prop)
        return

    prop.google_place_id = place_id
    prop.address_source = "GOOGLE"
    if latitude is not None:
        prop.latitude = latitude
        prop.longitude = longitude
        prop.location_cached_at = utc_now()
    else:
        prop.latitude = None
        prop.longitude = None
        prop.location_cached_at = None


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def fresh_coordinates(prop: Property, *, now: datetime | None = None) -> tuple[float, float] | None:
    """The cached coordinates, or ``None`` once the 30-day cache window has passed."""
    if prop.latitude is None or prop.longitude is None:
        return None
    cached_at = _aware(prop.location_cached_at)
    if cached_at is None:
        return None
    reference = _aware(now) or utc_now()
    if reference - cached_at > timedelta(days=COORDINATE_CACHE_DAYS):
        return None
    return (prop.latitude, prop.longitude)


def expire_stale_coordinates(session: Session, *, now: datetime | None = None) -> int:
    """Clears coordinates whose permitted cache window has elapsed.

    Returns the number of properties changed; the caller commits. Run this from a
    daily maintenance job so Google content is not retained past its cache window.
    """
    cutoff = (_aware(now) or utc_now()) - timedelta(days=COORDINATE_CACHE_DAYS)
    cached_column = Property.__table__.c.location_cached_at
    stale = session.exec(
        select(Property).where(
            cached_column.is_not(None),
            cached_column < cutoff,
        )
    ).all()
    for prop in stale:
        prop.latitude = None
        prop.longitude = None
        prop.location_cached_at = None
        session.add(prop)
    return len(stale)


def google_maps_url(prop: Property) -> str:
    """The property's Google Maps link, using a pinned place ID when one exists.

    A custom URL set by the owner still wins, because it is the owner's explicit
    choice rather than a derived address lookup.
    """
    if prop.google_maps_url and prop.google_maps_url.strip():
        return prop.google_maps_url.strip()

    parts = [prop.address_line1]
    if prop.address_line2:
        parts.append(prop.address_line2)
    locality = f"{prop.city}, {prop.state} {prop.zip_code}".strip()
    if locality:
        parts.append(locality)
    full_address = ", ".join(part for part in parts if part)

    query = {"api": "1", "query": full_address}
    if prop.google_place_id:
        query["query_place_id"] = prop.google_place_id
    return "https://www.google.com/maps/search/?" + urllib.parse.urlencode(query)
