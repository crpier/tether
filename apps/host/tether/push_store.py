"""SQLite model, lifecycle operations, and schema for Push subscriptions."""

from __future__ import annotations

from uuid import uuid7

from pydantic import UUID7
from snekql import sqlite
from snekql.sqlite import (
    CurrentTimestamp,
    Database,
    Fetched,
    Model,
    Pending,
    Text,
    UtcDatetime,
    insert,
    select,
    update,
)
from snekql.sqlite._schema_ddl import scaffold_sqlite_statements

from tether.push_model import PushStatus


class PushSubscription[S = Pending](Model[S, "PushSubscription[Fetched]"]):
    """One browser subscription keyed by its provider endpoint."""

    id: sqlite.GenCol[UUID7] = Text(primary_key=True, default_factory=uuid7)  # ty: ignore[invalid-assignment]
    endpoint: sqlite.Col[str] = Text(unique=True)
    p256dh: sqlite.Col[str] = Text()
    auth: sqlite.Col[str] = Text()
    created_at: sqlite.GenCol[UtcDatetime] = Text(default=CurrentTimestamp)
    updated_at: sqlite.GenCol[UtcDatetime] = Text(default=CurrentTimestamp)
    deleted_at: sqlite.Col[UtcDatetime | None] = Text(
        default=None,
        nullable=True,
    )


class PushStore:
    """Persist and convergently mutate browser Push subscriptions."""

    def __init__(self, database: Database) -> None:
        self.database: Database = database

    async def subscribe(
        self,
        endpoint: str,
        *,
        p256dh: str,
        auth: str,
    ) -> PushSubscription[Fetched]:
        """Insert, refresh, or revive the one row for an endpoint."""
        async with self.database.transaction(mode="immediate") as transaction:
            existing = await transaction.fetch_one_or_none(
                select(PushSubscription).where(PushSubscription.endpoint.eq(endpoint))
            )
            if existing is None:
                return await transaction.execute(
                    insert(
                        PushSubscription(endpoint=endpoint, p256dh=p256dh, auth=auth)
                    ).returning()
                )
            _ = await transaction.execute(
                update(PushSubscription)
                .set(PushSubscription.p256dh.to(p256dh))
                .set(PushSubscription.auth.to(auth))
                .set(PushSubscription.deleted_at.to(None))
                .set(PushSubscription.updated_at.to(CurrentTimestamp))
                .where(PushSubscription.endpoint.eq(endpoint))
            )
            refreshed = await transaction.fetch_one_or_none(
                select(PushSubscription).where(PushSubscription.endpoint.eq(endpoint))
            )
            assert refreshed is not None
            return refreshed

    async def unsubscribe(self, endpoint: str) -> bool:
        """Convergently soft-delete an endpoint and report whether it changed."""
        async with self.database.transaction(mode="immediate") as transaction:
            matched = await transaction.execute(
                update(PushSubscription)
                .set(PushSubscription.deleted_at.to(CurrentTimestamp))
                .set(PushSubscription.updated_at.to(CurrentTimestamp))
                .where(PushSubscription.endpoint.eq(endpoint))
                .where(PushSubscription.deleted_at.is_null())
            )
        return bool(matched)

    async def status(self, endpoint: str | None = None) -> PushStatus:
        """Report live subscription count and endpoint membership."""
        live = await self.active_subscriptions()
        count = len(live)
        if endpoint is None:
            return PushStatus(subscribed=count > 0, count=count)
        return PushStatus(
            subscribed=any(subscription.endpoint == endpoint for subscription in live),
            count=count,
        )

    async def active_subscriptions(self) -> list[PushSubscription[Fetched]]:
        """List all subscriptions eligible for delivery."""
        async with self.database.transaction() as transaction:
            return await transaction.fetch_all(
                select(PushSubscription).where(PushSubscription.deleted_at.is_null())
            )


async def create_push_schema(database: Database) -> None:
    """Create the push-subscription table on an initialized database."""
    migrations = {
        f"006_{label}": sql
        for label, sql in scaffold_sqlite_statements([PushSubscription])
    }
    await database.migrate(migrations)
