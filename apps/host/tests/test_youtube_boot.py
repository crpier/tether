"""Regression tests for deferring the ingestion boot sync off startup.

`_wire_youtube` used to `await` the likes/transcript boot pass inside the ASGI
lifespan startup, so uvicorn only bound its port once a full (potentially slow)
sync finished — the `just dev` hang of #119/#122. These tests pin the boot pass
to a background task: wiring returns promptly even when the upstream blocks, and
a `youtube_boot_done` barrier on app state lets callers await boot completion.
"""

import asyncio
import types
from collections.abc import AsyncGenerator
from pathlib import Path

import structlog
from opentelemetry import trace
from snekql.sqlite import Config, Database
from snektest import assert_false, assert_true, fixture, load_fixture, test
from starlette.applications import Starlette

from tether.events import EventHub
from tether.server import AppConfig, _wire_youtube
from tether.youtube import (
    InMemoryYouTubeApi,
    LikedPage,
    RawYouTubeVideo,
    create_youtube_schema,
)


def video(video_id: str) -> RawYouTubeVideo:
    """A minimal raw upstream video for seeding the fake liked list."""
    return RawYouTubeVideo(
        video_id=video_id, title="A Talk", channel="PyConf", topic="python"
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
) -> AsyncGenerator[tuple[Starlette, list[asyncio.Task[None]]]]:
    """Run `_wire_youtube` over a fresh in-memory DB with the given upstream."""
    db = await Database.initialize(backend=Config(database=":memory:"))
    await create_youtube_schema(db)
    app = Starlette()
    app.state.logger = structlog.stdlib.get_logger("test.youtube_boot")
    app.state.telemetry = types.SimpleNamespace(tracer=trace.get_tracer("test"))
    config = AppConfig(
        app_password="test-app-password",
        session_secret="test-session-secret",
        database_path=Path(":memory:"),
        youtube_api=api,
    )
    tasks = await asyncio.wait_for(
        _wire_youtube(app, config=config, database=db, event_publisher=EventHub()),
        timeout=1.0,
    )
    yield app, tasks
    for task in tasks:
        _ = task.cancel()
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
    app, _tasks = await load_fixture(wired_app(api))

    assert_false(app.state.youtube_boot_done.is_set())

    # Releasing the upstream lets the deferred boot pass run to completion.
    release.set()
    await asyncio.wait_for(app.state.youtube_boot_done.wait(), timeout=1.0)
    assert_true(app.state.youtube_boot_done.is_set())
