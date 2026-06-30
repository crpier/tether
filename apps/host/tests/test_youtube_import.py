"""Behaviour tests for the active-workbench YouTube backup import.

These drive `import_backup` against a real in-memory SQLite Tether database and a
faked `LikesBackupReader` seeded with representative records — never the 125 MB
backup, never a YouTube call. A small fixture SQLite covers
`SqliteLikesBackupReader`'s parsing of the foreign schema in isolation.
"""

import json
import sqlite3
import tempfile
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from pathlib import Path

import structlog
from snekql.sqlite import Config, Database, Fetched, insert, select, update
from snektest import (
    assert_eq,
    assert_is_none,
    assert_is_not_none,
    assert_true,
    fixture,
    load_fixture,
    test,
)

from tether.logging import Logger
from tether.youtube import IngestedVideo, create_youtube_schema
from tether.youtube_import import (
    BackupLikedVideo,
    BackupTranscript,
    InMemoryLikesBackupReader,
    SqliteLikesBackupReader,
    import_backup,
)


def test_logger() -> Logger:
    """A throwaway structured logger for the mandatory importer logger arg."""
    return structlog.stdlib.get_logger("test.youtube_import")


@fixture
async def make_db() -> AsyncGenerator[Database]:
    """A fresh in-memory Tether database with the YouTube schema applied."""
    db = await Database.initialize(backend=Config(database=":memory:"))
    await create_youtube_schema(db)
    yield db
    await db.close()


async def _video(db: Database, video_id: str) -> IngestedVideo[Fetched] | None:
    async with db.transaction() as tx:
        return await tx.fetch_one_or_none(
            select(IngestedVideo).where(IngestedVideo.video_id.eq(video_id))
        )


def _liked(video_id: str, **overrides: object) -> BackupLikedVideo:
    """A backup liked record with sensible defaults, fields overridable."""
    base: dict[str, object] = {
        "video_id": video_id,
        "title": "A Talk",
        "channel_title": "PyConf",
    }
    base.update(overrides)
    return BackupLikedVideo(**base)  # pyright: ignore[reportArgumentType]


# --- A liked record imports with all enriched fields and liked-at ordering ---


@test()
async def liked_record_imports_with_enriched_fields() -> None:
    """A liked record maps every backup field onto the enriched columns."""
    db = await load_fixture(make_db())
    liked_at = datetime(2023, 5, 1, 12, 0, tzinfo=UTC)
    reader = InMemoryLikesBackupReader(
        liked=[
            _liked(
                "v1",
                title="Async Python",
                channel_title="PyConf",
                description="a talk about asyncio",
                channel_id="UC123",
                liked_at=liked_at,
                duration_seconds=1800,
                category_id="28",
                default_language="en",
                caption_available=True,
                statistics_view_count=42,
                topic_categories=(
                    "https://en.wikipedia.org/wiki/Python_(programming_language)",
                ),
                tags=("python", "async"),
                thumbnails={"default": "http://img/x.jpg"},
            )
        ]
    )

    report = await import_backup(db, reader, logger=test_logger())

    assert_eq(report.videos_inserted, 1)
    video = await _video(db, "v1")
    assert_is_not_none(video)
    assert video is not None
    assert_eq(video.title, "Async Python")
    assert_eq(video.channel, "PyConf")
    assert_eq(video.channel_id, "UC123")
    assert_eq(video.liked_at, liked_at)
    assert_eq(video.duration_seconds, 1800)
    assert_eq(video.caption_available, 1)
    assert_eq(video.statistics_view_count, 42)
    assert_eq(video.topic, "python (programming language)")
    assert_is_not_none(video.tags_json)
    assert video.tags_json is not None
    assert_eq(json.loads(video.tags_json), ["python", "async"])
    assert video.thumbnails_json is not None
    assert_eq(json.loads(video.thumbnails_json), {"default": "http://img/x.jpg"})


@test()
async def topic_falls_back_to_category_id_then_default() -> None:
    """Topic comes from category id when no topic categories, else a default."""
    db = await load_fixture(make_db())
    reader = InMemoryLikesBackupReader(
        liked=[_liked("v1", category_id="22"), _liked("v2")]
    )

    _ = await import_backup(db, reader, logger=test_logger())

    by_category = await _video(db, "v1")
    bare = await _video(db, "v2")
    assert by_category is not None
    assert bare is not None
    assert_eq(by_category.topic, "22")
    assert_eq(bare.topic, "youtube")


# --- A transcript record attaches to its video ---


@test()
async def transcript_attaches_to_its_liked_video() -> None:
    """A transcript joins its liked video by video_id and becomes searchable."""
    db = await load_fixture(make_db())
    reader = InMemoryLikesBackupReader(
        liked=[_liked("v1")],
        transcripts=[BackupTranscript(video_id="v1", transcript="hello world")],
    )

    report = await import_backup(db, reader, logger=test_logger())

    assert_eq(report.transcripts_imported, 1)
    video = await _video(db, "v1")
    assert video is not None
    assert_eq(video.transcript, "hello world")


