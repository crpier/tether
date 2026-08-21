"""Persisted YouTube corpus models, state transitions, and schema."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import ClassVar, Literal
from uuid import uuid7

from pydantic import UUID7
from snekql.sqlite import (
    CurrentTimestamp,
    Database,
    Fetched,
    Index,
    Integer,
    Model,
    Pending,
    Text,
    Transaction,
    UtcDatetime,
    delete,
    insert,
    select,
    update,
)

from tether.youtube.quota import RawYouTubeVideo

type YouTubeSource = Literal["liked"]
"""Which saved list a video was ingested from.

Only ``liked`` is ever written: the YouTube Data API does not expose the Watch
Later playlist, so liked videos are the sole ingestion source today."""

type IngestState = Literal["active", "ignored"]
"""Whether an ingested video is live in browse/search or purged from it."""

# Cap on videos returned by semantic search when the caller passes no explicit
# limit, keeping assistant-facing results within the model's context.


class IngestedVideo[S = Pending](Model[S, "IngestedVideo[Fetched]"]):
    id: IngestedVideo.GenCol[UUID7] = Text(primary_key=True, default_factory=uuid7)
    video_id: IngestedVideo.Col[str] = Text(nullable=False, unique=True)
    """The upstream YouTube id; the stable identity ingestion mirrors against."""
    source: IngestedVideo.Col[YouTubeSource] = Text()
    """Which saved list the video came from."""
    title: IngestedVideo.Col[str] = Text()
    channel: IngestedVideo.Col[str] = Text()
    topic: IngestedVideo.Col[str] = Text()
    """The topic browse filters on."""
    description: IngestedVideo.Col[str] = Text()
    """Saved-content text searched alongside the transcript."""
    transcript: IngestedVideo.Col[str | None] = Text(default=None, nullable=True)
    """The transcript, present only once explicitly fetched."""
    transcript_source: IngestedVideo.Col[str | None] = Text(default=None, nullable=True)
    """Which provider produced the stored transcript (e.g. `supadata`,
    `youtube_transcript_api`); null until one is fetched."""
    ignored_at: IngestedVideo.Col[UtcDatetime | None] = Text(
        default=None, nullable=True
    )
    """When the video was purged from ingestion; null while it is active."""
    # --- Enriched metadata (nullable; filled by sync detail fetch / import). ---
    channel_id: IngestedVideo.Col[str | None] = Text(default=None, nullable=True)
    liked_at: IngestedVideo.Col[UtcDatetime | None] = Text(default=None, nullable=True)
    """When the user liked the video; the ordering key for browse."""
    video_published_at: IngestedVideo.Col[UtcDatetime | None] = Text(
        default=None, nullable=True
    )
    duration_seconds: IngestedVideo.Col[int | None] = Integer(
        default=None, nullable=True
    )
    category_id: IngestedVideo.Col[str | None] = Text(default=None, nullable=True)
    default_language: IngestedVideo.Col[str | None] = Text(default=None, nullable=True)
    default_audio_language: IngestedVideo.Col[str | None] = Text(
        default=None, nullable=True
    )
    caption_available: IngestedVideo.Col[int | None] = Integer(
        default=None, nullable=True
    )
    privacy_status: IngestedVideo.Col[str | None] = Text(default=None, nullable=True)
    licensed_content: IngestedVideo.Col[int | None] = Integer(
        default=None, nullable=True
    )
    made_for_kids: IngestedVideo.Col[int | None] = Integer(default=None, nullable=True)
    live_broadcast_content: IngestedVideo.Col[str | None] = Text(
        default=None, nullable=True
    )
    definition: IngestedVideo.Col[str | None] = Text(default=None, nullable=True)
    dimension: IngestedVideo.Col[str | None] = Text(default=None, nullable=True)
    statistics_view_count: IngestedVideo.Col[int | None] = Integer(
        default=None, nullable=True
    )
    statistics_like_count: IngestedVideo.Col[int | None] = Integer(
        default=None, nullable=True
    )
    statistics_comment_count: IngestedVideo.Col[int | None] = Integer(
        default=None, nullable=True
    )
    statistics_fetched_at: IngestedVideo.Col[UtcDatetime | None] = Text(
        default=None, nullable=True
    )
    topic_categories_json: IngestedVideo.Col[str | None] = Text(
        default=None, nullable=True
    )
    tags_json: IngestedVideo.Col[str | None] = Text(default=None, nullable=True)
    thumbnails_json: IngestedVideo.Col[str | None] = Text(default=None, nullable=True)
    created_at: IngestedVideo.GenCol[UtcDatetime] = Text(default=CurrentTimestamp)
    updated_at: IngestedVideo.GenCol[UtcDatetime] = Text(default=CurrentTimestamp)

    __indexes__: ClassVar = [Index(topic)]


type TranscriptPersistedStatus = Literal["retrying", "needs_review", "unavailable"]
type TranscriptStatus = Literal[
    "pending", "retrying", "needs_review", "available", "unavailable"
]
"""Public transcript acquisition state derived from content and failure state."""


class YouTubeTranscriptState[S = Pending](Model[S, "YouTubeTranscriptState[Fetched]"]):
    """Durable per-video transcript bookkeeping for the background worker.

    Keyed by the upstream `video_id`. Absence means *pending*; a row carries the
    state-machine status, the attempt count, the next-attempt time (for backed-off
    retries that survive restarts), and the last error for observability.
    """

    video_id: YouTubeTranscriptState.Col[str] = Text(primary_key=True)
    status: YouTubeTranscriptState.Col[TranscriptPersistedStatus] = Text(nullable=False)
    attempts: YouTubeTranscriptState.Col[int] = Integer(default=0)
    next_attempt_at: YouTubeTranscriptState.Col[UtcDatetime | None] = Text(
        default=None, nullable=True
    )
    """When the next retry becomes due; null unless `status` is `retrying`."""
    last_error: YouTubeTranscriptState.Col[str | None] = Text(
        default=None, nullable=True
    )
    updated_at: YouTubeTranscriptState.GenCol[UtcDatetime] = Text(
        default=CurrentTimestamp
    )


def derive_ingest_state(video: IngestedVideo[Fetched]) -> IngestState:
    """Derive whether a video is live in ingestion or purged from it."""
    return "ignored" if video.ignored_at is not None else "active"


def _json_or_none(values: Sequence[str] | Mapping[str, str]) -> str | None:
    """Encode a non-empty sequence/mapping as JSON, else None."""
    return json.dumps(values) if values else None


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


async def upsert_ingested_video(tx: Transaction, raw: RawYouTubeVideo) -> None:
    """Insert or refresh an ingested video from a raw liked video by `video_id`.

    A new id is inserted fresh; an existing one has its metadata overwritten in
    place. Either way the local-only columns — `transcript` and `ignored_at` —
    are left untouched, so a sync (or the backup import) never clobbers a fetched
    transcript or resurrects a video the user purged. Shared by the background
    sync and the active-workbench backup importer so both mirror likes the same
    way.
    """
    existing = await tx.fetch_one_or_none(
        select(IngestedVideo).where(IngestedVideo.video_id.eq(raw.video_id))
    )
    if existing is None:
        _ = await tx.execute(insert(_new_ingested_video(raw)))
        return
    _ = await tx.execute(
        update(IngestedVideo)
        .set(IngestedVideo.source.to("liked"))
        .set(IngestedVideo.title.to(raw.title))
        .set(IngestedVideo.channel.to(raw.channel))
        .set(IngestedVideo.topic.to(raw.topic))
        .set(IngestedVideo.description.to(raw.description))
        .set(IngestedVideo.channel_id.to(raw.channel_id))
        # A raw without liked_at (e.g. a detail-only record) must not clear a
        # timestamp an earlier liked-page pass already recorded.
        .set(
            IngestedVideo.liked_at.to(
                raw.liked_at if raw.liked_at is not None else existing.liked_at
            )
        )
        .set(IngestedVideo.video_published_at.to(raw.video_published_at))
        .set(IngestedVideo.duration_seconds.to(raw.duration_seconds))
        .set(IngestedVideo.category_id.to(raw.category_id))
        .set(IngestedVideo.default_language.to(raw.default_language))
        .set(IngestedVideo.default_audio_language.to(raw.default_audio_language))
        .set(
            IngestedVideo.caption_available.to(
                _bool_to_int(value=raw.caption_available)
            )
        )
        .set(IngestedVideo.privacy_status.to(raw.privacy_status))
        .set(
            IngestedVideo.licensed_content.to(_bool_to_int(value=raw.licensed_content))
        )
        .set(IngestedVideo.made_for_kids.to(_bool_to_int(value=raw.made_for_kids)))
        .set(IngestedVideo.live_broadcast_content.to(raw.live_broadcast_content))
        .set(IngestedVideo.definition.to(raw.definition))
        .set(IngestedVideo.dimension.to(raw.dimension))
        .set(IngestedVideo.statistics_view_count.to(raw.statistics_view_count))
        .set(IngestedVideo.statistics_like_count.to(raw.statistics_like_count))
        .set(IngestedVideo.statistics_comment_count.to(raw.statistics_comment_count))
        .set(IngestedVideo.statistics_fetched_at.to(raw.statistics_fetched_at))
        .set(
            IngestedVideo.topic_categories_json.to(_json_or_none(raw.topic_categories))
        )
        .set(IngestedVideo.tags_json.to(_json_or_none(raw.tags)))
        .set(IngestedVideo.thumbnails_json.to(_json_or_none(raw.thumbnails)))
        .set(IngestedVideo.updated_at.to(CurrentTimestamp))
        .where(IngestedVideo.video_id.eq(raw.video_id))
    )
    # Self-correction: when captions appear on a video the worker previously could
    # not transcribe (its manual-caption flag flipped false -> true), clear the
    # settled/review state so it re-enters the sweep.
    if existing.caption_available == 0 and raw.caption_available is True:
        await _reopen_unavailable_transcript(tx, raw.video_id)


async def _reopen_unavailable_transcript(tx: Transaction, video_id: str) -> None:
    """Return a review-needed/unavailable transcript to pending when captions appear."""
    existing = await fetch_transcript_state(tx, video_id)
    if existing is None or existing.status not in {"needs_review", "unavailable"}:
        return
    _ = await tx.execute(
        delete(YouTubeTranscriptState).where(
            YouTubeTranscriptState.video_id.eq(video_id)
        )
    )


def derive_transcript_status(
    video: IngestedVideo[Fetched],
    state: YouTubeTranscriptState[Fetched] | None,
) -> TranscriptStatus:
    """Derive the normalized public status from transcript content and acquisition."""
    if video.transcript is not None:
        return "available"
    return state.status if state is not None else "pending"


async def fetch_transcript_statuses(
    tx: Transaction, videos: Sequence[IngestedVideo[Fetched]]
) -> dict[str, TranscriptStatus]:
    """Load normalized transcript statuses for a batch of video rows."""
    if not videos:
        return {}
    states = await tx.fetch_all(
        select(YouTubeTranscriptState).where(
            YouTubeTranscriptState.video_id.in_(*(video.video_id for video in videos))
        )
    )
    state_by_id = {state.video_id: state for state in states}
    return {
        video.video_id: derive_transcript_status(video, state_by_id.get(video.video_id))
        for video in videos
    }


async def fetch_transcript_state(
    tx: Transaction, video_id: str
) -> YouTubeTranscriptState[Fetched] | None:
    """Return a video's persisted transcript state row, or None when pending."""
    return await tx.fetch_one_or_none(
        select(YouTubeTranscriptState).where(
            YouTubeTranscriptState.video_id.eq(video_id)
        )
    )


