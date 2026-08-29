"""VAPID Web Push transport and delivery across stored subscriptions."""

from __future__ import annotations

import asyncio
import importlib
import json
from typing import Any, Protocol

from snekok.result import Err, Ok, Result
from snekql.sqlite import Fetched

from tether.notification_delivery import PushNotification
from tether.push_errors import WebPushFailure, WebPushGoneFailure
from tether.push_model import VapidConfig
from tether.push_store import PushSubscription


class WebPushTransport(Protocol):
    """Send one payload through a browser Push provider."""

    async def send(
        self,
        *,
        endpoint: str,
        p256dh: str,
        auth: str,
        body: str,
    ) -> Result[None, WebPushFailure]:
        """Return expected provider lifecycle failures as values."""
        ...


class PushSubscriptionPort(Protocol):
    """Subscription operations required by the delivery fan-out."""

    async def active_subscriptions(self) -> list[PushSubscription[Fetched]]:
        """List subscriptions currently eligible for delivery."""
        ...

    async def unsubscribe(self, endpoint: str) -> None:
        """Convergently remove an endpoint."""
        ...


class VapidWebPushTransport:
    """Send browser Push messages through `pywebpush` in a worker thread."""

    def __init__(self, config: VapidConfig) -> None:
        self.config: VapidConfig = config

    async def send(
        self,
        *,
        endpoint: str,
        p256dh: str,
        auth: str,
        body: str,
    ) -> Result[None, WebPushFailure]:
        """Send one payload without blocking the event loop."""
        return await asyncio.to_thread(
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
    ) -> Result[None, WebPushFailure]:
        """Translate only provider-reported gone endpoints into typed values."""
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
                return Err(WebPushGoneFailure(endpoint=endpoint))
            raise
        return Ok(None)


class StoredPushSender:
    """Fan one message out to live subscriptions and prune gone endpoints."""

    def __init__(
        self,
        *,
        push_service: PushSubscriptionPort,
        transport: WebPushTransport,
    ) -> None:
        self.push_service: PushSubscriptionPort = push_service
        self.transport: WebPushTransport = transport

    async def send(self, notification: PushNotification) -> None:
        """Deliver structured content and preserve unexpected provider defects."""
        body = json.dumps(
            {
                "body": notification.body,
                "title": notification.title,
                "url": notification.url,
            },
            separators=(",", ":"),
        )
        for subscription in await self.push_service.active_subscriptions():
            if not subscription.endpoint.startswith(("https://", "http://")):
                continue
            outcome = await self.transport.send(
                endpoint=subscription.endpoint,
                p256dh=subscription.p256dh,
                auth=subscription.auth,
                body=body,
            )
            if isinstance(outcome, Err):
                await self.push_service.unsubscribe(outcome.error.endpoint)
