"""Behavior tests for the Memory spine service layer.

These drive the *service* seam directly against a real (in-memory) SQLite
database — no HTTP, no agent — which is the primary testing seam: call a
capability and assert on observable behavior, never on internal structure.

The service under test is `tether.memories.MemoryService`, constructed over a
snekql `Database`. Each method owns its own transaction:

    capture(content)                 -> Memory
    tether(memory)                   -> Memory
    edit_content(memory, content)    -> Memory
    delete(memory)                   -> Memory
    search(query)                    -> list[Memory]

A `Memory` exposes `.id`, `.content`, `.version`, and the
`.tethered_at` / `.deleted_at` timestamps that derive its state.
"""

import asyncio
import contextlib
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from pathlib import Path

import structlog
from anyio import TemporaryDirectory
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import Tracer
from pydantic import PositiveInt
from snekql.sqlite import Config, Database, Fetched, delete, select, update
from snektest import (
    assert_eq,
    assert_gt,
    assert_in,
    assert_is_none,
    assert_is_not_none,
    assert_not_in,
    assert_raises,
    assert_true,
    fixture,
    load_fixture,
    test,
)
from structlog.testing import capture_logs

from tether.embeddings import FakeEmbedder
from tether.logging import Logger
from tether.memories import (
    EmptyMemoryContentError,
    EmptySearchQueryError,
    KnowledgeBaseService,
    Memory,
    MemoryConflictError,
    MemoryNotFoundError,
    MemoryProvenance,
    MemoryService,
    MemoryState,
    create_memory_schema,
)
from tether.reconciler import SearchReconciler
from tether.search_index import SearchDocument, SearchIndex
from tether.search_meta import SearchMetaService, create_search_meta_schema


def noop_tracer() -> Tracer:
    """A tracer that emits nowhere, for tests that don't assert on spans."""
    return trace.NoOpTracerProvider().get_tracer("test.memory_service")


class LoggedMemoryService:
    """Test adapter that supplies the mandatory service logger."""

    def __init__(self, service: MemoryService, *, logger: Logger) -> None:
        self.service: MemoryService = service
        self.logger: Logger = logger

    @property
    def database(self) -> Database:
        """Expose the wrapped database for DB-observable assertions."""
        return self.service.database

    @property
    def kb_service(self) -> KnowledgeBaseService:
        """Expose the wrapped KB service for projection assertions."""
        return self.service.kb_service

    async def capture(
        self,
        content: str,
        provenance: MemoryProvenance | None = None,
    ) -> Memory[Fetched]:
        """Capture through the wrapped service with logging context."""
        return await self.service.capture(
            content, provenance=provenance, logger=self.logger
        )

    async def search(
        self,
        query: str,
        limit: PositiveInt = 50,
    ) -> list[Memory[Fetched]]:
        """Search through the wrapped service with logging context."""
        return await self.service.search(query, limit=limit, logger=self.logger)

    async def browse_by_state(
        self, state: MemoryState, limit: int | None = None
    ) -> list[Memory[Fetched]]:
        """Browse through the wrapped service with logging context."""
        return await self.service.browse_by_state(
            state, limit=limit, logger=self.logger
        )

    async def tether(self, memory: Memory[Fetched]) -> Memory[Fetched]:
        """Tether through the wrapped service with logging context."""
        return await self.service.tether(memory, logger=self.logger)

    async def edit_content(
        self,
        memory: Memory[Fetched],
        content: str,
    ) -> Memory[Fetched]:
        """Edit through the wrapped service with logging context."""
        return await self.service.edit_content(memory, content, logger=self.logger)

    async def delete(self, memory: Memory[Fetched]) -> Memory[Fetched]:
        """Delete through the wrapped service with logging context."""
        return await self.service.delete(memory, logger=self.logger)

    async def regenerate_knowledge_base(self) -> None:
        """Regenerate projections with logging context."""
        await self.service.regenerate_knowledge_base(logger=self.logger)


async def capture_tethered_memory(
    service: LoggedMemoryService, content: str
) -> Memory[Fetched]:
    """Create a tethered Memory as test setup."""
    memory = await service.capture(content)
    return await service.tether(memory)


async def fetch_memory_row(
    service: LoggedMemoryService, memory: Memory[Fetched]
) -> Memory[Fetched] | None:
    """Fetch a Memory row directly for DB-observable assertions."""
    async with service.database.transaction() as tx:
        return await tx.fetch_one_or_none(select(Memory).where(Memory.id.eq(memory.id)))


async def hard_delete_memory_row(
    service: LoggedMemoryService, memory: Memory[Fetched]
) -> None:
    """Physically remove a row to simulate a missing observed Memory."""
    async with service.database.transaction() as tx:
        _ = await tx.execute(delete(Memory).where(Memory.id.eq(memory.id)))


def projection_path(service: LoggedMemoryService, memory: Memory[Fetched]) -> Path:
    """Return the derived KB projection path for a Memory."""
    return service.kb_service.kb_root / f"{memory.id}.md"


@fixture
async def memory_service() -> AsyncGenerator[LoggedMemoryService]:
    """A fresh, isolated Tether database + an empty markdown KB directory.

    The KB lives in a throwaway temp dir so projection assertions observe real
    files on disk; both DB and dir are torn down after each test.
    """
    db = await Database.initialize(backend=Config(database=":memory:"))
    await create_memory_schema(db)
    async with TemporaryDirectory() as kb_root:
        kb_service = KnowledgeBaseService(kb_root=Path(kb_root))
        yield LoggedMemoryService(
            MemoryService(database=db, kb_service=kb_service, tracer=noop_tracer()),
            logger=structlog.stdlib.get_logger("test.memory_service"),
        )
    await db.close()


