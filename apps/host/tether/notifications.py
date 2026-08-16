"""Durable notification lifecycle and cache invalidation."""

from uuid import UUID

from snekql.sqlite import Fetched

from tether.events import EventPublisher, InvalidateEvent, NullEventPublisher
from tether.notification_model import (
    DEFAULT_NOTIFICATION_LIST_LIMIT,
    NotificationDraft,
)
from tether.notification_store import Notification, NotificationStore


class NotificationService:
    """Coordinate notification persistence with browser invalidation.

    >>> service = NotificationService(store=store)
    >>> _ = await service.record(NotificationDraft(body="stand up"))
    >>> len(await service.list_recent())
    1
    """

    def __init__(
        self,
        store: NotificationStore,
        event_publisher: EventPublisher | None = None,
    ) -> None:
        self.event_publisher: EventPublisher = event_publisher or NullEventPublisher()
        self.store: NotificationStore = store

    async def record(self, draft: NotificationDraft) -> Notification[Fetched]:
        """Persist a delivered notification and invalidate browser listings."""
        notification = await self.store.record(draft)
        await self.event_publisher.publish(InvalidateEvent(keys=["notifications"]))
        return notification

    async def list_recent(
        self, *, limit: int = DEFAULT_NOTIFICATION_LIST_LIMIT
    ) -> list[Notification[Fetched]]:
        """Return undismissed notifications newest-first."""
        return await self.store.list_recent(limit=limit)

    async def dismiss(self, notification_id: UUID) -> None:
        """Convergently dismiss one notification."""
        if await self.store.dismiss(notification_id):
            await self.event_publisher.publish(InvalidateEvent(keys=["notifications"]))

    async def clear(self) -> int:
        """Dismiss every live notification and return the affected count."""
        matched = await self.store.clear()
        if matched:
            await self.event_publisher.publish(InvalidateEvent(keys=["notifications"]))
        return matched
