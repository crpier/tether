"""Domain values for durable user-facing notifications."""

from dataclasses import dataclass

DEFAULT_NOTIFICATION_LIST_LIMIT = 50
"""Number of recent notifications loaded by default."""


@dataclass(frozen=True, slots=True)
class NotificationDraft:
    """Resolved notification content ready for durable recording."""

    body: str
    trigger_id: str | None = None
    action_kind: str | None = None
    source_label: str | None = None
