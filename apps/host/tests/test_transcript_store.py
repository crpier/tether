"""Behavior tests for source-independent Transcription persistence."""

from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import cast

from pydantic import ValidationError
from snekok.validation import validate_python_unsafe
from snekql.sqlite import Config, Database
from snektest import (
    Param,
    assert_eq,
    assert_is_none,
    assert_isinstance,
    assert_raises,
    fixture,
    load_fixture,
    test,
)

from tether.transcripts import (
    TranscriptionAvailable,
    TranscriptionKey,
    TranscriptionRetrying,
    TranscriptionStore,
    TranscriptionUnavailable,
    TranscriptSegment,
    create_transcript_schema,
)


@fixture
async def transcript_database() -> AsyncGenerator[Database]:
    """Create transcript storage without creating any Integration schema."""
    database = await Database.initialize(backend=Config(database=":memory:"))
    await create_transcript_schema(database)
    yield database
    await database.close()


@test()
async def transcript_can_be_stored_without_a_youtube_video() -> None:
    """A generic Transcription key is enough to persist transcript content."""
    database = await load_fixture(transcript_database())
    store = TranscriptionStore(database)
    key = TranscriptionKey("document:meeting-notes")

    await store.save_available(
        key,
        source="manual",
        text="Discuss the launch plan",
        segments=(),
    )

    state = assert_isinstance(await store.read(key), TranscriptionAvailable)
    assert_eq(state.key, key)
    assert_eq(state.transcript.text, "Discuss the launch plan")


@test()
async def available_transcript_replaces_retry_state() -> None:
    """Success keeps Transcription identity and clears its acquisition failure."""
    database = await load_fixture(transcript_database())
    store = TranscriptionStore(database)
    key = TranscriptionKey("recording:weekly-sync")
    segment = validate_python_unsafe(
        TranscriptSegment,
        {"text": "Hello", "start_ms": 1250, "duration_ms": 800},
    )
    await store.save_retrying(
        key,
        failed_attempts=2,
        last_error="temporary outage",
        next_attempt_at=datetime(2026, 9, 2, 10, 30, tzinfo=UTC),
    )
    retrying = assert_isinstance(await store.read(key), TranscriptionRetrying)

    await store.save_available(
        key,
        source="provider",
        text="Hello",
        segments=(segment,),
    )

    available = assert_isinstance(await store.read(key), TranscriptionAvailable)
    assert_eq(available.created_at, retrying.created_at)
    assert_eq(available.transcript.text, "Hello")
    assert_eq(available.transcript.segments, (segment,))


@test()
async def unavailable_transcription_removes_completed_content() -> None:
    """Settled absence cannot leave an old Transcript attached."""
    database = await load_fixture(transcript_database())
    store = TranscriptionStore(database)
    key = TranscriptionKey("audio:lost-recording")
    await store.save_available(
        key,
        source="provider",
        text="Old transcript",
        segments=(),
    )

    await store.save_unavailable(
        key,
        failed_attempts=3,
        last_error="providers exhausted",
    )

    unavailable = assert_isinstance(await store.read(key), TranscriptionUnavailable)
    assert_eq(unavailable.failed_attempts, 3)
    assert_eq(unavailable.last_error, "providers exhausted")


@test()
async def available_transcript_requires_exact_segment_duration() -> None:
    """Timed Transcript writes reject a cue without provider-reported duration."""
    database = await load_fixture(transcript_database())
    store = TranscriptionStore(database)
    incomplete = cast(
        "tuple[TranscriptSegment, ...]",
        ({"text": "Missing duration", "start_ms": 0},),
    )

    with assert_raises(ValidationError):
        await store.save_available(
            TranscriptionKey("audio:incomplete"),
            source="provider",
            text="Missing duration",
            segments=incomplete,
        )


@test()
async def restarted_transcription_returns_to_pending() -> None:
    """Restart removes materialized state without needing source media storage."""
    database = await load_fixture(transcript_database())
    store = TranscriptionStore(database)
    key = TranscriptionKey("document:retry")
    await store.save_review_needed(
        key,
        failed_attempts=1,
        last_error="not found",
    )

    await store.restart(key)

    assert_is_none(await store.read(key))


@test()
async def transcript_storage_preserves_fractional_milliseconds() -> None:
    """Timed cues retain sub-millisecond precision through JSON persistence."""
    database = await load_fixture(transcript_database())
    store = TranscriptionStore(database)
    key = TranscriptionKey("recording:fractional")
    segment = validate_python_unsafe(
        TranscriptSegment,
        {"text": "Hello", "start_ms": 1250.25, "duration_ms": 500.125},
    )

    await store.save_available(
        key, source="supadata", text="Hello", segments=(segment,)
    )

    available = assert_isinstance(await store.read(key), TranscriptionAvailable)
    saved = available.transcript.segments[0]
    assert_eq(saved.start_ms, 1250.25)
    assert_eq(saved.duration_ms, 500.125)
    assert_eq(saved.end_ms, 1750.375)


@test(
    [Param("start_ms", name="start"), Param("duration_ms", name="duration")],
    [
        Param(-0.25, name="negative"),
        Param(True, name="boolean"),
        Param("0.25", name="string"),
        Param(None, name="null"),
        Param(float("nan"), name="nan"),
        Param(float("inf"), name="infinity"),
    ],
)
async def transcript_storage_rejects_invalid_timing(field: str, timing: object) -> None:
    """Persistence validates timing even when callers bypass segment construction."""
    database = await load_fixture(transcript_database())
    store = TranscriptionStore(database)
    key = TranscriptionKey("recording:invalid")
    segments = ({"text": "hello", "start_ms": 0, "duration_ms": 500, field: timing},)

    with assert_raises(ValidationError):
        await store.save_available(
            key,
            source="supadata",
            text="hello",
            segments=segments,  # ty: ignore[invalid-argument-type] deliberate invalid input
        )

    assert_is_none(await store.read(key))
