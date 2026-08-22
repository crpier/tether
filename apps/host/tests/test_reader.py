"""Behaviour tests for the Readwise Reader v3 progress rider.

These drive `ReaderClient` and `ReaderSyncService` against a real in-memory
SQLite database, mocking only the HTTP boundary with a scripted transport. They
assert pagination, append dedupe, source document identity, completion state,
and full-then-incremental watermarks without direct Memory writes.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Sequence
from dataclasses import dataclass, field
from datetime import timedelta

import structlog
from snekok import Err, Ok, Result
from snekql.sqlite import Config, Database, Fetched, select
from snektest import (
    assert_eq,
    assert_is_not_none,
    fixture,
    load_fixture,
    test,
)

from tether.kosync_store import (
    EbookDocument,
    EbookProgressEvent,
    create_kosync_schema,
)
from tether.reader import ReaderClient, ReaderSyncService
from tether.readwise_http import (
    ReadwiseAuthenticationFailure,
    ReadwiseHttpFailure,
    ReadwiseNetworkFailure,
    ReadwiseProtocolFailure,
    ReadwiseResponse,
)
from tether.readwise_store import create_readwise_schema
from tether.structured_logging import Logger


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

    async def aclose(self) -> None:
        """Release no resources in the in-memory transport."""

    async def fetch_list(
        self,
        *,
        updated_after: object,
        category: str,
        page_cursor: str | None,
    ) -> Result[ReadwiseResponse, ReadwiseNetworkFailure]:
        self.calls.append(
            ListCall(
                updated_after=updated_after,
                category=category,
                page_cursor=page_cursor,
            )
        )
        queue = self.pages.get(category, [])
        if queue:
            return Ok(queue.pop(0))
        return Ok(list_response([]))


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
    """A Reader-ready database and source store."""

    database: Database
    logger: Logger

    def sync_service(self, transport: FakeReaderTransport) -> ReaderSyncService:
        """Wire a sync service over a scripted transport with instant backoff."""
        return ReaderSyncService(
            database=self.database,
            client=ReaderClient(transport=transport, sleep=_noop_sleep),
        )

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
    """A fresh database with the ebook and Readwise source schemas."""
    database = await Database.initialize(backend=Config(database=":memory:"))
    await create_kosync_schema(database)
    await create_readwise_schema(database)
    yield ReaderEnv(database=database, logger=test_logger())
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

    assert isinstance(documents, Ok)
    assert_eq([document.document_id for document in documents.value], ["e1", "p1"])
    assert_eq([call.category for call in transport.calls], ["epub", "pdf"])


@test()
async def an_unauthorized_reader_page_is_an_authentication_failure() -> None:
    """A rejected Reader token remains a typed provider failure."""
    transport = FakeReaderTransport(
        pages={"epub": [list_response([], status_code=401)]}
    )

    documents = await ReaderClient(
        transport=transport, sleep=_noop_sleep
    ).fetch_documents(updated_after=None, logger=test_logger())

    assert isinstance(documents, Err)
    assert_eq(
        documents.error,
        ReadwiseAuthenticationFailure(operation="list", status_code=401),
    )


@test()
async def a_malformed_reader_result_is_a_protocol_failure() -> None:
    """An invalid document entry cannot be silently skipped past the cursor."""
    transport = FakeReaderTransport(
        pages={
            "epub": [
                ReadwiseResponse(
                    payload={"results": ["invalid"], "nextPageCursor": None}
                )
            ]
        }
    )

    documents = await ReaderClient(
        transport=transport, sleep=_noop_sleep
    ).fetch_documents(updated_after=None, logger=test_logger())

    assert isinstance(documents, Err)
    assert_eq(documents.error, ReadwiseProtocolFailure(operation="list"))


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

    assert isinstance(documents, Ok)
    assert_eq([document.document_id for document in documents.value], ["e1", "e2"])
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

    assert isinstance(documents, Ok)
    assert_eq([document.document_id for document in documents.value], ["e1"])


@test()
async def a_failed_reader_sync_returns_the_provider_failure() -> None:
    """An expected list outage does not escape the ingestion service."""
    env = await load_fixture(reader_env())
    transport = FakeReaderTransport(
        pages={"epub": [list_response([], status_code=503)]}
    )

    report = await env.sync_service(transport).sync(logger=env.logger)

    assert isinstance(report, Err)
    assert_eq(
        report.error,
        ReadwiseHttpFailure(operation="list", retry_after=None, status_code=503),
    )


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
async def archived_document_records_source_completion() -> None:
    """Archive completion stays on the source document for Dreaming."""
    env = await load_fixture(reader_env())
    transport = FakeReaderTransport(
        pages={"epub": [list_response([reader_document("done", location="archive")])]}
    )

    report = await env.sync_service(transport).sync(logger=env.logger)
    document = await env.document("reader:done")

    assert isinstance(report, Ok)
    assert_eq(report.value.finished, 1)
    assert_is_not_none(document)
    assert document is not None
    assert_is_not_none(document.finished_at)


@test()
async def progress_threshold_records_source_completion() -> None:
    env = await load_fixture(reader_env())
    transport = FakeReaderTransport(
        pages={
            "epub": [list_response([reader_document("done", reading_progress=0.98)])]
        }
    )

    report = await env.sync_service(transport).sync(logger=env.logger)

    assert isinstance(report, Ok)
    assert_eq(report.value.finished, 1)


@test()
async def completion_is_recorded_once_per_document() -> None:
    env = await load_fixture(reader_env())
    service = env.sync_service(
        FakeReaderTransport(
            pages={
                "epub": [list_response([reader_document("done", location="archive")])]
            }
        )
    )
    first = await service.sync(logger=env.logger)
    service.client.transport = FakeReaderTransport(
        pages={"epub": [list_response([reader_document("done", location="archive")])]}
    )
    second = await service.sync(logger=env.logger)

    assert isinstance(first, Ok)
    assert isinstance(second, Ok)
    assert_eq(first.value.finished, 1)
    assert_eq(second.value.finished, 0)
