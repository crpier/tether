"""Shared database setup for tests spanning YouTube and Transcription."""

from snekql.sqlite import Database

from tether.transcripts.store import transcript_migrations
from tether.youtube.store import (
    _youtube_migrations,
    legacy_youtube_transcript_migrations,
    remove_legacy_youtube_transcript_storage,
)


async def create_youtube_transcript_test_schema(database: Database) -> None:
    """Install YouTube, Transcription, and their one-time compatibility migration."""
    await database.migrate(
        {
            **_youtube_migrations(),
            **transcript_migrations(),
            **legacy_youtube_transcript_migrations(),
        }
    )
    await remove_legacy_youtube_transcript_storage(database)
