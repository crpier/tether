"""Migration tests for legacy YouTube-owned transcript rows."""

from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from uuid import uuid7

from snekql.sqlite import Config, Database
from snektest import (
    assert_eq,
    assert_is_none,
    assert_isinstance,
    fixture,
    load_fixture,
    test,
)

from tether.transcripts import (
    TranscriptionAvailable,
    TranscriptionRetrying,
    TranscriptionStore,
)
from tether.transcripts.provider_health import load_all_provider_pauses
from tether.transcripts.store import transcript_migrations
from tether.youtube.store import (
    _youtube_migrations,
    legacy_youtube_transcript_migrations,
    remove_legacy_youtube_transcript_storage,
)
from tether.youtube.transcription import youtube_transcription_target
from tether.youtube.types import VideoId


@fixture
async def migrated_legacy_youtube() -> AsyncGenerator[Database]:
    """Upgrade text, retrying, and incomplete timed legacy examples."""
    database = await Database.initialize(backend=Config(database=":memory:"))
    available_id = str(uuid7())
    retrying_id = str(uuid7())
    timed_id = str(uuid7())
    legacy_migrations: dict[str, str] = {}
    for name, statement in _youtube_migrations().items():
        if name.startswith("013_"):
            break
        legacy_migrations[name] = statement
    await database.migrate(legacy_migrations)
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
        (
            'INSERT INTO "you_tube_sync_state" ("key", "value") VALUES '
            "('transcript_provider_paused_until:library', "
            "'2026-09-02T10:30:00Z')"
        ),
        (
            'INSERT INTO "you_tube_sync_state" ("key", "value") VALUES '
            "('transcript_provider_block_streak:library', '2')"
        ),
    ]
    async with database.transaction(mode="immediate") as transaction:
        connection = transaction.require_connection()
        for statement in seed_statements:
            cursor = await connection.execute(statement, ())
            await cursor.close()
    await database.migrate(
        {
            **_youtube_migrations(),
            **transcript_migrations(),
            **legacy_youtube_transcript_migrations(),
        }
    )
    await remove_legacy_youtube_transcript_storage(database)
    yield database
    await database.close()


@test()
async def text_only_youtube_content_becomes_a_transcript() -> None:
    """The cutover preserves complete old text and its provider."""
    database = await load_fixture(migrated_legacy_youtube())

    state = assert_isinstance(
        await TranscriptionStore(database).read(
            youtube_transcription_target(VideoId("available-video")).key
        ),
        TranscriptionAvailable,
    )

    assert_eq(state.transcript.text, "legacy text")
    assert_eq(state.transcript.source, "legacy-provider")
    assert_eq(state.transcript.segments, ())


@test()
async def legacy_retry_becomes_a_transcription_retry() -> None:
    """The cutover repairs and retains old acquisition failure state."""
    database = await load_fixture(migrated_legacy_youtube())

    state = assert_isinstance(
        await TranscriptionStore(database).read(
            youtube_transcription_target(VideoId("retrying-video")).key
        ),
        TranscriptionRetrying,
    )

    assert_eq(state.failed_attempts, 1)
    assert_eq(state.last_error, "retrying-video")


@test()
async def provider_pause_moves_out_of_youtube_sync_state() -> None:
    """The cutover retains provider health under Transcript-owned settings."""
    database = await load_fixture(migrated_legacy_youtube())

    pauses = await load_all_provider_pauses(database)

    assert_eq(pauses["library"].streak, 2)
    assert_eq(
        pauses["library"].paused_until,
        datetime(2026, 9, 2, 10, 30, tzinfo=UTC),
    )


@test()
async def legacy_timing_without_durations_stays_pending() -> None:
    """The cutover leaves incomplete old cues for an exact provider refetch."""
    database = await load_fixture(migrated_legacy_youtube())

    state = await TranscriptionStore(database).read(
        youtube_transcription_target(VideoId("timed-video")).key
    )

    assert_is_none(state)
