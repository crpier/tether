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

import asyncio
import importlib
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any, Protocol, cast
from uuid import uuid7

from fastapi import APIRouter, Query
from pydantic import UUID7, BaseModel
from snekql.sqlite import (
    CurrentTimestamp,
    Database,
    Fetched,
    Model,
    Pending,
    Text,
    Transaction,
    UtcDatetime,
    insert,
    select,
    update,
)
from snekql.sqlite._schema_ddl import scaffold_sqlite_statements
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from tether.events import EventPublisher, InvalidateEvent, NullEventPublisher


class PushSubscription[S = Pending](Model[S, "PushSubscription[Fetched]"]):
    """One browser's Web Push subscription, keyed by its push endpoint."""

    id: PushSubscription.GenCol[UUID7] = Text(primary_key=True, default_factory=uuid7)
    endpoint: PushSubscription.Col[str] = Text(unique=True)
    """The browser push endpoint URL; the subscription's stable identity."""
    p256dh: PushSubscription.Col[str] = Text()
    """The subscription's public key, used by a future VAPID transport."""
    auth: PushSubscription.Col[str] = Text()
    """The subscription's auth secret, used by a future VAPID transport."""
    created_at: PushSubscription.GenCol[UtcDatetime] = Text(default=CurrentTimestamp)
    updated_at: PushSubscription.GenCol[UtcDatetime] = Text(default=CurrentTimestamp)
    deleted_at: PushSubscription.Col[UtcDatetime | None] = Text(
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


class WebPushGoneError(Exception):
    """The browser push service no longer knows this subscription."""

    def __init__(self, endpoint: str) -> None:
        super().__init__("web push subscription is gone")
        self.endpoint: str = endpoint


@dataclass(frozen=True, slots=True)
class VapidConfig:
    """Secrets and public key used to authenticate browser push messages."""

    private_key: str
    public_key: str
    subject: str


class WebPushTransport(Protocol):
    """Sends one browser push message through a concrete provider."""

    async def send(
        self,
        *,
        endpoint: str,
        p256dh: str,
        auth: str,
        body: str,
    ) -> None:
        """Send one Web Push payload."""
        ...


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

        async def _subscribe(tx: Transaction) -> PushSubscription[Fetched]:
            existing = await tx.fetch_one_or_none(
                select(PushSubscription).where(PushSubscription.endpoint.eq(endpoint))
            )
            if existing is None:
                return await tx.execute(
                    insert(
                        PushSubscription(endpoint=endpoint, p256dh=p256dh, auth=auth)
                    ).returning()
                )
            _ = await tx.execute(
                update(PushSubscription)
                .set(PushSubscription.p256dh.to(p256dh))
                .set(PushSubscription.auth.to(auth))
                .set(PushSubscription.deleted_at.to(None))
                .set(PushSubscription.updated_at.to(CurrentTimestamp))
                .where(PushSubscription.endpoint.eq(endpoint))
            )
            refreshed = await tx.fetch_one_or_none(
                select(PushSubscription).where(PushSubscription.endpoint.eq(endpoint))
            )
            assert refreshed is not None  # row exists: just updated it
            return refreshed

        async with self.database.transaction(mode="immediate") as tx:
            subscription = await _subscribe(tx)
        await self.event_publisher.publish(InvalidateEvent(keys=["push"]))
        return subscription

    async def unsubscribe(self, endpoint: str) -> None:
        """Remove a subscription convergently; a missing endpoint is a no-op."""

        async def _unsubscribe(tx: Transaction) -> int:
            return await tx.execute(
                update(PushSubscription)
                .set(PushSubscription.deleted_at.to(CurrentTimestamp))
                .set(PushSubscription.updated_at.to(CurrentTimestamp))
                .where(PushSubscription.endpoint.eq(endpoint))
                .where(PushSubscription.deleted_at.is_null())
            )

        async with self.database.transaction(mode="immediate") as tx:
            matched = await _unsubscribe(tx)
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
        """Return every live subscription for Web Push delivery."""
        async with self.database.transaction() as tx:
            return await tx.fetch_all(
                select(PushSubscription).where(PushSubscription.deleted_at.is_null())
            )


class VapidWebPushTransport:
    """Sends browser push messages through `pywebpush` in a worker thread."""

    def __init__(self, config: VapidConfig) -> None:
        self.config: VapidConfig = config

    async def send(
        self,
        *,
        endpoint: str,
        p256dh: str,
        auth: str,
        body: str,
    ) -> None:
        """Send one VAPID-authenticated Web Push payload."""
        await asyncio.to_thread(
            self._send_blocking,
            endpoint=endpoint,
            p256dh=p256dh,
            auth=auth,
            body=body,
        )

    def _send_blocking(
        self,
        *,
        endpoint: str,
        p256dh: str,
        auth: str,
        body: str,
    ) -> None:
        """Bridge the synchronous `pywebpush` API behind the async port."""
        webpush_module: Any = importlib.import_module("pywebpush")
        try:
            _ = webpush_module.webpush(
                subscription_info={
                    "endpoint": endpoint,
                    "keys": {"p256dh": p256dh, "auth": auth},
                },
                data=body,
                vapid_private_key=self.config.private_key,
                vapid_claims={"sub": self.config.subject},
            )
        except Exception as error:
            response: Any = getattr(error, "response", None)
            if getattr(response, "status_code", None) in {404, 410}:
                raise WebPushGoneError(endpoint) from error
            raise


class StoredPushSender:
    """Sends one notification to every currently live subscription.

    Gone subscriptions are pruned convergently so failed browsers do not poison
    later deliveries.
    """

    def __init__(
        self, *, push_service: PushService, transport: WebPushTransport
    ) -> None:
        self.push_service: PushService = push_service
        self.transport: WebPushTransport = transport

    async def send(self, body: str) -> None:
        """Send `body` to all live subscriptions, pruning gone endpoints."""
        for subscription in await self.push_service.active_subscriptions():
            if not subscription.endpoint.startswith(("https://", "http://")):
                continue
            try:
                await self.transport.send(
                    endpoint=subscription.endpoint,
                    p256dh=subscription.p256dh,
                    auth=subscription.auth,
                    body=body,
                )
            except WebPushGoneError:
                await self.push_service.unsubscribe(subscription.endpoint)


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


class PushConfigRead(BaseModel):
    """HTTP representation of browser push configuration."""

    vapid_public_key: str


class _PushRuntime(Protocol):
    """Push dependencies available while the host serves requests."""

    push_service: PushService
    vapid_public_key: str


def _runtime(request: Request) -> _PushRuntime:
    """Read push dependencies from the canonical host runtime."""
    return cast("_PushRuntime", request.app.state.runtime)


router = APIRouter()


@router.get("/api/push/config", response_model=PushConfigRead)
async def push_config(request: Request) -> Response:
    """Expose the VAPID public key the browser needs to subscribe."""
    return JSONResponse(
        PushConfigRead(vapid_public_key=_runtime(request).vapid_public_key).model_dump(
            mode="json"
        )
    )


@router.post(
    "/api/push/subscriptions", response_model=PushSubscriptionRead, status_code=201
)
async def subscribe_push(request: Request, body: SubscribeRequest) -> Response:
    """Register (or refresh) this browser's push subscription."""
    subscription = await _runtime(request).push_service.subscribe(
        body.endpoint, p256dh=body.p256dh, auth=body.auth
    )
    return JSONResponse(
        PushSubscriptionRead.from_subscription(subscription).model_dump(mode="json"),
        status_code=201,
    )


@router.delete("/api/push/subscriptions", response_model=PushStatusRead)
async def unsubscribe_push(request: Request, body: UnsubscribeRequest) -> Response:
    """Remove this browser's push subscription."""
    await _runtime(request).push_service.unsubscribe(body.endpoint)
    status = await _runtime(request).push_service.status(body.endpoint)
    return JSONResponse(PushStatusRead.from_status(status).model_dump(mode="json"))


@router.get("/api/push/status", response_model=PushStatusRead)
async def push_status(
    request: Request, query: Annotated[StatusQuery, Query()]
) -> Response:
    """Report whether this browser (or any browser) is subscribed."""
    status = await _runtime(request).push_service.status(query.endpoint)
    return JSONResponse(PushStatusRead.from_status(status).model_dump(mode="json"))
