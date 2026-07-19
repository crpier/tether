"""Behaviour tests for the Readwise Reader v3 progress rider.

These drive `ReaderClient` and `ReaderSyncService` against a real in-memory
SQLite database and a real `MemoryService`, mocking only the HTTP boundary with a
scripted `FakeReaderTransport` — never a live Reader call. They assert the
per-category pagination, the append-dedupe over the shared `ebook_progress_event`
Telemetry table, the document upsert with the API title, the finished-book
derivation (archive or `>= 0.98`, machine-synced, faceted, once ever), and the
full-then-incremental watermark.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

import structlog
from anyio import TemporaryDirectory
from opentelemetry import trace
from opentelemetry.trace import Tracer
from snekql.sqlite import Config, Database, Fetched, select
from snektest import (
    assert_eq,
    assert_is_none,
    assert_is_not_none,
    assert_true,
    fixture,
    load_fixture,
    test,
)

from tether.kosync import EbookDocument, EbookProgressEvent, create_kosync_schema
from tether.logging import Logger
from tether.memories import (
    KnowledgeBaseService,
    Memory,
    MemoryService,
    create_memory_schema,
)
from tether.readwise import (
    ReaderClient,
    ReaderSyncService,
    ReadwiseResponse,
    create_readwise_schema,
)


def noop_tracer() -> Tracer:
    """A tracer that emits nowhere."""
    return trace.NoOpTracerProvider().get_tracer("test.reader")


def test_logger() -> Logger:
    """A throwaway structured logger for the mandatory service logger arg."""
    return structlog.stdlib.get_logger("test.reader")


async def _noop_sleep(_: float) -> None:
    """A sleep that returns at once, so `Retry-After` backoff tests don't wait."""


@dataclass
class ListCall:
    """One recorded `fetch_list` invocation, for request-shape assertions."""

    updated_after: object
    category: str
    page_cursor: str | None


@dataclass
class FakeReaderTransport:
    """A scripted `ReaderTransport`: queued pages per category, records calls.

    `pages` maps a category to the response pages handed out in order for that
    category's `fetch_list` calls; an exhausted (or absent) category returns an
    empty page so pagination terminates.
    """

    pages: dict[str, list[ReadwiseResponse]]
    calls: list[ListCall] = field(default_factory=list["ListCall"])

    async def fetch_list(
        self,
        *,
        updated_after: object,
        category: str,
        page_cursor: str | None,
    ) -> ReadwiseResponse:
        self.calls.append(
            ListCall(
                updated_after=updated_after,
                category=category,
                page_cursor=page_cursor,
            )
        )
        queue = self.pages.get(category, [])
        if queue:
            return queue.pop(0)
        return list_response([])


def reader_document(  # noqa: PLR0913 (a builder mirroring the list API's shape)
    document_id: str,
    *,
    title: str = "A Book",
    author: str = "An Author",
    category: str = "epub",
    reading_progress: float = 0.1,
    location: str = "later",
    last_opened_at: str = "2026-01-01T00:00:00Z",
    updated_at: str = "2026-01-02T00:00:00Z",
) -> dict[str, object]:
    """Build one raw Reader document as the v3 list API shapes it."""
    return {
        "id": document_id,
        "title": title,
        "author": author,
        "category": category,
        "reading_progress": reading_progress,
        "location": location,
        "last_opened_at": last_opened_at,
        "updated_at": updated_at,
    }


def list_response(
    documents: Sequence[dict[str, object]],
    *,
    next_page_cursor: str | None = None,
    status_code: int = 200,
    retry_after_seconds: int | None = None,
) -> ReadwiseResponse:
    """Build one list-page response wrapping the given documents."""
    return ReadwiseResponse(
        status_code=status_code,
        payload={
            "count": len(documents),
            "nextPageCursor": next_page_cursor,
            "results": list(documents),
        },
        retry_after=(
            timedelta(seconds=retry_after_seconds)
            if retry_after_seconds is not None
            else None
        ),
    )