_SEARCH_DIM = 64
"""FakeEmbedder width for search fixtures: wide enough to avoid token collisions."""


@dataclass
class SearchableHarness:
    """A MemoryService wired with the real search seam, plus its internals.

    Exposes the underlying `SearchIndex` so tests can deliberately drift it (index
    an id SQLite would never return) and prove the ADR-0001 re-filter holds."""

    service: LoggedMemoryService
    index: SearchIndex
    embedder: FakeEmbedder
    logger: Logger


@fixture
async def searchable_memory_service() -> AsyncGenerator[SearchableHarness]:
    """A MemoryService backed by a real LanceDB index + a deterministic embedder.

    Uses `FakeEmbedder` (no model download) and a throwaway on-disk index, so the
    full tether/edit/delete -> index -> hybrid-search path runs in the gate.
    """
    db = await Database.initialize(backend=Config(database=":memory:"))
    await create_memory_schema(db)
    await create_search_meta_schema(db)
    embedder = FakeEmbedder(vector_dim=_SEARCH_DIM)
    logger = structlog.stdlib.get_logger("test.memory_service.search")
    async with TemporaryDirectory() as root:
        root_path = Path(root)
        kb_root = root_path / "kb"
        kb_root.mkdir()
        index = await SearchIndex.open(
            index_dir=root_path / "index", vector_dim=_SEARCH_DIM
        )
        reconciler = SearchReconciler(
            database=db,
            index=index,
            embedder=embedder,
            meta=SearchMetaService(database=db),
        )
        service = MemoryService(
            database=db,
            kb_service=KnowledgeBaseService(kb_root=kb_root),
            tracer=noop_tracer(),
            searcher=reconciler,
        )
        yield SearchableHarness(
            service=LoggedMemoryService(service, logger=logger),
            index=index,
            embedder=embedder,
            logger=logger,
        )
    await db.close()


class FailingOnceKnowledgeBaseService(KnowledgeBaseService):
    """A KB adapter that drops the first projection write."""

    def __init__(self, kb_root: Path) -> None:
        super().__init__(kb_root=kb_root)
        self.failures_remaining: int = 1

    async def set_projection(self, memory: Memory[Fetched]) -> None:
        """Fail the first write, then behave like the real KB service."""
        if self.failures_remaining:
            self.failures_remaining -= 1
            message = "projection target unavailable"
            raise OSError(message)
        await super().set_projection(memory)


@test()
async def capture_emits_a_trace_span() -> None:
    """Capture creates a domain span without putting Memory content on it."""
    db = await Database.initialize(backend=Config(database=":memory:"))
    await create_memory_schema(db)
    span_exporter = InMemorySpanExporter()
    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(SimpleSpanProcessor(span_exporter))
    async with TemporaryDirectory() as kb_root:
        service = LoggedMemoryService(
            MemoryService(
                database=db,
                kb_service=KnowledgeBaseService(kb_root=Path(kb_root)),
                tracer=tracer_provider.get_tracer("test.memory_service"),
            ),
            logger=structlog.stdlib.get_logger("test.memory_service"),
        )

        _ = await service.capture("I prefer aisle seats")

    await db.close()
    spans = span_exporter.get_finished_spans()
    assert_in("MemoryService.capture", [span.name for span in spans])
    assert_true(all("content" not in (span.attributes or {}) for span in spans))


@test()
async def capture_logs_the_captured_memory_id_without_content() -> None:
    """Capture emits a domain event without leaking Memory content."""
    service = await load_fixture(memory_service())

    with capture_logs() as logs:
        memory = await service.capture("I prefer aisle seats on flights")

    assert_in(
        {
            "event": "Memory captured",
            "log_level": "info",
            "memory_id": str(memory.id),
            "version": 1,
        },
        logs,
    )
    assert_true(all(log.get("content") is None for log in logs))


@test()
async def search_logs_the_result_count() -> None:
    """Hybrid Search emits debug context about candidate and result counts."""
    service = (await load_fixture(searchable_memory_service())).service
    _ = await capture_tethered_memory(service, "needle matching memory")

    with capture_logs() as logs:
        _ = await service.search("needle")

    assert_in(
        {
            "event": "Memory Search completed",
            "log_level": "debug",
            "limit": 50,
            "candidate_count": 1,
            "result_count": 1,
        },
        logs,
    )


@test()
async def capture_lands_loose() -> None:
    """Capture always lands loose — never directly tethered."""
    service = await load_fixture(memory_service())

    memory = await service.capture("I prefer aisle seats on flights")

    assert_is_none(memory.tethered_at)


@test()
async def capture_records_manual_provenance() -> None:
    """Capture only ever produces manual provenance."""
    service = await load_fixture(memory_service())

    memory = await service.capture("I prefer aisle seats on flights")

    assert_eq(memory.provenance, {"kind": "manual"})


@test()
async def capture_stores_supplied_provenance() -> None:
    """Capture persists a richer provenance verbatim when one is supplied."""
    service = await load_fixture(memory_service())

    memory = await service.capture(
        "imported during a YouTube binge",
        MemoryProvenance(kind="youtube", confidence="low", batch="yt-2026-06"),
    )

    assert_eq(
        memory.provenance,
        {"kind": "youtube", "confidence": "low", "batch": "yt-2026-06"},
    )


@test()
async def capture_trims_content() -> None:
    """Capture stores the Memory content without surrounding whitespace."""
    service = await load_fixture(memory_service())

    memory = await service.capture("  I prefer aisle seats on flights  ")

    assert_eq(memory.content, "I prefer aisle seats on flights")


