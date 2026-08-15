"""HTTP routes for Artifacts.

Creation is agent-tool-only (`tether.artifact_tools`) — there is no REST
create, matching the "artifacts are agent-generated" framing: a human never
authors one directly. The REST surface exists for the browser to read (latest
version, a specific past version, the summary list) and to relay one write:
`POST .../events`, the `postMessage` relay target the sandboxed iframe's
viewer calls under the browser's own session. Domain exceptions translate to
status codes through the domain's `ErrorRule` table (`ARTIFACT_ERRORS`) —
absence -> 404, oversized `html` (tool path only; REST never accepts `html`)
-> 422 — the same table the internal tool surface maps onto envelope codes.

Routes register on the public FastAPI router ahead of the SPA catch-all mount,
so `/api/artifacts/*` never falls through to the static shell.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter
from pydantic import BaseModel
from starlette.requests import Request
from starlette.responses import Response

from tether import artifact_capabilities
from tether.artifact_capabilities import (
    ARTIFACT_ERRORS,
    ArtifactEventRead,
    ArtifactRead,
    ArtifactSummaryRead,
)
from tether.artifacts import ArtifactNotFoundError
from tether.capabilities import rest_response, translate_domain_errors


class PostArtifactEventRequest(BaseModel):
    """Body for relaying one artifact event.

    `payload` is opaque, free-form JSON — the `postMessage` payload a
    sandboxed artifact posted to its parent, relayed verbatim under the
    browser's own session; no schema is enforced beyond "a JSON object".

    >>> PostArtifactEventRequest(payload={"type": "answer", "value": 3}).payload
    {'type': 'answer', 'value': 3}
    """

    payload: dict[str, Any]


def _path_artifact_id(raw_id: str) -> UUID:
    """Parse the `{artifact_id}` path segment, treating a bad id as absent."""
    try:
        return UUID(raw_id)
    except ValueError as error:
        raise ArtifactNotFoundError(raw_id) from error


def _path_version(raw_version: str) -> int:
    """Parse the `{version}` path segment, treating a bad version as absent."""
    try:
        return int(raw_version)
    except ValueError as error:
        raise ArtifactNotFoundError(raw_version) from error


_translate_domain_errors = translate_domain_errors(ARTIFACT_ERRORS)


router = APIRouter()


@router.get("/api/artifacts", response_model=list[ArtifactSummaryRead])
async def list_artifacts(request: Request) -> Response:
    """List every artifact's latest version as lightweight summaries."""
    return rest_response(await artifact_capabilities.list_artifacts(request))


@router.get("/api/artifacts/{artifact_id}", response_model=ArtifactRead)
@_translate_domain_errors
async def get_artifact(request: Request, artifact_id: str) -> Response:
    """Fetch an artifact's newest version, `html` included."""
    outcome = await artifact_capabilities.get_latest(
        request, _path_artifact_id(artifact_id)
    )
    return rest_response(outcome)


@router.get(
    "/api/artifacts/{artifact_id}/versions/{version}", response_model=ArtifactRead
)
@_translate_domain_errors
async def get_artifact_version(
    request: Request, artifact_id: str, version: str
) -> Response:
    """Fetch one specific past version of an artifact, `html` included."""
    outcome = await artifact_capabilities.get_version(
        request, _path_artifact_id(artifact_id), _path_version(version)
    )
    return rest_response(outcome)


@router.get(
    "/api/artifacts/{artifact_id}/events", response_model=list[ArtifactEventRead]
)
@_translate_domain_errors
async def list_artifact_events(request: Request, artifact_id: str) -> Response:
    """List an artifact's events, oldest first."""
    outcome = await artifact_capabilities.list_events(
        request, _path_artifact_id(artifact_id)
    )
    return rest_response(outcome)


@router.post(
    "/api/artifacts/{artifact_id}/events",
    response_model=ArtifactEventRead,
    status_code=201,
)
@_translate_domain_errors
async def post_artifact_event(
    request: Request, body: PostArtifactEventRequest, artifact_id: str
) -> Response:
    """Append one free-form event to an artifact's log.

    The `postMessage` relay target: the viewer's `message` listener validates
    `event.source` against the mounted iframe before calling this under the
    browser's own session.
    """
    outcome = await artifact_capabilities.post_event(
        request, _path_artifact_id(artifact_id), body.payload
    )
    return rest_response(outcome, status_code=201)


# `/api/artifacts/{artifact_id}/versions/{version}` and `.../events` precede
# nothing more specific under `/api/artifacts/{artifact_id}` — the router
# matches routes in declaration order, so the more specific literal segments
# must be declared before the bare `{artifact_id}` route they'd otherwise be
# swallowed by. Here both are already suffixed paths past `{artifact_id}`, so
# ordering among them is not load-bearing, only against a hypothetical bare
# `{artifact_id}/{rest}` route (which does not exist).
