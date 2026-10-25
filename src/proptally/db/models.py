"""SQLModel domain entities matching PropTally V2.2 Database Architecture."""

from datetime import date, datetime, timezone
from typing import Optional

from sqlalchemy import CheckConstraint, Index, UniqueConstraint
from sqlmodel import Field, SQLModel


def utc_now() -> datetime:
    """Return timezone-aware UTC datetime."""
    return datetime.now(timezone.utc)


# --- 1. Property & Rental Domain ---

class Property(SQLModel, table=True):
    __tablename__ = "properties"
    __table_args__ = (
        CheckConstraint(
            "property_type IN ('RENTAL_INVESTMENT', 'PRIMARY_RESIDENCE')",
            name="check_property_type",
        ),
    )

    id: str = Field(primary_key=True)  # e.g. "prop_tinsley_huntley"
    name: str
    address_line1: str
    address_line2: Optional[str] = None
    city: str
    state: str = Field(default="IL")
    zip_code: str
    property_type: str = Field(default="RENTAL_INVESTMENT")  # 'RENTAL_INVESTMENT' | 'PRIMARY_RESIDENCE'
    acquisition_date: Optional[date] = None
    purchase_price_cents: Optional[int] = None
    land_basis_cents: Optional[int] = None
    building_basis_cents: Optional[int] = None
    # Acquisition costs entered by the owner or read from a closing statement.
    down_payment_cents: Optional[int] = None
    closing_costs_cents: Optional[int] = None
    furnishing_costs_cents: Optional[int] = None
    is_active: bool = Field(default=True)
    description: Optional[str] = Field(default=None)
    google_maps_url: Optional[str] = Field(default=None)
    #: Google Places identifier for the confirmed location. Place IDs may be cached
    #: indefinitely, so the map preview and the Google Maps link depend on this rather
    #: than on coordinates, which Google permits us to cache for only 30 days.
    google_place_id: Optional[str] = Field(default=None)
    #: How the address was established: MANUAL (typed, no Google location attached)
    #: or GOOGLE (a Places suggestion was selected and confirmed).
    address_source: str = Field(default="MANUAL")
    #: Coordinates are a cache, not the property record. They are written when a
    #: suggestion is selected, and are only exposed while less than 30 days old.
    latitude: Optional[float] = Field(default=None)
    longitude: Optional[float] = Field(default=None)
    location_cached_at: Optional[datetime] = Field(default=None)
    cover_photo_id: Optional[str] = Field(default=None)
    # The property's fixed-asset ledger account. Allocated on creation by
    # domain.accounting.coa; readers use it instead of assuming a code per door.
    asset_ledger_code: Optional[int] = Field(default=None, foreign_key="ledger_accounts.code")
    # Optional compact label for navigation; when absent the shell derives one
    # from the street address rather than matching on the property's id.
    short_label: Optional[str] = Field(default=None)
    created_at: datetime = Field(default_factory=utc_now)


class PortfolioSetup(SQLModel, table=True):
    """Where the owner is in the guided portfolio setup.

    Completion itself is derived from the portfolio's data, never stored here: a
    stored "done" tick would lie after a property or account was removed. This row
    only remembers intent that cannot be inferred -- which optional steps the owner
    explicitly skipped -- and marks a portfolio as enrolled in the guided flow.
    """

    __tablename__ = "portfolio_setup"

    id: str = Field(primary_key=True, default="portfolio")
    #: Comma-separated step keys the owner explicitly skipped (currently only
    #: ``mortgage``). An empty string means no step has been skipped.
    skipped_steps: str = Field(default="")
    #: When the owner published the portfolio. ``None`` is the draft stage:
    #: setup and data entry continue, and the dashboard says so.
    published_at: Optional[datetime] = Field(default=None)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class PropertyPhoto(SQLModel, table=True):
    __tablename__ = "property_photos"

    id: str = Field(primary_key=True)
    property_id: str = Field(foreign_key="properties.id", index=True)
    filename: str
    storage_path: str
    caption: Optional[str] = None
    is_cover: bool = Field(default=False)
    created_at: datetime = Field(default_factory=utc_now)


