"""Stable error identities for the Synthetic panel domain."""


class PanelNotFoundError(Exception):
    """An operation targeted a panel that does not exist."""


class PanelConflictError(Exception):
    """A panel moved on from the version on which a caller acted."""


class InvalidPanelSpecError(Exception):
    """A panel's saved query or render choice is malformed."""
