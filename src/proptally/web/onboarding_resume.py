"""Resume context for document onboarding, shared by the choice and overview screens.

The onboarding session lives in the control database while the portfolio is still
empty. Both the onboarding entry screen and the empty Portfolio Overview need the
same "where did the import stop?" card, so the status mapping lives here rather
than inside one router.
"""

from __future__ import annotations

from sqlmodel import Session, select

from proptally.db.onboarding_models import (
    ONBOARDING_APPROVED,
    ONBOARDING_CANCELLED,
    ONBOARDING_COMPLETED,
    ONBOARDING_PARTIAL,
    ONBOARDING_PROCESSING,
    ONBOARDING_PROCESSING_FAILED,
    ONBOARDING_PROVISIONING,
    ONBOARDING_PROVISIONING_FAILED,
    ONBOARDING_READY,
    ONBOARDING_REVIEWING,
    ONBOARDING_UPLOADING,
    OnboardingSession,
)

SESSION_LABELS = {
    ONBOARDING_UPLOADING: "Uploading",
    ONBOARDING_PROCESSING: "Processing",
    ONBOARDING_READY: "Ready for review",
    ONBOARDING_PARTIAL: "Partially processed",
    ONBOARDING_PROCESSING_FAILED: "Processing failed",
    ONBOARDING_REVIEWING: "In review",
    ONBOARDING_APPROVED: "Approved",
    ONBOARDING_PROVISIONING: "Creating portfolio",
    ONBOARDING_PROVISIONING_FAILED: "Import failed",
    ONBOARDING_COMPLETED: "Portfolio created",
    ONBOARDING_CANCELLED: "Cancelled",
    "EXPIRED": "Expired",
    "CREATED": "Started",
}

SESSION_VARIANTS = {
    ONBOARDING_PROCESSING: "info",
    ONBOARDING_READY: "accent",
    ONBOARDING_PARTIAL: "warning",
    ONBOARDING_PROCESSING_FAILED: "error",
    ONBOARDING_REVIEWING: "info",
    ONBOARDING_APPROVED: "accent",
    ONBOARDING_PROVISIONING: "info",
    ONBOARDING_PROVISIONING_FAILED: "error",
    ONBOARDING_COMPLETED: "success",
    ONBOARDING_CANCELLED: "neutral",
    "EXPIRED": "neutral",
}

#: A finished, cancelled or expired import has nothing left to act on. Everything
#: else is still on the customer's path to a portfolio.
ACTIONABLE_STATUSES = {
    "CREATED",
    ONBOARDING_UPLOADING,
    ONBOARDING_PROCESSING,
    ONBOARDING_READY,
    ONBOARDING_PARTIAL,
    ONBOARDING_PROCESSING_FAILED,
    ONBOARDING_REVIEWING,
    ONBOARDING_APPROVED,
    ONBOARDING_PROVISIONING,
    ONBOARDING_PROVISIONING_FAILED,
}


def journey_steps(status: str | None) -> list[dict[str, str]]:
    labels = ("Upload", "Process", "Review")
    if status in {ONBOARDING_UPLOADING, "CREATED", None}:
        states = ("current", "upcoming", "upcoming")
    elif status in {ONBOARDING_PROCESSING, ONBOARDING_PROCESSING_FAILED}:
        states = ("done", "current", "upcoming")
    elif status == ONBOARDING_COMPLETED:
        states = ("done", "done", "done")
    else:
        states = ("done", "done", "current")
    return [{"label": label, "state": state} for label, state in zip(labels, states)]


def resume_context(item: OnboardingSession | None) -> dict | None:
    if item is None:
        return None
    status = item.status
    resumable = {
        "CREATED", ONBOARDING_UPLOADING, ONBOARDING_PROCESSING,
        ONBOARDING_READY, ONBOARDING_PARTIAL, ONBOARDING_PROCESSING_FAILED,
        ONBOARDING_REVIEWING, ONBOARDING_APPROVED, ONBOARDING_PROVISIONING_FAILED,
    }
    copy_by_status = {
        ONBOARDING_UPLOADING: (
            "An import is being uploaded",
            "Finish the upload to start processing.",
        ),
        ONBOARDING_PROCESSING: (
            "An import is already under way",
            "Documents are read in the background. You can leave this page and come back.",
        ),
        ONBOARDING_READY: (
            "An import is ready for your review",
            "PropTally has grouped your documents. Review every proposed record before creating the portfolio.",
        ),
        ONBOARDING_PARTIAL: (
            "An import is ready, with files needing attention",
            "Review what was found, or retry the documents that could not be read.",
        ),
        ONBOARDING_PROCESSING_FAILED: (
            "Your last import needs attention",
            "No records could be proposed. Inspect the documents and retry.",
        ),
        ONBOARDING_REVIEWING: (
            "An import is part-way through review",
            "Continue accepting, editing or deferring the proposed records.",
        ),
        ONBOARDING_APPROVED: (
            "An import is approved",
            "PropTally is ready to create the portfolio.",
        ),
        ONBOARDING_PROVISIONING: (
            "An import is creating the portfolio",
            "This continues in the background. You can leave this page and come back.",
        ),
        ONBOARDING_PROVISIONING_FAILED: (
            "The portfolio import needs a retry",
            "Records already created were kept. Retrying continues from where it stopped.",
        ),
        ONBOARDING_COMPLETED: (
            "Your last import created a portfolio",
            "Open it to see what was created, or start another import.",
        ),
        ONBOARDING_CANCELLED: (
            "Your last import was cancelled",
            "Its staged documents were removed.",
        ),
        "EXPIRED": (
            "Your last import expired",
            "Staged documents were removed after 30 days.",
        ),
    }
    default = (
        "Continue your last import",
        "Open the import to see where it stopped.",
    )
    headline, description = copy_by_status.get(status, default)
    if status in {ONBOARDING_READY, ONBOARDING_PARTIAL, ONBOARDING_REVIEWING}:
        action = "Review proposed portfolio"
        href = f"/onboarding/import/{item.id}/review"
    elif status in {ONBOARDING_PROCESSING, ONBOARDING_UPLOADING}:
        action = "View progress"
        href = f"/onboarding/import/{item.id}"
    elif status == ONBOARDING_COMPLETED:
        action = "Continue portfolio setup"
        href = "/setup"
    elif status in {ONBOARDING_PROVISIONING, ONBOARDING_PROVISIONING_FAILED}:
        action = "View import"
        href = f"/onboarding/import/{item.id}"
    elif status in resumable:
        action = "Resume import"
        href = f"/onboarding/import/{item.id}"
    else:
        action = "View import"
        href = f"/onboarding/import/{item.id}"
    return {
        "session": item,
        "label": SESSION_LABELS.get(status, status),
        "variant": SESSION_VARIANTS.get(status, "neutral"),
        "headline": headline,
        "description": description,
        "action": action,
        "href": href,
        "steps": journey_steps(status),
        "needs_action": status in ACTIONABLE_STATUSES,
    }


def latest_session(control_engine, user_id: str, tenant_id: str) -> OnboardingSession | None:
    """The newest onboarding session for this account and portfolio."""
    with Session(control_engine) as control:
        onboarding = control.exec(
            select(OnboardingSession)
            .where(
                OnboardingSession.user_id == user_id,
                OnboardingSession.tenant_id == tenant_id,
            )
            .order_by(OnboardingSession.created_at.desc())
            .limit(1)
        ).first()
        if onboarding is not None:
            control.expunge(onboarding)
        return onboarding


def latest_resume(control_engine, user_id: str, tenant_id: str) -> dict | None:
    return resume_context(latest_session(control_engine, user_id, tenant_id))