@test()
async def capture_rejects_blank_content() -> None:
    """Capture requires content after whitespace is trimmed."""
    service = await load_fixture(memory_service())

    with assert_raises(EmptyMemoryContentError):
        _ = await service.capture("   ")


@test()
async def capture_starts_at_version_one() -> None:
    """Optimistic concurrency starts from the first observed Memory revision."""
    service = await load_fixture(memory_service())

    memory = await service.capture("I prefer aisle seats on flights")

    assert_eq(memory.version, 1)


@test()
async def capturing_does_not_project_markdown() -> None:
    """a loose Memory is absent from the Knowledge base."""
    service = await load_fixture(memory_service())

    memory = await service.capture("I prefer aisle seats")

    assert_true(not projection_path(service, memory).exists())


@test()
async def loose_memory_is_excluded_from_search() -> None:
    """a loose Memory is not yet part of the assistant's Search."""
    service = (await load_fixture(searchable_memory_service())).service

    loose = await service.capture("I prefer aisle seats on flights")

    found = [hit.id for hit in await service.search("aisle")]
    assert_not_in(loose.id, found)


@test()
async def tether_makes_loose_memory_searchable() -> None:
    """Tether is the trust transition that admits a Memory to Search."""
    service = (await load_fixture(searchable_memory_service())).service

    memory = await capture_tethered_memory(
        service, "I prefer window seats on long flights"
    )

    found = [hit.id for hit in await service.search("window")]
    assert_in(memory.id, found)


@test()
async def deleted_memory_is_excluded_from_search() -> None:
    """Reject removes a tethered Memory from the assistant's Search."""
    service = (await load_fixture(searchable_memory_service())).service
    memory = await capture_tethered_memory(
        service, "got a penicillin prescription back in 2019"
    )

    _ = await service.delete(memory)

    found = [hit.id for hit in await service.search("penicillin")]
    assert_not_in(memory.id, found)


@test()
async def search_requires_a_non_empty_query() -> None:
    """Hybrid Search rejects blank queries before reaching the index.

    The blank-query guard runs ahead of the searcher check, so a bare service
    (no search seam) still rejects rather than embedding an empty string."""
    service = await load_fixture(memory_service())

    with assert_raises(EmptySearchQueryError):
        _ = await service.search("   ")


@test()
async def search_ranks_the_more_relevant_memory_first() -> None:
    """Hybrid Search is relevance-ranked: the stronger match leads the results."""
    service = (await load_fixture(searchable_memory_service())).service
    relevant = await capture_tethered_memory(
        service, "penicillin prescription from the pharmacy"
    )
    _ = await capture_tethered_memory(service, "grocery shopping list for sunday")

    found = [hit.id for hit in await service.search("penicillin prescription")]

    assert_eq(found[0], relevant.id)


@test()
async def search_returns_nothing_when_no_memory_is_tethered() -> None:
    """With nothing tethered the index is empty, so Search yields no results."""
    service = (await load_fixture(searchable_memory_service())).service
    _ = await service.capture("a loose, never-tethered memory")

    assert_eq(await service.search("memory"), [])


@test()
async def search_excludes_an_orphan_left_in_a_drifted_index() -> None:
    """ADR-0001 is enforced by the SQLite re-filter, not the index.

    A missed event can leave the index holding a Memory that is no longer
    `tethered ∧ ¬deleted`. Even when such an orphan is the top candidate, the
    re-filter against SQLite drops it: the assistant never sees it. Here a loose
    Memory is indexed directly (simulating drift) and must not surface."""
    harness = await load_fixture(searchable_memory_service())
    service = harness.service
    loose = await service.capture("orphaned aisle-seat preference")
    vector = await harness.embedder.embed_documents([loose.content])

    await harness.index.upsert(
        [SearchDocument(id=loose.id, content=loose.content, vector=vector[0])]
    )

    found = [hit.id for hit in await service.search("aisle")]
    assert_not_in(loose.id, found)


@test()
async def human_edit_of_tethered_memory_is_searchable_by_new_text() -> None:
    """A human edit of tethered Memory re-indexes it under the new text."""
    service = (await load_fixture(searchable_memory_service())).service
    memory = await capture_tethered_memory(service, "I live in Berlin")

    memory = await service.edit_content(memory, "I live in Munich")

    found = [hit.id for hit in await service.search("Munich")]
    assert_in(memory.id, found)


@test()
async def tether_bumps_version() -> None:
    """Tether consumes one observed revision and returns the next revision."""
    service = await load_fixture(memory_service())
    memory = await service.capture("I prefer aisle seats on flights")

    tethered = await service.tether(memory)

    assert_eq(tethered.version, memory.version + 1)


@test()
async def edit_bumps_version() -> None:
    """A human edit consumes one observed revision and returns the next revision."""
    service = await load_fixture(memory_service())
    memory = await service.capture("I live in Berlin")

    edited = await service.edit_content(memory, "I live in Munich")

    assert_eq(edited.version, memory.version + 1)


@test()
async def tether_stamps_tethered_at() -> None:
    """tether records when the trust transition happened."""
    service = await load_fixture(memory_service())
    memory = await service.capture("I prefer window seats on long flights")

    tethered = await service.tether(memory)

    _ = assert_is_not_none(tethered.tethered_at)


@test()
async def re_tethering_a_memory_raises_conflict() -> None:
    """a tethered Memory cannot pass through Review twice."""
    service = await load_fixture(memory_service())
    memory = await service.capture("I prefer aisle seats")
    _ = await service.tether(memory)

    with assert_raises(MemoryConflictError):
        _ = await service.tether(memory)


