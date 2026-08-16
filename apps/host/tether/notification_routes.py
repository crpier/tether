"""HTTP presentation for persisted notifications."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol, cast
from uuid import UUID

from fastapi import APIRouter
from pydantic import BaseModel
from snekql.sqlite import Fetched
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from tether.notification_store import Notification
from tether.notifications import NotificationService


def _as_utc(value: datetime) -> datetime:
    """Read SQLite current timestamps as aware UTC values."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _path_notification_id(raw_id: str) -> UUID:
    """Parse a path id, mapping malformed values to an absent sentinel."""
    try:
        return UUID(raw_id)
    except ValueError:
        return UUID(int=0)


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
        """Render a canonical notification row for browser clients."""
        return cls(
            id=notification.id,
            trigger_id=notification.trigger_id,
            action_kind=notification.action_kind,
            source_label=notification.source_label,
            body=notification.body,
            created_at=_as_utc(notification.created_at),
        )


class _NotificationRuntime(Protocol):
    """Notification dependency available while serving requests."""

    notification_service: NotificationService


def _runtime(request: Request) -> _NotificationRuntime:
    """Read notification dependencies from the canonical host runtime."""
    return cast("_NotificationRuntime", request.app.state.runtime)


router = APIRouter()


@router.get("/api/notifications", response_model=list[NotificationRead])
async def list_notifications(request: Request) -> Response:
    """List undismissed notifications, newest first."""
    notifications = await _runtime(request).notification_service.list_recent()
    return JSONResponse(
        [
            NotificationRead.from_notification(notification).model_dump(mode="json")
            for notification in notifications
        ]
    )


@router.delete("/api/notifications/{notification_id}", status_code=204)
async def dismiss_notification(request: Request, notification_id: str) -> Response:
    """Dismiss one notification."""
    await _runtime(request).notification_service.dismiss(
        _path_notification_id(notification_id)
    )
    return Response(status_code=204)


@router.delete("/api/notifications", status_code=204)
async def clear_notifications(request: Request) -> Response:
    """Dismiss every live notification."""
    _ = await _runtime(request).notification_service.clear()
    return Response(status_code=204)
