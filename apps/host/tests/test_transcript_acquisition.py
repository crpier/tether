"""Behavior tests for shared transcript acquisition policy and persistence."""

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from snekok import Err, Ok
from snekql.sqlite import Config, Database, insert, select
from snektest import assert_eq, assert_false, assert_isinstance, test

from tether.transcripts.acquisition import (
    TranscriptAcquisitionConfig,
    TranscriptAcquisitionService,
)
from tether.transcripts.contracts import (
    FetchedTranscript,
    TranscriptAcquisitionDeferred,
    TranscriptBlockedFailure,
    TranscriptFetchResult,
    TranscriptNeedsReview,
    TranscriptProviderBlocked,
    TranscriptProviderChain,
    TranscriptRetryScheduled,
    TranscriptStored,
    TranscriptTransientFailure,
    TranscriptUnavailableFailure,
)
from tether.youtube_store import (
    IngestedVideo,
    create_youtube_schema,
)

_NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


class ScriptedSource:
    """Transcript source returning configured outcomes in call order."""

    def __init__(
        self,
        source: str,
        outcomes: Sequence[TranscriptFetchResult],
    ) -> None:
        self._outcomes: list[TranscriptFetchResult] = list(outcomes)
        self.calls: int = 0
        self.source: str = source

    async def fetch(self, video_id: str) -> TranscriptFetchResult:
        _ = video_id
        await asyncio.sleep(0)
        outcome = self._outcomes[min(self.calls, len(self._outcomes) - 1)]
        self.calls += 1
        return outcome


async def _database() -> Database:
    database = await Database.initialize(backend=Config(database=":memory:"))
    await create_youtube_schema(database)
    return database


async def _seed(
    database: Database,
    video_id: str,
    *,
    caption_available: int | None = None,
) -> None:
    async with database.transaction() as tx:
        _ = await tx.execute(
            insert(
                IngestedVideo(
                    video_id=video_id,
                    source="liked",
                    title="Talk",
                    channel="PyConf",
                    topic="python",
                    description="",
                    caption_available=caption_available,
                )
            )
        )


async def _stored_text(database: Database, video_id: str) -> str | None:
    async with database.transaction() as tx:
        video = await tx.fetch_one_or_none(
            select(IngestedVideo).where(IngestedVideo.video_id.eq(video_id))
        )
    return video.transcript if video is not None else None


@test()
async def successful_acquisition_stores_transcript_once() -> None:
    """The shared service coalesces concurrent requests around one source call."""
    database = await _database()
    await _seed(database, "video")
    source = ScriptedSource(
        "youtube_transcript_api",
        [Ok(FetchedTranscript(source="youtube_transcript_api", text="hello"))],
    )
    acquisition = TranscriptAcquisitionService(
        database=database,
        provider=TranscriptProviderChain([source]),
    )

    outcomes = await asyncio.gather(
        acquisition.acquire("video", now=_NOW),
        acquisition.acquire("video", now=_NOW),
    )

    stored = [assert_isinstance(outcome, TranscriptStored) for outcome in outcomes]
    assert_eq(source.calls, 1)
    assert_eq(sorted(outcome.cached for outcome in stored), [False, True])
    assert_eq(await _stored_text(database, "video"), "hello")
    await database.close()


@test()
async def upstream_block_is_persisted_and_honored_on_demand() -> None:
    """Every caller observes the same provider cooldown after one real block."""
    database = await _database()
    await _seed(database, "video")
    source = ScriptedSource(
        "youtube_transcript_api",
        [
            Err(
                TranscriptBlockedFailure(
                    message="blocked",
                    source="youtube_transcript_api",
                )
            ),
            Ok(FetchedTranscript(source="youtube_transcript_api", text="hello")),
        ],
    )
    acquisition = TranscriptAcquisitionService(
        database=database,
        provider=TranscriptProviderChain([source]),
        config=TranscriptAcquisitionConfig(
            block_pause_base=timedelta(minutes=5),
            block_pause_cap=timedelta(hours=1),
        ),
    )

    blocked = await acquisition.acquire("video", now=_NOW)
    deferred = await acquisition.acquire("video", now=_NOW)

    _ = assert_isinstance(blocked, TranscriptProviderBlocked)
    _ = assert_isinstance(deferred, TranscriptAcquisitionDeferred)
    assert_eq(source.calls, 1)
    await database.close()


@test()
async def caption_metadata_does_not_skip_supadata() -> None:
    """Supadata remains eligible when YouTube reports no caption track."""
    database = await _database()
    await _seed(database, "video", caption_available=0)
    supadata = ScriptedSource(
        "supadata",
        [Ok(FetchedTranscript(source="supadata", text="paid"))],
    )
    library = ScriptedSource(
        "youtube_transcript_api",
        [Ok(FetchedTranscript(source="youtube_transcript_api", text="free"))],
    )
    acquisition = TranscriptAcquisitionService(
        database=database,
        provider=TranscriptProviderChain([supadata, library]),
    )

    outcome = await acquisition.acquire("video", now=_NOW)

    stored = assert_isinstance(outcome, TranscriptStored)
    assert_eq(stored.source, "supadata")
    assert_eq(supadata.calls, 1)
    assert_eq(library.calls, 0)
    await database.close()


@test()
async def exhausted_sources_request_human_review() -> None:
    """Permanent absence is persisted as a review decision, not an exception."""
    database = await _database()
    await _seed(database, "video")
    source = ScriptedSource(
        "youtube_transcript_api",
        [Err(TranscriptUnavailableFailure(video_id="video"))],
    )
    acquisition = TranscriptAcquisitionService(
        database=database,
        provider=TranscriptProviderChain([source]),
    )

    outcome = await acquisition.acquire("video", now=_NOW)

    _ = assert_isinstance(outcome, TranscriptNeedsReview)
    assert_false(isinstance(outcome, Exception))
    await database.close()


@test()
async def transient_failure_returns_the_typed_retry_deadline() -> None:
    """Retry scheduling remains observable without optional attempt fields."""
    database = await _database()
    await _seed(database, "video")
    source = ScriptedSource(
        "youtube_transcript_api",
        [Err(TranscriptTransientFailure(message="network"))],
    )
    acquisition = TranscriptAcquisitionService(
        database=database,
        provider=TranscriptProviderChain([source]),
        config=TranscriptAcquisitionConfig(
            backoff_base=timedelta(minutes=10),
            backoff_cap=timedelta(hours=1),
        ),
    )

    outcome = await acquisition.acquire("video", now=_NOW)

    retry = assert_isinstance(outcome, TranscriptRetryScheduled)
    assert_eq(retry.next_attempt_at, _NOW + timedelta(minutes=10))
    await database.close()
