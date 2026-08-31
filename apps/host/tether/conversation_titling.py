"""First-message conversation auto-titling (host-owned, fire-and-forget).

A Scoped Conversation may start with no name at all. When its first user
message lands, one ephemeral model prompt proposes a short title; the first
proposal wins and later messages never rename the chat.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Protocol, runtime_checkable
from uuid import UUID

from tether.conversation_model import ConversationNotFoundError
from tether.conversations import ConversationService
from tether.structured_logging import Logger

MAX_TITLE_LENGTH = 60
"""Longest generated title before truncation keeps sidebar labels usable."""


@runtime_checkable
class TitleGenerator(Protocol):
    """Propose a chat title from a conversation's first user message."""

    async def generate_title(self, *, first_message: str) -> str:
        """Return one proposed title."""
        ...


class PiPromptRunner(Protocol):
    """Run one ephemeral prompt and return its raw response."""

    async def run(self, prompt: str) -> str:
        """Return the model response text."""
        ...


@dataclass(frozen=True, slots=True)
class PiTitleGenerator:
    """Run the title proposal through an ephemeral pi one-shot."""

    runner: PiPromptRunner
    """An `EphemeralPiPromptRunner`-shaped runner (`run(prompt) -> str`)."""

    async def generate_title(self, *, first_message: str) -> str:
        """Ask the model for a title and return its raw reply text."""
        prompt = (
            "Propose a concise title (at most six words, Title Case, no quotes "
            "or trailing punctuation) for a chat that starts with this message. "
            "Reply with only the title.\n\n"
            f"Message: {first_message}"
        )
        return await self.runner.run(prompt)


_WRAPPING_QUOTES = "\"'\u201c\u201d\u2018\u2019"


def _sanitize_title(raw: str) -> str:
    """Collapse whitespace, strip wrapping quotes, and cap the length."""
    cleaned = " ".join(raw.split())
    cleaned = cleaned.strip(_WRAPPING_QUOTES).strip()
    if len(cleaned) > MAX_TITLE_LENGTH:
        cleaned = cleaned[:MAX_TITLE_LENGTH].rstrip()
    return cleaned


class ConversationTitler:
    """Name untitled chats from their first message, exactly once."""

    def __init__(
        self,
        *,
        conversation_service: ConversationService,
        generator: TitleGenerator,
        logger: Logger,
    ) -> None:
        self.conversation_service: ConversationService = conversation_service
        self.generator: TitleGenerator = generator
        self.logger: Logger = logger
        self._tasks: set[asyncio.Task[None]] = set()

    def schedule(self, *, conversation_id: UUID, first_message: str) -> None:
        """Spawn one fire-and-forget titling run for a settled user message."""
        task = asyncio.create_task(
            self.title_from_first_message(
                conversation_id,
                first_message=first_message,
            )
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def title_from_first_message(
        self,
        conversation_id: UUID,
        *,
        first_message: str,
    ) -> None:
        """Generate and apply a title if (and only if) the chat is untitled.

        Expected failures are swallowed and logged: titling is best-effort
        decoration around a live chat turn, never a turn-blocking step.
        """
        try:
            conversation = await self.conversation_service.fetch_conversation(
                conversation_id
            )
            if conversation.title is not None:
                return
            raw = await self.generator.generate_title(first_message=first_message)
            title = _sanitize_title(raw)
            if not title:
                self.logger.info(
                    "conversation titling produced no usable reply",
                    conversation_id=str(conversation_id),
                )
                return
            applied = await self.conversation_service.set_generated_title(
                conversation_id,
                title=title,
            )
            if applied:
                await self.conversation_service.publish_navigation_state()
        except ConversationNotFoundError:
            return
        except Exception as error:
            self.logger.warning(
                "conversation titling failed",
                conversation_id=str(conversation_id),
                error=str(error),
            )


__all__ = [
    "ConversationTitler",
    "PiTitleGenerator",
    "TitleGenerator",
]
