"""Service-layer tests for host-owned conversation session rotation.

These drive the `ConversationService` seam directly against a real in-memory
SQLite database — no HTTP, no pi — asserting on the observable session-boundary
behavior: a warm gap keeps the conversation's `pi_session_id`, a cold gap
rotates it to a fresh session while the transcript rows stay put.
"""

from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid7

from snekql.sqlite import Config, Database, insert, update
from snektest import (
    assert_eq,
    assert_in,
    assert_is_none,
    assert_raises,
    assert_true,
    fixture,
    load_fixture,
    test,
)

from tether.conversation_model import (
    ConversationArchiveBlockedError,
    ConversationNotFoundError,
    MessageDraft,
)
from tether.conversation_store import Message
from tether.conversations import ConversationService
from tether.host_schema import create_host_schema
from tether.trigger_store import ScheduledTrigger

GAP = timedelta(minutes=5)


@fixture
async def conversation_service() -> AsyncGenerator[ConversationService]:
    """A fresh, isolated conversation database for each test."""
    db = await Database.initialize(backend=Config(database=":memory:"))
    await create_host_schema(db)
    yield ConversationService(db)
    await db.close()


@test()
async def first_access_creates_the_permanent_main_conversation() -> None:
    """The singleton Main Conversation has explicit lifecycle state."""
    service = await load_fixture(conversation_service())

    conversations = await service.list_conversations()

    assert_eq(len(conversations), 1)
    assert_eq(conversations[0].kind, "main")
    assert_eq(conversations[0].status, "active")
    assert_eq(conversations[0].display_name, None)
    assert_eq(conversations[0].scope_brief, None)


@test()
async def create_scoped_conversation_persists_its_scope() -> None:
    """A named Scoped Conversation starts active with its first scope revision."""
    service = await load_fixture(conversation_service())

    conversation = await service.create_scoped_conversation(
        display_name="Garden planning",
        scope_brief="Plan this year's vegetable garden.",
    )

    assert_eq(conversation.kind, "scoped")
    assert_eq(conversation.status, "active")
    assert_eq(conversation.display_name, "Garden planning")
    assert_eq(conversation.scope_brief, "Plan this year's vegetable garden.")
    assert_eq(conversation.scope_revision, 1)


@test()
async def scoped_conversation_display_names_need_not_be_unique() -> None:
    """Conversation UUIDs, not presentation names, preserve identity."""
    service = await load_fixture(conversation_service())
    first = await service.create_scoped_conversation(
        display_name="Garden planning",
        scope_brief="Plan the vegetable garden.",
    )

    second = await service.create_scoped_conversation(
        display_name="Garden planning",
        scope_brief="Plan the flower garden.",
    )

    assert_true(first.id != second.id)
    assert_eq(first.display_name, second.display_name)


@test()
async def create_scoped_conversation_allows_an_untitled_chat() -> None:
    """A chat can start unnamed; auto-titling fills the name in later."""
    service = await load_fixture(conversation_service())

    conversation = await service.create_scoped_conversation(
        display_name=None,
        scope_brief="Plan this year's vegetable garden.",
    )

    assert_eq(conversation.kind, "scoped")
    assert_eq(conversation.status, "active")
    assert_is_none(conversation.display_name)
    assert_is_none(conversation.title)
    assert_eq(conversation.scope_brief, "Plan this year's vegetable garden.")


@test()
async def a_blank_display_name_creates_an_untitled_chat() -> None:
    """Whitespace stands in for 'no name yet', not for a validation error."""
    service = await load_fixture(conversation_service())

    conversation = await service.create_scoped_conversation(
        display_name="   ",
        scope_brief="Plan this year's vegetable garden.",
    )

    assert_is_none(conversation.display_name)
    assert_is_none(conversation.title)


