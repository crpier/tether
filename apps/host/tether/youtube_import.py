"""One-shot import of an active-workbench YouTube backup into Tether's corpus.

The previous personal-assistant project (active-workbench) already synced the
user's full YouTube history into a local SQLite `state.db`: ~1090 liked videos
with rich metadata and ~1402 fetched transcripts. Re-pulling that through the
quota-limited OAuth client would cost thousands of API units and days of slow
backfill to recover data the user already owns. This module absorbs that backup
directly into Tether's `IngestedVideo` corpus, **never calling YouTube**.

The importer depends on a narrow `LikesBackupReader` seam that yields normalised
liked-video and transcript records. The production reader
(`SqliteLikesBackupReader`) opens the active-workbench `state.db` read-only and
queries `youtube_likes_cache` and `youtube_transcript_cache`; tests inject a fake
reader seeded with representative records, so the import logic is exercised
without the 125 MB file and without a network call.

Import is an **idempotent upsert by `video_id`**: re-running updates existing
rows rather than duplicating them, preserves a video's local ignore state and any
richer existing transcript, and creates a sparse video for an orphan transcript
whose video is not in the likes table — so no transcript is silently dropped. A
dry-run computes and reports the same counts without writing.

>>> reader = InMemoryLikesBackupReader(
...     liked=[BackupLikedVideo(video_id="v1", title="T", channel_title="C")])
>>> reader.liked_videos()[0].video_id
'v1'
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, cast, runtime_checkable
from urllib.parse import unquote

from snekql.sqlite import (
    CurrentTimestamp,
    Database,
    Fetched,
    Transaction,
    insert,
    select,
    update,
)

from tether.logging import Logger
from tether.youtube import (
    IngestedVideo,
    RawYouTubeVideo,
    upsert_ingested_video,
)

_DEFAULT_TOPIC = "youtube"
"""Topic assigned when a backup liked video carries no topic signal at all."""


def _empty_thumbnails() -> dict[str, str]:
    """Typed default factory for a liked record's thumbnail map."""
    return {}


@dataclass(frozen=True, slots=True)
class BackupLikedVideo:
    """A liked-video record read from the active-workbench backup.

    Mirrors `youtube_likes_cache`: `video_id` is identity, `channel_title` is the
    channel name (renamed onto `IngestedVideo.channel` during import), and the
    rest are the enriched metadata that maps straight onto the columns landed in
    #80. A record with a blank `video_id` is malformed and skipped by the
    importer (it cannot key a row).
    """

    video_id: str
    title: str
    channel_title: str
    description: str = ""
    channel_id: str | None = None
    liked_at: datetime | None = None
    video_published_at: datetime | None = None
    duration_seconds: int | None = None
    category_id: str | None = None
    default_language: str | None = None
    default_audio_language: str | None = None
    caption_available: bool | None = None
    privacy_status: str | None = None
    licensed_content: bool | None = None
    made_for_kids: bool | None = None
    live_broadcast_content: str | None = None
    definition: str | None = None
    dimension: str | None = None
    statistics_view_count: int | None = None
    statistics_like_count: int | None = None
    statistics_comment_count: int | None = None
    statistics_fetched_at: datetime | None = None
    topic_categories: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    thumbnails: dict[str, str] = field(default_factory=_empty_thumbnails)


@dataclass(frozen=True, slots=True)
class BackupTranscript:
    """A fetched-transcript record from the backup's `youtube_transcript_cache`.

    `transcript` is the plain text written onto the ingested video, joined by
    `video_id`. `title` is the transcript record's own title, used only when the
    video is an orphan (no row in the likes table) so a sparse video can still be
    created from it. A record with a blank `video_id` or empty `transcript` is
    malformed and skipped.
    """

    video_id: str
    transcript: str
    title: str | None = None


@runtime_checkable
class LikesBackupReader(Protocol):
    """The narrow source the importer reads liked videos and transcripts from.

    A structural seam: the production `SqliteLikesBackupReader` opens the real
    backup, while tests inject a fake seeded with representative records. Both
    yield already-normalised records, so the import logic never touches SQLite
    row tuples or the foreign schema.
    """

    def liked_videos(self) -> Iterable[BackupLikedVideo]:
        """Yield every liked-video record in the backup."""
        ...

    def transcripts(self) -> Iterable[BackupTranscript]:
        """Yield every fetched-transcript record in the backup."""
        ...


