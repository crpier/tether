"""HTTP routes for Scheduled triggers.

Each route adapts one trigger capability to HTTP: `endpoint` validates the
request body or query string with Pydantic, the handler binds the validated
input (plus any path id) onto the capability execute in
`tether.trigger_capabilities`, and the outcome is served as `TriggerRead` JSON.
Domain exceptions translate to status codes through the domain's `ErrorRule`
table (`TRIGGER_ERRORS`) — absence -> 404, conflict -> 409, malformed time
spec -> 422 — the same table the internal tool surface maps onto envelope
codes.

Update and Delete are optimistic-concurrency checked: the client sends the
`version` it last observed (in the body for update, the query string for
delete), and a version that has moved on surfaces as a 409.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query
from pydantic import BaseModel, PositiveInt
from starlette.requests import Request
from starlette.responses import Response

from tether import trigger_capabilities
from tether.capabilities import rest_response, translate_domain_errors
from tether.trigger_capabilities import TRIGGER_ERRORS, TriggerRead, TriggerSpecBody
from tether.triggers import TriggerNotFoundError


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


def _path_trigger_id(raw_id: str) -> UUID:
    """Parse the `{trigger_id}` path segment, treating a bad id as absent."""
    try:
        return UUID(raw_id)
    except ValueError as error:
        raise TriggerNotFoundError(raw_id) from error


_translate_domain_errors = translate_domain_errors(TRIGGER_ERRORS)


router = APIRouter()


@router.post("/api/triggers", response_model=TriggerRead, status_code=201)
@_translate_domain_errors
async def create_trigger(request: Request, body: CreateTriggerRequest) -> Response:
    """Create a Scheduled trigger."""
    outcome = await trigger_capabilities.create(request, body.to_spec())
    return rest_response(outcome, status_code=201)


@router.get("/api/triggers", response_model=list[TriggerRead])
async def list_triggers(request: Request) -> Response:
    """List live Scheduled triggers, soonest next fire first."""
    return rest_response(await trigger_capabilities.list_triggers(request))


@router.put("/api/triggers/{trigger_id}", response_model=TriggerRead)
@_translate_domain_errors
async def update_trigger(
    request: Request, body: UpdateTriggerRequest, trigger_id: str
) -> Response:
    """Replace a trigger's definition, re-arming it from its next occurrence."""
    outcome = await trigger_capabilities.update(
        request, _path_trigger_id(trigger_id), body.to_spec(), body.version
    )
    return rest_response(outcome)


@router.delete("/api/triggers/{trigger_id}", response_model=TriggerRead)
@_translate_domain_errors
async def delete_trigger(
    request: Request, query: Annotated[DeleteTriggerQuery, Query()], trigger_id: str
) -> Response:
    """Delete a Scheduled trigger."""
    outcome = await trigger_capabilities.delete(
        request, _path_trigger_id(trigger_id), query.version
    )
    return rest_response(outcome)
