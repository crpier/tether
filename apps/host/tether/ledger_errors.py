"""Domain failures raised by generic Ledger capabilities."""


class InvalidLedgerError(Exception):
    """A Ledger request violates a definition or authority invariant."""


class LedgerFieldValueError(InvalidLedgerError):
    """One entry value does not match its approved field interpretation."""

    def __init__(self, field_id: str, requirement: str) -> None:
        super().__init__(f"Ledger field {field_id} {requirement}")


class LedgerConflictError(Exception):
    """A Ledger changed since the caller observed it."""


class LedgerNotFoundError(Exception):
    """A requested Ledger identity does not exist."""


__all__ = [
    "InvalidLedgerError",
    "LedgerConflictError",
    "LedgerFieldValueError",
    "LedgerNotFoundError",
]
