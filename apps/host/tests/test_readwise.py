"""Behaviour tests for the Readwise ingestion gate.

These drive `ReadwiseClient` and `ReadwiseSyncService` against a real in-memory
SQLite database, mocking only the HTTP boundary with a scripted transport. They
assert canonical highlight content/metadata, create/edit/delete convergence,
full-then-incremental request shape, and the persisted watermark.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import structlog
from snekok import Err, Ok, Result
from snekql.sqlite import Config, Database, Fetched, select
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

from tether.readwise import ReadwiseClient, ReadwiseSyncService
from tether.readwise_http import (
    ReadwiseAuthenticationFailure,
    ReadwiseHttpFailure,
    ReadwiseNetworkFailure,
    ReadwiseProtocolFailure,
    ReadwiseRateLimitFailure,
    ReadwiseResponse,
)
from tether.readwise_store import ReadwiseHighlight, create_readwise_schema
from tether.structured_logging import Logger


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

    async def aclose(self) -> None:
        """Release no resources in the in-memory transport."""

    async def fetch_export(
        self,
        *,
        updated_after: datetime | None,
        page_cursor: str | None,
        include_deleted: bool,
    ) -> Result[ReadwiseResponse, ReadwiseNetworkFailure]:
        self.export_calls.append(
            ExportCall(
                updated_after=updated_after,
                page_cursor=page_cursor,
                include_deleted=include_deleted,
            )
        )
        return Ok(self.export_responses.pop(0))

    async def verify_token(
        self,
    ) -> Result[ReadwiseResponse, ReadwiseNetworkFailure]:
        return Ok(ReadwiseResponse(status_code=self.auth_status, payload={}))


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
    """A Readwise-ready canonical source store."""

    database: Database
    logger: Logger

    def sync_service(self, transport: FakeReadwiseTransport) -> ReadwiseSyncService:
        """Wire a sync service over a scripted transport with instant backoff."""
        return ReadwiseSyncService(
            database=self.database,
            client=ReadwiseClient(transport=transport, sleep=_noop_sleep),
        )

    async def highlights(self) -> list[ReadwiseHighlight[Fetched]]:
        """Return canonical highlight Evidence ordered by upstream identity."""
        async with self.database.transaction() as transaction:
            return await transaction.fetch_all(
                select(ReadwiseHighlight)
                .all()
                .order_by(ReadwiseHighlight.highlight_id.asc())
            )


@fixture
async def readwise_env() -> AsyncGenerator[ReadwiseEnv]:
    """A fresh database with the Readwise Evidence schema."""
    db = await Database.initialize(backend=Config(database=":memory:"))
    await create_readwise_schema(db)
    yield ReadwiseEnv(database=db, logger=test_logger())
    await db.close()


@test()
async def token_check_passes_on_a_204() -> None:
    """The token check reports valid only when auth returns 204."""
    client = ReadwiseClient(
        transport=FakeReadwiseTransport(export_responses=[], auth_status=204),
        sleep=_noop_sleep,
    )

    token = await client.verify_token(logger=test_logger())

    assert isinstance(token, Ok)


@test()
async def token_check_fails_on_a_non_204() -> None:
    """A non-204 auth response fails the token check (worker would disable)."""
    client = ReadwiseClient(
        transport=FakeReadwiseTransport(export_responses=[], auth_status=401),
        sleep=_noop_sleep,
    )

    token = await client.verify_token(logger=test_logger())

    assert isinstance(token, Err)
    assert_eq(
        token.error,
        ReadwiseAuthenticationFailure(operation="verify-token", status_code=401),
    )


@test()
async def an_unauthorized_export_is_an_authentication_failure() -> None:
    """A rejected export token remains a typed provider failure."""
    client = ReadwiseClient(
        transport=FakeReadwiseTransport(
            export_responses=[export_response([], status_code=401)]
        ),
        sleep=_noop_sleep,
    )

    books = await client.fetch_export(
        updated_after=None, include_deleted=False, logger=test_logger()
    )

    assert isinstance(books, Err)
    assert_eq(
        books.error,
        ReadwiseAuthenticationFailure(operation="export", status_code=401),
    )


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

    assert isinstance(books, Ok)
    assert_eq(len(books.value), 2)


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

    assert isinstance(books, Ok)
    assert_eq(books.value[0].highlights[0].text, "after backoff")


@test()
async def a_malformed_successful_export_is_a_protocol_failure() -> None:
    """Successful status cannot hide an invalid export contract."""
    client = ReadwiseClient(
        transport=FakeReadwiseTransport(
            export_responses=[
                ReadwiseResponse(status_code=200, payload={"results": "invalid"})
            ]
        ),
        sleep=_noop_sleep,
    )

    books = await client.fetch_export(
        updated_after=None, include_deleted=False, logger=test_logger()
    )

    assert isinstance(books, Err)
    assert_eq(books.error, ReadwiseProtocolFailure(operation="export"))


@test()
async def a_malformed_export_result_is_a_protocol_failure() -> None:
    """An invalid book entry cannot be silently skipped past the cursor."""
    client = ReadwiseClient(
        transport=FakeReadwiseTransport(
            export_responses=[
                ReadwiseResponse(
                    status_code=200,
                    payload={"results": ["invalid"], "nextPageCursor": None},
                )
            ]
        ),
        sleep=_noop_sleep,
    )

    books = await client.fetch_export(
        updated_after=None, include_deleted=False, logger=test_logger()
    )

    assert isinstance(books, Err)
    assert_eq(books.error, ReadwiseProtocolFailure(operation="export"))


@test()
async def an_upstream_export_failure_preserves_status_and_retry_hint() -> None:
    """A non-authentication HTTP failure remains operational data."""
    client = ReadwiseClient(
        transport=FakeReadwiseTransport(
            export_responses=[
                export_response([], status_code=503, retry_after_seconds=7)
            ]
        ),
        sleep=_noop_sleep,
    )

    books = await client.fetch_export(
        updated_after=None, include_deleted=False, logger=test_logger()
    )

    assert isinstance(books, Err)
    assert_eq(
        books.error,
        ReadwiseHttpFailure(
            operation="export",
            retry_after=timedelta(seconds=7),
            status_code=503,
        ),
    )


@test()
async def an_export_that_exhausts_rate_limit_retries_is_typed() -> None:
    """Persistent throttling remains retryable provider data."""
    client = ReadwiseClient(
        transport=FakeReadwiseTransport(
            export_responses=[
                export_response([], status_code=429, retry_after_seconds=1)
                for _ in range(5)
            ]
        ),
        sleep=_noop_sleep,
    )

    books = await client.fetch_export(
        updated_after=None, include_deleted=False, logger=test_logger()
    )

    assert isinstance(books, Err)
    assert_eq(
        books.error,
        ReadwiseRateLimitFailure(operation="export", retry_after=timedelta(seconds=1)),
    )


@test()
async def a_failed_sync_returns_the_provider_failure() -> None:
    """An expected export outage does not escape the ingestion service."""
    env = await load_fixture(readwise_env())
    transport = FakeReadwiseTransport(
        export_responses=[export_response([], status_code=503)]
    )

    report = await env.sync_service(transport).sync(logger=env.logger)

    assert isinstance(report, Err)
    assert_eq(
        report.error,
        ReadwiseHttpFailure(operation="export", retry_after=None, status_code=503),
    )


@test()
async def first_sync_persists_one_evidence_row_per_highlight() -> None:
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

    assert isinstance(report, Ok)
    assert_eq(report.value.created, 2)
    assert_eq(len(await env.highlights()), 2)


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

    memory = (await env.highlights())[0]
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

    memory = (await env.highlights())[0]
    assert_eq(
        memory.metadata,
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

    memory = (await env.highlights())[0]
    assert_eq(memory.metadata, {"source": "readwise", "title": "Solo"})


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

    assert isinstance(report, Ok)
    assert_eq(report.value.created, 0)
    assert_eq(await env.highlights(), [])


@test()
async def highlight_metadata_carries_readwise_source() -> None:
    """Every ingested highlight lands with machine-synced Readwise provenance."""
    env = await load_fixture(readwise_env())
    transport = FakeReadwiseTransport(
        export_responses=[export_response([book_payload([highlight_payload(1, "x")])])]
    )

    _ = await env.sync_service(transport).sync(logger=env.logger)

    memory = (await env.highlights())[0]
    assert_eq(memory.metadata["source"], "readwise")


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
async def an_edited_highlight_updates_evidence_in_place() -> None:
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

    memories = await env.highlights()
    assert isinstance(report, Ok)
    assert_eq(report.value.updated, 1)
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

    memory = (await env.highlights())[0]
    assert isinstance(report, Ok)
    assert_eq(report.value.skipped, 1)
    assert_eq(memory.updated_at, "2026-01-01T00:00:00+00:00")


@test()
async def a_deleted_highlight_removes_evidence() -> None:
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

    assert isinstance(report, Ok)
    assert_eq(report.value.deleted, 1)
    assert_eq(await env.highlights(), [])


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

    assert isinstance(report, Ok)
    assert_eq(report.value.deleted, 1)
    assert_eq(await env.highlights(), [])
