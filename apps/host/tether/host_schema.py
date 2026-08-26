"""Ordered schema composition for retained host domains."""

from snekql.sqlite import Database

from tether.bucket_item_store import create_bucket_item_schema
from tether.todo_store import create_todo_schema


async def create_host_schema(database: Database) -> None:
    """Apply only retained domain migrations, leaving legacy tables untouched."""
    await create_bucket_item_schema(database)
    await create_todo_schema(database)


__all__ = ["create_host_schema"]
