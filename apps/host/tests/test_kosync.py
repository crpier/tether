"""Behaviour tests for the kosync ingestion gate's service layer.

These drive `KosyncService` against a real in-memory SQLite database and a real
`MemoryService` over a temp Knowledge base — no HTTP, no device. They assert the
Telemetry storage (append-only events, per-document upsert), the furthest-
progress view, the finished-book derivation (once ever, machine-synced, faceted),
and the hash→title labeling the protocol itself never carries.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import structlog
from anyio import TemporaryDirectory
from opentelemetry import trace
from opentelemetry.trace import Tracer
from snekql.sqlite import Config, Database, Fetched
from snektest import (
    assert_eq,
    assert_is_none,
    assert_is_not_none,
    assert_true,
    fixture,
    load_fixture,
    test,
)

from tether.kosync import (
    FINISHED_THRESHOLD,
    KosyncService,
    ProgressUpdate,
    create_kosync_schema,
    ebook_hash_for_filename,
)
from tether.logging import Logger
from tether.memories import (
    KnowledgeBaseService,
    Memory,
    MemoryService,
    create_memory_schema,
)

_FIXED_NOW = datetime(2026, 7, 19, 12, 0, 0, tzinfo=UTC)


def noop_tracer() -> Tracer:
    """A tracer that emits nowhere."""
    return trace.NoOpTracerProvider().get_tracer("test.kosync")


def test_logger() -> Logger:
    """A throwaway structured logger for the mandatory service logger arg."""
    return structlog.stdlib.get_logger("test.kosync")


def progress(
    document: str = "hash-abc",
    percentage: float = 0.5,
    *,
    progress_marker: str = "/body/DocFragment[3]",
    device: str = "Phone",
    device_id: str = "",
) -> ProgressUpdate:
    """Build a progress push with sensible defaults for the field under test."""
    return ProgressUpdate(
        document=document,
        percentage=percentage,
        progress=progress_marker,
        device=device,
        device_id=device_id,
    )


@dataclass
class KosyncEnv:
    """A kosync-ready database plus a live `MemoryService` over a temp KB."""

    service: KosyncService
    memory_service: MemoryService
    logger: Logger

    async def record(self, update: ProgressUpdate) -> int:
        """Record one push at the fixed test clock, returning the timestamp."""
        return await self.service.record_progress(
            update, logger=self.logger, now=_FIXED_NOW
        )

    async def tethered_memories(self) -> list[Memory[Fetched]]:
        """The current tethered corpus, for finished-derivation assertions."""
        return await self.memory_service.browse_by_state("tethered", logger=self.logger)


@fixture
async def kosync_env() -> AsyncGenerator[KosyncEnv]:
    """A fresh database with the Memory + kosync schema and a live KB dir."""
    database = await Database.initialize(backend=Config(database=":memory:"))
    await create_memory_schema(database)
    await create_kosync_schema(database)
    async with TemporaryDirectory() as kb_root:
        memory_service = MemoryService(
            database=database,
            kb_service=KnowledgeBaseService(kb_root=Path(kb_root)),
            tracer=noop_tracer(),
        )
        yield KosyncEnv(
            service=KosyncService(database=database, memory_service=memory_service),
            memory_service=memory_service,
            logger=test_logger(),
        )
    await database.close()


@test()
async def ebook_hash_for_filename_hashes_the_basename() -> None:
    """The filename-mode hash is `md5` of the basename, directory ignored."""
    assert_eq(
        ebook_hash_for_filename("/mnt/onboard/books/Deep Work.epub"),
        ebook_hash_for_filename("Deep Work.epub"),
    )


@test()
async def record_progress_returns_the_server_timestamp() -> None:
    """The stored timestamp is the server clock, echoed back to the device."""
    env = await load_fixture(kosync_env())

    timestamp = await env.record(progress())

    assert_eq(timestamp, int(_FIXED_NOW.timestamp()))


@test()
async def latest_progress_is_none_for_an_unknown_document() -> None:
    """A document never pushed has no furthest-progress view."""
    env = await load_fixture(kosync_env())

    assert_is_none(await env.service.latest_progress("never-seen"))


@test()
async def latest_progress_reflects_the_newest_push() -> None:
    """The furthest-progress view is the most recent event, not the first."""
    env = await load_fixture(kosync_env())
    _ = await env.record(progress("book", 0.20, progress_marker="/p1"))
    _ = await env.record(progress("book", 0.60, progress_marker="/p2"))

    latest = await env.service.latest_progress("book")

    assert_is_not_none(latest)
    assert_eq(latest.percentage, 0.60)  # pyright: ignore[reportOptionalMemberAccess]
    assert_eq(latest.progress, "/p2")  # pyright: ignore[reportOptionalMemberAccess]


@test()
async def a_first_push_upserts_an_unlabeled_document() -> None:
    """An unknown hash's first push registers it as an unlabeled document."""
    env = await load_fixture(kosync_env())
    _ = await env.record(progress("fresh-hash", 0.10))

    unlabeled = await env.service.list_unlabeled()

    assert_eq([document.document_hash for document in unlabeled], ["fresh-hash"])


