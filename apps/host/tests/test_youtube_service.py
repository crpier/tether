"""Behaviour tests for the YouTube ingestion service layer.

These drive `YouTubeService` directly against a real in-memory SQLite database
and an in-memory `YouTubeApi` (`InMemoryYouTubeApi`) — never a live YouTube call.
The fake counts its calls so we can assert ingestion stays within quota and that
caching elides repeats.
"""

from collections.abc import AsyncGenerator

import structlog
from opentelemetry import trace
from opentelemetry.trace import Tracer
from snekql.sqlite import Config, Database
from snektest import (
    assert_eq,
    assert_in,
    assert_is_none,
    assert_is_not_none,
    assert_not_in,
    assert_raises,
    fixture,
    load_fixture,
    test,
)

from tether.logging import Logger
from tether.youtube import (
    CacheMeta,
    EmptyYouTubeSearchQueryError,
    InMemoryYouTubeApi,
    QuotaMeta,
    RawYouTubeVideo,
    TranscriptUnavailableError,
    YouTubeApiClient,
    YouTubeQuotaExceededError,
    YouTubeService,
    YouTubeVideoNotFoundError,
    create_youtube_schema,
    derive_ingest_state,
)


def noop_tracer() -> Tracer:
    """A tracer that emits nowhere, for tests that don't assert on spans."""
    return trace.NoOpTracerProvider().get_tracer("test.youtube_service")


def test_logger() -> Logger:
    """A throwaway structured logger for the mandatory service logger arg."""
    return structlog.stdlib.get_logger("test.youtube_service")


def video(
    video_id: str,
    *,
    title: str = "A Talk",
    channel: str = "PyConf",
    topic: str = "python",
    description: str = "",
) -> RawYouTubeVideo:
    """Build a raw upstream video with sensible defaults."""
    return RawYouTubeVideo(
        video_id=video_id,
        title=title,
        channel=channel,
        topic=topic,
        description=description,
    )


@fixture
async def make_service(
    api: InMemoryYouTubeApi,
    *,
    quota_limit: int = 1000,
) -> AsyncGenerator[YouTubeService]:
    """A fresh DB + guarded client wrapping the given in-memory API."""
    db = await Database.initialize(backend=Config(database=":memory:"))
    await create_youtube_schema(db)
    client = YouTubeApiClient(api, quota_limit=quota_limit)
    yield YouTubeService(database=db, client=client, tracer=noop_tracer())
    await db.close()


# --- Browse: liked + watch-later, topic-filtered ---


@test()
async def browse_returns_liked_and_watch_later() -> None:
    """Browsing surfaces videos from both saved lists."""
    api = InMemoryYouTubeApi(
        liked=[video("v1", title="Liked one")],
        watch_later=[video("v2", title="Later one")],
    )
    service = await load_fixture(make_service(api))

    result = await service.browse(logger=test_logger())

    found = {row.video_id for row in result.videos}
    assert_in("v1", found)
    assert_in("v2", found)


@test()
async def browse_filters_by_topic() -> None:
    """A topic filter narrows browse to videos under that topic."""
    api = InMemoryYouTubeApi(
        liked=[
            video("v1", topic="python"),
            video("v2", topic="rust"),
        ]
    )
    service = await load_fixture(make_service(api))

    result = await service.browse(topic="python", logger=test_logger())

    found = {row.video_id for row in result.videos}
    assert_in("v1", found)
    assert_not_in("v2", found)


@test()
async def browse_topic_filter_is_case_insensitive() -> None:
    """Topic filtering matches regardless of case."""
    api = InMemoryYouTubeApi(liked=[video("v1", topic="Python")])
    service = await load_fixture(make_service(api))

    result = await service.browse(topic="python", logger=test_logger())

    assert_in("v1", {row.video_id for row in result.videos})


@test()
async def browse_filters_by_source() -> None:
    """A source filter restricts browse to one saved list and only pulls it."""
    api = InMemoryYouTubeApi(
        liked=[video("v1")],
        watch_later=[video("v2")],
    )
    service = await load_fixture(make_service(api))

    result = await service.browse(source="watch_later", logger=test_logger())

    found = {row.video_id for row in result.videos}
    assert_in("v2", found)
    assert_not_in("v1", found)


@test()
async def repeated_browse_is_served_from_cache() -> None:
    """A second browse spends no further quota and reports a cache hit."""
    api = InMemoryYouTubeApi(liked=[video("v1")], watch_later=[video("v2")])
    service = await load_fixture(make_service(api))

    first = await service.browse(logger=test_logger())
    second = await service.browse(logger=test_logger())

    assert_eq(first.cache.hit, False)
    assert_eq(second.cache.hit, True)
    # Two sources pulled live exactly once each on the first browse.
    assert_eq(api.list_calls, 2)


