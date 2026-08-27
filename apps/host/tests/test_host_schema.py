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
    assert_not_in("autonomy_grant", tables)
    assert_not_in("memory", tables)
    assert_not_in("proposal", tables)
    assert_not_in("proposal_action", tables)
    assert_not_in("todo_memory", tables)


@test()
async def existing_proposal_tables_remain_inert_for_rollback() -> None:
    """Current schema composition leaves old Proposal storage untouched."""
    database = await Database.initialize(backend=Config(database=":memory:"))
    try:
        async with database.transaction() as transaction:
            connection = transaction.require_connection()
            _ = await connection.execute('CREATE TABLE "proposal" ("id" TEXT)', ())
            _ = await connection.execute(
                'CREATE TABLE "proposal_action" ("id" TEXT)', ()
            )
            _ = await connection.execute(
                'CREATE TABLE "autonomy_grant" ("id" TEXT)', ()
            )
        await create_host_schema(database)
        tables = await table_names(database)
    finally:
        await database.close()

    assert_in("autonomy_grant", tables)
    assert_in("proposal", tables)
    assert_in("proposal_action", tables)
