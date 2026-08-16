"""HTTP presentation for host-owned conversations and transcripts."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Annotated, Any, Protocol, cast
from uuid import UUID

from fastapi import APIRouter, Query
from pydantic import UUID7, BaseModel, PositiveInt
from snekql.sqlite import Fetched
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from tether.conversation_model import ConversationNotFoundError, MessageRole
from tether.conversation_store import Conversation, Message
from tether.conversations import SESSION_GAP, ConversationService
from tether.model_selection import AgentModelConfig, ModelNotAllowedError
from tether.pi_errors import PiRuntimeError


class ConversationRead(BaseModel):
    """HTTP representation of a host-owned conversation.

    `session_gap_seconds` and `latest_activity` let the frontend compute
    whether the *next* message will land on a fresh pi session (see
    `ConversationService.resolve_session`) without hardcoding the gap.
    """

    created_at: datetime
    id: UUID7
    latest_activity: datetime | None
    pi_session_id: UUID7
    selected_model: str | None
    session_gap_seconds: int
    title: str | None

    @classmethod
    def from_conversation(
        cls,
        conversation: Conversation[Fetched],
        *,
        latest_activity: datetime | None,
    ) -> ConversationRead:
        """Render canonical state with its current activity signal."""
        return cls(
            created_at=conversation.created_at,
            id=conversation.id,
            latest_activity=latest_activity,
            pi_session_id=conversation.pi_session_id,
            selected_model=conversation.selected_model,
            session_gap_seconds=int(SESSION_GAP.total_seconds()),
            title=conversation.title,
        )


class MessageRead(BaseModel):
    """HTTP representation of a settled transcript row."""

    content: str
    conversation_id: UUID7
    created_at: datetime
    id: UUID7
    pi_message_id: str | None
    role: MessageRole
    seq: PositiveInt
    tool_args: dict[str, Any] | None
    tool_name: str | None
    tool_result: dict[str, Any] | None

    @classmethod
    def from_message(cls, message: Message[Fetched]) -> MessageRead:
        """Decode stored JSON fields at the HTTP presentation boundary."""
        return cls(
            content=message.content,
            conversation_id=message.conversation_id,
            created_at=message.created_at,
            id=message.id,
            pi_message_id=message.pi_message_id,
            role=message.role,
            seq=message.seq,
            tool_args=(
                json.loads(message.tool_args) if message.tool_args is not None else None
            ),
            tool_name=message.tool_name,
            tool_result=(
                json.loads(message.tool_result)
                if message.tool_result is not None
                else None
            ),
        )


class SetConversationModelRequest(BaseModel):
    """Body for selecting a conversation's model."""

    selected_model: str


class MessagesQuery(BaseModel):
    """Query string for windowed transcript pagination."""

    limit: PositiveInt | None = None
    before_seq: PositiveInt | None = None


class _ConversationRuntimeRegistry(Protocol):
    """Live process operations required by conversation routes."""

    async def set_model(
        self,
        conversation_id: object,
        model: AgentModelConfig,
    ) -> None: ...

    async def discard(self, conversation_id: object) -> None: ...


class _ConversationRuntime(Protocol):
    """Conversation dependencies available while serving requests."""

    conversation_runtime_registry: _ConversationRuntimeRegistry
    conversation_service: ConversationService


def _runtime(request: Request) -> _ConversationRuntime:
    """Read conversation dependencies from the canonical host runtime."""
    return cast("_ConversationRuntime", request.app.state.runtime)


async def _to_read(
    service: ConversationService,
    conversation: Conversation[Fetched],
) -> ConversationRead:
    """Render canonical conversation state with its latest activity."""
    return ConversationRead.from_conversation(
        conversation,
        latest_activity=await service.latest_activity(conversation.id),
    )


async def _messages_response(
    request: Request,
    conversation_id: UUID,
    *,
    limit: int | None = None,
    before_seq: int | None = None,
) -> Response:
    """Serialize settled transcript rows or translate absence to 404."""
    try:
        messages = await _runtime(request).conversation_service.fetch_messages(
            conversation_id,
            limit=limit,
            before_seq=before_seq,
        )
    except ConversationNotFoundError:
        return JSONResponse({"detail": "conversation not found"}, status_code=404)
    return JSONResponse(
        [
            MessageRead.from_message(message).model_dump(mode="json")
            for message in messages
        ]
    )


router = APIRouter()


@router.get("/api/conversations", response_model=list[ConversationRead])
async def list_conversations(request: Request) -> Response:
    """List host-owned conversations."""
    service = _runtime(request).conversation_service
    conversations = await service.list_conversations()
    return JSONResponse(
        [
            (await _to_read(service, conversation)).model_dump(mode="json")
            for conversation in conversations
        ]
    )


@router.post(
    "/api/conversations/{conversation_id}/model",
    response_model=ConversationRead,
)
async def set_conversation_model(
    request: Request,
    body: SetConversationModelRequest,
    conversation_id: str,
) -> Response:
    """Select the model used for subsequent turns in one conversation."""
    try:
        parsed_conversation_id = UUID(conversation_id)
    except ValueError:
        return JSONResponse({"detail": "conversation not found"}, status_code=404)
    try:
        conversation, selected_model = await _runtime(
            request
        ).conversation_service.set_selected_model(
            parsed_conversation_id,
            body.selected_model,
        )
    except ConversationNotFoundError:
        return JSONResponse({"detail": "conversation not found"}, status_code=404)
    except ModelNotAllowedError:
        return JSONResponse({"detail": "model not allowed"}, status_code=422)
    try:
        await _runtime(request).conversation_runtime_registry.set_model(
            conversation.id,
            selected_model,
        )
    except PiRuntimeError:
        return JSONResponse({"detail": "set_model failed"}, status_code=502)
    return JSONResponse(
        (
            await _to_read(_runtime(request).conversation_service, conversation)
        ).model_dump(mode="json")
    )


@router.get(
    "/api/conversations/{conversation_id}/messages",
    response_model=list[MessageRead],
)
async def list_messages(
    request: Request,
    query: Annotated[MessagesQuery, Query()],
    conversation_id: str,
) -> Response:
    """List settled transcript rows for one conversation."""
    try:
        parsed_conversation_id = UUID(conversation_id)
    except ValueError:
        return JSONResponse({"detail": "conversation not found"}, status_code=404)
    return await _messages_response(
        request,
        parsed_conversation_id,
        limit=query.limit,
        before_seq=query.before_seq,
    )


@router.delete(
    "/api/conversations/{conversation_id}/messages",
    response_model=ConversationRead,
)
async def clear_messages(request: Request, conversation_id: str) -> Response:
    """Clear one conversation's transcript and rotate its pi session."""
    try:
        parsed_conversation_id = UUID(conversation_id)
    except ValueError:
        return JSONResponse({"detail": "conversation not found"}, status_code=404)
    service = _runtime(request).conversation_service
    try:
        conversation = await service.clear_conversation(parsed_conversation_id)
    except ConversationNotFoundError:
        return JSONResponse({"detail": "conversation not found"}, status_code=404)
    await _runtime(request).conversation_runtime_registry.discard(conversation.id)
    return JSONResponse((await _to_read(service, conversation)).model_dump(mode="json"))


__all__ = ["ConversationRead", "MessageRead", "MessagesQuery", "router"]
