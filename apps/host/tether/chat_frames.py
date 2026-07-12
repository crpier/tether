"""Pydantic models for the outbound chat WebSocket frame contract.

The chat ws stream is the one turn-path contract that stays outside the
generated ADR-0005/0008 OpenAPI pipeline, so its frame shapes were previously
hand-maintained as inline `dict[str, object]` literals in `chat_ws`. These
models name each frame once on the host side; `chat_ws` emits `Frame(...).wire()`
and the TS `ChatFrame` union in apps/web is the single translation point on the
browser side. This only names the shapes — the wire bytes are unchanged and the
stream remains outside OpenAPI (ADR 0008).
"""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel


class _WireFrame(BaseModel):
    """Base for browser-bound frames; `wire()` is the exact send_json payload.

    `mode="json"` narrows the model's typed fields (UUID, ...) to JSON-native
    values so the dict is safe to hand straight to `WebSocket.send_json`.
    """

    def wire(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class _ConversationFrame(_WireFrame):
    """Base for turn-path frames the browser folds into the live timeline."""

    type: Literal["chat"] = "chat"
    conversation_id: UUID
    event: str


class UserMessageFrame(_ConversationFrame):
    """A persisted user turn, tagged with its stored id and monotonic seq."""

    event: Literal["user_message"] = "user_message"
    message_id: UUID
    seq: int


class MessageStartFrame(_ConversationFrame):
    """pi opened a new assistant message (one model turn began)."""

    event: Literal["message_start"] = "message_start"


class StreamUpdateFrame(_ConversationFrame):
    """One streamed assistant update, forwarding pi's raw delta verbatim.

    `event` is dynamic — `text_delta`, `thinking_delta`, or an uninterpreted
    stream-note kind (`text_start`, `toolcall_delta`, ...) — and `delta` carries
    the provider payload exactly as pi emitted it (a bare string or `{text}`).
    """

    delta: Any
    content_index: int | None


class MessageEndFrame(_ConversationFrame):
    """pi closed an assistant message; its settled rows are now persisted."""

    event: Literal["message_end"] = "message_end"


class ToolStartFrame(_ConversationFrame):
    """pi began executing one tool call."""

    event: Literal["tool_start"] = "tool_start"
    tool_name: str | None
    tool_id: str | None
    tool_args: dict[str, Any]


class ToolEndFrame(_ConversationFrame):
    """pi finished one tool call; `tool_result` is its JSON result object."""

    event: Literal["tool_end"] = "tool_end"
    tool_name: str | None
    tool_id: str | None
    tool_result: dict[str, Any]


class AgentEndFrame(_ConversationFrame):
    """pi finished the whole turn; the terminal frame of a turn stream."""

    event: Literal["agent_end"] = "agent_end"


class AbortAckFrame(_ConversationFrame):
    """The host acknowledged a browser abort request."""

    event: Literal["abort_ack"] = "abort_ack"


class ErrorFrame(_WireFrame):
    """A tagged chat error; `conversation_id` is dropped when the failure is
    not scoped to a conversation (e.g. a malformed inbound frame)."""

    type: Literal["chat"] = "chat"
    event: Literal["error"] = "error"
    detail: str
    conversation_id: UUID | None = None

    def wire(self) -> dict[str, Any]:
        # Omit the conversation tag entirely when absent, rather than sending a
        # null — the browser treats the key as optional.
        return self.model_dump(mode="json", exclude_none=True)


class NotifyFrame(_WireFrame):
    """A push notification relayed from the service-layer event hub."""

    type: Literal["notify"] = "notify"
    trigger_id: str
    title: str | None
    body: str


class InvalidateFrame(_WireFrame):
    """A cache-invalidation signal carrying the affected query keys."""

    type: Literal["invalidate"] = "invalidate"
    keys: list[str]
