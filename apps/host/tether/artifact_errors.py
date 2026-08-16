"""Stable error identities for the Artifact domain."""


class ArtifactNotFoundError(Exception):
    """An operation targeted an absent artifact identity or version."""


class ArtifactHtmlTooLargeError(Exception):
    """Artifact HTML exceeded the host-side size cap."""
