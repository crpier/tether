"""Destructive #507 host-schema cutover assertions."""

from snekql.sqlite import Config, Database
from snektest import assert_in, assert_not_in, test

from tether.host_schema import create_host_schema


async def table_names(database: Database) -> set[str]:
    async with database.transaction() as transaction:
        connection = transaction.require_connection()
        cursor = await connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'", ()
        )
        rows = await cursor.fetchall()
        await cursor.close()
    return {str(row[0]) for row in rows}


@test()
async def fresh_schema_contains_dreaming_memory_without_legacy_lifecycles() -> None:
    """Current Memory has no row, Review, or Todo-link compatibility tables."""
    database = await Database.initialize(backend=Config(database=":memory:"))
    try:
        await create_host_schema(database)
        tables = await table_names(database)
    finally:
        await database.close()

    assert_in("dreaming_workspace_file", tables)
    assert_not_in("memory", tables)
    assert_not_in("todo_memory", tables)
