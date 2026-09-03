"""Behavior tests for shared transcript acquisition policy and persistence."""

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from snekok.result import Err, Ok
from snekql.sqlite import Config, Database
from snektest import assert_eq, assert_false, assert_isinstance, test

from tether.transcripts import (
    TranscriptionAvailable,
    TranscriptionKey,
    TranscriptionStore,
    create_transcript_schema,
)
from tether.transcripts.acquisition import (
    TranscriptAcquisitionConfig,
    TranscriptAcquisitionService,
)
from tether.transcripts.contracts import (
    FetchedTranscript,
    TranscriptAcquisitionDeferred,
    TranscriptBlockedFailure,
    TranscriptFetchResult,
    TranscriptionTarget,
    TranscriptNeedsReview,
    TranscriptProviderBlocked,
    TranscriptProviderChain,
    TranscriptRetryScheduled,
    TranscriptStored,
    TranscriptTransientFailure,
    TranscriptUnavailableFailure,
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

    async def fetch(self, locator: str) -> TranscriptFetchResult:
        _ = locator
        await asyncio.sleep(0)
        outcome = self._outcomes[min(self.calls, len(self._outcomes) - 1)]
        self.calls += 1
        return outcome


async def _database() -> Database:
    database = await Database.initialize(backend=Config(database=":memory:"))
    await create_transcript_schema(database)
    return database


def _target(locator: str = "provider-media-id") -> TranscriptionTarget:
    return TranscriptionTarget(key=TranscriptionKey(f"test:{locator}"), locator=locator)


async def _stored_text(database: Database, target: TranscriptionTarget) -> str | None:
    state = await TranscriptionStore(database).read(target.key)
    return state.transcript.text if isinstance(state, TranscriptionAvailable) else None


@test()
async def acquisition_does_not_require_a_youtube_video() -> None:
    """Acquisition persists against a generic target without YouTube storage."""
    database = await _database()
    source = ScriptedSource(
        "youtube_transcript_api",
        [Ok(FetchedTranscript(source="youtube_transcript_api", text="hello"))],
    )
    acquisition = TranscriptAcquisitionService(
        database=database,
        provider=TranscriptProviderChain([source]),
    )
    target = _target("standup")

    outcome = await acquisition.acquire(target, now=_NOW)

    _ = assert_isinstance(outcome, TranscriptStored)
    assert_eq(await _stored_text(database, target), "hello")
    await database.close()


@test()
async def successful_acquisition_stores_transcript_once() -> None:
    """The shared service coalesces concurrent requests around one source call."""
    database = await _database()
    target = _target()
    source = ScriptedSource(
        "youtube_transcript_api",
        [Ok(FetchedTranscript(source="youtube_transcript_api", text="hello"))],
    )
    acquisition = TranscriptAcquisitionService(
        database=database,
        provider=TranscriptProviderChain([source]),
    )

    outcomes = await asyncio.gather(
        acquisition.acquire(target, now=_NOW),
        acquisition.acquire(target, now=_NOW),
    )

    stored = [assert_isinstance(outcome, TranscriptStored) for outcome in outcomes]
    assert_eq(source.calls, 1)
    assert_eq(sorted(outcome.cached for outcome in stored), [False, True])
    assert_eq(await _stored_text(database, target), "hello")
    await database.close()


@test()
async def upstream_block_is_persisted_and_honored_on_demand() -> None:
    """Every caller observes the same provider cooldown after one real block."""
    database = await _database()
    target = _target()
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

    blocked = await acquisition.acquire(target, now=_NOW)
    deferred = await acquisition.acquire(target, now=_NOW)

    _ = assert_isinstance(blocked, TranscriptProviderBlocked)
    _ = assert_isinstance(deferred, TranscriptAcquisitionDeferred)
    assert_eq(source.calls, 1)
    await database.close()


@test()
async def exhausted_sources_request_human_review() -> None:
    """Permanent absence is persisted as a review decision, not an exception."""
    database = await _database()
    target = _target()
    source = ScriptedSource(
        "youtube_transcript_api",
        [Err(TranscriptUnavailableFailure(locator=target.locator))],
    )
    acquisition = TranscriptAcquisitionService(
        database=database,
        provider=TranscriptProviderChain([source]),
    )

    outcome = await acquisition.acquire(target, now=_NOW)

    _ = assert_isinstance(outcome, TranscriptNeedsReview)
    assert_false(isinstance(outcome, Exception))
    await database.close()


@test()
async def transient_failure_returns_the_typed_retry_deadline() -> None:
    """Retry scheduling remains observable without optional attempt fields."""
    database = await _database()
    target = _target()
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

    outcome = await acquisition.acquire(target, now=_NOW)

    retry = assert_isinstance(outcome, TranscriptRetryScheduled)
    assert_eq(retry.next_attempt_at, _NOW + timedelta(minutes=10))
    await database.close()
