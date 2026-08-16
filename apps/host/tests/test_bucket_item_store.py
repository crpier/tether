"""Behavior tests for canonical Bucket Item persistence and schema replay."""

from collections.abc import AsyncGenerator
from uuid import uuid7

from snekql.sqlite import Config, Database, insert, select
from snektest import assert_eq, fixture, load_fixture, test

from tether.bucket_item_store import BucketItem, create_bucket_item_schema

_LEGACY_BUCKET_ITEM_MIGRATIONS = {
    "002_create_bucket_item": (
        'CREATE TABLE "bucket_item" ('
        '"id" TEXT PRIMARY KEY NOT NULL, "item_type" TEXT NOT NULL, '
        '"title" TEXT NOT NULL, "dedup_key" TEXT NOT NULL, "data" TEXT NOT NULL, '
        '"intent_context" TEXT NOT NULL, "provenance" TEXT NOT NULL, '
        '"version" INTEGER NOT NULL, '
        '"created_at" TEXT NOT NULL DEFAULT '
        "(strftime('%Y-%m-%dT%H:%M:%fZ', 'now')), "
        '"updated_at" TEXT NOT NULL DEFAULT '
        "(strftime('%Y-%m-%dT%H:%M:%fZ', 'now')), "
        '"completed_at" TEXT, "deleted_at" TEXT) STRICT'
    ),
    "002_create_index_ix_bucket_item_item_type_dedup_key": (
        'CREATE INDEX "ix_bucket_item_item_type_dedup_key" '
        'ON "bucket_item" ("item_type", "dedup_key")'
    ),
}


@fixture
async def existing_bucket_item_database() -> AsyncGenerator[Database]:
    """Create a database carrying the original Bucket Item migration chain."""
    database = await Database.initialize(backend=Config(database=":memory:"))
    await database.migrate(_LEGACY_BUCKET_ITEM_MIGRATIONS)
    yield database
    await database.close()


@test()
async def fresh_schema_persists_the_current_bucket_item_shape() -> None:
    """Fresh databases support every canonical Bucket Item field."""
    database = await Database.initialize(backend=Config(database=":memory:"))
    await create_bucket_item_schema(database)
    async with database.transaction(mode="immediate") as transaction:
        item = await transaction.execute(
            insert(
                BucketItem(
                    data={"title": "Dune"},
                    dedup_key="dune",
                    intent_context="recommended",
                    item_type="movie",
                    title="Dune",
                )
            ).returning()
        )

    assert_eq(item.data, {"title": "Dune"})
    assert_eq(item.provenance, {"kind": "manual"})
    await database.close()


@test()
async def replaying_schema_on_an_existing_database_preserves_rows() -> None:
    """Recognized migration names leave existing Bucket Items untouched."""
    database = await load_fixture(existing_bucket_item_database())
    item_id = uuid7()
    async with database.transaction(mode="immediate") as transaction:
        connection = transaction.require_connection()
        _ = await connection.execute(
            "".join(
                (
                    'INSERT INTO "bucket_item" ',
                    "(id, item_type, title, dedup_key, data, intent_context, ",
                    "provenance, version) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                )
            ),
            (
                str(item_id),
                "movie",
                "Dune",
                "dune",
                '{"title":"Dune"}',
                "recommended",
                '{"kind":"manual"}',
                1,
            ),
        )

    await create_bucket_item_schema(database)

    async with database.transaction() as transaction:
        item = await transaction.fetch_one(
            select(BucketItem).where(BucketItem.id.eq(item_id))
        )
    assert_eq(item.title, "Dune")
