"""Behaviour tests for the Readwise ingestion gate.

These drive `ReadwiseClient` and `ReadwiseSyncService` against a real in-memory
SQLite database and a real `MemoryService`, mocking only the HTTP boundary with a
scripted `FakeReadwiseTransport` — never a live Readwise call. They assert the
mapping between an export payload and the Commons: one machine-synced Memory per
highlight, content/facets shaping, the create/edit/delete state machine over the
`readwise_highlight` idempotency table, the full-then-incremental export request
shape, and the persisted watermark.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import structlog
from anyio import TemporaryDirectory
from opentelemetry import trace
from opentelemetry.trace import Tracer
from snekql.sqlite import Config, Database, Fetched
from snektest import (
    assert_eq,
    assert_false,
    assert_is_none,
    assert_is_not_none,
    assert_true,
    fixture,
    load_fixture,
    test,
)

from tether.logging import Logger
from tether.memories import (
    KnowledgeBaseService,
    Memory,
    MemoryService,
    create_memory_schema,
)
from tether.readwise import (
    ReadwiseClient,
    ReadwiseResponse,
    ReadwiseSyncService,
    create_readwise_schema,
)


def noop_tracer() -> Tracer:
    """A tracer that emits nowhere."""
    return trace.NoOpTracerProvider().get_tracer("test.readwise")


def test_logger() -> Logger:
    """A throwaway structured logger for the mandatory service logger arg."""
    return structlog.stdlib.get_logger("test.readwise")


async def _noop_sleep(_: float) -> None:
    """A sleep that returns at once, so `Retry-After` backoff tests don't wait."""


@dataclass
class ExportCall:
    """One recorded `fetch_export` invocation, for request-shape assertions."""

    updated_after: datetime | None
    page_cursor: str | None
    include_deleted: bool


@dataclass
class FakeReadwiseTransport:
    """A scripted `ReadwiseTransport`: returns queued responses, records calls.

    `export_responses` are handed out in order per `fetch_export` call (one page
    each), so a multi-pass test queues one page per sync. `auth_status` is what
    the token check returns.
    """

    export_responses: list[ReadwiseResponse]
    auth_status: int = 204
    export_calls: list[ExportCall] = field(default_factory=list[ExportCall])

    async def fetch_export(
        self,
        *,
        updated_after: datetime | None,
        page_cursor: str | None,
        include_deleted: bool,
    ) -> ReadwiseResponse:
        self.export_calls.append(
            ExportCall(
                updated_after=updated_after,
                page_cursor=page_cursor,
                include_deleted=include_deleted,
            )
        )
        return self.export_responses.pop(0)

    async def verify_token(self) -> ReadwiseResponse:
        return ReadwiseResponse(status_code=self.auth_status, payload={})


def highlight_payload(  # noqa: PLR0913 (a builder mirroring the export API's shape)
    highlight_id: int,
    text: str,
    *,
    note: str = "",
    tags: Sequence[str] = (),
    updated_at: str = "2026-01-01T00:00:00Z",
    is_discard: bool = False,
    is_deleted: bool = False,
) -> dict[str, object]:
    """Build one raw highlight mapping as the export API shapes it."""
    return {
        "id": highlight_id,
        "text": text,
        "note": note,
        "tags": [{"id": index, "name": name} for index, name in enumerate(tags)],
        "updated_at": updated_at,
        "is_discard": is_discard,
        "is_deleted": is_deleted,
    }


def book_payload(
    highlights: Sequence[dict[str, object]],
    *,
    readable_title: str = "A Book",
    author: str = "An Author",
    category: str = "books",
) -> dict[str, object]:
    """Build one raw book mapping with nested highlights."""
    return {
        "readable_title": readable_title,
        "author": author,
        "category": category,
        "highlights": list(highlights),
    }


