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

import asyncio
import contextlib
import json
from collections.abc import AsyncGenerator, Awaitable, Callable
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
    assert_raises,
    assert_true,
    fixture,
    load_fixture,
    test,
)

from tether.logging import Logger
from tether.transcript_library import LibraryPassBudget, YouTubeTranscriptApiProvider
from tether.transcript_worker import TranscriptSyncService
from tether.youtube import (
    _NO_PAUSED_SOURCES,
    DailyQuota,
    FallbackTranscriptProvider,
    FetchedTranscript,
    IngestedVideo,
    InMemoryYouTubeApi,
    TranscriptExcludedError,
    TranscriptSegment,
    TranscriptSyncConfig,
    TranscriptTransientError,
    TranscriptUnavailableError,
    YouTubeApiClient,
    YouTubeQuotaExceededError,
    YouTubeService,
    YouTubeTranscriptState,
    create_youtube_schema,
)
from tether.youtube_oauth import CaptionsTranscriptProvider


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


async def _no_charge() -> None:
    """The default no-op charge: a fake provider with no bound daily budget."""


class FakeTranscriptProvider:
    """A `TranscriptProvider` scripted per video, last outcome repeating.

    Each video maps to a list of outcomes consumed one per `fetch`; once a video
    is down to its last scripted outcome, that outcome repeats. An unscripted
    video reports unavailable. Records call counts so tests can prove the worker
    stops calling a terminal/not-due video.

    Mirrors `CaptionsTranscriptProvider`'s self-charging design: `charge` is a
    no-op unless a test explicitly binds it (e.g. to `client.charge_transcript`)
    to simulate a Data-API-backed source that spends the daily budget.
    """

    source: str = "fake"

    def __init__(self, scripts: dict[str, list[Outcome]]) -> None:
        self._scripts: dict[str, list[Outcome]] = {
            key: list(value) for key, value in scripts.items()
        }
        self.calls: dict[str, int] = {}
        self.charge: Callable[[], Awaitable[None]] = _no_charge

    async def fetch(
        self,
        video_id: str,
        *,
        paused_sources: frozenset[str] = _NO_PAUSED_SOURCES,
        skip_sources: frozenset[str] = _NO_PAUSED_SOURCES,
    ) -> FetchedTranscript:
        _ = (paused_sources, skip_sources)  # this fake has no blockable source
        await self.charge()
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


# --- sync_forever crash survival ---------------------------------------------


class _RaisingProvider:
    """A `TranscriptProvider` whose `fetch` always raises an unclassified error.

    Stands in for any exception `fetch_and_store_transcript` doesn't map onto
    one of the four typed outcomes (e.g. a raw network error a provider forgot
    to classify) — it propagates straight out of `sync()`. `sync_forever` is
    the last line of defense against that: this fake exercises it directly."""

    source: str = "raising"

    def __init__(self, error: Exception) -> None:
        self._error: Exception = error
        self.calls: int = 0

    async def fetch(
        self,
        video_id: str,
        *,
        paused_sources: frozenset[str] = _NO_PAUSED_SOURCES,
        skip_sources: frozenset[str] = _NO_PAUSED_SOURCES,
    ) -> FetchedTranscript:
        _ = (video_id, paused_sources, skip_sources)
        self.calls += 1
        raise self._error


@test()
async def sync_forever_survives_an_unclassified_exception_and_keeps_looping() -> None:
    """A pass-level exception that isn't one of the four typed outcomes (e.g. an
    unclassified network error escaping a provider) must not kill the loop task
    — it is logged and the next pass runs on the normal interval.

    Regression for a host crash: `TranscriptSyncService.sync_forever` looked
    like it already caught `Exception` broadly, but nothing exercised that the
    loop task actually survives *and keeps calling `sync` on schedule* rather
    than, say, silently dying without technically raising out of the task.
    """
    db = await Database.initialize(backend=Config(database=":memory:"))
    await create_youtube_schema(db)
    clock = FakeClock(datetime(2026, 6, 1, 12, 0, tzinfo=UTC))
    quota = DailyQuota(db, limit=1000)
    client = YouTubeApiClient(InMemoryYouTubeApi(), quota, clock=clock)
    provider = _RaisingProvider(RuntimeError("boom: unclassified failure"))
    worker = TranscriptSyncService(
        database=db, client=client, provider=provider, config=TranscriptSyncConfig()
    )
    await seed(db, "v1")

    logger = test_logger()
    task = asyncio.create_task(
        worker.sync_forever(interval_seconds=0.01, logger=logger)
    )
    try:
        # Several intervals' worth of real wall time: enough passes to prove
        # the loop keeps calling `sync` (not just that the first failure
        # didn't immediately crash the task).
        for _ in range(20):
            await asyncio.sleep(0.01)
            if provider.calls >= 3:
                break
        assert_false(task.done())
        assert_true(provider.calls >= 3)
    finally:
        _ = task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    # A real cancellation still propagates — the broad `except Exception` must
    # not also swallow `CancelledError`.
    assert_true(task.cancelled())


