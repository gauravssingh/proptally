"""Control-plane staging for document-based onboarding.

An onboarding session belongs to one account and one portfolio, but its data does
**not** live in that portfolio's database. Until the customer approves what the
documents were found to say, nothing is written into the ledger: proposals,
extractions and evidence are staged here, in the control plane, and the portfolio is
only populated afterwards through the normal domain services.

That separation is the point. A failed extraction, a rejected record, or a document
that turns out to belong to somebody else never reaches a tenant database, and adding
this capability does not add a single table to every portfolio file.

Every query in the web layer and the worker is further scoped by ``user_id`` and
``tenant_id``; the session id is never enough on its own to read a staging row.
"""

from datetime import UTC, datetime

from sqlalchemy import CheckConstraint, UniqueConstraint
from sqlmodel import Field, SQLModel


def utc_now() -> datetime:
    """Return timezone-aware UTC datetime."""
    return datetime.now(UTC)


# --- Session and upload ----------------------------------------------------

ONBOARDING_CREATED = "CREATED"
ONBOARDING_UPLOADING = "UPLOADING"
ONBOARDING_UPLOADED = "UPLOADED"
ONBOARDING_PROCESSING = "PROCESSING"
ONBOARDING_READY = "READY_FOR_REVIEW"
ONBOARDING_PARTIAL = "PARTIALLY_PROCESSED"
ONBOARDING_PROCESSING_FAILED = "PROCESSING_FAILED"
ONBOARDING_REVIEWING = "REVIEWING"
ONBOARDING_APPROVED = "APPROVED"
ONBOARDING_PROVISIONING = "PROVISIONING"
ONBOARDING_PROVISIONING_FAILED = "PROVISIONING_FAILED"
ONBOARDING_COMPLETED = "COMPLETED"
ONBOARDING_CANCELLED = "CANCELLED"
ONBOARDING_EXPIRED = "EXPIRED"

ONBOARDING_STATES = (
    ONBOARDING_CREATED,
    ONBOARDING_UPLOADING,
    ONBOARDING_UPLOADED,
    ONBOARDING_PROCESSING,
    ONBOARDING_READY,
    ONBOARDING_PARTIAL,
    ONBOARDING_PROCESSING_FAILED,
    ONBOARDING_REVIEWING,
    ONBOARDING_APPROVED,
    ONBOARDING_PROVISIONING,
    ONBOARDING_PROVISIONING_FAILED,
    ONBOARDING_COMPLETED,
    ONBOARDING_CANCELLED,
    ONBOARDING_EXPIRED,
)

#: States in which the customer may still review and decide.
REVIEWABLE_STATES = (
    ONBOARDING_READY,
    ONBOARDING_PARTIAL,
    ONBOARDING_REVIEWING,
)

#: States in which a provisioning run may be started or retried.
PROVISIONABLE_STATES = (
    ONBOARDING_APPROVED,
    ONBOARDING_PROVISIONING_FAILED,
)


class OnboardingSession(SQLModel, table=True):
    """One customer's document-import journey for one portfolio."""

    __tablename__ = "onboarding_sessions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('CREATED', 'UPLOADING', 'UPLOADED', 'PROCESSING', "
            "'READY_FOR_REVIEW', 'PARTIALLY_PROCESSED', 'PROCESSING_FAILED', "
            "'REVIEWING', 'APPROVED', 'PROVISIONING', 'PROVISIONING_FAILED', "
            "'COMPLETED', 'CANCELLED', 'EXPIRED')",
            name="check_onboarding_session_status",
        ),
    )

    id: str = Field(primary_key=True)  # e.g. "onb_5c81..."
    user_id: str = Field(foreign_key="users.id", index=True)
    tenant_id: str = Field(foreign_key="tenants.id", index=True)
    status: str = Field(default=ONBOARDING_CREATED, index=True)
    #: What the customer is told about the last failure, in their words rather than
    #: a traceback. The durable job row keeps the technical detail.
    last_error: str | None = Field(default=None)
    approved_at: datetime | None = Field(default=None)
    approved_by_user_id: str | None = Field(default=None, foreign_key="users.id")
    completed_at: datetime | None = Field(default=None)
    expires_at: datetime | None = Field(default=None, index=True)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


UPLOAD_RECEIVED = "RECEIVED"
UPLOAD_VALIDATED = "VALIDATED"
UPLOAD_EXTRACTED = "EXTRACTED"
UPLOAD_FAILED = "FAILED"
UPLOAD_STATES = (UPLOAD_RECEIVED, UPLOAD_VALIDATED, UPLOAD_EXTRACTED, UPLOAD_FAILED)