@test()
async def editing_with_a_stale_version_raises_conflict() -> None:
    """A human edit targets the Memory revision the human actually saw."""
    service = await load_fixture(memory_service())
    captured = await service.capture("I live in Berlin")
    observed = await service.tether(captured)
    _ = await service.edit_content(observed, "I live in Munich")

    with assert_raises(MemoryConflictError):
        _ = await service.edit_content(observed, "I live in Paris")


@test()
async def stale_edit_preserves_content() -> None:
    """A rejected stale edit does not overwrite current content."""
    service = await load_fixture(memory_service())
    captured = await service.capture("I live in Berlin")
    observed = await service.tether(captured)
    current = await service.edit_content(observed, "I live in Munich")

    with contextlib.suppress(MemoryConflictError):
        _ = await service.edit_content(observed, "I live in Paris")

    row = await fetch_memory_row(service, observed)
    assert row is not None, "stale edit must not remove the Memory"
    assert_eq(row.content, current.content)


@test()
async def stale_edit_preserves_version() -> None:
    """A rejected stale edit does not advance the stored version."""
    service = await load_fixture(memory_service())
    captured = await service.capture("I live in Berlin")
    observed = await service.tether(captured)
    current = await service.edit_content(observed, "I live in Munich")

    with contextlib.suppress(MemoryConflictError):
        _ = await service.edit_content(observed, "I live in Paris")

    row = await fetch_memory_row(service, observed)
    assert row is not None, "stale edit must not remove the Memory"
    assert_eq(row.version, current.version)


@test()
async def tethering_with_a_stale_version_raises_conflict() -> None:
    """Review targets the loose Memory revision the human actually saw."""
    service = await load_fixture(memory_service())
    observed = await service.capture("I think I prefer aisle seats")
    _ = await service.edit_content(observed, "I prefer aisle seats")

    with assert_raises(MemoryConflictError):
        _ = await service.tether(observed)


@test()
async def stale_tether_leaves_memory_loose() -> None:
    """A rejected stale tether does not promote the Memory."""
    service = await load_fixture(memory_service())
    observed = await service.capture("I think I prefer aisle seats")
    _ = await service.edit_content(observed, "I prefer aisle seats")

    with contextlib.suppress(MemoryConflictError):
        _ = await service.tether(observed)

    row = await fetch_memory_row(service, observed)
    assert row is not None, "stale tether must not remove the Memory"
    assert_is_none(row.tethered_at)


@test()
async def stale_tether_preserves_version() -> None:
    """A rejected stale tether does not advance the stored version."""
    service = await load_fixture(memory_service())
    observed = await service.capture("I think I prefer aisle seats")
    current = await service.edit_content(observed, "I prefer aisle seats")

    with contextlib.suppress(MemoryConflictError):
        _ = await service.tether(observed)

    row = await fetch_memory_row(service, observed)
    assert row is not None, "stale tether must not remove the Memory"
    assert_eq(row.version, current.version)


@test()
async def stale_tether_does_not_project_markdown() -> None:
    """A rejected stale tether does not admit the Memory to the Knowledge base."""
    service = await load_fixture(memory_service())
    observed = await service.capture("I think I prefer aisle seats")
    _ = await service.edit_content(observed, "I prefer aisle seats")

    with contextlib.suppress(MemoryConflictError):
        _ = await service.tether(observed)

    assert_true(not projection_path(service, observed).exists())


@test()
async def deleting_with_a_stale_version_raises_conflict() -> None:
    """Reject targets the Memory revision the human actually saw."""
    service = await load_fixture(memory_service())
    observed = await service.capture("I live in Berlin")
    _ = await service.tether(observed)

    with assert_raises(MemoryConflictError):
        _ = await service.delete(observed)


@test()
async def stale_delete_leaves_memory_live() -> None:
    """A rejected stale delete does not soft-delete the Memory."""
    service = await load_fixture(memory_service())
    observed = await service.capture("I live in Berlin")
    _ = await service.tether(observed)

    with contextlib.suppress(MemoryConflictError):
        _ = await service.delete(observed)

    row = await fetch_memory_row(service, observed)
    assert row is not None, "stale delete must not remove the Memory"
    assert_is_none(row.deleted_at)


@test()
async def stale_delete_keeps_memory_searchable() -> None:
    """A rejected stale delete does not remove the Memory from Search."""
    service = (await load_fixture(searchable_memory_service())).service
    observed = await service.capture("I live in Berlin")
    _ = await service.tether(observed)

    with contextlib.suppress(MemoryConflictError):
        _ = await service.delete(observed)

    found = [hit.id for hit in await service.search("Berlin")]
    assert_in(observed.id, found)


@test()
async def stale_delete_keeps_projection() -> None:
    """A rejected stale delete does not remove the projection."""
    service = await load_fixture(memory_service())
    observed = await service.capture("I live in Berlin")
    _ = await service.tether(observed)

    with contextlib.suppress(MemoryConflictError):
        _ = await service.delete(observed)

    assert_true(projection_path(service, observed).exists())


@test()
async def stale_delete_preserves_version() -> None:
    """A rejected stale delete does not advance the stored version."""
    service = await load_fixture(memory_service())
    observed = await service.capture("I live in Berlin")
    current = await service.tether(observed)

    with contextlib.suppress(MemoryConflictError):
        _ = await service.delete(observed)

    row = await fetch_memory_row(service, observed)
    assert row is not None, "stale delete must not remove the Memory"
    assert_eq(row.version, current.version)


@test()
async def tethering_a_deleted_memory_raises() -> None:
    """a soft-deleted Memory is not a live target for Review."""
    service = await load_fixture(memory_service())
    memory = await service.capture("a rejected loose memory")
    _ = await service.delete(memory)

    with assert_raises(MemoryNotFoundError):
        _ = await service.tether(memory)