@test()
async def set_generated_title_names_an_untitled_chat() -> None:
    """The first generated title fills both presentation columns."""
    service = await load_fixture(conversation_service())
    conversation = await service.create_scoped_conversation(
        scope_brief="Plan this year's vegetable garden.",
    )

    applied = await service.set_generated_title(
        conversation.id,
        title="Vegetable garden planning",
    )

    assert_true(applied)
    stored = await service.fetch_conversation(conversation.id)
    assert_eq(stored.title, "Vegetable garden planning")
    assert_eq(stored.display_name, "Vegetable garden planning")


@test()
async def set_generated_title_never_overrides_a_named_chat() -> None:
    """Once named — by generation or by hand — the title is stable."""
    service = await load_fixture(conversation_service())
    conversation = await service.create_scoped_conversation(
        display_name="Garden planning",
        scope_brief="Plan this year's vegetable garden.",
    )

    applied = await service.set_generated_title(
        conversation.id,
        title="Vegetable garden planning",
    )

    assert_true(not applied)
    stored = await service.fetch_conversation(conversation.id)
    assert_eq(stored.title, "Garden planning")


@test()
async def create_scoped_conversation_allows_an_empty_scope_brief() -> None:
    """A chat can start with neither name nor scope brief."""
    service = await load_fixture(conversation_service())

    conversation = await service.create_scoped_conversation()

    assert_eq(conversation.kind, "scoped")
    assert_eq(conversation.status, "active")
    assert_is_none(conversation.display_name)
    assert_is_none(conversation.title)
    assert_is_none(conversation.scope_brief)


@test()
async def create_scoped_conversation_rejects_a_blank_scope_brief() -> None:
    """Whitespace stands in for 'no scope yet', not for a validation error."""
    service = await load_fixture(conversation_service())

    conversation = await service.create_scoped_conversation(scope_brief="\n\t")

    assert_is_none(conversation.scope_brief)


@test()
async def editing_scope_increments_its_revision() -> None:
    """Each scope edit advances the revision used by later turns."""
    service = await load_fixture(conversation_service())
    conversation = await service.create_scoped_conversation(
        display_name="Garden planning",
        scope_brief="Plan this year's vegetable garden.",
    )

    updated = await service.update_scoped_conversation(
        conversation.id,
        scope_brief="Plan vegetables and irrigation.",
    )

    assert_eq(updated.scope_brief, "Plan vegetables and irrigation.")
    assert_eq(updated.scope_revision, 2)
    assert_eq(updated.pi_session_id, conversation.pi_session_id)


@test()
async def archiving_scoped_conversation_hides_it_and_rotates_its_session() -> None:
    """Archival keeps canonical state but removes Scoped Conversation navigation."""
    service = await load_fixture(conversation_service())
    conversation = await service.create_scoped_conversation(
        display_name="Garden planning",
        scope_brief="Plan this year's vegetable garden.",
    )

    archived = await service.archive_conversation(conversation.id)

    assert_eq(archived.status, "archived")
    assert_true(archived.pi_session_id != conversation.pi_session_id)
    assert_eq(
        [listed.id for listed in await service.list_conversations()],
        [(await service.fetch_main_conversation()).id],
    )


@test()
async def archive_is_blocked_by_a_nonterminal_conversation_turn_when_present() -> None:
    """Pending or running durable turns keep their target Conversation active."""
    service = await load_fixture(conversation_service())
    conversation = await service.create_scoped_conversation(
        display_name="Garden planning",
        scope_brief="Plan this year's vegetable garden.",
    )
    async with service.database.transaction(mode="immediate") as transaction:
        connection = transaction.require_connection()
        cursor = await connection.execute(
            'INSERT INTO "conversation_turn" ('
            '"id", "conversation_id", "origin", '
            '"scope_revision_snapshot", "status") '
            "VALUES (?, ?, 'interactive', 1, 'pending')",
            ("018f0000-0000-7000-8000-000000000099", str(conversation.id)),
        )
        await cursor.close()

    with assert_raises(ConversationArchiveBlockedError):
        _ = await service.archive_conversation(conversation.id)


