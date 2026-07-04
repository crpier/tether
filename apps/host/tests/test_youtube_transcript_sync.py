"""Behaviour tests for the background transcript sync worker.

These drive `TranscriptSyncService` (and the on-demand `YouTubeService` fetch
path) against a real in-memory SQLite database and a programmable fake
`TranscriptProvider` — never real captions, never the network. The fake returns
success, unavailable, excluded, or transient outcomes (scripted per video, so a
transient failure can later succeed), letting us assert the full per-video state
machine: store-and-done, terminal-and-never-retried, excluded-and-purged,
backed-off retries that survive a fresh worker instance, budget exhaustion, and
recency ordering.
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import structlog
from opentelemetry import trace
from opentelemetry.trace import Tracer
from snekql.sqlite import Config, Database, Fetched, insert, select
from snektest import (
    assert_eq,
    assert_false,
    assert_in,
    assert_is_none,
    assert_is_not_none,
    assert_not_in,
    assert_true,
    fixture,
    load_fixture,
    test,
)

from tether.logging import Logger
from tether.youtube import (
    _NO_PAUSED_SOURCES,
    DailyQuota,
    FetchedTranscript,
    IngestedVideo,
    InMemoryYouTubeApi,
    TranscriptExcludedError,
    TranscriptSegment,
    TranscriptSyncConfig,
    TranscriptSyncService,
    TranscriptTransientError,
    TranscriptUnavailableError,
    YouTubeApiClient,
    YouTubeService,
    YouTubeTranscriptState,
    create_youtube_schema,
)


def noop_tracer() -> Tracer:
    """A tracer that emits nowhere."""
    return trace.NoOpTracerProvider().get_tracer("test.transcript_sync")


def test_logger() -> Logger:
    """A throwaway structured logger for the mandatory service logger arg."""
    return structlog.stdlib.get_logger("test.transcript_sync")


class FakeClock:
    """A controllable clock so backoff/retry tests need not wait for wall time."""

    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        self._now += delta


@dataclass(frozen=True)
class Ok:
    """A scripted successful outcome carrying the transcript to return.

    `segments` and `source` default to the text-only, generic-source case so most
    tests need only the text; provenance tests set them explicitly."""

    text: str
    segments: tuple[TranscriptSegment, ...] = ()
    source: str = "fake"


# Scripted failure tokens, mapped to the provider's typed signals.
UNAVAILABLE = "unavailable"
EXCLUDED = "excluded"
TRANSIENT = "transient"

type Outcome = Ok | str


class FakeTranscriptProvider:
    """A `TranscriptProvider` scripted per video, last outcome repeating.

    Each video maps to a list of outcomes consumed one per `fetch`; once a video
    is down to its last scripted outcome, that outcome repeats. An unscripted
    video reports unavailable. Records call counts so tests can prove the worker
    stops calling a terminal/not-due video.
    """

    source: str = "fake"

    def __init__(self, scripts: dict[str, list[Outcome]]) -> None:
        self._scripts: dict[str, list[Outcome]] = {
            key: list(value) for key, value in scripts.items()
        }
        self.calls: dict[str, int] = {}

    async def fetch(
        self,
        video_id: str,
        *,
        paused_sources: frozenset[str] = _NO_PAUSED_SOURCES,
        skip_sources: frozenset[str] = _NO_PAUSED_SOURCES,
    ) -> FetchedTranscript:
        _ = (paused_sources, skip_sources)  # this fake has no blockable source
        self.calls[video_id] = self.calls.get(video_id, 0) + 1
        script = self._scripts.get(video_id)
        if not script:
            raise TranscriptUnavailableError(video_id)
        token = script.pop(0) if len(script) > 1 else script[0]
        if isinstance(token, Ok):
            return FetchedTranscript(
                text=token.text, segments=token.segments, source=token.source
            )
        if token == EXCLUDED:
            raise TranscriptExcludedError(video_id)
        if token == TRANSIENT:
            raise TranscriptTransientError(video_id)
        raise TranscriptUnavailableError(video_id)


@dataclass
class Env:
    """The worker + on-demand service sharing one database and budgeted client."""

    worker: TranscriptSyncService
    service: YouTubeService
    db: Database
    client: YouTubeApiClient
    provider: FakeTranscriptProvider
    clock: FakeClock


@fixture
async def make_env(
    provider: FakeTranscriptProvider,
    *,
    daily_limit: int = 1000,
    config: TranscriptSyncConfig | None = None,
) -> AsyncGenerator[Env]:
    """A fresh DB plus the worker + service wired over a shared budgeted client."""
    db = await Database.initialize(backend=Config(database=":memory:"))
    await create_youtube_schema(db)
    clock = FakeClock(datetime(2026, 6, 1, 12, 0, tzinfo=UTC))
    quota = DailyQuota(db, limit=daily_limit)
    client = YouTubeApiClient(InMemoryYouTubeApi(), quota, clock=clock)
    worker = TranscriptSyncService(
        database=db,
        client=client,
        provider=provider,
        config=config,
    )
    service = YouTubeService(
        database=db, client=client, provider=provider, tracer=noop_tracer()
    )
    if config is not None:
        # Match the composition root: the on-demand path shares the worker's config.
        service.config = config
    yield Env(
        worker=worker,
        service=service,
        db=db,
        client=client,
        provider=provider,
        clock=clock,
    )
    await db.close()


async def seed(
    db: Database,
    video_id: str,
    *,
    liked_at: datetime | None = None,
    title: str = "A Talk",
    topic: str = "python",
) -> None:
    """Insert an active, transcript-less ingested video into the corpus."""
    async with db.transaction() as tx:
        _ = await tx.execute(
            insert(
                IngestedVideo(
                    video_id=video_id,
                    source="liked",
                    title=title,
                    channel="PyConf",
                    topic=topic,
                    description="",
                    liked_at=liked_at,
                )
            )
        )


async def state_of(
    db: Database, video_id: str
) -> YouTubeTranscriptState[Fetched] | None:
    """Fetch a video's persisted transcript-state row, or None when pending."""
    async with db.transaction() as tx:
        return await tx.fetch_one_or_none(
            select(YouTubeTranscriptState).where(
                YouTubeTranscriptState.video_id.eq(video_id)
            )
        )


