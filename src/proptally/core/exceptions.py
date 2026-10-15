"""Custom exceptions for PropTally financial and database operations."""

class PropTallyError(Exception):
    """Base exception for all PropTally domain errors."""
    pass


class UnbalancedJournalEntryError(PropTallyError):
    """Raised when total debits do not equal total credits in a journal entry."""
    pass


class EmptyJournalEntryError(PropTallyError):
    """Raised when attempting to post an entry with no lines."""
    pass


class ImmutableLedgerError(PropTallyError):
    """Raised when attempting to modify or delete a posted journal entry or its lines."""
    pass


class DuplicatePostingError(PropTallyError):
    """Raised when an idempotency key collision occurs during journal posting."""
    pass


class InvalidSplitSumError(PropTallyError):
    """Raised when transaction splits do not sum to the parent transaction amount."""
    pass


class InvalidAllocationError(PropTallyError):
    """Raised when reconciliation allocations exceed available source or target capacity."""
    pass


class AccountNotFoundError(PropTallyError):
    """Raised when a referenced ledger account code does not exist."""
    pass


class TransactionAlreadyReversedError(PropTallyError):
    """Raised when attempting to reverse an already reversed journal entry."""
    pass


class GovernanceAuthorizationError(PermissionError, PropTallyError):
    """Raised when posting is attempted without the authorization a policy requires.

    Derives from ``PermissionError`` so existing callers that catch it keep working,
    while giving routes a precise type to translate into an HTTP 403.
    """
    pass
