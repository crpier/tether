"""Behaviour tests for the YouTube ingestion service layer.

These drive `YouTubeService`, `YouTubeSyncService`, and `DailyQuota` directly
against a real in-memory SQLite database and a paginated in-memory `YouTubeApi`
(`InMemoryYouTubeApi`) — never a live YouTube call. The fake counts its calls so
we can assert browse/search stay local and the sync stays within budget.
"""

from collections.abc import AsyncGenerator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, timezone

import structlog
from opentelemetry import trace
from opentelemetry.trace import Tracer
from snekql.sqlite import Config, Database, Fetched, Pending, insert, select
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
    _LIKES_LAST_RUN_KEY,
    DailyQuota,
    EmptyYouTubeSearchQueryError,
    IngestedVideo,
    InMemoryYouTubeApi,
    LikedPage,
    RawYouTubeVideo,
    TranscriptUnavailableError,
    YouTubeApi,
    YouTubeApiClient,
    YouTubeApiGate,
    YouTubeApiGateConfig,
    YouTubeQuotaExceededError,
    YouTubeService,
    YouTubeSyncConfig,
    YouTubeSyncService,
    YouTubeTranscriptState,
    YouTubeVideoNotFoundError,
    _state_get,
    create_youtube_schema,
    derive_ingest_state,
)


def noop_tracer() -> Tracer:
    """A tracer that emits nowhere, for tests that don't assert on spans."""
    return trace.NoOpTracerProvider().get_tracer("test.youtube_service")


def test_logger() -> Logger:
    """A throwaway structured logger for the mandatory service logger arg."""
    return structlog.stdlib.get_logger("test.youtube_service")


class FakeClock:
    """A controllable clock so quota-rollover tests need not wait for midnight."""

    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        self._now += delta


def video(
    video_id: str,
    *,
    title: str = "A Talk",
    topic: str = "python",
    description: str = "",
    liked_at: datetime | None = None,
) -> RawYouTubeVideo:
    """Build a raw upstream video with sensible defaults."""
    return RawYouTubeVideo(
        video_id=video_id,
        title=title,
        channel="PyConf",
        topic=topic,
        description=description,
        liked_at=liked_at,
    )


@dataclass
class Env:
    """The wired ingestion surfaces sharing one database and one client."""

    service: YouTubeService
    sync: YouTubeSyncService
    db: Database
    quota: DailyQuota
    client: YouTubeApiClient
    api: InMemoryYouTubeApi


@fixture
async def make_env(
    api: InMemoryYouTubeApi,
    *,
    daily_limit: int = 1000,
    clock: FakeClock | None = None,
    config: YouTubeSyncConfig | None = None,
) -> AsyncGenerator[Env]:
    """A fresh DB plus the service + sync wired over a shared budgeted client."""
    db = await Database.initialize(backend=Config(database=":memory:"))
    await create_youtube_schema(db)
    quota = DailyQuota(db, limit=daily_limit)
    client = YouTubeApiClient(api, quota, clock=clock)
    service = YouTubeService(
        database=db, client=client, provider=api, tracer=noop_tracer()
    )
    sync = YouTubeSyncService(
        database=db, client=client, tracer=noop_tracer(), config=config
    )
    yield Env(service=service, sync=sync, db=db, quota=quota, client=client, api=api)
    await db.close()


# --- Browse reads only the local corpus ---


@test()
async def browse_is_empty_until_a_sync_runs() -> None:
    """Browse reads only local state, so it never calls upstream itself."""
    api = InMemoryYouTubeApi(liked=[video("v1")])
    env = await load_fixture(make_env(api))

    result = await env.service.browse(logger=test_logger())

    assert_eq(result.videos, [])
    # No browse ever touches the upstream list.
    assert_eq(api.list_calls, 0)


@test()
async def sync_then_browse_surfaces_liked_videos() -> None:
    """A sync mirrors liked videos into the corpus that browse then reads."""
    api = InMemoryYouTubeApi(liked=[video("v1"), video("v2")])
    env = await load_fixture(make_env(api))

    _ = await env.sync.sync(logger=test_logger())
    result = await env.service.browse(logger=test_logger())

    found = {row.video_id for row in result.videos}
    assert_in("v1", found)
    assert_in("v2", found)


