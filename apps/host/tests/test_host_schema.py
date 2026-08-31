"""Destructive #507 host-schema cutover assertions."""

import hashlib
import json

from snekql.sqlite import Config, Database
from snektest import assert_eq, assert_in, assert_not_in, test

from tether.host_schema import create_host_schema, host_migrations


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
async def host_migration_chain_is_byte_and_order_stable() -> None:
    """Deployed migration identities, order, and checksums cannot drift."""
    encoded = json.dumps(await host_migrations(), separators=(",", ":")).encode()

    assert_eq(
        hashlib.sha256(encoded).hexdigest(),
        "029d81738e786bde2c45031d92383999f08be7feda9841a71f1a778b14dfd403",
    )


@test()
async def deployed_legacy_histories_are_exact_prefix_sets() -> None:
    """Every observed pre-0.6 host history is adoptable as one chain prefix."""
    names = list(await host_migrations())
    expected_prefixes = {
        107: "0286795c89a1489edaf9774d3f08dabedb9ecb2189f216e6d055917681375715",
        161: "0d36babf0fa7f110dbfef6e0918c35d5210166bda1c77013619680c2d6e3b83f",
        168: "cda53b047cbf2290ab16e6b6caec3e21c092ce176d14044e6b0323ebd6eaf5d9",
    }

    for length, expected_hash in expected_prefixes.items():
        encoded = json.dumps(sorted(names[:length]), separators=(",", ":")).encode()
        assert_eq(hashlib.sha256(encoded).hexdigest(), expected_hash)


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


@test()
async def observed_legacy_histories_adopt_and_apply_the_pending_suffix() -> None:
    """Every observed name-only history upgrades through the complete chain."""
    migrations = await host_migrations()
    for legacy_length in (107, 161, 168):
        database = await Database.initialize(backend=Config(database=":memory:"))
        await database.migrate(dict(list(migrations.items())[:legacy_length]))
        async with database.transaction(mode="immediate") as transaction:
            connection = transaction.require_connection()
            cursor = await connection.execute(
                'SELECT "name", "applied_at" FROM "snekql_migrations" '
                'ORDER BY "position"',
                (),
            )
            legacy_rows = await cursor.fetchall()
            await cursor.close()
            cursor = await connection.execute('DROP TABLE "snekql_migrations"', ())
            await cursor.close()
            cursor = await connection.execute(
                'CREATE TABLE "snekql_migrations" ('
                '"name" TEXT PRIMARY KEY NOT NULL, '
                '"applied_at" TEXT NOT NULL) STRICT',
                (),
            )
            await cursor.close()
            for legacy_row in legacy_rows:
                cursor = await connection.execute(
                    'INSERT INTO "snekql_migrations" ("name", "applied_at") '
                    "VALUES (?, ?)",
                    tuple(legacy_row),
                )
                await cursor.close()

        try:
            await create_host_schema(database)

            async with database.transaction() as transaction:
                connection = transaction.require_connection()
                cursor = await connection.execute(
                    'SELECT "position", "name", "checksum" '
                    'FROM "snekql_migrations" ORDER BY "position"',
                    (),
                )
                adopted_rows = await cursor.fetchall()
                await cursor.close()
        finally:
            await database.close()

        assert_eq(len(legacy_rows), legacy_length)
        assert_eq(len(adopted_rows), len(migrations))
        assert_eq([row[0] for row in adopted_rows], list(range(1, len(migrations) + 1)))
        assert_eq([row[1] for row in adopted_rows], list(migrations))
        assert_eq(all(len(str(row[2])) == 64 for row in adopted_rows), True)
