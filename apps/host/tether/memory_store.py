"""Canonical Memory persistence models and schema migrations."""

from __future__ import annotations

from typing import Literal, NotRequired, TypedDict
from uuid import uuid7

from pydantic import UUID7, Json, PositiveInt
from snekql.sqlite import (
    Blob,
    CurrentTimestamp,
    Database,
    Fetched,
    Integer,
    Model,
    Pending,
    SelectModelQuery,
    Text,
    UtcDatetime,
    select,
)

type MemoryState = Literal["loose", "tethered"]
"""A Memory's trust state. `deleted` is an orthogonal soft-delete marker, not a state."""


class MemoryProvenance(TypedDict):
    """The origin of a Captured Memory.

    `kind` records the source. `confidence` and `batch` are forward-compatible
    optional signals for non-manual producers (import, YouTube, web): a captured
    fact's trustworthiness and the bulk run it arrived in. Manual capture omits
    both, so it still serializes to exactly `{"kind": "manual"}`.
    """

    kind: Literal[
        "manual", "import", "youtube", "web", "readwise", "voice", "koreader", "gmail"
    ]
    confidence: NotRequired[Literal["low", "medium", "high"]]
    batch: NotRequired[str]


class Memory[S = Pending](Model[S, "Memory[Fetched]"]):
    """Canonical SQLite record for a captured Memory."""

    id: Memory.GenCol[UUID7] = Text(
        primary_key=True,
        default_factory=uuid7,
    )
    content: Memory.Col[str] = Text()
    """The actual content of the Memory."""
    version: Memory.Col[PositiveInt] = Integer(default=1)
    """Version number used for optimistic concurrency control."""
    provenance: Memory.Col[Json[MemoryProvenance]] = Text(
        default_factory=lambda: MemoryProvenance(kind="manual"),
    )
    """The origin of a Captured Memory."""
    facets: Memory.Col[Json[dict[str, str]]] = Text(default_factory=dict[str, str])
    """The Commons facet set: a flat `{"key": "value"}` map, one string value per
    key. Naming convention is lowercase snake_case keys and free-form lowercase
    string values, documented but not validated here — key/value drift is
    handled by curation (`rename_facet_key` / `merge_facet_value`), not code.
    `sensitivity` is a reserved key name but stored and treated like any other
    facet; there is no special-cased code path for it."""
    created_at: Memory.GenCol[UtcDatetime] = Text(default=CurrentTimestamp)
    updated_at: Memory.GenCol[UtcDatetime] = Text(default=CurrentTimestamp)
    tethered_at: Memory.Col[UtcDatetime | None] = Text(
        default=None,
        nullable=True,
    )
    deleted_at: Memory.Col[UtcDatetime | None] = Text(
        default=None,
        nullable=True,
    )
    embedding: Memory.Col[bytes | None] = Blob(
        default=None,
        nullable=True,
    )
    """Canonical embedding vector for this Memory, as raw bytes.

    SQLite is the source of truth for the vector; the LanceDB index is a derived
    projection rebuilt from it. `None` until the embedder has run."""
    embedded_version: Memory.Col[int | None] = Integer(
        default=None,
        nullable=True,
    )
    """The content `version` the stored `embedding` reflects.

    `None` means an embedding is owed (never produced, or content changed since).
    The reconciler embeds any Memory whose `embedded_version != version`."""


def tethered_corpus() -> SelectModelQuery[Memory, Memory[Fetched]]:
    """Select the trusted, non-deleted Memory corpus."""
    return select(Memory).where(
        Memory.tethered_at.is_not_null() & Memory.deleted_at.is_null()
    )


def loose_queue() -> SelectModelQuery[Memory, Memory[Fetched]]:
    """Select loose, non-deleted Memories awaiting Review."""
    return select(Memory).where(
        Memory.tethered_at.is_null() & Memory.deleted_at.is_null()
    )


# snekql builds schema by replaying a hand-authored migration chain and records
# each step by *name*, never re-running or checksumming an applied one. So a
# migration body must be frozen at authoring time: editing `001_memories` to add
# columns (e.g. via `scaffold([Memory])`, which regenerates the current model)
# adds them to fresh databases but silently skips every existing one. New columns
# therefore arrive as their own forward migration, applied on top of the frozen
# base. Replaying the whole chain on a fresh database yields the current schema.
_MEMORY_MIGRATIONS: dict[str, str] = {
    # Original Memory table, as first shipped (before embedding columns). Frozen.
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
    # Canonical embedding vector plus the content `version` it reflects. Kept as
    # forward migrations so databases created before hybrid search gain the fields.
    "002_memory_embedding": 'ALTER TABLE "memory" ADD COLUMN "embedding" BLOB',
    "003_memory_embedded_version": (
        'ALTER TABLE "memory" ADD COLUMN "embedded_version" INTEGER'
    ),
    # Commons facets as a flat JSON `{"key": "value"}` map. Existing rows
    # backfill to '{}' via the column default.
    "004_memory_facets": (
        'ALTER TABLE "memory" ADD COLUMN "facets" TEXT NOT NULL DEFAULT \'{}\''
    ),
}


async def create_memory_schema(database: Database) -> None:
    """Bring the Memory table to the current schema on an initialized database.

    The frozen migration chain upgrades existing databases and creates fresh
    databases without changing historical migration bodies.
    """
    await database.migrate(_MEMORY_MIGRATIONS)