# --- Success ----------------------------------------------------------------


@test()
async def successful_fetch_stores_transcript_and_marks_done() -> None:
    """A success stores the transcript, marks done, and makes it searchable."""
    provider = FakeTranscriptProvider({"v1": [Ok("coroutines at length")]})
    env = await load_fixture(make_env(provider))
    await seed(env.db, "v1")

    report = await env.worker.sync(logger=test_logger())

    assert_eq(report.fetched, 1)
    stored = await env.service.get_video("v1")
    assert_eq(stored.transcript, "coroutines at length")
    persisted = await state_of(env.db, "v1")
    assert_is_not_none(persisted)
    assert_eq(persisted.status if persisted is not None else None, "done")
    search = await env.service.search("coroutines", logger=test_logger())
    assert_in("v1", {row.video_id for row in search.videos})


@test()
async def a_done_video_is_not_fetched_again() -> None:
    """Once a transcript is stored, a later pass does not re-fetch it."""
    provider = FakeTranscriptProvider({"v1": [Ok("body")]})
    env = await load_fixture(make_env(provider))
    await seed(env.db, "v1")
    _ = await env.worker.sync(logger=test_logger())

    _ = await env.worker.sync(logger=test_logger())

    assert_eq(env.provider.calls["v1"], 1)


# --- Provenance: the producing source and timed segments are persisted -------


@test()
async def a_fetch_persists_the_producing_source() -> None:
    """The provider tag that produced a transcript is stored on the video row."""
    provider = FakeTranscriptProvider({"v1": [Ok("hi there", source="supadata")]})
    env = await load_fixture(make_env(provider))
    await seed(env.db, "v1")

    _ = await env.worker.sync(logger=test_logger())

    assert_eq((await env.service.get_video("v1")).transcript_source, "supadata")


@test()
async def a_fetch_persists_timed_segments_as_json() -> None:
    """A transcript's timed segments are stored as JSON alongside the joined text."""
    provider = FakeTranscriptProvider(
        {
            "v1": [
                Ok(
                    "hello world",
                    segments=(
                        TranscriptSegment(start_seconds=0.0, text="hello"),
                        TranscriptSegment(start_seconds=1.5, text="world"),
                    ),
                    source="supadata",
                )
            ]
        }
    )
    env = await load_fixture(make_env(provider))
    await seed(env.db, "v1")

    _ = await env.worker.sync(logger=test_logger())

    stored = (await env.service.get_video("v1")).transcript_segments_json
    assert_is_not_none(stored)
    assert_eq(
        json.loads(stored) if stored is not None else None,
        [
            {"start_seconds": 0.0, "text": "hello"},
            {"start_seconds": 1.5, "text": "world"},
        ],
    )


@test()
async def a_text_only_fetch_stores_null_segments() -> None:
    """A provider that yields no segments leaves the segments column null."""
    provider = FakeTranscriptProvider({"v1": [Ok("plain text", source="captions")]})
    env = await load_fixture(make_env(provider))
    await seed(env.db, "v1")

    _ = await env.worker.sync(logger=test_logger())

    assert_is_none((await env.service.get_video("v1")).transcript_segments_json)