@test()
async def archive_is_blocked_by_an_active_targeted_prompt_trigger_when_present() -> (
    None
):
    """An active Scheduled prompt keeps its target Conversation available."""
    service = await load_fixture(conversation_service())
    conversation = await service.create_scoped_conversation(
        display_name="Garden planning",
        scope_brief="Plan this year's vegetable garden.",
    )
    async with service.database.transaction(mode="immediate") as transaction:
        _ = await transaction.execute(
            insert(
                ScheduledTrigger(
                    recurrence="daily",
                    action_kind="prompt",
                    payload="plan the garden",
                    target_conversation_id=conversation.id,
                    timezone="UTC",
                    wall_time="09:00",
                    next_fire_at=datetime(2030, 1, 1, 9, tzinfo=UTC),
                    status="active",
                )
            )
        )

    with assert_raises(ConversationArchiveBlockedError):
        _ = await service.archive_conversation(conversation.id)


@test()
async def restoring_scoped_conversation_returns_it_to_active_navigation() -> None:
    """A restored Scoped Conversation keeps its canonical identity and scope."""
    service = await load_fixture(conversation_service())
    conversation = await service.create_scoped_conversation(
        display_name="Garden planning",
        scope_brief="Plan this year's vegetable garden.",
    )
    _ = await service.archive_conversation(conversation.id)

    restored = await service.restore_conversation(conversation.id)

    assert_eq(restored.status, "active")
    assert_eq(restored.archived_at, None)
    assert_in(restored.id, [item.id for item in await service.list_conversations()])


@test()
async def list_orders_scoped_conversations_by_latest_message_activity() -> None:
    """Main stays pinned while active Scoped Conversations follow Message recency."""
    service = await load_fixture(conversation_service())
    _ = await service.create_scoped_conversation(
        display_name="Older",
        scope_brief="An older active thread.",
    )
    newer = await service.create_scoped_conversation(
        display_name="Newer",
        scope_brief="A newer active thread.",
    )
    _ = await service.append_message(
        MessageDraft(content="latest", conversation_id=newer.id, role="user")
    )

    conversations = await service.list_conversations()

    assert_eq(
        [conversation.display_name for conversation in conversations],
        [None, "Newer", "Older"],
    )


@test()
async def marking_conversation_read_persists_latest_message_sequence() -> None:
    """Read position advances to the Conversation's current Message tail."""
    service = await load_fixture(conversation_service())
    conversation = await service.fetch_main_conversation()
    _ = await service.append_message(
        MessageDraft(content="one", conversation_id=conversation.id, role="user")
    )
    _ = await service.append_message(
        MessageDraft(content="two", conversation_id=conversation.id, role="assistant")
    )

    read = await service.mark_conversation_read(conversation.id)

    assert_eq(read.last_read_seq, 2)
    assert_eq((await service.fetch_conversation(conversation.id)).last_read_seq, 2)


@test()
async def marking_conversation_read_accepts_an_observed_message_sequence() -> None:
    """A client marks only the Message sequence it actually rendered."""
    service = await load_fixture(conversation_service())
    conversation = await service.fetch_main_conversation()
    _ = await service.append_message(
        MessageDraft(content="seen", conversation_id=conversation.id, role="assistant")
    )
    _ = await service.append_message(
        MessageDraft(content="raced", conversation_id=conversation.id, role="assistant")
    )

    read = await service.mark_conversation_read(conversation.id, last_read_seq=1)

    assert_eq(read.last_read_seq, 1)


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
async def fetch_messages_without_params_returns_full_ascending_history() -> None:
    """No params keeps the existing unbounded, ascending-seq behavior."""
    service = await load_fixture(conversation_service())
    conversation = (await service.list_conversations())[0]
    for content in ["one", "two", "three"]:
        _ = await service.append_message(
            MessageDraft(content=content, conversation_id=conversation.id, role="user")
        )

    full = await service.fetch_messages(conversation.id)

    assert_eq([message.content for message in full], ["one", "two", "three"])


