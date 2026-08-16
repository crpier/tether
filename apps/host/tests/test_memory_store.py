"""Behavior tests for canonical Memory persistence and schema upgrades."""

from collections.abc import AsyncGenerator
from uuid import uuid7

from snekql.sqlite import Config, Database, insert, select
from snektest import assert_eq, assert_is_none, fixture, load_fixture, test

from tether.memory_store import Memory, create_memory_schema

_LEGACY_MEMORY_DDL = (
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
)


@fixture
async def legacy_memory_database() -> AsyncGenerator[Database]:
    """Create an existing database that predates embeddings and facets."""
    database = await Database.initialize(backend=Config(database=":memory:"))
    await database.migrate({"001_memories": _LEGACY_MEMORY_DDL})
    yield database
    await database.close()


@test()
async def fresh_schema_persists_the_current_memory_shape() -> None:
    """Fresh databases support every field required by the canonical model."""
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

    assert_eq(memory.facets, {"topic": "travel"})
    assert_is_none(memory.embedding)
    assert_is_none(memory.embedded_version)
    await database.close()


@test()
async def upgrading_a_legacy_database_preserves_rows_and_backfills_facets() -> None:
    """Forward migrations retain old Memories while adding current fields."""
    database = await load_fixture(legacy_memory_database())
    memory_id = uuid7()
    async with database.transaction() as transaction:
        connection = transaction.require_connection()
        _ = await connection.execute(
            'INSERT INTO "memory" (id, content, version, provenance) VALUES (?, ?, ?, ?)',
            (str(memory_id), "a legacy memory", 1, '{"kind": "manual"}'),
        )

    await create_memory_schema(database)

    async with database.transaction() as transaction:
        memory = await transaction.fetch_one(
            select(Memory).where(Memory.id.eq(memory_id))
        )
    assert_eq(memory.facets, {})
    assert_is_none(memory.embedding)
    assert_is_none(memory.embedded_version)