@dataclass(frozen=True, slots=True)
class TranscriptStateWrite:
    """The mutable fields of one transcript-state transition."""

    attempts: int
    last_error: str | None
    next_attempt_at: datetime | None
    status: TranscriptPersistedStatus


async def write_transcript_state(
    tx: Transaction, video_id: str, fields: TranscriptStateWrite
) -> None:
    """Insert or refresh a video's transcript-state row in place."""
    existing = await fetch_transcript_state(tx, video_id)
    if existing is None:
        _ = await tx.execute(
            insert(
                YouTubeTranscriptState(
                    video_id=video_id,
                    status=fields.status,
                    attempts=fields.attempts,
                    next_attempt_at=fields.next_attempt_at,
                    last_error=fields.last_error,
                )
            )
        )
        return
    _ = await tx.execute(
        update(YouTubeTranscriptState)
        .set(YouTubeTranscriptState.status.to(fields.status))
        .set(YouTubeTranscriptState.attempts.to(fields.attempts))
        .set(YouTubeTranscriptState.next_attempt_at.to(fields.next_attempt_at))
        .set(YouTubeTranscriptState.last_error.to(fields.last_error))
        .set(YouTubeTranscriptState.updated_at.to(CurrentTimestamp))
        .where(YouTubeTranscriptState.video_id.eq(video_id))
    )


# snekql replays a frozen, hand-authored migration chain and records each step by
# *name*, never re-running an applied one. The original `ingested_video` table +
# indexes are frozen verbatim under their first-shipped keys so existing
# databases skip them; enriched columns and the new bookkeeping tables arrive as
# their own forward migrations. Replaying the whole chain on a fresh database
# yields the current schema.
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
    # Per-video transcript state machine for the background transcript worker.
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
    return migrations


async def create_youtube_schema(database: Database) -> None:
    """Bring the YouTube ingestion schema to current on an initialized database.

    Applies the frozen migration chain: the original ingested-video table and
    indexes (skipped on databases that already have them), the enriched-metadata
    columns, and the persisted daily-budget + sync-state tables.

    >>> from snekql.sqlite import Config
    >>> database = await Database.initialize(backend=Config(database=":memory:"))
    >>> await create_youtube_schema(database)
    """
    await database.migrate(_youtube_migrations())
