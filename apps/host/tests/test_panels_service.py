"""Behavior tests for the Synthetic panel service layer.

These drive the *service* seam directly against a real (in-memory) SQLite
database — no HTTP, no agent — asserting on observable behavior: created rows,
spec validation, the optimistic-concurrency guards, and query execution over a
real Memory corpus (facet AND matching, relative-window resolution, the
tethered-only invariant, and the display cap with its total).
"""

from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import structlog
from anyio import TemporaryDirectory
from opentelemetry import trace
from opentelemetry.trace import Tracer
from snekql.sqlite import Config, Database, Fetched, select, update
from snektest import (
    assert_eq,
    assert_raises,
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
from tether.panels import (
    InvalidPanelSpecError,
    PanelConflictError,
    PanelNotFoundError,
    PanelRenderKind,
    PanelService,
    PanelSpec,
    create_panel_schema,
)
from tether.search_index import SearchCandidate

LOGGER: Logger = structlog.stdlib.get_logger("test.panels_service")


def noop_tracer() -> Tracer:
    """A tracer that emits nowhere, for tests that don't assert on spans."""
    return trace.NoOpTracerProvider().get_tracer("test.panels_service")


class FakeSearcher:
    """A MemorySearcher that ranks every indexed id it was primed with.

    Stands in for the LanceDB seam: `candidates` returns the primed ids in
    order, so execute-with-query tests control ranking without an index.
    """

    def __init__(self) -> None:
        self.ids: list[Memory[Fetched]] = []

    async def candidates(
        self, query: str, *, limit: int, logger: Logger
    ) -> list[SearchCandidate]:
        return [
            SearchCandidate(id=memory.id, score=1.0 - position / 100)
            for position, memory in enumerate(self.ids[:limit])
        ]

    async def index_memory(self, memory: Memory[Fetched], *, logger: Logger) -> None:
        self.ids.append(memory)

    async def deindex_memory(self, memory_id: object, *, logger: Logger) -> None:
        self.ids = [memory for memory in self.ids if memory.id != memory_id]


class Harness:
    """A panel service plus the memory corpus it executes against."""

    def __init__(
        self,
        panel_service: PanelService,
        memory_service: MemoryService,
        database: Database,
    ) -> None:
        self.panels: PanelService = panel_service
        self.memories: MemoryService = memory_service
        self.database: Database = database

    async def tethered_memory(
        self,
        content: str,
        facets: dict[str, str] | None = None,
        *,
        tethered_at: datetime | None = None,
    ) -> Memory[Fetched]:
        """Capture and tether a Memory, optionally backdating `tethered_at`."""
        memory = await self.memories.capture(content, facets=facets, logger=LOGGER)
        memory = await self.memories.tether(memory, logger=LOGGER)
        if tethered_at is not None:
            async with self.database.transaction() as tx:
                _ = await tx.execute(
                    update(Memory)
                    .set(Memory.tethered_at.to(tethered_at))
                    .where(Memory.id.eq(memory.id))
                )
                memory = await tx.fetch_one(
                    select(Memory).where(Memory.id.eq(memory.id))
                )
        return memory


@fixture
async def panel_harness() -> AsyncGenerator[Harness]:
    """A fresh panel + memory database with a deterministic fake searcher."""
    db = await Database.initialize(backend=Config(database=":memory:"))
    await create_memory_schema(db)
    await create_panel_schema(db)
    async with TemporaryDirectory() as kb_root:
        memory_service = MemoryService(
            database=db,
            kb_service=KnowledgeBaseService(kb_root=Path(kb_root)),
            tracer=noop_tracer(),
            searcher=FakeSearcher(),
        )
        panel_service = PanelService(
            database=db, memory_service=memory_service, tracer=noop_tracer()
        )
        yield Harness(panel_service, memory_service, db)
    await db.close()


def facet_panel(
    name: str = "finance",
    *,
    window_days: int | None = None,
    render_kind: PanelRenderKind = "table",
    position: int = 0,
) -> PanelSpec:
    """A minimal valid facets-only panel spec."""
    return PanelSpec(
        name=name,
        facets={"domain": "finance"},
        window_days=window_days,
        render_kind=render_kind,
        position=position,
    )


# --- create / spec validation ---


@test()
async def create_returns_a_panel_with_defaults() -> None:
    """Creating stores the spec and fills the defaulted columns."""
    harness = await load_fixture(panel_harness())

    panel = await harness.panels.create(facet_panel(), logger=LOGGER)

    assert_eq(panel.name, "finance")
    assert_eq(panel.facets, {"domain": "finance"})
    assert_eq(panel.query, None)
    assert_eq(panel.window_days, None)
    assert_eq(panel.render_kind, "table")
    assert_eq(panel.version, 1)


@test()
async def create_rejects_a_blank_name() -> None:
    """A panel must be nameable; whitespace is not a name."""
    harness = await load_fixture(panel_harness())

    with assert_raises(InvalidPanelSpecError):
        _ = await harness.panels.create(facet_panel(name="   "), logger=LOGGER)


@test()
async def create_rejects_an_unscoped_panel() -> None:
    """A panel with neither facets nor a text query selects nothing meaningful."""
    harness = await load_fixture(panel_harness())

    with assert_raises(InvalidPanelSpecError):
        _ = await harness.panels.create(
            PanelSpec(name="everything", facets={}), logger=LOGGER
        )


@test()
async def create_allows_a_query_only_panel() -> None:
    """A text query alone is a valid scope; facets are optional then."""
    harness = await load_fixture(panel_harness())

    panel = await harness.panels.create(
        PanelSpec(name="gifts", facets={}, query="gift ideas"), logger=LOGGER
    )

    assert_eq(panel.query, "gift ideas")


@test()
async def create_rejects_vega_lite_without_a_spec() -> None:
    """The vega-lite render kind is meaningless without a stored spec template."""
    harness = await load_fixture(panel_harness())

    with assert_raises(InvalidPanelSpecError):
        _ = await harness.panels.create(
            facet_panel(render_kind="vega-lite"), logger=LOGGER
        )


@test()
async def create_rejects_a_non_positive_window() -> None:
    """A relative window is a positive day count."""
    harness = await load_fixture(panel_harness())

    with assert_raises(InvalidPanelSpecError):
        _ = await harness.panels.create(facet_panel(window_days=0), logger=LOGGER)


# --- list / update / delete ---


@test()
async def list_orders_by_position_then_created_and_hides_deleted() -> None:
    """Listing returns live panels in explicit position order."""
    harness = await load_fixture(panel_harness())
    second = await harness.panels.create(
        facet_panel(name="second", position=2), logger=LOGGER
    )
    first = await harness.panels.create(
        facet_panel(name="first", position=1), logger=LOGGER
    )
    gone = await harness.panels.create(
        facet_panel(name="gone", position=0), logger=LOGGER
    )
    _ = await harness.panels.delete(gone, logger=LOGGER)

    panels = await harness.panels.list_panels(logger=LOGGER)

    assert_eq([panel.name for panel in panels], ["first", "second"])
    assert_eq([panel.id for panel in panels], [first.id, second.id])


@test()
async def update_replaces_the_definition_and_bumps_the_version() -> None:
    """Updating swaps the stored spec at the observed version."""
    harness = await load_fixture(panel_harness())
    panel = await harness.panels.create(facet_panel(), logger=LOGGER)

    fresh = await harness.panels.update(
        panel,
        facet_panel(name="renamed", window_days=30),
        logger=LOGGER,
    )

    assert_eq(fresh.name, "renamed")
    assert_eq(fresh.window_days, 30)
    assert_eq(fresh.version, panel.version + 1)


@test()
async def update_conflicts_on_a_stale_version() -> None:
    """A stale observed version means someone else edited first."""
    harness = await load_fixture(panel_harness())
    panel = await harness.panels.create(facet_panel(), logger=LOGGER)
    _ = await harness.panels.update(panel, facet_panel(name="renamed"), logger=LOGGER)

    with assert_raises(PanelConflictError):
        _ = await harness.panels.update(panel, facet_panel(name="again"), logger=LOGGER)


@test()
async def update_raises_when_the_panel_is_absent() -> None:
    """Updating a deleted panel is absence, not conflict."""
    harness = await load_fixture(panel_harness())
    panel = await harness.panels.create(facet_panel(), logger=LOGGER)
    _ = await harness.panels.delete(panel, logger=LOGGER)

    with assert_raises(PanelNotFoundError):
        _ = await harness.panels.update(panel, facet_panel(name="x"), logger=LOGGER)


@test()
async def delete_is_convergent() -> None:
    """Deleting an already-deleted panel re-asserts the end state, no error."""
    harness = await load_fixture(panel_harness())
    panel = await harness.panels.create(facet_panel(), logger=LOGGER)

    first = await harness.panels.delete(panel, logger=LOGGER)
    second = await harness.panels.delete(panel, logger=LOGGER)

    assert_eq(first.id, second.id)
    assert_eq(await harness.panels.list_panels(logger=LOGGER), [])


# --- execute: facets-only path ---


@test()
async def execute_filters_by_exact_facet_and_match() -> None:
    """Only tethered Memories carrying every panel facet exactly survive."""
    harness = await load_fixture(panel_harness())
    match = await harness.tethered_memory("rent is 900", {"domain": "finance"})
    _ = await harness.tethered_memory("aisle seats", {"domain": "travel"})
    _ = await harness.tethered_memory("no facets at all")
    panel = await harness.panels.create(facet_panel(), logger=LOGGER)

    results = await harness.panels.execute(panel, now=datetime.now(UTC), logger=LOGGER)

    assert_eq([memory.id for memory in results.memories], [match.id])
    assert_eq(results.total, 1)


@test()
async def execute_never_returns_loose_memories() -> None:
    """A panel is a surface over the trusted corpus only (ADR 0001)."""
    harness = await load_fixture(panel_harness())
    _ = await harness.memories.capture(
        "loose guess", facets={"domain": "finance"}, logger=LOGGER
    )
    panel = await harness.panels.create(facet_panel(), logger=LOGGER)

    results = await harness.panels.execute(panel, now=datetime.now(UTC), logger=LOGGER)

    assert_eq(results.memories, [])
    assert_eq(results.total, 0)


@test()
async def execute_resolves_the_relative_window_at_query_time() -> None:
    """`window_days` bounds `tethered_at` against the caller's now (ADR 0006)."""
    harness = await load_fixture(panel_harness())
    now = datetime(2030, 6, 1, tzinfo=UTC)
    recent = await harness.tethered_memory(
        "recent", {"domain": "finance"}, tethered_at=now - timedelta(days=2)
    )
    _ = await harness.tethered_memory(
        "ancient", {"domain": "finance"}, tethered_at=now - timedelta(days=40)
    )
    panel = await harness.panels.create(facet_panel(window_days=7), logger=LOGGER)

    results = await harness.panels.execute(panel, now=now, logger=LOGGER)

    assert_eq([memory.id for memory in results.memories], [recent.id])


@test()
async def execute_orders_most_recently_tethered_first() -> None:
    """Without a text query, recency of trust is the panel's order."""
    harness = await load_fixture(panel_harness())
    now = datetime(2030, 6, 1, tzinfo=UTC)
    older = await harness.tethered_memory(
        "older", {"domain": "finance"}, tethered_at=now - timedelta(days=3)
    )
    newer = await harness.tethered_memory(
        "newer", {"domain": "finance"}, tethered_at=now - timedelta(days=1)
    )
    panel = await harness.panels.create(facet_panel(), logger=LOGGER)

    results = await harness.panels.execute(panel, now=now, logger=LOGGER)

    assert_eq([memory.id for memory in results.memories], [newer.id, older.id])


@test()
async def execute_caps_rows_but_reports_the_full_total() -> None:
    """A broad panel shows `limit` rows while `total` counts every match."""
    harness = await load_fixture(panel_harness())
    for index in range(5):
        _ = await harness.tethered_memory(f"fact {index}", {"domain": "finance"})
    panel = await harness.panels.create(facet_panel(), logger=LOGGER)

    results = await harness.panels.execute(
        panel, now=datetime.now(UTC), limit=2, logger=LOGGER
    )

    assert_eq(len(results.memories), 2)
    assert_eq(results.total, 5)


# --- execute: text-query path ---


@test()
async def execute_with_query_ranks_by_search_and_filters_by_facets() -> None:
    """A text query rides hybrid Search; panel facets still gate the results."""
    harness = await load_fixture(panel_harness())
    match = await harness.tethered_memory("gift: fancy knife", {"domain": "gifts"})
    _ = await harness.tethered_memory("gift: socks", {"domain": "clothes"})
    panel = await harness.panels.create(
        PanelSpec(name="gifts", facets={"domain": "gifts"}, query="gift"),
        logger=LOGGER,
    )

    results = await harness.panels.execute(panel, now=datetime.now(UTC), logger=LOGGER)

    assert_eq([memory.id for memory in results.memories], [match.id])


@test()
async def execute_with_query_applies_the_window() -> None:
    """The relative window bounds the search path the same way."""
    harness = await load_fixture(panel_harness())
    now = datetime(2030, 6, 1, tzinfo=UTC)
    recent = await harness.tethered_memory(
        "gift: recent idea",
        {"domain": "gifts"},
        tethered_at=now - timedelta(days=1),
    )
    _ = await harness.tethered_memory(
        "gift: stale idea",
        {"domain": "gifts"},
        tethered_at=now - timedelta(days=90),
    )
    panel = await harness.panels.create(
        PanelSpec(name="gifts", facets={}, query="gift", window_days=7),
        logger=LOGGER,
    )

    results = await harness.panels.execute(panel, now=now, logger=LOGGER)

    assert_eq([memory.id for memory in results.memories], [recent.id])