@test()
async def browse_filters_by_topic() -> None:
    """A topic filter narrows browse to videos under that topic."""
    api = InMemoryYouTubeApi(
        liked=[video("v1", topic="python"), video("v2", topic="rust")]
    )
    env = await load_fixture(make_env(api))
    _ = await env.sync.sync(logger=test_logger())

    result = await env.service.browse(topic="python", logger=test_logger())

    found = {row.video_id for row in result.videos}
    assert_in("v1", found)
    assert_not_in("v2", found)


@test()
async def browse_topic_filter_is_case_insensitive() -> None:
    """Topic filtering matches regardless of case."""
    api = InMemoryYouTubeApi(liked=[video("v1", topic="Python")])
    env = await load_fixture(make_env(api))
    _ = await env.sync.sync(logger=test_logger())

    result = await env.service.browse(topic="python", logger=test_logger())

    assert_in("v1", {row.video_id for row in result.videos})


@test()
async def browse_reports_the_days_quota_snapshot() -> None:
    """Browse exposes the day's persisted budget, not a per-call spend."""
    api = InMemoryYouTubeApi(liked=[video("v1")])
    env = await load_fixture(make_env(api, daily_limit=10))
    _ = await env.sync.sync(logger=test_logger())

    result = await env.service.browse(logger=test_logger())

    assert_eq(result.quota.limit, 10)
    # One hot page: one list unit + one metadata unit.
    assert_eq(result.quota.used, 2)
    assert_eq(result.quota.remaining, 8)
    assert_eq(result.cache.source, "cache")


@test()
async def browse_orders_by_liked_at_then_falls_back_to_created_at() -> None:
    """Browse is newest-liked-first; null-liked rows fall back to created-at."""
    env = await load_fixture(make_env(InMemoryYouTubeApi()))

    async with env.db.transaction() as tx:
        # A liked row (old created_at), and two null-liked rows whose only
        # ordering signal is created_at — the newest-created sorts first.
        _ = await tx.execute(
            insert(
                IngestedVideo(
                    video_id="liked",
                    source="liked",
                    title="t",
                    channel="c",
                    topic="python",
                    description="",
                    liked_at=datetime(2026, 6, 1, tzinfo=UTC),
                    created_at=datetime(2020, 1, 1, tzinfo=UTC),
                )
            )
        )
        _ = await tx.execute(
            insert(
                IngestedVideo(
                    video_id="null_new",
                    source="liked",
                    title="t",
                    channel="c",
                    topic="python",
                    description="",
                    created_at=datetime(2025, 6, 2, tzinfo=UTC),
                )
            )
        )
        _ = await tx.execute(
            insert(
                IngestedVideo(
                    video_id="null_old",
                    source="liked",
                    title="t",
                    channel="c",
                    topic="python",
                    description="",
                    created_at=datetime(2025, 6, 1, tzinfo=UTC),
                )
            )
        )

    order = [
        r.video_id for r in (await env.service.browse(logger=test_logger())).videos
    ]

    assert_eq(order, ["liked", "null_new", "null_old"])


# --- Sync: pagination, backfill cursor, cutoff ---


@test()
async def sync_pages_and_backfills_across_runs() -> None:
    """Hot pages plus an advancing backfill cursor cover history over passes."""
    api = InMemoryYouTubeApi(liked=[video(f"v{i}") for i in range(1, 6)])
    config = YouTubeSyncConfig(hot_pages=1, backfill_pages=1, page_size=2)
    env = await load_fixture(make_env(api, config=config))

    _ = await env.sync.sync(logger=test_logger())
    after_first = {
        row.video_id for row in (await env.service.browse(logger=test_logger())).videos
    }
    _ = await env.sync.sync(logger=test_logger())
    after_second = {
        row.video_id for row in (await env.service.browse(logger=test_logger())).videos
    }

    # First pass: hot page (v1,v2) + one backfill page (v3,v4).
    assert_eq(after_first, {"v1", "v2", "v3", "v4"})
    # Second pass resumes the cursor and reaches the tail.
    assert_eq(after_second, {"v1", "v2", "v3", "v4", "v5"})


@test()
async def backfill_cursor_resumes_after_a_restart() -> None:
    """The persisted cursor lets a fresh sync instance resume the backfill."""
    api = InMemoryYouTubeApi(liked=[video(f"v{i}") for i in range(1, 6)])
    config = YouTubeSyncConfig(hot_pages=1, backfill_pages=1, page_size=2)
    env = await load_fixture(make_env(api, config=config))
    _ = await env.sync.sync(logger=test_logger())

    # A new sync instance over the same database stands in for a restart.
    restarted = YouTubeSyncService(
        database=env.db, client=env.client, tracer=noop_tracer(), config=config
    )
    _ = await restarted.sync(logger=test_logger())

    found = {
        row.video_id for row in (await env.service.browse(logger=test_logger())).videos
    }
    assert_in("v5", found)


