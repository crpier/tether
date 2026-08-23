"""Conversation lifecycle and settled-transcript orchestration."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from uuid import UUID, uuid7

from snekql.sqlite import (
    Assignment,
    CurrentTimestamp,
    Database,
    Fetched,
    Pending,
    Transaction,
    insert,
    select,
    update,
)

from tether.conversation_model import (
    ConversationArchiveBlockedError,
    ConversationArchiveBlocker,
    ConversationNotFoundError,
    ConversationValidationError,
    MessageDraft,
)
from tether.conversation_store import Conversation, ConversationTurn, Message
from tether.events import EventPublisher, InvalidateEvent, NullEventPublisher
from tether.model_selection import (
    AgentModelCatalog,
    AgentModelConfig,
    ModelNotAllowedError,
)
from tether.trigger_store import ScheduledTrigger

SESSION_GAP = timedelta(minutes=5)
"""Idle window after which a new turn rotates onto a fresh pi session."""


@dataclass(frozen=True, slots=True)
class ConversationActivity:
    """Durable transcript and execution state used by Conversation navigation."""

    latest_activity: datetime | None
    latest_message_seq: int
    pending_turn_count: int
    running_turn_id: UUID | None


def _as_utc(value: datetime) -> datetime:
    """Read a stored timestamp as UTC-aware; SQLite timestamps may be naive."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


