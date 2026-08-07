"""HTTP behavior tests for provider authorization recovery."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from pathlib import Path
from tempfile import TemporaryDirectory

from snektest import assert_eq, test

from tests.surfaces import login, surface_client
from tether.provider_auth import DeviceCode


class DeviceCodeProviderAuthBackend:
    """Provider backend that waits after publishing its device code."""

    async def check(self) -> bool:
        return False

    async def authorize(self, report: Callable[[DeviceCode], None]) -> None:
        report(
            DeviceCode(
                expires_in_seconds=900,
                user_code="ABCD-EFGH",
                verification_uri="https://auth.openai.com/codex/device",
            )
        )
        await asyncio.Event().wait()


class FakeProviderAuthBackend:
    """Connected provider-auth backend for route tests."""

    async def check(self) -> bool:
        return True

    async def authorize(self, report: Callable[[DeviceCode], None]) -> None:
        raise AssertionError("authorization was not expected")


@test()
def provider_authorization_requires_app_authentication() -> None:
    """Provider credential controls are unavailable without an app session."""
    with (
        TemporaryDirectory() as directory,
        surface_client(
            Path(directory),
            provider_auth_backend=FakeProviderAuthBackend(),
            transcript_sync_enabled=False,
            youtube_sync_enabled=False,
        ) as client,
    ):
        response = client.get("/api/provider-auth/openai-codex")

    assert_eq(response.status_code, 401)


@test()
def authenticated_user_can_read_provider_status() -> None:
    """The settings API reports the server credential without exposing it."""
    with (
        TemporaryDirectory() as directory,
        surface_client(
            Path(directory),
            provider_auth_backend=FakeProviderAuthBackend(),
            transcript_sync_enabled=False,
            youtube_sync_enabled=False,
        ) as client,
    ):
        login(client)

        response = client.get("/api/provider-auth/openai-codex")

    assert_eq(response.status_code, 200)
    assert_eq(
        response.json(),
        {
            "error": None,
            "expires_in_seconds": None,
            "state": "connected",
            "user_code": None,
            "verification_uri": None,
        },
    )


@test()
def concurrent_authorization_request_returns_conflict() -> None:
    """A second API start cannot create a competing rotating-token writer."""
    with (
        TemporaryDirectory() as directory,
        surface_client(
            Path(directory),
            provider_auth_backend=DeviceCodeProviderAuthBackend(),
            transcript_sync_enabled=False,
            youtube_sync_enabled=False,
        ) as client,
    ):
        login(client)
        first = client.post("/api/provider-auth/openai-codex")

        second = client.post("/api/provider-auth/openai-codex")

    assert_eq(first.status_code, 202)
    assert_eq(second.status_code, 409)


@test()
def authenticated_user_can_cancel_device_authorization() -> None:
    """Cancelling through HTTP stops the active helper and clears its code."""
    with (
        TemporaryDirectory() as directory,
        surface_client(
            Path(directory),
            provider_auth_backend=DeviceCodeProviderAuthBackend(),
            transcript_sync_enabled=False,
            youtube_sync_enabled=False,
        ) as client,
    ):
        login(client)
        _ = client.post("/api/provider-auth/openai-codex")

        cancelled = client.delete("/api/provider-auth/openai-codex")

    assert_eq(cancelled.status_code, 200)
    assert_eq(cancelled.json()["state"], "disconnected")
    assert_eq(cancelled.json()["user_code"], None)


@test()
def authenticated_user_can_start_device_authorization() -> None:
    """Starting recovery quickly exposes OpenAI's code through status polling."""
    with (
        TemporaryDirectory() as directory,
        surface_client(
            Path(directory),
            provider_auth_backend=DeviceCodeProviderAuthBackend(),
            transcript_sync_enabled=False,
            youtube_sync_enabled=False,
        ) as client,
    ):
        login(client)

        started = client.post("/api/provider-auth/openai-codex")
        deadline = time.monotonic() + 1.0
        status = client.get("/api/provider-auth/openai-codex")
        while status.json()["user_code"] is None and time.monotonic() < deadline:
            time.sleep(0.01)
            status = client.get("/api/provider-auth/openai-codex")

    assert_eq(started.status_code, 202)
    assert_eq(status.json()["state"], "authorizing")
    assert_eq(status.json()["user_code"], "ABCD-EFGH")
