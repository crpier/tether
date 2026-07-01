"""Host-owned conversation and transcript storage."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, cast
from uuid import UUID, uuid7

from pydantic import UUID7, BaseModel, PositiveInt
from snekql.sqlite import (
    CurrentTimestamp,
    Database,
    Fetched,
    Integer,
    Model,
    Pending,
    Text,
    Transaction,
    delete,
    insert,
    select,
    update,
)
from snekql.sqlite._schema_ddl import scaffold_sqlite_statements
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from tether.db_retry import run_in_transaction
from tether.model_selection import (
    AgentModelCatalog,
    AgentModelConfig,
    ModelNotAllowedError,
)
from tether.openapi import EndpointRoute, endpoint

type MessageRole = Literal["user", "assistant", "tool", "reasoning"]
type JsonValue = (
    None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]
)


class ConversationNotFoundError(Exception):
    """Raised when transcript history is requested for an absent conversation."""


def _as_utc(value: datetime) -> datetime:
    """Read a stored timestamp as UTC-aware; SQLite `CURRENT_TIMESTAMP` is naive."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class MessageDraft:
    """A transcript row ready to append to one conversation."""

    content: str
    conversation_id: UUID
    role: MessageRole
    pi_message_id: str | None = None
    tool_args: dict[str, JsonValue] | None = None
    tool_name: str | None = None
    tool_result: dict[str, JsonValue] | None = None


class Conversation[S = Pending](Model[S, "Conversation[Fetched]"]):
    """A stable host-owned chat thread."""

    id: Conversation.GenCol[UUID7] = Text(primary_key=True, default_factory=uuid7)
    created_at: Conversation.GenCol[datetime] = Text(default=CurrentTimestamp)
    pi_session_id: Conversation.GenCol[UUID7] = Text(default_factory=uuid7)
    selected_model: Conversation.Col[str | None] = Text(default=None, nullable=True)
    title: Conversation.Col[str | None] = Text(default=None, nullable=True)


class Message[S = Pending](Model[S, "Message[Fetched]"]):
    """One settled transcript row owned by the host."""

    id: Message.GenCol[UUID7] = Text(primary_key=True, default_factory=uuid7)
    conversation_id: Message.Col[UUID7] = Text()
    seq: Message.Col[PositiveInt] = Integer()
    role: Message.Col[MessageRole] = Text()
    content: Message.Col[str] = Text()
    created_at: Message.GenCol[datetime] = Text(default=CurrentTimestamp)
    pi_message_id: Message.Col[str | None] = Text(default=None, nullable=True)
    tool_args: Message.Col[str | None] = Text(default=None, nullable=True)
    tool_name: Message.Col[str | None] = Text(default=None, nullable=True)
    tool_result: Message.Col[str | None] = Text(default=None, nullable=True)


class ConversationRead(BaseModel):
    """HTTP representation of a host-owned conversation."""

    created_at: datetime
    id: UUID7
    pi_session_id: UUID7
    selected_model: str | None
    title: str | None

    @classmethod
    def from_conversation(cls, conversation: Conversation[Fetched]) -> ConversationRead:
        """Render a stored Conversation as JSON-safe response data."""
        return cls(
            created_at=conversation.created_at,
            id=conversation.id,
            pi_session_id=conversation.pi_session_id,
            selected_model=conversation.selected_model,
            title=conversation.title,
        )


class SetConversationModelRequest(BaseModel):
    """Body for selecting a conversation's model."""

    selected_model: str


class MessageRead(BaseModel):
    """HTTP representation of a settled transcript row."""

    content: str
    conversation_id: UUID7
    created_at: datetime
    id: UUID7
    pi_message_id: str | None
    role: MessageRole
    seq: PositiveInt
    tool_args: dict[str, JsonValue] | None
    tool_name: str | None
    tool_result: dict[str, JsonValue] | None

    @classmethod
    def from_message(cls, message: Message[Fetched]) -> MessageRead:
        """Render a stored Message as JSON-safe response data."""
        return cls(
            content=message.content,
            conversation_id=message.conversation_id,
            created_at=message.created_at,
            id=message.id,
            pi_message_id=message.pi_message_id,
            role=message.role,
            seq=message.seq,
            tool_args=json.loads(message.tool_args)
            if message.tool_args is not None
            else None,
            tool_name=message.tool_name,
            tool_result=cast("dict[str, JsonValue]", json.loads(message.tool_result))
            if message.tool_result is not None
            else None,
        )


