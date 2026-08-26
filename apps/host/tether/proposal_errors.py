"""Proposal domain error identities."""


class ProposalNotFoundError(Exception):
    """Raised when an operation targets a proposal that does not exist."""


class ProposalConflictError(Exception):
    """Raised when a stale observed version cannot accept a mutation."""


class ProposalStateError(Exception):
    """Raised when a Proposal lifecycle transition is invalid."""


class InvalidActionError(Exception):
    """Raised when an action kind or its parameters are invalid."""
