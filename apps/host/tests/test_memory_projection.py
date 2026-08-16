"""Behavior tests for the Markdown Memory projection boundary."""

from collections.abc import AsyncGenerator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid7

from anyio import TemporaryDirectory
from snekql.sqlite import Config, Database, Fetched, insert, select, update
from snektest import (
    assert_eq,
    assert_in,
    assert_not_in,
    assert_true,
    fixture,
    load_fixture,
    test,
)

from tether.memory_projection import (
    KnowledgeBaseService,
    decode_projection_frontmatter,
)
from tether.memory_store import Memory, create_memory_schema


@dataclass(frozen=True, slots=True)
class ProjectionHarness:
    """A canonical Memory and isolated projection directory."""

    database: Database
    knowledge_base: KnowledgeBaseService
    memory: Memory[Fetched]
    projection_path: Path


@fixture
async def projection_harness() -> AsyncGenerator[ProjectionHarness]:
    """Create a persisted Memory and projection target with owned lifetimes."""
    database = await Database.initialize(backend=Config(database=":memory:"))
    await create_memory_schema(database)
    async with database.transaction() as transaction:
        memory = await transaction.execute(
            insert(
                Memory(
                    content="I prefer aisle seats",
                    facets={"topic": "travel"},
                    provenance={"kind": "manual"},
                )
            ).returning()
        )
    async with TemporaryDirectory() as kb_root:
        yield ProjectionHarness(
            database=database,
            knowledge_base=KnowledgeBaseService(kb_root=Path(kb_root)),
            memory=memory,
            projection_path=Path(kb_root) / f"{memory.id}.md",
        )
    await database.close()


@test()
def projection_frontmatter_decodes_the_persisted_memory_identity() -> None:
    """Projection metadata round-trips the canonical Memory identity and facets."""
    memory_id = uuid7()
    projected_at = datetime(2026, 8, 16, 8, 30, tzinfo=UTC)

    frontmatter = decode_projection_frontmatter(
        "\n".join(
            (
                "---",
                f"id: {memory_id}",
                f"created_at: {projected_at.isoformat()}",
                f"updated_at: {projected_at.isoformat()}",
                "provenance:",
                "  kind: manual",
                "tethered_at: null",
                "facets:",
                "  topic: travel",
                "---",
                "Memory body",
            )
        )
        + "\n"
    )

    assert_eq(frontmatter.id, memory_id)
    assert_eq(frontmatter.facets, {"topic": "travel"})


@test()
async def setting_a_projection_creates_the_complete_markdown_document() -> None:
    """Projection writes expose canonical metadata and Memory content together."""
    harness = await load_fixture(projection_harness())

    await harness.knowledge_base.set_projection(harness.memory)

    assert_true(harness.projection_path.exists())
    projection_content = harness.projection_path.read_text()
    assert_in("topic: travel", projection_content)
    assert_in("I prefer aisle seats", projection_content)


@test()
async def replacing_a_projection_drops_the_previous_memory_content() -> None:
    """Atomic replacement leaves one document containing only current content."""
    harness = await load_fixture(projection_harness())
    await harness.knowledge_base.set_projection(harness.memory)

    async with harness.database.transaction() as transaction:
        _ = await transaction.execute(
            update(Memory)
            .set(Memory.content.to("I prefer window seats"))
            .where(Memory.id.eq(harness.memory.id))
        )
        updated_memory = await transaction.fetch_one(
            select(Memory).where(Memory.id.eq(harness.memory.id))
        )
    await harness.knowledge_base.set_projection(updated_memory)

    projection_content = harness.projection_path.read_text()
    assert_in("I prefer window seats", projection_content)
    assert_not_in("I prefer aisle seats", projection_content)


@test()
async def removing_an_absent_projection_is_idempotent() -> None:
    """Projection cleanup succeeds whether or not the derived file exists."""
    harness = await load_fixture(projection_harness())

    await harness.knowledge_base.remove_projection(harness.memory.id)
    await harness.knowledge_base.remove_projection(harness.memory.id)

    assert_true(not harness.projection_path.exists())