@test()
async def sync_forever_still_propagates_cancellation() -> None:
    """`asyncio.CancelledError` must not be treated as just another exception to
    log-and-continue: a genuine shutdown/reload cancellation has to actually
    stop the loop, not be swallowed by the broad `except Exception`."""
    provider = FakeTranscriptProvider({})
    env = await load_fixture(make_env(provider))

    task = asyncio.create_task(
        env.worker.sync_forever(interval_seconds=10, logger=test_logger())
    )
    await asyncio.sleep(0)  # let it reach the first `asyncio.sleep`

    _ = task.cancel()
    with assert_raises(asyncio.CancelledError):
        await task
    assert_true(task.cancelled())


# --- Daily budget -----------------------------------------------------------


@test()
async def worker_stops_when_the_daily_budget_is_exhausted() -> None:
    """An exhausted budget halts the pass after spending what it can.

    Binds the fake provider's charge to the shared client, simulating a
    Data-API-backed source (e.g. captions) — the only kind that spends the
    daily budget."""
    provider = FakeTranscriptProvider(
        {"v1": [Ok("one")], "v2": [Ok("two")], "v3": [Ok("three")]}
    )
    env = await load_fixture(make_env(provider, daily_limit=2))
    provider.charge = env.client.charge_transcript
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
    """With room for one fetch, the newest-liked video is transcribed first.

    Binds the fake provider's charge to the shared client, simulating a
    Data-API-backed source (e.g. captions) — the only kind that spends the
    daily budget."""
    provider = FakeTranscriptProvider({"new": [Ok("fresh")], "old": [Ok("stale")]})
    env = await load_fixture(make_env(provider, daily_limit=1))
    provider.charge = env.client.charge_transcript
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


# --- Real youtube-transcript-api provider: per-pass budget end to end -------
#
# The rest of this file drives the worker over a `FakeTranscriptProvider`, which
# has no blockable source of its own. These tests wire the *real*
# `YouTubeTranscriptApiProvider` in directly (still with a fake, in-process
# fetcher — never the network) to prove the worker actually resets and honors
# its per-pass request budget end to end (issue #179): a pass must never fire
# more real calls at the library than the configured budget, and a fresh pass
# gets a fresh budget rather than staying blocked forever.


class _CountingFetcher:
    """A fetcher stand-in that always succeeds and counts real invocations."""

    def __init__(self) -> None:
        self.calls: int = 0

    def __call__(self, video_id: str) -> list[dict[str, object]]:
        self.calls += 1
        return [{"text": f"transcript for {video_id}", "start": 0.0}]


@test()
async def a_sync_pass_never_exceeds_the_librarys_request_budget() -> None:
    """Five eligible videos, a budget of two: only two real calls fire, the
    other three stay pending (no per-video state written, not terminal)."""
    db = await Database.initialize(backend=Config(database=":memory:"))
    await create_youtube_schema(db)
    clock = FakeClock(datetime(2026, 6, 1, 12, 0, tzinfo=UTC))
    client = YouTubeApiClient(
        InMemoryYouTubeApi(), DailyQuota(db, limit=1000), clock=clock
    )
    fetcher = _CountingFetcher()
    provider = YouTubeTranscriptApiProvider(
        fetcher, budget=LibraryPassBudget(max_requests_per_pass=2)
    )
    worker = TranscriptSyncService(
        database=db,
        client=client,
        provider=provider,
        config=TranscriptSyncConfig(block_pause_base=timedelta(minutes=1)),
    )
    for video_id in ("v1", "v2", "v3", "v4", "v5"):
        await seed(db, video_id)

    report = await worker.sync(logger=test_logger())

    assert_eq(fetcher.calls, 2)
    assert_eq(report.fetched, 2)
    assert_true(report.paused)
    # The three videos past the budget were deferred (a *blocked* outcome), not
    # attempted at all — no per-video state, so a later pass still sees them.
    still_pending = [
        video_id
        for video_id in ("v1", "v2", "v3", "v4", "v5")
        if (await state_of(db, video_id)) is None
    ]
    assert_eq(len(still_pending), 3)

    await db.close()