# --- An orphan transcript creates a sparse video ---


@test()
async def orphan_transcript_creates_sparse_video() -> None:
    """A transcript whose video is not in the likes table mints a sparse video."""
    db = await load_fixture(make_db())
    reader = InMemoryLikesBackupReader(
        transcripts=[
            BackupTranscript(video_id="orphan", transcript="text", title="Lonely Talk")
        ]
    )

    report = await import_backup(db, reader, logger=test_logger())

    assert_eq(report.orphans_created, 1)
    assert_eq(report.transcripts_imported, 1)
    video = await _video(db, "orphan")
    assert video is not None
    assert_eq(video.title, "Lonely Talk")
    assert_eq(video.transcript, "text")
    assert_eq(video.source, "liked")


# --- Re-running updates rather than duplicates ---


@test()
async def re_running_updates_rather_than_duplicating() -> None:
    """A second import upserts by video_id: same row count, refreshed metadata."""
    db = await load_fixture(make_db())
    first = InMemoryLikesBackupReader(liked=[_liked("v1", title="Old Title")])
    _ = await import_backup(db, first, logger=test_logger())

    second = InMemoryLikesBackupReader(liked=[_liked("v1", title="New Title")])
    report = await import_backup(db, second, logger=test_logger())

    assert_eq(report.videos_updated, 1)
    assert_eq(report.videos_inserted, 0)
    async with db.transaction() as tx:
        rows = await tx.fetch_all(
            select(IngestedVideo).where(IngestedVideo.video_id.eq("v1"))
        )
    assert_eq(len(rows), 1)
    assert_eq(rows[0].title, "New Title")


# --- An existing ignored video stays ignored ---


@test()
async def existing_ignored_video_stays_ignored() -> None:
    """Import does not resurrect a video the user purged: ignore state survives."""
    db = await load_fixture(make_db())
    _ = await import_backup(
        db, InMemoryLikesBackupReader(liked=[_liked("v1")]), logger=test_logger()
    )
    async with db.transaction() as tx:
        _ = await tx.execute(
            update(IngestedVideo)
            .set(IngestedVideo.ignored_at.to(datetime(2024, 1, 1, tzinfo=UTC)))
            .where(IngestedVideo.video_id.eq("v1"))
        )

    _ = await import_backup(
        db,
        InMemoryLikesBackupReader(liked=[_liked("v1", title="Refreshed")]),
        logger=test_logger(),
    )

    video = await _video(db, "v1")
    assert video is not None
    assert_is_not_none(video.ignored_at)
    assert_eq(video.title, "Refreshed")


# --- An existing non-empty transcript is not overwritten by an empty one ---


@test()
async def existing_transcript_not_overwritten_by_empty() -> None:
    """A stored transcript wins over a blank backup transcript for the same id."""
    db = await load_fixture(make_db())
    async with db.transaction() as tx:
        _ = await tx.execute(
            insert(
                IngestedVideo(
                    video_id="v1",
                    source="liked",
                    title="T",
                    channel="C",
                    topic="python",
                    description="",
                    transcript="rich existing transcript",
                )
            )
        )

    reader = InMemoryLikesBackupReader(
        liked=[_liked("v1")],
        transcripts=[BackupTranscript(video_id="v1", transcript="   ")],
    )
    report = await import_backup(db, reader, logger=test_logger())

    # The blank transcript is skipped (malformed) and the rich one is preserved.
    assert_eq(report.transcripts_imported, 0)
    video = await _video(db, "v1")
    assert video is not None
    assert_eq(video.transcript, "rich existing transcript")


@test()
async def existing_transcript_not_overwritten_by_present_backup() -> None:
    """An already-stored transcript is not clobbered by a different backup one."""
    db = await load_fixture(make_db())
    async with db.transaction() as tx:
        _ = await tx.execute(
            insert(
                IngestedVideo(
                    video_id="v1",
                    source="liked",
                    title="T",
                    channel="C",
                    topic="python",
                    description="",
                    transcript="local transcript",
                )
            )
        )

    reader = InMemoryLikesBackupReader(
        transcripts=[BackupTranscript(video_id="v1", transcript="backup transcript")]
    )
    report = await import_backup(db, reader, logger=test_logger())

    assert_eq(report.transcripts_imported, 0)
    video = await _video(db, "v1")
    assert video is not None
    assert_eq(video.transcript, "local transcript")


# --- Malformed rows are skipped and counted ---