@test()
async def cutoff_date_stops_the_backfill() -> None:
    """Videos liked before the cutoff are dropped and end the backfill."""
    recent = datetime(2026, 6, 1, tzinfo=UTC)
    old = datetime(2024, 1, 1, tzinfo=UTC)
    api = InMemoryYouTubeApi(
        liked=[
            video("v1", liked_at=recent),
            video("v2", liked_at=recent),
            video("v3", liked_at=old),
        ]
    )
    config = YouTubeSyncConfig(
        hot_pages=1, backfill_pages=2, page_size=2, cutoff_date=date(2025, 1, 1)
    )
    env = await load_fixture(make_env(api, config=config))

    _ = await env.sync.sync(logger=test_logger())

    found = {
        row.video_id for row in (await env.service.browse(logger=test_logger())).videos
    }
    assert_in("v1", found)
    assert_in("v2", found)
    assert_not_in("v3", found)


@test()
async def cutoff_compares_liked_at_in_utc() -> None:
    """A non-UTC liked_at is normalised to UTC before the cutoff comparison."""
    # 00:30 at +02:00 is 22:30 the *previous* UTC day, which falls before the
    # cutoff; a naive `.date()` would read 2025-01-01 and wrongly keep it.
    plus_two = timezone(timedelta(hours=2))
    api = InMemoryYouTubeApi(
        liked=[video("v1", liked_at=datetime(2025, 1, 1, 0, 30, tzinfo=plus_two))]
    )
    config = YouTubeSyncConfig(
        hot_pages=1, backfill_pages=0, page_size=10, cutoff_date=date(2025, 1, 1)
    )
    env = await load_fixture(make_env(api, config=config))

    _ = await env.sync.sync(logger=test_logger())

    found = {
        row.video_id for row in (await env.service.browse(logger=test_logger())).videos
    }
    assert_not_in("v1", found)


@test()
async def members_only_videos_are_skipped_during_sync() -> None:
    """A liked video with no fetchable metadata (members-only) is not ingested."""
    api = InMemoryYouTubeApi(
        liked=[video("v1"), video("members")], unavailable=["members"]
    )
    env = await load_fixture(make_env(api))

    _ = await env.sync.sync(logger=test_logger())

    found = {
        row.video_id for row in (await env.service.browse(logger=test_logger())).videos
    }
    assert_in("v1", found)
    assert_not_in("members", found)


@test()
async def sync_marks_last_run_from_the_injected_clock() -> None:
    """The last-run timestamp is sourced from the injected clock, not wall time."""
    clock = FakeClock(datetime(2026, 6, 1, 12, 0, tzinfo=UTC))
    api = InMemoryYouTubeApi(liked=[video("v1")])
    env = await load_fixture(make_env(api, clock=clock))

    _ = await env.sync.sync(logger=test_logger())

    last_run = await _state_get(env.db, _LIKES_LAST_RUN_KEY)
    assert_eq(last_run, clock.now().isoformat())


@test()
async def maybe_sync_runs_when_no_prior_run() -> None:
    """With a gate configured but no last-run stamped, the first pass runs."""
    clock = FakeClock(datetime(2026, 6, 1, 12, 0, tzinfo=UTC))
    api = InMemoryYouTubeApi(liked=[video("v1")])
    config = YouTubeSyncConfig(min_interval=timedelta(seconds=300))
    env = await load_fixture(make_env(api, clock=clock, config=config))

    report = await env.sync.maybe_sync(logger=test_logger())

    assert_is_not_none(report)
    assert_eq(api.list_calls, 1)


@test()
async def maybe_sync_skips_within_min_interval() -> None:
    """A restart inside the gate window does not call upstream again.

    This is the dev-loop protection: repeatedly booting the host must not burn
    quota when the last sync (by this or a prior process) is recent.
    """
    clock = FakeClock(datetime(2026, 6, 1, 12, 0, tzinfo=UTC))
    api = InMemoryYouTubeApi(liked=[video("v1")])
    config = YouTubeSyncConfig(min_interval=timedelta(seconds=300))
    env = await load_fixture(make_env(api, clock=clock, config=config))
    _ = await env.sync.sync(logger=test_logger())
    calls_after_first = api.list_calls

    clock.advance(timedelta(seconds=120))
    report = await env.sync.maybe_sync(logger=test_logger())

    assert_is_none(report)
    assert_eq(api.list_calls, calls_after_first)


