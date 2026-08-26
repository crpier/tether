"""Retained host schema composition tests."""

from snekql.sqlite import Config, Database
from snektest import assert_in, test

from tether.host_schema import create_host_schema


async def table_names(database: Database) -> set[str]:
    """Read SQLite table names without depending on a deleted domain model."""
    async with database.transaction() as transaction:
        cursor = await transaction.require_connection().execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'", ()
        )
        rows = await cursor.fetchall()
        await cursor.close()
    return {str(row[0]) for row in rows}


@test()
async def fresh_schema_contains_retained_canonical_domains() -> None:
    """A new main database creates Bucket and Todo persistence."""
    async with await Database.initialize(Config(database=":memory:")) as database:
        await create_host_schema(database)

        tables = await table_names(database)

    assert_in("bucket_item", tables)
    assert_in("todo", tables)


@test()
async def schema_composition_keeps_legacy_tables_inert() -> None:
    """Startup never destructively removes rollback-only assistant tables."""
    async with await Database.initialize(Config(database=":memory:")) as database:
        async with database.transaction(mode="immediate") as transaction:
            cursor = await transaction.require_connection().execute(
                "CREATE TABLE conversation (id TEXT PRIMARY KEY)", ()
            )
            await cursor.close()

        await create_host_schema(database)
        tables = await table_names(database)

    assert_in("conversation", tables)