class OnboardingUpload(SQLModel, table=True):
    """One ZIP archive received for an onboarding session."""

    __tablename__ = "onboarding_uploads"
    __table_args__ = (
        CheckConstraint(
            "status IN ('RECEIVED', 'VALIDATED', 'EXTRACTED', 'FAILED')",
            name="check_onboarding_upload_status",
        ),
    )

    id: str = Field(primary_key=True)
    onboarding_id: str = Field(foreign_key="onboarding_sessions.id", index=True)
    original_filename: str
    archive_sha256: str = Field(default="", index=True)
    #: Path relative to the onboarding store root, never an absolute path the
    #: customer could see or influence.
    storage_path: str
    size_bytes: int = Field(default=0)
    extracted_bytes: int = Field(default=0)
    file_count: int = Field(default=0)
    status: str = Field(default=UPLOAD_RECEIVED, index=True)
    error: str | None = Field(default=None)
    created_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = Field(default=None)


# --- Documents and extraction ---------------------------------------------

DOC_DISCOVERED = "DISCOVERED"
DOC_CLASSIFIED = "CLASSIFIED"
DOC_EXTRACTING = "EXTRACTING"
DOC_EXTRACTED = "EXTRACTED"
DOC_READY = "READY"
DOC_NEEDS_REVIEW = "NEEDS_REVIEW"
DOC_UNSUPPORTED = "UNSUPPORTED"
DOC_CORRUPT = "CORRUPT"
DOC_FAILED = "EXTRACTION_FAILED"
DOC_DUPLICATE = "DUPLICATE"

DOCUMENT_STATES = (
    DOC_DISCOVERED,
    DOC_CLASSIFIED,
    DOC_EXTRACTING,
    DOC_EXTRACTED,
    DOC_READY,
    DOC_NEEDS_REVIEW,
    DOC_UNSUPPORTED,
    DOC_CORRUPT,
    DOC_FAILED,
    DOC_DUPLICATE,
)

#: A terminal state needs no further processing.
TERMINAL_DOCUMENT_STATES = (
    DOC_READY,
    DOC_NEEDS_REVIEW,
    DOC_UNSUPPORTED,
    DOC_CORRUPT,
    DOC_FAILED,
    DOC_DUPLICATE,
)

DOC_CATEGORIES = (
    "PROPERTY_DEED",
    "MORTGAGE_STATEMENT",
    "LOAN_AGREEMENT",
    "BANK_STATEMENT",
    "RENTAL_AGREEMENT",
    "PROPERTY_TAX",
    "INSURANCE",
    "VALUATION",
    "MAINTENANCE",
    "UNKNOWN",
)


class OnboardingDocument(SQLModel, table=True):
    """One file inside an uploaded archive, staged outside every portfolio database."""

    __tablename__ = "onboarding_documents"
    __table_args__ = (
        CheckConstraint(
            "status IN ('DISCOVERED', 'CLASSIFIED', 'EXTRACTING', 'EXTRACTED', "
            "'READY', 'NEEDS_REVIEW', 'UNSUPPORTED', 'CORRUPT', "
            "'EXTRACTION_FAILED', 'DUPLICATE')",
            name="check_onboarding_document_status",
        ),
    )

    id: str = Field(primary_key=True)
    onboarding_id: str = Field(foreign_key="onboarding_sessions.id", index=True)
    upload_id: str = Field(foreign_key="onboarding_uploads.id", index=True)
    original_filename: str
    #: Generated name actually used on disk. The original is metadata only, so a
    #: crafted name cannot influence where a byte lands.
    internal_filename: str
    storage_path: str
    sha256: str = Field(default="", index=True)
    size_bytes: int = Field(default=0)
    mime_type: str = Field(default="application/octet-stream")
    category: str = Field(default="UNKNOWN")
    #: Native PDF, scanned PDF, image, DOCX, XLSX, CSV, unsupported, corrupt.
    format: str = Field(default="UNKNOWN")
    status: str = Field(default=DOC_DISCOVERED, index=True)
    page_count: int = Field(default=0)
    text_chars: int = Field(default=0)
    extraction_method: str | None = Field(default=None)
    error: str | None = Field(default=None)
    duplicate_of_document_id: str | None = Field(default=None)
    #: Set once the approved source document has been copied into the portfolio's
    #: own vault. That is the auditable link from staging to the permanent record.
    tenant_document_id: str | None = Field(default=None)
    created_at: datetime = Field(default_factory=utc_now)
    processed_at: datetime | None = Field(default=None)