class PropertyValuation(SQLModel, table=True):
    __tablename__ = "property_valuations"

    id: str = Field(primary_key=True)
    property_id: str = Field(foreign_key="properties.id")
    valuation_date: date
    estimated_value_cents: int
    valuation_source: str  # 'PURCHASE_PRICE', 'APPRAISAL', 'CLIENT_MANUAL'
    notes: Optional[str] = None
    created_at: datetime = Field(default_factory=utc_now)


class Lease(SQLModel, table=True):
    __tablename__ = "leases"

    id: str = Field(primary_key=True)
    property_id: str = Field(foreign_key="properties.id")
    tenant_name: str
    start_date: date
    end_date: Optional[date] = None
    monthly_rent_cents: int
    #: A deposit that was not stated is unknown, not equal to one month's rent.
    security_deposit_cents: Optional[int] = None
    document_id: Optional[str] = Field(default=None, foreign_key="documents.id")
    is_active: bool = Field(default=True)
    created_at: datetime = Field(default_factory=utc_now)


class TenantAlias(SQLModel, table=True):
    """A bank remitter confirmed to be a known tenant on one rental door.

    The remitter a bank prints is not the tenant's name: it carries initials,
    ordering and casing the lease does not. This record is the bridge, written
    when a person confirms a rent payment, so later payments from the same
    remitter are attributed without asking again. ``alias_key`` is the
    normalised form (lower case, single letters and company suffixes removed)
    and is unique within a portfolio database.
    """

    __tablename__ = "tenant_aliases"

    id: str = Field(primary_key=True)  # "alias_<sha1(alias_key)>"
    alias_key: str = Field(index=True)  # normalised remitter, see rent._name_key
    display_name: str  # as the bank printed it, for the review table
    tenant_name: str  # the name on the lease, when one exists
    property_id: str = Field(foreign_key="properties.id")
    lease_id: Optional[str] = Field(default=None, foreign_key="leases.id")
    created_at: datetime = Field(default_factory=utc_now)


class RentDismissal(SQLModel, table=True):
    """A transaction a person has ruled out as rent received.

    Discovery proposes rent from bank wording and confirmed remitters, and an
    amount equal to a door's rent is a hint, not evidence. When the owner says a
    payment is not rent, this record keeps it out of the rent workbench; the
    transaction itself stays in the review queue for its real classification.
    """

    __tablename__ = "rent_candidate_dismissals"

    transaction_id: str = Field(primary_key=True, foreign_key="transactions.id")
    reason: Optional[str] = None
    created_at: datetime = Field(default_factory=utc_now)


# --- 2. Financial Accounts & Mortgage Domain ---

class FinancialInstitution(SQLModel, table=True):
    __tablename__ = "financial_institutions"

    id: str = Field(primary_key=True)  # e.g. "inst_rocket", "inst_pnc"
    name: str
    routing_number: Optional[str] = None
    website: Optional[str] = None
    created_at: datetime = Field(default_factory=utc_now)


class FinancialAccount(SQLModel, table=True):
    __tablename__ = "financial_accounts"
    __table_args__ = (
        CheckConstraint(
            "mixed_use_status IN ('DEDICATED_RENTAL', 'MIXED_USE', 'UNCLASSIFIED')",
            name="check_account_mixed_use_status",
        ),
        CheckConstraint(
            "account_type IN ('CHECKING', 'SAVINGS', 'CREDIT_CARD', 'MORTGAGE')",
            name="check_account_type",
        ),
    )

    id: str = Field(primary_key=True)  # e.g. "acct_bofa_chk_9456"
    institution_id: str = Field(foreign_key="financial_institutions.id")
    account_name: str
    account_type: str  # 'CHECKING', 'CREDIT_CARD', 'MORTGAGE'
    account_number_masked: str  # e.g. "...9456"
    # The ledger account this deposit/card account settles in, allocated on
    # creation (see domain.accounting.coa). NULL for MORTGAGE accounts, whose
    # debt, clearing and escrow accounts belong to the LoanAccount instead.
    ledger_account_code: Optional[int] = Field(default=None, foreign_key="ledger_accounts.code")
    # Whether a credit card is used only for rental costs or shared with personal
    # spending. Only MIXED_USE cards need external review of rental deductions;
    # a dedicated rental card does not. UNCLASSIFIED is the safe default: an
    # account whose usage is unknown is never assumed to be dedicated rental use.
    mixed_use_status: str = Field(default="UNCLASSIFIED")
    currency: str = Field(default="USD")
    is_active: bool = Field(default=True)
    created_at: datetime = Field(default_factory=utc_now)