@test()
async def maybe_sync_runs_after_min_interval_elapsed() -> None:
    """Once the gate window has passed, the next pass runs again."""
    clock = FakeClock(datetime(2026, 6, 1, 12, 0, tzinfo=UTC))
    api = InMemoryYouTubeApi(liked=[video("v1")])
    config = YouTubeSyncConfig(min_interval=timedelta(seconds=300))
    env = await load_fixture(make_env(api, clock=clock, config=config))
    _ = await env.sync.sync(logger=test_logger())
    calls_after_first = api.list_calls

    clock.advance(timedelta(seconds=301))
    report = await env.sync.maybe_sync(logger=test_logger())

    assert_is_not_none(report)
    assert_eq(api.list_calls, calls_after_first + 1)


@test()
async def maybe_sync_always_runs_without_a_gate() -> None:
    """With no `min_interval` configured, the gate is off and every pass runs."""
    clock = FakeClock(datetime(2026, 6, 1, 12, 0, tzinfo=UTC))
    api = InMemoryYouTubeApi(liked=[video("v1")])
    env = await load_fixture(make_env(api, clock=clock))
    _ = await env.sync.sync(logger=test_logger())
    calls_after_first = api.list_calls

    report = await env.sync.maybe_sync(logger=test_logger())

    assert_is_not_none(report)
    assert_eq(api.list_calls, calls_after_first + 1)


@test()
async def quota_exhaustion_during_enrich_does_not_count_pulled() -> None:
    """A page whose metadata fetch is blocked by quota is not counted as pulled."""
    clock = FakeClock(datetime(2026, 6, 1, tzinfo=UTC))
    api = InMemoryYouTubeApi(liked=[video("v1"), video("v2")])
    # One unit covers the list call; the enrich call then exceeds the budget.
    config = YouTubeSyncConfig(hot_pages=1, backfill_pages=0, page_size=2)
    env = await load_fixture(make_env(api, daily_limit=1, clock=clock, config=config))

    report = await env.sync.sync(logger=test_logger())

    assert_eq(report.pulled, 0)
    assert_eq(report.upserted, 0)


@test()
async def sync_preserves_enriched_metadata() -> None:
    """The detail fetch's enriched fields round-trip onto the ingested row."""
    liked_at = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    raw = RawYouTubeVideo(
        video_id="v1",
        title="Async IO",
        channel="PyConf",
        topic="python",
        channel_id="UC123",
        liked_at=liked_at,
        duration_seconds=600,
        caption_available=True,
        topic_categories=("python", "async"),
    )
    env = await load_fixture(make_env(InMemoryYouTubeApi(liked=[raw])))
    _ = await env.sync.sync(logger=test_logger())

    stored = await env.service.get_video("v1")

    assert_eq(stored.channel_id, "UC123")
    assert_eq(stored.duration_seconds, 600)
    assert_eq(stored.caption_available, 1)
    assert_is_not_none(stored.liked_at)


# --- Ignore / retry survive re-sync ---


@test()
async def ignored_video_drops_out_of_browse_and_stays_ignored() -> None:
    """Purging removes a video from browse; a later sync keeps it ignored."""
    api = InMemoryYouTubeApi(liked=[video("v1"), video("v2")])
    env = await load_fixture(make_env(api))
    _ = await env.sync.sync(logger=test_logger())

    _ = await env.service.ignore("v1", logger=test_logger())
    _ = await env.sync.sync(logger=test_logger())
    result = await env.service.browse(logger=test_logger())

    assert_not_in("v1", {row.video_id for row in result.videos})
    assert_in("v2", {row.video_id for row in result.videos})


@test()
async def retry_returns_an_ignored_video_to_ingestion() -> None:
    """Retry un-ignores a purged video so browse surfaces it again."""
    api = InMemoryYouTubeApi(liked=[video("v1")])
    env = await load_fixture(make_env(api))
    _ = await env.sync.sync(logger=test_logger())
    _ = await env.service.ignore("v1", logger=test_logger())

    retried = await env.service.retry("v1", logger=test_logger())
    result = await env.service.browse(logger=test_logger())

    assert_eq(derive_ingest_state(retried), "active")
    assert_is_none(retried.ignored_at)
    assert_in("v1", {row.video_id for row in result.videos})