@test()
async def tethering_a_deleted_memory_does_not_stamp_tethered_at() -> None:
    """Failed tether on a deleted Memory does not promote it."""
    service = await load_fixture(memory_service())
    memory = await service.capture("a rejected loose memory")
    _ = await service.delete(memory)

    with contextlib.suppress(MemoryNotFoundError):
        _ = await service.tether(memory)

    row = await fetch_memory_row(service, memory)
    assert row is not None, "soft-deleted row must remain inspectable"
    assert_is_none(row.tethered_at)


@test()
async def tethering_a_deleted_memory_does_not_project_markdown() -> None:
    """Failed tether on a deleted Memory does not write a projection."""
    service = await load_fixture(memory_service())
    memory = await service.capture("a rejected loose memory")
    _ = await service.delete(memory)

    with contextlib.suppress(MemoryNotFoundError):
        _ = await service.tether(memory)

    assert_true(not projection_path(service, memory).exists())


@test()
async def tethering_a_deleted_memory_with_current_version_does_not_promote_it() -> None:
    """A deleted Memory stays deleted even if the caller has its latest version."""
    service = await load_fixture(memory_service())
    memory = await service.capture("a rejected loose memory")
    deleted = await service.delete(memory)

    with contextlib.suppress(MemoryNotFoundError):
        _ = await service.tether(deleted)

    row = await fetch_memory_row(service, memory)
    assert row is not None, "soft-deleted row must remain inspectable"
    assert_is_none(row.tethered_at)


@test()
async def editing_a_deleted_memory_with_current_version_preserves_content() -> None:
    """A deleted Memory cannot be edited with its returned delete version."""
    service = await load_fixture(memory_service())
    memory = await service.capture("original rejected content")
    deleted = await service.delete(memory)

    with contextlib.suppress(MemoryNotFoundError):
        _ = await service.edit_content(deleted, "mutated after delete")

    row = await fetch_memory_row(service, memory)
    assert row is not None, "soft-deleted row must remain inspectable"
    assert_eq(row.content, "original rejected content")


@test()
async def editing_a_loose_memory_changes_content() -> None:
    """a human edit of loose Memory changes its content."""
    service = await load_fixture(memory_service())
    memory = await service.capture("I think I am allergic to penicillin")

    edited = await service.edit_content(memory, "I am allergic to penicillin")

    assert_eq(edited.content, "I am allergic to penicillin")


@test()
async def editing_a_memory_trims_content() -> None:
    """A human edit stores content without surrounding whitespace."""
    service = await load_fixture(memory_service())
    memory = await service.capture("I live in Berlin")

    edited = await service.edit_content(memory, "  I live in Munich  ")

    assert_eq(edited.content, "I live in Munich")


@test()
async def editing_a_memory_rejects_blank_content() -> None:
    """A human edit requires content after whitespace is trimmed."""
    service = await load_fixture(memory_service())
    memory = await service.capture("I live in Berlin")

    with assert_raises(EmptyMemoryContentError):
        _ = await service.edit_content(memory, "   ")


@test()
async def editing_a_loose_memory_keeps_it_loose() -> None:
    """a human edit of loose Memory does not promote it."""
    service = await load_fixture(memory_service())
    memory = await service.capture("I think I am allergic to penicillin")

    edited = await service.edit_content(memory, "I am allergic to penicillin")

    assert_is_none(edited.tethered_at)


@test()
async def editing_a_loose_memory_stays_excluded_from_search() -> None:
    """edited loose Memory stays outside assistant Search."""
    service = (await load_fixture(searchable_memory_service())).service
    memory = await service.capture("I think I am allergic to penicillin")

    _ = await service.edit_content(memory, "I am allergic to penicillin")

    found = [hit.id for hit in await service.search("penicillin")]
    assert_not_in(memory.id, found)


@test()
async def editing_a_loose_memory_does_not_project_markdown() -> None:
    """editing loose Memory does not admit it to the Knowledge base."""
    service = await load_fixture(memory_service())
    memory = await service.capture("I think I prefer aisle seats")

    _ = await service.edit_content(memory, "I prefer aisle seats")

    assert_true(not projection_path(service, memory).exists())


@test()
async def editing_a_memory_bumps_updated_at() -> None:
    """every edit advances `updated_at`."""
    service = await load_fixture(memory_service())
    memory = await service.capture("I live in Berlin")

    await asyncio.sleep(0.01)
    edited = await service.edit_content(memory, "I live in Munich")

    assert_gt(edited.updated_at, memory.updated_at)


@test()
async def deleting_a_memory_stamps_deleted_at() -> None:
    """reject stamps `deleted_at`."""
    service = await load_fixture(memory_service())
    memory = await service.capture("got a penicillin prescription back in 2019")

    _ = await service.delete(memory)

    row = await fetch_memory_row(service, memory)
    assert row is not None, "soft-delete must retain the row in the DB"
    _ = assert_is_not_none(row.deleted_at)


@test()
async def deleting_a_memory_retains_the_row() -> None:
    """reject is a soft-delete, so the DB row survives."""
    service = await load_fixture(memory_service())
    memory = await service.capture("got a penicillin prescription back in 2019")

    _ = await service.delete(memory)

    row = await fetch_memory_row(service, memory)
    assert row is not None, "soft-delete must retain the row in the DB"


@test()
async def deleting_a_memory_preserves_content() -> None:
    """soft-deleted Memory text stays recoverable in the DB."""
    service = await load_fixture(memory_service())
    memory = await service.capture("got a penicillin prescription back in 2019")

    _ = await service.delete(memory)

    row = await fetch_memory_row(service, memory)
    assert row is not None, "soft-delete must retain the row in the DB"
    assert_eq(row.content, "got a penicillin prescription back in 2019")