@test()
async def browse_reports_quota_metadata() -> None:
    """Browse exposes the remaining quota budget after its live calls."""
    api = InMemoryYouTubeApi(liked=[video("v1")], watch_later=[video("v2")])
    service = await load_fixture(make_service(api, quota_limit=10))

    result = await service.browse(logger=test_logger())

    assert isinstance(result.quota, QuotaMeta)
    assert_eq(result.quota.limit, 10)
    assert_eq(result.quota.used, 2)
    assert_eq(result.quota.remaining, 8)


@test()
async def browse_raises_when_quota_is_exhausted() -> None:
    """A depleted budget guards the upstream API rather than calling it."""
    api = InMemoryYouTubeApi(liked=[video("v1")], watch_later=[video("v2")])
    service = await load_fixture(make_service(api, quota_limit=1))

    with assert_raises(YouTubeQuotaExceededError):
        _ = await service.browse(logger=test_logger())

    # The second source list was never pulled.
    assert_eq(api.list_calls, 1)


# --- Ignore / purge with retry ---


@test()
async def ignored_video_drops_out_of_browse() -> None:
    """Purging a video removes it from browse results."""
    api = InMemoryYouTubeApi(liked=[video("v1"), video("v2")])
    service = await load_fixture(make_service(api))
    _ = await service.browse(logger=test_logger())

    _ = await service.ignore("v1", logger=test_logger())
    result = await service.browse(logger=test_logger())

    assert_not_in("v1", {row.video_id for row in result.videos})
    assert_in("v2", {row.video_id for row in result.videos})


@test()
async def ignore_marks_the_video_ignored() -> None:
    """Ignoring stamps the ignore state on the row."""
    api = InMemoryYouTubeApi(liked=[video("v1")])
    service = await load_fixture(make_service(api))
    _ = await service.browse(logger=test_logger())

    ignored = await service.ignore("v1", logger=test_logger())

    assert_eq(derive_ingest_state(ignored), "ignored")
    _ = assert_is_not_none(ignored.ignored_at)


@test()
async def retry_returns_an_ignored_video_to_ingestion() -> None:
    """Retry un-ignores a purged video so browse surfaces it again."""
    api = InMemoryYouTubeApi(liked=[video("v1")])
    service = await load_fixture(make_service(api))
    _ = await service.browse(logger=test_logger())
    _ = await service.ignore("v1", logger=test_logger())

    retried = await service.retry("v1", logger=test_logger())
    result = await service.browse(logger=test_logger())

    assert_eq(derive_ingest_state(retried), "active")
    assert_is_none(retried.ignored_at)
    assert_in("v1", {row.video_id for row in result.videos})


@test()
async def re_browsing_does_not_resurrect_an_ignored_video() -> None:
    """A still-upstream ignored video stays purged across re-ingestion."""
    api = InMemoryYouTubeApi(liked=[video("v1")])
    service = await load_fixture(make_service(api))
    _ = await service.browse(logger=test_logger())
    _ = await service.ignore("v1", logger=test_logger())

    # A browse forced past the cache would still re-mirror v1; it must stay ignored.
    result = await service.browse(logger=test_logger())

    assert_not_in("v1", {row.video_id for row in result.videos})


@test()
async def ignoring_an_unknown_video_raises() -> None:
    """Purging a video that was never ingested is a not-found error."""
    api = InMemoryYouTubeApi()
    service = await load_fixture(make_service(api))

    with assert_raises(YouTubeVideoNotFoundError):
        _ = await service.ignore("nope", logger=test_logger())


# --- Fetch transcript ---


@test()
async def fetch_transcript_returns_and_persists_the_text() -> None:
    """Fetching a transcript returns the text and stores it on the row."""
    api = InMemoryYouTubeApi(
        liked=[video("v1")], transcripts={"v1": "the transcript body"}
    )
    service = await load_fixture(make_service(api))
    _ = await service.browse(logger=test_logger())

    result = await service.fetch_transcript("v1", logger=test_logger())

    assert_eq(result.transcript, "the transcript body")
    assert_eq(result.video.transcript, "the transcript body")