@test()
async def fetch_messages_windows_to_the_latest_page_when_limit_is_given() -> None:
    """A `limit` returns only the newest rows, still in ascending seq order."""
    service = await load_fixture(conversation_service())
    conversation = (await service.list_conversations())[0]
    for content in ["one", "two", "three", "four"]:
        _ = await service.append_message(
            MessageDraft(content=content, conversation_id=conversation.id, role="user")
        )

    page = await service.fetch_messages(conversation.id, limit=2)

    assert_eq([message.content for message in page], ["three", "four"])


@test()
async def fetch_messages_before_seq_walks_backwards_through_history() -> None:
    """`before_seq` paired with `limit` returns the window just older than it."""
    service = await load_fixture(conversation_service())
    conversation = (await service.list_conversations())[0]
    for content in ["one", "two", "three", "four"]:
        _ = await service.append_message(
            MessageDraft(content=content, conversation_id=conversation.id, role="user")
        )

    page = await service.fetch_messages(conversation.id, limit=2, before_seq=3)

    assert_eq([message.content for message in page], ["one", "two"])


@test()
async def fetch_messages_before_seq_past_the_start_is_empty() -> None:
    """Requesting rows older than the first seq is an empty window, not an error."""
    service = await load_fixture(conversation_service())
    conversation = (await service.list_conversations())[0]
    _ = await service.append_message(
        MessageDraft(content="only", conversation_id=conversation.id, role="user")
    )

    page = await service.fetch_messages(conversation.id, limit=10, before_seq=1)

    assert_eq(page, [])


async def _age_rows(
    service: ConversationService, conversation_id: UUID, minutes: int
) -> None:
    """Push every existing row `minutes` into the past, opening a cold gap."""
    stale = (datetime.now(UTC) - timedelta(minutes=minutes)).replace(tzinfo=None)
    async with service.database.transaction() as tx:
        _ = await tx.execute(
            update(Message)
            .set(Message.created_at.to(stale))
            .where(Message.conversation_id.eq(conversation_id))
        )


@test()
async def current_session_start_seq_is_none_without_a_cold_gap() -> None:
    """A conversation that never went cold has no prior-session boundary."""
    service = await load_fixture(conversation_service())
    conversation = (await service.list_conversations())[0]
    for content in ["one", "two"]:
        _ = await service.append_message(
            MessageDraft(content=content, conversation_id=conversation.id, role="user")
        )

    boundary = await service.current_session_start_seq(conversation.id, gap=GAP)

    assert_is_none(boundary)


@test()
async def current_session_start_seq_finds_the_most_recent_cold_gap() -> None:
    """The boundary lands on the first row after the latest cold gap."""
    service = await load_fixture(conversation_service())
    conversation = (await service.list_conversations())[0]
    for content in ["one", "two"]:
        _ = await service.append_message(
            MessageDraft(content=content, conversation_id=conversation.id, role="user")
        )
    await _age_rows(service, conversation.id, 10)
    live = await service.append_message(
        MessageDraft(content="three", conversation_id=conversation.id, role="user")
    )

    boundary = await service.current_session_start_seq(conversation.id, gap=GAP)

    assert_eq(boundary, live.seq)


@test()
async def fetch_prior_session_messages_is_empty_without_a_cold_gap() -> None:
    """No rotation means nothing predates the live session."""
    service = await load_fixture(conversation_service())
    conversation = (await service.list_conversations())[0]
    _ = await service.append_message(
        MessageDraft(content="one", conversation_id=conversation.id, role="user")
    )

    prior = await service.fetch_prior_session_messages(conversation.id, limit=20)

    assert_eq(prior, [])


