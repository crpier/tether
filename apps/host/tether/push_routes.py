"""HTTP presentation for Web Push configuration and subscriptions."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Protocol, cast

from fastapi import APIRouter, Query
from pydantic import BaseModel
from snekql.sqlite import Fetched
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from tether.push import PushService
from tether.push_model import PushStatus
from tether.push_store import PushSubscription


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
        """Render a canonical subscription row for browser clients."""
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
        """Render a domain status for browser clients."""
        return cls(subscribed=status.subscribed, count=status.count)


class PushConfigRead(BaseModel):
    """HTTP representation of browser push configuration."""

    vapid_public_key: str


class _PushRuntime(Protocol):
    """Push dependencies available while serving requests."""

    push_service: PushService
    vapid_public_key: str


def _runtime(request: Request) -> _PushRuntime:
    """Read Push dependencies from the canonical host runtime."""
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