# --- Unavailable: terminal, never retried -----------------------------------


@test()
async def unavailable_marks_terminal_and_is_never_retried() -> None:
    """An unavailable result goes terminal and is skipped on later passes."""
    provider = FakeTranscriptProvider({"v1": [UNAVAILABLE]})
    env = await load_fixture(make_env(provider))
    await seed(env.db, "v1")

    first = await env.worker.sync(logger=test_logger())
    second = await env.worker.sync(logger=test_logger())

    assert_eq(first.unavailable, 1)
    # Terminal videos are excluded from the next pass, so the provider is not
    # called again and nothing is processed.
    assert_eq(second.unavailable, 0)
    assert_eq(env.provider.calls["v1"], 1)
    persisted = await state_of(env.db, "v1")
    assert_eq(persisted.status if persisted is not None else None, "terminal")


# --- Excluded: terminal + purged from active ingestion ----------------------


@test()
async def excluded_marks_terminal_and_purges_from_ingestion() -> None:
    """An excluded (members-only) result goes terminal and drops out of browse."""
    provider = FakeTranscriptProvider({"v1": [EXCLUDED]})
    env = await load_fixture(make_env(provider))
    await seed(env.db, "v1")

    report = await env.worker.sync(logger=test_logger())

    assert_eq(report.excluded, 1)
    browsed = await env.service.browse(logger=test_logger())
    assert_not_in("v1", {row.video_id for row in browsed.videos})
    purged = await env.service.get_video("v1")
    assert_is_not_none(purged.ignored_at)


# --- Transient: backed-off retry that survives a restart --------------------


@test()
async def transient_failure_schedules_a_backed_off_retry() -> None:
    """A transient failure increments attempts and is not retried until due."""
    provider = FakeTranscriptProvider({"v1": [TRANSIENT, Ok("eventually")]})
    config = TranscriptSyncConfig(backoff_base=timedelta(minutes=5))
    env = await load_fixture(make_env(provider, config=config))
    await seed(env.db, "v1")

    first = await env.worker.sync(logger=test_logger())

    assert_eq(first.retried, 1)
    persisted = await state_of(env.db, "v1")
    assert_eq(persisted.status if persisted is not None else None, "retry")
    assert_eq(persisted.attempts if persisted is not None else None, 1)
    assert_is_not_none(persisted.next_attempt_at if persisted is not None else None)

    # A fresh worker over the same database stands in for a restart; the retry is
    # not yet due, so it is skipped and the provider is not called again.
    restarted = TranscriptSyncService(
        database=env.db,
        client=env.client,
        provider=env.provider,
        config=config,
    )
    _ = await restarted.sync(logger=test_logger())
    assert_eq(env.provider.calls["v1"], 1)
    assert_is_none((await env.service.get_video("v1")).transcript)

    # Once the backoff elapses, the retry runs and succeeds.
    env.clock.advance(timedelta(minutes=6))
    third = await restarted.sync(logger=test_logger())
    assert_eq(third.fetched, 1)
    assert_eq(env.provider.calls["v1"], 2)
    assert_eq((await env.service.get_video("v1")).transcript, "eventually")


# --- Transient-failure storm: a systematic failure halts the pass -----------


@test()
async def a_transient_failure_storm_stops_the_pass_early() -> None:
    """Consecutive transient failures trip the storm breaker: the pass halts after
    the threshold instead of burning a call (and a paid credit) on every candidate.

    This is the guard against a systematic failure (e.g. every Supadata request
    400ing) marching through the whole recent window before something else pauses
    it."""
    ids = ("v1", "v2", "v3", "v4", "v5")
    provider = FakeTranscriptProvider({vid: [TRANSIENT] for vid in ids})
    config = TranscriptSyncConfig(transient_storm_threshold=3)
    env = await load_fixture(make_env(provider, config=config))
    for offset, vid in enumerate(ids):
        await seed(env.db, vid, liked_at=datetime(2026, 6, 5 - offset, tzinfo=UTC))

    report = await env.worker.sync(logger=test_logger())

    # Exactly `threshold` calls, then the breaker stops the pass; the remaining
    # candidates are never fetched, so no credit is spent on them.
    assert_eq(sum(env.provider.calls.values()), 3)
    assert_eq(report.retried, 3)
    assert_true(report.transient_storm)


