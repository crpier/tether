"""Provider parsers normalising AI-chat exports into one conversation shape.

Conversation import bootstraps Memories and Bucket items from external AI-chat
exports (#22). Each provider exports its own JSON dialect; this module is the
first stage of the pipeline: a per-provider parser that reads a raw export and
yields a list of `ImportedConversation` — the single normalised shape the rest
of the pipeline (scheduler jobs, agentic extraction, the Candidate gate) builds
on, with no provider-specific knowledge leaking past here.

The normalised shape carries exactly what downstream stages need: the
`source` provider and `source_conversation_id` (the idempotency key a re-import
deduplicates on), the title and timestamps for provenance, and the ordered
user/assistant turns. Parsers are faithful normalisers, not filters: they drop
only what cannot become a turn (empty content, messages orphaned from any
conversation) and leave extraction-quality judgements to later stages.

>>> parser = T3ChatParser()
>>> conversations = parser.parse(raw_export_text)
>>> conversations[0].source
't3chat'
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol, cast, runtime_checkable

type ConversationSource = Literal["t3chat", "chatgpt", "claude"]
"""Which provider an imported conversation was exported from."""

type ImportedRole = Literal["user", "assistant"]
"""The author of a normalised turn. Provider-specific roles (system, tool) are
not turns the extractor reasons over and are dropped during parsing."""


class ConversationParseError(Exception):
    """Raised when a raw export cannot be parsed into the normalised shape.

    Signals a malformed or unexpected export envelope (not valid JSON, or the
    wrong top-level structure for the provider), as opposed to an export that
    parses cleanly but happens to contain no usable conversations.
    """


@dataclass(frozen=True, slots=True)
class ImportedMessage:
    """One normalised user/assistant turn within an imported conversation."""

    role: ImportedRole
    content: str
    source_message_id: str | None
    created_at: datetime | None


@dataclass(frozen=True, slots=True)
class ImportedConversation:
    """A provider-agnostic conversation: the unit one import job processes.

    `source_conversation_id` is the provider's own id for the conversation and
    the key re-imports deduplicate on, so it must be stable across exports of the
    same conversation. `messages` are ordered oldest-first and always non-empty
    (a conversation with no usable turns is dropped by the parser).
    """

    source: ConversationSource
    source_conversation_id: str
    title: str | None
    created_at: datetime | None
    messages: tuple[ImportedMessage, ...]


@runtime_checkable
class ConversationParser(Protocol):
    """The seam every provider parser implements, dispatched on `source`.

    A structural Protocol so the import pipeline can hold a registry of parsers
    keyed by provider without importing each concrete class."""

    source: ConversationSource

    def parse(self, raw: str | bytes) -> list[ImportedConversation]:
        """Normalise a raw provider export into ordered conversations."""
        ...


def _from_epoch_millis(value: object) -> datetime | None:
    """Convert a provider's epoch-millisecond timestamp to an aware datetime.

    Returns `None` for an absent or non-numeric timestamp rather than raising:
    a missing timestamp is provenance we simply don't have, not a parse failure.
    """
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    return datetime.fromtimestamp(value / 1000, tz=UTC)


def _normalise_role(value: object) -> ImportedRole | None:
    """Map a provider role to a normalised turn author, or `None` to drop it."""
    if value == "user":
        return "user"
    if value == "assistant":
        return "assistant"
    return None


def _sort_key(value: object) -> float:
    """Order key for a turn: its epoch-ms capture time, undated turns last."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return float("inf")


class T3ChatParser:
    """Parser for t3chat's `threads-export-*.json` dialect.

    t3chat exports a flat envelope — `{"threads": [...], "messages": [...]}` —
    where messages are *not* nested under their thread but linked by `threadId`.
    So parsing groups the flat message list by thread, orders each group by its
    capture time, and pairs it with the matching thread's metadata. Messages
    whose `threadId` names no exported thread are orphans with no conversation to
    join and are dropped.
    """

    source: ConversationSource = "t3chat"

    def parse(self, raw: str | bytes) -> list[ImportedConversation]:
        """Normalise a t3chat export into ordered, non-empty conversations."""
        try:
            loaded: object = json.loads(raw)
        except json.JSONDecodeError as error:
            msg = "t3chat export is not valid JSON"
            raise ConversationParseError(msg) from error
        if not isinstance(loaded, dict):
            msg = "t3chat export must be a JSON object"
            raise ConversationParseError(msg)
        document = cast("dict[str, object]", loaded)
        raw_threads = document.get("threads")
        raw_messages = document.get("messages")
        if not isinstance(raw_threads, list) or not isinstance(raw_messages, list):
            msg = "t3chat export must contain `threads` and `messages` arrays"
            raise ConversationParseError(msg)

        turns_by_thread = self._group_turns(cast("list[object]", raw_messages))
        conversations: list[ImportedConversation] = []
        for raw_thread in cast("list[object]", raw_threads):
            if not isinstance(raw_thread, dict):
                continue
            thread = cast("dict[str, object]", raw_thread)
            thread_id = thread.get("id") or thread.get("threadId")
            if not isinstance(thread_id, str):
                continue
            turns = turns_by_thread.get(thread_id)
            if not turns:
                # A thread whose only messages were empty (or had none) has no
                # turn to extract from; drop it rather than emit an empty shell.
                continue
            turns.sort(key=_turn_sort_key)
            title = thread.get("title")
            conversations.append(
                ImportedConversation(
                    source="t3chat",
                    source_conversation_id=thread_id,
                    title=title if isinstance(title, str) and title else None,
                    created_at=_from_epoch_millis(thread.get("created_at")),
                    messages=tuple(message for _, message in turns),
                )
            )
        return conversations

    def _group_turns(
        self, messages: list[object]
    ) -> dict[str, list[tuple[float, ImportedMessage]]]:
        """Bucket usable messages by `threadId`, keeping their sort key alongside.

        A message is usable only if it has a known role and non-blank content;
        everything else (a `waiting`/`error` stub, a system turn) carries no turn
        and is skipped. Orphans land under a `threadId` no thread will claim, so
        they never reach a conversation.
        """
        grouped: dict[str, list[tuple[float, ImportedMessage]]] = defaultdict(list)
        for raw_message in messages:
            if not isinstance(raw_message, dict):
                continue
            message = cast("dict[str, object]", raw_message)
            thread_id = message.get("threadId")
            role = _normalise_role(message.get("role"))
            content = message.get("content")
            if not isinstance(thread_id, str) or role is None:
                continue
            if not isinstance(content, str) or not content.strip():
                continue
            created_raw = message.get("created_at")
            message_id = message.get("id")
            grouped[thread_id].append(
                (
                    _sort_key(created_raw),
                    ImportedMessage(
                        role=role,
                        content=content.strip(),
                        source_message_id=message_id
                        if isinstance(message_id, str)
                        else None,
                        created_at=_from_epoch_millis(created_raw),
                    ),
                )
            )
        return grouped


def _turn_sort_key(turn: tuple[float, ImportedMessage]) -> float:
    """Order turns oldest-first by capture time; undated turns sort last."""
    return turn[0]


PARSERS: dict[ConversationSource, ConversationParser] = {
    parser.source: parser for parser in (T3ChatParser(),)
}
"""Registry of available provider parsers, keyed by source.

The import pipeline dispatches on the chosen provider through this. Only t3chat
ships today; chatgpt and claude join as their parsers land."""
