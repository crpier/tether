"""Tests for `YouTubeService.search`'s semantic path.

When a `TranscriptSearchService` is wired, `search` ranks videos by the injected
match order, attaches each match's snippet, and drops matches whose video has
since been ignored (index drift the next reconcile would clean up). With no
searcher it falls back to the lexical `LIKE` query. These drive the service with
a stub searcher and a real in-memory database, so no LanceDB or model is needed.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from dataclasses import dataclass

import structlog
from opentelemetry import trace
from snekql.sqlite import Config, CurrentTimestamp, Database, insert, update
from snektest import assert_eq, fixture, load_fixture, test

from tether.logging import Logger
from tether.transcript_search import VideoMatch
from tether.youtube import (
    DailyQuota,
    IngestedVideo,
    InMemoryYouTubeApi,
    YouTubeApiClient,
    YouTubeService,
    create_youtube_schema,
)


def _logger() -> Logger:
    return structlog.stdlib.get_logger("test.youtube_semantic_search")


class StubTranscriptSearch:
    """Returns canned video matches, recording the query and limit it saw."""

    def __init__(self, matches: list[VideoMatch]) -> None:
        self._matches: list[VideoMatch] = matches
        self.seen_query: str | None = None
        self.seen_limit: int | None = None

    async def candidates(
        self, query: str, *, limit: int, logger: Logger
    ) -> list[VideoMatch]:
        _ = logger
        self.seen_query = query
        self.seen_limit = limit
        return self._matches


@dataclass
class Env:
    db: Database
    client: YouTubeApiClient


@fixture
async def make_env() -> AsyncGenerator[Env]:
    db = await Database.initialize(backend=Config(database=":memory:"))
    await create_youtube_schema(db)
    client = YouTubeApiClient(InMemoryYouTubeApi(), DailyQuota(db, limit=1000))
    yield Env(db=db, client=client)
    await db.close()


def _tracer() -> trace.Tracer:
    return trace.NoOpTracerProvider().get_tracer("test.youtube_semantic_search")


async def _add_video(db: Database, video_id: str, *, ignored: bool = False) -> None:
    async with db.transaction() as tx:
        created = await tx.execute(
            insert(
                IngestedVideo(
                    video_id=video_id,
                    source="liked",
                    title=f"title {video_id}",
                    channel="chan",
                    topic="topic",
                    description="desc",
                    transcript="body",
                )
            ).returning()
        )
        if ignored:
            _ = await tx.execute(
                update(IngestedVideo)
                .set(IngestedVideo.ignored_at.to(CurrentTimestamp))
                .where(IngestedVideo.id.eq(created.id))
            )


@test()
async def semantic_search_orders_by_match_and_attaches_snippets() -> None:
    env = await load_fixture(make_env())
    await _add_video(env.db, "vid1")
    await _add_video(env.db, "vid2")
    searcher = StubTranscriptSearch(
        [
            VideoMatch(video_id="vid2", snippet="strong hit", score=0.9),
            VideoMatch(video_id="vid1", snippet="weaker hit", score=0.4),
        ]
    )
    service = YouTubeService(database=env.db, client=env.client, tracer=_tracer())
    service.transcript_search = searcher  # pyright: ignore[reportAttributeAccessIssue]

    result = await service.search("android", limit=10, logger=_logger())

    assert_eq([v.video_id for v in result.videos], ["vid2", "vid1"])
    assert_eq(result.snippets, {"vid2": "strong hit", "vid1": "weaker hit"})
    assert_eq(searcher.seen_query, "android")
    assert_eq(searcher.seen_limit, 10)


@test()
async def semantic_search_drops_matches_for_ignored_videos() -> None:
    env = await load_fixture(make_env())
    await _add_video(env.db, "vid1")
    await _add_video(env.db, "gone", ignored=True)
    searcher = StubTranscriptSearch(
        [
            VideoMatch(video_id="gone", snippet="ignored", score=0.9),
            VideoMatch(video_id="vid1", snippet="kept", score=0.4),
        ]
    )
    service = YouTubeService(database=env.db, client=env.client, tracer=_tracer())
    service.transcript_search = searcher  # pyright: ignore[reportAttributeAccessIssue]

    result = await service.search("android", limit=10, logger=_logger())

    assert_eq([v.video_id for v in result.videos], ["vid1"])
    assert_eq(result.snippets, {"vid1": "kept"})


@test()
async def no_matches_returns_an_empty_result() -> None:
    env = await load_fixture(make_env())
    await _add_video(env.db, "vid1")
    service = YouTubeService(database=env.db, client=env.client, tracer=_tracer())
    service.transcript_search = StubTranscriptSearch([])  # pyright: ignore[reportAttributeAccessIssue]

    result = await service.search("nothing", limit=10, logger=_logger())

    assert_eq(result.videos, [])
    assert_eq(result.snippets, {})
