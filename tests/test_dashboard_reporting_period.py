"""Phase 2.1 remediation §3: the dashboard label must match the calculation.

The dashboard previously showed a "Q1 2026 MTD" chip while computing over every
posted entry. These tests pin the displayed window to the dates the figures
actually came from, and prove that filtering really excludes out-of-window rows.
"""

from datetime import date

import pytest
from sqlmodel import Session

from proptally.db.connection import get_engine
from proptally.domain.accounting.ledger import (
    add_journal_line,
    create_journal_entry,
    post_journal_entry,
)

LARKSPUR = "prop_tinsley_huntley"


@pytest.fixture(name="client")
def client_fixture(owner, tmp_path):
    """A signed-in owner's client, pointed at a known portfolio database file.

    ``db_url`` is attached so the direct-posting helpers below open the same file.
    """
    db_path = tmp_path / "period.db"
    db_url = f"sqlite:///{db_path}"
    signed_in = owner(db_url=db_url, reference_portfolio=True)
    signed_in.client.db_url = db_url
    return signed_in.client


def _add_repair(client, day: date, amount_cents: int, key: str):
    """Posts a tagged repair directly, so no posting-policy gate is involved."""
    engine = get_engine(client.db_url)
    with Session(engine) as session:
        entry = create_journal_entry(
            session, day, "EXPENSE_POST", f"Repair {day}", key
        )
        add_journal_line(session, entry.id, 5040, debit_cents=amount_cents, property_id=LARKSPUR)
        add_journal_line(session, entry.id, 1010, credit_cents=amount_cents)
        post_journal_entry(session, entry.id)


def test_lifetime_label_states_the_dates_the_figures_cover(client):
    """With no filter the label must say lifetime-to-date and name the window."""
    _add_repair(client, date(2025, 11, 5), 10000, "PERIOD:2025")
    _add_repair(client, date(2026, 6, 9), 20000, "PERIOD:2026-06")

    html = client.get("/").text
    assert "Q1 2026 MTD" not in html
    assert "LIFETIME TO DATE" in html
    assert "All posted entries" in html
    # The label names the window of entries actually included: the 2025 repair I
    # posted is the earliest, the June 2026 repair the latest. Opening balances
    # are excluded from these metrics by design, so 01 Jan 2026 is not the floor.
    assert "05 Nov 2025" in html
    assert "09 Jun 2026" in html


def test_period_filter_excludes_entries_outside_the_window(client):
    """A $100 repair in 2025 and a $200 repair in June must not appear in Q1."""
    _add_repair(client, date(2025, 11, 5), 10000, "PERIOD:2025")
    _add_repair(client, date(2026, 6, 9), 20000, "PERIOD:2026-06")

    lifetime = client.get("/").text
    # Demo repairs 250.00 + 100.00 + 200.00 = 550.00
    assert "-$550.00" in lifetime
    assert "LIFETIME TO DATE" in lifetime

    q1 = client.get("/?start=2026-01-01&end=2026-03-31").text
    # Only the in-window demo repair remains: 250.00
    assert "-$250.00" in q1
    assert "-$550.00" not in q1
    assert "FILTERED WINDOW" in q1
    assert "01 Jan 2026" in q1 and "31 Mar 2026" in q1

    # Changing the window changes the figures again: June alone has the 200.00.
    june = client.get("/?start=2026-06-01&end=2026-06-30").text
    assert "-$200.00" in june
    assert "-$250.00" not in june


def test_filter_boundaries_are_inclusive(client):
    """An entry exactly on the boundary date belongs to the window."""
    _add_repair(client, date(2026, 3, 31), 30000, "PERIOD:BOUNDARY")

    on_boundary = client.get("/?start=2026-03-01&end=2026-03-31").text
    assert "-$550.00" in on_boundary  # 250 demo + 300 boundary

    day_before = client.get("/?start=2026-03-01&end=2026-03-30").text
    assert "-$250.00" in day_before


def test_invalid_or_inverted_period_is_rejected(client):
    assert client.get("/?start=not-a-date").status_code == 400
    assert client.get("/?end=2026-13-45").status_code == 400
    assert client.get("/?start=2026-06-01&end=2026-01-01").status_code == 400


