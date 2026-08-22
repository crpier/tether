"""Domain failures for Product observations."""


class ProductObservationError(Exception):
    """Base failure for Product-observation operations."""


class InvalidProductObservationError(ProductObservationError):
    """Raised when requested feedback has no usable text."""


class ProductObservationConflictError(ProductObservationError):
    """Raised when a mutation uses a stale observed version."""


class ProductObservationNotFoundError(ProductObservationError):
    """Raised when an operation targets an absent Product observation."""