class ConversationService:
    """Persistence boundary for conversations and settled transcript rows."""

    def __init__(
        self,
        database: Database,
        *,
        model_catalog: AgentModelCatalog | None = None,
    ) -> None:
        self.database: Database = database
        self.model_catalog: AgentModelCatalog = model_catalog or AgentModelCatalog(
            default_model=None,
            models=(),
        )

    async def list_conversations(self) -> list[Conversation[Fetched]]:
        """Return all conversations, creating the v1 default on first access."""

        async def _list(tx: Transaction) -> list[Conversation[Fetched]]:
            conversations = await tx.fetch_all(
                select(Conversation).all().order_by(Conversation.created_at.asc())
            )
            if conversations:
                return conversations
            conversation = await tx.execute(
                insert(
                    Conversation(selected_model=self.model_catalog.default_model)
                ).returning()
            )
            return [conversation]

        return await run_in_transaction(self.database, _list)

    async def fetch_conversation(self, conversation_id: UUID) -> Conversation[Fetched]:
        """Return one conversation or raise when the id is unknown."""
        async with self.database.transaction() as tx:
            conversation = await tx.fetch_one_or_none(
                select(Conversation).where(Conversation.id.eq(conversation_id))
            )
        if conversation is None:
            raise ConversationNotFoundError(conversation_id)
        return conversation

    async def set_selected_model(
        self,
        conversation_id: UUID,
        selected_model: str,
    ) -> tuple[Conversation[Fetched], AgentModelConfig]:
        """Persist a conversation's selected allowlist model id."""
        model = self.model_catalog.resolve(selected_model)
        if model is None:
            raise ModelNotAllowedError(selected_model)

        async def _set_selected_model(
            tx: Transaction,
        ) -> Conversation[Fetched] | None:
            _ = await tx.execute(
                update(Conversation)
                .set(Conversation.selected_model.to(model.id))
                .where(Conversation.id.eq(conversation_id))
            )
            return await tx.fetch_one_or_none(
                select(Conversation).where(Conversation.id.eq(conversation_id))
            )

        conversation = await run_in_transaction(self.database, _set_selected_model)
        if conversation is None:
            raise ConversationNotFoundError(conversation_id)
        return conversation, model

    async def resolve_session(
        self,
        conversation: Conversation[Fetched],
        *,
        now: datetime,
        gap: timedelta,
    ) -> Conversation[Fetched]:
        """Return the conversation to prompt, rotating pi if the gap ran cold.

        A gap shorter than `gap` reuses the live pi session (warm provider
        cache); a longer gap rotates to a fresh session. A conversation with no
        prior activity is treated as warm — there is nothing stale to abandon.
        """
        last = await self.latest_activity(conversation.id)
        if last is None or _as_utc(now) - _as_utc(last) < gap:
            return conversation
        return await self.rotate_pi_session(conversation.id)

    async def latest_activity(self, conversation_id: UUID) -> datetime | None:
        """Return when the last transcript row landed, or None if empty.

        This is the server-observed wall-clock of the most recent turn — the
        signal used to decide whether an incoming message still lands inside the
        provider's prompt-cache warmth window.
        """
        async with self.database.transaction() as tx:
            latest = await tx.fetch_one_or_none(
                select(Message)
                .where(Message.conversation_id.eq(conversation_id))
                .order_by(Message.seq.desc())
                .limit(1)
            )
        return latest.created_at if latest is not None else None

    async def rotate_pi_session(self, conversation_id: UUID) -> Conversation[Fetched]:
        """Point a conversation at a fresh pi session; transcript rows are kept.

        The user keeps seeing one continuous conversation; only the underlying
        pi session identity changes, so the next turn starts pi with an empty
        context instead of resending a stale, uncached history.
        """

        async def _rotate(tx: Transaction) -> Conversation[Fetched] | None:
            _ = await tx.execute(
                update(Conversation)
                .set(Conversation.pi_session_id.to(uuid7()))
                .where(Conversation.id.eq(conversation_id))
            )
            return await tx.fetch_one_or_none(
                select(Conversation).where(Conversation.id.eq(conversation_id))
            )

        conversation = await run_in_transaction(self.database, _rotate)
        if conversation is None:
            raise ConversationNotFoundError(conversation_id)
        return conversation

    async def clear_conversation(self, conversation_id: UUID) -> Conversation[Fetched]:
        """Delete a conversation's transcript rows and rotate its pi session.

        The user's single continuous thread is emptied so they can start fresh;
        rotating `pi_session_id` in the same transaction means the next turn
        also starts pi with an empty context rather than replaying the (now
        deleted) history.

        This is a deliberately destructive, unconditional "New chat" action: the
        transcript is hard-deleted with no version precondition and no undo. That
        is accepted here because it is explicit single-user intent, not a
        convergence/overwrite hazard — the caveat in docs/principles.md about
        state-destructive edits is knowingly waived for this reset.
        """

        async def _clear(tx: Transaction) -> Conversation[Fetched] | None:
            _ = await tx.execute(
                delete(Message).where(Message.conversation_id.eq(conversation_id))
            )
            _ = await tx.execute(
                update(Conversation)
                .set(Conversation.pi_session_id.to(uuid7()))
                .where(Conversation.id.eq(conversation_id))
            )
            return await tx.fetch_one_or_none(
                select(Conversation).where(Conversation.id.eq(conversation_id))
            )

        conversation = await run_in_transaction(self.database, _clear)
        if conversation is None:
            raise ConversationNotFoundError(conversation_id)
        return conversation

    async def fetch_messages(self, conversation_id: UUID) -> list[Message[Fetched]]:
        """Return settled transcript rows for a conversation in display order."""
        async with self.database.transaction() as tx:
            conversation = await tx.fetch_one_or_none(
                select(Conversation).where(Conversation.id.eq(conversation_id))
            )
            if conversation is None:
                raise ConversationNotFoundError(conversation_id)
            return await tx.fetch_all(
                select(Message)
                .where(Message.conversation_id.eq(conversation_id))
                .order_by(Message.seq.asc())
            )

    async def append_message(self, draft: MessageDraft) -> Message[Fetched]:
        """Append one settled transcript row with a monotonic per-thread sequence."""

        async def _append(tx: Transaction) -> Message[Fetched]:
            conversation = await tx.fetch_one_or_none(
                select(Conversation).where(Conversation.id.eq(draft.conversation_id))
            )
            if conversation is None:
                raise ConversationNotFoundError(draft.conversation_id)
            if draft.pi_message_id is not None:
                existing = await tx.fetch_one_or_none(
                    select(Message)
                    .where(Message.conversation_id.eq(draft.conversation_id))
                    .where(Message.pi_message_id.eq(draft.pi_message_id))
                )
                if existing is not None:
                    return existing
            latest = await tx.fetch_one_or_none(
                select(Message)
                .where(Message.conversation_id.eq(draft.conversation_id))
                .order_by(Message.seq.desc())
                .limit(1)
            )
            return await tx.execute(
                insert(
                    Message(
                        content=draft.content,
                        conversation_id=conversation.id,
                        pi_message_id=draft.pi_message_id,
                        role=draft.role,
                        seq=1 if latest is None else latest.seq + 1,
                        tool_args=json.dumps(draft.tool_args)
                        if draft.tool_args is not None
                        else None,
                        tool_name=draft.tool_name,
                        tool_result=json.dumps(draft.tool_result)
                        if draft.tool_result is not None
                        else None,
                    )
                ).returning()
            )

        return await run_in_transaction(self.database, _append)


