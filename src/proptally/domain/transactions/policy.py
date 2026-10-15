"""CBD-1 / CBD-2 posting authorization policy.

Single source of truth for whether posting a transaction requires explicit client
confirmation or external review. It is evaluated by the domain posting service so
that every entry point — the transaction register, the reconciliation queue and
reclassification — is governed identically and no route can manufacture an
authorization or bypass another route's rules.

Policy inputs, in order of authority:

1. Durable flags on the transaction record (``requires_client_confirmation`` /
   ``requires_cpa_review``). These are set when a transaction is created and do
   not change if its wording is later edited.
2. Account identity. A rental deduction funded by a credit card is only a
   mixed-use question if that card is *designated* mixed-use
   (``FinancialAccount.mixed_use_status``). A dedicated rental card is not gated.
   An unclassified card is never assumed to be dedicated rental use.
3. Description patterns, used only as a fallback for records that predate the
   explicit flags.

Authorization vocabulary
------------------------
``user_attested_cpa_review`` is a **self-attestation**: the user asserts that the
required external review has happened. It is not proof of CPA identity or
authorization, and nothing here treats it as such. Real CPA authorization would
require authenticated identity, a CPA role, an explicit approval record with a
timestamp and the approved item/version, and immutable audit history — none of
which exists yet.

Split ``tax_review_status`` records classification review state; it is never
treated as an authorization grant, so a caller cannot unlock posting by writing a
status onto a split.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from sqlmodel import Session

from proptally.core.exceptions import GovernanceAuthorizationError
from proptally.db.models import FinancialAccount, Property, Transaction

# Categories that deduct against rental income.
RENTAL_DEDUCTION_CATEGORIES = frozenset({
    "REPAIRS",
    "SUPPLIES",
    "TAXES",
    "INSURANCE",
    "SNOW_LANDSCAPING",
    "MANAGEMENT",
    "LEGAL",
    "MORTGAGE",
})

# Categories that represent rental income received.
RENTAL_INCOME_CATEGORIES = frozenset({"RENT_INCOME", "RENT", "LATE_FEE", "FEE"})

CHECKING_ACCOUNT_TYPE = "CHECKING"
CREDIT_CARD_ACCOUNT_TYPE = "CREDIT_CARD"
DEPOSIT_ACCOUNT_TYPES = frozenset({"CHECKING", "SAVINGS"})

# Property.property_type values.
RENTAL_INVESTMENT = "RENTAL_INVESTMENT"
PRIMARY_RESIDENCE = "PRIMARY_RESIDENCE"

# FinancialAccount.mixed_use_status values.
DEDICATED_RENTAL = "DEDICATED_RENTAL"
MIXED_USE = "MIXED_USE"
UNCLASSIFIED = "UNCLASSIFIED"

# Split classification status implied by the authorization supplied. The status
# is a summary; the two authorizations are recorded independently and immutably
# in posting_authorizations.
CLIENT_CONFIRMED_STATUS = "CLIENT_CONFIRMED"
USER_ATTESTED_CPA_REVIEW_STATUS = "USER_ATTESTED_CPA_REVIEW"
CLIENT_CONFIRMED_AND_USER_ATTESTED_STATUS = (
    "CLIENT_CONFIRMED+USER_ATTESTED_CPA_REVIEW"
)
SELF_CLASSIFIED_STATUS = "SELF_CLASSIFIED"

# Fallback hint for legacy records created before the explicit flags existed.
_CBD1_DESCRIPTION_HINTS = ("CBD-1", "ATM CASH DEPOSIT", "ATM DEPOSIT")


@dataclass(frozen=True)
class PostingPolicy:
    """The authorization a transaction needs before it may be posted."""

    requires_client_confirmation: bool = False
    requires_cpa_review: bool = False
    # UI hint: the card is not dedicated rental use, so once the allocation is
    # entered it may need review. The exact requirement still depends on the
    # allocation, so this never substitutes for enforcement.
    may_require_review: bool = False
    reasons: tuple[str, ...] = ()

    @property
    def requires_authorization(self) -> bool:
        return self.requires_client_confirmation or self.requires_cpa_review

    @property
    def should_offer_review(self) -> bool:
        """Whether an interface should show the review attestation control."""
        return self.requires_cpa_review or self.may_require_review


def is_card_settlement(
    session: Session,
    txn: Transaction,
    source_account: FinancialAccount | None = None,
    counter_account: FinancialAccount | None = None,
) -> bool:
    """True when a deposit-account payment settles a credit-card account.

    Determined from account identity only — the direction of the money and the
    type of the two accounts. The description plays no part, so wording can never
    turn an ordinary expense into a transfer or vice versa.
    """
    if txn.direction != "OUTFLOW" or not txn.counter_account_id:
        return False
    if source_account is None:
        source_account = session.get(FinancialAccount, txn.financial_account_id)
    if counter_account is None:
        counter_account = session.get(FinancialAccount, txn.counter_account_id)
    if source_account is None or counter_account is None:
        return False
    return (
        source_account.account_type in DEPOSIT_ACCOUNT_TYPES
        and counter_account.account_type == CREDIT_CARD_ACCOUNT_TYPE
    )


def evaluate_posting_policy(
    session: Session,
    txn: Transaction,
    splits: Sequence = (),
    account: FinancialAccount | None = None,
    counter_account: FinancialAccount | None = None,
) -> PostingPolicy:
    """Derives the posting authorization policy for a transaction."""
    if account is None and txn.financial_account_id:
        account = session.get(FinancialAccount, txn.financial_account_id)
    if counter_account is None and txn.counter_account_id:
        counter_account = session.get(FinancialAccount, txn.counter_account_id)

    categories = {str(getattr(s, "category", "")).upper() for s in splits}
    reasons = []

    requires_client_confirmation = bool(txn.requires_client_confirmation)
    requires_cpa_review = bool(txn.requires_cpa_review)
    if requires_client_confirmation:
        reasons.append("transaction is flagged as requiring client confirmation")
    if requires_cpa_review:
        reasons.append("transaction is flagged as requiring external review")

    # Which card bears the rental deduction, if any. For a card purchase that is
    # the funding card; for a card settlement it is the card being paid.
    is_settlement = is_card_settlement(session, txn, account, counter_account)
    if is_settlement:
        gating_card = counter_account
    elif account is not None and account.account_type == CREDIT_CARD_ACCOUNT_TYPE:
        gating_card = account
    else:
        gating_card = None

    may_require_review = False
    if gating_card is not None:
        status = gating_card.mixed_use_status
        if status == DEDICATED_RENTAL:
            # A purpose-limited rental card needs no extra review.
            pass
        elif is_settlement:
            # The allocation of a card payment *is* the mixed-use determination,
            # and it is decided at posting time. A settlement of a shared or
            # unclassified card therefore always needs its allocation reviewed.
            requires_cpa_review = True
            reasons.append(_card_reason(gating_card, status, settled=True))
        else:
            # A rental deduction requires an expense category AND an allocation to
            # a rental door: a repair on the primary residence is not a Schedule E
            # rental deduction, and neither is unattributed spend.
            has_deduction_category = bool(categories & RENTAL_DEDUCTION_CATEGORIES)
            if _has_rental_deduction(session, splits):
                requires_cpa_review = True
                reasons.append(_card_reason(gating_card, status, settled=False))
            elif has_deduction_category or not categories:
                # Either a deduction category that is not (yet) attributed to a
                # rental door, or an allocation the user has not entered yet. Not
                # a gate, but the interface should offer the attestation.
                may_require_review = True
                reasons.append(_card_reason(gating_card, status, settled=False, pending=True))

    # Supplementary fallback for records that predate the explicit flags.
    description = (txn.raw_description or "").upper()
    if any(hint in description for hint in _CBD1_DESCRIPTION_HINTS):
        if not requires_client_confirmation:
            reasons.append("description matched a CBD-1 cash-deposit pattern")
        requires_client_confirmation = True

    return PostingPolicy(
        requires_client_confirmation=requires_client_confirmation,
        requires_cpa_review=requires_cpa_review,
        may_require_review=may_require_review,
        reasons=tuple(reasons),
    )


def _has_rental_deduction(session: Session, splits: Sequence) -> bool:
    """True when an allocation is an expense against a rental door.

    CBD-2 is an external-review gate over *rental* deductions, so the category and
    the property's classification must both hold. Lookups are cached per call
    because the property list is small and stable.
    """
    cache: dict = {}
    for split in splits:
        category = str(getattr(split, "category", "")).upper()
        if category not in RENTAL_DEDUCTION_CATEGORIES:
            continue
        property_id = getattr(split, "property_id", None)
        if property_id is None:
            continue
        if property_id not in cache:
            prop = session.get(Property, property_id)
            cache[property_id] = prop is not None and prop.property_type == RENTAL_INVESTMENT
        if cache[property_id]:
            return True
    return False


def _card_reason(card, status: str, settled: bool, pending: bool = False) -> str:
    """A reason string naming the card and why it is (or may be) reviewable."""
    if status == MIXED_USE:
        basis = f"card '{card.account_name}' is designated mixed-use"
    else:
        basis = f"card '{card.account_name}' is not classified as dedicated rental use"
    if settled:
        return f"{basis}: the allocation of this card payment needs review before posting"
    if pending:
        return f"{basis}: review will be required if any of this is allocated to a rental door"
    return f"{basis}: rental deductions need external review"


def assert_posting_authorized(
    policy: PostingPolicy,
    client_confirmed: bool = False,
    user_attested_cpa_review: bool = False,
) -> None:
    """Raises unless the supplied authorizations satisfy the policy.

    The two gates are deliberately **not** interchangeable:

    * CBD-1 is a client-authorization gate. Client confirmation satisfies it.
    * CBD-2 is an external-review gate over a deduction that will be claimed on
      Schedule E. The party that benefits from the deduction must not be able to
      authorize it alone, so client confirmation does **not** satisfy it; a user
      attestation that external review occurred is required.

    Authorization is only ever what the caller passes in from the request. It is
    never inferred from split state or from the transaction itself, and a
    self-attestation is never upgraded to a verified approval.
    """
    if policy.requires_client_confirmation and not client_confirmed:
        raise GovernanceAuthorizationError(
            "CBD-1 Policy Violation: this transaction requires explicit client "
            "confirmation before it may be posted."
        )
    if policy.requires_cpa_review and not user_attested_cpa_review:
        raise GovernanceAuthorizationError(
            "CBD-2 Policy Violation: this mixed-use or unclassified credit card "
            "charge requires a user attestation that external review occurred "
            "before posting. Client confirmation does not satisfy this gate."
        )


def classification_status_for(client_confirmed: bool, user_attested_cpa_review: bool) -> str:
    """The split review status implied by the authorization supplied.

    This is a *summary* for display and for the "classified or not" check. When
    both authorizations were given it records both, so the fact that the user
    attested external review is not lost behind the client confirmation. The
    authoritative, independently-timestamped record lives in
    ``posting_authorizations``.
    """
    if client_confirmed and user_attested_cpa_review:
        return CLIENT_CONFIRMED_AND_USER_ATTESTED_STATUS
    if client_confirmed:
        return CLIENT_CONFIRMED_STATUS
    if user_attested_cpa_review:
        return USER_ATTESTED_CPA_REVIEW_STATUS
    return SELF_CLASSIFIED_STATUS
