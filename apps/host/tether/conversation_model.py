"""Host-owned conversation domain values and error identities."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from uuid import UUID

type JsonValue = (
    None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]
)
type MessageRole = Literal["user", "assistant", "tool", "reasoning"]


class ConversationNotFoundError(Exception):
    """A requested host-owned conversation does not exist."""


@dataclass(frozen=True, slots=True)
class MessageDraft:
    """A settled transcript row ready to append to one conversation."""

    content: str
    conversation_id: UUID
    role: MessageRole
    pi_message_id: str | None = None
    tool_args: dict[str, JsonValue] | None = None
    tool_name: str | None = None
    tool_result: dict[str, JsonValue] | None = None


__all__ = ["ConversationNotFoundError", "JsonValue", "MessageDraft", "MessageRole"]