async def create_conversation_schema(database: Database) -> None:
    """Create conversation and transcript tables on an initialized database."""
    migrations = {
        f"003_{label}": sql
        for label, sql in scaffold_sqlite_statements([Conversation, Message])
    }
    await database.migrate(migrations)


@endpoint(response=ConversationRead, response_is_list=True)
async def list_conversations(request: Request) -> Response:
    """List host-owned conversations."""
    conversations = await request.app.state.conversation_service.list_conversations()
    return JSONResponse(
        [
            ConversationRead.from_conversation(conversation).model_dump(mode="json")
            for conversation in conversations
        ]
    )


async def _messages_response(request: Request, conversation_id: UUID) -> Response:
    """Serialize settled transcript rows or translate absence to 404."""
    try:
        messages = await request.app.state.conversation_service.fetch_messages(
            conversation_id
        )
    except ConversationNotFoundError:
        return JSONResponse({"detail": "conversation not found"}, status_code=404)
    return JSONResponse(
        [
            MessageRead.from_message(message).model_dump(mode="json")
            for message in messages
        ]
    )


@endpoint(request_body=SetConversationModelRequest, response=ConversationRead)
async def set_conversation_model(
    request: Request,
    body: SetConversationModelRequest,
) -> Response:
    """Select the model used for subsequent turns in one conversation."""
    raw_conversation_id = request.path_params["conversation_id"]
    try:
        conversation_id = UUID(raw_conversation_id)
    except ValueError:
        return JSONResponse({"detail": "conversation not found"}, status_code=404)
    try:
        (
            conversation,
            selected_model,
        ) = await request.app.state.conversation_service.set_selected_model(
            conversation_id,
            body.selected_model,
        )
    except ConversationNotFoundError:
        return JSONResponse({"detail": "conversation not found"}, status_code=404)
    except ModelNotAllowedError:
        return JSONResponse({"detail": "model not allowed"}, status_code=422)
    runtime = request.app.state.conversation_runtime_registry.current_for(
        conversation.id
    )
    if runtime is not None:
        response = await runtime.client.request(
            "set_model",
            provider=selected_model.provider,
            modelId=selected_model.model_id,
        )
        if response.get("success") is not True:
            return JSONResponse({"detail": "set_model failed"}, status_code=502)
    return JSONResponse(
        ConversationRead.from_conversation(conversation).model_dump(mode="json")
    )