@test()
async def tethering_a_missing_memory_raises() -> None:
    """operating on an absent Memory is a well-formed error."""
    service = await load_fixture(memory_service())
    memory = await service.capture("a memory removed outside the service")
    await hard_delete_memory_row(service, memory)

    with assert_raises(MemoryNotFoundError):
        _ = await service.tether(memory)


@test()
async def editing_a_deleted_memory_raises() -> None:
    """a soft-deleted Memory is no longer a live target for edits."""
    service = await load_fixture(memory_service())
    memory = await service.capture("a fact I will reject before editing")
    _ = await service.delete(memory)

    with assert_raises(MemoryNotFoundError):
        _ = await service.edit_content(memory, "too late, already gone")


@test()
async def editing_a_deleted_memory_preserves_content() -> None:
    """Failed edits on deleted Memory leave content unchanged."""
    service = await load_fixture(memory_service())
    memory = await service.capture("original rejected content")
    _ = await service.delete(memory)

    with contextlib.suppress(MemoryNotFoundError):
        _ = await service.edit_content(memory, "mutated after delete")

    row = await fetch_memory_row(service, memory)
    assert row is not None, "soft-deleted row must remain inspectable"
    assert_eq(row.content, "original rejected content")


@test()
async def deleting_an_already_deleted_memory_raises() -> None:
    """a second reject finds no live Memory."""
    service = await load_fixture(memory_service())
    memory = await service.capture("a fact I will reject twice")
    _ = await service.delete(memory)

    with assert_raises(MemoryConflictError):
        _ = await service.delete(memory)


@test()
async def tethering_projects_markdown() -> None:
    """tether projects `kb/<id>.md` synchronously."""
    service = await load_fixture(memory_service())
    memory = await service.capture("I prefer aisle seats")

    _ = await service.tether(memory)

    assert_true(projection_path(service, memory).exists())


@test()
async def projected_file_contains_required_frontmatter_keys() -> None:
    """projection frontmatter carries the required keys."""
    service = await load_fixture(memory_service())
    memory = await capture_tethered_memory(service, "I prefer aisle seats on flights")

    contents = projection_path(service, memory).read_text()

    # TODO: I'd prefer this to use `assert_eq` instead.
    assert_in(str(memory.id), contents)
    assert_in("provenance", contents)
    assert_in("created_at", contents)
    assert_in("tethered_at", contents)
    assert_in("updated_at", contents)


@test()
async def projected_frontmatter_records_manual_provenance() -> None:
    """projected markdown exposes Memory provenance."""
    service = await load_fixture(memory_service())
    memory = await capture_tethered_memory(service, "I prefer aisle seats on flights")

    contents = projection_path(service, memory).read_text()

    assert_in("kind: manual", contents)


@test()
async def projected_file_contains_body() -> None:
    """projection body is the Memory text verbatim."""
    service = await load_fixture(memory_service())
    memory = await capture_tethered_memory(service, "I prefer aisle seats on flights")

    contents = projection_path(service, memory).read_text()

    assert_in("I prefer aisle seats on flights", contents)


@test()
async def editing_a_tethered_memory_reprojects_new_text() -> None:
    """editing tethered Memory writes the new text to markdown."""
    service = await load_fixture(memory_service())
    memory = await capture_tethered_memory(service, "I live in Berlin")

    _ = await service.edit_content(memory, "I live in Munich")

    contents = projection_path(service, memory).read_text()
    assert_in("I live in Munich", contents)


@test()
async def editing_a_tethered_memory_removes_old_projection_text() -> None:
    """re-projection drops the old text."""
    service = await load_fixture(memory_service())
    memory = await capture_tethered_memory(service, "I live in Berlin")

    _ = await service.edit_content(memory, "I live in Munich")

    contents = projection_path(service, memory).read_text()
    assert_not_in("Berlin", contents)


@test()
async def deleting_a_tethered_memory_removes_its_file() -> None:
    """rejecting a tethered Memory removes its markdown file."""
    service = await load_fixture(memory_service())
    memory = await capture_tethered_memory(service, "I prefer aisle seats")

    _ = await service.delete(memory)

    assert_true(not projection_path(service, memory).exists())


@test()
async def the_kb_directory_mirrors_the_tethered_set() -> None:
    """kb/ exactly matches tethered, non-deleted Memories."""
    service = await load_fixture(memory_service())

    _ = await service.capture("loose: never tethered")
    first = await capture_tethered_memory(service, "tethered: aisle seats")
    second = await capture_tethered_memory(service, "tethered: window seats")
    rejected = await capture_tethered_memory(service, "tethered then rejected")
    _ = await service.delete(rejected)

    files = {p.name for p in service.kb_service.kb_root.iterdir()}
    assert_eq(files, {f"{first.id}.md", f"{second.id}.md"})


@test()
async def projection_failure_does_not_roll_back_tether() -> None:
    """A post-commit projection failure leaves SQLite as the source of truth."""
    db = await Database.initialize(backend=Config(database=":memory:"))
    await create_memory_schema(db)
    async with TemporaryDirectory() as kb_root:
        service = LoggedMemoryService(
            MemoryService(
                database=db,
                kb_service=FailingOnceKnowledgeBaseService(kb_root=Path(kb_root)),
                tracer=noop_tracer(),
            ),
            logger=structlog.stdlib.get_logger("test.memory_service"),
        )
        memory = await service.capture("I prefer aisle seats")

        tethered = await service.tether(memory)

        live = [hit.id for hit in await service.browse_by_state("tethered")]
        assert_in(tethered.id, live)
        assert_true(not projection_path(service, tethered).exists())
    await db.close()