@test()
async def a_fresh_pass_refills_the_librarys_request_budget() -> None:
    """After the per-source pause lifts, the next pass again gets its own full
    budget rather than staying exhausted/latched from the previous pass."""
    db = await Database.initialize(backend=Config(database=":memory:"))
    await create_youtube_schema(db)
    clock = FakeClock(datetime(2026, 6, 1, 12, 0, tzinfo=UTC))
    client = YouTubeApiClient(
        InMemoryYouTubeApi(), DailyQuota(db, limit=1000), clock=clock
    )
    fetcher = _CountingFetcher()
    provider = YouTubeTranscriptApiProvider(
        fetcher, budget=LibraryPassBudget(max_requests_per_pass=2)
    )
    worker = TranscriptSyncService(
        database=db,
        client=client,
        provider=provider,
        config=TranscriptSyncConfig(block_pause_base=timedelta(minutes=1)),
    )
    for video_id in ("v1", "v2", "v3", "v4", "v5"):
        await seed(db, video_id)
    _ = await worker.sync(logger=test_logger())
    assert_eq(fetcher.calls, 2)

    # Move past the one-minute cooldown the first pass's budget-exhaustion trip.
    clock.advance(timedelta(minutes=2))
    report = await worker.sync(logger=test_logger())

    # A fresh pass, a fresh budget: two more real calls, not zero.
    assert_eq(fetcher.calls, 4)
    assert_eq(report.fetched, 2)

    await db.close()


# --- DailyQuota accounting: only captions spends it (#2, mixing bug) --------


class _FakeCaptionRequest:
    """A built request that returns a canned value."""

    def __init__(self, result: object) -> None:
        self._result = result

    def execute(self) -> object:
        return self._result


class _FakeCaptionsCollection:
    """A fake `captions` collection serving exactly one human-authored track."""

    def list(self, **_kwargs: object) -> _FakeCaptionRequest:
        return _FakeCaptionRequest(
            {"items": [{"id": "t1", "snippet": {"trackKind": "standard"}}]}
        )

    def download(self, **_kwargs: object) -> _FakeCaptionRequest:
        srt = "1\n00:00:01,000 --> 00:00:02,000\nhello\n"
        return _FakeCaptionRequest(srt)


class _FakeCaptionsResource:
    """A discovery resource exposing only the captions collection."""

    def captions(self) -> _FakeCaptionsCollection:
        return _FakeCaptionsCollection()


@test()
async def a_fallback_source_serving_the_video_never_spends_the_daily_quota() -> None:
    """A video served by a non-captions fallback (Supadata/library stand-in)
    leaves the YouTube Data API daily quota untouched — only captions.list/
    download are billed Data API calls; the free/paid transcript sources aren't.
    """
    db = await Database.initialize(backend=Config(database=":memory:"))
    await create_youtube_schema(db)
    clock = FakeClock(datetime(2026, 6, 1, 12, 0, tzinfo=UTC))
    quota = DailyQuota(db, limit=10)
    client = YouTubeApiClient(InMemoryYouTubeApi(), quota, clock=clock)
    captions = CaptionsTranscriptProvider(
        _FakeCaptionsResource()  # pyright: ignore[reportArgumentType]
    )
    captions.charge = client.charge_transcript
    fake_fallback = FakeTranscriptProvider(
        {"v1": [Ok(text="from the fallback", source="fake")]}
    )
    # A caption-less video: the primary is unavailable, so the fallback serves it.
    empty_captions = CaptionsTranscriptProvider(
        _EmptyCaptionsResource()  # pyright: ignore[reportArgumentType]
    )
    empty_captions.charge = client.charge_transcript
    chain = FallbackTranscriptProvider(empty_captions, fallbacks=[fake_fallback])
    worker = TranscriptSyncService(database=db, client=client, provider=chain)
    await seed(db, "v1")

    report = await worker.sync(logger=test_logger())

    assert_eq(report.fetched, 1)
    # The captions primary *did* charge (it always runs first), so the quota
    # accounting reflects that live call — but the fallback's own success never
    # charges a second unit.
    assert_eq(await quota.used(now=clock.now()), 1)

    await db.close()


class _EmptyCaptionsResource:
    """A discovery resource whose captions collection has no tracks."""

    def captions(self) -> _EmptyCaptionsCollection:
        return _EmptyCaptionsCollection()