class LoanAccount(SQLModel, table=True):
    __tablename__ = "loan_accounts"

    id: str = Field(primary_key=True)  # e.g. "loan_pnc_1000000002"
    financial_account_id: str = Field(foreign_key="financial_accounts.id", unique=True)
    property_id: str = Field(foreign_key="properties.id")
    loan_number: str = Field(unique=True)
    origination_date: Optional[date] = None
    original_principal_cents: Optional[int] = None
    term_months: int = Field(default=360)
    # FIXED or ARM. The rate itself is stored in current_interest_rate_bps; this
    # says whether the owner should expect it to change.
    rate_type: str = Field(default="FIXED")
    # Legacy field name; stored in thousandths of a percent (2500 = 2.500%).
    current_interest_rate_bps: Optional[int] = None
    is_active: bool = Field(default=True)
    # The loan's ledger accounts, allocated as a set on creation
    # (see domain.accounting.coa). Principal carries the debt balance, clearing
    # carries funds the bank has settled but the servicer has not yet applied, and
    # escrow carries the servicer-held reserve for taxes and insurance.
    principal_ledger_code: Optional[int] = Field(default=None, foreign_key="ledger_accounts.code")
    clearing_ledger_code: Optional[int] = Field(default=None, foreign_key="ledger_accounts.code")
    escrow_ledger_code: Optional[int] = Field(default=None, foreign_key="ledger_accounts.code")
    created_at: datetime = Field(default_factory=utc_now)


class EscrowAccount(SQLModel, table=True):
    __tablename__ = "escrow_accounts"

    id: str = Field(primary_key=True)
    loan_account_id: str = Field(foreign_key="loan_accounts.id", unique=True)
    opening_balance_cents: int = Field(default=0)
    last_analysis_date: Optional[date] = None
    projected_annual_tax_cents: int = Field(default=0)
    projected_annual_insurance_cents: int = Field(default=0)
    projected_shortage_cents: int = Field(default=0)
    monthly_shortage_payment_cents: int = Field(default=0)
    updated_at: datetime = Field(default_factory=utc_now)


class EscrowMovement(SQLModel, table=True):
    __tablename__ = "escrow_movements"
    __table_args__ = (
        CheckConstraint(
            "movement_type IN ('CONTRIBUTION', 'TAX_DISBURSEMENT', "
            "'INSURANCE_DISBURSEMENT', 'REFUND', 'INTEREST_EARNED', "
            "'SHORTAGE_ADJUSTMENT')",
            name="check_escrow_movement_type",
        ),
    )

    id: str = Field(primary_key=True)
    escrow_account_id: str = Field(foreign_key="escrow_accounts.id")
    movement_date: date
    movement_type: str  # 'CONTRIBUTION', 'TAX_DISBURSEMENT', 'INSURANCE_DISBURSEMENT', 'REFUND', 'INTEREST_EARNED', 'SHORTAGE_ADJUSTMENT'
    amount_cents: int  # Positive for additions, negative for disbursements
    running_calculated_balance_cents: int
    statement_id: Optional[str] = Field(default=None, foreign_key="statements.id")
    description: Optional[str] = None
    provenance_notes: Optional[str] = None
    created_at: datetime = Field(default_factory=utc_now)


# --- 3. Documents, Statements & Extraction Versions ---

#: Document types that represent legal contracts, deeds, policies or tax bills,
#: which are filed in the vault and property records rather than reconciled as
#: financial account statements.
NON_STATEMENT_DOCUMENT_TYPES: set[str] = {
    "LEASE_AGREEMENT",
    "PROPERTY_DEED",
    "INSURANCE_POLICY",
    "PROPERTY_TAX",
}