class InMemoryLikesBackupReader:
    """A seedable in-memory `LikesBackupReader`, for tests and docs.

    >>> reader = InMemoryLikesBackupReader(
    ...     transcripts=[BackupTranscript(video_id="v1", transcript="hello")])
    >>> reader.transcripts()[0].transcript
    'hello'
    """

    def __init__(
        self,
        *,
        liked: Sequence[BackupLikedVideo] = (),
        transcripts: Sequence[BackupTranscript] = (),
    ) -> None:
        self._liked: list[BackupLikedVideo] = list(liked)
        self._transcripts: list[BackupTranscript] = list(transcripts)

    def liked_videos(self) -> Iterable[BackupLikedVideo]:
        return list(self._liked)

    def transcripts(self) -> Iterable[BackupTranscript]:
        return list(self._transcripts)


@dataclass(frozen=True, slots=True)
class ImportReport:
    """A summary of one backup import, printed by the CLI for confirmation.

    `videos_inserted` + `videos_updated` is how many likes were absorbed;
    `orphans_created` counts transcripts whose video was not in the likes table
    and which therefore minted a sparse video; `transcripts_imported` counts
    transcripts actually written (a non-empty transcript not overwritten by an
    empty one is *not* counted); `skipped` counts malformed/partial records that
    were dropped rather than aborting the run. `dry_run` records that nothing was
    written.
    """

    videos_inserted: int = 0
    videos_updated: int = 0
    orphans_created: int = 0
    transcripts_imported: int = 0
    skipped: int = 0
    dry_run: bool = False


def _topic_from_category_url(url: str) -> str:
    """Derive a browse topic from a Wikipedia topic-category URL.

    active-workbench stores `topicCategories` as Wikipedia article URLs (e.g.
    `https://en.wikipedia.org/wiki/Python_(programming_language)`). The final
    path segment, percent-decoded and underscore-spaced, is a readable topic.
    """
    slug = url.rstrip("/").rsplit("/", 1)[-1]
    return unquote(slug).replace("_", " ").strip().lower() or _DEFAULT_TOPIC


def _derive_topic(record: BackupLikedVideo) -> str:
    """Pick a browse topic: first topic category, else category id, else default."""
    if record.topic_categories:
        return _topic_from_category_url(record.topic_categories[0])
    if record.category_id:
        return record.category_id
    return _DEFAULT_TOPIC


def _as_raw(record: BackupLikedVideo) -> RawYouTubeVideo:
    """Map a backup liked record onto the raw upstream shape the upsert consumes."""
    return RawYouTubeVideo(
        video_id=record.video_id,
        title=record.title,
        channel=record.channel_title,
        topic=_derive_topic(record),
        description=record.description,
        channel_id=record.channel_id,
        liked_at=record.liked_at,
        video_published_at=record.video_published_at,
        duration_seconds=record.duration_seconds,
        category_id=record.category_id,
        default_language=record.default_language,
        default_audio_language=record.default_audio_language,
        caption_available=record.caption_available,
        privacy_status=record.privacy_status,
        licensed_content=record.licensed_content,
        made_for_kids=record.made_for_kids,
        live_broadcast_content=record.live_broadcast_content,
        definition=record.definition,
        dimension=record.dimension,
        statistics_view_count=record.statistics_view_count,
        statistics_like_count=record.statistics_like_count,
        statistics_comment_count=record.statistics_comment_count,
        statistics_fetched_at=record.statistics_fetched_at,
        topic_categories=record.topic_categories,
        tags=record.tags,
        thumbnails=record.thumbnails,
    )


async def _import_likes(
    tx: Transaction,
    reader: LikesBackupReader,
    *,
    dry_run: bool,
    planned_ids: set[str],
) -> tuple[int, int, int]:
    """Mirror the backup's liked videos; returns (inserted, updated, skipped)."""
    inserted = 0
    updated = 0
    skipped = 0
    for record in reader.liked_videos():
        if not record.video_id:
            skipped += 1
            continue
        if await _video_exists(tx, record.video_id):
            updated += 1
        else:
            inserted += 1
        planned_ids.add(record.video_id)
        if not dry_run:
            await upsert_ingested_video(tx, _as_raw(record))
    return inserted, updated, skipped


