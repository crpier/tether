"""Web Push subscription lifecycle with browser cache invalidation."""

from snekql.sqlite import Fetched

from tether.events import EventPublisher, InvalidateEvent, NullEventPublisher
from tether.push_model import PushStatus
from tether.push_store import PushStore, PushSubscription


class PushService:
    """Coordinate canonical Push subscription state and invalidation.

    >>> service = PushService(store=store)
    >>> _ = await service.subscribe("https://push/abc", p256dh="k", auth="a")
    >>> (await service.status()).count
    1
    """

    def __init__(
        self,
        store: PushStore,
        event_publisher: EventPublisher | None = None,
    ) -> None:
        self.event_publisher: EventPublisher = event_publisher or NullEventPublisher()
        self.store: PushStore = store

    async def subscribe(
        self,
        endpoint: str,
        *,
        p256dh: str,
        auth: str,
    ) -> PushSubscription[Fetched]:
        """Insert, refresh, or revive one endpoint and publish invalidation."""
        subscription = await self.store.subscribe(endpoint, p256dh=p256dh, auth=auth)
        await self.event_publisher.publish(InvalidateEvent(keys=["push"]))
        return subscription

    async def unsubscribe(self, endpoint: str) -> None:
        """Convergently remove an endpoint and invalidate when state changed."""
        if await self.store.unsubscribe(endpoint):
            await self.event_publisher.publish(InvalidateEvent(keys=["push"]))

    async def status(self, endpoint: str | None = None) -> PushStatus:
        """Report live subscription count and endpoint membership."""
        return await self.store.status(endpoint)

    async def active_subscriptions(self) -> list[PushSubscription[Fetched]]:
        """List every live subscription for delivery."""
        return await self.store.active_subscriptions()