def export_response(
    books: Sequence[dict[str, object]],
    *,
    next_page_cursor: str | None = None,
    status_code: int = 200,
    retry_after_seconds: int | None = None,
) -> ReadwiseResponse:
    """Build one export-page response wrapping the given books."""
    return ReadwiseResponse(
        status_code=status_code,
        payload={
            "count": len(books),
            "nextPageCursor": next_page_cursor,
            "results": list(books),
        },
        retry_after=(
            timedelta(seconds=retry_after_seconds)
            if retry_after_seconds is not None
            else None
        ),
    )


@dataclass
class ReadwiseEnv:
    """A Readwise-ready database plus a live `MemoryService` over a temp KB."""

    database: Database
    memory_service: MemoryService
    logger: Logger

    def sync_service(self, transport: FakeReadwiseTransport) -> ReadwiseSyncService:
        """Wire a sync service over a scripted transport with instant backoff."""
        return ReadwiseSyncService(
            database=self.database,
            client=ReadwiseClient(transport=transport, sleep=_noop_sleep),
            memory_service=self.memory_service,
        )

    async def tethered_memories(self) -> list[Memory[Fetched]]:
        """The current tethered corpus, for content/facet assertions."""
        return await self.memory_service.browse_by_state("tethered", logger=self.logger)


@fixture
async def readwise_env() -> AsyncGenerator[ReadwiseEnv]:
    """A fresh database with the Memory + Readwise schema and a live KB dir."""
    db = await Database.initialize(backend=Config(database=":memory:"))
    await create_memory_schema(db)
    await create_readwise_schema(db)
    async with TemporaryDirectory() as kb_root:
        yield ReadwiseEnv(
            database=db,
            memory_service=MemoryService(
                database=db,
                kb_service=KnowledgeBaseService(kb_root=Path(kb_root)),
                tracer=noop_tracer(),
            ),
            logger=test_logger(),
        )
    await db.close()


@test()
async def token_check_passes_on_a_204() -> None:
    """The token check reports valid only when auth returns 204."""
    client = ReadwiseClient(
        transport=FakeReadwiseTransport(export_responses=[], auth_status=204),
        sleep=_noop_sleep,
    )

    assert_true(await client.verify_token(logger=test_logger()))


@test()
async def token_check_fails_on_a_non_204() -> None:
    """A non-204 auth response fails the token check (worker would disable)."""
    client = ReadwiseClient(
        transport=FakeReadwiseTransport(export_responses=[], auth_status=401),
        sleep=_noop_sleep,
    )

    assert_false(await client.verify_token(logger=test_logger()))


@test()
async def export_follows_the_next_page_cursor() -> None:
    """Pagination walks every page until `nextPageCursor` is null."""
    transport = FakeReadwiseTransport(
        export_responses=[
            export_response(
                [book_payload([highlight_payload(1, "first")])],
                next_page_cursor="cursor-2",
            ),
            export_response([book_payload([highlight_payload(2, "second")])]),
        ]
    )
    client = ReadwiseClient(transport=transport, sleep=_noop_sleep)

    books = await client.fetch_export(
        updated_after=None, include_deleted=False, logger=test_logger()
    )

    assert_eq(len(books), 2)


@test()
async def export_retries_after_a_rate_limit() -> None:
    """A 429 is retried on its `Retry-After` hint, then the page is parsed."""
    transport = FakeReadwiseTransport(
        export_responses=[
            export_response([], status_code=429, retry_after_seconds=1),
            export_response([book_payload([highlight_payload(1, "after backoff")])]),
        ]
    )
    client = ReadwiseClient(transport=transport, sleep=_noop_sleep)

    books = await client.fetch_export(
        updated_after=None, include_deleted=False, logger=test_logger()
    )

    assert_eq(books[0].highlights[0].text, "after backoff")