#: Document types that represent financial ledger statements requiring balance
#: or payment reconciliation.
STATEMENT_DOCUMENT_TYPES: set[str] = {
    "MORTGAGE_STATEMENT",
    "CHECKING_STATEMENT",
    "CREDIT_CARD_STATEMENT",
    "HOA_STATEMENT",
}


class Document(SQLModel, table=True):
    __tablename__ = "documents"

    id: str = Field(primary_key=True)
    file_sha256: str = Field(unique=True, index=True)
    original_filename: str
    storage_path: str
    file_size_bytes: int
    mime_type: str = Field(default="application/pdf")
    document_type: Optional[str] = Field(default=None)  # 'LEASE_AGREEMENT', 'MORTGAGE_STATEMENT', 'CHECKING_STATEMENT', 'CREDIT_CARD_STATEMENT', 'INSURANCE_POLICY', 'PROPERTY_TAX', 'HOA_STATEMENT', 'OTHER'
    property_id: Optional[str] = Field(default=None, foreign_key="properties.id")
    uploaded_at: datetime = Field(default_factory=utc_now)


class Statement(SQLModel, table=True):
    __tablename__ = "statements"

    id: str = Field(primary_key=True)
    document_id: str = Field(foreign_key="documents.id")
    financial_account_id: str = Field(foreign_key="financial_accounts.id")
    statement_type: str  # 'MORTGAGE', 'CHECKING', 'CREDIT_CARD', 'ESCROW_ANALYSIS', 'FORM_1098'
    active_parser_name: str
    active_parser_version: str
    period_start_date: Optional[date] = None
    period_end_date: Optional[date] = None
    statement_date: date
    opening_balance_cents: Optional[int] = None
    closing_balance_cents: Optional[int] = None
    ingestion_status: str = Field(default="COMMITTED")  # COMMITTED only after explicit approval and import.
    created_at: datetime = Field(default_factory=utc_now)


class ExtractionVersion(SQLModel, table=True):
    __tablename__ = "extraction_versions"

    id: str = Field(primary_key=True)
    statement_id: str = Field(foreign_key="statements.id")
    parser_name: str
    parser_version: str
    extracted_at: datetime = Field(default_factory=utc_now)
    extracted_data_json: str  # JSON-encoded extracted dictionary
    is_active: bool = Field(default=True)


class DocumentExtraction(SQLModel, table=True):
    __tablename__ = "document_extractions"
    __table_args__ = (
        CheckConstraint(
            "validation_status IN ('PROCESSING', 'EXTRACTED', 'VALIDATED', 'NEEDS_REVIEW')",
            name="check_document_extraction_validation_status_v2",
        ),
        CheckConstraint(
            "approval_status IN ('PENDING', 'APPROVED')",
            name="check_document_extraction_approval_status",
        ),
    )

    id: str = Field(primary_key=True)
    document_id: str = Field(foreign_key="documents.id", index=True)
    parser_name: str
    parser_version: str
    validation_status: str = Field(default="EXTRACTED")
    approval_status: str = Field(default="PENDING")
    extracted_data_json: str
    approved_at: datetime | None = None
    imported_statement_id: str | None = Field(default=None, foreign_key="statements.id")
    created_at: datetime = Field(default_factory=utc_now)


class MortgagePeriod(SQLModel, table=True):
    __tablename__ = "mortgage_periods"

    id: str = Field(primary_key=True)
    statement_id: str = Field(foreign_key="statements.id")
    loan_account_id: str = Field(foreign_key="loan_accounts.id")
    payment_due_date: date
    statement_date: date
    # Billed amounts (servicer statement due)
    principal_billed_cents: int
    interest_billed_cents: int
    escrow_billed_cents: int
    fees_billed_cents: int = Field(default=0)
    total_amount_billed_cents: int
    # Confirmed applied amounts (upon settlement)
    principal_applied_cents: int = Field(default=0)
    interest_applied_cents: int = Field(default=0)
    escrow_applied_cents: int = Field(default=0)
    total_amount_applied_cents: int = Field(default=0)
    outstanding_principal_cents: int
    reported_escrow_balance_cents: Optional[int] = None
    # Legacy field name; stored in thousandths of a percent (2500 = 2.500%).
    interest_rate_bps: int
    status: str = Field(default="BILLED")  # 'BILLED', 'PARTIALLY_SETTLED', 'FULLY_SETTLED'
    created_at: datetime = Field(default_factory=utc_now)


