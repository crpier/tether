"""Behavior tests for the background transcript acquisition worker."""

from collections.abc import AsyncGenerator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import structlog
from snekok.result import Err, Ok
from snekok.validation import validate_python_unsafe
from snekql.sqlite import Config, Database, insert, select
from snektest import (
    assert_eq,
    assert_false,
    assert_isinstance,
    assert_true,
    fixture,
    load_fixture,
    test,
)

from tether.structured_logging import Logger
from tether.transcripts.acquisition import (
    TranscriptAcquisitionConfig,
    TranscriptAcquisitionService,
)
from tether.transcripts.contracts import (
    FetchedTranscript,
    TranscriptFetchResult,
    TranscriptProviderChain,
    TranscriptSegment,
    TranscriptTransientFailure,
    TranscriptUnavailableFailure,
)
from tether.transcripts.worker import (
    TranscriptSyncConfig,
    TranscriptSyncService,
)
from tether.youtube.store import (
    IngestedVideo,
    TranscriptAvailable,
    TranscriptRetrying,
    TranscriptReviewNeeded,
    TranscriptState,
    create_youtube_schema,
    fetch_transcript_state,
)
from tether.youtube.types import VideoId


class FakeClock:
    """Controllable UTC clock."""

    def __init__(self, now: datetime) -> None:
        self._now: datetime = now

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        self._now += delta


class ScriptedSource:
    """Source returning configured outcomes and recording attempt order."""

    def __init__(self, outcomes: Sequence[TranscriptFetchResult]) -> None:
        self._outcomes: list[TranscriptFetchResult] = list(outcomes)
        self.calls: list[str] = []
        self.source: str = "youtube_transcript_api"

    async def fetch(self, video_id: str) -> TranscriptFetchResult:
        outcome = self._outcomes[min(len(self.calls), len(self._outcomes) - 1)]
        self.calls.append(video_id)
        return outcome


@dataclass(frozen=True, slots=True)
class WorkerEnv:
    """Worker collaborators backed by one in-memory database."""

    clock: FakeClock
    database: Database
    source: ScriptedSource
    worker: TranscriptSyncService


def _logger() -> Logger:
    return structlog.stdlib.get_logger("test.transcript_sync")


@fixture
async def worker_env(
    outcomes: Sequence[TranscriptFetchResult],
    *,
    acquisition_config: TranscriptAcquisitionConfig | None = None,
    sync_config: TranscriptSyncConfig | None = None,
) -> AsyncGenerator[WorkerEnv]:
    database = await Database.initialize(backend=Config(database=":memory:"))
    await create_youtube_schema(database)
    clock = FakeClock(datetime(2026, 6, 1, 12, 0, tzinfo=UTC))
    source = ScriptedSource(outcomes)
    acquisition = TranscriptAcquisitionService(
        database=database,
        provider=TranscriptProviderChain([source]),
        config=acquisition_config,
    )
    yield WorkerEnv(
        clock=clock,
        database=database,
        source=source,
        worker=TranscriptSyncService(
            acquisition=acquisition,
            clock=clock,
            database=database,
            config=sync_config,
        ),
    )
    await database.close()


async def _seed(
    database: Database,
    video_id: str,
    *,
    liked_at: datetime | None = None,
) -> None:
    async with database.transaction() as tx:
        _ = await tx.execute(
            insert(
                IngestedVideo(
                    video_id=VideoId(video_id),
                    source="liked",
                    title="Talk",
                    channel="PyConf",
                    topic="python",
                    description="",
                    liked_at=liked_at,
                )
            )
        )


async def _mark_legacy_timed(database: Database, video_id: str) -> None:
    """Give a saved video the pre-duration timed transcript representation."""
    await database.migrate(
        {
            f"test_mark_{video_id}_legacy_timed": (
                'UPDATE "ingested_video" SET '
                "\"transcript\" = 'old joined text', "
                '"transcript_segments_json" = '
                '\'[{"start_seconds":0.0,"text":"old segment"}]\' '
                f"WHERE \"video_id\" = '{video_id}'"
            )
        }
    )