class OnboardingExtraction(SQLModel, table=True):
    """Structured output for one document, versioned so a schema can evolve."""

    __tablename__ = "onboarding_extractions"

    id: str = Field(primary_key=True)
    onboarding_id: str = Field(foreign_key="onboarding_sessions.id", index=True)
    document_id: str = Field(foreign_key="onboarding_documents.id", index=True)
    schema_name: str
    schema_version: str
    payload_json: str
    #: The extractor that produced this payload, e.g. ``rules`` or ``ocr``.
    method: str
    created_at: datetime = Field(default_factory=utc_now)


# --- Proposals, evidence and decisions -------------------------------------

PROPOSAL_NEEDS_REVIEW = "NEEDS_REVIEW"
PROPOSAL_ACCEPTED = "ACCEPTED"
PROPOSAL_REJECTED = "REJECTED"
PROPOSAL_DEFERRED = "DEFERRED"
PROPOSAL_IMPORTED = "IMPORTED"

PROPOSAL_STATES = (
    PROPOSAL_NEEDS_REVIEW,
    PROPOSAL_ACCEPTED,
    PROPOSAL_REJECTED,
    PROPOSAL_DEFERRED,
    PROPOSAL_IMPORTED,
)

ENTITY_PROPERTY = "PROPERTY"
ENTITY_MORTGAGE = "MORTGAGE"
ENTITY_ACCOUNT = "ACCOUNT"
ENTITY_LEASE = "LEASE"

ENTITY_TYPES = (ENTITY_PROPERTY, ENTITY_MORTGAGE, ENTITY_ACCOUNT, ENTITY_LEASE)


class OnboardingProposal(SQLModel, table=True):
    """A proposed domain record, with the evidence that supports it.

    ``original_payload_json`` is what extraction produced and is never rewritten.
    ``payload_json`` is what the customer has agreed to so far; every change is also
    recorded in :class:`OnboardingReviewDecision`, which is the audit trail.
    """

    __tablename__ = "onboarding_proposals"
    __table_args__ = (
        CheckConstraint(
            "entity_type IN ('PROPERTY', 'MORTGAGE', 'ACCOUNT', 'LEASE')",
            name="check_onboarding_proposal_entity_type",
        ),
        CheckConstraint(
            "status IN ('NEEDS_REVIEW', 'ACCEPTED', 'REJECTED', 'DEFERRED', 'IMPORTED')",
            name="check_onboarding_proposal_status",
        ),
    )

    id: str = Field(primary_key=True)
    onboarding_id: str = Field(foreign_key="onboarding_sessions.id", index=True)
    entity_type: str = Field(index=True)
    status: str = Field(default=PROPOSAL_NEEDS_REVIEW, index=True)
    #: Stable natural key used to merge documents that describe the same thing.
    group_key: str = Field(default="", index=True)
    #: Human label shown in lists; the payload holds the values.
    title: str
    original_payload_json: str
    payload_json: str
    #: JSON object: field -> list of {value, document_id, page, evidence, confidence}.
    conflicts_json: str = Field(default="{}")
    #: JSON object: field -> {original, revised}. The original extraction survives
    #: in ``original_payload_json`` as well.
    edited_fields_json: str = Field(default="{}")
    source_document_ids_json: str = Field(default="[]")
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class OnboardingFieldEvidence(SQLModel, table=True):
    """Where one proposed value came from: document, page and supporting text."""

    __tablename__ = "onboarding_field_evidence"

    id: str = Field(primary_key=True)
    onboarding_id: str = Field(foreign_key="onboarding_sessions.id", index=True)
    proposal_id: str = Field(foreign_key="onboarding_proposals.id", index=True)
    field_name: str
    #: JSON-encoded typed value, so a list and an integer are distinguishable.
    value_json: str
    document_id: str = Field(foreign_key="onboarding_documents.id", index=True)
    page_number: int = Field(default=1)
    evidence_text: str = Field(default="")
    method: str = Field(default="RULE")
    confidence: float = Field(default=0.7)
    created_at: datetime = Field(default_factory=utc_now)


DECISION_ACCEPT = "ACCEPT"
DECISION_REJECT = "REJECT"
DECISION_DEFER = "DEFER"
DECISION_EDIT = "EDIT"
DECISION_MARK_UNKNOWN = "MARK_UNKNOWN"
DECISION_RESOLVE = "RESOLVE_CONFLICT"
DECISION_RESTORE = "RESTORE"

