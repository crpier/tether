"""HTTP routes for Synthetic panels.

Each route adapts one panel capability to HTTP: `endpoint` validates the
request body or query string with Pydantic, the handler binds the validated
input (plus any path id) onto the capability execute in
`tether.panel_capabilities`, and the outcome is served as `PanelRead` (or
`PanelResultsRead`) JSON. Domain exceptions translate through `PANEL_ERRORS` —
absence -> 404, conflict -> 409, malformed spec -> 422 — the same table the
internal tool surface maps onto envelope codes.

Update and Delete are optimistic-concurrency checked: the client sends the
`version` it last observed (in the body for update, the query string for
delete), and a version that has moved on surfaces as a 409. Results are served
from their own subresource (`GET .../results`) and recomputed per request —
a panel execution is a Search, never cached (ADR 0006).
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query
from pydantic import BaseModel, PositiveInt
from starlette.requests import Request
from starlette.responses import Response

from tether import panel_capabilities
from tether.capabilities import rest_response, translate_domain_errors
from tether.panel_capabilities import (
    PANEL_ERRORS,
    PanelRead,
    PanelResultsRead,
    PanelSpecBody,
)
from tether.panels import EXECUTE_DEFAULT_LIMIT, PanelNotFoundError


class CreatePanelRequest(PanelSpecBody):
    """Body for creating a Synthetic panel."""


class UpdatePanelRequest(PanelSpecBody):
    """Body for replacing a panel's definition at an observed version."""

    version: PositiveInt


class DeletePanelQuery(BaseModel):
    """Query string carrying the `version` a delete targets.

    >>> DeletePanelQuery(version=1).version
    1
    """

    version: PositiveInt


class PanelResultsQuery(BaseModel):
    """Query string capping how many result rows an execution returns.

    >>> PanelResultsQuery().limit
    20
    """

    limit: PositiveInt = EXECUTE_DEFAULT_LIMIT


def _path_panel_id(raw_id: str) -> UUID:
    """Parse the `{panel_id}` path segment, treating a bad id as absent."""
    try:
        return UUID(raw_id)
    except ValueError as error:
        raise PanelNotFoundError(raw_id) from error


_translate_domain_errors = translate_domain_errors(PANEL_ERRORS)


router = APIRouter()


@router.post("/api/panels", response_model=PanelRead, status_code=201)
@_translate_domain_errors
async def create_panel(request: Request, body: CreatePanelRequest) -> Response:
    """Create a Synthetic panel."""
    outcome = await panel_capabilities.create(request, body.to_spec())
    return rest_response(outcome, status_code=201)


@router.get("/api/panels", response_model=list[PanelRead])
async def list_panels(request: Request) -> Response:
    """List live Synthetic panels in position order."""
    return rest_response(await panel_capabilities.list_panels(request))


@router.put("/api/panels/{panel_id}", response_model=PanelRead)
@_translate_domain_errors
async def update_panel(
    request: Request, body: UpdatePanelRequest, panel_id: str
) -> Response:
    """Replace a panel's definition at an observed version."""
    outcome = await panel_capabilities.update(
        request, _path_panel_id(panel_id), body.to_spec(), body.version
    )
    return rest_response(outcome)


@router.delete("/api/panels/{panel_id}", response_model=PanelRead)
@_translate_domain_errors
async def delete_panel(
    request: Request, query: Annotated[DeletePanelQuery, Query()], panel_id: str
) -> Response:
    """Delete a Synthetic panel."""
    outcome = await panel_capabilities.delete(
        request, _path_panel_id(panel_id), query.version
    )
    return rest_response(outcome)


@router.get("/api/panels/{panel_id}/results", response_model=PanelResultsRead)
@_translate_domain_errors
async def panel_results(
    request: Request, query: Annotated[PanelResultsQuery, Query()], panel_id: str
) -> Response:
    """Run a panel's saved query, recomputed against the corpus right now."""
    outcome = await panel_capabilities.execute(
        request, _path_panel_id(panel_id), query.limit
    )
    return rest_response(outcome)
