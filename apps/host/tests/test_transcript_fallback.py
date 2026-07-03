"""Behaviour tests for the transcript fallback composition and provider pause.

Two layers, both fake-driven and offline:

* `FallbackTranscriptProvider` unit tests prove the captions-first, fall-through-
  on-unavailable composition and the per-source `paused_sources` pause hook
  directly (skip a paused source, run an unpaused one, stamp blocks with a source).
* Worker tests drive `TranscriptSyncService` against a real in-memory SQLite
  database and a *real* `FallbackTranscriptProvider` composed of fake sources
  (a non-blockable "captions" primary plus blockable "library"/"supadata"
  fallbacks). They assert the external behaviour the issue calls for: fall-through
  coverage, all-unavailable terminality, the per-source block pause (escalating,
  retry-after-honoring, restart-surviving, streak-resetting), and that an unpaused
  source keeps flowing while another is paused.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import structlog
from snekql.sqlite import Config, Database, Fetched, insert, select
from snektest import (
    assert_eq,
    assert_is_none,
    assert_raises,
    fixture,
    load_fixture,
    test,
)

from tether.logging import Logger
from tether.youtube import (
    _NO_PAUSED_SOURCES,
    DailyQuota,
    FallbackTranscriptProvider,
    FetchedTranscript,
    IngestedVideo,
    InMemoryYouTubeApi,
    TranscriptBlockedError,
    TranscriptExcludedError,
    TranscriptProvider,
    TranscriptSyncConfig,
    TranscriptSyncService,
    TranscriptTransientError,
    TranscriptUnavailableError,
    YouTubeApiClient,
    YouTubeTranscriptState,
    _load_provider_pause,
    create_youtube_schema,
)


def test_logger() -> Logger:
    """A throwaway structured logger for the mandatory service logger arg."""
    return structlog.stdlib.get_logger("test.transcript_fallback")


class FakeClock:
    """A controllable clock so pause/cooldown tests need not wait for wall time."""

    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        self._now += delta


@dataclass(frozen=True)
class Ok:
    """A scripted success carrying the transcript text to return."""

    text: str


@dataclass(frozen=True)
class Blocked:
    """A scripted IP-block, optionally carrying a retry-after hint."""

    retry_after: timedelta | None = None


UNAVAILABLE = "unavailable"
EXCLUDED = "excluded"
TRANSIENT = "transient"

type Outcome = Ok | Blocked | str


class FakeSource(TranscriptProvider):
    """A `TranscriptProvider` scripted per video, last outcome repeating.

    Its `source` is its `name`, so the composite skips it by source and stamps its
    blocks with it. Records call counts so tests can prove the composite skipped (or
    ran) it.
    """

    def __init__(self, scripts: dict[str, list[Outcome]], *, name: str) -> None:
        self._scripts: dict[str, list[Outcome]] = {
            key: list(value) for key, value in scripts.items()
        }
        self.name: str = name
        self.calls: dict[str, int] = {}

    @property
    def source(self) -> str:
        return self.name

    async def fetch(
        self,
        video_id: str,
        *,
        paused_sources: frozenset[str] = _NO_PAUSED_SOURCES,
        disabled_sources: frozenset[str] = _NO_PAUSED_SOURCES,
    ) -> FetchedTranscript:
        _ = (paused_sources, disabled_sources)
        self.calls[video_id] = self.calls.get(video_id, 0) + 1
        script = self._scripts.get(video_id)
        if not script:
            raise TranscriptUnavailableError(video_id)
        token = script.pop(0) if len(script) > 1 else script[0]
        if isinstance(token, Ok):
            return FetchedTranscript(text=token.text, segments=(), source=self.name)
        if isinstance(token, Blocked):
            raise TranscriptBlockedError(video_id, retry_after=token.retry_after)
        if token == EXCLUDED:
            raise TranscriptExcludedError(video_id)
        if token == TRANSIENT:
            raise TranscriptTransientError(video_id)
        raise TranscriptUnavailableError(video_id)


# --- Composite unit tests ---------------------------------------------------


@test()
async def composite_prefers_primary() -> None:
    """The primary wins when it has a transcript; the fallback is not consulted."""
    primary = FakeSource({"v1": [Ok("captions")]}, name="captions")
    library = FakeSource({"v1": [Ok("library")]}, name="library")
    composite = FallbackTranscriptProvider(primary, fallbacks=[library])

    result = await composite.fetch("v1")

    assert_eq(result.text, "captions")
    assert_eq(library.calls.get("v1"), None)


@test()
async def composite_falls_through_on_unavailable() -> None:
    """An unavailable primary falls through to the library fallback."""
    primary = FakeSource({"v1": [UNAVAILABLE]}, name="captions")
    library = FakeSource({"v1": [Ok("library")]}, name="library")
    composite = FallbackTranscriptProvider(primary, fallbacks=[library])

    result = await composite.fetch("v1")

    assert_eq(result.text, "library")
    assert_eq(result.source, "library")


@test()
async def composite_unavailable_only_when_all_unavailable() -> None:
    """Terminal unavailable requires the primary and every fallback to have nothing."""
    primary = FakeSource({"v1": [UNAVAILABLE]}, name="captions")
    library = FakeSource({"v1": [UNAVAILABLE]}, name="library")
    composite = FallbackTranscriptProvider(primary, fallbacks=[library])

    with assert_raises(TranscriptUnavailableError):
        _ = await composite.fetch("v1")


@test()
async def composite_surfaces_excluded_without_fallback() -> None:
    """An excluded primary surfaces immediately; the fallback is not tried."""
    primary = FakeSource({"v1": [EXCLUDED]}, name="captions")
    library = FakeSource({"v1": [Ok("library")]}, name="library")
    composite = FallbackTranscriptProvider(primary, fallbacks=[library])

    with assert_raises(TranscriptExcludedError):
        _ = await composite.fetch("v1")
    assert_eq(library.calls.get("v1"), None)


@test()
async def composite_skips_paused_source_and_defers() -> None:
    """A paused fallback is skipped and the composite defers via a blocked signal."""
    primary = FakeSource({"v1": [UNAVAILABLE]}, name="captions")
    library = FakeSource({"v1": [Ok("library")]}, name="library")
    composite = FallbackTranscriptProvider(primary, fallbacks=[library])

    with assert_raises(TranscriptBlockedError):
        _ = await composite.fetch("v1", paused_sources=frozenset({"library"}))
    # The paused fallback was never consulted.
    assert_eq(library.calls.get("v1"), None)


@test()
async def composite_runs_an_unpaused_source_while_another_is_paused() -> None:
    """A later fallback still runs when only an earlier one is paused (Supadata covers)."""
    primary = FakeSource({"v1": [UNAVAILABLE]}, name="captions")
    library = FakeSource({"v1": [Ok("library")]}, name="library")
    supadata = FakeSource({"v1": [Ok("supadata")]}, name="supadata")
    composite = FallbackTranscriptProvider(primary, fallbacks=[library, supadata])

    result = await composite.fetch("v1", paused_sources=frozenset({"library"}))

    assert_eq(result.text, "supadata")
    assert_eq(result.source, "supadata")
    # The paused library was skipped; the unpaused supadata served it.
    assert_eq(library.calls.get("v1"), None)


@test()
async def composite_primary_still_runs_when_paused() -> None:
    """A captions hit is returned even while a blockable fallback is skipped."""
    primary = FakeSource({"v1": [Ok("captions")]}, name="captions")
    library = FakeSource({"v1": [Ok("library")]}, name="library")
    composite = FallbackTranscriptProvider(primary, fallbacks=[library])

    result = await composite.fetch("v1", paused_sources=frozenset({"library"}))

    assert_eq(result.text, "captions")
    assert_eq(library.calls.get("v1"), None)


@test()
async def composite_stamps_block_with_the_fallback_source() -> None:
    """A fallback's block propagates carrying that fallback's source for the worker."""
    primary = FakeSource({"v1": [UNAVAILABLE]}, name="captions")
    library = FakeSource({"v1": [Blocked()]}, name="library")
    composite = FallbackTranscriptProvider(primary, fallbacks=[library])

    with assert_raises(TranscriptBlockedError) as caught:
        _ = await composite.fetch("v1")
    assert_eq(caught.exception.source, "library")


# --- Worker integration over the real composite -----------------------------


@dataclass
class Env:
    """A worker over a real composite of two fake sources, on a shared database."""

    worker: TranscriptSyncService
    db: Database
    client: YouTubeApiClient
    captions: FakeSource
    library: FakeSource
    clock: FakeClock
    config: TranscriptSyncConfig


@fixture
async def make_env(
    captions: FakeSource,
    library: FakeSource,
    *,
    config: TranscriptSyncConfig | None = None,
) -> AsyncGenerator[Env]:
    """A fresh DB plus a worker over `FallbackTranscriptProvider(captions, library)`."""
    db = await Database.initialize(backend=Config(database=":memory:"))
    await create_youtube_schema(db)
    clock = FakeClock(datetime(2026, 6, 1, 12, 0, tzinfo=UTC))
    client = YouTubeApiClient(
        InMemoryYouTubeApi(), DailyQuota(db, limit=1000), clock=clock
    )
    resolved = config or TranscriptSyncConfig(
        block_pause_base=timedelta(minutes=10),
        block_pause_cap=timedelta(hours=4),
    )
    provider = FallbackTranscriptProvider(captions, fallbacks=[library])
    worker = TranscriptSyncService(
        database=db, client=client, provider=provider, config=resolved
    )
    yield Env(
        worker=worker,
        db=db,
        client=client,
        captions=captions,
        library=library,
        clock=clock,
        config=resolved,
    )
    await db.close()


async def seed(
    db: Database,
    video_id: str,
    *,
    liked_at: datetime,
    caption_available: int | None = None,
) -> None:
    """Insert an active, transcript-less ingested video."""
    async with db.transaction() as tx:
        _ = await tx.execute(
            insert(
                IngestedVideo(
                    video_id=video_id,
                    source="liked",
                    title="A Talk",
                    channel="PyConf",
                    topic="python",
                    description="",
                    liked_at=liked_at,
                    caption_available=caption_available,
                )
            )
        )


async def transcript_of(db: Database, video_id: str) -> str | None:
    async with db.transaction() as tx:
        row = await tx.fetch_one_or_none(
            select(IngestedVideo).where(IngestedVideo.video_id.eq(video_id))
        )
    return row.transcript if row is not None else None


async def state_of(
    db: Database, video_id: str
) -> YouTubeTranscriptState[Fetched] | None:
    async with db.transaction() as tx:
        return await tx.fetch_one_or_none(
            select(YouTubeTranscriptState).where(
                YouTubeTranscriptState.video_id.eq(video_id)
            )
        )


LATER = datetime(2026, 6, 3, tzinfo=UTC)
EARLIER = datetime(2026, 6, 1, tzinfo=UTC)


@test()
async def captions_unavailable_falls_through_to_library() -> None:
    """The worker stores a library transcript when captions has none."""
    captions = FakeSource({"v1": [UNAVAILABLE]}, name="captions")
    library = FakeSource({"v1": [Ok("library body")]}, name="library")
    env = await load_fixture(make_env(captions, library))
    await seed(env.db, "v1", liked_at=LATER)

    report = await env.worker.sync(logger=test_logger())

    assert_eq(report.fetched, 1)
    assert_eq(await transcript_of(env.db, "v1"), "library body")


@test()
async def both_unavailable_marks_terminal() -> None:
    """A video goes terminal only when captions and the library both have nothing."""
    captions = FakeSource({"v1": [UNAVAILABLE]}, name="captions")
    library = FakeSource({"v1": [UNAVAILABLE]}, name="library")
    env = await load_fixture(make_env(captions, library))
    await seed(env.db, "v1", liked_at=LATER)

    report = await env.worker.sync(logger=test_logger())

    assert_eq(report.unavailable, 1)
    persisted = await state_of(env.db, "v1")
    assert_eq(persisted.status if persisted is not None else None, "terminal")


@test()
async def a_block_pauses_the_provider_and_increments_the_streak() -> None:
    """An IP block sets a future paused-until, bumps the streak, leaves video pending."""
    captions = FakeSource({"v1": [UNAVAILABLE]}, name="captions")
    library = FakeSource({"v1": [Blocked()]}, name="library")
    env = await load_fixture(make_env(captions, library))
    await seed(env.db, "v1", liked_at=LATER)

    report = await env.worker.sync(logger=test_logger())

    assert_eq(report.blocked, 1)
    assert_eq(report.paused, True)
    pause = await _load_provider_pause(env.db, source="library")
    assert_eq(pause.streak, 1)
    # base = 10 minutes for the first block, in the future relative to now.
    assert_eq(pause.paused_until, env.clock.now() + timedelta(minutes=10))
    # A blocked video is never terminal — it stays pending for after the cooldown.
    assert_is_none(await state_of(env.db, "v1"))


@test()
async def captions_keep_flowing_while_the_library_is_paused() -> None:
    """While paused the worker skips the library but still stores captions hits."""
    # v_block trips the pause; v_caption is then processed in the same pass with the
    # library skipped, so its captions transcript still lands and the library is not
    # called for it.
    captions = FakeSource(
        {"v_block": [UNAVAILABLE], "v_caption": [Ok("captions body")]},
        name="captions",
    )
    library = FakeSource(
        {"v_block": [Blocked()], "v_caption": [Ok("library body")]},
        name="library",
    )
    env = await load_fixture(make_env(captions, library))
    await seed(env.db, "v_block", liked_at=LATER)
    await seed(env.db, "v_caption", liked_at=EARLIER)

    report = await env.worker.sync(logger=test_logger())

    assert_eq(report.paused, True)
    assert_eq(await transcript_of(env.db, "v_caption"), "captions body")
    # The library was never called for the captions-served video while paused.
    assert_eq(env.library.calls.get("v_caption"), None)
    # The blocked video stays pending.
    assert_is_none(await transcript_of(env.db, "v_block"))


@test()
async def the_pause_escalates_across_consecutive_blocks() -> None:
    """A second consecutive block doubles the cooldown (exponential in the streak)."""
    captions = FakeSource({"v1": [UNAVAILABLE]}, name="captions")
    library = FakeSource({"v1": [Blocked()]}, name="library")
    env = await load_fixture(make_env(captions, library))
    await seed(env.db, "v1", liked_at=LATER)

    _ = await env.worker.sync(logger=test_logger())
    first = await _load_provider_pause(env.db, source="library")
    assert_eq(first.streak, 1)
    # base = 10 minutes for the first block.
    assert_eq(first.paused_until, env.clock.now() + timedelta(minutes=10))

    # Wait out the first cooldown, then block again.
    env.clock.advance(timedelta(minutes=11))
    _ = await env.worker.sync(logger=test_logger())
    second = await _load_provider_pause(env.db, source="library")
    assert_eq(second.streak, 2)
    # base * 2**(2-1) = 20 minutes for the second consecutive block.
    assert_eq(second.paused_until, env.clock.now() + timedelta(minutes=20))


@test()
async def the_pause_honors_a_retry_after_hint() -> None:
    """A retry-after hint larger than the escalated cooldown wins."""
    captions = FakeSource({"v1": [UNAVAILABLE]}, name="captions")
    library = FakeSource(
        {"v1": [Blocked(retry_after=timedelta(hours=2))]}, name="library"
    )
    env = await load_fixture(make_env(captions, library))
    await seed(env.db, "v1", liked_at=LATER)

    _ = await env.worker.sync(logger=test_logger())

    pause = await _load_provider_pause(env.db, source="library")
    # First block's exponential cooldown is 10 minutes, but the 2h hint is honored.
    assert_eq(pause.paused_until, env.clock.now() + timedelta(hours=2))


@test()
async def the_pause_survives_a_fresh_worker() -> None:
    """Paused-until and streak are persisted, so a restart keeps skipping the library."""
    captions = FakeSource({"v1": [UNAVAILABLE], "v2": [UNAVAILABLE]}, name="captions")
    # v1 blocks once then recovers, so it does not perpetually re-trip the pause and
    # starve v2 on later passes.
    library = FakeSource(
        {"v1": [Blocked(), Ok("v1 body")], "v2": [Ok("library body")]},
        name="library",
    )
    env = await load_fixture(make_env(captions, library))
    await seed(env.db, "v1", liked_at=LATER)
    _ = await env.worker.sync(logger=test_logger())

    # A fresh worker over the same database stands in for a restart.
    restarted = TranscriptSyncService(
        database=env.db,
        client=env.client,
        provider=FallbackTranscriptProvider(env.captions, fallbacks=[env.library]),
        config=env.config,
    )
    await seed(env.db, "v2", liked_at=EARLIER)
    _ = await restarted.sync(logger=test_logger())

    # Still paused: the library is skipped for v2, so it stays pending.
    assert_eq(env.library.calls.get("v2"), None)
    assert_is_none(await transcript_of(env.db, "v2"))

    # Once the cooldown elapses, the library runs again and v2 is transcribed.
    env.clock.advance(timedelta(minutes=11))
    _ = await restarted.sync(logger=test_logger())
    assert_eq(await transcript_of(env.db, "v2"), "library body")


@test()
async def a_clean_success_resets_the_streak() -> None:
    """After the block clears, a clean library fetch returns backoff to baseline."""
    captions = FakeSource({"v1": [UNAVAILABLE]}, name="captions")
    library = FakeSource({"v1": [Blocked(), Ok("eventually")]}, name="library")
    env = await load_fixture(make_env(captions, library))
    await seed(env.db, "v1", liked_at=LATER)

    _ = await env.worker.sync(logger=test_logger())
    assert_eq((await _load_provider_pause(env.db, source="library")).streak, 1)

    env.clock.advance(timedelta(minutes=11))
    _ = await env.worker.sync(logger=test_logger())

    assert_eq(await transcript_of(env.db, "v1"), "eventually")
    cleared = await _load_provider_pause(env.db, source="library")
    assert_eq(cleared.streak, 0)
    assert_is_none(cleared.paused_until)


@test()
async def a_per_video_transient_does_not_trip_the_global_pause() -> None:
    """A transient library failure uses per-video backoff, not the provider pause."""
    captions = FakeSource({"v1": [UNAVAILABLE]}, name="captions")
    library = FakeSource({"v1": [TRANSIENT]}, name="library")
    env = await load_fixture(make_env(captions, library))
    await seed(env.db, "v1", liked_at=LATER)

    report = await env.worker.sync(logger=test_logger())

    assert_eq(report.retried, 1)
    assert_eq(report.paused, False)
    pause = await _load_provider_pause(env.db, source="library")
    assert_eq(pause.streak, 0)
    assert_is_none(pause.paused_until)
    persisted = await state_of(env.db, "v1")
    assert_eq(persisted.status if persisted is not None else None, "retry")


# --- Worker integration with a paid Supadata fallback in the chain -----------


@dataclass
class Env3:
    """A worker over a real composite of captions -> library -> supadata sources."""

    worker: TranscriptSyncService
    db: Database
    client: YouTubeApiClient
    captions: FakeSource
    library: FakeSource
    supadata: FakeSource
    clock: FakeClock
    config: TranscriptSyncConfig


@fixture
async def make_env3(
    captions: FakeSource,
    library: FakeSource,
    supadata: FakeSource,
) -> AsyncGenerator[Env3]:
    """A worker over `FallbackTranscriptProvider(captions, [library, supadata])`."""
    db = await Database.initialize(backend=Config(database=":memory:"))
    await create_youtube_schema(db)
    clock = FakeClock(datetime(2026, 6, 1, 12, 0, tzinfo=UTC))
    client = YouTubeApiClient(
        InMemoryYouTubeApi(), DailyQuota(db, limit=1000), clock=clock
    )
    config = TranscriptSyncConfig(
        block_pause_base=timedelta(minutes=10),
        block_pause_cap=timedelta(hours=4),
    )
    provider = FallbackTranscriptProvider(captions, fallbacks=[library, supadata])
    worker = TranscriptSyncService(
        database=db, client=client, provider=provider, config=config
    )
    yield Env3(
        worker=worker,
        db=db,
        client=client,
        captions=captions,
        library=library,
        supadata=supadata,
        clock=clock,
        config=config,
    )
    await db.close()


@test()
async def supadata_covers_what_the_free_sources_miss() -> None:
    """When captions and the library have nothing, Supadata's transcript is stored, tagged."""
    captions = FakeSource({"v1": [UNAVAILABLE]}, name="captions")
    library = FakeSource({"v1": [UNAVAILABLE]}, name="library")
    supadata = FakeSource({"v1": [Ok("supadata body")]}, name="supadata")
    env = await load_fixture(make_env3(captions, library, supadata))
    await seed(env.db, "v1", liked_at=LATER)

    report = await env.worker.sync(logger=test_logger())

    assert_eq(report.fetched, 1)
    assert_eq(await transcript_of(env.db, "v1"), "supadata body")
    persisted = await state_of(env.db, "v1")
    assert_eq(persisted.status if persisted is not None else None, "done")