@test()
async def ignoring_an_unknown_video_raises() -> None:
    """Purging a video that was never ingested is a not-found error."""
    env = await load_fixture(make_env(InMemoryYouTubeApi()))

    with assert_raises(YouTubeVideoNotFoundError):
        _ = await env.service.ignore("nope", logger=test_logger())


# --- Transcript fetch (still upstream, budget-guarded) ---


@test()
async def fetch_transcript_returns_and_persists_the_text() -> None:
    """Fetching a transcript returns the text and stores it on the row."""
    api = InMemoryYouTubeApi(
        liked=[video("v1")], transcripts={"v1": "the transcript body"}
    )
    env = await load_fixture(make_env(api))
    _ = await env.sync.sync(logger=test_logger())

    result = await env.service.fetch_transcript("v1", logger=test_logger())

    assert_eq(result.transcript, "the transcript body")
    assert_eq(result.video.transcript, "the transcript body")


@test()
async def fetch_transcript_is_served_from_the_row_on_repeat() -> None:
    """A stored transcript short-circuits with no further upstream call."""
    api = InMemoryYouTubeApi(liked=[video("v1")], transcripts={"v1": "body"})
    env = await load_fixture(make_env(api))
    _ = await env.sync.sync(logger=test_logger())

    first = await env.service.fetch_transcript("v1", logger=test_logger())
    second = await env.service.fetch_transcript("v1", logger=test_logger())

    assert_eq(first.cache.hit, False)
    assert_eq(second.cache.hit, True)
    assert_eq(api.transcript_calls, 1)


@test()
async def transcript_survives_a_re_sync() -> None:
    """Re-ingesting a video never drops its locally fetched transcript."""
    api = InMemoryYouTubeApi(liked=[video("v1")], transcripts={"v1": "body"})
    env = await load_fixture(make_env(api))
    _ = await env.sync.sync(logger=test_logger())
    _ = await env.service.fetch_transcript("v1", logger=test_logger())

    _ = await env.sync.sync(logger=test_logger())
    stored = await env.service.get_video("v1")

    assert_eq(stored.transcript, "body")


@test()
async def fetch_transcript_for_unknown_video_raises() -> None:
    """A transcript fetch for a non-ingested video is a not-found error."""
    env = await load_fixture(make_env(InMemoryYouTubeApi(transcripts={"v1": "body"})))

    with assert_raises(YouTubeVideoNotFoundError):
        _ = await env.service.fetch_transcript("v1", logger=test_logger())


@test()
async def fetch_transcript_unavailable_raises() -> None:
    """A video with no upstream transcript surfaces as unavailable."""
    api = InMemoryYouTubeApi(liked=[video("v1")])
    env = await load_fixture(make_env(api))
    _ = await env.sync.sync(logger=test_logger())

    with assert_raises(TranscriptUnavailableError):
        _ = await env.service.fetch_transcript("v1", logger=test_logger())


# --- Search across saved content + transcript text (local only) ---


@test()
async def search_matches_saved_title() -> None:
    """Search matches against the saved video title."""
    api = InMemoryYouTubeApi(liked=[video("v1", title="Async Python deep dive")])
    env = await load_fixture(make_env(api))
    _ = await env.sync.sync(logger=test_logger())

    result = await env.service.search("async", logger=test_logger())

    assert_in("v1", {row.video_id for row in result.videos})


@test()
async def search_matches_fetched_transcript_text() -> None:
    """Once fetched, transcript text is searchable."""
    api = InMemoryYouTubeApi(
        liked=[video("v1", title="Talk")],
        transcripts={"v1": "today we discuss coroutines at length"},
    )
    env = await load_fixture(make_env(api))
    _ = await env.sync.sync(logger=test_logger())
    _ = await env.service.fetch_transcript("v1", logger=test_logger())

    result = await env.service.search("coroutines", logger=test_logger())

    assert_in("v1", {row.video_id for row in result.videos})


@test()
async def search_does_not_match_unfetched_transcript() -> None:
    """A term only in a not-yet-fetched transcript does not match."""
    api = InMemoryYouTubeApi(
        liked=[video("v1", title="Talk")], transcripts={"v1": "coroutines"}
    )
    env = await load_fixture(make_env(api))
    _ = await env.sync.sync(logger=test_logger())

    result = await env.service.search("coroutines", logger=test_logger())

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
    env = await load_fixture(make_env(api))
    _ = await env.sync.sync(logger=test_logger())

    result = await env.service.search("async python", logger=test_logger())

    found = {row.video_id for row in result.videos}
    assert_in("v1", found)
    assert_not_in("v2", found)


