"""HTTP routes for Scheduled triggers.

Each handler adapts one `TriggerService` capability to HTTP: `endpoint`
validates the request body or query string with Pydantic, the handler calls
`request.app.state.trigger_service`, and the result is serialised as
`TriggerRead`. Domain exceptions translate to status codes at this boundary —
`TriggerNotFoundError` -> 404, `TriggerConflictError` -> 409, and a malformed
time spec (`InvalidTriggerSpecError`) -> 422.

Update and Delete are optimistic-concurrency checked: the client sends the
`version` it last observed (in the body for update, the query string for
delete), and a version that has moved on surfaces as a 409.
"""

from __future__ import annotations

import functools
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import cast
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, PositiveInt
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from tether.logging import Logger, get_request_logger
from tether.openapi import EndpointRoute, endpoint
from tether.triggers import (
    Fetched,
    InvalidTriggerSpecError,
    ScheduledTrigger,
    TriggerActionKind,
    TriggerConflictError,
    TriggerNotFoundError,
    TriggerRecurrence,
    TriggerSpec,
    TriggerStatus,
)


class TriggerSpecBody(BaseModel):
    """The shared time-spec + action body for creating or updating a trigger.

    >>> TriggerSpecBody(
    ...     recurrence="daily",
    ...     action_kind="message",
    ...     payload="stand up",
    ...     timezone="UTC",
    ...     time_of_day="09:00",
    ... ).recurrence
    'daily'
    """

    recurrence: TriggerRecurrence
    action_kind: TriggerActionKind
    payload: str
    timezone: str | None = None
    time_of_day: str | None = None
    weekday: int | None = None
    fire_at: AwareDatetime | None = None

    def to_spec(self) -> TriggerSpec:
        """Project the request body onto the service's `TriggerSpec`."""
        return TriggerSpec(
            recurrence=self.recurrence,
            action_kind=self.action_kind,
            payload=self.payload,
            timezone=self.timezone,
            time_of_day=self.time_of_day,
            weekday=self.weekday,
            fire_at=self.fire_at,
        )


class CreateTriggerRequest(TriggerSpecBody):
    """Body for creating a Scheduled trigger."""


class UpdateTriggerRequest(TriggerSpecBody):
    """Body for replacing a trigger's definition at an observed version."""

    version: PositiveInt


class DeleteTriggerQuery(BaseModel):
    """Query string carrying the `version` a delete targets.

    >>> DeleteTriggerQuery(version=1).version
    1
    """

    version: PositiveInt


class TriggerRead(BaseModel):
    """HTTP representation of a Scheduled trigger."""

    id: UUID
    recurrence: TriggerRecurrence
    action_kind: TriggerActionKind
    payload: str
    timezone: str
    wall_time: str | None
    weekday: int | None
    next_fire_at: datetime
    status: TriggerStatus
    attempts: int
    next_attempt_at: datetime | None
    last_error: str | None
    version: PositiveInt
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_trigger(cls, trigger: ScheduledTrigger[Fetched]) -> TriggerRead:
        """Render a stored trigger as its HTTP representation."""
        return cls(
            id=trigger.id,
            recurrence=trigger.recurrence,
            action_kind=trigger.action_kind,
            payload=trigger.payload,
            timezone=trigger.timezone,
            wall_time=trigger.wall_time,
            weekday=trigger.weekday,
            next_fire_at=trigger.next_fire_at,
            status=trigger.status,
            attempts=trigger.attempts,
            next_attempt_at=trigger.next_attempt_at,
            last_error=trigger.last_error,
            version=trigger.version,
            created_at=trigger.created_at,
            updated_at=trigger.updated_at,
        )


def _trigger_reference(
    trigger_id: UUID, version: PositiveInt
) -> ScheduledTrigger[Fetched]:
    """Build a detached trigger carrying only the identity a mutation acts on.

    Update/Delete read just `id` and `version` to run their optimistic-
    concurrency check and re-fetch the live row, so a hand-built reference is
    enough; the other columns are required placeholders with no role here.
    """
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


def _path_trigger_id(request: Request) -> UUID:
    """Parse the `{trigger_id}` path segment, treating a bad id as absent."""
    raw_id = request.path_params["trigger_id"]
    try:
        return UUID(raw_id)
    except ValueError as error:
        raise TriggerNotFoundError(raw_id) from error


def _read_response(
    trigger: ScheduledTrigger[Fetched], *, status_code: int = 200
) -> JSONResponse:
    """Serialise a stored trigger as its `TriggerRead` JSON body."""
    return JSONResponse(
        TriggerRead.from_trigger(trigger).model_dump(mode="json"),
        status_code=status_code,
    )


def _request_logger(request: Request) -> Logger:
    """Return the request logging context installed by middleware."""
    return get_request_logger(request)


def _translate_domain_errors(
    handler: Callable[..., Awaitable[Response]],
) -> Callable[..., Awaitable[Response]]:
    """Map trigger domain failures onto HTTP status codes at the boundary."""

    @functools.wraps(handler)
    async def translated(*arguments: object) -> Response:
        try:
            return await handler(*arguments)
        except TriggerNotFoundError:
            return JSONResponse({"detail": "trigger not found"}, status_code=404)
        except TriggerConflictError as error:
            return JSONResponse({"detail": str(error)}, status_code=409)
        except InvalidTriggerSpecError as error:
            return JSONResponse({"detail": str(error)}, status_code=422)

    return translated


@endpoint(request_body=CreateTriggerRequest, response=TriggerRead, status=201)
@_translate_domain_errors
async def create_trigger(request: Request, body: CreateTriggerRequest) -> Response:
    """Create a Scheduled trigger."""
    trigger = await request.app.state.trigger_service.create(
        body.to_spec(),
        now=datetime.now(UTC),
        logger=_request_logger(request),
    )
    return _read_response(trigger, status_code=201)


@endpoint(response=TriggerRead, response_is_list=True)
async def list_triggers(request: Request) -> Response:
    """List live Scheduled triggers, soonest next fire first."""
    triggers = await request.app.state.trigger_service.list_triggers(
        logger=_request_logger(request),
    )
    return JSONResponse(
        [
            TriggerRead.from_trigger(trigger).model_dump(mode="json")
            for trigger in triggers
        ]
    )


@endpoint(request_body=UpdateTriggerRequest, response=TriggerRead)
@_translate_domain_errors
async def update_trigger(request: Request, body: UpdateTriggerRequest) -> Response:
    """Replace a trigger's definition, re-arming it from its next occurrence."""
    trigger = await request.app.state.trigger_service.update(
        _trigger_reference(_path_trigger_id(request), body.version),
        body.to_spec(),
        now=datetime.now(UTC),
        logger=_request_logger(request),
    )
    return _read_response(trigger)


@endpoint(query=DeleteTriggerQuery, response=TriggerRead)
@_translate_domain_errors
async def delete_trigger(request: Request, query: DeleteTriggerQuery) -> Response:
    """Delete a Scheduled trigger."""
    trigger = await request.app.state.trigger_service.delete(
        _trigger_reference(_path_trigger_id(request), query.version),
        now=datetime.now(UTC),
        logger=_request_logger(request),
    )
    return _read_response(trigger)


trigger_routes: list[Route] = [
    EndpointRoute("/api/triggers", create_trigger, methods=["POST"]),
    EndpointRoute("/api/triggers", list_triggers, methods=["GET"]),
    EndpointRoute("/api/triggers/{trigger_id}", update_trigger, methods=["PUT"]),
    EndpointRoute("/api/triggers/{trigger_id}", delete_trigger, methods=["DELETE"]),
]