async def _import_transcripts(
    tx: Transaction,
    reader: LikesBackupReader,
    *,
    dry_run: bool,
    planned_ids: set[str],
) -> tuple[int, int, int]:
    """Attach transcripts; returns (orphans, transcripts_imported, skipped)."""
    orphans = 0
    transcripts_imported = 0
    skipped = 0
    for transcript in reader.transcripts():
        if not transcript.video_id or not transcript.transcript.strip():
            skipped += 1
            continue
        existing = await _fetch_video(tx, transcript.video_id)
        if existing is None and transcript.video_id not in planned_ids:
            orphans += 1
            transcripts_imported += 1
            planned_ids.add(transcript.video_id)
            if not dry_run:
                await _insert_orphan_video(tx, transcript)
            continue
        if existing is not None and existing.transcript and existing.transcript.strip():
            # A richer existing transcript must not be clobbered; the import
            # is additive, so an already-stored transcript wins.
            continue
        transcripts_imported += 1
        if not dry_run:
            await _attach_transcript(tx, transcript)
    return orphans, transcripts_imported, skipped


async def import_backup(
    database: Database,
    reader: LikesBackupReader,
    *,
    dry_run: bool = False,
    logger: Logger,
) -> ImportReport:
    """Absorb a backup's liked videos and transcripts into the ingested corpus.

    Upserts each liked record by `video_id` (preserving local ignore state and
    any fetched transcript), then attaches each transcript to its video —
    creating a sparse video for an orphan transcript, and never overwriting a
    non-empty existing transcript with an empty one. Malformed records (blank
    `video_id`, empty transcript) are skipped and counted, never aborting. With
    `dry_run`, the same counts are computed but nothing is written.
    """
    logger.info("YouTube backup import starting", dry_run=dry_run)
    # The ids the likes phase mirrors. A transcript for one of these joins an
    # existing video, never an orphan — tracked explicitly so a dry-run (which
    # writes nothing) classifies transcripts exactly as a real run would.
    planned_ids: set[str] = set()

    async with database.transaction() as tx:
        inserted, updated, likes_skipped = await _import_likes(
            tx, reader, dry_run=dry_run, planned_ids=planned_ids
        )
        orphans, transcripts_imported, transcript_skipped = await _import_transcripts(
            tx, reader, dry_run=dry_run, planned_ids=planned_ids
        )

    report = ImportReport(
        videos_inserted=inserted,
        videos_updated=updated,
        orphans_created=orphans,
        transcripts_imported=transcripts_imported,
        skipped=likes_skipped + transcript_skipped,
        dry_run=dry_run,
    )
    logger.info(
        "YouTube backup import completed",
        videos_inserted=report.videos_inserted,
        videos_updated=report.videos_updated,
        orphans_created=report.orphans_created,
        transcripts_imported=report.transcripts_imported,
        skipped=report.skipped,
        dry_run=report.dry_run,
    )
    return report


async def _video_exists(tx: Transaction, video_id: str) -> bool:
    return (await _fetch_video(tx, video_id)) is not None


async def _fetch_video(tx: Transaction, video_id: str) -> IngestedVideo[Fetched] | None:
    return await tx.fetch_one_or_none(
        select(IngestedVideo).where(IngestedVideo.video_id.eq(video_id))
    )


async def _insert_orphan_video(tx: Transaction, transcript: BackupTranscript) -> None:
    """Mint a sparse ingested video for a transcript with no liked-list row."""
    _ = await tx.execute(
        insert(
            IngestedVideo(
                video_id=transcript.video_id,
                source="liked",
                title=transcript.title or transcript.video_id,
                channel="",
                topic=_DEFAULT_TOPIC,
                description="",
                transcript=transcript.transcript,
            )
        )
    )


async def _attach_transcript(tx: Transaction, transcript: BackupTranscript) -> None:
    _ = await tx.execute(
        update(IngestedVideo)
        .set(IngestedVideo.transcript.to(transcript.transcript))
        .set(IngestedVideo.updated_at.to(CurrentTimestamp))
        .where(IngestedVideo.video_id.eq(transcript.video_id))
    )


