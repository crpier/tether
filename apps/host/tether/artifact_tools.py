"""The internal Artifact tool surface, over the shared response envelope.

These mount alongside the Memory and Bucket item tools under
`/internal/tools/*` — the loopback seam a pi process calls back into — reusing
the same auth gate, params-to-envelope validation, and rule-driven domain-
error translation (`tether.tools`). The capabilities execute live in
`tether.artifact_capabilities`, shared with the REST routes (`list_events`
only — Create/Update are agent-tool-only, absent from REST entirely); this
module only names each tool's params model and mounts it.

`create_artifact`/`update_artifact` return `ArtifactPointerRead` (`id`,
`version`) — small pointers only. `html` never appears in a tool result: the
agent authors it once and never reads it back, matching ADR 0011's "the agent
never reads an artifact's rendered content or DOM" boundary. `system_prompt`
instructs the agent to link a freshly created artifact into its reply with an
`artifact` fence, since a pointer alone is otherwise invisible in the turn.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import UUID7, BaseModel
from starlette.requests import Request
from starlette.routing import Route

from tether.artifact_capabilities import (
    ARTIFACT_ERRORS,
    create,
    list_events,
    update,
)
from tether.capabilities import CapabilityOutcome, bind_params
from tether.tools import ToolSpec


class CreateArtifactParams(BaseModel):
    """Params for creating a new artifact at version 1."""

    title: str
    html: str


class UpdateArtifactParams(BaseModel):
    """Params for appending a new version onto an existing artifact.

    `id` names the stable artifact identity (not a specific version's own row
    id); the title carries forward from the latest version unchanged.
    """

    id: UUID
    html: str


class ListArtifactEventsParams(BaseModel):
    """Params for listing an artifact's events, oldest first."""

    artifact_id: UUID7


async def _update_artifact(
    request: Request, params: UpdateArtifactParams
) -> CapabilityOutcome:
    """Project `UpdateArtifactParams.id` onto the capability's `artifact_id`.

    The tool's wire field is `id` (per the ticket's `update_artifact(id, html)`
    contract); the capability names it `artifact_id` to avoid shadowing the
    `id` builtin, so this small named binding does the field projection
    `bind_params` can't (it only ever splats matching names).
    """
    return await update(request, params.id, params.html)


ARTIFACT_TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        "create_artifact", CreateArtifactParams, bind_params(create), ARTIFACT_ERRORS
    ),
    ToolSpec(
        "update_artifact", UpdateArtifactParams, _update_artifact, ARTIFACT_ERRORS
    ),
    ToolSpec(
        "list_artifact_events",
        ListArtifactEventsParams,
        bind_params(list_events),
        ARTIFACT_ERRORS,
    ),
)
"""The Artifact capabilities exposed as internal tools, in generated order."""


def internal_artifact_tool_routes() -> list[Route]:
    """Mount the Artifact capabilities as `/internal/tools/*` POST endpoints.

    Returned separately from the public Artifact routes so they stay absent
    from the public OpenAPI document and generated web client.
    """
    return [spec.route() for spec in ARTIFACT_TOOL_SPECS]
