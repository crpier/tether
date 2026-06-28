"""Behaviour tests for conversation-import provider parsers.

These drive each parser against a small redacted fixture distilled from a real
export and assert on the normalised shape it produces — the conversations, their
ordered turns, dropped noise, and preserved provenance — never on parser
internals. The fixture deliberately carries the edge cases a real export does:
out-of-order messages, empty/incomplete turns, an all-empty thread, and a
message orphaned from any thread.
"""

from datetime import UTC, datetime
from pathlib import Path

from snektest import assert_eq, assert_raises, test

from tether.conversation_import import (
    PARSERS,
    ConversationParseError,
    ConversationParser,
    T3ChatParser,
)

_FIXTURES = Path(__file__).parent / "fixtures" / "conversation_import"
_RECIPE_THREAD_ID = "264c0550-f4b2-445d-a71c-6e951e1fed60"


def _t3chat_export() -> str:
    """The redacted t3chat export fixture as raw text."""
    return (_FIXTURES / "t3chat_export.json").read_text()


@test()
def t3chat_drops_empty_and_orphan_conversations() -> None:
    """Only threads with at least one usable turn survive parsing.

    The fixture's blank thread (all-empty messages) and the orphan message
    (threadId naming no exported thread) yield nothing, leaving one conversation.
    """
    conversations = T3ChatParser().parse(_t3chat_export())

    assert_eq(len(conversations), 1)
    assert_eq(conversations[0].source_conversation_id, _RECIPE_THREAD_ID)


@test()
def t3chat_tags_source_and_title() -> None:
    """The conversation carries its provider source and the thread title."""
    conversation = T3ChatParser().parse(_t3chat_export())[0]

    assert_eq(conversation.source, "t3chat")
    assert_eq(conversation.title, "Recipe ideas")


@test()
def t3chat_orders_turns_oldest_first() -> None:
    """Turns are ordered by capture time despite the flat list being unordered.

    The fixture lists the assistant reply before the user prompt; parsing must
    restore user-then-assistant order.
    """
    conversation = T3ChatParser().parse(_t3chat_export())[0]

    roles = [message.role for message in conversation.messages]
    assert_eq(roles, ["user", "assistant"])


@test()
def t3chat_skips_blank_turns_and_strips_content() -> None:
    """Empty and still-generating turns are dropped; content is whitespace-trimmed.

    The recipe thread has a blank `done` turn and an empty `waiting` turn beyond
    its two real turns; only the two real turns survive, trimmed.
    """
    conversation = T3ChatParser().parse(_t3chat_export())[0]

    assert_eq(len(conversation.messages), 2)
    assert_eq(
        conversation.messages[0].content,
        "What can I cook with potatoes and carrots?",
    )
    assert_eq(
        conversation.messages[1].content,
        "Try a sheet-pan dinner: roast the vegetables together.",
    )


@test()
def t3chat_preserves_message_ids_and_timestamps() -> None:
    """Provenance survives: per-turn source ids and epoch-ms timestamps convert."""
    conversation = T3ChatParser().parse(_t3chat_export())[0]

    assert_eq(
        conversation.messages[0].source_message_id,
        "a1aaaaaa-0000-0000-0000-000000000001",
    )
    assert_eq(
        conversation.created_at,
        datetime.fromtimestamp(1737934227856 / 1000, tz=UTC),
    )
    assert_eq(
        conversation.messages[0].created_at,
        datetime.fromtimestamp(1737934227900 / 1000, tz=UTC),
    )


@test()
def t3chat_rejects_non_json() -> None:
    """A non-JSON export is a parse failure, not an empty result."""
    with assert_raises(ConversationParseError):
        _ = T3ChatParser().parse("not json at all")


@test()
def t3chat_rejects_wrong_envelope() -> None:
    """An export missing the threads/messages arrays is a parse failure."""
    with assert_raises(ConversationParseError):
        _ = T3ChatParser().parse('{"version": "10.63.3"}')


@test()
def t3chat_handles_empty_export() -> None:
    """A well-formed but empty export yields no conversations, without raising."""
    conversations = T3ChatParser().parse('{"threads": [], "messages": []}')

    assert_eq(conversations, [])


@test()
def registry_exposes_t3chat_parser() -> None:
    """The provider registry dispatches t3chat to a conforming parser."""
    parser = PARSERS["t3chat"]

    assert isinstance(parser, ConversationParser)
    assert_eq(parser.source, "t3chat")
