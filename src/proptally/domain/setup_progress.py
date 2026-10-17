"""The guided setup checklist, evaluated from the portfolio's own data.

No "done" tick is stored. Each step asks the database a question, so deleting a
property, account or opening balance makes the tracker honest again. The only
stored intent is an explicit skip (see :mod:`proptally.domain.setup_state`).
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlmodel import Session, select

from proptally.db.models import (
    FinancialAccount,
    JournalEntry,
    LoanAccount,
    Property,
    Statement,
    Transaction,
)
from proptally.domain.setup_state import load, skipped_steps


@dataclass(frozen=True)
class SetupStep:
    key: str
    title: str
    short: str
    description: str
    why: str
    weight: int = 20
    optional: bool = False


#: The five essentials. The mortgage step is optional because a portfolio can be
#: owned free and clear; skipping it removes it from the denominator.
STEPS: tuple[SetupStep, ...] = (
    SetupStep(
        key="property",
        title="Add your first property",
        short="Property",
        description="The door the portfolio reports on.",
        why=(
            "Every figure in PropTally is attached to a door. A rental property is "
            "a Schedule E asset; a primary residence is a non-operating asset."
        ),
    ),
    SetupStep(
        key="accounts",
        title="Add a bank or card account",
        short="Accounts",
        description="Where rent lands and where expenses are paid.",
        why=(
            "Transactions need an account to land in. A checking account and a "
            "rental card are enough to start; more can be added later."
        ),
    ),
    SetupStep(
        key="mortgage",
        title="Add a mortgage loan",
        short="Mortgage",
        description="The debt on the property, if there is one.",
        why=(
            "A loan carries its own principal, clearing and escrow ledger accounts. "
            "If the property is owned free and clear, skip this step."
        ),
        optional=True,
    ),
    SetupStep(
        key="opening-balances",
        title="Record opening balances",
        short="Opening balances",
        description="One dated baseline for the portfolio.",
        why=(
            "The opening balance is the starting point for equity: the property's "
            "cost basis, cash on hand and any card balances as at one date."
        ),
    ),
    SetupStep(
        key="records",
        title="Bring in your first records",
        short="First records",
        description="A statement or a first manual transaction.",
        why=(
            "The review queue and reports come alive once there is a transaction. "
            "Import a statement, or enter one rent deposit by hand."
        ),
    ),
)

STEP_BY_KEY = {step.key: step for step in STEPS}
STEP_KEYS = tuple(step.key for step in STEPS)

#: The first four steps are the manual-onboarding gate. The records step is the
#: start of the next phase (statements and review), so it is recommended but not
#: required to publish.
ESSENTIAL_STEP_KEYS = ("property", "accounts", "mortgage", "opening-balances")


@dataclass(frozen=True)
class SetupStepState:
    step: SetupStep
    done: bool
    skipped: bool
    href: str


@dataclass(frozen=True)
class SetupProgress:
    steps: tuple[SetupStepState, ...]
    percent: int
    done_count: int
    total: int
    next_step: str | None
    complete: bool
    enrolled: bool
    has_properties: bool
    published: bool
    publishable: bool


def _has_properties(session: Session) -> bool:
    return session.exec(
        select(Property).where(Property.is_active == True)
    ).first() is not None


def _step_done(session: Session, key: str) -> bool:
    if key == "property":
        return _has_properties(session)
    if key == "accounts":
        return session.exec(
            select(FinancialAccount).where(FinancialAccount.account_type != "MORTGAGE")
        ).first() is not None
    if key == "mortgage":
        return session.exec(select(LoanAccount)).first() is not None
    if key == "opening-balances":
        return session.exec(
            select(JournalEntry).where(
                JournalEntry.entry_type == "OPENING_BALANCE",
                JournalEntry.status == "POSTED",
            )
        ).first() is not None
    if key == "records":
        if session.exec(select(Statement)).first() is not None:
            return True
        return session.exec(select(Transaction)).first() is not None
    return False


def evaluate(session: Session) -> SetupProgress:
    setup = load(session)
    has_properties = _has_properties(session)
    skips = skipped_steps(setup)

    states: list[SetupStepState] = []
    for step in STEPS:
        done = _step_done(session, step.key)
        # A skip is only meaningful while the step is still undone. Adding a loan
        # after saying "no mortgage" makes the step applicable and complete again.
        skipped = step.optional and step.key in skips and not done
        states.append(SetupStepState(
            step=step,
            done=done,
            skipped=skipped,
            href=f"/setup/{step.key}",
        ))

    applicable = [state for state in states if not state.skipped]
    total = len(applicable)
    done_count = sum(1 for state in applicable if state.done)
    weight_total = sum(state.step.weight for state in applicable)
    weight_done = sum(state.step.weight for state in applicable if state.done)
    percent = int(weight_done * 100 / weight_total) if weight_total else 0
    if percent >= 100 and done_count < total:
        percent = 99

    next_step = next(
        (state.step.key for state in applicable if not state.done),
        None,
    )
    complete = done_count == total

    # A portfolio with no doors is enrolled by definition: there is nothing to
    # manage yet. A populated legacy portfolio is not enrolled until its owner
    # opens the guided setup explicitly, so nobody is retroactively nagged.
    enrolled = setup is not None or not has_properties

    # A legacy portfolio (no setup row) with doors predates the draft flow and is
    # already live. A new portfolio is a draft until its owner publishes it.
    published = (
        setup.published_at is not None if setup is not None else has_properties
    )
    essential_states = [
        state for state in states if state.step.key in ESSENTIAL_STEP_KEYS
    ]
    publishable = all(state.done or state.skipped for state in essential_states)

    return SetupProgress(
        steps=tuple(states),
        percent=percent,
        done_count=done_count,
        total=total,
        next_step=next_step,
        complete=complete,
        enrolled=enrolled,
        has_properties=has_properties,
        published=published,
        publishable=publishable,
    )