@test()
async def crossing_the_threshold_captures_one_finished_memory() -> None:
    """A push at or past the finished line mints exactly one tethered Memory."""
    env = await load_fixture(kosync_env())

    _ = await env.record(progress("done", FINISHED_THRESHOLD))

    assert_eq(len(await env.tethered_memories()), 1)


@test()
async def a_below_threshold_push_captures_nothing() -> None:
    """Progress short of the finished line derives no Memory."""
    env = await load_fixture(kosync_env())

    _ = await env.record(progress("reading", 0.5))

    assert_eq(await env.tethered_memories(), [])


@test()
async def an_unlabeled_finished_memory_names_the_hash() -> None:
    """Finishing an unlabeled document falls back to its hash in the content."""
    env = await load_fixture(kosync_env())

    _ = await env.record(progress("hash-xyz", 0.99))

    memory = (await env.tethered_memories())[0]
    assert_eq(memory.content, "hash-xyz (unlabeled ebook)")


@test()
async def a_labeled_finished_memory_names_the_title() -> None:
    """A labeled document's finished Memory names the book and facets its title."""
    env = await load_fixture(kosync_env())
    _ = await env.service.label_ebook("war", "War and Peace")

    _ = await env.record(progress("war", 0.99))

    memory = (await env.tethered_memories())[0]
    assert_eq(memory.content, "Finished reading War and Peace")
    assert_eq(memory.facets["title"], "War and Peace")


@test()
async def the_finished_memory_is_machine_synced_koreader() -> None:
    """Finished captures land with koreader provenance and the ebook facets."""
    env = await load_fixture(kosync_env())

    _ = await env.record(progress("doc", 0.99))

    memory = (await env.tethered_memories())[0]
    assert_eq(memory.provenance, {"kind": "koreader"})
    assert_eq(memory.facets, {"source": "koreader", "category": "ebook"})


@test()
async def the_finished_memory_fires_once_per_document() -> None:
    """A re-read past the threshold never mints a second finished Memory."""
    env = await load_fixture(kosync_env())
    _ = await env.record(progress("once", 0.99))

    _ = await env.record(progress("once", 1.0))

    assert_eq(len(await env.tethered_memories()), 1)


@test()
async def label_ebook_removes_a_document_from_the_unlabeled_list() -> None:
    """Labeling a document drops it out of the unlabeled listing."""
    env = await load_fixture(kosync_env())
    _ = await env.record(progress("to-label", 0.3))

    _ = await env.service.label_ebook("to-label", "Some Book")

    assert_eq(await env.service.list_unlabeled(), [])


@test()
async def match_ebook_filename_labels_the_computed_hash() -> None:
    """Matching a filename labels the document its basename hashes to."""
    env = await load_fixture(kosync_env())

    document = await env.service.match_ebook_filename("/mnt/Deep Work.epub")

    assert_eq(document.document_hash, ebook_hash_for_filename("Deep Work.epub"))
    assert_eq(document.title, "Deep Work")
    assert_true(document.finished_captured_at is None)
