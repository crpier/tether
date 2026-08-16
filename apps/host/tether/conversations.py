"""Conversation lifecycle and settled-transcript orchestration."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from uuid import UUID, uuid7

from snekql.sqlite import (
    Database,
    Fetched,
    Transaction,
    delete,
    insert,
    select,
    update,
)

from tether.conversation_model import ConversationNotFoundError, MessageDraft
from tether.conversation_store import Conversation, Message
from tether.model_selection import (
    AgentModelCatalog,
    AgentModelConfig,
    ModelNotAllowedError,
)

SESSION_GAP = timedelta(minutes=5)
"""Idle window after which a new turn rotates onto a fresh pi session."""


def _as_utc(value: datetime) -> datetime:
    """Read a stored timestamp as UTC-aware; SQLite timestamps may be naive."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


class ConversationService:
    """Coordinate conversation sessions and canonical transcript persistence."""

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
        """Return all conversations, creating the default on first access."""

        async def _list(transaction: Transaction) -> list[Conversation[Fetched]]:
            conversations = await transaction.fetch_all(
                select(Conversation).all().order_by(Conversation.created_at.asc())
            )
            if conversations:
                return conversations
            conversation = await transaction.execute(
                insert(
                    Conversation(selected_model=self.model_catalog.default_model)
                ).returning()
            )
            return [conversation]

        async with self.database.transaction(mode="immediate") as transaction:
            return await _list(transaction)

    async def fetch_conversation(self, conversation_id: UUID) -> Conversation[Fetched]:
        """Return one conversation or raise when its id is unknown."""
        async with self.database.transaction() as transaction:
            conversation = await transaction.fetch_one_or_none(
                select(Conversation).where(Conversation.id.eq(conversation_id))
            )
        if conversation is None:
            raise ConversationNotFoundError(conversation_id)
        return conversation

    async def fetch_conversation_by_pi_session_id(
        self,
        pi_session_id: UUID,
    ) -> Conversation[Fetched]:
        """Resolve a live pi session id back to its host conversation."""
        async with self.database.transaction() as transaction:
            conversation = await transaction.fetch_one_or_none(
                select(Conversation).where(Conversation.pi_session_id.eq(pi_session_id))
            )
        if conversation is None:
            raise ConversationNotFoundError(pi_session_id)
        return conversation

    async def set_selected_model(
        self,
        conversation_id: UUID,
        selected_model: str,
    ) -> tuple[Conversation[Fetched], AgentModelConfig]:
        """Persist one allowlisted model selection."""
        model = self.model_catalog.resolve(selected_model)
        if model is None:
            raise ModelNotAllowedError(selected_model)

        async with self.database.transaction(mode="immediate") as transaction:
            _ = await transaction.execute(
                update(Conversation)
                .set(Conversation.selected_model.to(model.id))
                .where(Conversation.id.eq(conversation_id))
            )
            conversation = await transaction.fetch_one_or_none(
                select(Conversation).where(Conversation.id.eq(conversation_id))
            )
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
        """Reuse a warm pi session or rotate one whose activity gap ran cold."""
        last = await self.latest_activity(conversation.id)
        if last is None or _as_utc(now) - _as_utc(last) < gap:
            return conversation
        return await self.rotate_pi_session(conversation.id)

    async def latest_activity(self, conversation_id: UUID) -> datetime | None:
        """Return when the most recent transcript row landed, if any."""
        async with self.database.transaction() as transaction:
            latest = await transaction.fetch_one_or_none(
                select(Message)
                .where(Message.conversation_id.eq(conversation_id))
                .order_by(Message.seq.desc())
                .limit(1)
            )
        return latest.created_at if latest is not None else None

    async def rotate_pi_session(self, conversation_id: UUID) -> Conversation[Fetched]:
        """Rotate pi identity while retaining the host transcript."""
        async with self.database.transaction(mode="immediate") as transaction:
            _ = await transaction.execute(
                update(Conversation)
                .set(Conversation.pi_session_id.to(uuid7()))
                .where(Conversation.id.eq(conversation_id))
            )
            conversation = await transaction.fetch_one_or_none(
                select(Conversation).where(Conversation.id.eq(conversation_id))
            )
        if conversation is None:
            raise ConversationNotFoundError(conversation_id)
        return conversation

    async def clear_conversation(self, conversation_id: UUID) -> Conversation[Fetched]:
        """Delete transcript rows and rotate pi identity in one transaction."""
        async with self.database.transaction(mode="immediate") as transaction:
            _ = await transaction.execute(
                delete(Message).where(Message.conversation_id.eq(conversation_id))
            )
            _ = await transaction.execute(
                update(Conversation)
                .set(Conversation.pi_session_id.to(uuid7()))
                .where(Conversation.id.eq(conversation_id))
            )
            conversation = await transaction.fetch_one_or_none(
                select(Conversation).where(Conversation.id.eq(conversation_id))
            )
        if conversation is None:
            raise ConversationNotFoundError(conversation_id)
        return conversation

    async def fetch_messages(
        self,
        conversation_id: UUID,
        *,
        limit: int | None = None,
        before_seq: int | None = None,
    ) -> list[Message[Fetched]]:
        """Return a full transcript or an ascending window of its newest rows."""
        async with self.database.transaction() as transaction:
            conversation = await transaction.fetch_one_or_none(
                select(Conversation).where(Conversation.id.eq(conversation_id))
            )
            if conversation is None:
                raise ConversationNotFoundError(conversation_id)
            query = select(Message).where(Message.conversation_id.eq(conversation_id))
            if before_seq is not None:
                query = query.where(Message.seq.lt(before_seq))
            if limit is None:
                return await transaction.fetch_all(query.order_by(Message.seq.asc()))
            page = await transaction.fetch_all(
                query.order_by(Message.seq.desc()).limit(limit)
            )
            return list(reversed(page))

    async def current_session_start_seq(
        self,
        conversation_id: UUID,
        *,
        gap: timedelta = SESSION_GAP,
    ) -> int | None:
        """Recover the first transcript sequence after the latest cold gap."""
        async with self.database.transaction() as transaction:
            rows = await transaction.fetch_all(
                select(Message)
                .where(Message.conversation_id.eq(conversation_id))
                .order_by(Message.seq.asc())
            )
        boundary_seq: int | None = None
        for previous, current in pairwise(rows):
            if _as_utc(current.created_at) - _as_utc(previous.created_at) >= gap:
                boundary_seq = current.seq
        return boundary_seq

    async def fetch_prior_session_messages(
        self,
        conversation_id: UUID,
        *,
        limit: int,
        before_seq: int | None = None,
    ) -> list[Message[Fetched]]:
        """Return rows strictly before the live pi session's recovered boundary."""
        boundary_seq = await self.current_session_start_seq(conversation_id)
        if boundary_seq is None:
            return []
        effective_before_seq = (
            boundary_seq if before_seq is None else min(before_seq, boundary_seq)
        )
        return await self.fetch_messages(
            conversation_id,
            limit=limit,
            before_seq=effective_before_seq,
        )

    async def append_message(self, draft: MessageDraft) -> Message[Fetched]:
        """Idempotently append a settled row with a monotonic thread sequence."""

        async def _append(transaction: Transaction) -> Message[Fetched]:
            conversation = await transaction.fetch_one_or_none(
                select(Conversation).where(Conversation.id.eq(draft.conversation_id))
            )
            if conversation is None:
                raise ConversationNotFoundError(draft.conversation_id)
            if draft.pi_message_id is not None:
                existing = await transaction.fetch_one_or_none(
                    select(Message)
                    .where(Message.conversation_id.eq(draft.conversation_id))
                    .where(Message.pi_message_id.eq(draft.pi_message_id))
                )
                if existing is not None:
                    return existing
            latest = await transaction.fetch_one_or_none(
                select(Message)
                .where(Message.conversation_id.eq(draft.conversation_id))
                .order_by(Message.seq.desc())
                .limit(1)
            )
            return await transaction.execute(
                insert(
                    Message(
                        content=draft.content,
                        conversation_id=conversation.id,
                        pi_message_id=draft.pi_message_id,
                        role=draft.role,
                        seq=1 if latest is None else latest.seq + 1,
                        tool_args=(
                            json.dumps(draft.tool_args)
                            if draft.tool_args is not None
                            else None
                        ),
                        tool_name=draft.tool_name,
                        tool_result=(
                            json.dumps(draft.tool_result)
                            if draft.tool_result is not None
                            else None
                        ),
                    )
                ).returning()
            )

        async with self.database.transaction(mode="immediate") as transaction:
            return await _append(transaction)


__all__ = ["SESSION_GAP", "ConversationService"]