@test()
async def fetch_prior_session_messages_returns_only_rows_before_the_boundary() -> None:
    """Rows from the live session never come back, however large the limit."""
    service = await load_fixture(conversation_service())
    conversation = (await service.list_conversations())[0]
    for content in ["one", "two"]:
        _ = await service.append_message(
            MessageDraft(content=content, conversation_id=conversation.id, role="user")
        )
    await _age_rows(service, conversation.id, 10)
    for content in ["three", "four"]:
        _ = await service.append_message(
            MessageDraft(content=content, conversation_id=conversation.id, role="user")
        )

    prior = await service.fetch_prior_session_messages(conversation.id, limit=20)

    assert_eq([message.content for message in prior], ["one", "two"])


@test()
async def fetch_prior_session_messages_windows_to_the_newest_prior_rows() -> None:
    """`limit` windows to the newest prior-session rows, still ascending."""
    service = await load_fixture(conversation_service())
    conversation = (await service.list_conversations())[0]
    for content in ["one", "two", "three", "four"]:
        _ = await service.append_message(
            MessageDraft(content=content, conversation_id=conversation.id, role="user")
        )
    await _age_rows(service, conversation.id, 10)
    _ = await service.append_message(
        MessageDraft(content="live", conversation_id=conversation.id, role="user")
    )

    prior = await service.fetch_prior_session_messages(conversation.id, limit=2)

    assert_eq([message.content for message in prior], ["three", "four"])


@test()
async def fetch_prior_session_messages_before_cursor_walks_further_back() -> None:
    """A `before_seq` cursor clamped under the boundary pages further back."""
    service = await load_fixture(conversation_service())
    conversation = (await service.list_conversations())[0]
    for content in ["one", "two", "three", "four"]:
        _ = await service.append_message(
            MessageDraft(content=content, conversation_id=conversation.id, role="user")
        )
    await _age_rows(service, conversation.id, 10)
    _ = await service.append_message(
        MessageDraft(content="live", conversation_id=conversation.id, role="user")
    )
    newest = await service.fetch_prior_session_messages(conversation.id, limit=2)
    oldest_seq_seen = min(message.seq for message in newest)

    older = await service.fetch_prior_session_messages(
        conversation.id, limit=2, before_seq=oldest_seq_seen
    )

    assert_eq([message.content for message in older], ["one", "two"])


@test()
async def fetch_prior_session_messages_before_cursor_ignores_a_cursor_past_the_boundary() -> (
    None
):
    """A `before_seq` at/after the boundary never leaks the live session."""
    service = await load_fixture(conversation_service())
    conversation = (await service.list_conversations())[0]
    _ = await service.append_message(
        MessageDraft(content="one", conversation_id=conversation.id, role="user")
    )
    await _age_rows(service, conversation.id, 10)
    live = await service.append_message(
        MessageDraft(content="two", conversation_id=conversation.id, role="user")
    )

    prior = await service.fetch_prior_session_messages(
        conversation.id, limit=20, before_seq=live.seq + 100
    )

    assert_eq([message.content for message in prior], ["one"])


@test()
async def fetch_conversation_by_pi_session_id_resolves_the_live_conversation() -> None:
    """The tool auth seam's only lookup: pi session id -> host conversation."""
    service = await load_fixture(conversation_service())
    conversation = (await service.list_conversations())[0]

    found = await service.fetch_conversation_by_pi_session_id(
        conversation.pi_session_id
    )

    assert_eq(found.id, conversation.id)


@test()
async def fetch_conversation_by_pi_session_id_raises_for_an_unknown_session() -> None:
    """An id with no matching conversation raises, not a silent None."""
    service = await load_fixture(conversation_service())
    _ = (await service.list_conversations())[0]
    unknown = uuid7()

    with assert_raises(ConversationNotFoundError):
        _ = await service.fetch_conversation_by_pi_session_id(unknown)