@test()
async def first_sync_creates_one_memory_per_highlight() -> None:
    """A full backfill mirrors each highlight into its own tethered Memory."""
    env = await load_fixture(readwise_env())
    transport = FakeReadwiseTransport(
        export_responses=[
            export_response(
                [
                    book_payload(
                        [highlight_payload(1, "one"), highlight_payload(2, "two")]
                    )
                ]
            )
        ]
    )

    report = await env.sync_service(transport).sync(logger=env.logger)

    assert_eq(report.created, 2)
    assert_eq(len(await env.tethered_memories()), 2)


@test()
async def first_sync_requests_a_full_export() -> None:
    """The first pass sends no `updatedAfter` and does not include deletes."""
    env = await load_fixture(readwise_env())
    transport = FakeReadwiseTransport(
        export_responses=[export_response([book_payload([highlight_payload(1, "x")])])]
    )

    _ = await env.sync_service(transport).sync(logger=env.logger)

    assert_is_none(transport.export_calls[0].updated_after)
    assert_false(transport.export_calls[0].include_deleted)


@test()
async def a_note_is_appended_as_a_trailing_paragraph() -> None:
    """A highlight's note becomes a trailing `Note: …` paragraph on the Memory."""
    env = await load_fixture(readwise_env())
    transport = FakeReadwiseTransport(
        export_responses=[
            export_response(
                [book_payload([highlight_payload(1, "passage", note="my thought")])]
            )
        ]
    )

    _ = await env.sync_service(transport).sync(logger=env.logger)

    memory = (await env.tethered_memories())[0]
    assert_eq(memory.content, "passage\n\nNote: my thought")


@test()
async def book_and_tag_fields_map_to_facets() -> None:
    """Book metadata and highlight tags become the Commons facet set."""
    env = await load_fixture(readwise_env())
    transport = FakeReadwiseTransport(
        export_responses=[
            export_response(
                [
                    book_payload(
                        [highlight_payload(1, "passage", tags=["ml", "ai"])],
                        readable_title="Deep Work",
                        author="Cal Newport",
                        category="books",
                    )
                ]
            )
        ]
    )

    _ = await env.sync_service(transport).sync(logger=env.logger)

    memory = (await env.tethered_memories())[0]
    assert_eq(
        memory.facets,
        {
            "source": "readwise",
            "title": "Deep Work",
            "author": "Cal Newport",
            "category": "books",
            "tags": "ml, ai",
        },
    )


@test()
async def empty_book_fields_are_omitted_from_facets() -> None:
    """Blank author/category/tags leave no empty facet keys behind."""
    env = await load_fixture(readwise_env())
    transport = FakeReadwiseTransport(
        export_responses=[
            export_response(
                [
                    book_payload(
                        [highlight_payload(1, "passage")],
                        readable_title="Solo",
                        author="",
                        category="",
                    )
                ]
            )
        ]
    )

    _ = await env.sync_service(transport).sync(logger=env.logger)

    memory = (await env.tethered_memories())[0]
    assert_eq(memory.facets, {"source": "readwise", "title": "Solo"})


@test()
async def a_discarded_highlight_is_not_ingested() -> None:
    """A highlight flagged `is_discard` produces no Memory."""
    env = await load_fixture(readwise_env())
    transport = FakeReadwiseTransport(
        export_responses=[
            export_response(
                [book_payload([highlight_payload(1, "junk", is_discard=True)])]
            )
        ]
    )

    report = await env.sync_service(transport).sync(logger=env.logger)

    assert_eq(report.created, 0)
    assert_eq(await env.tethered_memories(), [])


@test()
async def synced_memory_carries_readwise_provenance() -> None:
    """Every ingested highlight lands with machine-synced Readwise provenance."""
    env = await load_fixture(readwise_env())
    transport = FakeReadwiseTransport(
        export_responses=[export_response([book_payload([highlight_payload(1, "x")])])]
    )

    _ = await env.sync_service(transport).sync(logger=env.logger)

    memory = (await env.tethered_memories())[0]
    assert_eq(memory.provenance, {"kind": "readwise"})