@test()
async def malformed_rows_are_skipped_and_counted() -> None:
    """A blank-id like and a blank-id/empty transcript are skipped, not fatal."""
    db = await load_fixture(make_db())
    reader = InMemoryLikesBackupReader(
        liked=[_liked(""), _liked("v1")],
        transcripts=[
            BackupTranscript(video_id="", transcript="x"),
            BackupTranscript(video_id="v2", transcript=""),
            BackupTranscript(video_id="v1", transcript="good"),
        ],
    )

    report = await import_backup(db, reader, logger=test_logger())

    assert_eq(report.videos_inserted, 1)
    assert_eq(report.transcripts_imported, 1)
    assert_eq(report.skipped, 3)
    assert_is_none(await _video(db, ""))
    assert_is_none(await _video(db, "v2"))


# --- Dry-run writes nothing but reports the same counts ---


@test()
async def dry_run_writes_nothing_but_reports_counts() -> None:
    """A dry run computes the full report without persisting any row."""
    db = await load_fixture(make_db())
    reader = InMemoryLikesBackupReader(
        liked=[_liked("v1")],
        transcripts=[
            BackupTranscript(video_id="v1", transcript="t"),
            BackupTranscript(video_id="orphan", transcript="o", title="Orphan"),
        ],
    )

    report = await import_backup(db, reader, dry_run=True, logger=test_logger())

    assert_true(report.dry_run)
    assert_eq(report.videos_inserted, 1)
    assert_eq(report.orphans_created, 1)
    assert_eq(report.transcripts_imported, 2)
    # Nothing was written.
    assert_is_none(await _video(db, "v1"))
    assert_is_none(await _video(db, "orphan"))


@test()
async def dry_run_then_real_import_matches_the_preview() -> None:
    """The counts a dry run reports match what a subsequent real import writes."""
    db = await load_fixture(make_db())
    reader = InMemoryLikesBackupReader(
        liked=[_liked("v1"), _liked("v2")],
        transcripts=[BackupTranscript(video_id="v1", transcript="t")],
    )
    preview = await import_backup(db, reader, dry_run=True, logger=test_logger())

    actual = await import_backup(db, reader, logger=test_logger())

    assert_eq(preview.videos_inserted, actual.videos_inserted)
    assert_eq(preview.transcripts_imported, actual.transcripts_imported)


# --- The production SQLite reader parses the foreign schema ---


def _seed_backup(path: Path) -> None:
    """Write a tiny active-workbench-shaped state.db to `path`."""
    connection = sqlite3.connect(path)
    try:
        _ = connection.execute(
            """
            CREATE TABLE youtube_likes_cache (
                video_id TEXT, title TEXT, channel_title TEXT, description TEXT,
                channel_id TEXT, liked_at TEXT, video_published_at TEXT,
                duration_seconds INTEGER, category_id TEXT, default_language TEXT,
                default_audio_language TEXT, caption_available INTEGER,
                privacy_status TEXT, licensed_content INTEGER, made_for_kids INTEGER,
                live_broadcast_content TEXT, definition TEXT, dimension TEXT,
                statistics_view_count INTEGER, statistics_like_count INTEGER,
                statistics_comment_count INTEGER, statistics_fetched_at TEXT,
                topic_categories TEXT, tags TEXT, thumbnails TEXT)
            """
        )
        _ = connection.execute(
            "CREATE TABLE youtube_transcript_cache (video_id TEXT, transcript TEXT, title TEXT)"
        )
        _ = connection.execute(
            """
            INSERT INTO youtube_likes_cache
            (video_id, title, channel_title, liked_at, caption_available,
            topic_categories, thumbnails) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "v1",
                "Async Python",
                "PyConf",
                "2023-05-01T12:00:00Z",
                1,
                json.dumps(
                    ["https://en.wikipedia.org/wiki/Python_(programming_language)"]
                ),
                json.dumps({"default": "http://img/x.jpg"}),
            ),
        )
        _ = connection.execute(
            "INSERT INTO youtube_transcript_cache (video_id, transcript, title) VALUES (?, ?, ?)",
            ("v1", "hello world", "Async Python"),
        )
        connection.commit()
    finally:
        connection.close()


@test()
async def sqlite_reader_parses_a_fixture_backup() -> None:
    """The production reader normalises a fixture state.db end to end."""
    db = await load_fixture(make_db())
    # snektest has no tmp-path fixture, so use an on-disk temp file.
    with tempfile.TemporaryDirectory() as tmp:
        backup_path = Path(tmp) / "state.db"
        _seed_backup(backup_path)
        reader = SqliteLikesBackupReader(backup_path)

        report = await import_backup(db, reader, logger=test_logger())

    assert_eq(report.videos_inserted, 1)
    assert_eq(report.transcripts_imported, 1)
    video = await _video(db, "v1")
    assert video is not None
    assert_eq(video.title, "Async Python")
    assert_eq(video.channel, "PyConf")
    assert_eq(video.liked_at, datetime(2023, 5, 1, 12, 0, tzinfo=UTC))
    assert_eq(video.caption_available, 1)
    assert_eq(video.topic, "python (programming language)")
    assert_eq(video.transcript, "hello world")
