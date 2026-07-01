"""Persisted user-facing notifications: record / list / dismiss / clear.

A notification is the durable record of a fired Scheduled trigger's delivery.
The live delivery path still fans a `NotifyEvent` out over the open WebSocket
(see `tether.scheduler`), but that frame is ephemeral — a browser that was
closed when the trigger fired never saw it. Persisting each fired delivery here
lets the panel show a timestamped history that survives a reload, carry the
source that produced it, and be dismissed one at a time or cleared wholesale.

Dismissal is convergent: dismissing an already-dismissed (or absent)
notification is a no-op, not an error, so a client that lost track can always
converge on "gone". Listing returns only the still-live (undismissed) rows,
newest first, so the panel reads as a recent-activity feed.

>>> service = NotificationService(database=database)
>>> _ = await service.record(NotificationDraft(body="stand up", action_kind="message"))
>>> len(await service.list_recent())
1
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import ClassVar
from uuid import UUID, uuid7

from pydantic import UUID7, BaseModel
from snekql.sqlite import (
    CurrentTimestamp,
    Database,
    Fetched,
    Index,
    Model,
    Pending,
    Text,
    Transaction,
    insert,
    select,
    update,
)
from snekql.sqlite._schema_ddl import scaffold_sqlite_statements
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from tether.db_retry import run_in_transaction
from tether.events import EventPublisher, InvalidateEvent, NullEventPublisher
from tether.openapi import EndpointRoute, endpoint

DEFAULT_LIST_LIMIT = 50
"""How many recent notifications the panel loads by default."""


def _as_utc(value: datetime) -> datetime:
    """Read a stored timestamp as UTC-aware; SQLite `CURRENT_TIMESTAMP` is naive."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


class Notification[S = Pending](Model[S, "Notification[Fetched]"]):
    """One delivered notification, kept after its live frame has gone."""

    id: Notification.GenCol[UUID7] = Text(primary_key=True, default_factory=uuid7)
    trigger_id: Notification.Col[str | None] = Text(default=None, nullable=True)
    """The Scheduled trigger that produced this, when it came from one."""
    action_kind: Notification.Col[str | None] = Text(default=None, nullable=True)
    """The producing action (`message` or `prompt`); differentiates the source."""
    source_label: Notification.Col[str | None] = Text(default=None, nullable=True)
    """Human-facing origin — the reminder text or the agent prompt that fired."""
    body: Notification.Col[str] = Text()
    """The delivered message: a fixed reminder verbatim, or an agent result."""
    created_at: Notification.GenCol[datetime] = Text(default=CurrentTimestamp)
    dismissed_at: Notification.Col[datetime | None] = Text(
        default=None,
        nullable=True,
    )
    """Stamped when the user dismisses the row; live listings hide it thereafter."""

    __indexes__: ClassVar = [Index(dismissed_at, created_at)]


@dataclass(frozen=True, slots=True)
class NotificationDraft:
    """The content of one notification to persist."""

    body: str
    trigger_id: str | None = None
    action_kind: str | None = None
    source_label: str | None = None


