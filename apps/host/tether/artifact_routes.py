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

Routes register in `public_api_routes` ahead of the SPA catch-all mount, per
existing route-ordering convention (`/api/artifacts/*` must never fall through
to the static shell).
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from tether import artifact_capabilities
from tether.artifact_capabilities import (
    ARTIFACT_ERRORS,
    ArtifactEventRead,
    ArtifactRead,
    ArtifactSummaryRead,
)
from tether.artifacts import ArtifactNotFoundError, JsonValue
from tether.capabilities import rest_response, translate_domain_errors
from tether.openapi import EndpointRoute, endpoint


class PostArtifactEventRequest(BaseModel):
    """Body for relaying one artifact event.

    `payload` is opaque, free-form JSON — the `postMessage` payload a
    sandboxed artifact posted to its parent, relayed verbatim under the
    browser's own session; no schema is enforced beyond "a JSON object".

    >>> PostArtifactEventRequest(payload={"type": "answer", "value": 3}).payload
    {'type': 'answer', 'value': 3}
    """

    payload: dict[str, JsonValue]


def _path_artifact_id(request: Request) -> UUID:
    """Parse the `{artifact_id}` path segment, treating a bad id as absent."""
    raw_id = request.path_params["artifact_id"]
    try:
        return UUID(raw_id)
    except ValueError as error:
        raise ArtifactNotFoundError(raw_id) from error


def _path_version(request: Request) -> int:
    """Parse the `{version}` path segment, treating a bad version as absent."""
    raw_version = request.path_params["version"]
    try:
        return int(raw_version)
    except ValueError as error:
        raise ArtifactNotFoundError(raw_version) from error


_translate_domain_errors = translate_domain_errors(ARTIFACT_ERRORS)


@endpoint(response=ArtifactSummaryRead, response_is_list=True)
async def list_artifacts(request: Request) -> Response:
    """List every artifact's latest version as lightweight summaries."""
    return rest_response(await artifact_capabilities.list_artifacts(request))


@endpoint(response=ArtifactRead)
@_translate_domain_errors
async def get_artifact(request: Request) -> Response:
    """Fetch an artifact's newest version, `html` included."""
    outcome = await artifact_capabilities.get_latest(
        request, _path_artifact_id(request)
    )
    return rest_response(outcome)


@endpoint(response=ArtifactRead)
@_translate_domain_errors
async def get_artifact_version(request: Request) -> Response:
    """Fetch one specific past version of an artifact, `html` included."""
    outcome = await artifact_capabilities.get_version(
        request, _path_artifact_id(request), _path_version(request)
    )
    return rest_response(outcome)


@endpoint(response=ArtifactEventRead, response_is_list=True)
@_translate_domain_errors
async def list_artifact_events(request: Request) -> Response:
    """List an artifact's events, oldest first."""
    outcome = await artifact_capabilities.list_events(
        request, _path_artifact_id(request)
    )
    return rest_response(outcome)


@endpoint(request_body=PostArtifactEventRequest, response=ArtifactEventRead, status=201)
@_translate_domain_errors
async def post_artifact_event(
    request: Request, body: PostArtifactEventRequest
) -> Response:
    """Append one free-form event to an artifact's log.

    The `postMessage` relay target: the viewer's `message` listener validates
    `event.source` against the mounted iframe before calling this under the
    browser's own session.
    """
    outcome = await artifact_capabilities.post_event(
        request, _path_artifact_id(request), body.payload
    )
    return rest_response(outcome, status_code=201)


# `/api/artifacts/{artifact_id}/versions/{version}` and `.../events` precede
# nothing more specific under `/api/artifacts/{artifact_id}` — Starlette
# matches routes in declaration order, so the more specific literal segments
# must be declared before the bare `{artifact_id}` route they'd otherwise be
# swallowed by. Here both are already suffixed paths past `{artifact_id}`, so
# ordering among them is not load-bearing, only against a hypothetical bare
# `{artifact_id}/{rest}` route (which does not exist).
artifact_routes: list[Route] = [
    EndpointRoute("/api/artifacts", list_artifacts, methods=["GET"]),
    EndpointRoute("/api/artifacts/{artifact_id}", get_artifact, methods=["GET"]),
    EndpointRoute(
        "/api/artifacts/{artifact_id}/versions/{version}",
        get_artifact_version,
        methods=["GET"],
    ),
    EndpointRoute(
        "/api/artifacts/{artifact_id}/events",
        list_artifact_events,
        methods=["GET"],
    ),
    EndpointRoute(
        "/api/artifacts/{artifact_id}/events",
        post_artifact_event,
        methods=["POST"],
    ),
]
