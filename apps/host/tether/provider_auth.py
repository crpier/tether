"""Server-owned model-provider authorization and device-code recovery."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, cast

ProviderAuthState = Literal["authorizing", "connected", "disconnected", "error"]


def provider_auth_helper_command() -> tuple[str, str]:
    """Resolve the co-resident Node helper in source and container layouts."""
    repository_root = Path(__file__).resolve().parents[3]
    return (
        str(repository_root / "apps/agent/node_modules/.bin/tsx"),
        str(repository_root / "apps/agent/src/provider-auth.ts"),
    )


def _decode_event(line: bytes) -> dict[str, object]:
    """Decode one helper event without letting untrusted shapes cross the seam."""
    try:
        decoded = json.loads(line)
    except json.JSONDecodeError as error:
        message = "provider auth helper emitted invalid JSON"
        raise ProviderAuthProcessError(message) from error
    if not isinstance(decoded, dict):
        message = "provider auth helper event is not an object"
        raise ProviderAuthProcessError(message)
    return cast("dict[str, object]", decoded)


@dataclass(frozen=True, slots=True)
class DeviceCode:
    """Browser-safe data for completing a provider device authorization."""

    expires_in_seconds: int | None
    user_code: str
    verification_uri: str


@dataclass(frozen=True, slots=True)
class ProviderAuthStatus:
    """Current server credential or recovery-attempt state."""

    error: str | None = None
    expires_in_seconds: int | None = None
    state: ProviderAuthState = "disconnected"
    user_code: str | None = None
    verification_uri: str | None = None


class ProviderAuthorizationActiveError(Exception):
    """Raised when recovery starts while another attempt is active."""


class ProviderAuthProcessError(Exception):
    """Raised when pi's provider-auth helper violates or fails its protocol."""


class ProviderAuthBackend(Protocol):
    """Process boundary to pi's provider-auth runtime."""

    async def check(self) -> bool:
        """Refresh if needed and report whether provider auth is usable."""
        ...

    async def authorize(self, report: Callable[[DeviceCode], None]) -> None:
        """Run device authorization, reporting its browser-safe code."""
        ...


class SubprocessProviderAuthBackend:
    """Drive pi's provider auth through its newline-delimited JSON helper."""

    def __init__(self, command: Sequence[str]) -> None:
        self._command: tuple[str, ...] = tuple(command)

    async def check(self) -> bool:
        """Run the helper's refresh-aware status command."""
        status: bool | None = None

        def receive(event: dict[str, object]) -> None:
            nonlocal status
            if event.get("type") == "status" and isinstance(
                event.get("connected"), bool
            ):
                status = cast("bool", event["connected"])

        await self._run("status", receive)
        if status is None:
            message = "provider auth helper omitted its status event"
            raise ProviderAuthProcessError(message)
        return status

    async def authorize(self, report: Callable[[DeviceCode], None]) -> None:
        """Run device login and forward only validated browser-safe events."""
        completed = False

        def receive(event: dict[str, object]) -> None:
            nonlocal completed
            event_type = event.get("type")
            if event_type == "complete":
                completed = True
                return
            if event_type != "device_code":
                return
            user_code = event.get("user_code")
            verification_uri = event.get("verification_uri")
            expires = event.get("expires_in_seconds")
            if (
                not isinstance(user_code, str)
                or not user_code
                or verification_uri != "https://auth.openai.com/codex/device"
                or (expires is not None and not isinstance(expires, int))
            ):
                message = "provider auth helper returned an invalid device code"
                raise ProviderAuthProcessError(message)
            report(
                DeviceCode(
                    expires_in_seconds=expires,
                    user_code=user_code,
                    verification_uri="https://auth.openai.com/codex/device",
                )
            )

        await self._run("login", receive)
        if not completed:
            message = "provider auth helper omitted its completion event"
            raise ProviderAuthProcessError(message)

    async def _run(
        self, command: str, receive: Callable[[dict[str, object]], None]
    ) -> None:
        if not self._command:
            message = "provider auth helper command is empty"
            raise ProviderAuthProcessError(message)
        process = await asyncio.create_subprocess_exec(
            *self._command,
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        if process.stdout is None:
            message = "provider auth helper stdout is unavailable"
            raise ProviderAuthProcessError(message)
        try:
            while line := await process.stdout.readline():
                receive(_decode_event(line))
            return_code = await process.wait()
        except BaseException:
            if process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    process.terminate()
                try:
                    _ = await asyncio.wait_for(process.wait(), timeout=2.0)
                except TimeoutError:
                    process.kill()
                    _ = await process.wait()
            raise
        if return_code != 0:
            message = "provider auth helper failed"
            raise ProviderAuthProcessError(message)


class ProviderAuthService:
    """Coordinate one server-owned provider authorization attempt."""

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
            self._task = None
            return self._status
        try:
            connected = await self._backend.check()
        except Exception:
            if self._task is not None:
                return self._status
            self._status = ProviderAuthStatus(
                error="Could not check provider authorization.", state="error"
            )
        else:
            if self._task is not None:
                return self._status
            self._status = ProviderAuthStatus(
                state="connected" if connected else "disconnected"
            )
        return self._status

    async def start(self) -> ProviderAuthStatus:
        """Start device-code recovery without blocking the request."""
        if self._task is not None and not self._task.done():
            raise ProviderAuthorizationActiveError
        self._status = ProviderAuthStatus(state="authorizing")
        self._task = asyncio.create_task(self._authorize())
        return self._status

    async def cancel(self) -> ProviderAuthStatus:
        """Cancel active recovery and recheck the existing credential."""
        await self.shutdown()
        self._task = None
        return await self.status()

    async def shutdown(self) -> None:
        """Cancel an active provider authorization during host shutdown."""
        if self._task is None or self._task.done():
            return
        _ = self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task

    async def _authorize(self) -> None:
        try:
            await self._backend.authorize(self._report_device_code)
            if self._on_authorized is not None:
                await self._on_authorized()
        except asyncio.CancelledError:
            self._status = ProviderAuthStatus(state="disconnected")
            raise
        except Exception:
            self._status = ProviderAuthStatus(
                error="Provider authorization failed. Try again.",
                state="disconnected",
            )
        else:
            self._status = ProviderAuthStatus(state="connected")

    def _report_device_code(self, device_code: DeviceCode) -> None:
        self._status = ProviderAuthStatus(
            expires_in_seconds=device_code.expires_in_seconds,
            state="authorizing",
            user_code=device_code.user_code,
            verification_uri=device_code.verification_uri,
        )
