"""Synthetic panel domain: a saved faceted query rendered through Widgets.

A Synthetic panel is a saved faceted query over the Commons — a panel assembled
from convention, with no dedicated code per domain. The row stores the *scope*
(a facet AND-filter, an optional text query, an optional relative time window)
and the *render choice* (a Tether-styled table by default, or a stored
Vega-Lite spec template); execution recomputes the results on every view
(ADR 0006) against the trusted corpus only (ADR 0001).

The service owns the human/agent-facing CRUD (create / list / update / delete,
the mutations optimistic-concurrency checked like Scheduled triggers) plus
`execute`, which reuses the Memory search seam end-to-end: a text query rides
`search_candidates` + `hydrate_tethered` (rank order), a facets-only panel is a
recency-ordered corpus listing with the same facet post-filter semantics.

>>> service = PanelService(database=db, memory_service=memories, tracer=tracer)
>>> panel = await service.create(
...     PanelSpec(name="finance", facets={"domain": "finance"}), logger=logger
... )
>>> results = await service.execute(panel, now=datetime.now(UTC), logger=logger)
>>> results.total
0
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import ClassVar, Literal
from uuid import uuid7

from opentelemetry.trace import Tracer
from pydantic import UUID7, Json, PositiveInt
from snekql.sqlite import (
    CurrentTimestamp,
    Database,
    Fetched,
    Index,
    Integer,
    Model,
    Pending,
    Text,
    Transaction,
    insert,
    select,
    update,
)
from snekql.sqlite._schema_ddl import scaffold_sqlite_statements

from tether.db_retry import run_in_transaction
from tether.events import EventPublisher, InvalidateEvent, NullEventPublisher
from tether.logging import Logger
from tether.memories import Memory, MemoryService

type PanelRenderKind = Literal["table", "vega-lite"]
"""How a panel's results render: a Tether-styled table, or a stored Vega-Lite
spec template the result rows are injected into (ADR 0011 vocabulary only)."""

EXECUTE_DEFAULT_LIMIT = 20
"""Rows a panel shows by default; `total` still counts every match."""

_SEARCH_CANDIDATE_LIMIT = 200
"""Candidate bound for the text-query path: generous enough that the facet /
window post-filter rarely starves the display cap, small enough to stay cheap.
The facets-only path needs no bound — it counts matches for `total` anyway."""


class PanelNotFoundError(Exception):
    """Raised when an operation targets a panel that does not exist."""


class PanelConflictError(Exception):
    """Raised when a live panel cannot accept the requested operation.

    A stale observed version, not absence: the caller acted on a panel that
    has moved on since it was read.
    """


class InvalidPanelSpecError(Exception):
    """Raised when a panel's saved query or render choice is malformed."""


def _debug(logger: Logger, event: str, **context: object) -> None:
    """Emit a debug event using caller-supplied logging context."""
    logger.debug(event, **context)


def _info(logger: Logger, event: str, **context: object) -> None:
    """Emit an info event using caller-supplied logging context."""
    logger.info(event, **context)


@dataclass(frozen=True)
class PanelSpec:
    """The saved query + render choice a create or update carries.

    Validation happens in `_normalise_spec` before any write: a panel must be
    nameable and scoped (facets and/or a text query), a `vega-lite` render kind
    must carry its spec template, and a window is a positive day count.
    """

    name: str
    facets: dict[str, str]
    query: str | None = None
    window_days: int | None = None
    columns: list[str] = field(default_factory=list[str])
    render_kind: PanelRenderKind = "table"
    vega_lite_spec: str | None = None
    position: int = 0


@dataclass(frozen=True)
class _NormalisedSpec:
    """A `PanelSpec` after validation and whitespace normalisation."""

    name: str
    facets: dict[str, str]
    query: str | None
    window_days: int | None
    columns: list[str]
    render_kind: PanelRenderKind
    vega_lite_spec: str | None
    position: int


