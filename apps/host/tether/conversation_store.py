"""Canonical SQLite models and schema for host-owned conversations."""

from __future__ import annotations

from uuid import uuid7

from pydantic import UUID7, PositiveInt
from snekql.sqlite import (
    CurrentTimestamp,
    Database,
    Fetched,
    Integer,
    Model,
    Pending,
    Text,
    UtcDatetime,
)
from snekql.sqlite._schema_ddl import scaffold_sqlite_statements

from tether.conversation_model import MessageRole


class Conversation[S = Pending](Model[S, "Conversation[Fetched]"]):
    """A stable host-owned chat thread."""

    id: Conversation.GenCol[UUID7] = Text(primary_key=True, default_factory=uuid7)
    created_at: Conversation.GenCol[UtcDatetime] = Text(default=CurrentTimestamp)
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
    created_at: Message.GenCol[UtcDatetime] = Text(default=CurrentTimestamp)
    pi_message_id: Message.Col[str | None] = Text(default=None, nullable=True)
    tool_args: Message.Col[str | None] = Text(default=None, nullable=True)
    tool_name: Message.Col[str | None] = Text(default=None, nullable=True)
    tool_result: Message.Col[str | None] = Text(default=None, nullable=True)


async def create_conversation_schema(database: Database) -> None:
    """Create conversation and transcript tables on an initialized database."""
    migrations = {
        f"003_{label}": sql
        for label, sql in scaffold_sqlite_statements([Conversation, Message])
    }
    await database.migrate(migrations)


__all__ = ["Conversation", "Message", "create_conversation_schema"]
