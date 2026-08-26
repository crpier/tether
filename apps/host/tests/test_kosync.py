"""Behavior tests for KOReader progress as canonical Evidence."""

from collections.abc import AsyncGenerator
from datetime import UTC, datetime

import structlog
from snekql.sqlite import Config, Database
from snektest import (
    assert_eq,
    assert_is_none,
    assert_is_not_none,
    fixture,
    load_fixture,
    test,
)

from tether.kosync import KosyncService
from tether.kosync_model import ProgressUpdate
from tether.kosync_store import KosyncStore, create_kosync_schema
from tether.structured_logging import Logger

LOGGER: Logger = structlog.stdlib.get_logger("test.kosync")
NOW = datetime(2026, 1, 1, tzinfo=UTC)


@fixture
async def kosync_fixture() -> AsyncGenerator[tuple[KosyncService, KosyncStore]]:
    """Create an isolated service over its real source store."""
    database = await Database.initialize(backend=Config(database=":memory:"))
    await create_kosync_schema(database)
    store = KosyncStore(database)
    yield KosyncService(store), store
    await database.close()


def update(document: str = "book-hash", percentage: float = 0.5) -> ProgressUpdate:
    """Build one representative device progress event."""
    return ProgressUpdate(
        document=document,
        percentage=percentage,
        progress="/body/DocFragment[3]",
        device="Phone",
        device_id="device-1",
    )


@test()
async def progress_event_round_trips_without_creating_memory() -> None:
    """A push remains source-owned reading Evidence."""
    service, _ = await load_fixture(kosync_fixture())

    timestamp = await service.record_progress(update(), logger=LOGGER, now=NOW)
    latest = await service.latest_progress("book-hash")

    assert_eq(timestamp, int(NOW.timestamp()))
    assert_is_not_none(latest)
    assert latest is not None
    assert_eq(latest.percentage, 0.5)
    assert_eq(latest.device, "Phone")


@test()
async def first_finished_event_is_stamped_on_the_source_document() -> None:
    """Completion is source state for later Dreaming, not a Memory write."""
    service, store = await load_fixture(kosync_fixture())

    _ = await service.record_progress(update(percentage=0.99), logger=LOGGER, now=NOW)
    documents = await store.list_unlabeled()

    assert_eq(len(documents), 1)
    assert_is_not_none(documents[0].finished_at)


@test()
async def unfinished_event_does_not_stamp_completion() -> None:
    service, store = await load_fixture(kosync_fixture())

    _ = await service.record_progress(update(percentage=0.8), logger=LOGGER, now=NOW)
    documents = await store.list_unlabeled()

    assert_is_none(documents[0].finished_at)


@test()
async def repeated_finished_events_keep_one_completion_stamp() -> None:
    """The document-level completion transition remains convergent."""
    service, store = await load_fixture(kosync_fixture())

    _ = await service.record_progress(update(percentage=0.99), logger=LOGGER, now=NOW)
    first = (await store.list_unlabeled())[0].finished_at
    _ = await service.record_progress(update(percentage=1.0), logger=LOGGER, now=NOW)
    second = (await store.list_unlabeled())[0].finished_at

    assert_eq(second, first)


@test()
async def filename_matching_labels_source_identity() -> None:
    service, _ = await load_fixture(kosync_fixture())

    document = await service.match_ebook_filename("Books/Async IO.epub")

    assert_eq(document.title, "Async IO")
