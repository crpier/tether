"""Behavior tests for persisted YouTube transcript state transitions."""

from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import cast
from uuid import uuid7

from pydantic import UUID7, ValidationError
from snekok.validation import validate_python_unsafe
from snekql.sqlite import Config, Database, insert, select
from snektest import (
    assert_eq,
    assert_is_none,
    assert_isinstance,
    assert_raises,
    fixture,
    load_fixture,
    test,
)

from tether.transcripts.contracts import TranscriptSegment
from tether.youtube.store import (
    IngestedVideo,
    TranscriptAvailable,
    TranscriptRetrying,
    TranscriptUnavailable,
    YouTubeTranscript,
    _youtube_migrations,
    create_youtube_schema,
    fetch_transcript_state,
    write_transcript_available,
    write_transcript_retrying,
    write_transcript_unavailable,
)
from tether.youtube.types import VideoId


@fixture
async def transcript_video() -> AsyncGenerator[tuple[Database, UUID7]]:
    """Create a saved video whose transcript state can transition."""
    database = await Database.initialize(backend=Config(database=":memory:"))
    await create_youtube_schema(database)
    await database.verify([YouTubeTranscript], policy="strict")
    async with database.transaction() as transaction:
        video = await transaction.execute(
            insert(
                IngestedVideo(
                    video_id=VideoId("video-1"),
                    source="liked",
                    title="Async Python",
                    channel="PyCon",
                    topic="python",
                    description="",
                )
            ).returning()
        )
    yield database, video.id
    await database.close()


@test()
async def retrying_transition_inserts_retry_state() -> None:
    """A first transient failure persists its retry schedule and diagnosis."""
    database, ingested_video_id = await load_fixture(transcript_video())
    next_attempt_at = datetime(2026, 9, 2, 10, 30, tzinfo=UTC)

    async with database.transaction() as transaction:
        await write_transcript_retrying(
            transaction,
            ingested_video_id,
            failed_attempts=1,
            last_error="provider timeout",
            next_attempt_at=next_attempt_at,
        )
        state = assert_isinstance(
            await fetch_transcript_state(transaction, ingested_video_id),
            TranscriptRetrying,
        )

    assert_eq(state.ingested_video_id, ingested_video_id)
    assert_eq(state.failed_attempts, 1)
    assert_eq(state.last_error, "provider timeout")
    assert_eq(state.next_attempt_at, next_attempt_at)


@test()
async def available_transition_replaces_retry_state_with_content() -> None:
    """Fetched content settles a retrying transcript without changing its identity."""
    database, ingested_video_id = await load_fixture(transcript_video())
    segment = validate_python_unsafe(
        TranscriptSegment,
        {"text": "Hello world", "start_ms": 1250, "duration_ms": 800},
    )

    async with database.transaction() as transaction:
        await write_transcript_retrying(
            transaction,
            ingested_video_id,
            failed_attempts=2,
            last_error="temporary outage",
            next_attempt_at=datetime(2026, 9, 2, 10, 30, tzinfo=UTC),
        )
        retrying = assert_isinstance(
            await fetch_transcript_state(transaction, ingested_video_id),
            TranscriptRetrying,
        )
        await write_transcript_available(
            transaction,
            ingested_video_id,
            text="Hello world",
            segments=(segment,),
        )
        available = assert_isinstance(
            await fetch_transcript_state(transaction, ingested_video_id),
            TranscriptAvailable,
        )

    assert_eq(available.ingested_video_id, ingested_video_id)
    assert_eq(available.created_at, retrying.created_at)
    assert_eq(available.text, "Hello world")
    assert_eq(available.segments, (segment,))


@test()
async def unavailable_transition_replaces_content_with_permanent_failure() -> None:
    """A permanent failure settles acquisition and retains its diagnosis."""
    database, ingested_video_id = await load_fixture(transcript_video())

    async with database.transaction() as transaction:
        await write_transcript_available(
            transaction,
            ingested_video_id,
            text="Previously fetched transcript",
            segments=(),
        )
        available = assert_isinstance(
            await fetch_transcript_state(transaction, ingested_video_id),
            TranscriptAvailable,
        )
        await write_transcript_unavailable(
            transaction,
            ingested_video_id,
            failed_attempts=3,
            last_error="providers exhausted",
        )
        unavailable = assert_isinstance(
            await fetch_transcript_state(transaction, ingested_video_id),
            TranscriptUnavailable,
        )

    assert_eq(unavailable.ingested_video_id, ingested_video_id)
    assert_eq(unavailable.created_at, available.created_at)
    assert_eq(unavailable.failed_attempts, 3)
    assert_eq(unavailable.last_error, "providers exhausted")