@test()
async def a_successful_pass_persists_the_watermark() -> None:
    """The next pass runs incrementally with `updatedAfter` + `includeDeleted`."""
    env = await load_fixture(readwise_env())
    transport = FakeReadwiseTransport(
        export_responses=[
            export_response([book_payload([highlight_payload(1, "x")])]),
            export_response([]),
        ]
    )
    service = env.sync_service(transport)

    _ = await service.sync(logger=env.logger)
    _ = await service.sync(logger=env.logger)

    assert_is_not_none(transport.export_calls[1].updated_after)
    assert_true(transport.export_calls[1].include_deleted)


@test()
async def an_edited_highlight_updates_the_memory_in_place() -> None:
    """A newer `updated_at` rewrites the mapped Memory rather than duplicating it."""
    env = await load_fixture(readwise_env())
    transport = FakeReadwiseTransport(
        export_responses=[
            export_response(
                [
                    book_payload(
                        [
                            highlight_payload(
                                1, "before", updated_at="2026-01-01T00:00:00Z"
                            )
                        ]
                    )
                ]
            ),
            export_response(
                [
                    book_payload(
                        [
                            highlight_payload(
                                1, "after", updated_at="2026-02-01T00:00:00Z"
                            )
                        ]
                    )
                ]
            ),
        ]
    )
    service = env.sync_service(transport)

    _ = await service.sync(logger=env.logger)
    report = await service.sync(logger=env.logger)

    memories = await env.tethered_memories()
    assert_eq(report.updated, 1)
    assert_eq([memory.content for memory in memories], ["after"])


@test()
async def an_unchanged_highlight_is_skipped_on_reexport() -> None:
    """A re-export at the same `updated_at` neither edits nor bumps the version."""
    env = await load_fixture(readwise_env())
    unchanged = book_payload(
        [highlight_payload(1, "stable", updated_at="2026-01-01T00:00:00Z")]
    )
    transport = FakeReadwiseTransport(
        export_responses=[export_response([unchanged]), export_response([unchanged])]
    )
    service = env.sync_service(transport)

    _ = await service.sync(logger=env.logger)
    report = await service.sync(logger=env.logger)

    memory = (await env.tethered_memories())[0]
    assert_eq(report.skipped, 1)
    assert_eq(memory.version, 1)


@test()
async def a_deleted_highlight_removes_the_memory() -> None:
    """An incremental `is_deleted` soft-deletes the Memory the highlight produced."""
    env = await load_fixture(readwise_env())
    transport = FakeReadwiseTransport(
        export_responses=[
            export_response([book_payload([highlight_payload(1, "doomed")])]),
            export_response(
                [
                    book_payload(
                        [
                            highlight_payload(
                                1,
                                "doomed",
                                updated_at="2026-03-01T00:00:00Z",
                                is_deleted=True,
                            )
                        ]
                    )
                ]
            ),
        ]
    )
    service = env.sync_service(transport)

    _ = await service.sync(logger=env.logger)
    report = await service.sync(logger=env.logger)

    assert_eq(report.deleted, 1)
    assert_eq(await env.tethered_memories(), [])


@test()
async def discarding_a_previously_ingested_highlight_removes_it() -> None:
    """A highlight later flagged `is_discard` is removed like a delete."""
    env = await load_fixture(readwise_env())
    transport = FakeReadwiseTransport(
        export_responses=[
            export_response([book_payload([highlight_payload(1, "kept")])]),
            export_response(
                [
                    book_payload(
                        [
                            highlight_payload(
                                1,
                                "kept",
                                updated_at="2026-03-01T00:00:00Z",
                                is_discard=True,
                            )
                        ]
                    )
                ]
            ),
        ]
    )
    service = env.sync_service(transport)

    _ = await service.sync(logger=env.logger)
    report = await service.sync(logger=env.logger)

    assert_eq(report.deleted, 1)
    assert_eq(await env.tethered_memories(), [])