@test()
async def search_excludes_ignored_videos() -> None:
    """A purged video drops out of Search."""
    api = InMemoryYouTubeApi(liked=[video("v1", title="needle one")])
    env = await load_fixture(make_env(api))
    _ = await env.sync.sync(logger=test_logger())
    _ = await env.service.ignore("v1", logger=test_logger())

    result = await env.service.search("needle", logger=test_logger())

    assert_not_in("v1", {row.video_id for row in result.videos})


@test()
async def search_rejects_a_blank_query() -> None:
    """A blank Search query is rejected rather than listing everything."""
    env = await load_fixture(make_env(InMemoryYouTubeApi(liked=[video("v1")])))

    with assert_raises(EmptyYouTubeSearchQueryError):
        _ = await env.service.search("   ", logger=test_logger())


# --- DailyQuota: persistence, exhaustion, rollover ---


@test()
async def quota_persists_spend_across_instances() -> None:
    """A fresh DailyQuota over the same database sees prior spend."""
    api = InMemoryYouTubeApi(liked=[video("v1")])
    env = await load_fixture(make_env(api, daily_limit=100))
    now = datetime(2026, 6, 1, tzinfo=UTC)
    await env.quota.spend(7, now=now)

    reopened = DailyQuota(env.db, limit=100)

    assert_eq(await reopened.used(now=now), 7)


@test()
async def quota_raises_before_calling_out_when_exhausted() -> None:
    """A depleted day guards the upstream call rather than overspending."""
    api = InMemoryYouTubeApi(liked=[video("v1")])
    env = await load_fixture(make_env(api, daily_limit=3))
    now = datetime(2026, 6, 1, tzinfo=UTC)
    await env.quota.spend(3, now=now)

    with assert_raises(YouTubeQuotaExceededError):
        await env.quota.spend(1, now=now)


@test()
async def quota_rolls_over_at_the_next_utc_day() -> None:
    """Spend resets on a new UTC day so sync resumes automatically."""
    api = InMemoryYouTubeApi(liked=[video("v1")])
    env = await load_fixture(make_env(api, daily_limit=5))
    day_one = datetime(2026, 6, 1, 23, 0, tzinfo=UTC)
    await env.quota.spend(5, now=day_one)

    day_two = day_one + timedelta(hours=2)

    assert_eq(await env.quota.used(now=day_two), 0)
    await env.quota.spend(5, now=day_two)
    assert_eq(await env.quota.used(now=day_two), 5)


@test()
async def sync_stops_on_quota_exhaustion_without_raising() -> None:
    """An exhausted budget halts the sync gracefully, mirroring partial work."""
    clock = FakeClock(datetime(2026, 6, 1, tzinfo=UTC))
    api = InMemoryYouTubeApi(liked=[video(f"v{i}") for i in range(1, 6)])
    # Budget covers one page (list + metadata) and no more.
    config = YouTubeSyncConfig(hot_pages=1, backfill_pages=2, page_size=2)
    env = await load_fixture(make_env(api, daily_limit=2, clock=clock, config=config))

    report = await env.sync.sync(logger=test_logger())

    # The first page mirrored; the budget then stopped further pulls.
    assert_eq(report.upserted, 2)
    found = {
        row.video_id for row in (await env.service.browse(logger=test_logger())).videos
    }
    assert_eq(found, {"v1", "v2"})


# --- YouTubeApiGate: global Data API backoff ---


_GATE_NOW = datetime(2026, 6, 1, tzinfo=UTC)


def gate_config() -> YouTubeApiGateConfig:
    """A 15-minute base / 6-hour cap gate, matching the production defaults."""
    return YouTubeApiGateConfig(
        pause_base=timedelta(minutes=15), pause_cap=timedelta(hours=6)
    )


async def gate_db() -> Database:
    """A fresh schema-initialised database for a standalone gate."""
    db = await Database.initialize(backend=Config(database=":memory:"))
    await create_youtube_schema(db)
    return db