@dataclass
class ReaderEnv:
    """A Reader-ready database plus a live `MemoryService` over a temp KB."""

    database: Database
    memory_service: MemoryService
    logger: Logger

    def sync_service(self, transport: FakeReaderTransport) -> ReaderSyncService:
        """Wire a sync service over a scripted transport with instant backoff."""
        return ReaderSyncService(
            database=self.database,
            client=ReaderClient(transport=transport, sleep=_noop_sleep),
            memory_service=self.memory_service,
        )

    async def tethered_memories(self) -> list[Memory[Fetched]]:
        """The current tethered corpus, for finished-derivation assertions."""
        return await self.memory_service.browse_by_state("tethered", logger=self.logger)

    async def events(self, key: str) -> list[EbookProgressEvent[Fetched]]:
        """Every stored progress event for a document key, oldest first."""
        async with self.database.transaction() as tx:
            return await tx.fetch_all(
                select(EbookProgressEvent)
                .where(EbookProgressEvent.document_hash.eq(key))
                .order_by(EbookProgressEvent.id.asc())
            )

    async def document(self, key: str) -> EbookDocument[Fetched] | None:
        """The stored document row for a key, or None when unseen."""
        async with self.database.transaction() as tx:
            return await tx.fetch_one_or_none(
                select(EbookDocument).where(EbookDocument.document_hash.eq(key))
            )


@fixture
async def reader_env() -> AsyncGenerator[ReaderEnv]:
    """A fresh database with the Memory + kosync + Readwise schema and a KB dir."""
    database = await Database.initialize(backend=Config(database=":memory:"))
    await create_memory_schema(database)
    await create_kosync_schema(database)
    await create_readwise_schema(database)
    async with TemporaryDirectory() as kb_root:
        yield ReaderEnv(
            database=database,
            memory_service=MemoryService(
                database=database,
                kb_service=KnowledgeBaseService(kb_root=Path(kb_root)),
                tracer=noop_tracer(),
            ),
            logger=test_logger(),
        )
    await database.close()


@test()
async def fetch_documents_polls_each_category() -> None:
    """The client polls the list endpoint once per epub/pdf category."""
    transport = FakeReaderTransport(
        pages={
            "epub": [list_response([reader_document("e1", category="epub")])],
            "pdf": [list_response([reader_document("p1", category="pdf")])],
        }
    )

    documents = await ReaderClient(
        transport=transport, sleep=_noop_sleep
    ).fetch_documents(updated_after=None, logger=test_logger())

    assert_eq([document.document_id for document in documents], ["e1", "p1"])
    assert_eq([call.category for call in transport.calls], ["epub", "pdf"])


@test()
async def fetch_documents_follows_the_next_page_cursor() -> None:
    """Pagination walks every page of a category until `nextPageCursor` is null."""
    transport = FakeReaderTransport(
        pages={
            "epub": [
                list_response([reader_document("e1")], next_page_cursor="cursor-2"),
                list_response([reader_document("e2")]),
            ]
        }
    )

    documents = await ReaderClient(
        transport=transport, sleep=_noop_sleep
    ).fetch_documents(updated_after=None, logger=test_logger())

    assert_eq([document.document_id for document in documents], ["e1", "e2"])
    assert_eq(transport.calls[1].page_cursor, "cursor-2")


@test()
async def a_rate_limited_page_is_retried_after_backoff() -> None:
    """A 429 is retried on its `Retry-After` hint and the next page is returned."""
    transport = FakeReaderTransport(
        pages={
            "epub": [
                list_response([], status_code=429, retry_after_seconds=1),
                list_response([reader_document("e1")]),
            ]
        }
    )

    documents = await ReaderClient(
        transport=transport, sleep=_noop_sleep
    ).fetch_documents(updated_after=None, logger=test_logger())

    assert_eq([document.document_id for document in documents], ["e1"])


@test()
async def first_sync_appends_a_progress_event() -> None:
    """A first-seen document lands one progress event with the Reader device."""
    env = await load_fixture(reader_env())
    transport = FakeReaderTransport(
        pages={"epub": [list_response([reader_document("d1", reading_progress=0.3)])]}
    )

    _ = await env.sync_service(transport).sync(logger=env.logger)

    events = await env.events("reader:d1")
    assert_eq(len(events), 1)
    assert_eq(events[0].percentage, 0.3)
    assert_eq(events[0].progress, "later")
    assert_eq(events[0].device, "readwise-reader")


