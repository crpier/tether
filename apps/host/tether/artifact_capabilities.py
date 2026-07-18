"""The Artifact domain's capability descriptor.

The pieces the REST routes (`tether.artifact_routes`) and the internal tools
(`tether.artifact_tools`) both need live here once: the Read models, the
domain→code map (`ARTIFACT_ERRORS`), and one execute function per capability
— the service call plus its Read-model rendering.

Tool results are small pointers only: `create`/`update` return
`ArtifactPointerRead` (`id`, `version`) — never `html`, which must never
round-trip back into the model's own context. REST reads (`get_latest`,
`get_version`) carry the full `html` body, since the browser fetches it to
mount the sandboxed iframe. `list_artifacts` renders the lighter
`ArtifactSummaryRead` (no `html`) since it is an overview, not a fetch target.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import UUID7, BaseModel, PositiveInt
from starlette.requests import Request

from tether.artifacts import (
    Artifact,
    ArtifactEvent,
    ArtifactHtmlTooLargeError,
    ArtifactNotFoundError,
    Fetched,
    JsonValue,
)
from tether.capabilities import CapabilityOutcome, ErrorRule
from tether.logging import get_request_logger

ARTIFACT_ERRORS: tuple[ErrorRule, ...] = (
    ErrorRule((ArtifactNotFoundError,), "not_found", 404, detail="artifact not found"),
    ErrorRule((ArtifactHtmlTooLargeError,), "invalid_input", 422),
)
"""The Artifact domain→code map both surfaces translate failures through."""


class ArtifactPointerRead(BaseModel):
    """The small pointer a mutation returns: an id and its resulting version.

    Deliberately carries no `html` and no `title` — a tool result is a
    pointer, not a round-trip of the document it names.
    """

    id: UUID7
    version: PositiveInt


class ArtifactRead(BaseModel):
    """Full HTTP representation of one artifact version, `html` included.

    Served by the REST reads (latest / by version) the browser fetches to
    mount an artifact's sandboxed iframe.
    """

    id: UUID7
    title: str
    html: str
    version: PositiveInt
    created_at: datetime

    @classmethod
    def from_artifact(cls, artifact: Artifact[Fetched]) -> ArtifactRead:
        """Render a stored artifact version as its full HTTP representation."""
        return cls(
            id=artifact.artifact_id,
            title=artifact.title,
            html=artifact.html,
            version=artifact.version,
            created_at=artifact.created_at,
        )


class ArtifactSummaryRead(BaseModel):
    """Overview HTTP representation of an artifact's latest version, no `html`."""

    id: UUID7
    title: str
    version: PositiveInt
    created_at: datetime

    @classmethod
    def from_artifact(cls, artifact: Artifact[Fetched]) -> ArtifactSummaryRead:
        """Render a stored artifact version as its overview representation."""
        return cls(
            id=artifact.artifact_id,
            title=artifact.title,
            version=artifact.version,
            created_at=artifact.created_at,
        )


class ArtifactEventRead(BaseModel):
    """HTTP representation of one artifact event."""

    id: UUID7
    artifact_id: UUID7
    payload: dict[str, JsonValue]
    created_at: datetime

    @classmethod
    def from_event(cls, event: ArtifactEvent[Fetched]) -> ArtifactEventRead:
        """Render a stored artifact event as its HTTP representation."""
        return cls(
            id=event.id,
            artifact_id=event.artifact_id,
            payload=event.payload,
            created_at=event.created_at,
        )


def _pointer(artifact: Artifact[Fetched]) -> CapabilityOutcome:
    """Render a mutation outcome as the small tool-facing pointer."""
    return CapabilityOutcome(
        result=ArtifactPointerRead(
            id=artifact.artifact_id, version=artifact.version
        ).model_dump(mode="json")
    )


def _full(artifact: Artifact[Fetched]) -> CapabilityOutcome:
    """Render a single artifact version, `html` included, for a REST read."""
    return CapabilityOutcome(
        result=ArtifactRead.from_artifact(artifact).model_dump(mode="json")
    )


async def create(request: Request, title: str, html: str) -> CapabilityOutcome:
    """Create a new artifact at version 1; the outcome is a small pointer."""
    artifact = await request.app.state.artifact_service.create(
        title, html, logger=get_request_logger(request)
    )
    return _pointer(artifact)


async def update(request: Request, artifact_id: UUID, html: str) -> CapabilityOutcome:
    """Append a new version onto an existing artifact; outcome is a small pointer."""
    artifact = await request.app.state.artifact_service.update(
        artifact_id, html, logger=get_request_logger(request)
    )
    return _pointer(artifact)


async def get_latest(request: Request, artifact_id: UUID) -> CapabilityOutcome:
    """Fetch an artifact's newest version, `html` included."""
    artifact = await request.app.state.artifact_service.get_latest(
        artifact_id, logger=get_request_logger(request)
    )
    return _full(artifact)


async def get_version(
    request: Request, artifact_id: UUID, version: PositiveInt
) -> CapabilityOutcome:
    """Fetch one specific past version of an artifact, `html` included."""
    artifact = await request.app.state.artifact_service.get_version(
        artifact_id, version, logger=get_request_logger(request)
    )
    return _full(artifact)


async def list_artifacts(request: Request) -> CapabilityOutcome:
    """List every artifact's latest version as lightweight summaries."""
    artifacts = await request.app.state.artifact_service.list_artifacts(
        logger=get_request_logger(request)
    )
    return CapabilityOutcome(
        result=[
            ArtifactSummaryRead.from_artifact(artifact).model_dump(mode="json")
            for artifact in artifacts
        ]
    )


async def post_event(
    request: Request, artifact_id: UUID, payload: dict[str, JsonValue]
) -> CapabilityOutcome:
    """Append one free-form event to an artifact's log (the postMessage relay target)."""
    event = await request.app.state.artifact_service.record_event(
        artifact_id, payload, logger=get_request_logger(request)
    )
    return CapabilityOutcome(
        result=ArtifactEventRead.from_event(event).model_dump(mode="json")
    )


async def list_events(request: Request, artifact_id: UUID) -> CapabilityOutcome:
    """List an artifact's events, oldest first — the agent's sole read-back channel."""
    events = await request.app.state.artifact_service.list_events(
        artifact_id, logger=get_request_logger(request)
    )
    return CapabilityOutcome(
        result=[
            ArtifactEventRead.from_event(event).model_dump(mode="json")
            for event in events
        ]
    )