class QuotaBlockingApi(YouTubeApi):
    """A `YouTubeApi` double that 403s on its first `fail_times` list calls.

    Stands in for the live Data API returning `quotaExceeded` despite local budget,
    so the gate's escalation/reset can be driven through `YouTubeApiClient`.
    """

    def __init__(self, *, fail_times: int) -> None:
        self._remaining: int = fail_times
        self.list_calls: int = 0

    async def list_liked_page(
        self, *, page_token: str | None, page_size: int
    ) -> LikedPage:
        _ = (page_token, page_size)
        self.list_calls += 1
        if self._remaining > 0:
            self._remaining -= 1
            raise YouTubeQuotaExceededError("live 403 quotaExceeded")
        return LikedPage(videos=[], next_page_token=None)

    async def fetch_video_metadata(
        self, video_ids: object
    ) -> dict[str, RawYouTubeVideo]:
        _ = video_ids
        return {}


@test()
async def api_gate_is_open_when_unpaused() -> None:
    """A fresh gate lets calls through without raising."""
    db = await gate_db()
    gate = YouTubeApiGate(db, config=gate_config())

    await gate.ensure_open(now=_GATE_NOW)

    await db.close()


@test()
async def api_gate_pauses_then_reopens_when_cooldown_elapses() -> None:
    """A quota error pauses for the base interval, reopening once it elapses."""
    db = await gate_db()
    gate = YouTubeApiGate(db, config=gate_config())

    paused_until = await gate.record_quota_error(now=_GATE_NOW)

    assert_eq(paused_until, _GATE_NOW + timedelta(minutes=15))
    with assert_raises(YouTubeQuotaExceededError):
        await gate.ensure_open(now=_GATE_NOW + timedelta(minutes=14))
    await gate.ensure_open(now=_GATE_NOW + timedelta(minutes=16))
    await db.close()


@test()
async def api_gate_escalates_exponentially_capped_at_six_hours() -> None:
    """Consecutive quota errors double the cooldown, clamped to six hours."""
    db = await gate_db()
    gate = YouTubeApiGate(db, config=gate_config())

    cooldowns = [
        (await gate.record_quota_error(now=_GATE_NOW)) - _GATE_NOW for _ in range(7)
    ]

    assert_eq(
        cooldowns,
        [
            timedelta(minutes=15),
            timedelta(minutes=30),
            timedelta(minutes=60),
            timedelta(minutes=120),
            timedelta(minutes=240),
            timedelta(hours=6),
            timedelta(hours=6),
        ],
    )
    await db.close()


@test()
async def api_gate_retry_after_hint_floors_the_cooldown() -> None:
    """A provider retry-after longer than the computed cooldown wins."""
    db = await gate_db()
    gate = YouTubeApiGate(db, config=gate_config())

    paused_until = await gate.record_quota_error(
        now=_GATE_NOW, retry_after=timedelta(hours=2)
    )

    assert_eq(paused_until, _GATE_NOW + timedelta(hours=2))
    await db.close()


@test()
async def api_gate_success_clears_pause_and_resets_streak() -> None:
    """A clean call clears the pause, so the next error starts from the base."""
    db = await gate_db()
    gate = YouTubeApiGate(db, config=gate_config())
    _ = await gate.record_quota_error(now=_GATE_NOW)
    _ = await gate.record_quota_error(now=_GATE_NOW)

    await gate.record_success()

    await gate.ensure_open(now=_GATE_NOW)
    reset = await gate.record_quota_error(now=_GATE_NOW)
    assert_eq(reset - _GATE_NOW, timedelta(minutes=15))
    await db.close()


@test()
async def api_gate_pause_persists_across_instances() -> None:
    """A standing pause survives a restart (a fresh gate over the same db)."""
    db = await gate_db()
    await YouTubeApiGate(db, config=gate_config()).record_quota_error(now=_GATE_NOW)

    reopened = YouTubeApiGate(db, config=gate_config())

    with assert_raises(YouTubeQuotaExceededError):
        await reopened.ensure_open(now=_GATE_NOW + timedelta(minutes=10))
    await db.close()


@test()
async def client_live_quota_error_pauses_every_call() -> None:
    """A live 403 escalates the gate, so later calls short-circuit before YouTube."""
    db = await gate_db()
    clock = FakeClock(_GATE_NOW)
    api = QuotaBlockingApi(fail_times=99)
    client = YouTubeApiClient(
        api,
        DailyQuota(db, limit=1000),
        clock=clock,
        gate=YouTubeApiGate(db, config=gate_config()),
    )

    with assert_raises(YouTubeQuotaExceededError):
        _ = await client.list_liked_page(page_token=None, page_size=2)

    # The live call happened exactly once; the gate now closed, so the next list
    # and the (shared) transcript spend both raise before reaching upstream.
    assert_eq(api.list_calls, 1)
    with assert_raises(YouTubeQuotaExceededError):
        _ = await client.list_liked_page(page_token=None, page_size=2)
    assert_eq(api.list_calls, 1)
    with assert_raises(YouTubeQuotaExceededError):
        await client.charge_transcript()
    await db.close()


