"""Lifecycle ownership for server-owned provider authorization recovery."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from typing import Protocol

from snekok import Err, Ok, Result

from tether.provider_auth_errors import (
    ProviderAuthFailure,
    ProviderAuthorizationActiveFailure,
)
from tether.provider_auth_model import DeviceCode, ProviderAuthStatus


class ProviderAuthBackend(Protocol):
    """Boundary to the provider credential runtime."""

    async def check(self) -> Result[bool, ProviderAuthFailure]:
        """Refresh if needed and report whether authorization is usable."""
        ...

    async def authorize(
        self, report: Callable[[DeviceCode], None]
    ) -> Result[None, ProviderAuthFailure]:
        """Run device authorization and report its validated browser-safe code."""
        ...


class ProviderAuthService:
    """Coordinate exactly one server-owned provider authorization attempt."""

    def __init__(
        self,
        backend: ProviderAuthBackend,
        *,
        on_authorized: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._backend: ProviderAuthBackend = backend
        self._on_authorized: Callable[[], Awaitable[None]] | None = on_authorized
        self._status: ProviderAuthStatus = ProviderAuthStatus()
        self._task: asyncio.Task[None] | None = None

    async def status(self) -> ProviderAuthStatus:
        """Refresh and report the server-owned credential state."""
        if self._task is not None:
            if not self._task.done():
                return self._status
            completed_task = self._task
            self._task = None
            completed_task.result()
            return self._status
        outcome = await self._backend.check()
        if self._task is not None:
            return self._status
        if isinstance(outcome, Err):
            self._status = ProviderAuthStatus(
                error="Could not check provider authorization.", state="error"
            )
        else:
            self._status = ProviderAuthStatus(
                state="connected" if outcome.value else "disconnected"
            )
        return self._status

    async def start(
        self,
    ) -> Result[ProviderAuthStatus, ProviderAuthorizationActiveFailure]:
        """Start device-code recovery without blocking the request."""
        if self._task is not None and not self._task.done():
            return Err(ProviderAuthorizationActiveFailure())
        if self._task is not None:
            completed_task = self._task
            self._task = None
            completed_task.result()
        self._status = ProviderAuthStatus(state="authorizing")
        self._task = asyncio.create_task(self._authorize())
        return Ok(self._status)

    async def cancel(self) -> ProviderAuthStatus:
        """Cancel active recovery and recheck the existing credential."""
        await self.shutdown()
        self._task = None
        return await self.status()

    async def shutdown(self) -> None:
        """Cancel active recovery and retrieve any completed task outcome."""
        if self._task is None:
            return
        task = self._task
        self._task = None
        if not task.done():
            _ = task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            return
        task.result()

    async def _authorize(self) -> None:
        try:
            outcome = await self._backend.authorize(self._report_device_code)
            if isinstance(outcome, Err):
                self._status = ProviderAuthStatus(
                    error="Provider authorization failed. Try again.",
                    state="disconnected",
                )
                return
            if self._on_authorized is not None:
                await self._on_authorized()
        except asyncio.CancelledError:
            self._status = ProviderAuthStatus(state="disconnected")
            raise
        self._status = ProviderAuthStatus(state="connected")

    def _report_device_code(self, device_code: DeviceCode) -> None:
        self._status = ProviderAuthStatus(
            expires_in_seconds=device_code.expires_in_seconds,
            state="authorizing",
            user_code=device_code.user_code,
            verification_uri=device_code.verification_uri,
        )