@test()
async def regenerating_the_kb_recovers_a_missed_projection() -> None:
    """The next explicit regeneration projects tethered Memories from SQLite."""
    db = await Database.initialize(backend=Config(database=":memory:"))
    await create_memory_schema(db)
    async with TemporaryDirectory() as kb_root:
        service = LoggedMemoryService(
            MemoryService(
                database=db,
                kb_service=FailingOnceKnowledgeBaseService(kb_root=Path(kb_root)),
                tracer=noop_tracer(),
            ),
            logger=structlog.stdlib.get_logger("test.memory_service"),
        )
        memory = await service.capture("I prefer aisle seats")
        tethered = await service.tether(memory)

        await service.regenerate_knowledge_base()

        assert_true(projection_path(service, tethered).exists())
    await db.close()


# --- Filter-only Search: the loose review queue and tethered browse ---
# Backs GET /memories?state=loose|tethered. A filter-only Search (no query) is
# state-agnostic as a mechanism but scoped by the requested state, and always
# excludes soft-deleted Memories.


@test()
async def loose_queue_returns_loose_memories() -> None:
    """GET /memories?state=loose surfaces Memories still awaiting Review."""
    service = await load_fixture(memory_service())
    loose = await service.capture("I think I prefer aisle seats")

    found = [hit.id for hit in await service.browse_by_state("loose")]

    assert_in(loose.id, found)


@test()
async def loose_queue_caps_rows_at_the_limit() -> None:
    """`limit` bounds the review queue so the assistant tool can't over-fetch."""
    service = await load_fixture(memory_service())
    for n in range(3):
        _ = await service.capture(f"loose memory {n}")

    found = await service.browse_by_state("loose", limit=2)

    assert_eq(len(found), 2)


@test()
async def loose_queue_excludes_tethered_memories() -> None:
    """The review queue is loose-only: a tethered Memory has left it."""
    service = await load_fixture(memory_service())
    tethered = await capture_tethered_memory(service, "I prefer window seats")

    found = [hit.id for hit in await service.browse_by_state("loose")]

    assert_not_in(tethered.id, found)


@test()
async def loose_queue_excludes_soft_deleted_memories() -> None:
    """A rejected loose Memory drops out of the review queue."""
    service = await load_fixture(memory_service())
    loose = await service.capture("a loose memory I will reject")
    _ = await service.delete(loose)

    found = [hit.id for hit in await service.browse_by_state("loose")]

    assert_not_in(loose.id, found)


@test()
async def loose_queue_orders_newest_first() -> None:
    """fresh captures surface first, reviewed while context is warm."""
    service = await load_fixture(memory_service())
    older = await service.capture("older loose memory")

    await asyncio.sleep(0.01)
    newer = await service.capture("newer loose memory")

    found = [hit.id for hit in await service.browse_by_state("loose")]

    assert_eq(found, [newer.id, older.id])


@test()
async def tethered_browse_returns_tethered_memories() -> None:
    """GET /memories?state=tethered browses the trusted corpus."""
    service = await load_fixture(memory_service())
    tethered = await capture_tethered_memory(service, "I prefer aisle seats")

    found = [hit.id for hit in await service.browse_by_state("tethered")]

    assert_in(tethered.id, found)


@test()
async def tethered_browse_excludes_loose_memories() -> None:
    """Tethered browse never shows Memories still awaiting Review."""
    service = await load_fixture(memory_service())
    loose = await service.capture("a loose memory")

    found = [hit.id for hit in await service.browse_by_state("tethered")]

    assert_not_in(loose.id, found)


@test()
async def tethered_browse_excludes_soft_deleted_memories() -> None:
    """A rejected tethered Memory drops out of the browse list."""
    service = await load_fixture(memory_service())
    memory = await capture_tethered_memory(service, "a tethered memory I will reject")
    _ = await service.delete(memory)

    found = [hit.id for hit in await service.browse_by_state("tethered")]

    assert_not_in(memory.id, found)


@test()
async def tethered_browse_orders_by_tethered_at_not_created_at() -> None:
    """Tethered browse is ordered by tether time, newest first — not capture time."""
    service = await load_fixture(memory_service())
    captured_first = await service.capture("captured first, tethered second")
    captured_second = await service.capture("captured second, tethered first")

    _ = await service.tether(captured_second)
    await asyncio.sleep(0.01)
    _ = await service.tether(captured_first)

    found = [hit.id for hit in await service.browse_by_state("tethered")]

    assert_eq(found, [captured_first.id, captured_second.id])


# --- Named corpus selectors (ADR-0001 invariant, written once) ---
# `MemoryService.tethered_corpus()` / `loose_queue()` are the single home of
# the trust predicate; every corpus/queue selection composes on top of them.


@test()
async def tethered_corpus_selects_only_tethered_non_deleted() -> None:
    """The named trusted-corpus selector enforces ADR-0001 on its own."""
    service = await load_fixture(memory_service())
    loose = await service.capture("a loose memory")
    tethered = await capture_tethered_memory(service, "a tethered memory")
    deleted = await capture_tethered_memory(service, "a tethered memory to reject")
    _ = await service.delete(deleted)

    async with service.database.transaction() as tx:
        corpus = await tx.fetch_all(MemoryService.tethered_corpus())

    found = [memory.id for memory in corpus]
    assert_in(tethered.id, found)
    assert_not_in(loose.id, found)
    assert_not_in(deleted.id, found)