@test()
async def a_synced_document_is_upserted_with_the_api_title() -> None:
    """The document row carries the title from the list API, no labeling needed."""
    env = await load_fixture(reader_env())
    transport = FakeReaderTransport(
        pages={"epub": [list_response([reader_document("d1", title="Deep Work")])]}
    )

    _ = await env.sync_service(transport).sync(logger=env.logger)

    document = await env.document("reader:d1")
    assert_is_not_none(document)
    assert_eq(document.title, "Deep Work")  # pyright: ignore[reportOptionalMemberAccess]


@test()
async def an_unchanged_document_appends_no_second_event() -> None:
    """A later pass with identical progress and location adds no telemetry row."""
    env = await load_fixture(reader_env())
    payload = reader_document("d1", reading_progress=0.3, location="later")
    _ = await env.sync_service(
        FakeReaderTransport(pages={"epub": [list_response([payload])]})
    ).sync(logger=env.logger)

    _ = await env.sync_service(
        FakeReaderTransport(pages={"epub": [list_response([dict(payload)])]})
    ).sync(logger=env.logger)

    assert_eq(len(await env.events("reader:d1")), 1)


@test()
async def changed_reading_progress_appends_a_new_event() -> None:
    """A later pass with advanced reading progress appends a fresh event."""
    env = await load_fixture(reader_env())
    _ = await env.sync_service(
        FakeReaderTransport(
            pages={
                "epub": [list_response([reader_document("d1", reading_progress=0.3)])]
            }
        )
    ).sync(logger=env.logger)

    _ = await env.sync_service(
        FakeReaderTransport(
            pages={
                "epub": [list_response([reader_document("d1", reading_progress=0.6)])]
            }
        )
    ).sync(logger=env.logger)

    assert_eq([event.percentage for event in await env.events("reader:d1")], [0.3, 0.6])


@test()
async def a_changed_location_appends_a_new_event() -> None:
    """A moved document (same progress, new location) appends a fresh event."""
    env = await load_fixture(reader_env())
    _ = await env.sync_service(
        FakeReaderTransport(
            pages={
                "epub": [
                    list_response(
                        [reader_document("d1", reading_progress=0.3, location="later")]
                    )
                ]
            }
        )
    ).sync(logger=env.logger)

    _ = await env.sync_service(
        FakeReaderTransport(
            pages={
                "epub": [
                    list_response(
                        [
                            reader_document(
                                "d1", reading_progress=0.3, location="shortlist"
                            )
                        ]
                    )
                ]
            }
        )
    ).sync(logger=env.logger)

    assert_eq(
        [event.progress for event in await env.events("reader:d1")],
        ["later", "shortlist"],
    )


@test()
async def an_archived_document_captures_one_finished_memory() -> None:
    """A document in the archive location mints exactly one tethered Memory."""
    env = await load_fixture(reader_env())
    transport = FakeReaderTransport(
        pages={
            "epub": [
                list_response(
                    [reader_document("d1", reading_progress=0.5, location="archive")]
                )
            ]
        }
    )

    _ = await env.sync_service(transport).sync(logger=env.logger)

    assert_eq(len(await env.tethered_memories()), 1)


@test()
async def reading_past_the_threshold_captures_a_finished_memory() -> None:
    """Reading progress at or past 0.98 mints a finished Memory without archiving."""
    env = await load_fixture(reader_env())
    transport = FakeReaderTransport(
        pages={
            "epub": [
                list_response(
                    [reader_document("d1", reading_progress=0.98, location="later")]
                )
            ]
        }
    )

    _ = await env.sync_service(transport).sync(logger=env.logger)

    assert_eq(len(await env.tethered_memories()), 1)


@test()
async def a_document_short_of_finished_captures_nothing() -> None:
    """An unarchived document below the threshold derives no Memory."""
    env = await load_fixture(reader_env())
    transport = FakeReaderTransport(
        pages={
            "epub": [
                list_response(
                    [reader_document("d1", reading_progress=0.5, location="later")]
                )
            ]
        }
    )

    _ = await env.sync_service(transport).sync(logger=env.logger)

    assert_eq(await env.tethered_memories(), [])


