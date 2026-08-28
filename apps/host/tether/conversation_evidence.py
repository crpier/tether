"""Conversation Message authority for durable Memory Claims."""

from collections.abc import Collection
from uuid import UUID

from snekql.sqlite import Database, Fetched, select

from tether.conversation_store import ConversationTurn, Message


async def fetch_claim_supporting_message_ids(
    database: Database,
    messages: Collection[Message[Fetched]],
) -> set[UUID]:
    """Return user Messages and succeeded turns' final assistant Messages.

    A failed turn may have persisted partial assistant prose before its failure.
    Selecting the last assistant Message only from succeeded turns keeps that
    partial output from gaining Memory authority.
    """
    supporting_ids: set[UUID] = {
        message.id for message in messages if message.role == "user"
    }
    assistant_turn_ids = {
        message.turn_id
        for message in messages
        if message.role == "assistant" and message.turn_id is not None
    }
    if not assistant_turn_ids:
        return supporting_ids

    async with database.transaction() as transaction:
        succeeded_turns = await transaction.fetch_all(
            select(ConversationTurn)
            .where(ConversationTurn.id.in_(*sorted(assistant_turn_ids)))
            .where(ConversationTurn.status.eq("succeeded"))
        )
        if not succeeded_turns:
            return supporting_ids
        succeeded_turn_ids = {turn.id for turn in succeeded_turns}
        assistant_messages = await transaction.fetch_all(
            select(Message)
            .where(Message.turn_id.in_(*sorted(succeeded_turn_ids)))
            .where(Message.role.eq("assistant"))
        )

    final_by_turn: dict[UUID, Message[Fetched]] = {}
    for message in assistant_messages:
        if message.turn_id is None:
            continue
        prior = final_by_turn.get(message.turn_id)
        if prior is None or (message.turn_message_seq or 0) > (
            prior.turn_message_seq or 0
        ):
            final_by_turn[message.turn_id] = message
    supporting_ids.update(message.id for message in final_by_turn.values())
    return supporting_ids


__all__ = ["fetch_claim_supporting_message_ids"]
