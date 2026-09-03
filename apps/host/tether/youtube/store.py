"""Persisted YouTube corpus models, state transitions, and schema."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import ClassVar, Literal
from uuid import uuid7

from pydantic import UUID7
from snekql import sqlite
from snekql.sqlite import (
    CurrentTimestamp,
    Database,
    DoUpdate,
    Fetched,
    Index,
    Integer,
    Model,
    Pending,
    Text,
    Transaction,
    UtcDatetime,
    insert,
    select,
)

from tether.youtube.quota import RawYouTubeVideo
from tether.youtube.types import VideoId

type YouTubeSource = Literal["liked"]
"""Which saved list a video was ingested from.

Only ``liked`` is ever written: the YouTube Data API does not expose the Watch
Later playlist, so liked videos are the sole ingestion source today."""

type IngestState = Literal["active", "ignored"]
"""Whether an ingested video is live in browse/search or purged from it."""


class IngestedVideo[S = Pending](Model[S, "IngestedVideo[Fetched]"]):
    id: sqlite.GenCol[UUID7] = Text(primary_key=True, default_factory=uuid7)  # ty: ignore[invalid-assignment]
    video_id: sqlite.Col[VideoId] = Text(nullable=False, unique=True)
    """The upstream YouTube id; the stable identity ingestion mirrors against."""
    source: sqlite.Col[YouTubeSource] = Text()
    """Which saved list the video came from."""
    title: sqlite.Col[str] = Text()
    channel: sqlite.Col[str] = Text()
    topic: sqlite.Col[str] = Text()
    """The topic browse filters on."""
    description: sqlite.Col[str] = Text()
    """Saved-content text searched alongside the transcript."""
    ignored_at: sqlite.Col[UtcDatetime | None] = Text(default=None, nullable=True)
    """When the video was purged from ingestion; null while it is active."""
    # --- Enriched metadata (nullable; filled by sync detail fetch / import). ---
    channel_id: sqlite.Col[str | None] = Text(default=None, nullable=True)
    liked_at: sqlite.Col[UtcDatetime | None] = Text(default=None, nullable=True)
    """When the user liked the video; the ordering key for browse."""
    video_published_at: sqlite.Col[UtcDatetime | None] = Text(
        default=None, nullable=True
    )
    duration_seconds: sqlite.Col[int | None] = Integer(default=None, nullable=True)
    category_id: sqlite.Col[str | None] = Text(default=None, nullable=True)
    default_language: sqlite.Col[str | None] = Text(default=None, nullable=True)
    default_audio_language: sqlite.Col[str | None] = Text(default=None, nullable=True)
    caption_available: sqlite.Col[int | None] = Integer(default=None, nullable=True)
    privacy_status: sqlite.Col[str | None] = Text(default=None, nullable=True)
    licensed_content: sqlite.Col[int | None] = Integer(default=None, nullable=True)
    made_for_kids: sqlite.Col[int | None] = Integer(default=None, nullable=True)
    live_broadcast_content: sqlite.Col[str | None] = Text(default=None, nullable=True)
    definition: sqlite.Col[str | None] = Text(default=None, nullable=True)
    dimension: sqlite.Col[str | None] = Text(default=None, nullable=True)
    statistics_view_count: sqlite.Col[int | None] = Integer(default=None, nullable=True)
    statistics_like_count: sqlite.Col[int | None] = Integer(default=None, nullable=True)
    statistics_comment_count: sqlite.Col[int | None] = Integer(
        default=None, nullable=True
    )
    statistics_fetched_at: sqlite.Col[UtcDatetime | None] = Text(
        default=None, nullable=True
    )
    topic_categories_json: sqlite.Col[str | None] = Text(default=None, nullable=True)
    tags_json: sqlite.Col[str | None] = Text(default=None, nullable=True)
    thumbnails_json: sqlite.Col[str | None] = Text(default=None, nullable=True)
    created_at: sqlite.GenCol[UtcDatetime] = Text(default=CurrentTimestamp)
    updated_at: sqlite.GenCol[UtcDatetime] = Text(default=CurrentTimestamp)

    __indexes__: ClassVar = [Index(topic)]


def derive_ingest_state(video: IngestedVideo[Fetched]) -> IngestState:
    """Derive whether a video is live in ingestion or purged from it."""
    return "ignored" if video.ignored_at is not None else "active"


def _json_or_none(values: Sequence[str] | Mapping[str, str]) -> str | None:
    """Encode a non-empty sequence/mapping as JSON, else None."""
    return json.dumps(values) if values else None


# TODO: smells
def _bool_to_int(*, value: bool | None) -> int | None:
    """Map an optional bool onto the 0/1 integer the column stores."""
    return None if value is None else int(value)


def _new_ingested_video(raw: RawYouTubeVideo) -> IngestedVideo[Pending]:
    """Build a fresh ingested-video row from a raw upstream video (source liked)."""
    return IngestedVideo(
        video_id=raw.video_id,
        source="liked",
        title=raw.title,
        channel=raw.channel,
        topic=raw.topic,
        description=raw.description,
        channel_id=raw.channel_id,
        liked_at=raw.liked_at,
        video_published_at=raw.video_published_at,
        duration_seconds=raw.duration_seconds,
        category_id=raw.category_id,
        default_language=raw.default_language,
        default_audio_language=raw.default_audio_language,
        caption_available=_bool_to_int(value=raw.caption_available),
        privacy_status=raw.privacy_status,
        licensed_content=_bool_to_int(value=raw.licensed_content),
        made_for_kids=_bool_to_int(value=raw.made_for_kids),
        live_broadcast_content=raw.live_broadcast_content,
        definition=raw.definition,
        dimension=raw.dimension,
        statistics_view_count=raw.statistics_view_count,
        statistics_like_count=raw.statistics_like_count,
        statistics_comment_count=raw.statistics_comment_count,
        statistics_fetched_at=raw.statistics_fetched_at,
        topic_categories_json=_json_or_none(raw.topic_categories),
        tags_json=_json_or_none(raw.tags),
        thumbnails_json=_json_or_none(raw.thumbnails),
    )


async def upsert_ingested_video(tx: Transaction, raw: RawYouTubeVideo) -> bool:
    """Insert or refresh an ingested video from a raw liked video by `video_id`.

    A new id is inserted fresh; an existing one has its metadata overwritten in
    place. The local-only `ignored_at` column is left untouched, and Transcription
    state lives outside the YouTube schema, so synchronization never resurrects
    a purged video or clobbers acquisition. Shared by the background sync and
    backup importer so both mirror likes the same way. Returns whether captions
    became available, allowing the caller to restart related Transcription work.
    """
    existing = await tx.fetch_one_or_none(
        select(IngestedVideo).where(IngestedVideo.video_id.eq(raw.video_id))
    )
    await tx.execute(
        insert(_new_ingested_video(raw)).on_conflict(
            IngestedVideo.video_id,
            action=DoUpdate(
                IngestedVideo.source.to_inserted(),
                IngestedVideo.title.to_inserted(),
                IngestedVideo.channel.to_inserted(),
                IngestedVideo.topic.to_inserted(),
                IngestedVideo.description.to_inserted(),
                IngestedVideo.channel_id.to_inserted(),
                # A detail-only row must not clear an earlier liked timestamp.
                *(
                    (IngestedVideo.liked_at.to_inserted(),)
                    if raw.liked_at is not None
                    else ()
                ),
                IngestedVideo.video_published_at.to_inserted(),
                IngestedVideo.duration_seconds.to_inserted(),
                IngestedVideo.category_id.to_inserted(),
                IngestedVideo.default_language.to_inserted(),
                IngestedVideo.default_audio_language.to_inserted(),
                IngestedVideo.caption_available.to_inserted(),
                IngestedVideo.privacy_status.to_inserted(),
                IngestedVideo.licensed_content.to_inserted(),
                IngestedVideo.made_for_kids.to_inserted(),
                IngestedVideo.live_broadcast_content.to_inserted(),
                IngestedVideo.definition.to_inserted(),
                IngestedVideo.dimension.to_inserted(),
                IngestedVideo.statistics_view_count.to_inserted(),
                IngestedVideo.statistics_like_count.to_inserted(),
                IngestedVideo.statistics_comment_count.to_inserted(),
                IngestedVideo.statistics_fetched_at.to_inserted(),
                IngestedVideo.topic_categories_json.to_inserted(),
                IngestedVideo.tags_json.to_inserted(),
                IngestedVideo.thumbnails_json.to_inserted(),
                IngestedVideo.updated_at.to(CurrentTimestamp),
            ),
        )
    )
    return (
        existing is not None
        and existing.caption_available == 0
        and raw.caption_available is True
    )


# snekql replays a frozen, hand-authored migration chain and records each step by
# *name*, never re-running an applied one. The original `ingested_video` table +
# indexes are frozen verbatim under their first-shipped keys so existing
# databases skip them; enriched columns and the new bookkeeping tables arrive as
# their own forward migrations. Host composition later migrates and removes the
# historical transcript storage after the source-independent tables exist.
_INGESTED_VIDEO_COLUMNS: tuple[tuple[str, str], ...] = (
    ("channel_id", "TEXT"),
    ("liked_at", "TEXT"),
    ("video_published_at", "TEXT"),
    ("duration_seconds", "INTEGER"),
    ("category_id", "TEXT"),
    ("default_language", "TEXT"),
    ("default_audio_language", "TEXT"),
    ("caption_available", "INTEGER"),
    ("privacy_status", "TEXT"),
    ("licensed_content", "INTEGER"),
    ("made_for_kids", "INTEGER"),
    ("live_broadcast_content", "TEXT"),
    ("definition", "TEXT"),
    ("dimension", "TEXT"),
    ("statistics_view_count", "INTEGER"),
    ("statistics_like_count", "INTEGER"),
    ("statistics_comment_count", "INTEGER"),
    ("statistics_fetched_at", "TEXT"),
    ("topic_categories_json", "TEXT"),
    ("tags_json", "TEXT"),
    ("thumbnails_json", "TEXT"),
)


def _youtube_migrations() -> dict[str, str]:
    # TODO: think again about migrations. Should they be deleted after a while?
    migrations: dict[str, str] = {
        # Original table + indexes, as first shipped (#76). Frozen verbatim.
        "004_create_ingested_video": (
            'CREATE TABLE "ingested_video" ('
            '"id" TEXT PRIMARY KEY NOT NULL, '
            '"video_id" TEXT NOT NULL, '
            '"source" TEXT, "title" TEXT, "channel" TEXT, "topic" TEXT, '
            '"description" TEXT, "transcript" TEXT, "ignored_at" TEXT, '
            "\"created_at\" TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')), "
            "\"updated_at\" TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))"
            ") STRICT"
        ),
        "004_create_index_ux_ingested_video_video_id": (
            'CREATE UNIQUE INDEX "ux_ingested_video_video_id" '
            'ON "ingested_video" ("video_id")'
        ),
        "004_create_index_ix_ingested_video_topic": (
            'CREATE INDEX "ix_ingested_video_topic" ON "ingested_video" ("topic")'
        ),
    }
    # Enriched metadata columns (sync-into-cache pivot, #80).
    for column, affinity in _INGESTED_VIDEO_COLUMNS:
        migrations[f"005_ingested_video_{column}"] = (
            f'ALTER TABLE "ingested_video" ADD COLUMN "{column}" {affinity}'
        )
    # Persisted daily budget + ingestion bookkeeping (#80). Table names match the
    # snekql model-derived names (`YouTubeQuotaDaily` -> `you_tube_quota_daily`).
    migrations["006_create_you_tube_quota_daily"] = (
        'CREATE TABLE "you_tube_quota_daily" ('
        '"day" TEXT PRIMARY KEY NOT NULL, "used" INTEGER'
        ") STRICT"
    )
    migrations["007_create_you_tube_sync_state"] = (
        'CREATE TABLE "you_tube_sync_state" ('
        '"key" TEXT PRIMARY KEY NOT NULL, "value" TEXT NOT NULL'
        ") STRICT"
    )
    # Historical YouTube-owned transcript state, retained for migration replay.
    migrations["008_create_you_tube_transcript_state"] = (
        'CREATE TABLE "you_tube_transcript_state" ('
        '"video_id" TEXT PRIMARY KEY NOT NULL, '
        '"status" TEXT NOT NULL, '
        '"attempts" INTEGER, '
        '"next_attempt_at" TEXT, '
        '"last_error" TEXT, '
        "\"updated_at\" TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))"
        ") STRICT"
    )
    # Historical timed-cue storage remains in the frozen migration chain for
    # existing databases; current acquisition stores only text and provenance.
    migrations["009_ingested_video_transcript_segments_json"] = (
        'ALTER TABLE "ingested_video" ADD COLUMN "transcript_segments_json" TEXT'
    )
    migrations["009_ingested_video_transcript_source"] = (
        'ALTER TABLE "ingested_video" ADD COLUMN "transcript_source" TEXT'
    )
    migrations["010_normalize_transcript_status"] = (
        'UPDATE "you_tube_transcript_state" SET "status" = CASE "status" '
        "WHEN 'done' THEN 'available' "
        "WHEN 'retry' THEN 'retrying' "
        "WHEN 'terminal' THEN 'unavailable' "
        'ELSE "status" END'
    )
    migrations["011_normalize_transcript_state_defaults"] = (
        'UPDATE "you_tube_transcript_state" '
        'SET "attempts" = COALESCE("attempts", 0), '
        '"updated_at" = COALESCE('
        '"updated_at", '
        "strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"
        ")"
    )
    migrations["012_remove_duplicate_available_transcript_state"] = (
        'DELETE FROM "you_tube_transcript_state" WHERE "status" = \'available\''
    )
    migrations["013_create_you_tube_transcript"] = (
        'CREATE TABLE "you_tube_transcript" ('
        '"id" INTEGER PRIMARY KEY AUTOINCREMENT, '
        '"ingested_video_id" TEXT NOT NULL, '
        '"status" TEXT NOT NULL, '
        '"failed_attempts" INTEGER NOT NULL, '
        '"last_error" TEXT, '
        '"next_attempt_at" TEXT, '
        '"source" TEXT, '
        '"text" TEXT, '
        '"segments" TEXT, '
        '"created_at" TEXT NOT NULL '
        "DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')), "
        '"updated_at" TEXT NOT NULL '
        "DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')), "
        'FOREIGN KEY ("ingested_video_id") REFERENCES "ingested_video" ("id") '
        "ON DELETE CASCADE"
        ") STRICT"
    )
    migrations["013_create_index_ux_you_tube_transcript_ingested_video_id"] = (
        'CREATE UNIQUE INDEX "ux_you_tube_transcript_ingested_video_id" '
        'ON "you_tube_transcript" ("ingested_video_id")'
    )
    # Text-only historical transcripts are complete as-is. Historical timed
    # transcripts are deliberately omitted: absence from the canonical table
    # makes the worker refetch their exact provider-reported durations. Each
    # successful write then atomically installs the provider's current cue set.
    migrations["014_migrate_available_you_tube_transcripts"] = (
        'INSERT INTO "you_tube_transcript" ('
        '"ingested_video_id", "status", "failed_attempts", "source", "text", '
        '"segments") SELECT "id", \'available\', 0, '
        "NULLIF(\"transcript_source\", ''), \"transcript\", '[]' "
        'FROM "ingested_video" WHERE "transcript" IS NOT NULL '
        "AND trim(\"transcript\") <> '' "
        'AND ("transcript_segments_json" IS NULL '
        "OR trim(\"transcript_segments_json\") IN ('', '[]'))"
    )
    migrations["015_migrate_failed_you_tube_transcripts"] = (
        'INSERT INTO "you_tube_transcript" ('
        '"ingested_video_id", "status", "failed_attempts", "last_error", '
        '"next_attempt_at") SELECT video."id", state."status", '
        'CASE WHEN state."attempts" > 0 THEN state."attempts" ELSE 1 END, '
        'COALESCE(NULLIF(state."last_error", \'\'), video."video_id"), '
        "CASE WHEN state.\"status\" = 'retrying' THEN COALESCE("
        "state.\"next_attempt_at\", strftime('%Y-%m-%dT%H:%M:%fZ', 'now')) "
        "ELSE NULL END "
        'FROM "you_tube_transcript_state" AS state '
        'JOIN "ingested_video" AS video ON video."video_id" = state."video_id" '
        'WHERE NOT EXISTS (SELECT 1 FROM "you_tube_transcript" AS transcript '
        'WHERE transcript."ingested_video_id" = video."id")'
    )
    return migrations


async def create_youtube_schema(database: Database) -> None:
    """Bring the YouTube ingestion schema to current on an initialized database.

    Applies the frozen YouTube migration chain. Host schema composition then
    migrates and removes its historical transcript columns and tables after the
    source-independent Transcript schema exists.

    >>> from snekql.sqlite import Config
    >>> database = await Database.initialize(backend=Config(database=":memory:"))
    >>> await create_youtube_schema(database)
    """
    await database.migrate(_youtube_migrations())


_COPY_INGESTED_VIDEO_WITHOUT_TRANSCRIPTS_SQL = (
    'INSERT INTO "ingested_video_without_transcripts" ('
    '"id", "video_id", "source", "title", "channel", "topic", '
    '"description", "ignored_at", "channel_id", "liked_at", '
    '"video_published_at", "duration_seconds", "category_id", '
    '"default_language", "default_audio_language", "caption_available", '
    '"privacy_status", "licensed_content", "made_for_kids", '
    '"live_broadcast_content", "definition", "dimension", '
    '"statistics_view_count", "statistics_like_count", '
    '"statistics_comment_count", "statistics_fetched_at", '
    '"topic_categories_json", "tags_json", "thumbnails_json", '
    '"created_at", "updated_at") SELECT '
    '"id", "video_id", "source", "title", "channel", "topic", '
    '"description", "ignored_at", "channel_id", "liked_at", '
    '"video_published_at", "duration_seconds", "category_id", '
    '"default_language", "default_audio_language", "caption_available", '
    '"privacy_status", "licensed_content", "made_for_kids", '
    '"live_broadcast_content", "definition", "dimension", '
    '"statistics_view_count", "statistics_like_count", '
    '"statistics_comment_count", "statistics_fetched_at", '
    '"topic_categories_json", "tags_json", "thumbnails_json", '
    '"created_at", "updated_at" FROM "ingested_video"'
)


def legacy_youtube_transcript_migrations() -> dict[str, str]:
    """Return the one-time migration from YouTube rows to Transcriptions."""
    return {
        "017_migrate_youtube_transcriptions": (
            'INSERT INTO "transcription" ('
            '"id", "key", "status", "failed_attempts", "last_error", '
            '"next_attempt_at", "created_at", "updated_at") '
            'SELECT legacy."id", \'youtube:\' || video."video_id", '
            'legacy."status", legacy."failed_attempts", '
            'legacy."last_error", legacy."next_attempt_at", '
            'legacy."created_at", legacy."updated_at" '
            'FROM "you_tube_transcript" AS legacy '
            'JOIN "ingested_video" AS video '
            'ON video."id" = legacy."ingested_video_id"'
        ),
        "017_migrate_youtube_transcript_content": (
            'INSERT INTO "transcript" ('
            '"id", "transcription_id", "source", "text", "segments", '
            '"created_at", "updated_at") '
            'SELECT legacy."id", legacy."id", legacy."source", '
            'legacy."text", COALESCE(legacy."segments", \'[]\'), '
            'legacy."created_at", legacy."updated_at" '
            'FROM "you_tube_transcript" AS legacy '
            "WHERE legacy.\"status\" = 'available'"
        ),
        "017_migrate_youtube_transcript_provider_settings": (
            'INSERT INTO "transcription_setting" ("key", "value") '
            'SELECT "key", "value" FROM "you_tube_sync_state" '
            "WHERE \"key\" LIKE 'transcript_provider_paused_until:%' "
            "OR \"key\" LIKE 'transcript_provider_block_streak:%'"
        ),
        "018_drop_you_tube_transcript": 'DROP TABLE "you_tube_transcript"',
        "018_drop_you_tube_transcript_state": (
            'DROP TABLE "you_tube_transcript_state"'
        ),
        "018_remove_legacy_youtube_transcript_storage": "SELECT 1",
    }


async def migrate_legacy_youtube_transcripts(database: Database) -> None:
    """Declare the migration from old YouTube rows into Transcript storage."""
    await database.migrate(legacy_youtube_transcript_migrations())


async def remove_legacy_youtube_transcript_storage(database: Database) -> None:
    """Atomically remove migrated transcript state from the YouTube schema."""
    async with database.transaction(mode="immediate") as transaction:
        connection = transaction.require_connection()
        cursor = await connection.execute('PRAGMA table_info("ingested_video")', ())
        columns = {str(row[1]) for row in await cursor.fetchall()}
        await cursor.close()
        if "transcript" not in columns:
            return
        statements = (
            'DROP TABLE IF EXISTS "you_tube_transcript"',
            'DROP TABLE IF EXISTS "you_tube_transcript_state"',
            'CREATE TABLE "ingested_video_without_transcripts" ('
            '"id" TEXT PRIMARY KEY NOT NULL, "video_id" TEXT NOT NULL, '
            '"source" TEXT, "title" TEXT, "channel" TEXT, "topic" TEXT, '
            '"description" TEXT, "ignored_at" TEXT, "channel_id" TEXT, '
            '"liked_at" TEXT, "video_published_at" TEXT, '
            '"duration_seconds" INTEGER, "category_id" TEXT, '
            '"default_language" TEXT, "default_audio_language" TEXT, '
            '"caption_available" INTEGER, "privacy_status" TEXT, '
            '"licensed_content" INTEGER, "made_for_kids" INTEGER, '
            '"live_broadcast_content" TEXT, "definition" TEXT, "dimension" TEXT, '
            '"statistics_view_count" INTEGER, "statistics_like_count" INTEGER, '
            '"statistics_comment_count" INTEGER, "statistics_fetched_at" TEXT, '
            '"topic_categories_json" TEXT, "tags_json" TEXT, '
            '"thumbnails_json" TEXT, '
            '"created_at" TEXT DEFAULT '
            "(strftime('%Y-%m-%dT%H:%M:%fZ', 'now')), "
            '"updated_at" TEXT DEFAULT '
            "(strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))"
            ") STRICT",
            _COPY_INGESTED_VIDEO_WITHOUT_TRANSCRIPTS_SQL,
            'DROP TABLE "ingested_video"',
            'ALTER TABLE "ingested_video_without_transcripts" '
            'RENAME TO "ingested_video"',
            'CREATE UNIQUE INDEX "ux_ingested_video_video_id" '
            'ON "ingested_video" ("video_id")',
            'CREATE INDEX "ix_ingested_video_topic" ON "ingested_video" ("topic")',
        )
        for statement in statements:
            cursor = await connection.execute(statement, ())
            await cursor.close()
