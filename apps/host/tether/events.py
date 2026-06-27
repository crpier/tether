"""In-process event hub for browser invalidation frames."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class InvalidateEvent:
    """A cache invalidation signal emitted by mutating services."""

    keys: list[str]


class EventPublisher(Protocol):
    """Minimal publisher protocol accepted by service layers."""

    async def publish(self, event: InvalidateEvent) -> None:
        """Publish one event."""
        ...


class NullEventPublisher:
    """No-op publisher used when a service is tested without an event hub."""

    async def publish(self, event: InvalidateEvent) -> None:
        """Drop the event."""
        _ = event


class EventHub:
    """Asyncio pub/sub hub for in-process browser events."""

    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[InvalidateEvent]] = set()

    async def publish(self, event: InvalidateEvent) -> None:
        """Fan an event out to every current subscriber."""
        for subscriber in set(self._subscribers):
            await subscriber.put(event)

    def subscribe(self) -> asyncio.Queue[InvalidateEvent]:
        """Create a subscription queue owned by one WebSocket connection."""
        queue: asyncio.Queue[InvalidateEvent] = asyncio.Queue()
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[InvalidateEvent]) -> None:
        """Remove a subscription queue."""
        self._subscribers.discard(queue)