async def _state(
    database: Database,
    video_id: str,
) -> TranscriptState | None:
    async with database.transaction() as tx:
        video = await tx.fetch_one_or_none(
            select(IngestedVideo).where(IngestedVideo.video_id.eq(VideoId(video_id)))
        )
        if video is None:
            return None
        return await fetch_transcript_state(tx, video.id)


@test()
async def successful_pass_stores_transcript() -> None:
    """A successful source result is persisted and counted once."""
    env = await load_fixture(
        worker_env(
            [
                Ok(
                    FetchedTranscript(
                        source="youtube_transcript_api",
                        text="hello",
                    )
                )
            ]
        )
    )
    await _seed(env.database, "video")

    report = await env.worker.sync(logger=_logger())

    assert_eq(report.fetched, 1)
    state = assert_isinstance(await _state(env.database, "video"), TranscriptAvailable)
    assert_eq(state.text, "hello")


@test()
async def legacy_timed_backfill_replaces_upstream_segmentation_exactly() -> None:
    """A duration backfill atomically adopts the provider's current cue boundaries."""
    replacement = validate_python_unsafe(
        TranscriptSegment,
        {"text": "current segment", "start_ms": 250, "duration_ms": 1750},
    )
    env = await load_fixture(
        worker_env(
            [
                Ok(
                    FetchedTranscript(
                        source="youtube_transcript_api",
                        text="current joined text",
                        segments=(replacement,),
                    )
                )
            ]
        )
    )
    await _seed(env.database, "legacy-timed")
    await _mark_legacy_timed(env.database, "legacy-timed")

    report = await env.worker.sync(logger=_logger())

    assert_eq(report.fetched, 1)
    state = assert_isinstance(
        await _state(env.database, "legacy-timed"), TranscriptAvailable
    )
    assert_eq(state.text, "current joined text")
    assert_eq(state.segments, (replacement,))


@test()
async def legacy_timed_backfill_resumes_after_a_bounded_pass() -> None:
    """A later pass continues with candidates left pending by the request limit."""
    segment = validate_python_unsafe(
        TranscriptSegment,
        {"text": "exact", "start_ms": 0, "duration_ms": 1000},
    )
    env = await load_fixture(
        worker_env(
            [
                Ok(
                    FetchedTranscript(
                        source="youtube_transcript_api",
                        text="exact",
                        segments=(segment,),
                    )
                )
            ],
            sync_config=TranscriptSyncConfig(library_requests_per_pass=1),
        )
    )
    for video_id in ("legacy-one", "legacy-two"):
        await _seed(env.database, video_id)
        await _mark_legacy_timed(env.database, video_id)

    first = await env.worker.sync(logger=_logger())
    second = await env.worker.sync(logger=_logger())

    assert_eq(first.fetched, 1)
    assert_true(first.deferred)
    assert_eq(second.fetched, 1)
    assert_eq(len(env.source.calls), 2)
    assert_isinstance(await _state(env.database, "legacy-one"), TranscriptAvailable)
    assert_isinstance(await _state(env.database, "legacy-two"), TranscriptAvailable)


@test()
async def unavailable_legacy_timed_backfill_waits_for_human_review() -> None:
    """A candidate that cannot be refetched remains explicitly inspectable."""
    env = await load_fixture(
        worker_env([Err(TranscriptUnavailableFailure(video_id="legacy-timed"))])
    )
    await _seed(env.database, "legacy-timed")
    await _mark_legacy_timed(env.database, "legacy-timed")

    report = await env.worker.sync(logger=_logger())

    assert_eq(report.needs_review, 1)
    assert_isinstance(
        await _state(env.database, "legacy-timed"), TranscriptReviewNeeded
    )