def _normalise_spec(spec: PanelSpec) -> _NormalisedSpec:
    """Validate a spec, rejecting malformed scope or render choices."""
    name = spec.name.strip()
    if not name:
        message = "panel name must not be blank"
        raise InvalidPanelSpecError(message)
    query = spec.query.strip() if spec.query is not None else None
    if not query:
        query = None
    if not spec.facets and query is None:
        message = "panel must be scoped by facets and/or a text query"
        raise InvalidPanelSpecError(message)
    if spec.window_days is not None and spec.window_days < 1:
        message = "panel window must be a positive day count"
        raise InvalidPanelSpecError(message)
    vega_lite_spec = (
        spec.vega_lite_spec.strip() if spec.vega_lite_spec is not None else None
    )
    if not vega_lite_spec:
        vega_lite_spec = None
    if spec.render_kind == "vega-lite" and vega_lite_spec is None:
        message = "a vega-lite panel requires a stored spec template"
        raise InvalidPanelSpecError(message)
    return _NormalisedSpec(
        name=name,
        facets=dict(spec.facets),
        query=query,
        window_days=spec.window_days,
        columns=list(spec.columns),
        render_kind=spec.render_kind,
        vega_lite_spec=vega_lite_spec,
        position=spec.position,
    )


def _default_render_kind() -> PanelRenderKind:
    """The render kind a panel starts with: the plain Tether-styled table."""
    return "table"


class SyntheticPanel[S = Pending](Model[S, "SyntheticPanel[Fetched]"]):
    """A saved faceted query over the Commons plus its render choice."""

    id: SyntheticPanel.GenCol[UUID7] = Text(
        primary_key=True,
        default_factory=uuid7,
    )
    name: SyntheticPanel.Col[str] = Text()
    """The human-facing panel title."""
    facets: SyntheticPanel.Col[Json[dict[str, str]]] = Text(
        default_factory=dict[str, str]
    )
    """The exact-match AND facet filter, same semantics as Memory search."""
    query: SyntheticPanel.Col[str | None] = Text(default=None, nullable=True)
    """Optional text query; when present, results ride hybrid Search's ranking."""
    window_days: SyntheticPanel.Col[int | None] = Integer(default=None, nullable=True)
    """Optional relative window bounding `tethered_at`, resolved at query time."""
    columns: SyntheticPanel.Col[Json[list[str]]] = Text(default_factory=list[str])
    """Facet keys shown as table columns beside the Memory content."""
    render_kind: SyntheticPanel.Col[PanelRenderKind] = Text(
        default_factory=_default_render_kind
    )
    """`table` renders rows directly; `vega-lite` injects them into the template."""
    vega_lite_spec: SyntheticPanel.Col[str | None] = Text(default=None, nullable=True)
    """The stored Vega-Lite spec template for the `vega-lite` render kind."""
    position: SyntheticPanel.Col[int] = Integer(default=0)
    """Explicit sort position; the panel column never reshuffles on its own."""
    version: SyntheticPanel.Col[PositiveInt] = Integer(default=1)
    """Version number used for optimistic concurrency control."""
    created_at: SyntheticPanel.GenCol[datetime] = Text(default=CurrentTimestamp)
    updated_at: SyntheticPanel.GenCol[datetime] = Text(default=CurrentTimestamp)
    deleted_at: SyntheticPanel.Col[datetime | None] = Text(
        default=None,
        nullable=True,
    )

    __indexes__: ClassVar = [Index(deleted_at, position)]


@dataclass(frozen=True)
class PanelResults:
    """One execution of a panel: the capped rows plus the uncapped match count."""

    memories: list[Memory[Fetched]]
    total: int


