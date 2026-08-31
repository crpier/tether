"""Persisted YouTube corpus models, state transitions, and schema."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Annotated, ClassVar, Literal
from uuid import uuid7

from pydantic import UUID7, BaseModel, ConfigDict, Field, Json
from snekok.types import (
    NonBlankStr,
    NonEmptyStr,
    NonNegativeInt,
    PositiveInt,
)
from snekok.validation import validate_python_unsafe
from snekql.sqlite import (
    PENDING_GENERATION,
    CurrentTimestamp,
    Database,
    Fetched,
    ForeignKey,
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

from tether.transcripts.contracts import TranscriptSegment
from tether.youtube.quota import RawYouTubeVideo
from tether.youtube.types import VideoId

type YouTubeSource = Literal["liked"]
"""Which saved list a video was ingested from.

Only ``liked`` is ever written: the YouTube Data API does not expose the Watch
Later playlist, so liked videos are the sole ingestion source today."""

type IngestState = Literal["active", "ignored"]
"""Whether an ingested video is live in browse/search or purged from it."""


class IngestedVideo[S = Pending](Model[S, "IngestedVideo[Fetched]"]):
    id: IngestedVideo.GenCol[UUID7] = Text(primary_key=True, default_factory=uuid7)
    video_id: IngestedVideo.Col[VideoId] = Text(nullable=False, unique=True)
    """The upstream YouTube id; the stable identity ingestion mirrors against."""
    source: IngestedVideo.Col[YouTubeSource] = Text()
    """Which saved list the video came from."""
    title: IngestedVideo.Col[str] = Text()
    channel: IngestedVideo.Col[str] = Text()
    topic: IngestedVideo.Col[str] = Text()
    """The topic browse filters on."""
    description: IngestedVideo.Col[str] = Text()
    """Saved-content text searched alongside the transcript."""
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


async def upsert_ingested_video(tx: Transaction, raw: RawYouTubeVideo) -> None:
    """Insert or refresh an ingested video from a raw liked video by `video_id`.

    A new id is inserted fresh; an existing one has its metadata overwritten in
    place. The local-only `ignored_at` column is left untouched, and transcript
    state lives in its own table, so synchronization never resurrects a purged
    video or clobbers transcript acquisition. Shared by the background sync and
    backup importer so both mirror likes the same way.
    """
    existing = await tx.fetch_one_or_none(
        select(IngestedVideo).where(IngestedVideo.video_id.eq(raw.video_id))
    )
    # TODO: after snekql adds upsert, use that instead of this.
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
    if existing.caption_available == 0 and raw.caption_available is True:
        state = await fetch_transcript_state(tx, existing.id)
        if isinstance(state, TranscriptReviewNeeded | TranscriptUnavailable):
            _ = await tx.execute(
                delete(YouTubeTranscript).where(
                    YouTubeTranscript.ingested_video_id.eq(existing.id)
                )
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

    Applies the frozen migration chain: the original ingested-video table and
    indexes (skipped on databases that already have them), the enriched-metadata
    columns, and the persisted daily-budget + sync-state tables.

    >>> from snekql.sqlite import Config
    >>> database = await Database.initialize(backend=Config(database=":memory:"))
    >>> await create_youtube_schema(database)
    """
    await database.migrate(_youtube_migrations())


type TranscriptPersistedStatus = Literal[
    "retrying", "needs_review", "available", "unavailable"
]
type TranscriptStatus = Literal[
    "pending", "retrying", "needs_review", "available", "unavailable"
]

_ZERO_FAILED_ATTEMPTS = validate_python_unsafe(NonNegativeInt, 0)


class YouTubeTranscript[S = Pending](Model[S, "YouTubeTranscript[Fetched]"]):
    """Canonical transcript content and acquisition state for one saved video."""

    id: YouTubeTranscript.GenCol[PositiveInt] = Integer(
        primary_key=True, auto_increment=True, default=PENDING_GENERATION
    )
    ingested_video_id: YouTubeTranscript.FKCol[IngestedVideo, UUID7] = ForeignKey(
        IngestedVideo.id, nullable=False, unique=True, on_delete="CASCADE"
    )
    status: YouTubeTranscript.Col[TranscriptPersistedStatus] = Text(nullable=False)
    failed_attempts: YouTubeTranscript.Col[NonNegativeInt] = Integer(
        default=_ZERO_FAILED_ATTEMPTS
    )
    last_error: YouTubeTranscript.Col[NonEmptyStr | None] = Text(
        default=None, nullable=True
    )
    next_attempt_at: YouTubeTranscript.Col[UtcDatetime | None] = Text(
        nullable=True, default=None
    )
    source: YouTubeTranscript.Col[NonEmptyStr | None] = Text(
        nullable=True, default=None
    )
    text: YouTubeTranscript.Col[NonBlankStr | None] = Text(nullable=True, default=None)
    segments: YouTubeTranscript.Col[Json[tuple[TranscriptSegment, ...] | None]] = Text(
        nullable=True, default=None
    )
    created_at: YouTubeTranscript.GenCol[UtcDatetime] = Text(default=CurrentTimestamp)
    updated_at: YouTubeTranscript.GenCol[UtcDatetime] = Text(default=CurrentTimestamp)


