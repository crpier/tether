"""Subprocess adapter for the co-resident model-provider authorization helper."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import cast

from snekok.result import Err, Ok, Result

from tether.provider_auth_errors import ProviderAuthFailure, ProviderAuthProcessFailure
from tether.provider_auth_model import DeviceCode


class _ProviderAuthProtocolError(Exception):
    """The helper process failed its trusted host-side protocol."""


def provider_auth_helper_command() -> tuple[str, str]:
    """Resolve the co-resident Node helper in source and container layouts."""
    repository_root = Path(__file__).resolve().parents[3]
    return (
        str(repository_root / "apps/agent/node_modules/.bin/tsx"),
        str(repository_root / "apps/agent/src/provider-auth.ts"),
    )


def _decode_event(line: bytes) -> dict[str, object]:
    """Decode one helper event without allowing untrusted shapes past the adapter."""
    try:
        decoded = json.loads(line)
    except json.JSONDecodeError as error:
        message = "provider auth helper emitted invalid JSON"
        raise _ProviderAuthProtocolError(message) from error
    if not isinstance(decoded, dict):
        message = "provider auth helper event is not an object"
        raise _ProviderAuthProtocolError(message)
    return cast("dict[str, object]", decoded)


class SubprocessProviderAuthBackend:
    """Drive provider status and login through a newline-delimited JSON helper."""

    def __init__(self, command: Sequence[str]) -> None:
        self._command: tuple[str, ...] = tuple(command)

    async def check(self) -> Result[bool, ProviderAuthFailure]:
        """Run the helper's refresh-aware status command."""
        status: bool | None = None

        def receive(event: dict[str, object]) -> None:
            nonlocal status
            if event.get("type") == "status" and isinstance(
                event.get("connected"), bool
            ):
                status = cast("bool", event["connected"])

        try:
            await self._run("status", receive)
            if status is None:
                message = "provider auth helper omitted its status event"
                raise _ProviderAuthProtocolError(message)  # noqa: TRY301
        except (_ProviderAuthProtocolError, OSError) as error:
            return Err(
                ProviderAuthProcessFailure(operation="status", reason=str(error))
            )
        return Ok(status)

    async def authorize(
        self, report: Callable[[DeviceCode], None]
    ) -> Result[None, ProviderAuthFailure]:
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
                raise _ProviderAuthProtocolError(message)
            report(
                DeviceCode(
                    expires_in_seconds=expires,
                    user_code=user_code,
                    verification_uri="https://auth.openai.com/codex/device",
                )
            )

        try:
            await self._run("login", receive)
            if not completed:
                message = "provider auth helper omitted its completion event"
                raise _ProviderAuthProtocolError(message)  # noqa: TRY301
        except (_ProviderAuthProtocolError, OSError) as error:
            return Err(ProviderAuthProcessFailure(operation="login", reason=str(error)))
        return Ok(None)

    async def _run(
        self, command: str, receive: Callable[[dict[str, object]], None]
    ) -> None:
        """Own helper process cleanup across success, cancellation, and failure."""
        if not self._command:
            message = "provider auth helper command is empty"
            raise _ProviderAuthProtocolError(message)
        process = await asyncio.create_subprocess_exec(
            *self._command,
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            if process.stdout is None:
                message = "provider auth helper stdout is unavailable"
                raise _ProviderAuthProtocolError(message)  # noqa: TRY301
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
            raise _ProviderAuthProtocolError(message)