class ConversationService:
    """Coordinate conversation sessions and canonical transcript persistence."""

    def __init__(
        self,
        database: Database,
        *,
        event_publisher: EventPublisher | None = None,
        model_catalog: AgentModelCatalog | None = None,
    ) -> None:
        self.database: Database = database
        self.event_publisher: EventPublisher = event_publisher or NullEventPublisher()
        self.model_catalog: AgentModelCatalog = model_catalog or AgentModelCatalog(
            default_model=None,
            models=(),
        )

    async def publish_navigation_state(self, *, include_messages: bool = False) -> None:
        """Refresh browser read models after a durable turn lifecycle change."""
        await self.event_publisher.publish(
            InvalidateEvent(
                keys=["conversations", "messages"]
                if include_messages
                else ["conversations"]
            )
        )

    async def list_conversations(
        self,
        *,
        include_archived: bool = False,
    ) -> list[Conversation[Fetched]]:
        """List Main first and omit archived Scoped Conversations by default."""

        async def _list(transaction: Transaction) -> list[Conversation[Fetched]]:
            conversations = await transaction.fetch_all(
                select(Conversation).all().order_by(Conversation.created_at.asc())
            )
            if not any(item.kind == "main" for item in conversations):
                conversations.append(
                    await transaction.execute(
                        insert(
                            Conversation(
                                kind="main",
                                selected_model=self.model_catalog.default_model,
                            )
                        ).returning()
                    )
                )
            visible = (
                conversations
                if include_archived
                else [item for item in conversations if item.status == "active"]
            )
            messages = await transaction.fetch_all(select(Message).all())
            latest_activity_by_conversation: dict[UUID, datetime] = {}
            for message in messages:
                current = latest_activity_by_conversation.get(message.conversation_id)
                if current is None or _as_utc(message.created_at) > _as_utc(current):
                    latest_activity_by_conversation[message.conversation_id] = (
                        message.created_at
                    )
            main = [item for item in visible if item.kind == "main"]
            scoped = [item for item in visible if item.kind == "scoped"]
            scoped.sort(
                key=lambda item: (
                    _as_utc(latest_activity_by_conversation[item.id]).timestamp()
                    if item.id in latest_activity_by_conversation
                    else 0.0,
                    _as_utc(item.created_at).timestamp(),
                ),
                reverse=True,
            )
            return [*main, *scoped]

        async with self.database.transaction(mode="immediate") as transaction:
            return await _list(transaction)

    async def fetch_main_conversation(self) -> Conversation[Fetched]:
        """Return the permanent Main Conversation, creating it on first access."""
        conversations = await self.list_conversations(include_archived=True)
        return next(item for item in conversations if item.kind == "main")

    async def create_scoped_conversation(
        self,
        *,
        display_name: str | None = None,
        scope_brief: str,
    ) -> Conversation[Fetched]:
        """Create one active Scoped Conversation with its own model profile.

        `display_name` may be `None` (or blank): the chat then starts untitled
        and is named later by first-message auto-titling.
        """
        if display_name is not None:
            display_name = display_name.strip() or None
        scope_brief = scope_brief.strip()
        if not scope_brief:
            message = "scope brief must not be blank"
            raise ConversationValidationError(message)
        _ = await self.fetch_main_conversation()
        async with self.database.transaction(mode="immediate") as transaction:
            return await transaction.execute(
                insert(
                    Conversation(
                        display_name=display_name,
                        kind="scoped",
                        scope_brief=scope_brief,
                        selected_model=self.model_catalog.default_model,
                        title=display_name,
                    )
                ).returning()
            )

    async def set_generated_title(
        self,
        conversation_id: UUID,
        *,
        title: str,
    ) -> bool:
        """Name an untitled chat; never override an existing name.

        Returns whether the generated title was applied.
        """
        title = title.strip()
        if not title:
            return False
        async with self.database.transaction(mode="immediate") as transaction:
            conversation = await transaction.fetch_one_or_none(
                select(Conversation).where(Conversation.id.eq(conversation_id))
            )
            if conversation is None or conversation.title is not None:
                return False
            _ = await transaction.execute(
                update(Conversation)
                .set(
                    Conversation.title.to(title),
                    Conversation.display_name.to(title),
                )
                .where(Conversation.id.eq(conversation_id))
            )
            return True

    async def update_scoped_conversation(
        self,
        conversation_id: UUID,
        *,
        display_name: str | None = None,
        scope_brief: str | None = None,
    ) -> Conversation[Fetched]:
        """Edit Scoped Conversation presentation or scope state."""
        if display_name is not None:
            display_name = display_name.strip()
            if not display_name:
                message = "display name must not be blank"
                raise ConversationValidationError(message)
        if scope_brief is not None:
            scope_brief = scope_brief.strip()
            if not scope_brief:
                message = "scope brief must not be blank"
                raise ConversationValidationError(message)
        async with self.database.transaction(mode="immediate") as transaction:
            conversation = await transaction.fetch_one_or_none(
                select(Conversation).where(Conversation.id.eq(conversation_id))
            )
            if conversation is None:
                raise ConversationNotFoundError(conversation_id)
            if conversation.kind != "scoped":
                message = "Main Conversation cannot be edited"
                raise ConversationValidationError(message)
            assignments: list[Assignment[Conversation[Pending]]] = []
            if display_name is not None and display_name != conversation.display_name:
                assignments.extend(
                    [
                        Conversation.display_name.to(display_name),
                        Conversation.title.to(display_name),
                    ]
                )
            if scope_brief is not None and scope_brief != conversation.scope_brief:
                assignments.extend(
                    [
                        Conversation.scope_brief.to(scope_brief),
                        Conversation.scope_revision.to(conversation.scope_revision + 1),
                    ]
                )
            if assignments:
                _ = await transaction.execute(
                    update(Conversation)
                    .set(*assignments)
                    .where(Conversation.id.eq(conversation_id))
                )
                conversation = await transaction.fetch_one(
                    select(Conversation).where(Conversation.id.eq(conversation_id))
                )
            return conversation

    async def archive_conversation(
        self,
        conversation_id: UUID,
    ) -> Conversation[Fetched]:
        """Archive a Scoped Conversation and rotate its disposable pi session."""
        async with self.database.transaction(mode="immediate") as transaction:
            conversation = await transaction.fetch_one_or_none(
                select(Conversation).where(Conversation.id.eq(conversation_id))
            )
            if conversation is None:
                raise ConversationNotFoundError(conversation_id)
            if conversation.kind == "main":
                message = "Main Conversation cannot be archived"
                raise ConversationValidationError(message)
            blocker = await self._fetch_archive_blocker(
                transaction,
                conversation_id,
            )
            if blocker is not None:
                raise ConversationArchiveBlockedError(blocker)
            _ = await transaction.execute(
                update(Conversation)
                .set(
                    Conversation.archived_at.to(CurrentTimestamp),
                    Conversation.pi_session_id.to(uuid7()),
                    Conversation.status.to("archived"),
                )
                .where(Conversation.id.eq(conversation_id))
            )
            return await transaction.fetch_one(
                select(Conversation).where(Conversation.id.eq(conversation_id))
            )

    async def restore_conversation(
        self,
        conversation_id: UUID,
    ) -> Conversation[Fetched]:
        """Restore an archived Scoped Conversation to active navigation."""
        async with self.database.transaction(mode="immediate") as transaction:
            conversation = await transaction.fetch_one_or_none(
                select(Conversation).where(Conversation.id.eq(conversation_id))
            )
            if conversation is None:
                raise ConversationNotFoundError(conversation_id)
            if conversation.kind == "main":
                message = "Main Conversation cannot be restored"
                raise ConversationValidationError(message)
            _ = await transaction.execute(
                update(Conversation)
                .set(
                    Conversation.archived_at.to(None),
                    Conversation.status.to("active"),
                )
                .where(Conversation.id.eq(conversation_id))
            )
            return await transaction.fetch_one(
                select(Conversation).where(Conversation.id.eq(conversation_id))
            )

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

    async def mark_conversation_read(
        self,
        conversation_id: UUID,
        *,
        last_read_seq: int | None = None,
    ) -> Conversation[Fetched]:
        """Advance durable read position to an observed or current Message tail."""
        async with self.database.transaction(mode="immediate") as transaction:
            conversation = await transaction.fetch_one_or_none(
                select(Conversation).where(Conversation.id.eq(conversation_id))
            )
            if conversation is None:
                raise ConversationNotFoundError(conversation_id)
            latest = await transaction.fetch_one_or_none(
                select(Message)
                .where(Message.conversation_id.eq(conversation_id))
                .order_by(Message.seq.desc())
                .limit(1)
            )
            latest_seq = 0 if latest is None else latest.seq
            if last_read_seq is not None and (
                last_read_seq < 0 or last_read_seq > latest_seq
            ):
                message = "last read sequence is outside the Conversation transcript"
                raise ConversationValidationError(message)
            target_seq = latest_seq if last_read_seq is None else last_read_seq
            _ = await transaction.execute(
                update(Conversation)
                .set(
                    Conversation.last_read_seq.to(
                        max(conversation.last_read_seq, target_seq)
                    )
                )
                .where(Conversation.id.eq(conversation_id))
            )
            return await transaction.fetch_one(
                select(Conversation).where(Conversation.id.eq(conversation_id))
            )

    async def activate_turn_scope(
        self,
        conversation: Conversation[Fetched],
        *,
        scope_revision: int,
    ) -> Conversation[Fetched]:
        """Rotate pi state exactly when FIFO execution crosses a scope revision."""
        if conversation.runtime_scope_revision == scope_revision:
            return conversation
        async with self.database.transaction(mode="immediate") as transaction:
            _ = await transaction.execute(
                update(Conversation)
                .set(
                    Conversation.pi_session_id.to(uuid7()),
                    Conversation.runtime_scope_revision.to(scope_revision),
                )
                .where(Conversation.id.eq(conversation.id))
            )
            return await transaction.fetch_one(
                select(Conversation).where(Conversation.id.eq(conversation.id))
            )

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

    async def conversation_activity(
        self,
        conversation_id: UUID,
    ) -> ConversationActivity:
        """Derive transcript tail and nonterminal work from durable rows."""
        async with self.database.transaction() as transaction:
            latest = await transaction.fetch_one_or_none(
                select(Message)
                .where(Message.conversation_id.eq(conversation_id))
                .order_by(Message.seq.desc())
                .limit(1)
            )
            pending = await transaction.fetch_all(
                select(ConversationTurn)
                .where(ConversationTurn.conversation_id.eq(conversation_id))
                .where(ConversationTurn.status.eq("pending"))
            )
            running = await transaction.fetch_one_or_none(
                select(ConversationTurn)
                .where(ConversationTurn.conversation_id.eq(conversation_id))
                .where(ConversationTurn.status.eq("running"))
                .order_by(ConversationTurn.created_at.asc())
                .limit(1)
            )
        return ConversationActivity(
            latest_activity=None if latest is None else latest.created_at,
            latest_message_seq=0 if latest is None else latest.seq,
            pending_turn_count=len(pending),
            running_turn_id=None if running is None else running.id,
        )

    async def latest_activity(self, conversation_id: UUID) -> datetime | None:
        """Return when the most recent transcript row landed, if any."""
        return (await self.conversation_activity(conversation_id)).latest_activity

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

    async def fetch_messages(
        self,
        conversation_id: UUID,
        *,
        limit: int | None = None,
        before_seq: int | None = None,
        turn_id: UUID | None = None,
    ) -> list[Message[Fetched]]:
        """Return a full transcript or an ascending window of its newest rows."""
        async with self.database.transaction() as transaction:
            conversation = await transaction.fetch_one_or_none(
                select(Conversation).where(Conversation.id.eq(conversation_id))
            )
            if conversation is None:
                raise ConversationNotFoundError(conversation_id)
            query = select(Message).where(Message.conversation_id.eq(conversation_id))
            if turn_id is not None:
                query = query.where(Message.turn_id.eq(turn_id))
            if before_seq is not None:
                query = query.where(Message.seq.lt(before_seq))
            if limit is None:
                return await transaction.fetch_all(query.order_by(Message.seq.asc()))
            page = await transaction.fetch_all(
                query.order_by(Message.seq.desc()).limit(limit)
            )
            return list(reversed(page))

    async def fetch_user_message(
        self,
        conversation_id: UUID,
        message_id: UUID,
    ) -> Message[Fetched] | None:
        """Return one user Message only when it belongs to the Conversation."""
        async with self.database.transaction() as transaction:
            return await transaction.fetch_one_or_none(
                select(Message)
                .where(Message.id.eq(message_id))
                .where(Message.conversation_id.eq(conversation_id))
                .where(Message.role.eq("user"))
            )

    async def fetch_turn_user_message(
        self,
        *,
        conversation_id: UUID,
        turn_id: UUID,
    ) -> Message[Fetched] | None:
        """Return the initiating user Message owned by one exact turn."""
        async with self.database.transaction() as transaction:
            return await transaction.fetch_one_or_none(
                select(Message)
                .where(Message.conversation_id.eq(conversation_id))
                .where(Message.turn_id.eq(turn_id))
                .where(Message.role.eq("user"))
                .order_by(Message.turn_message_seq.asc())
                .limit(1)
            )

    async def fetch_latest_user_message(
        self, conversation_id: UUID
    ) -> Message[Fetched] | None:
        """Return the newest user Message in a Conversation, if one exists."""
        async with self.database.transaction() as transaction:
            return await transaction.fetch_one_or_none(
                select(Message)
                .where(Message.conversation_id.eq(conversation_id))
                .where(Message.role.eq("user"))
                .order_by(Message.seq.desc())
                .limit(1)
            )

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
        async with self.database.transaction(mode="immediate") as transaction:
            return await self._append_message(transaction, draft)

    async def append_initiating_message(
        self,
        transaction: Transaction,
        draft: MessageDraft,
    ) -> Message[Fetched]:
        """Append one turn's initiating Message exactly once in its claim transaction."""
        if draft.turn_id is None or draft.role not in {"user", "scheduled"}:
            message = "an initiating Message requires a turn and initiating role"
            raise ConversationValidationError(message)
        existing = await transaction.fetch_one_or_none(
            select(Message)
            .where(Message.turn_id.eq(draft.turn_id))
            .where(Message.role.in_("user", "scheduled"))
        )
        if existing is not None:
            return existing
        return await self._append_message(transaction, draft)

    async def _append_message(
        self,
        transaction: Transaction,
        draft: MessageDraft,
    ) -> Message[Fetched]:
        """Allocate both Conversation and turn Message sequences under one write lock."""
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
        turn_message_seq = draft.turn_message_seq
        if draft.turn_id is not None and turn_message_seq is None:
            latest_turn_message = await transaction.fetch_one_or_none(
                select(Message)
                .where(Message.turn_id.eq(draft.turn_id))
                .order_by(Message.turn_message_seq.desc())
                .limit(1)
            )
            turn_message_seq = (
                1
                if latest_turn_message is None
                else (latest_turn_message.turn_message_seq or 0) + 1
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
                    turn_id=draft.turn_id,
                    turn_message_seq=turn_message_seq,
                )
            ).returning()
        )

    async def _fetch_archive_blocker(
        self,
        transaction: Transaction,
        conversation_id: UUID,
    ) -> ConversationArchiveBlocker | None:
        """Check durable turn and Scheduled-trigger lifecycle dependencies."""
        nonterminal_turn = await transaction.fetch_one_or_none(
            select(ConversationTurn)
            .where(ConversationTurn.conversation_id.eq(conversation_id))
            .where(ConversationTurn.status.in_("pending", "running"))
            .limit(1)
        )
        if nonterminal_turn is not None:
            return "nonterminal_turn"
        active_prompt_trigger = await transaction.fetch_one_or_none(
            select(ScheduledTrigger)
            .where(ScheduledTrigger.target_conversation_id.eq(conversation_id))
            .where(ScheduledTrigger.action_kind.eq("prompt"))
            .where(ScheduledTrigger.status.eq("active"))
            .where(ScheduledTrigger.deleted_at.is_null())
            .limit(1)
        )
        if active_prompt_trigger is not None:
            return "active_prompt_trigger"
        return None


__all__ = ["SESSION_GAP", "ConversationActivity", "ConversationService"]