@test()
async def a_finished_memory_names_the_title_and_facets_the_author() -> None:
    """A finished capture names the book and carries source/category/title/author."""
    env = await load_fixture(reader_env())
    transport = FakeReaderTransport(
        pages={
            "epub": [
                list_response(
                    [
                        reader_document(
                            "d1",
                            title="Deep Work",
                            author="Cal Newport",
                            location="archive",
                        )
                    ]
                )
            ]
        }
    )

    _ = await env.sync_service(transport).sync(logger=env.logger)

    memory = (await env.tethered_memories())[0]
    assert_eq(memory.content, "Finished reading Deep Work")
    assert_eq(
        memory.facets,
        {
            "source": "readwise-reader",
            "category": "ebook",
            "title": "Deep Work",
            "author": "Cal Newport",
        },
    )


@test()
async def a_finished_memory_omits_the_author_facet_when_absent() -> None:
    """A document without an author facets only source/category/title."""
    env = await load_fixture(reader_env())
    transport = FakeReaderTransport(
        pages={
            "epub": [
                list_response(
                    [
                        reader_document(
                            "d1", title="Untitled", author="", location="archive"
                        )
                    ]
                )
            ]
        }
    )

    _ = await env.sync_service(transport).sync(logger=env.logger)

    memory = (await env.tethered_memories())[0]
    assert_eq(
        memory.facets,
        {"source": "readwise-reader", "category": "ebook", "title": "Untitled"},
    )


@test()
async def a_finished_memory_is_machine_synced_readwise() -> None:
    """Finished captures land with readwise provenance."""
    env = await load_fixture(reader_env())
    transport = FakeReaderTransport(
        pages={"epub": [list_response([reader_document("d1", location="archive")])]}
    )

    _ = await env.sync_service(transport).sync(logger=env.logger)

    memory = (await env.tethered_memories())[0]
    assert_eq(memory.provenance, {"kind": "readwise"})


@test()
async def an_untitled_finished_memory_falls_back_to_the_key() -> None:
    """A titleless finished document names its `reader:<id>` key in the content."""
    env = await load_fixture(reader_env())
    transport = FakeReaderTransport(
        pages={
            "epub": [
                list_response([reader_document("d1", title="", location="archive")])
            ]
        }
    )

    _ = await env.sync_service(transport).sync(logger=env.logger)

    memory = (await env.tethered_memories())[0]
    assert_eq(memory.content, "reader:d1 (unlabeled ebook)")


@test()
async def the_finished_memory_fires_once_per_document() -> None:
    """A second qualifying pass never mints a second finished Memory."""
    env = await load_fixture(reader_env())
    _ = await env.sync_service(
        FakeReaderTransport(
            pages={"epub": [list_response([reader_document("d1", location="archive")])]}
        )
    ).sync(logger=env.logger)

    _ = await env.sync_service(
        FakeReaderTransport(
            pages={
                "epub": [
                    list_response(
                        [
                            reader_document(
                                "d1", reading_progress=1.0, location="archive"
                            )
                        ]
                    )
                ]
            }
        )
    ).sync(logger=env.logger)

    assert_eq(len(await env.tethered_memories()), 1)


@test()
async def the_first_sync_pulls_without_a_watermark() -> None:
    """With no stored watermark the first pass is a full pull (`updatedAfter` unset)."""
    env = await load_fixture(reader_env())
    transport = FakeReaderTransport(
        pages={"epub": [list_response([reader_document("d1")])]}
    )

    _ = await env.sync_service(transport).sync(logger=env.logger)

    assert_is_none(transport.calls[0].updated_after)


@test()
async def the_watermark_is_passed_on_the_next_sync() -> None:
    """A completed pass persists a watermark the next pass sends as `updatedAfter`."""
    env = await load_fixture(reader_env())
    _ = await env.sync_service(
        FakeReaderTransport(pages={"epub": [list_response([reader_document("d1")])]})
    ).sync(logger=env.logger)

    second = FakeReaderTransport(
        pages={"epub": [list_response([reader_document("d1")])]}
    )
    _ = await env.sync_service(second).sync(logger=env.logger)

    assert_is_not_none(second.calls[0].updated_after)


@test()
async def a_re_synced_document_keeps_its_finished_stamp() -> None:
    """The once-ever finished guard is stamped after the capture."""
    env = await load_fixture(reader_env())
    transport = FakeReaderTransport(
        pages={"epub": [list_response([reader_document("d1", location="archive")])]}
    )

    _ = await env.sync_service(transport).sync(logger=env.logger)

    document = await env.document("reader:d1")
    assert_is_not_none(document)
    assert_true(document.finished_captured_at is not None)  # pyright: ignore[reportOptionalMemberAccess]