@test()
async def a_success_between_transients_resets_the_storm_counter() -> None:
    """The breaker counts *consecutive* transients: a success resets the run, so a
    flaky-but-working provider still drains the whole pass."""
    ids = ("v1", "v2", "v3", "v4", "v5")
    provider = FakeTranscriptProvider(
        {
            "v1": [TRANSIENT],
            "v2": [TRANSIENT],
            "v3": [Ok("body")],
            "v4": [TRANSIENT],
            "v5": [TRANSIENT],
        }
    )
    config = TranscriptSyncConfig(transient_storm_threshold=3)
    env = await load_fixture(make_env(provider, config=config))
    for offset, vid in enumerate(ids):
        await seed(env.db, vid, liked_at=datetime(2026, 6, 5 - offset, tzinfo=UTC))

    report = await env.worker.sync(logger=test_logger())

    # Newest-first: v1,v2 transient (run 2), v3 success (reset), v4,v5 transient
    # (run 2) — never 3 in a row, so no storm and every candidate is attempted.
    assert_eq(sum(env.provider.calls.values()), 5)
    assert_false(report.transient_storm)
    assert_eq(report.fetched, 1)
    assert_eq(report.retried, 4)


# --- Daily budget -----------------------------------------------------------


@test()
async def worker_stops_when_the_daily_budget_is_exhausted() -> None:
    """An exhausted budget halts the pass after spending what it can."""
    provider = FakeTranscriptProvider(
        {"v1": [Ok("one")], "v2": [Ok("two")], "v3": [Ok("three")]}
    )
    env = await load_fixture(make_env(provider, daily_limit=2))
    await seed(env.db, "v1", liked_at=datetime(2026, 6, 3, tzinfo=UTC))
    await seed(env.db, "v2", liked_at=datetime(2026, 6, 2, tzinfo=UTC))
    await seed(env.db, "v3", liked_at=datetime(2026, 6, 1, tzinfo=UTC))

    report = await env.worker.sync(logger=test_logger())

    # The budget covers two transcript fetches; the third is left for next day.
    assert_eq(report.fetched, 2)
    transcribed = {
        vid
        for vid in ("v1", "v2", "v3")
        if (await env.service.get_video(vid)).transcript is not None
    }
    assert_eq(transcribed, {"v1", "v2"})


# --- Ordering ---------------------------------------------------------------


@test()
async def worker_processes_most_recently_liked_first() -> None:
    """With room for one fetch, the newest-liked video is transcribed first."""
    provider = FakeTranscriptProvider({"new": [Ok("fresh")], "old": [Ok("stale")]})
    env = await load_fixture(make_env(provider, daily_limit=1))
    await seed(env.db, "old", liked_at=datetime(2026, 1, 1, tzinfo=UTC))
    await seed(env.db, "new", liked_at=datetime(2026, 6, 1, tzinfo=UTC))

    _ = await env.worker.sync(logger=test_logger())

    assert_eq((await env.service.get_video("new")).transcript, "fresh")
    assert_is_none((await env.service.get_video("old")).transcript)


# --- On-demand fetch shares the same provider -------------------------------


@test()
async def on_demand_fetch_uses_the_same_provider() -> None:
    """The explicit fetch path runs through the provider and persists done."""
    provider = FakeTranscriptProvider({"v1": [Ok("manual body")]})
    env = await load_fixture(make_env(provider))
    await seed(env.db, "v1")

    result = await env.service.fetch_transcript("v1", logger=test_logger())

    assert_eq(result.transcript, "manual body")
    assert_eq(env.provider.calls["v1"], 1)
    persisted = await state_of(env.db, "v1")
    assert_eq(persisted.status if persisted is not None else None, "done")


@test()
async def on_demand_unavailable_raises_and_marks_terminal() -> None:
    """An on-demand unavailable surfaces an error and stops future retries."""
    provider = FakeTranscriptProvider({"v1": [UNAVAILABLE]})
    env = await load_fixture(make_env(provider))
    await seed(env.db, "v1")

    raised = False
    try:
        _ = await env.service.fetch_transcript("v1", logger=test_logger())
    except TranscriptUnavailableError:
        raised = True

    assert_eq(raised, True)
    persisted = await state_of(env.db, "v1")
    assert_eq(persisted.status if persisted is not None else None, "terminal")


@test()
async def on_demand_transient_retry_uses_the_configured_backoff() -> None:
    """An on-demand transient failure schedules its retry on the service's config,
    not a divergent default backoff."""
    provider = FakeTranscriptProvider({"v1": [TRANSIENT]})
    config = TranscriptSyncConfig(backoff_base=timedelta(minutes=45))
    env = await load_fixture(make_env(provider, config=config))
    await seed(env.db, "v1")

    raised = False
    try:
        _ = await env.service.fetch_transcript("v1", logger=test_logger())
    except TranscriptTransientError:
        raised = True

    assert_eq(raised, True)
    persisted = await state_of(env.db, "v1")
    due = persisted.next_attempt_at if persisted is not None else None
    assert_eq(due, (env.clock.now() + timedelta(minutes=45)).isoformat())