# --- 4. Transactions & Splits ---

class Transaction(SQLModel, table=True):
    __tablename__ = "transactions"
    __table_args__ = (
        CheckConstraint("direction IN ('INFLOW', 'OUTFLOW')", name="check_transaction_direction"),
        Index("ix_transactions_account_date", "financial_account_id", "transaction_date"),
    )

    id: str = Field(primary_key=True)
    statement_id: str = Field(foreign_key="statements.id")
    financial_account_id: str = Field(foreign_key="financial_accounts.id")
    transaction_date: date
    posting_date: Optional[date] = None
    amount_cents: int  # Signed: positive = credit/deposit, negative = debit/withdrawal
    direction: str  # 'INFLOW' | 'OUTFLOW'
    raw_description: str
    reference_number: Optional[str] = None
    # The other account in a transfer, e.g. the credit card a bank payment
    # settles. Account identity (deposit account paying a card) is what marks a
    # settlement; the description is never used to infer one.
    counter_account_id: Optional[str] = Field(default=None, foreign_key="financial_accounts.id")
    # Durable governance inputs. Description text is a hint, never the sole basis
    # for requiring authorization: these flags are set when the transaction is
    # created and survive any later wording change.
    requires_client_confirmation: bool = Field(default=False)
    requires_cpa_review: bool = Field(default=False)
    created_at: datetime = Field(default_factory=utc_now)


class TransactionSplit(SQLModel, table=True):
    __tablename__ = "transaction_splits"
    __table_args__ = (
        CheckConstraint("amount_cents > 0", name="check_split_amount_positive"),
        CheckConstraint(
            "category IN ('RENT_INCOME', 'RENT', 'LATE_FEE', 'FEE', 'FEE_INCOME', "
            "'TAXES', 'INSURANCE', 'REPAIRS', 'SUPPLIES', 'SNOW_LANDSCAPING', "
            "'UTILITIES', 'BANK_FEES', 'MANAGEMENT', 'COMMISSIONS', 'HOA', "
            "'LEGAL', 'PERSONAL', 'MORTGAGE', 'SECURITY_DEPOSIT')",
            name="check_split_category",
        ),
        UniqueConstraint(
            "transaction_id", "property_id", "category",
            name="uq_split_transaction_property_category",
        ),
    )

    id: str = Field(primary_key=True)
    transaction_id: str = Field(foreign_key="transactions.id")
    property_id: Optional[str] = Field(default=None, foreign_key="properties.id")
    category: str  # 'RENT_INCOME', 'REPAIRS', 'SUPPLIES', 'TAXES', 'PERSONAL'
    amount_cents: int  # Strictly positive integer
    schedule_e_line: Optional[int] = None
    tax_review_status: str = Field(default="UNCLASSIFIED")  # 'UNCLASSIFIED', 'AUTO_SUGGESTED', 'CLIENT_CONFIRMED', 'CAPEX_REVIEW_REQUIRED', 'CPA_APPROVED'
    notes: Optional[str] = None
    created_at: datetime = Field(default_factory=utc_now)


class TransactionProcessingState(SQLModel, table=True):
    __tablename__ = "transaction_processing_states"
    __table_args__ = (
        CheckConstraint(
            "allocation_status IN ('UNALLOCATED', 'PARTIALLY_ALLOCATED', 'FULLY_ALLOCATED')",
            name="check_processing_allocation_status",
        ),
        CheckConstraint(
            "posting_status IN ('UNPOSTED', 'POSTED', 'REVERSED')",
            name="check_processing_posting_status",
        ),
    )

    transaction_id: str = Field(primary_key=True, foreign_key="transactions.id")
    allocation_status: str = Field(default="UNALLOCATED")  # 'UNALLOCATED', 'PARTIALLY_ALLOCATED', 'FULLY_ALLOCATED'
    allocated_cents: int = Field(default=0)
    residual_cents: int  # abs(transaction.amount_cents) - allocated_cents
    posting_status: str = Field(default="UNPOSTED")  # 'UNPOSTED', 'POSTED', 'REVERSED'
    journal_entry_id: Optional[str] = Field(default=None, foreign_key="journal_entries.id")
    is_reconciled: bool = Field(default=False)
    updated_at: datetime = Field(default_factory=utc_now)


