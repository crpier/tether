"""Behavioural tests for the Memory Capture + active-list slice.

These exercise the application services through real SQLite + a real filesystem
under a temporary state root, since the risk in this slice is the consistency
between the two stores (a queryable row *and* a Memory Document on disk).

The imported names below are a proposed contract, not a mandate: rename or
restructure freely as you implement, then bring the tests along.
"""

from snekql.sqlite import Config, Database
from snektest import AsyncFixture, assert_eq, assert_in, load_fixture, test

from tether.memory import (
    MemoryItem,
    MemoryService,
    SourceRef,
)


class NullStructuredLogger:
    """Structured logger fake that intentionally ignores all events."""

    def debug(self, event: str, **fields: object) -> None:
        _ = event
        _ = fields

    def info(self, event: str, **fields: object) -> None:
        _ = event
        _ = fields

    def warning(self, event: str, **fields: object) -> None:
        _ = event
        _ = fields

    def error(self, event: str, **fields: object) -> None:
        _ = event
        _ = fields


async def memory_service() -> AsyncFixture[MemoryService]:
    db = await Database.initialize(
        backend=Config(database=":memory:"), logger=NullStructuredLogger()
    )
    yield MemoryService(database=db)
    await db.close()


@test()
async def test_captured_memory_appears_in_active_list() -> None:
    """A captured Memory is returned by the active Memory list."""
    services = await load_fixture(memory_service())

    captured = await services.capture_memory(
        MemoryItem(
            title="Espresso ratio",
            body="Pull 1:2 in ~28s for the house blend.",
            tags=["coffee", "ratios"],
        )
    )
    active = await services.list_active_memories()

    assert_eq([memory.id for memory in active], [captured.id])


@test()
async def test_capture_writes_markdown_document_with_body() -> None:
    """Capture writes the authored body to the Memory Document on disk."""
    services = await load_fixture(memory_service())

    captured = await services.capture_memory(
        MemoryItem(title="Knife skills", body="Claw grip; rock the blade.", tags=[])
    )
    document_text = captured.document_path.read_text(encoding="utf-8")

    assert_in("Claw grip; rock the blade.", document_text)


@test()
async def test_capture_defaults_to_manual_provenance() -> None:
    """A Memory captured without Source Refs is stamped with manual provenance."""
    services = await load_fixture(memory_service())

    captured = await services.capture_memory(
        MemoryItem(title="Plain note", body="No source given.", tags=[])
    )

    assert_eq(captured.source_refs, [SourceRef(kind="manual")])