class SqliteLikesBackupReader:
    """Reads an active-workbench `state.db` read-only as a `LikesBackupReader`.

    Opens the foreign database in read-only mode (`mode=ro`, immutable) and
    queries `youtube_likes_cache` and `youtube_transcript_cache`, normalising
    each row into a `BackupLikedVideo` / `BackupTranscript`. Columns are read by
    name and missing ones tolerated, so a backup with a slightly older schema
    still imports what it has rather than crashing. JSON columns
    (`topic_categories`, `tags`, `thumbnails`) and timestamps are decoded here;
    rows whose JSON or timestamps are malformed degrade to empty/None rather than
    failing the whole read.
    """

    def __init__(self, path: Path) -> None:
        self._path: Path = path

    def _connect(self) -> sqlite3.Connection:
        # `mode=ro` opens the foreign file read-only; the importer never mutates
        # the backup. `uri=True` is required for the `file:` URI form.
        uri = f"file:{self._path}?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
        connection.row_factory = sqlite3.Row
        return connection

    def liked_videos(self) -> Iterable[BackupLikedVideo]:
        connection = self._connect()
        try:
            rows = connection.execute("SELECT * FROM youtube_likes_cache").fetchall()
        finally:
            connection.close()
        records: list[BackupLikedVideo] = []
        for row in rows:
            mapping = dict(row)
            records.append(
                BackupLikedVideo(
                    video_id=_text(mapping.get("video_id")) or "",
                    title=_text(mapping.get("title")) or "",
                    channel_title=_text(mapping.get("channel_title")) or "",
                    description=_text(mapping.get("description")) or "",
                    channel_id=_text(mapping.get("channel_id")),
                    liked_at=_timestamp(mapping.get("liked_at")),
                    video_published_at=_timestamp(mapping.get("video_published_at")),
                    duration_seconds=_int(mapping.get("duration_seconds")),
                    category_id=_text(mapping.get("category_id")),
                    default_language=_text(mapping.get("default_language")),
                    default_audio_language=_text(mapping.get("default_audio_language")),
                    caption_available=_bool(mapping.get("caption_available")),
                    privacy_status=_text(mapping.get("privacy_status")),
                    licensed_content=_bool(mapping.get("licensed_content")),
                    made_for_kids=_bool(mapping.get("made_for_kids")),
                    live_broadcast_content=_text(mapping.get("live_broadcast_content")),
                    definition=_text(mapping.get("definition")),
                    dimension=_text(mapping.get("dimension")),
                    statistics_view_count=_int(mapping.get("statistics_view_count")),
                    statistics_like_count=_int(mapping.get("statistics_like_count")),
                    statistics_comment_count=_int(
                        mapping.get("statistics_comment_count")
                    ),
                    statistics_fetched_at=_timestamp(
                        mapping.get("statistics_fetched_at")
                    ),
                    topic_categories=_json_str_tuple(mapping.get("topic_categories")),
                    tags=_json_str_tuple(mapping.get("tags")),
                    thumbnails=_json_str_map(mapping.get("thumbnails")),
                )
            )
        return records

    def transcripts(self) -> Iterable[BackupTranscript]:
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT * FROM youtube_transcript_cache"
            ).fetchall()
        finally:
            connection.close()
        records: list[BackupTranscript] = []
        for row in rows:
            mapping = dict(row)
            records.append(
                BackupTranscript(
                    video_id=_text(mapping.get("video_id")) or "",
                    transcript=_text(mapping.get("transcript")) or "",
                    title=_text(mapping.get("title")),
                )
            )
        return records


def _text(value: object) -> str | None:
    """Coerce a SQLite cell to text, treating empty/absent as `None`."""
    if value is None:
        return None
    if isinstance(value, str):
        return value or None
    return str(value)


def _int(value: object) -> int | None:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value)
    return None


def _bool(value: object) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes"}:
            return True
        if lowered in {"0", "false", "no", ""}:
            return False
    return None


def _timestamp(value: object) -> datetime | None:
    """Parse a backup timestamp (ISO string or epoch number) to aware UTC."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=UTC)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        normalised = text.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(normalised)
        except ValueError:
            return None
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    return None


def _json_str_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, str) or not value.strip():
        return ()
    try:
        loaded: object = json.loads(value)
    except json.JSONDecodeError:
        return ()
    if not isinstance(loaded, list):
        return ()
    items = cast("list[object]", loaded)
    return tuple(item for item in items if isinstance(item, str))


def _json_str_map(value: object) -> dict[str, str]:
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        loaded: object = json.loads(value)
    except json.JSONDecodeError:
        return {}
    if not isinstance(loaded, dict):
        return {}
    pairs = cast("dict[object, object]", loaded)
    return {k: v for k, v in pairs.items() if isinstance(k, str) and isinstance(v, str)}
