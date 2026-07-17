"""The internal `read_conversation_history` tool.

Mounts alongside the Memory/Bucket/Recall tools under `/internal/tools/*` —
the loopback seam a pi process calls back into — reusing the same auth gate
and params-to-envelope validation (`tether.tools`). It has no REST twin: it
exists only so pi can recover context after a session rotation.

Every turn, `ConversationService.resolve_session` may rotate the conversation
onto a fresh pi session once the idle gap runs cold (`SESSION_GAP`), so the
visible transcript (host-owned `Message` rows) outlives what pi's own context
holds. This tool lets pi read the settled rows that predate its live session —
sourced from those `Message` rows (clean, provider-agnostic), never from the
pi-sessions JSONL transcripts.

The output is shaped for conversational context, not a raw transcript dump:
`tool` rows collapse to a one-line "used <tool>" marker (their `tool_args`/
`tool_result` can be arbitrarily large and are dropped entirely), `reasoning`
rows are skipped (internal chain-of-thought, not context a reply should lean
on), and long `user`/`assistant` content is truncated. Timestamps ride along
so pi can judge recency.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, cast
from uuid import UUID

from pydantic import BaseModel, Field
from snekql.sqlite import Fetched
from starlette.requests import Request
from starlette.routing import Route

from tether.capabilities import CapabilityOutcome, bind_params
from tether.conversations import ConversationNotFoundError, Message, MessageRole
from tether.tools import ToolSpec

_DEFAULT_LIMIT = 20
_MAX_LIMIT = 50
_MAX_CONTENT_CHARS = 2000
_TRUNCATION_MARKER = "…[truncated]"


class ReadConversationHistoryParams(BaseModel):
    """Params for reading transcript rows from before the live pi session.

    `limit` windows to the newest `limit` prior-session rows (capped at 50 so
    a single call can't flood the model's context); `before` is an optional
    `seq` cursor for paging further back once the first page has been read.
    """

    limit: Annotated[int, Field(ge=1, le=_MAX_LIMIT)] = _DEFAULT_LIMIT
    before: Annotated[int, Field(ge=1)] | None = None


class ConversationHistoryEntryRead(BaseModel):
    """One compact prior-session transcript row.

    `seq` rides along so a further call can pass it back as `before` to page
    still further into the past.

    >>> ConversationHistoryEntryRead(
    ...     seq=1,
    ...     role="tool",
    ...     content="used search",
    ...     created_at=datetime(2026, 1, 1),
    ... ).role
    'tool'
    """

    seq: int
    role: MessageRole
    content: str
    created_at: datetime


def _truncate(content: str) -> str:
    """Cap a `user`/`assistant` row's content so one huge message can't blow the payload."""
    if len(content) <= _MAX_CONTENT_CHARS:
        return content
    return content[:_MAX_CONTENT_CHARS] + _TRUNCATION_MARKER


def _render(message: Message[Fetched]) -> ConversationHistoryEntryRead | None:
    """Shape one settled row for context, or drop it (`reasoning` rows)."""
    if message.role == "reasoning":
        return None
    if message.role == "tool":
        content = f"used {message.tool_name or 'a tool'}"
    else:
        content = _truncate(message.content)
    return ConversationHistoryEntryRead(
        seq=message.seq,
        role=message.role,
        content=content,
        created_at=message.created_at,
    )


async def read_conversation_history(
    request: Request, limit: int, before: int | None
) -> CapabilityOutcome:
    """Read settled transcript rows from before the caller's live pi session.

    `request.state.session_id` is the caller's pi session id, set by
    `ToolEndpoint` once the auth gate has already validated it against the
    live `SessionRegistry`; it is resolved back to the host-owned conversation
    it currently belongs to.
    """
    session_id = cast("str", request.state.session_id)
    service = request.app.state.conversation_service
    try:
        conversation = await service.fetch_conversation_by_pi_session_id(
            UUID(session_id)
        )
    except ConversationNotFoundError:
        return CapabilityOutcome(result=[])
    messages = await service.fetch_prior_session_messages(
        conversation.id, limit=limit, before_seq=before
    )
    entries = [entry for entry in (_render(message) for message in messages) if entry]
    return CapabilityOutcome(
        result=[entry.model_dump(mode="json") for entry in entries]
    )


CONVERSATION_HISTORY_TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        "read_conversation_history",
        ReadConversationHistoryParams,
        bind_params(read_conversation_history),
    ),
)
"""The prior-session transcript read, exposed as an internal tool."""


def internal_conversation_history_tool_routes() -> list[Route]:
    """Mount `read_conversation_history` as an `/internal/tools/*` POST endpoint.

    Returned separately from the public conversation routes (and the other
    tools) so it stays absent from the public OpenAPI document and generated
    client.
    """
    return [spec.route() for spec in CONVERSATION_HISTORY_TOOL_SPECS]