class VendorRule(SQLModel, table=True):
    __tablename__ = "vendor_rules"

    id: str = Field(primary_key=True)  # e.g. "vrule_apple", "vrule_hertz"
    pattern: str = Field(index=True)  # Substring/pattern matched against transaction raw_description
    vendor_name: str  # Display name: "Apple Services", "Hertz Rent-A-Car"
    service_type: str  # Plain English description, e.g. "Digital Subscriptions & Cloud Storage"
    default_category: str = Field(default="PERSONAL")  # 'PERSONAL', 'REPAIRS', 'SUPPLIES', 'UTILITIES', etc.
    default_property_id: Optional[str] = Field(default=None)
    confidence: float = Field(default=0.90)
    requires_client_confirmation: bool = Field(default=False)
    requires_cpa_review: bool = Field(default=False)
    is_active: bool = Field(default=True)
    notes: Optional[str] = None
    created_at: datetime = Field(default_factory=utc_now)


# --- 5. Double-Entry Accounting Domain ---

class LedgerAccount(SQLModel, table=True):
    __tablename__ = "ledger_accounts"

    code: int = Field(primary_key=True)  # e.g. 1010, 2120, 5010
    name: str
    account_type: str  # 'ASSET', 'LIABILITY', 'EQUITY', 'INCOME', 'EXPENSE'
    normal_balance: str  # 'DEBIT' | 'CREDIT'


class JournalEntry(SQLModel, table=True):
    __tablename__ = "journal_entries"
    __table_args__ = (
        CheckConstraint(
            "status IN ('DRAFT', 'BALANCED', 'POSTED')",
            name="check_journal_entry_status",
        ),
    )

    id: str = Field(primary_key=True)
    entry_date: date
    entry_type: str  # 'OPENING_BALANCE', 'MORTGAGE_BILLING', 'CASH_SETTLEMENT', 'EXPENSE_POST', 'ESCROW_DISBURSEMENT', 'REVERSAL'
    status: str = Field(default="DRAFT")  # 'DRAFT', 'BALANCED', 'POSTED'
    idempotency_key: Optional[str] = Field(default=None, unique=True, index=True)
    reversal_of_id: Optional[str] = Field(default=None, foreign_key="journal_entries.id")
    # Explicit provenance: the transaction this entry was posted from, when there
    # is one. Metrics attribute cash-flow and settlement allocations through this
    # reference rather than inferring them from optional property tags on lines.
    source_transaction_id: Optional[str] = Field(
        default=None, foreign_key="transactions.id", index=True
    )
    description: str
    posted_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=utc_now)


class JournalEntryLine(SQLModel, table=True):
    __tablename__ = "journal_entry_lines"
    __table_args__ = (
        CheckConstraint(
            "(debit_cents > 0 AND credit_cents = 0) OR (credit_cents > 0 AND debit_cents = 0)",
            name="check_journal_line_one_sided",
        ),
        CheckConstraint(
            "debit_cents >= 0 AND credit_cents >= 0",
            name="check_journal_line_non_negative",
        ),
    )

    id: str = Field(primary_key=True)
    journal_entry_id: str = Field(foreign_key="journal_entries.id", index=True)
    ledger_account_code: int = Field(foreign_key="ledger_accounts.code", index=True)
    property_id: Optional[str] = Field(default=None, foreign_key="properties.id")
    debit_cents: int = Field(default=0)
    credit_cents: int = Field(default=0)
    description: Optional[str] = None


