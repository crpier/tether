"""Web push subscriptions: subscribe / unsubscribe / status.

This is the durable record of which browsers have asked to receive pushed
notifications. Today the delivery transport for a fired Scheduled trigger is the
in-process event hub over the open WebSocket (see `tether.scheduler`); these
stored subscriptions are the half that lets a real VAPID Web Push transport — to
a browser whose tab is closed — be added later without reworking the surface.

Subscribing is idempotent on the push `endpoint` (the unique browser identity):
re-subscribing refreshes the keys and revives a previously removed row.
Unsubscribing is convergent — removing an endpoint that is already gone (or was
never seen) is a no-op, not an error — so a browser that lost local state can
always converge on "not subscribed".

>>> service = PushService(database=database)
>>> _ = await service.subscribe("https://push/abc", p256dh="k", auth="a")
>>> (await service.status()).count
1
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import uuid7

from pydantic import UUID7, BaseModel
from snekql.sqlite import (
    CurrentTimestamp,
    Database,
    Fetched,
    Model,
    Pending,
    Text,
    insert,
    select,
    update,
)
from snekql.sqlite._schema_ddl import scaffold_sqlite_statements
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from tether.events import EventPublisher, InvalidateEvent, NullEventPublisher
from tether.openapi import EndpointRoute, endpoint


class PushSubscription[S = Pending](Model[S, "PushSubscription[Fetched]"]):
    """One browser's Web Push subscription, keyed by its push endpoint."""

    id: PushSubscription.GenCol[UUID7] = Text(primary_key=True, default_factory=uuid7)
    endpoint: PushSubscription.Col[str] = Text(unique=True)
    """The browser push endpoint URL; the subscription's stable identity."""
    p256dh: PushSubscription.Col[str] = Text()
    """The subscription's public key, used by a future VAPID transport."""
    auth: PushSubscription.Col[str] = Text()
    """The subscription's auth secret, used by a future VAPID transport."""
    created_at: PushSubscription.GenCol[datetime] = Text(default=CurrentTimestamp)
    updated_at: PushSubscription.GenCol[datetime] = Text(default=CurrentTimestamp)
    deleted_at: PushSubscription.Col[datetime | None] = Text(
        default=None,
        nullable=True,
    )


@dataclass(frozen=True, slots=True)
class PushStatus:
    """A snapshot of push-subscription state for the browser.

    `count` is the number of live subscriptions; `subscribed` answers whether a
    queried endpoint is live (or, when no endpoint is queried, whether any
    subscription exists at all).
    """

    subscribed: bool
    count: int


class PushService:
    """Persistence boundary for Web Push subscriptions."""

    def __init__(
        self,
        database: Database,
        event_publisher: EventPublisher | None = None,
    ) -> None:
        self.database: Database = database
        self.event_publisher: EventPublisher = event_publisher or NullEventPublisher()

    async def subscribe(
        self,
        endpoint: str,
        *,
        p256dh: str,
        auth: str,
    ) -> PushSubscription[Fetched]:
        """Record (or refresh) a subscription for one browser endpoint.

        Idempotent on the endpoint: an existing row has its keys refreshed and
        any prior removal undone, so re-subscribing the same browser converges
        on a single live row rather than accumulating duplicates.
        """
        async with self.database.transaction() as tx:
            existing = await tx.fetch_one_or_none(
                select(PushSubscription).where(PushSubscription.endpoint.eq(endpoint))
            )
            if existing is None:
                subscription = await tx.execute(
                    insert(
                        PushSubscription(endpoint=endpoint, p256dh=p256dh, auth=auth)
                    ).returning()
                )
            else:
                _ = await tx.execute(
                    update(PushSubscription)
                    .set(PushSubscription.p256dh.to(p256dh))
                    .set(PushSubscription.auth.to(auth))
                    .set(PushSubscription.deleted_at.to(None))
                    .set(PushSubscription.updated_at.to(CurrentTimestamp))
                    .where(PushSubscription.endpoint.eq(endpoint))
                )
                refreshed = await tx.fetch_one_or_none(
                    select(PushSubscription).where(
                        PushSubscription.endpoint.eq(endpoint)
                    )
                )
                assert refreshed is not None  # row exists: just updated it
                subscription = refreshed
        await self.event_publisher.publish(InvalidateEvent(keys=["push"]))
        return subscription

    async def unsubscribe(self, endpoint: str) -> None:
        """Remove a subscription convergently; a missing endpoint is a no-op."""
        async with self.database.transaction() as tx:
            matched = await tx.execute(
                update(PushSubscription)
                .set(PushSubscription.deleted_at.to(CurrentTimestamp))
                .set(PushSubscription.updated_at.to(CurrentTimestamp))
                .where(PushSubscription.endpoint.eq(endpoint))
                .where(PushSubscription.deleted_at.is_null())
            )
        if matched:
            await self.event_publisher.publish(InvalidateEvent(keys=["push"]))

    async def status(self, endpoint: str | None = None) -> PushStatus:
        """Report live-subscription count and whether `endpoint` is subscribed."""
        async with self.database.transaction() as tx:
            live = await tx.fetch_all(
                select(PushSubscription).where(PushSubscription.deleted_at.is_null())
            )
        count = len(live)
        if endpoint is None:
            return PushStatus(subscribed=count > 0, count=count)
        subscribed = any(subscription.endpoint == endpoint for subscription in live)
        return PushStatus(subscribed=subscribed, count=count)

    async def active_subscriptions(self) -> list[PushSubscription[Fetched]]:
        """Return every live subscription (for a future push transport)."""
        async with self.database.transaction() as tx:
            return await tx.fetch_all(
                select(PushSubscription).where(PushSubscription.deleted_at.is_null())
            )