class _TranscriptStateBase(BaseModel):
    """Shared immutable identity and timestamps for materialized transcript states."""

    ingested_video_id: UUID7
    created_at: UtcDatetime
    updated_at: UtcDatetime

    model_config = ConfigDict(frozen=True, from_attributes=True, extra="ignore")


class TranscriptRetrying(_TranscriptStateBase):
    status: Literal["retrying"]
    failed_attempts: PositiveInt
    last_error: NonEmptyStr
    next_attempt_at: UtcDatetime


class TranscriptReviewNeeded(_TranscriptStateBase):
    status: Literal["needs_review"]
    failed_attempts: PositiveInt
    last_error: NonEmptyStr


class TranscriptAvailable(_TranscriptStateBase):
    status: Literal["available"]
    source: NonEmptyStr | None
    text: NonBlankStr
    segments: tuple[TranscriptSegment, ...]


class TranscriptUnavailable(_TranscriptStateBase):
    status: Literal["unavailable"]
    failed_attempts: PositiveInt
    last_error: NonEmptyStr


type TranscriptState = Annotated[
    TranscriptRetrying
    | TranscriptReviewNeeded
    | TranscriptAvailable
    | TranscriptUnavailable,
    Field(discriminator="status"),
]


class _TranscriptFailureWrite(BaseModel):
    """Validated fields shared by failed acquisition transitions."""

    ingested_video_id: UUID7
    failed_attempts: NonNegativeInt = Field(gt=0)
    last_error: NonEmptyStr

    model_config = ConfigDict(frozen=True)


class _TranscriptRetryingWrite(_TranscriptFailureWrite):
    """Validated fields that make the `retrying` storage shape valid."""

    next_attempt_at: UtcDatetime


class _TranscriptAvailableWrite(BaseModel):
    """Validated fields that make the `available` storage shape valid."""

    ingested_video_id: UUID7
    source: NonEmptyStr | None
    text: NonBlankStr
    segments: tuple[TranscriptSegment, ...]

    model_config = ConfigDict(frozen=True)


async def fetch_transcript_state(
    tx: Transaction, ingested_video_id: UUID7
) -> TranscriptState | None:
    """Fetch one persisted transcript state, or `None` before acquisition."""
    row = await tx.fetch_one_or_none(
        select(YouTubeTranscript).where(
            YouTubeTranscript.ingested_video_id.eq(ingested_video_id)
        )
    )
    if row is None:
        return None
    return validate_python_unsafe(TranscriptState, row)


async def fetch_transcript_states(
    tx: Transaction, videos: Sequence[IngestedVideo[Fetched]]
) -> dict[UUID7, TranscriptState]:
    """Fetch transcript states for saved videos, keyed by their internal IDs."""
    if not videos:
        return {}
    rows = await tx.fetch_all(
        select(YouTubeTranscript).where(
            YouTubeTranscript.ingested_video_id.in_(*(video.id for video in videos))
        )
    )
    states = (validate_python_unsafe(TranscriptState, row) for row in rows)
    return {state.ingested_video_id: state for state in states}


async def write_transcript_retrying(
    tx: Transaction,
    ingested_video_id: UUID7,
    *,
    failed_attempts: int,
    last_error: str,
    next_attempt_at: datetime,
) -> None:
    """Persist a transient failure and the time its next attempt becomes due."""
    fields = validate_python_unsafe(
        _TranscriptRetryingWrite,
        {
            "ingested_video_id": ingested_video_id,
            "failed_attempts": failed_attempts,
            "last_error": last_error,
            "next_attempt_at": next_attempt_at,
        },
    )
    existing = await fetch_transcript_state(tx, fields.ingested_video_id)
    if existing is None:
        _ = await tx.execute(
            insert(
                YouTubeTranscript(
                    ingested_video_id=fields.ingested_video_id,
                    status="retrying",
                    failed_attempts=fields.failed_attempts,
                    last_error=fields.last_error,
                    next_attempt_at=fields.next_attempt_at,
                )
            )
        )
        return
    _ = await tx.execute(
        update(YouTubeTranscript)
        .set(YouTubeTranscript.status.to("retrying"))
        .set(YouTubeTranscript.failed_attempts.to(fields.failed_attempts))
        .set(YouTubeTranscript.last_error.to(fields.last_error))
        .set(YouTubeTranscript.next_attempt_at.to(fields.next_attempt_at))
        .set(YouTubeTranscript.source.to(None))
        .set(YouTubeTranscript.text.to(None))
        .set(YouTubeTranscript.segments.to(None))
        .set(YouTubeTranscript.updated_at.to(CurrentTimestamp))
        .where(YouTubeTranscript.ingested_video_id.eq(fields.ingested_video_id))
    )


