"""SQLite model, persistence operations, and schema for notifications."""

from __future__ import annotations

from typing import ClassVar
from uuid import UUID, uuid7

from pydantic import UUID7
from snekql import sqlite
from snekql.sqlite import (
    CurrentTimestamp,
    Database,
    Fetched,
    Index,
    Model,
    Pending,
    Text,
    UtcDatetime,
    insert,
    select,
    update,
)
from snekql.sqlite._schema_ddl import scaffold_sqlite_statements

from tether.notification_model import NotificationDraft


class Notification[S = Pending](Model[S, "Notification[Fetched]"]):
    """One durable delivered notification, optionally tied to a trigger."""

    id: sqlite.GenCol[UUID7] = Text(primary_key=True, default_factory=uuid7)  # ty: ignore[invalid-assignment]
    trigger_id: sqlite.Col[str | None] = Text(default=None, nullable=True)
    action_kind: sqlite.Col[str | None] = Text(default=None, nullable=True)
    source_label: sqlite.Col[str | None] = Text(default=None, nullable=True)
    body: sqlite.Col[str] = Text()
    created_at: sqlite.GenCol[UtcDatetime] = Text(default=CurrentTimestamp)
    dismissed_at: sqlite.Col[UtcDatetime | None] = Text(
        default=None,
        nullable=True,
    )

    __indexes__: ClassVar = [Index(dismissed_at, created_at)]


class NotificationStore:
    """Persist, list, and convergently dismiss notification rows."""

    def __init__(self, database: Database) -> None:
        self.database: Database = database

    async def record(self, draft: NotificationDraft) -> Notification[Fetched]:
        """Insert one resolved notification and return its canonical row."""
        async with self.database.transaction(mode="immediate") as transaction:
            return await transaction.execute(
                insert(
                    Notification(
                        body=draft.body,
                        trigger_id=draft.trigger_id,
                        action_kind=draft.action_kind,
                        source_label=draft.source_label,
                    )
                ).returning()
            )

    async def list_recent(self, *, limit: int) -> list[Notification[Fetched]]:
        """List live notifications newest-first up to the requested limit."""
        async with self.database.transaction() as transaction:
            return await transaction.fetch_all(
                select(Notification)
                .where(Notification.dismissed_at.is_null())
                .order_by(Notification.created_at.desc())
                .order_by(Notification.id.desc())
                .limit(limit)
            )

    async def dismiss(self, notification_id: UUID) -> bool:
        """Convergently dismiss one row and report whether state changed."""
        async with self.database.transaction(mode="immediate") as transaction:
            matched = await transaction.execute(
                update(Notification)
                .set(Notification.dismissed_at.to(CurrentTimestamp))
                .where(Notification.id.eq(notification_id))
                .where(Notification.dismissed_at.is_null())
            )
        return bool(matched)

    async def clear(self) -> int:
        """Dismiss every live notification and return the affected count."""
        async with self.database.transaction(mode="immediate") as transaction:
            return await transaction.execute(
                update(Notification)
                .set(Notification.dismissed_at.to(CurrentTimestamp))
                .where(Notification.dismissed_at.is_null())
            )


async def create_notification_schema(database: Database) -> None:
    """Create the notification table and its index on an initialized database."""
    migrations = {
        f"009_{label}": sql for label, sql in scaffold_sqlite_statements([Notification])
    }
    await database.migrate(migrations)