class NotificationService:
    """Persistence boundary for user-facing notifications."""

    def __init__(
        self,
        database: Database,
        event_publisher: EventPublisher | None = None,
    ) -> None:
        self.database: Database = database
        self.event_publisher: EventPublisher = event_publisher or NullEventPublisher()

    async def record(self, draft: NotificationDraft) -> Notification[Fetched]:
        """Persist one delivered notification and return the stored row.

        Recording publishes a `notifications` invalidation so any other open tab
        refetches its list; the live `notify` frame the scheduler also emits
        drives the acting tab. The body is stored verbatim — delivery already
        resolved it (a fixed reminder, or an agent-prompt result).
        """

        async def _record(tx: Transaction) -> Notification[Fetched]:
            return await tx.execute(
                insert(
                    Notification(
                        body=draft.body,
                        trigger_id=draft.trigger_id,
                        action_kind=draft.action_kind,
                        source_label=draft.source_label,
                    )
                ).returning()
            )

        notification = await run_in_transaction(self.database, _record)
        await self.event_publisher.publish(InvalidateEvent(keys=["notifications"]))
        return notification

    async def list_recent(
        self, *, limit: int = DEFAULT_LIST_LIMIT
    ) -> list[Notification[Fetched]]:
        """Return undismissed notifications, newest first, capped at `limit`."""
        async with self.database.transaction() as tx:
            return await tx.fetch_all(
                select(Notification)
                .where(Notification.dismissed_at.is_null())
                .order_by(Notification.created_at.desc())
                .order_by(Notification.id.desc())
                .limit(limit)
            )

    async def dismiss(self, notification_id: UUID) -> None:
        """Dismiss one notification convergently; a missing id is a no-op."""

        async def _dismiss(tx: Transaction) -> int:
            return await tx.execute(
                update(Notification)
                .set(Notification.dismissed_at.to(CurrentTimestamp))
                .where(Notification.id.eq(notification_id))
                .where(Notification.dismissed_at.is_null())
            )

        matched = await run_in_transaction(self.database, _dismiss)
        if matched:
            await self.event_publisher.publish(InvalidateEvent(keys=["notifications"]))

    async def clear(self) -> int:
        """Dismiss every live notification, returning how many were cleared."""

        async def _clear(tx: Transaction) -> int:
            return await tx.execute(
                update(Notification)
                .set(Notification.dismissed_at.to(CurrentTimestamp))
                .where(Notification.dismissed_at.is_null())
            )

        matched = await run_in_transaction(self.database, _clear)
        if matched:
            await self.event_publisher.publish(InvalidateEvent(keys=["notifications"]))
        return matched


async def create_notification_schema(database: Database) -> None:
    """Create the notification table and its index on an initialized database."""
    migrations = {
        f"009_{label}": sql for label, sql in scaffold_sqlite_statements([Notification])
    }
    await database.migrate(migrations)


class NotificationRead(BaseModel):
    """HTTP representation of a persisted notification."""

    id: UUID
    trigger_id: str | None
    action_kind: str | None
    source_label: str | None
    body: str
    created_at: datetime

    @classmethod
    def from_notification(cls, notification: Notification[Fetched]) -> NotificationRead:
        """Render a stored notification for browser clients."""
        return cls(
            id=notification.id,
            trigger_id=notification.trigger_id,
            action_kind=notification.action_kind,
            source_label=notification.source_label,
            body=notification.body,
            created_at=_as_utc(notification.created_at),
        )


def _path_notification_id(request: Request) -> UUID:
    """Parse the `{notification_id}` path segment, treating a bad id as absent."""
    raw_id = request.path_params["notification_id"]
    try:
        return UUID(raw_id)
    except ValueError:
        # A malformed id names nothing; dismissal is convergent, so a sentinel
        # that matches no row lets the handler return its usual no-content result.
        return UUID(int=0)


@endpoint(response=NotificationRead, response_is_list=True)
async def list_notifications(request: Request) -> Response:
    """List undismissed notifications, newest first."""
    notifications = await request.app.state.notification_service.list_recent()
    return JSONResponse(
        [
            NotificationRead.from_notification(notification).model_dump(mode="json")
            for notification in notifications
        ]
    )


@endpoint(status=204)
async def dismiss_notification(request: Request) -> Response:
    """Dismiss one notification."""
    await request.app.state.notification_service.dismiss(_path_notification_id(request))
    return Response(status_code=204)


@endpoint(status=204)
async def clear_notifications(request: Request) -> Response:
    """Dismiss every live notification."""
    _ = await request.app.state.notification_service.clear()
    return Response(status_code=204)


notification_routes: list[Route] = [
    EndpointRoute("/api/notifications", list_notifications, methods=["GET"]),
    EndpointRoute("/api/notifications", clear_notifications, methods=["DELETE"]),
    EndpointRoute(
        "/api/notifications/{notification_id}",
        dismiss_notification,
        methods=["DELETE"],
    ),
]
