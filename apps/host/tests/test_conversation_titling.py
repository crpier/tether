"""Unit tests for first-message conversation auto-titling."""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field

from snekql.sqlite import Config, Database
from snektest import (
    assert_eq,
    assert_is_none,
    assert_true,
    fixture,
    load_fixture,
    test,
)
from structlog.stdlib import get_logger

from tether.conversation_store import create_conversation_schema
from tether.conversation_titling import ConversationTitler
from tether.conversations import ConversationService
from tether.events import InvalidateEvent


@dataclass(slots=True)
class StubTitleGenerator:
    """A `TitleGenerator` returning canned replies and recording prompts."""

    reply: str | None = "Vegetable garden planning"
    error: Exception | None = None
    prompts: list[str] = field(default_factory=list[str])

    async def generate_title(self, *, first_message: str) -> str:
        self.prompts.append(first_message)
        if self.error is not None:
            raise self.error
        assert self.reply is not None
        return self.reply


@fixture
async def titler_environment() -> AsyncGenerator[
    tuple[ConversationTitler, ConversationService, StubTitleGenerator, list[object]]
]:
    database = await Database.initialize(backend=Config(database=":memory:"))
    await create_conversation_schema(database)
    published: list[object] = []

    @dataclass(slots=True)
    class RecordingPublisher:
        events: list[object]

        async def publish(self, event: object) -> None:
            self.events.append(event)

    service = ConversationService(
        database,
        event_publisher=RecordingPublisher(published),
    )
    generator = StubTitleGenerator()
    titler = ConversationTitler(
        conversation_service=service,
        generator=generator,
        logger=get_logger("test"),
    )
    yield titler, service, generator, published
    await database.close()


@test()
async def the_first_message_titles_an_untitled_chat() -> None:
    """A generated title fills both presentation columns and invalidates."""
    titler, service, generator, published = await load_fixture(titler_environment())
    conversation = await service.create_scoped_conversation(
        scope_brief="Plan this year's vegetable garden.",
    )

    await titler.title_from_first_message(
        conversation.id,
        first_message="When should I start tomato seedlings indoors?",
    )

    stored = await service.fetch_conversation(conversation.id)
    assert_eq(stored.title, "Vegetable garden planning")
    assert_eq(stored.display_name, "Vegetable garden planning")
    assert_eq(generator.prompts, ["When should I start tomato seedlings indoors?"])
    invalidations = [event for event in published if isinstance(event, InvalidateEvent)]
    assert_eq(len(invalidations), 1)
    assert_eq(invalidations[0].keys, ["conversations"])


@test()
async def an_already_named_chat_is_never_regenerated() -> None:
    """Titling runs at most once per conversation."""
    titler, service, generator, _ = await load_fixture(titler_environment())
    conversation = await service.create_scoped_conversation(
        display_name="Garden planning",
        scope_brief="Plan this year's vegetable garden.",
    )

    await titler.title_from_first_message(
        conversation.id,
        first_message="When should I start tomato seedlings indoors?",
    )

    assert_eq(generator.prompts, [])
    stored = await service.fetch_conversation(conversation.id)
    assert_eq(stored.title, "Garden planning")


@test()
async def a_missing_conversation_is_ignored() -> None:
    """Deleted conversations cannot crash the fire-and-forget hook."""
    titler, _, generator, _ = await load_fixture(titler_environment())

    await titler.title_from_first_message(
        uuid.uuid7(),
        first_message="hello",
    )

    assert_eq(generator.prompts, [])


@test()
async def model_reply_whitespace_and_quotes_are_stripped() -> None:
    """The raw reply is cleaned into a plain presentation title."""
    titler, service, generator, _ = await load_fixture(titler_environment())
    generator.reply = '  "Vegetable\nGarden Planning" \n'
    conversation = await service.create_scoped_conversation(
        scope_brief="Plan this year's vegetable garden.",
    )

    await titler.title_from_first_message(
        conversation.id,
        first_message="tomatoes?",
    )

    stored = await service.fetch_conversation(conversation.id)
    assert_eq(stored.title, "Vegetable Garden Planning")


@test()
async def an_empty_reply_leaves_the_chat_untitled() -> None:
    """No usable reply means no title — a later message can retry."""
    titler, service, generator, published = await load_fixture(titler_environment())
    generator.reply = '   ""  '
    conversation = await service.create_scoped_conversation(
        scope_brief="Plan this year's vegetable garden.",
    )

    await titler.title_from_first_message(conversation.id, first_message="hi")

    stored = await service.fetch_conversation(conversation.id)
    assert_is_none(stored.title)
    assert_eq(published, [])
    assert_true(len(generator.prompts) == 1)


@test()
async def a_generator_failure_never_propagates() -> None:
    """Model errors are swallowed; the chat simply stays untitled."""
    titler, service, generator, _ = await load_fixture(titler_environment())
    generator.error = RuntimeError("provider down")
    conversation = await service.create_scoped_conversation(
        scope_brief="Plan this year's vegetable garden.",
    )

    await titler.title_from_first_message(conversation.id, first_message="hi")

    stored = await service.fetch_conversation(conversation.id)
    assert_is_none(stored.title)


@test()
async def long_replies_are_truncated_to_a_usable_label() -> None:
    """Titles are capped so sidebar labels stay readable."""
    titler, service, _, _ = await load_fixture(titler_environment())
    conversation = await service.create_scoped_conversation(
        scope_brief="Plan this year's vegetable garden.",
    )
    runaway = "Tomato Seedling Schedule For The Indoor Propagation Shelf And More"

    await titler.title_from_first_message(conversation.id, first_message=runaway)

    stored = await service.fetch_conversation(conversation.id)
    assert_true(stored.title is not None and len(stored.title) <= 60)