DECISIONS = (
    DECISION_ACCEPT,
    DECISION_REJECT,
    DECISION_DEFER,
    DECISION_EDIT,
    DECISION_MARK_UNKNOWN,
    DECISION_RESOLVE,
    DECISION_RESTORE,
)


class OnboardingReviewDecision(SQLModel, table=True):
    """Append-only record of what the customer decided, and what it replaced."""

    __tablename__ = "onboarding_review_decisions"

    id: str = Field(primary_key=True)
    onboarding_id: str = Field(foreign_key="onboarding_sessions.id", index=True)
    proposal_id: str | None = Field(
        default=None, foreign_key="onboarding_proposals.id", index=True
    )
    field_name: str | None = Field(default=None)
    decision: str = Field(index=True)
    previous_value_json: str | None = Field(default=None)
    new_value_json: str | None = Field(default=None)
    actor_user_id: str = Field(foreign_key="users.id", index=True)
    note: str | None = Field(default=None)
    created_at: datetime = Field(default_factory=utc_now)


# --- Durable jobs ----------------------------------------------------------

JOB_PENDING = "PENDING"
JOB_RUNNING = "RUNNING"
JOB_DONE = "DONE"
JOB_FAILED = "FAILED"

JOB_STATES = (JOB_PENDING, JOB_RUNNING, JOB_DONE, JOB_FAILED)

JOB_DOCUMENT = "PROCESS_DOCUMENT"
JOB_FINALIZE = "FINALIZE_ONBOARDING"
JOB_PROVISION = "PROVISION_PORTFOLIO"

JOB_KINDS = (JOB_DOCUMENT, JOB_FINALIZE, JOB_PROVISION)


class OnboardingJob(SQLModel, table=True):
    """A durable unit of background work.

    The worker claims a row with a conditional UPDATE, so two workers (or one worker
    and a retry request) cannot process the same document twice. A failed document is
    a failed row, not a failed archive.
    """

    __tablename__ = "onboarding_jobs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('PENDING', 'RUNNING', 'DONE', 'FAILED')",
            name="check_onboarding_job_status",
        ),
        CheckConstraint(
            "kind IN ('PROCESS_DOCUMENT', 'FINALIZE_ONBOARDING', 'PROVISION_PORTFOLIO')",
            name="check_onboarding_job_kind",
        ),
    )

    id: str = Field(primary_key=True)
    onboarding_id: str = Field(foreign_key="onboarding_sessions.id", index=True)
    kind: str = Field(index=True)
    document_id: str | None = Field(
        default=None, foreign_key="onboarding_documents.id", index=True
    )
    status: str = Field(default=JOB_PENDING, index=True)
    attempts: int = Field(default=0)
    max_attempts: int = Field(default=3)
    error: str | None = Field(default=None)
    started_at: datetime | None = Field(default=None)
    finished_at: datetime | None = Field(default=None)
    created_at: datetime = Field(default_factory=utc_now)


# --- Provisioning manifest -------------------------------------------------

PROVISION_DONE = "DONE"
PROVISION_FAILED = "FAILED"

PROVISION_STATES = (PROVISION_DONE, PROVISION_FAILED)


class OnboardingProvisionEntry(SQLModel, table=True):
    """One proposal's journey into the portfolio, for idempotent recovery.

    The unique proposal key is the contract: a retry after a partial failure skips
    everything already recorded here and never creates a second property, mortgage
    or account.
    """

    __tablename__ = "onboarding_provision_entries"
    __table_args__ = (
        UniqueConstraint("onboarding_id", "proposal_id", name="uq_onboarding_provision_proposal"),
        CheckConstraint(
            "status IN ('DONE', 'FAILED')",
            name="check_onboarding_provision_status",
        ),
    )

    id: str = Field(primary_key=True)
    onboarding_id: str = Field(foreign_key="onboarding_sessions.id", index=True)
    proposal_id: str = Field(foreign_key="onboarding_proposals.id", index=True)
    entity_type: str
    tenant_entity_id: str | None = Field(default=None)
    status: str = Field(default=PROVISION_DONE)
    error: str | None = Field(default=None)
    created_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = Field(default=None)


ONBOARDING_TABLES = (
    OnboardingSession.__table__,
    OnboardingUpload.__table__,
    OnboardingDocument.__table__,
    OnboardingExtraction.__table__,
    OnboardingProposal.__table__,
    OnboardingFieldEvidence.__table__,
    OnboardingReviewDecision.__table__,
    OnboardingJob.__table__,
    OnboardingProvisionEntry.__table__,
)
