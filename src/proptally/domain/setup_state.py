"""Intent-only state for the guided portfolio setup.

Completion is derived from the portfolio's data. This module stores only the
facts that cannot be inferred: whether the owner explicitly said a property has
no mortgage, and whether the portfolio has been enrolled in the guided flow.
"""

from __future__ import annotations

from sqlmodel import Session

from proptally.db.models import PortfolioSetup, utc_now

SETUP_ID = "portfolio"
SKIPPABLE_STEPS = frozenset({"mortgage"})


def load(session: Session) -> PortfolioSetup | None:
    return session.get(PortfolioSetup, SETUP_ID)


def ensure(session: Session, *, already_live: bool = False) -> PortfolioSetup:
    """The setup row, created on first use. The caller commits.

    ``already_live`` is for a portfolio that predates the draft flow: opening
    Guided Setup must not ask its owner to publish a portfolio that has been in
    use. A brand-new portfolio is a draft.
    """
    setup = load(session)
    if setup is None:
        setup = PortfolioSetup(
            id=SETUP_ID,
            published_at=utc_now() if already_live else None,
        )
        session.add(setup)
        session.flush()
    return setup


def publish(session: Session) -> PortfolioSetup:
    """Publishes the portfolio. The caller checks completeness and commits."""
    setup = ensure(session)
    if setup.published_at is None:
        setup.published_at = utc_now()
    setup.updated_at = utc_now()
    session.add(setup)
    return setup


def skipped_steps(setup: PortfolioSetup | None) -> set[str]:
    if setup is None or not setup.skipped_steps:
        return set()
    return {key.strip() for key in setup.skipped_steps.split(",") if key.strip()}


def skip_step(session: Session, key: str) -> PortfolioSetup:
    if key not in SKIPPABLE_STEPS:
        raise ValueError(f"{key!r} is not a skippable setup step.")
    setup = ensure(session)
    keys = skipped_steps(setup)
    keys.add(key)
    setup.skipped_steps = ",".join(sorted(keys))
    setup.updated_at = utc_now()
    session.add(setup)
    return setup


def unskip_step(session: Session, key: str) -> PortfolioSetup:
    setup = ensure(session)
    keys = skipped_steps(setup)
    keys.discard(key)
    setup.skipped_steps = ",".join(sorted(keys))
    setup.updated_at = utc_now()
    session.add(setup)
    return setup
