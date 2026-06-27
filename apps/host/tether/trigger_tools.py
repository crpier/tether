"""The internal Scheduled-trigger tool surface, over the shared envelope.

These mount alongside the Memory and Bucket tools under `/internal/tools/*` —
the loopback seam a pi process calls back into — reusing the same auth gate,
params-to-envelope validation, and domain-error translation (`tether.tools`).

The agent can set up a reminder (`create_trigger`), see what is scheduled
(`list_triggers`), and cancel one (`delete_trigger`). Editing an existing
trigger's definition is left to the REST/UI surface, where optimistic-
concurrency on a freshly-read version is natural.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast
from uuid import UUID

from pydantic import UUID7, AwareDatetime, BaseModel, PositiveInt
from starlette.requests import Request
from starlette.routing import Route

from tether.logging import Logger, get_request_logger
from tether.tools import ToolEndpoint, ToolEnvelope, ToolRoute
from tether.trigger_routes import TriggerRead
from tether.triggers import (
    Fetched,
    ScheduledTrigger,
    TriggerActionKind,
    TriggerRecurrence,
    TriggerSpec,
)


class CreateTriggerParams(BaseModel):
    """Params for scheduling a trigger.

    `once` carries an absolute `fire_at`; `daily`/`weekly` carry `timezone` and
    `time_of_day` (and a `weekday` for weekly). Mismatched fields are rejected
    as a well-formed `invalid_input` envelope, never a corrupt row.
    """

    recurrence: TriggerRecurrence
    action_kind: TriggerActionKind
    payload: str
    timezone: str | None = None
    time_of_day: str | None = None
    weekday: int | None = None
    fire_at: AwareDatetime | None = None


class ListTriggersParams(BaseModel):
    """Params for listing live triggers; the listing takes no inputs."""


class DeleteTriggerParams(BaseModel):
    """Params for deleting a trigger at an observed version."""

    trigger_id: UUID7
    version: PositiveInt


def _trigger_reference(
    trigger_id: UUID, version: PositiveInt
) -> ScheduledTrigger[Fetched]:
    """Build a detached trigger carrying only the identity a delete acts on."""
    return cast(
        "ScheduledTrigger[Fetched]",
        ScheduledTrigger.construct(
            id=trigger_id,
            version=version,
            recurrence="once",
            action_kind="message",
            payload="",
            timezone="UTC",
            next_fire_at=datetime(1970, 1, 1, tzinfo=UTC),
            status="active",
            attempts=0,
        ),
    )


def _ok_trigger(trigger: ScheduledTrigger[Fetched]) -> ToolEnvelope:
    """Envelope a single-trigger result."""
    return ToolEnvelope(
        success=True,
        result=TriggerRead.from_trigger(trigger).model_dump(mode="json"),
    )


def _ok_triggers(triggers: list[ScheduledTrigger[Fetched]]) -> ToolEnvelope:
    """Envelope a trigger collection."""
    return ToolEnvelope(
        success=True,
        result=[
            TriggerRead.from_trigger(trigger).model_dump(mode="json")
            for trigger in triggers
        ],
    )


def _tool_logger(request: Request) -> Logger:
    """Return the request logging context installed by middleware."""
    return get_request_logger(request)


async def _create_trigger(
    request: Request, params: CreateTriggerParams
) -> ToolEnvelope:
    """Schedule a new trigger."""
    spec = TriggerSpec(
        recurrence=params.recurrence,
        action_kind=params.action_kind,
        payload=params.payload,
        timezone=params.timezone,
        time_of_day=params.time_of_day,
        weekday=params.weekday,
        fire_at=params.fire_at,
    )
    trigger = await request.app.state.trigger_service.create(
        spec, now=datetime.now(UTC), logger=_tool_logger(request)
    )
    return _ok_trigger(trigger)


async def _list_triggers(request: Request, _params: ListTriggersParams) -> ToolEnvelope:
    """List live Scheduled triggers."""
    triggers = await request.app.state.trigger_service.list_triggers(
        logger=_tool_logger(request)
    )
    return _ok_triggers(triggers)


async def _delete_trigger(
    request: Request, params: DeleteTriggerParams
) -> ToolEnvelope:
    """Delete a Scheduled trigger."""
    trigger = await request.app.state.trigger_service.delete(
        _trigger_reference(params.trigger_id, params.version),
        now=datetime.now(UTC),
        logger=_tool_logger(request),
    )
    return _ok_trigger(trigger)


def internal_trigger_tool_routes() -> list[Route]:
    """Mount the trigger capabilities as `/internal/tools/*` POST endpoints.

    Returned separately from the public trigger routes so they stay absent from
    the public OpenAPI document and generated client.
    """
    return [
        ToolRoute(
            "/internal/tools/create_trigger",
            ToolEndpoint(CreateTriggerParams, _create_trigger),
            methods=["POST"],
        ),
        ToolRoute(
            "/internal/tools/list_triggers",
            ToolEndpoint(ListTriggersParams, _list_triggers),
            methods=["POST"],
        ),
        ToolRoute(
            "/internal/tools/delete_trigger",
            ToolEndpoint(DeleteTriggerParams, _delete_trigger),
            methods=["POST"],
        ),
    ]
