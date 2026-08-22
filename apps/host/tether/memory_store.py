"""Destructive schema cutover from the legacy Memory-row lifecycle."""

from __future__ import annotations

from snekql.sqlite import Database

# Historical bodies remain frozen because snekql records migration names only.
# Fresh databases replay the old shape and immediately drop it; upgraded
# databases drop their existing loose/tethered corpus. Current Memory state is
# DreamingWorkspaceFile plus canonical Markdown, not a replacement row model.
_MEMORY_MIGRATIONS: dict[str, str] = {
    "001_memories": (
        'CREATE TABLE "memory" ('
        '"id" TEXT PRIMARY KEY, '
        '"content" TEXT, '
        '"version" INTEGER, '
        '"provenance" TEXT, '
        "\"created_at\" TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')), "
        "\"updated_at\" TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')), "
        '"tethered_at" TEXT, '
        '"deleted_at" TEXT'
        ") STRICT"
    ),
    "002_memory_embedding": 'ALTER TABLE "memory" ADD COLUMN "embedding" BLOB',
    "003_memory_embedded_version": (
        'ALTER TABLE "memory" ADD COLUMN "embedded_version" INTEGER'
    ),
    "004_memory_facets": (
        'ALTER TABLE "memory" ADD COLUMN "facets" TEXT NOT NULL DEFAULT \'{}\''
    ),
    "027_drop_legacy_memory": 'DROP TABLE "memory"',
}


async def create_memory_schema(database: Database) -> None:
    """Apply the intentional #507 removal of legacy Memory rows."""
    await database.migrate(_MEMORY_MIGRATIONS)