@test()
async def unavailable_transcript_waits_for_human_review() -> None:
    """Permanent source absence leaves no automatic retry deadline."""
    env = await load_fixture(
        worker_env([Err(TranscriptUnavailableFailure(video_id="video"))])
    )
    await _seed(env.database, "video")

    report = await env.worker.sync(logger=_logger())
    state = await _state(env.database, "video")

    assert_eq(report.needs_review, 1)
    state = assert_isinstance(state, TranscriptReviewNeeded)
    assert_eq(state.status, "needs_review")


@test()
async def transient_failure_persists_typed_retry_deadline() -> None:
    """Per-video retry state uses a UTC datetime rather than encoded text."""
    env = await load_fixture(
        worker_env(
            [Err(TranscriptTransientFailure(message="network"))],
            acquisition_config=TranscriptAcquisitionConfig(
                backoff_base=timedelta(minutes=10),
                backoff_cap=timedelta(hours=1),
            ),
        )
    )
    await _seed(env.database, "video")

    report = await env.worker.sync(logger=_logger())
    state = await _state(env.database, "video")

    assert_eq(report.retried, 1)
    state = assert_isinstance(state, TranscriptRetrying)
    assert_eq(
        state.next_attempt_at,
        env.clock.now() + timedelta(minutes=10),
    )


@test()
async def worker_processes_newest_likes_first() -> None:
    """A bounded pass prioritizes the most recently liked videos."""
    env = await load_fixture(
        worker_env(
            [
                Ok(
                    FetchedTranscript(
                        source="youtube_transcript_api",
                        text="hello",
                    )
                )
            ]
        )
    )
    await _seed(
        env.database,
        "old",
        liked_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    await _seed(
        env.database,
        "new",
        liked_at=datetime(2026, 2, 1, tzinfo=UTC),
    )

    _ = await env.worker.sync(logger=_logger())

    assert_eq(env.source.calls, ["new", "old"])


@test()
async def local_pass_limit_defers_without_pausing_source() -> None:
    """Worker capacity does not masquerade as an upstream provider block."""
    env = await load_fixture(
        worker_env(
            [
                Ok(
                    FetchedTranscript(
                        source="youtube_transcript_api",
                        text="hello",
                    )
                )
            ],
            sync_config=TranscriptSyncConfig(library_requests_per_pass=2),
        )
    )
    for video_id in ("v1", "v2", "v3", "v4"):
        await _seed(env.database, video_id)

    report = await env.worker.sync(logger=_logger())

    assert_eq(len(env.source.calls), 2)
    assert_eq(report.fetched, 2)
    assert_true(report.deferred)
    assert_false(report.paused)


@test()
async def fresh_pass_gets_a_fresh_library_limit() -> None:
    """A later pass continues pending work without waiting for a fake cooldown."""
    env = await load_fixture(
        worker_env(
            [
                Ok(
                    FetchedTranscript(
                        source="youtube_transcript_api",
                        text="hello",
                    )
                )
            ],
            sync_config=TranscriptSyncConfig(library_requests_per_pass=1),
        )
    )
    await _seed(env.database, "v1")
    await _seed(env.database, "v2")

    first = await env.worker.sync(logger=_logger())
    second = await env.worker.sync(logger=_logger())

    assert_eq(first.fetched, 1)
    assert_eq(second.fetched, 1)
    assert_eq(len(env.source.calls), 2)


@test()
async def consecutive_transients_stop_the_pass() -> None:
    """The storm breaker bounds repeated retryable upstream failures."""
    env = await load_fixture(
        worker_env(
            [Err(TranscriptTransientFailure(message="network"))],
            sync_config=TranscriptSyncConfig(transient_storm_threshold=2),
        )
    )
    for video_id in ("v1", "v2", "v3"):
        await _seed(env.database, video_id)

    report = await env.worker.sync(logger=_logger())

    assert_eq(report.retried, 2)
    assert_true(report.transient_storm)
    assert_eq(len(env.source.calls), 2)