def test_property_view_uses_the_same_window_labelling(client):
    """The two views must not contradict each other about the period."""
    dashboard = client.get("/").text
    detail = client.get(f"/properties/{LARKSPUR}").text
    assert "All posted entries" in detail
    assert "All posted entries" in dashboard
    assert "Q1 2026 MTD" not in detail


def test_year_and_range_conflict_is_still_rejected(client):
    """The earlier conflict handling survives the dashboard work."""
    from proptally.domain.accounting.metrics import calculate_property_financials

    engine = get_engine(client.db_url)
    with pytest.raises(ValueError, match="Conflicting reporting filters"), Session(engine) as session:
        calculate_property_financials(
            session, year=2026, start_date=date(2026, 1, 1)
        )


# --- period_label(): one-sided and empty windows ---------------------------

def test_period_label_covers_every_window_shape():
    """The label must describe a one-sided window as open-ended, never as lifetime."""
    from datetime import date

    from proptally.web.helpers import period_label

    both = {"period_start": date(2026, 3, 1), "period_end": date(2026, 3, 31),
            "coverage_start": date(2026, 3, 1), "coverage_end": date(2026, 3, 28)}
    assert period_label(both) == "01 Mar 2026 – 31 Mar 2026"

    start_only = {"period_start": date(2026, 3, 1), "period_end": None,
                  "coverage_start": date(2026, 3, 1), "coverage_end": date(2026, 3, 28)}
    assert period_label(start_only) == "From 01 Mar 2026 onwards"

    end_only = {"period_start": None, "period_end": date(2026, 3, 15),
                "coverage_start": date(2026, 3, 1), "coverage_end": date(2026, 3, 15)}
    assert period_label(end_only) == "Through 15 Mar 2026"

    unfiltered = {"period_start": None, "period_end": None,
                  "coverage_start": date(2026, 3, 1), "coverage_end": date(2026, 3, 28)}
    assert period_label(unfiltered) == "All posted entries · 01 Mar 2026 – 28 Mar 2026"

    nothing = {"period_start": None, "period_end": None,
               "coverage_start": None, "coverage_end": None}
    assert period_label(nothing) == "All posted entries · no posted activity"


def test_period_label_says_when_a_filtered_window_is_empty():
    """An empty filtered window must not be described as lifetime coverage."""
    from datetime import date

    from proptally.web.helpers import period_label

    empty_both = {"period_start": date(2027, 1, 1), "period_end": date(2027, 1, 31),
                  "coverage_start": None, "coverage_end": None}
    label = period_label(empty_both)
    assert label == "01 Jan 2027 – 31 Jan 2027 · no posted activity in this period"
    assert "All posted entries" not in label

    empty_start = {"period_start": date(2027, 1, 1), "period_end": None,
                   "coverage_start": None, "coverage_end": None}
    label = period_label(empty_start)
    assert label == "From 01 Jan 2027 onwards · no posted activity in this period"
    assert "All posted entries" not in label

    empty_end = {"period_start": None, "period_end": date(2025, 1, 31),
                 "coverage_start": None, "coverage_end": None}
    label = period_label(empty_end)
    assert label == "Through 31 Jan 2025 · no posted activity in this period"
    assert "All posted entries" not in label


def test_start_only_filter_is_labelled_open_ended(client):
    """?start alone means "from here on", and the figures follow that window."""
    html = client.get("/?start=2026-03-01").text
    assert "From 01 Mar 2026 onwards" in html
    assert "All posted entries" not in html
    assert "FILTERED WINDOW" in html
    # The seeded 01 Jan opening balance is outside the window; the March rent is in.
    assert "$2,600.00" in html


def test_end_only_filter_is_labelled_through_that_date(client):
    html = client.get("/?end=2026-03-15").text
    assert "Through 15 Mar 2026" in html
    assert "All posted entries" not in html
    # Nothing had been received by mid-March except the 01 Mar rent.
    assert "$2,600.00" in html


def test_empty_filtered_window_reports_no_activity_in_the_period(client):
    html = client.get("/?start=2027-01-01&end=2027-01-31").text
    assert "no posted activity in this period" in html
    assert "All posted entries" not in html
    assert "01 Jan 2027" in html and "31 Jan 2027" in html
