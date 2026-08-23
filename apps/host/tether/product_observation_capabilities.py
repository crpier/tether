"""Shared chat-tool and HTTP capabilities for Product observations."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, cast
from uuid import UUID

from pydantic import UUID7, BaseModel, PositiveInt
from snekql.sqlite import Fetched
from starlette.requests import Request

from tether.agent_trace_recorder import AgentTraceRecorder
from tether.capability_contracts import CapabilityOutcome, ErrorRule
from tether.conversation_store import Message
from tether.conversations import ConversationService
from tether.product_observation_errors import (
    InvalidProductObservationError,
    ProductObservationConflictError,
    ProductObservationNotFoundError,
)
from tether.product_observation_model import ProductObservationStatus
from tether.product_observation_store import ProductObservation
from tether.product_observations import (
    ProductObservationService,
    product_observation_reference,
)

PRODUCT_OBSERVATION_ERRORS: tuple[ErrorRule, ...] = (
    ErrorRule(
        (ProductObservationNotFoundError,),
        "not_found",
        404,
        detail="product observation not found",
    ),
    ErrorRule((ProductObservationConflictError,), "conflict", 409),
    ErrorRule((InvalidProductObservationError,), "invalid_input", 422),
)
"""Product-observation failures translated at both request seams."""


class ProductObservationRead(BaseModel):
    """Browser and tool representation of one Product observation."""

    id: UUID7
    conversation_id: UUID7
    message_id: UUID7
    wording: str
    interpretation: str
    status: ProductObservationStatus
    version: PositiveInt
    created_at: datetime
    updated_at: datetime
    resolved_at: datetime | None

    @classmethod
    def from_observation(
        cls, observation: ProductObservation[Fetched]
    ) -> ProductObservationRead:
        """Render one stored Product observation."""
        return cls.model_validate(observation, from_attributes=True)


class _ProductObservationRuntime(Protocol):
    """Runtime dependencies required by Product-observation capabilities."""

    conversation_service: ConversationService
    product_observation_service: ProductObservationService
    trace_recorder: AgentTraceRecorder


def _runtime(request: Request) -> _ProductObservationRuntime:
    """Read Product-observation dependencies from the application runtime."""
    return cast("_ProductObservationRuntime", request.app.state.runtime)


def _single(observation: ProductObservation[Fetched]) -> CapabilityOutcome:
    """Render one observation as a capability outcome."""
    return CapabilityOutcome(
        result=ProductObservationRead.from_observation(observation).model_dump(
            mode="json"
        )
    )


async def _active_user_message(request: Request) -> Message[Fetched]:
    """Resolve exact source text from the foreground Conversation, not model args."""
    runtime = _runtime(request)
    run = runtime.trace_recorder.current_run(request.state.session_id)
    if run is None or run.kind != "conversation" or run.conversation_id is None:
        message = "product feedback can only be recorded during a conversation"
        raise InvalidProductObservationError(message)
    source = await runtime.conversation_service.fetch_latest_user_message(
        UUID(run.conversation_id)
    )
    if source is None:
        message = "the active conversation has no current user message"
        raise InvalidProductObservationError(message)
    return source


async def record(request: Request, interpretation: str) -> CapabilityOutcome:
    """Record explicit feedback against the active user Message."""
    source = await _active_user_message(request)
    observation = await _runtime(request).product_observation_service.record(
        wording=source.content,
        interpretation=interpretation,
        conversation_id=source.conversation_id,
        message_id=source.id,
    )
    return _single(observation)


async def record_message(
    request: Request,
    *,
    conversation_id: UUID,
    interpretation: str,
    message_id: UUID,
) -> CapabilityOutcome:
    """Record explicit feedback from a browser-selected user Message."""
    runtime = _runtime(request)
    source = await runtime.conversation_service.fetch_user_message(
        conversation_id,
        message_id,
    )
    if source is None:
        message = "product feedback source must be a user message in the conversation"
        raise InvalidProductObservationError(message)
    observation = await runtime.product_observation_service.record(
        wording=source.content,
        interpretation=interpretation,
        conversation_id=source.conversation_id,
        message_id=source.id,
    )
    return _single(observation)


async def list_open(request: Request) -> CapabilityOutcome:
    """List unresolved Product observations newest first."""
    observations = await _runtime(request).product_observation_service.list_open()
    return CapabilityOutcome(
        result=[
            ProductObservationRead.from_observation(observation).model_dump(mode="json")
            for observation in observations
        ]
    )


async def resolve(
    request: Request, observation_id: UUID, version: PositiveInt
) -> CapabilityOutcome:
    """Resolve one Product observation at its observed version."""
    observation = await _runtime(request).product_observation_service.resolve(
        product_observation_reference(observation_id, version)
    )
    return _single(observation)
