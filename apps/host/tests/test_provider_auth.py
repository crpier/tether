"""Behavior tests for server-owned provider authorization."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable

from snektest import assert_eq, assert_raises, test

from tether.provider_auth import (
    DeviceCode,
    ProviderAuthorizationActiveError,
    ProviderAuthService,
    SubprocessProviderAuthBackend,
)


class FakeProviderAuthBackend:
    """Controllable provider-auth process boundary."""

    def __init__(self, *, connected: bool) -> None:
        self.connected = connected
        self.device_code: DeviceCode | None = None
        self.authorization_started = asyncio.Event()
        self.finish_authorization = asyncio.Event()

    async def check(self) -> bool:
        return self.connected

    async def authorize(self, report: Callable[[DeviceCode], None]) -> None:
        if self.device_code is None:
            raise AssertionError("authorization was not expected")
        report(self.device_code)
        self.authorization_started.set()
        await self.finish_authorization.wait()
        self.connected = True


@test()
async def subprocess_status_reads_the_json_line_protocol() -> None:
    """The host adapter recognizes the helper's connected status event."""
    backend = SubprocessProviderAuthBackend(
        (
            sys.executable,
            "-c",
            "import json; print(json.dumps({'connected': True, 'type': 'status'}))",
        )
    )

    connected = await backend.check()

    assert_eq(connected, True)


@test()
async def subprocess_login_forwards_the_validated_device_code() -> None:
    """The host adapter forwards safe code data and recognizes completion."""
    script = (
        "import json; "
        "print(json.dumps({'type': 'device_code', 'user_code': 'ABCD-EFGH', "
        "'verification_uri': 'https://auth.openai.com/codex/device', "
        "'expires_in_seconds': 900}), flush=True); "
        "print(json.dumps({'type': 'complete'}), flush=True)"
    )
    backend = SubprocessProviderAuthBackend((sys.executable, "-c", script))
    codes: list[DeviceCode] = []

    await backend.authorize(codes.append)

    assert_eq(
        codes,
        [
            DeviceCode(
                expires_in_seconds=900,
                user_code="ABCD-EFGH",
                verification_uri="https://auth.openai.com/codex/device",
            )
        ],
    )


@test()
async def status_reports_refreshable_server_credential() -> None:
    """A successful helper health check is exposed as connected."""
    service = ProviderAuthService(FakeProviderAuthBackend(connected=True))

    status = await service.status()

    assert_eq(status.state, "connected")


@test()
async def authorization_reports_the_provider_device_code() -> None:
    """An active recovery exposes only the code and trusted verification URL."""
    backend = FakeProviderAuthBackend(connected=False)
    backend.device_code = DeviceCode(
        expires_in_seconds=900,
        user_code="ABCD-EFGH",
        verification_uri="https://auth.openai.com/codex/device",
    )
    service = ProviderAuthService(backend)

    _ = await service.start()
    await backend.authorization_started.wait()
    status = await service.status()

    assert_eq(status.state, "authorizing")
    assert_eq(status.user_code, "ABCD-EFGH")
    assert_eq(status.verification_uri, "https://auth.openai.com/codex/device")
    assert_eq(status.expires_in_seconds, 900)
    backend.finish_authorization.set()
    await service.shutdown()


@test()
async def concurrent_authorization_is_rejected() -> None:
    """Only one rotating-credential login may own the helper at a time."""
    backend = FakeProviderAuthBackend(connected=False)
    backend.device_code = DeviceCode(
        expires_in_seconds=900,
        user_code="ABCD-EFGH",
        verification_uri="https://auth.openai.com/codex/device",
    )
    service = ProviderAuthService(backend)
    _ = await service.start()

    with assert_raises(ProviderAuthorizationActiveError):
        _ = await service.start()

    await service.shutdown()


@test()
async def failed_authorization_is_reported_without_provider_details() -> None:
    """A failed helper yields a safe retry message, never raw OAuth data."""
    service = ProviderAuthService(FakeProviderAuthBackend(connected=False))
    _ = await service.start()
    await asyncio.sleep(0)

    status = await service.status()

    assert_eq(status.state, "disconnected")
    assert_eq(status.error, "Provider authorization failed. Try again.")


@test()
async def successful_authorization_invalidates_live_runtimes() -> None:
    """Credential replacement invokes the host's live-runtime reset callback."""
    backend = FakeProviderAuthBackend(connected=False)
    backend.device_code = DeviceCode(
        expires_in_seconds=900,
        user_code="ABCD-EFGH",
        verification_uri="https://auth.openai.com/codex/device",
    )
    invalidated = asyncio.Event()

    async def invalidate() -> None:
        invalidated.set()

    service = ProviderAuthService(backend, on_authorized=invalidate)
    _ = await service.start()
    await backend.authorization_started.wait()

    backend.finish_authorization.set()
    await invalidated.wait()

    assert_eq((await service.status()).state, "connected")


@test()
async def cancellation_stops_active_authorization() -> None:
    """Cancelling recovery returns to the server credential's current state."""
    backend = FakeProviderAuthBackend(connected=False)
    backend.device_code = DeviceCode(
        expires_in_seconds=900,
        user_code="ABCD-EFGH",
        verification_uri="https://auth.openai.com/codex/device",
    )
    service = ProviderAuthService(backend)
    _ = await service.start()
    await backend.authorization_started.wait()

    status = await service.cancel()

    assert_eq(status.state, "disconnected")