@test()
async def caption_unavailable_videos_skip_supadata_but_try_library() -> None:
    """Manual-caption false saves Supadata spend while the library can still fetch."""
    captions = FakeSource({"v1": [UNAVAILABLE]}, name="captions")
    library = FakeSource({"v1": [Ok("library body")]}, name="library")
    supadata = FakeSource({"v1": [Ok("supadata body")]}, name="supadata")
    env = await load_fixture(make_env3(captions, library, supadata))
    await seed(env.db, "v1", liked_at=LATER, caption_available=0)

    report = await env.worker.sync(logger=test_logger())

    assert_eq(report.fetched, 1)
    assert_eq(await transcript_of(env.db, "v1"), "library body")
    assert_eq(env.supadata.calls.get("v1"), None)


@test()
async def terminal_requires_supadata_to_also_be_unavailable() -> None:
    """A video goes terminal only when captions, library, and Supadata all have nothing."""
    captions = FakeSource({"v1": [UNAVAILABLE]}, name="captions")
    library = FakeSource({"v1": [UNAVAILABLE]}, name="library")
    supadata = FakeSource({"v1": [UNAVAILABLE]}, name="supadata")
    env = await load_fixture(make_env3(captions, library, supadata))
    await seed(env.db, "v1", liked_at=LATER)

    report = await env.worker.sync(logger=test_logger())

    assert_eq(report.unavailable, 1)
    # Every configured source was consulted before giving up.
    assert_eq(env.supadata.calls.get("v1"), 1)
    persisted = await state_of(env.db, "v1")
    assert_eq(persisted.status if persisted is not None else None, "terminal")