class _EmptyCaptionsCollection:
    """A fake `captions` collection with no tracks (drives the unavailable path)."""

    def list(self, **_kwargs: object) -> _FakeCaptionRequest:
        return _FakeCaptionRequest({"items": []})

    def download(self, **_kwargs: object) -> _FakeCaptionRequest:
        raise AssertionError("no track to download")


@test()
async def a_captions_hit_spends_exactly_one_daily_quota_unit() -> None:
    """A video the captions primary itself serves spends exactly one unit."""
    db = await Database.initialize(backend=Config(database=":memory:"))
    await create_youtube_schema(db)
    clock = FakeClock(datetime(2026, 6, 1, 12, 0, tzinfo=UTC))
    quota = DailyQuota(db, limit=10)
    client = YouTubeApiClient(InMemoryYouTubeApi(), quota, clock=clock)
    captions = CaptionsTranscriptProvider(
        _FakeCaptionsResource()  # pyright: ignore[reportArgumentType]
    )
    captions.charge = client.charge_transcript
    worker = TranscriptSyncService(database=db, client=client, provider=captions)
    await seed(db, "v1")

    report = await worker.sync(logger=test_logger())

    assert_eq(report.fetched, 1)
    assert_eq(await quota.used(now=clock.now()), 1)

    await db.close()


@test()
async def an_unbound_chain_with_only_fallback_sources_never_touches_the_quota() -> None:
    """A chain with no captions leaf at all (the default supadata/library order)
    never spends the daily quota, however many videos it fetches — reproducing
    and fixing the mixing bug where every transcript, regardless of provider,
    spent one YouTube Data API unit."""
    db = await Database.initialize(backend=Config(database=":memory:"))
    await create_youtube_schema(db)
    clock = FakeClock(datetime(2026, 6, 1, 12, 0, tzinfo=UTC))
    quota = DailyQuota(db, limit=10)
    client = YouTubeApiClient(InMemoryYouTubeApi(), quota, clock=clock)
    fake_provider = FakeTranscriptProvider(
        {
            "v1": [Ok(text="one", source="supadata")],
            "v2": [Ok(text="two", source="youtube_transcript_api")],
        }
    )
    worker = TranscriptSyncService(database=db, client=client, provider=fake_provider)
    await seed(db, "v1")
    await seed(db, "v2")

    report = await worker.sync(logger=test_logger())

    assert_eq(report.fetched, 2)
    assert_eq(await quota.used(now=clock.now()), 0)


@test()
async def on_demand_fetch_raises_when_quota_exhausted_and_captions_is_the_source() -> (
    None
):
    """The on-demand path still stops before a live captions call once the day's
    Data API budget is spent (translated to 429 at the HTTP boundary)."""
    db = await Database.initialize(backend=Config(database=":memory:"))
    await create_youtube_schema(db)
    clock = FakeClock(datetime(2026, 6, 1, 12, 0, tzinfo=UTC))
    quota = DailyQuota(db, limit=1)
    await quota.spend(1, now=clock.now())
    client = YouTubeApiClient(InMemoryYouTubeApi(), quota, clock=clock)
    captions = CaptionsTranscriptProvider(
        _FakeCaptionsResource()  # pyright: ignore[reportArgumentType]
    )
    captions.charge = client.charge_transcript
    service = YouTubeService(
        database=db, client=client, provider=captions, tracer=noop_tracer()
    )
    await seed(db, "v1")

    with assert_raises(YouTubeQuotaExceededError):
        _ = await service.fetch_transcript("v1", logger=test_logger())

    await db.close()


@test()
async def on_demand_fetch_ignores_exhausted_quota_with_no_captions_in_the_chain() -> (
    None
):
    """A chain with no captions leaf never checks (or spends) the daily quota, so
    a video still fetches even once the day's budget is nominally exhausted."""
    db = await Database.initialize(backend=Config(database=":memory:"))
    await create_youtube_schema(db)
    clock = FakeClock(datetime(2026, 6, 1, 12, 0, tzinfo=UTC))
    quota = DailyQuota(db, limit=1)
    await quota.spend(1, now=clock.now())
    client = YouTubeApiClient(InMemoryYouTubeApi(), quota, clock=clock)
    fake_provider = FakeTranscriptProvider(
        {"v1": [Ok(text="served", source="supadata")]}
    )
    service = YouTubeService(
        database=db, client=client, provider=fake_provider, tracer=noop_tracer()
    )
    await seed(db, "v1")

    result = await service.fetch_transcript("v1", logger=test_logger())

    assert_eq(result.transcript, "served")

    await db.close()

    await db.close()