class PanelService:
    """Capability surface for Synthetic panels, over a snekql database.

    Mutations own one transaction each and return the resulting row; `execute`
    owns no panel state at all — it recomputes through the Memory search seam
    on every call (ADR 0006).
    """

    def __init__(
        self,
        database: Database,
        memory_service: MemoryService,
        tracer: Tracer,
        event_publisher: EventPublisher | None = None,
    ) -> None:
        self.database: Database = database
        self.memory_service: MemoryService = memory_service
        self.event_publisher: EventPublisher = event_publisher or NullEventPublisher()
        self.tracer: Tracer = tracer

    async def create(
        self, spec: PanelSpec, *, logger: Logger
    ) -> SyntheticPanel[Fetched]:
        """Create a panel, validating its scope and render choice first."""
        normalised = _normalise_spec(spec)
        with self.tracer.start_as_current_span(
            "PanelService.create",
            attributes={"panel.render_kind": normalised.render_kind},
        ) as span:
            _debug(logger, "Creating Synthetic panel", name=normalised.name)

            async def _create(tx: Transaction) -> SyntheticPanel[Fetched]:
                return await tx.execute(
                    insert(
                        SyntheticPanel(
                            name=normalised.name,
                            facets=normalised.facets,
                            query=normalised.query,
                            window_days=normalised.window_days,
                            columns=normalised.columns,
                            render_kind=normalised.render_kind,
                            vega_lite_spec=normalised.vega_lite_spec,
                            position=normalised.position,
                        )
                    ).returning()
                )

            panel = await run_in_transaction(self.database, _create)
            span.set_attribute("panel.id", str(panel.id))
            _info(
                logger,
                "Synthetic panel created",
                panel_id=str(panel.id),
                name=panel.name,
            )
        await self.event_publisher.publish(InvalidateEvent(keys=["panels"]))
        return panel

    async def list_panels(self, *, logger: Logger) -> list[SyntheticPanel[Fetched]]:
        """List live panels in explicit position order (creation breaks ties)."""
        _debug(logger, "Listing Synthetic panels")
        query = (
            select(SyntheticPanel)
            .where(SyntheticPanel.deleted_at.is_null())
            .order_by(SyntheticPanel.position.asc())
            .order_by(SyntheticPanel.created_at.asc())
        )
        async with self.database.transaction() as tx:
            panels = await tx.fetch_all(query)
        _debug(logger, "Synthetic panel list completed", result_count=len(panels))
        return panels

    async def update(
        self,
        panel: SyntheticPanel[Fetched],
        spec: PanelSpec,
        *,
        logger: Logger,
    ) -> SyntheticPanel[Fetched]:
        """Replace a panel's definition at an observed version.

        A stale observed version conflicts; an absent or deleted panel raises.
        """
        normalised = _normalise_spec(spec)
        _debug(
            logger,
            "Updating Synthetic panel",
            panel_id=str(panel.id),
            observed_version=panel.version,
        )

        async def _update(tx: Transaction) -> SyntheticPanel[Fetched]:
            matched = await tx.execute(
                update(SyntheticPanel)
                .set(SyntheticPanel.name.to(normalised.name))
                .set(SyntheticPanel.facets.to(normalised.facets))
                .set(SyntheticPanel.query.to(normalised.query))
                .set(SyntheticPanel.window_days.to(normalised.window_days))
                .set(SyntheticPanel.columns.to(normalised.columns))
                .set(SyntheticPanel.render_kind.to(normalised.render_kind))
                .set(SyntheticPanel.vega_lite_spec.to(normalised.vega_lite_spec))
                .set(SyntheticPanel.position.to(normalised.position))
                .set(SyntheticPanel.version.to(panel.version + 1))
                .set(SyntheticPanel.updated_at.to(CurrentTimestamp))
                .where(SyntheticPanel.id.eq(panel.id))
                .where(SyntheticPanel.deleted_at.is_null())
                .where(SyntheticPanel.version.eq(panel.version))
            )
            fresh = await self._fetch_live(tx, panel.id)
            if matched == 0:
                raise PanelConflictError(panel.id)
            return fresh

        fresh = await run_in_transaction(self.database, _update)
        _info(
            logger,
            "Synthetic panel updated",
            panel_id=str(fresh.id),
            version=fresh.version,
        )
        await self.event_publisher.publish(InvalidateEvent(keys=["panels"]))
        return fresh

    async def delete(
        self,
        panel: SyntheticPanel[Fetched],
        *,
        logger: Logger,
    ) -> SyntheticPanel[Fetched]:
        """Soft-delete a panel at an observed version, convergently.

        Deleting an already-deleted panel is a no-op, not an error. A stale
        observed version on a still-live panel conflicts; an absent one raises.
        """
        _debug(
            logger,
            "Deleting Synthetic panel",
            panel_id=str(panel.id),
            observed_version=panel.version,
        )

        async def _delete(tx: Transaction) -> SyntheticPanel[Fetched]:
            current = await tx.fetch_one_or_none(
                select(SyntheticPanel).where(SyntheticPanel.id.eq(panel.id))
            )
            if current is None:
                raise PanelNotFoundError(panel.id)
            if current.deleted_at is not None:
                return current
            matched = await tx.execute(
                update(SyntheticPanel)
                .set(SyntheticPanel.deleted_at.to(CurrentTimestamp))
                .set(SyntheticPanel.version.to(panel.version + 1))
                .set(SyntheticPanel.updated_at.to(CurrentTimestamp))
                .where(SyntheticPanel.id.eq(panel.id))
                .where(SyntheticPanel.deleted_at.is_null())
                .where(SyntheticPanel.version.eq(panel.version))
            )
            current = await tx.fetch_one_or_none(
                select(SyntheticPanel).where(SyntheticPanel.id.eq(panel.id))
            )
            assert current is not None
            if matched == 0:
                raise PanelConflictError(panel.id)
            return current

        current = await run_in_transaction(self.database, _delete)
        _info(logger, "Synthetic panel deleted", panel_id=str(current.id))
        await self.event_publisher.publish(InvalidateEvent(keys=["panels"]))
        return current

    async def fetch(self, panel_id: UUID7) -> SyntheticPanel[Fetched]:
        """Fetch a live panel by id, or raise when absent or deleted."""
        async with self.database.transaction() as tx:
            return await self._fetch_live(tx, panel_id)

    async def execute(
        self,
        panel: SyntheticPanel[Fetched],
        *,
        now: datetime,
        limit: PositiveInt = EXECUTE_DEFAULT_LIMIT,
        logger: Logger,
    ) -> PanelResults:
        """Run a panel's saved query against the trusted corpus, capped.

        The relative window resolves against the caller's `now` on every call,
        never at save time (ADR 0006). With a text query, ranking comes from
        hybrid Search's candidates and the facet/window filter lands in
        `hydrate_tethered`; without one, the corpus listing is recency-of-trust
        ordered with the same facet semantics applied post-fetch.
        """
        after = (
            now - timedelta(days=panel.window_days)
            if panel.window_days is not None
            else None
        )
        with self.tracer.start_as_current_span(
            "PanelService.execute",
            attributes={"panel.id": str(panel.id)},
        ) as span:
            _debug(
                logger,
                "Executing Synthetic panel",
                panel_id=str(panel.id),
                name=panel.name,
            )
            if panel.query is not None:
                matches = await self._execute_search(
                    panel.query, panel.facets, after=after, logger=logger
                )
            else:
                matches = await self._execute_listing(
                    panel.facets, after=after, logger=logger
                )
            span.set_attribute("panel.execute.total", len(matches))
            _debug(
                logger,
                "Synthetic panel execution completed",
                panel_id=str(panel.id),
                total=len(matches),
            )
            return PanelResults(memories=matches[:limit], total=len(matches))

    async def _execute_search(
        self,
        query: str,
        facets: dict[str, str],
        *,
        after: datetime | None,
        logger: Logger,
    ) -> list[Memory[Fetched]]:
        """The text-query arm: Search's candidates, re-filtered and rank-ordered."""
        candidates = await self.memory_service.search_candidates(
            query, limit=_SEARCH_CANDIDATE_LIMIT, logger=logger
        )
        if not candidates:
            return []
        rank = {candidate.id: position for position, candidate in enumerate(candidates)}
        memories = await self.memory_service.hydrate_tethered(
            list(rank),
            facets=facets or None,
            after=after,
            logger=logger,
        )
        memories.sort(key=lambda memory: rank[memory.id])
        return memories

    async def _execute_listing(
        self,
        facets: dict[str, str],
        *,
        after: datetime | None,
        logger: Logger,
    ) -> list[Memory[Fetched]]:
        """The facets-only arm: the trusted corpus, most recently tethered first."""
        query = MemoryService.tethered_corpus().order_by(Memory.tethered_at.desc())
        if after is not None:
            query = query.where(Memory.tethered_at.gte(after))
        async with self.database.transaction() as tx:
            memories = await tx.fetch_all(query)
        _ = logger
        if facets:
            memories = [
                memory
                for memory in memories
                if all(memory.facets.get(key) == value for key, value in facets.items())
            ]
        return memories

    async def _fetch_live(
        self, tx: Transaction, panel_id: UUID7
    ) -> SyntheticPanel[Fetched]:
        """Fetch a live panel inside a transaction, raising on absence."""
        panel = await tx.fetch_one_or_none(
            select(SyntheticPanel)
            .where(SyntheticPanel.id.eq(panel_id))
            .where(SyntheticPanel.deleted_at.is_null())
        )
        if panel is None:
            raise PanelNotFoundError(panel_id)
        return panel


async def create_panel_schema(database: Database) -> None:
    """Create the Synthetic panel table and its index on an initialized DB.

    Applied as its own ordered migrations after the earlier schemas (the
    artifact chain took `011_`).

    >>> database = await Database.initialize(backend=Config(database=":memory:"))
    >>> await create_panel_schema(database)
    """
    migrations = {
        f"012_{label}": sql
        for label, sql in scaffold_sqlite_statements([SyntheticPanel])
    }
    await database.migrate(migrations)