@endpoint(response=MessageRead, response_is_list=True)
async def list_messages(request: Request) -> Response:
    """List settled transcript rows for one conversation."""
    raw_conversation_id = request.path_params["conversation_id"]
    try:
        conversation_id = UUID(raw_conversation_id)
    except ValueError:
        return JSONResponse({"detail": "conversation not found"}, status_code=404)
    return await _messages_response(request, conversation_id)


@endpoint(response=ConversationRead)
async def clear_messages(request: Request) -> Response:
    """Clear one conversation's transcript and rotate its pi session."""
    raw_conversation_id = request.path_params["conversation_id"]
    try:
        conversation_id = UUID(raw_conversation_id)
    except ValueError:
        return JSONResponse({"detail": "conversation not found"}, status_code=404)
    try:
        conversation = await request.app.state.conversation_service.clear_conversation(
            conversation_id
        )
    except ConversationNotFoundError:
        return JSONResponse({"detail": "conversation not found"}, status_code=404)
    # Tear down any live runtime bound to the now-rotated session so the next
    # turn spawns clean against the fresh pi session instead of replaying it.
    runtime = request.app.state.conversation_runtime_registry.current_for(
        conversation.id
    )
    if runtime is not None:
        await runtime.shutdown()
    return JSONResponse(
        ConversationRead.from_conversation(conversation).model_dump(mode="json")
    )


conversation_routes: list[Route] = [
    EndpointRoute("/api/conversations", list_conversations, methods=["GET"]),
    EndpointRoute(
        "/api/conversations/{conversation_id}/model",
        set_conversation_model,
        methods=["POST"],
    ),
    EndpointRoute(
        "/api/conversations/{conversation_id}/messages",
        list_messages,
        methods=["GET"],
    ),
    EndpointRoute(
        "/api/conversations/{conversation_id}/messages",
        clear_messages,
        methods=["DELETE"],
    ),
]
