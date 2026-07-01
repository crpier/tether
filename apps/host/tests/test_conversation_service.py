"""Service-layer tests for host-owned conversation session rotation.

These drive the `ConversationService` seam directly against a real in-memory
SQLite database — no HTTP, no pi — asserting on the observable session-boundary
behavior: a warm gap keeps the conversation's `pi_session_id`, a cold gap
rotates it to a fresh session while the transcript rows stay put.
"""

from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

from snekql.sqlite import Config, Database
from snektest import assert_eq, assert_true, fixture, load_fixture, test

from tether.conversations import (
    ConversationService,
    MessageDraft,
    create_conversation_schema,
)

GAP = timedelta(minutes=5)


@fixture
async def conversation_service() -> AsyncGenerator[ConversationService]:
    """A fresh, isolated conversation database for each test."""
    db = await Database.initialize(backend=Config(database=":memory:"))
    await create_conversation_schema(db)
    yield ConversationService(db)
    await db.close()


@test()
async def resolve_session_keeps_pi_session_within_the_gap() -> None:
    """A message arriving inside the gap reuses the live pi session."""
    service = await load_fixture(conversation_service())
    conversation = (await service.list_conversations())[0]
    _ = await service.append_message(
        MessageDraft(
            content="hi",
            conversation_id=conversation.id,
            role="assistant",
        )
    )

    resolved = await service.resolve_session(
        conversation, now=datetime.now(UTC), gap=GAP
    )

    assert_eq(resolved.id, conversation.id)
    assert_eq(resolved.pi_session_id, conversation.pi_session_id)


@test()
async def resolve_session_rotates_pi_session_after_the_gap() -> None:
    """A message arriving past the gap rotates to a fresh pi session."""
    service = await load_fixture(conversation_service())
    conversation = (await service.list_conversations())[0]
    _ = await service.append_message(
        MessageDraft(
            content="earlier topic",
            conversation_id=conversation.id,
            role="assistant",
        )
    )

    resolved = await service.resolve_session(
        conversation, now=datetime.now(UTC) + timedelta(minutes=10), gap=GAP
    )

    assert_eq(resolved.id, conversation.id)
    assert_true(resolved.pi_session_id != conversation.pi_session_id)
    preserved = await service.fetch_messages(conversation.id)
    assert_eq([message.content for message in preserved], ["earlier topic"])


@test()
async def resolve_session_keeps_pi_session_when_never_used() -> None:
    """A conversation with no prior turns has nothing stale to abandon."""
    service = await load_fixture(conversation_service())
    conversation = (await service.list_conversations())[0]

    resolved = await service.resolve_session(
        conversation, now=datetime.now(UTC) + timedelta(hours=1), gap=GAP
    )

    assert_eq(resolved.pi_session_id, conversation.pi_session_id)


@test()
async def clear_conversation_drops_history_and_rotates_session() -> None:
    """Clearing empties the transcript and starts a fresh pi session."""
    service = await load_fixture(conversation_service())
    conversation = (await service.list_conversations())[0]
    _ = await service.append_message(
        MessageDraft(
            content="old topic",
            conversation_id=conversation.id,
            role="user",
        )
    )

    cleared = await service.clear_conversation(conversation.id)

    assert_eq(cleared.id, conversation.id)
    assert_true(cleared.pi_session_id != conversation.pi_session_id)
    assert_eq(await service.fetch_messages(conversation.id), [])


@test()
async def clear_conversation_resets_the_sequence_counter() -> None:
    """After clearing, the next appended row starts the sequence over at 1."""
    service = await load_fixture(conversation_service())
    conversation = (await service.list_conversations())[0]
    _ = await service.append_message(
        MessageDraft(content="one", conversation_id=conversation.id, role="user")
    )
    _ = await service.append_message(
        MessageDraft(content="two", conversation_id=conversation.id, role="assistant")
    )

    _ = await service.clear_conversation(conversation.id)
    appended = await service.append_message(
        MessageDraft(content="fresh", conversation_id=conversation.id, role="user")
    )

    assert_eq(appended.seq, 1)