@test()
async def fetch_transcript_is_cached_on_repeat() -> None:
    """A second fetch is served from cache without another live call."""
    api = InMemoryYouTubeApi(liked=[video("v1")], transcripts={"v1": "body"})
    service = await load_fixture(make_service(api))
    _ = await service.browse(logger=test_logger())

    first = await service.fetch_transcript("v1", logger=test_logger())
    second = await service.fetch_transcript("v1", logger=test_logger())

    assert_eq(first.cache.hit, False)
    assert_eq(second.cache.hit, True)
    assert_eq(api.transcript_calls, 1)


@test()
async def fetch_transcript_for_unknown_video_raises() -> None:
    """A transcript fetch for a non-ingested video is a not-found error."""
    api = InMemoryYouTubeApi(transcripts={"v1": "body"})
    service = await load_fixture(make_service(api))

    with assert_raises(YouTubeVideoNotFoundError):
        _ = await service.fetch_transcript("v1", logger=test_logger())


@test()
async def fetch_transcript_unavailable_raises_without_spending_repeatedly() -> None:
    """A video with no upstream transcript surfaces as unavailable."""
    api = InMemoryYouTubeApi(liked=[video("v1")])
    service = await load_fixture(make_service(api))
    _ = await service.browse(logger=test_logger())

    with assert_raises(TranscriptUnavailableError):
        _ = await service.fetch_transcript("v1", logger=test_logger())


# --- Search across saved content + transcript text ---


@test()
async def search_matches_saved_title() -> None:
    """Search matches against the saved video title."""
    api = InMemoryYouTubeApi(liked=[video("v1", title="Async Python deep dive")])
    service = await load_fixture(make_service(api))

    result = await service.search("async", logger=test_logger())

    assert_in("v1", {row.video_id for row in result.videos})


@test()
async def search_matches_saved_description() -> None:
    """Search matches against the saved description text."""
    api = InMemoryYouTubeApi(
        liked=[video("v1", title="Talk", description="covers asyncio internals")]
    )
    service = await load_fixture(make_service(api))

    result = await service.search("asyncio", logger=test_logger())

    assert_in("v1", {row.video_id for row in result.videos})


@test()
async def search_matches_fetched_transcript_text() -> None:
    """Once fetched, transcript text is searchable."""
    api = InMemoryYouTubeApi(
        liked=[video("v1", title="Talk", description="")],
        transcripts={"v1": "today we discuss coroutines at length"},
    )
    service = await load_fixture(make_service(api))
    _ = await service.browse(logger=test_logger())
    _ = await service.fetch_transcript("v1", logger=test_logger())

    result = await service.search("coroutines", logger=test_logger())

    assert_in("v1", {row.video_id for row in result.videos})


@test()
async def search_does_not_match_unfetched_transcript() -> None:
    """A term only in a not-yet-fetched transcript does not match."""
    api = InMemoryYouTubeApi(
        liked=[video("v1", title="Talk", description="")],
        transcripts={"v1": "coroutines"},
    )
    service = await load_fixture(make_service(api))

    result = await service.search("coroutines", logger=test_logger())

    assert_not_in("v1", {row.video_id for row in result.videos})


@test()
async def search_excludes_ignored_videos() -> None:
    """A purged video drops out of Search."""
    api = InMemoryYouTubeApi(liked=[video("v1", title="needle one")])
    service = await load_fixture(make_service(api))
    _ = await service.browse(logger=test_logger())
    _ = await service.ignore("v1", logger=test_logger())

    result = await service.search("needle", logger=test_logger())

    assert_not_in("v1", {row.video_id for row in result.videos})


@test()
async def search_ands_terms_together() -> None:
    """Search includes only videos containing every query term."""
    api = InMemoryYouTubeApi(
        liked=[
            video("v1", title="Async Python patterns"),
            video("v2", title="Async Rust patterns"),
        ]
    )
    service = await load_fixture(make_service(api))

    result = await service.search("async python", logger=test_logger())

    found = {row.video_id for row in result.videos}
    assert_in("v1", found)
    assert_not_in("v2", found)


@test()
async def search_rejects_a_blank_query() -> None:
    """A blank Search query is rejected rather than listing everything."""
    api = InMemoryYouTubeApi(liked=[video("v1")])
    service = await load_fixture(make_service(api))

    with assert_raises(EmptyYouTubeSearchQueryError):
        _ = await service.search("   ", logger=test_logger())


@test()
async def cache_metadata_is_well_formed() -> None:
    """Browse cache metadata is a structured CacheMeta value."""
    api = InMemoryYouTubeApi(liked=[video("v1")])
    service = await load_fixture(make_service(api))

    result = await service.browse(logger=test_logger())

    assert isinstance(result.cache, CacheMeta)
    assert_eq(result.cache.source, "live")
