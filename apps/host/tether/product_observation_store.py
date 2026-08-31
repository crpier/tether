"""SQLite model and schema for durable Product observations."""

from __future__ import annotations

from typing import ClassVar
from uuid import uuid7

from pydantic import UUID7, PositiveInt
from snekql import sqlite
from snekql.sqlite import (
    CurrentTimestamp,
    Database,
    Fetched,
    Index,
    Integer,
    Model,
    Pending,
    Text,
    UtcDatetime,
)

from tether.product_observation_model import ProductObservationStatus


class ProductObservation[S = Pending](Model[S, "ProductObservation[Fetched]"]):
    """One explicit piece of product feedback from a Conversation."""

    id: sqlite.GenCol[UUID7] = Text(primary_key=True, default_factory=uuid7)  # ty: ignore[invalid-assignment]
    wording: sqlite.Col[str] = Text()
    """The exact user Message that prompted capture."""
    interpretation: sqlite.Col[str] = Text()
    """A concise statement of the behavior the user expected."""
    conversation_id: sqlite.Col[UUID7] = Text()
    message_id: sqlite.Col[UUID7] = Text()
    status: sqlite.Col[ProductObservationStatus] = Text()
    version: sqlite.Col[PositiveInt] = Integer(default=1)
    created_at: sqlite.GenCol[UtcDatetime] = Text(default=CurrentTimestamp)
    updated_at: sqlite.GenCol[UtcDatetime] = Text(default=CurrentTimestamp)
    resolved_at: sqlite.Col[UtcDatetime | None] = Text(default=None, nullable=True)

    __indexes__: ClassVar = [Index(status, created_at)]


async def create_product_observation_schema(database: Database) -> None:
    """Create Product-observation storage on an initialized database."""
    await database.migrate(
        {
            "032_create_product_observation": (
                'CREATE TABLE "product_observation" ('
                '"id" TEXT PRIMARY KEY NOT NULL, "wording" TEXT NOT NULL, '
                '"interpretation" TEXT NOT NULL, "conversation_id" TEXT NOT NULL, '
                '"message_id" TEXT NOT NULL, "status" TEXT NOT NULL, '
                '"version" INTEGER NOT NULL, '
                '"created_at" TEXT NOT NULL DEFAULT '
                "(strftime('%Y-%m-%dT%H:%M:%fZ', 'now')), "
                '"updated_at" TEXT NOT NULL DEFAULT '
                "(strftime('%Y-%m-%dT%H:%M:%fZ', 'now')), "
                '"resolved_at" TEXT'
                ") STRICT"
            ),
            "032_create_index_ix_product_observation_status_created_at": (
                'CREATE INDEX "ix_product_observation_status_created_at" '
                'ON "product_observation" ("status", "created_at")'
            ),
        }
    )