@test()
async def client_clears_gate_after_a_recovered_call() -> None:
    """Once the cooldown elapses, a successful call reopens the gate for all spend."""
    db = await gate_db()
    clock = FakeClock(_GATE_NOW)
    api = QuotaBlockingApi(fail_times=1)
    client = YouTubeApiClient(
        api,
        DailyQuota(db, limit=1000),
        clock=clock,
        gate=YouTubeApiGate(db, config=gate_config()),
    )
    with assert_raises(YouTubeQuotaExceededError):
        _ = await client.list_liked_page(page_token=None, page_size=2)

    clock.advance(timedelta(minutes=16))
    page = await client.list_liked_page(page_token=None, page_size=2)

    assert_eq(page.next_page_token, None)
    # The gate is clear: the shared transcript spend now passes too.
    await client.charge_transcript()
    await db.close()


def _ingested(
    video_id: str,
    *,
    transcript: str | None = None,
    ignored_at: datetime | None = None,
) -> IngestedVideo[Pending]:
    """Build an ingested-video row for direct insertion in status tests."""
    return IngestedVideo(
        video_id=video_id,
        source="liked",
        title="t",
        channel="c",
        topic="python",
        description="",
        transcript=transcript,
        ignored_at=ignored_at,
    )


@test()
async def sync_status_partitions_the_corpus_by_transcript_state() -> None:
    """Status counts active videos, splitting them into done/pending/unavailable."""
    env = await load_fixture(make_env(InMemoryYouTubeApi(), daily_limit=50))
    async with env.db.transaction() as tx:
        _ = await tx.execute(insert(_ingested("done", transcript="hello")))
        _ = await tx.execute(insert(_ingested("pending")))
        _ = await tx.execute(insert(_ingested("terminal")))
        # An ignored video is out of the corpus and not counted at all.
        _ = await tx.execute(
            insert(_ingested("ignored", ignored_at=datetime(2026, 1, 1, tzinfo=UTC)))
        )
        # `terminal` will never get a transcript -> unavailable, not pending.
        _ = await tx.execute(
            insert(YouTubeTranscriptState(video_id="terminal", status="terminal"))
        )

    status = await env.service.sync_status(logger=test_logger())

    assert_eq(status.videos_total, 3)
    assert_eq(status.transcripts_done, 1)
    assert_eq(status.transcripts_pending, 1)
    assert_eq(status.transcripts_unavailable, 1)


@test()
async def sync_status_reports_last_run_quota_and_no_pauses_by_default() -> None:
    """A clean sync stamps last-run, exposes the day's quota, and is unpaused."""
    api = InMemoryYouTubeApi(liked=[video("v1")])
    env = await load_fixture(make_env(api, daily_limit=10))
    _ = await env.sync.sync(logger=test_logger())

    status = await env.service.sync_status(logger=test_logger())

    assert_is_not_none(status.last_synced_at)
    assert_eq(status.quota.limit, 10)
    # One hot page: one list unit + one metadata unit.
    assert_eq(status.quota.used, 2)
    assert_is_none(status.api_paused_until)
    assert_eq(status.transcript_providers_paused, [])


@test()
async def sync_status_is_empty_before_any_sync() -> None:
    """With no corpus and no run, status reports zeroes and a null last-run."""
    env = await load_fixture(make_env(InMemoryYouTubeApi()))

    status = await env.service.sync_status(logger=test_logger())

    assert_eq(status.videos_total, 0)
    assert_eq(status.transcripts_pending, 0)
    assert_is_none(status.last_synced_at)


@test()
async def schema_is_idempotent_to_create() -> None:
    """Re-running schema creation is a no-op (migrations recorded by name)."""
    db = await Database.initialize(backend=Config(database=":memory:"))
    await create_youtube_schema(db)
    await create_youtube_schema(db)
    async with db.transaction() as tx:
        rows: list[IngestedVideo[Fetched]] = await tx.fetch_all(
            select(IngestedVideo).all()
        )
    assert_eq(rows, [])
    await db.close()