@test()
async def loose_queue_selects_only_loose_non_deleted() -> None:
    """The named review-queue selector is the loose, non-deleted inverse."""
    service = await load_fixture(memory_service())
    loose = await service.capture("a loose memory")
    tethered = await capture_tethered_memory(service, "a tethered memory")
    deleted = await service.capture("a loose memory to reject")
    _ = await service.delete(deleted)

    async with service.database.transaction() as tx:
        queue = await tx.fetch_all(MemoryService.loose_queue())

    found = [memory.id for memory in queue]
    assert_in(loose.id, found)
    assert_not_in(tethered.id, found)
    assert_not_in(deleted.id, found)


# --- Keyword Search limit (default 50) ---


@test()
async def search_caps_results_at_the_given_limit() -> None:
    """Hybrid Search returns at most `limit` matches."""
    service = (await load_fixture(searchable_memory_service())).service
    for _ in range(3):
        _ = await capture_tethered_memory(service, "needle in the haystack")

    found = await service.search("needle", limit=2)

    assert_eq(len(found), 2)


@test()
async def search_defaults_to_a_limit_of_fifty() -> None:
    """Hybrid Search defaults `limit` to 50."""
    service = (await load_fixture(searchable_memory_service())).service
    for index in range(51):
        _ = await capture_tethered_memory(service, f"needle number {index}")

    found = await service.search("needle")

    assert_eq(len(found), 50)


@test()
async def a_captured_memory_owes_an_embedding() -> None:
    """A fresh Memory has no embedding yet: both embedding columns are NULL.

    The embedding vector is a *derived* artifact produced after capture, so a
    freshly captured Memory carries `embedding is None` (no bytes) and
    `embedded_version is None` (the vector owes the current content version)."""
    service = await load_fixture(memory_service())

    memory = await service.capture("I prefer aisle seats")

    assert_is_none(memory.embedding)
    assert_is_none(memory.embedded_version)


@test()
async def the_embedding_columns_round_trip_through_sqlite() -> None:
    """The embedding BLOB and embedded_version persist and read back exactly.

    SQLite holds the canonical vector as raw bytes; `embedded_version` records
    the content `version` the vector reflects. This asserts the storage contract
    the reconciler relies on, independent of how vectors are produced."""
    service = await load_fixture(memory_service())
    memory = await service.capture("I prefer aisle seats")
    payload = b"\x00\x01\x02\x03vector-bytes\xfe\xff"

    async with service.database.transaction() as tx:
        _ = await tx.execute(
            update(Memory)
            .set(Memory.embedding.to(payload))
            .set(Memory.embedded_version.to(memory.version))
            .where(Memory.id.eq(memory.id))
        )

    stored = await fetch_memory_row(service, memory)
    assert_is_not_none(stored)
    assert stored is not None
    assert_eq(stored.embedding, payload)
    assert_eq(stored.embedded_version, memory.version)


# The legacy `memory` schema as shipped before the embedding columns existed
# (PR #72 / hybrid search). A real pre-#72 `.tether/tether.sqlite3` carries
# exactly this DDL, recorded under the `001_memories` migration key. Frozen here
# so the test can stand up a database that looks like an existing deployment.
_LEGACY_MEMORY_DDL = (
    'CREATE TABLE "memory" ('
    '"id" TEXT PRIMARY KEY, '
    '"content" TEXT, '
    '"version" INTEGER, '
    '"provenance" TEXT, '
    "\"created_at\" TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')), "
    "\"updated_at\" TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')), "
    '"tethered_at" TEXT, '
    '"deleted_at" TEXT'
    ") STRICT"
)


@fixture
async def legacy_upgraded_memory_service() -> AsyncGenerator[LoggedMemoryService]:
    """A service over a database that began life on the pre-embedding schema.

    Stands up the legacy `memory` table under the original `001_memories` key,
    then runs `create_memory_schema` to bring it current — the exact path a real
    pre-#72 `.tether/tether.sqlite3` takes on the next boot.
    """
    db = await Database.initialize(backend=Config(database=":memory:"))
    await db.migrate({"001_memories": _LEGACY_MEMORY_DDL})
    await create_memory_schema(db)
    async with TemporaryDirectory() as kb_root:
        yield LoggedMemoryService(
            MemoryService(
                database=db,
                kb_service=KnowledgeBaseService(kb_root=Path(kb_root)),
                tracer=noop_tracer(),
            ),
            logger=structlog.stdlib.get_logger("test.memory_service"),
        )
    await db.close()


@test()
async def create_memory_schema_upgrades_a_legacy_pre_embedding_database() -> None:
    """An existing pre-embedding database gains the embedding columns.

    snekql records migrations by key and never re-runs an applied one, so a
    database that ran the original `001_memories` (before the embedding columns
    were added to the model) keeps a `memory` table without them. Capture then
    fails with `table memory has no column named embedding`. `create_memory_schema`
    must carry a forward migration that adds the columns to such a database, not
    rely on `scaffold` regenerating the current model under the same key.
    """
    service = await load_fixture(legacy_upgraded_memory_service())

    # Capture exercises the INSERT that references the embedding columns, and
    # browse exercises the SELECT that decodes them: both 500'd before the fix.
    memory = await service.capture("I prefer window seats")
    assert_is_none(memory.embedding)
    assert_is_none(memory.embedded_version)

    loose = await service.browse_by_state("loose")
    assert_eq([m.id for m in loose], [memory.id])

    payload = b"vector-bytes"
    async with service.database.transaction() as tx:
        _ = await tx.execute(
            update(Memory)
            .set(Memory.embedding.to(payload))
            .set(Memory.embedded_version.to(memory.version))
            .where(Memory.id.eq(memory.id))
        )
    stored = await fetch_memory_row(service, memory)
    assert stored is not None
    assert_eq(stored.embedding, payload)
    assert_eq(stored.embedded_version, memory.version)