async def write_transcript_available(
    tx: Transaction,
    ingested_video_id: UUID7,
    *,
    text: str,
    segments: tuple[TranscriptSegment, ...],
    source: str | None = None,
) -> None:
    """Persist fetched transcript content and clear acquisition failures."""
    fields = validate_python_unsafe(
        _TranscriptAvailableWrite,
        {
            "ingested_video_id": ingested_video_id,
            "source": source,
            "text": text,
            "segments": segments,
        },
    )
    existing = await fetch_transcript_state(tx, fields.ingested_video_id)
    if existing is None:
        _ = await tx.execute(
            insert(
                YouTubeTranscript(
                    ingested_video_id=fields.ingested_video_id,
                    status="available",
                    source=fields.source,
                    text=fields.text,
                    segments=fields.segments,
                )
            )
        )
        return
    _ = await tx.execute(
        update(YouTubeTranscript)
        .set(YouTubeTranscript.status.to("available"))
        .set(
            YouTubeTranscript.failed_attempts.to(
                validate_python_unsafe(NonNegativeInt, 0)
            )
        )
        .set(YouTubeTranscript.last_error.to(None))
        .set(YouTubeTranscript.next_attempt_at.to(None))
        .set(YouTubeTranscript.source.to(fields.source))
        .set(YouTubeTranscript.text.to(fields.text))
        .set(YouTubeTranscript.segments.to(fields.segments))
        .set(YouTubeTranscript.updated_at.to(CurrentTimestamp))
        .where(YouTubeTranscript.ingested_video_id.eq(fields.ingested_video_id))
    )


async def write_transcript_review_needed(
    tx: Transaction,
    ingested_video_id: UUID7,
    *,
    failed_attempts: int,
    last_error: str,
) -> None:
    """Persist provider exhaustion while awaiting a human decision."""
    fields = validate_python_unsafe(
        _TranscriptFailureWrite,
        {
            "ingested_video_id": ingested_video_id,
            "failed_attempts": failed_attempts,
            "last_error": last_error,
        },
    )
    existing = await fetch_transcript_state(tx, fields.ingested_video_id)
    if existing is None:
        _ = await tx.execute(
            insert(
                YouTubeTranscript(
                    ingested_video_id=fields.ingested_video_id,
                    status="needs_review",
                    failed_attempts=fields.failed_attempts,
                    last_error=fields.last_error,
                )
            )
        )
        return
    _ = await tx.execute(
        update(YouTubeTranscript)
        .set(YouTubeTranscript.status.to("needs_review"))
        .set(YouTubeTranscript.failed_attempts.to(fields.failed_attempts))
        .set(YouTubeTranscript.last_error.to(fields.last_error))
        .set(YouTubeTranscript.next_attempt_at.to(None))
        .set(YouTubeTranscript.source.to(None))
        .set(YouTubeTranscript.text.to(None))
        .set(YouTubeTranscript.segments.to(None))
        .set(YouTubeTranscript.updated_at.to(CurrentTimestamp))
        .where(YouTubeTranscript.ingested_video_id.eq(fields.ingested_video_id))
    )


async def write_transcript_unavailable(
    tx: Transaction,
    ingested_video_id: UUID7,
    *,
    failed_attempts: int,
    last_error: str,
) -> None:
    """Persist a permanent failure and clear its retry schedule and content."""
    fields = validate_python_unsafe(
        _TranscriptFailureWrite,
        {
            "ingested_video_id": ingested_video_id,
            "failed_attempts": failed_attempts,
            "last_error": last_error,
        },
    )
    existing = await fetch_transcript_state(tx, fields.ingested_video_id)
    if existing is None:
        _ = await tx.execute(
            insert(
                YouTubeTranscript(
                    ingested_video_id=fields.ingested_video_id,
                    status="unavailable",
                    failed_attempts=fields.failed_attempts,
                    last_error=fields.last_error,
                )
            )
        )
        return
    _ = await tx.execute(
        update(YouTubeTranscript)
        .set(YouTubeTranscript.status.to("unavailable"))
        .set(YouTubeTranscript.failed_attempts.to(fields.failed_attempts))
        .set(YouTubeTranscript.last_error.to(fields.last_error))
        .set(YouTubeTranscript.next_attempt_at.to(None))
        .set(YouTubeTranscript.source.to(None))
        .set(YouTubeTranscript.text.to(None))
        .set(YouTubeTranscript.segments.to(None))
        .set(YouTubeTranscript.updated_at.to(CurrentTimestamp))
        .where(YouTubeTranscript.ingested_video_id.eq(fields.ingested_video_id))
    )
