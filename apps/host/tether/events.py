"""In-process event hub for browser invalidation and notification frames."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class InvalidateEvent:
    """A cache invalidation signal emitted by mutating services."""

    keys: list[str]


@dataclass(frozen=True, slots=True)
class NotifyEvent:
    """A user-facing notification pushed to connected browsers.

    The delivery half of a fired Scheduled trigger: `body` is the message (a
    fixed reminder verbatim, or an agent-prompt result), `trigger_id` ties it
    back to its source, and `title` is an optional short heading.
    """

    body: str
    trigger_id: str
    title: str | None = None


type HubEvent = InvalidateEvent | NotifyEvent
"""Any frame the in-process hub fans out to browser WebSocket connections."""


class EventPublisher(Protocol):
    """Minimal publisher protocol accepted by service layers."""

    async def publish(self, event: HubEvent) -> None:
        """Publish one event."""
        ...


class NullEventPublisher:
    """No-op publisher used when a service is tested without an event hub."""

    async def publish(self, event: HubEvent) -> None:
        """Drop the event."""
        _ = event


class EventHub:
    """Asyncio pub/sub hub for in-process browser events."""

    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[HubEvent]] = set()

    async def publish(self, event: HubEvent) -> None:
        """Fan an event out to every current subscriber."""
        for subscriber in set(self._subscribers):
            await subscriber.put(event)

    def subscribe(self) -> asyncio.Queue[HubEvent]:
        """Create a subscription queue owned by one WebSocket connection."""
        queue: asyncio.Queue[HubEvent] = asyncio.Queue()
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[HubEvent]) -> None:
        """Remove a subscription queue."""
        self._subscribers.discard(queue)