@test()
async def available_transition_rejects_segments_without_duration() -> None:
    """New timed-segment writes require exact provider-reported duration."""
    database, ingested_video_id = await load_fixture(transcript_video())
    incomplete = cast(
        "tuple[TranscriptSegment, ...]",
        ({"text": "Missing duration", "start_ms": 0},),
    )

    async with database.transaction() as transaction:
        with assert_raises(ValidationError):
            await write_transcript_available(
                transaction,
                ingested_video_id,
                text="Missing duration",
                segments=incomplete,
            )


@test()
async def schema_migrates_legacy_content_and_retry_state() -> None:
    """The cutover preserves text and repairs incomplete historical failures."""
    database = await Database.initialize(backend=Config(database=":memory:"))
    available_id = str(uuid7())
    retrying_id = str(uuid7())
    timed_id = str(uuid7())
    legacy_migrations: dict[str, str] = {}
    for name, statement in _youtube_migrations().items():
        if name.startswith("013_"):
            break
        legacy_migrations[name] = statement
    seed_statements = [
        (
            'INSERT INTO "ingested_video" ('
            '"id", "video_id", "source", "title", "channel", "topic", '
            '"description", "transcript", "transcript_source") VALUES '
            f"('{available_id}', 'available-video', 'liked', 'Title', 'Channel', "
            "'python', '', 'legacy text', 'legacy-provider')"
        ),
        (
            'INSERT INTO "ingested_video" ('
            '"id", "video_id", "source", "title", "channel", "topic", '
            '"description") VALUES '
            f"('{retrying_id}', 'retrying-video', 'liked', 'Title', 'Channel', "
            "'python', '')"
        ),
        (
            'INSERT INTO "ingested_video" ('
            '"id", "video_id", "source", "title", "channel", "topic", '
            '"description", "transcript", "transcript_segments_json") VALUES '
            f"('{timed_id}', 'timed-video', 'liked', 'Title', 'Channel', "
            "'python', '', 'legacy timed text', "
            '\'[{"start_seconds":0.0,"text":"legacy timed text"}]\')'
        ),
        (
            'INSERT INTO "you_tube_transcript_state" ('
            '"video_id", "status", "attempts", "next_attempt_at", "last_error") '
            "VALUES ('retrying-video', 'retrying', 0, NULL, NULL)"
        ),
    ]
    await database.migrate(legacy_migrations)
    async with database.transaction(mode="immediate") as transaction:
        connection = transaction.require_connection()
        for statement in seed_statements:
            cursor = await connection.execute(statement, ())
            await cursor.close()

    await create_youtube_schema(database)

    async with database.transaction() as transaction:
        available_video = await transaction.fetch_one_or_none(
            select(IngestedVideo).where(
                IngestedVideo.video_id.eq(VideoId("available-video"))
            )
        )
        retrying_video = await transaction.fetch_one_or_none(
            select(IngestedVideo).where(
                IngestedVideo.video_id.eq(VideoId("retrying-video"))
            )
        )
        timed_video = await transaction.fetch_one_or_none(
            select(IngestedVideo).where(
                IngestedVideo.video_id.eq(VideoId("timed-video"))
            )
        )
        assert available_video is not None
        assert retrying_video is not None
        assert timed_video is not None
        available = assert_isinstance(
            await fetch_transcript_state(transaction, available_video.id),
            TranscriptAvailable,
        )
        retrying = assert_isinstance(
            await fetch_transcript_state(transaction, retrying_video.id),
            TranscriptRetrying,
        )
        timed = await fetch_transcript_state(transaction, timed_video.id)

    assert_eq(available.text, "legacy text")
    assert_eq(available.source, "legacy-provider")
    assert_eq(available.segments, ())
    assert_eq(retrying.failed_attempts, 1)
    assert_eq(retrying.last_error, "retrying-video")
    assert_is_none(timed)
    await database.close()
