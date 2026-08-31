"""Regression tests for deferring the ingestion boot sync off startup.

YouTube composition used to `await` the likes/transcript boot pass inside the ASGI
lifespan startup, so uvicorn only bound its port once a full (potentially slow)
sync finished. These tests pin the boot pass to a background task: wiring returns
promptly even when the upstream blocks, and the component exposes readiness.
"""

import asyncio
from collections.abc import AsyncGenerator
from pathlib import Path

import structlog
from opentelemetry import trace
from snekql.sqlite import Config, Database
from snektest import assert_false, assert_true, fixture, load_fixture, test

from tether.background_runtime import BackgroundRuntime
from tether.events import EventHub
from tether.host_config import AppConfig
from tether.ingestion_composition import YouTubeComponent, compose_youtube
from tether.youtube.local import InMemoryYouTubeApi
from tether.youtube.quota import (
    LikedPage,
    RawYouTubeVideo,
)
from tether.youtube.store import create_youtube_schema
from tether.youtube.types import VideoId


def video(video_id: str) -> RawYouTubeVideo:
    """A minimal raw upstream video for seeding the fake liked list."""
    return RawYouTubeVideo(
        video_id=VideoId(video_id), title="A Talk", channel="PyConf", topic="python"
    )


class BlockingLikedApi(InMemoryYouTubeApi):
    """A fake whose liked-list page blocks until `release` is set.

    Stands in for a slow upstream: any code that `await`s a boot pass driven by
    this API cannot make progress until the test releases it.
    """

    def __init__(self, *, liked: list[RawYouTubeVideo], release: asyncio.Event) -> None:
        super().__init__(liked=liked)
        self._release = release

    async def list_liked_page(
        self, *, page_token: str | None, page_size: int
    ) -> LikedPage:
        await self._release.wait()
        return await super().list_liked_page(page_token=page_token, page_size=page_size)


@fixture
async def wired_app(
    api: InMemoryYouTubeApi,
) -> AsyncGenerator[tuple[YouTubeComponent, BackgroundRuntime]]:
    """Compose YouTube over a fresh in-memory database with the given upstream."""
    db = await Database.initialize(backend=Config(database=":memory:"))
    await create_youtube_schema(db)
    logger = structlog.stdlib.get_logger("test.youtube_boot")
    background_runtime = BackgroundRuntime(logger)
    config = AppConfig(
        app_password="test-app-password",
        session_secret="test-session-secret",
        database_path=Path(":memory:"),
        youtube_api=api,
    )
    component = await asyncio.wait_for(
        compose_youtube(
            background_runtime=background_runtime,
            config=config,
            database=db,
            event_publisher=EventHub(),
            logger=logger,
            tracer=trace.get_tracer("test"),
        ),
        timeout=1.0,
    )
    async with background_runtime:
        yield component, background_runtime
    await db.close()


@test()
async def wiring_returns_before_a_blocked_boot_pass_completes() -> None:
    """Startup wiring does not wait on the boot sync (the #119 port-bind hang).

    With an upstream whose liked-list page never resolves, an eager boot pass
    would hang the lifespan; deferring it to a task lets wiring return with the
    boot barrier still unset.
    """
    release = asyncio.Event()
    api = BlockingLikedApi(liked=[video("v1")], release=release)
    component, _lifecycle = await load_fixture(wired_app(api))

    assert_false(component.likes_ready.is_set())

    # Releasing the upstream lets the deferred boot pass run to completion.
    release.set()
    await asyncio.wait_for(component.likes_ready.wait(), timeout=1.0)
    assert_true(component.likes_ready.is_set())
