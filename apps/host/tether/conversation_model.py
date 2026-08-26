"""Host-owned conversation domain values and error identities."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from uuid import UUID

type JsonValue = (
    None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]
)
type ConversationArchiveBlocker = Literal[
    "active_prompt_trigger",
    "nonterminal_turn",
]
type ConversationKind = Literal["main", "scoped"]
type ConversationStatus = Literal["active", "archived"]
type ConversationTurnOrigin = Literal[
    "capture", "historical", "interactive", "scheduled"
]
type ConversationTurnStatus = Literal[
    "pending",
    "running",
    "succeeded",
    "failed",
    "cancelled",
]
type MessageRole = Literal["user", "scheduled", "assistant", "tool", "reasoning"]


class ConversationArchiveBlockedError(Exception):
    """A durable dependent lifecycle prevents Conversation archival."""

    def __init__(self, blocker: ConversationArchiveBlocker) -> None:
        self.blocker: ConversationArchiveBlocker = blocker
        super().__init__(blocker)


class ConversationNotFoundError(Exception):
    """A requested host-owned conversation does not exist."""


class ConversationValidationError(Exception):
    """Conversation lifecycle input violates a domain invariant."""


@dataclass(frozen=True, slots=True)
class MessageDraft:
    """A settled transcript row ready to append to one conversation."""

    content: str
    conversation_id: UUID
    role: MessageRole
    turn_id: UUID | None = None
    turn_message_seq: int | None = None
    pi_message_id: str | None = None
    tool_args: dict[str, JsonValue] | None = None
    tool_name: str | None = None
    tool_result: dict[str, JsonValue] | None = None


__all__ = [
    "ConversationArchiveBlockedError",
    "ConversationArchiveBlocker",
    "ConversationKind",
    "ConversationNotFoundError",
    "ConversationStatus",
    "ConversationTurnOrigin",
    "ConversationTurnStatus",
    "ConversationValidationError",
    "JsonValue",
    "MessageDraft",
    "MessageRole",
]