@test()
async def a_supadata_rate_limit_pauses_only_supadata() -> None:
    """A Supadata block pauses Supadata while the free library keeps serving videos."""
    # v_supa trips the Supadata pause (captions + library miss it, Supadata blocks);
    # v_lib is then served by the library in the same pass, proving the library is
    # not caught up in Supadata's pause.
    captions = FakeSource(
        {"v_supa": [UNAVAILABLE], "v_lib": [UNAVAILABLE]}, name="captions"
    )
    library = FakeSource(
        {"v_supa": [UNAVAILABLE], "v_lib": [Ok("library body")]}, name="library"
    )
    supadata = FakeSource({"v_supa": [Blocked()]}, name="supadata")
    env = await load_fixture(make_env3(captions, library, supadata))
    await seed(env.db, "v_supa", liked_at=LATER)
    await seed(env.db, "v_lib", liked_at=EARLIER)

    report = await env.worker.sync(logger=test_logger())

    assert_eq(report.paused, True)
    # Supadata is paused; the library still served the other video.
    assert_eq(await transcript_of(env.db, "v_lib"), "library body")
    supadata_pause = await _load_provider_pause(env.db, source="supadata")
    assert_eq(supadata_pause.streak, 1)
    assert_eq(supadata_pause.paused_until, env.clock.now() + timedelta(minutes=10))
    # The library never tripped a pause of its own.
    library_pause = await _load_provider_pause(env.db, source="library")
    assert_eq(library_pause.streak, 0)
    assert_is_none(library_pause.paused_until)
    # The Supadata-blocked video stays pending for after the cooldown.
    assert_is_none(await transcript_of(env.db, "v_supa"))


@test()
async def supadata_is_skipped_while_paused_then_runs_after_cooldown() -> None:
    """While Supadata is paused it is skipped; once the cooldown elapses it runs again."""
    captions = FakeSource({"v1": [UNAVAILABLE]}, name="captions")
    library = FakeSource({"v1": [UNAVAILABLE]}, name="library")
    # Supadata blocks first, then (after the cooldown) yields a transcript.
    supadata = FakeSource({"v1": [Blocked(), Ok("eventually")]}, name="supadata")
    env = await load_fixture(make_env3(captions, library, supadata))
    await seed(env.db, "v1", liked_at=LATER)

    _ = await env.worker.sync(logger=test_logger())
    # Paused: the video is still pending and not terminal.
    assert_is_none(await transcript_of(env.db, "v1"))
    assert_is_none(await state_of(env.db, "v1"))

    env.clock.advance(timedelta(minutes=11))
    _ = await env.worker.sync(logger=test_logger())

    assert_eq(await transcript_of(env.db, "v1"), "eventually")
    cleared = await _load_provider_pause(env.db, source="supadata")
    assert_eq(cleared.streak, 0)
    assert_is_none(cleared.paused_until)