class PostingAuthorization(SQLModel, table=True):
    """Immutable record of the authorizations that permitted a posting.

    The two gates are stored independently, each with its own timestamp, so the
    evidence survives posting and reclassification even when only one of them was
    required. Collapsing them into a single "status" would lose the fact that a
    user attested external review.

    ``*_at`` records when the authorization was accepted with the posting; the
    application does not capture consent earlier than the posting request.

    ``authorized_by_*`` attributes the CBD-2 attestation to the identity that
    supplied it and the capacity it acted in. ``verified_cpa_reviewer`` is True
    only when that identity was a member holding the ``CPA_REVIEWER`` role, so an
    owner self-attestation stays recorded as a self-attestation and is never
    promoted to a verified CPA authorization.
    """

    __tablename__ = "posting_authorizations"

    id: str = Field(primary_key=True)
    transaction_id: str = Field(foreign_key="transactions.id", index=True)
    journal_entry_id: str = Field(foreign_key="journal_entries.id", index=True)
    # What the policy demanded for this posting.
    required_client_confirmation: bool = Field(default=False)
    required_cpa_review: bool = Field(default=False)
    # What was supplied, one field per gate.
    client_confirmed: bool = Field(default=False)
    client_confirmed_at: datetime | None = Field(default=None)
    user_attested_cpa_review: bool = Field(default=False)
    user_attested_cpa_review_at: datetime | None = Field(default=None)
    # Who asserted the external review, and in what capacity. Set only when a
    # review attestation was supplied; the identity is the user that supplied it.
    authorized_by_user_id: Optional[str] = Field(default=None)
    authorized_by_role: Optional[str] = Field(default=None)
    verified_cpa_reviewer: bool = Field(default=False)
    # The allocation the authorization applied to, so a later reclassification
    # does not retroactively alter what was authorised.
    split_count: int = Field(default=0)
    split_total_cents: int = Field(default=0)
    policy_reasons: str | None = Field(default=None)
    created_at: datetime = Field(default_factory=utc_now)


# --- 6. Reconciliation & Tax Domain ---

class ReconciliationSession(SQLModel, table=True):
    __tablename__ = "reconciliation_sessions"

    id: str = Field(primary_key=True)
    session_date: datetime = Field(default_factory=utc_now)
    status: str = Field(default="OPEN")  # 'OPEN', 'COMPLETED'
    created_by: str = Field(default="system")


class ReconciliationAllocation(SQLModel, table=True):
    __tablename__ = "reconciliation_allocations"
    __table_args__ = (
        CheckConstraint("allocated_cents > 0", name="check_reconciliation_allocation_positive"),
        CheckConstraint(
            "status IN ('PROPOSED', 'CONFIRMED', 'REVERSED', 'REJECTED')",
            name="check_reconciliation_allocation_status",
        ),
    )

    id: str = Field(primary_key=True)
    session_id: str = Field(foreign_key="reconciliation_sessions.id")
    source_transaction_id: str = Field(foreign_key="transactions.id")
    target_type: str  # 'MORTGAGE_PERIOD', 'CREDIT_CARD_PAYMENT', 'ADDITIONAL_PRINCIPAL', 'EXPENSE_SPLIT'
    target_mortgage_period_id: Optional[str] = Field(default=None, foreign_key="mortgage_periods.id")
    target_transaction_id: Optional[str] = Field(default=None, foreign_key="transactions.id")
    allocated_cents: int
    status: str = Field(default="PROPOSED")  # 'PROPOSED', 'CONFIRMED', 'REVERSED', 'REJECTED'
    matched_by: str = Field(default="DETERMINISTIC_RULE")
    variance_cents: int = Field(default=0)
    notes: Optional[str] = None
    created_at: datetime = Field(default_factory=utc_now)


class TaxMapping(SQLModel, table=True):
    __tablename__ = "tax_mappings"

    id: str = Field(primary_key=True)
    tax_year: int
    category_code: str
    schedule_e_line: int
    description: str
    requires_cpa_review: bool = Field(default=False)
    created_at: datetime = Field(default_factory=utc_now)


class AuditLog(SQLModel, table=True):
    __tablename__ = "audit_log"

    id: str = Field(primary_key=True)
    entity_name: str
    entity_id: str
    action: str  # 'INSERT', 'UPDATE', 'SPLIT', 'RECONCILE', 'REVERSE', 'POST'
    changed_by: str = Field(default="system")
    old_values_json: Optional[str] = None
    new_values_json: Optional[str] = None
    reason: Optional[str] = None
    created_at: datetime = Field(default_factory=utc_now)