async def create_push_schema(database: Database) -> None:
    """Create the push-subscription table on an initialized database."""
    migrations = {
        f"006_{label}": sql
        for label, sql in scaffold_sqlite_statements([PushSubscription])
    }
    await database.migrate(migrations)


class SubscribeRequest(BaseModel):
    """Body for registering a browser push subscription.

    >>> SubscribeRequest(endpoint="https://push/abc", p256dh="k", auth="a").endpoint
    'https://push/abc'
    """

    endpoint: str
    p256dh: str
    auth: str


class UnsubscribeRequest(BaseModel):
    """Body for removing a browser push subscription."""

    endpoint: str


class StatusQuery(BaseModel):
    """Query string for the push-status check, optionally scoped to an endpoint."""

    endpoint: str | None = None


class PushSubscriptionRead(BaseModel):
    """HTTP representation of a stored push subscription."""

    endpoint: str
    created_at: datetime

    @classmethod
    def from_subscription(
        cls, subscription: PushSubscription[Fetched]
    ) -> PushSubscriptionRead:
        """Render a stored subscription for browser clients."""
        return cls(
            endpoint=subscription.endpoint,
            created_at=subscription.created_at,
        )


class PushStatusRead(BaseModel):
    """HTTP representation of the browser's push-subscription status."""

    subscribed: bool
    count: int

    @classmethod
    def from_status(cls, status: PushStatus) -> PushStatusRead:
        """Render a push status snapshot for browser clients."""
        return cls(subscribed=status.subscribed, count=status.count)


@endpoint(request_body=SubscribeRequest, response=PushSubscriptionRead, status=201)
async def subscribe_push(request: Request, body: SubscribeRequest) -> Response:
    """Register (or refresh) this browser's push subscription."""
    subscription = await request.app.state.push_service.subscribe(
        body.endpoint, p256dh=body.p256dh, auth=body.auth
    )
    return JSONResponse(
        PushSubscriptionRead.from_subscription(subscription).model_dump(mode="json"),
        status_code=201,
    )


@endpoint(request_body=UnsubscribeRequest, response=PushStatusRead)
async def unsubscribe_push(request: Request, body: UnsubscribeRequest) -> Response:
    """Remove this browser's push subscription."""
    await request.app.state.push_service.unsubscribe(body.endpoint)
    status = await request.app.state.push_service.status(body.endpoint)
    return JSONResponse(PushStatusRead.from_status(status).model_dump(mode="json"))


@endpoint(query=StatusQuery, response=PushStatusRead)
async def push_status(request: Request, query: StatusQuery) -> Response:
    """Report whether this browser (or any browser) is subscribed."""
    status = await request.app.state.push_service.status(query.endpoint)
    return JSONResponse(PushStatusRead.from_status(status).model_dump(mode="json"))


push_routes: list[Route] = [
    EndpointRoute("/api/push/subscriptions", subscribe_push, methods=["POST"]),
    EndpointRoute("/api/push/subscriptions", unsubscribe_push, methods=["DELETE"]),
    EndpointRoute("/api/push/status", push_status, methods=["GET"]),
]
