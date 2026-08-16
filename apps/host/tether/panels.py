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

>>> service = PanelService(database=db, executor=executor, tracer=tracer)
>>> panel = await service.create(
...     PanelSpec(name="finance", facets={"domain": "finance"}), logger=logger
... )
>>> results = await service.execute(panel, now=datetime.now(UTC), logger=logger)
>>> results.total
0
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from opentelemetry.trace import Tracer
from pydantic import UUID7, PositiveInt
from snekql.sqlite import (
    CurrentTimestamp,
    Database,
    Fetched,
    Transaction,
    insert,
    select,
    update,
)

from tether.events import EventPublisher, InvalidateEvent, NullEventPublisher
from tether.panel_errors import (
    InvalidPanelSpecError,
    PanelConflictError,
    PanelNotFoundError,
)
from tether.panel_execution import PanelExecutionPort, PanelResults
from tether.panel_model import EXECUTE_DEFAULT_LIMIT, PanelRenderKind, PanelSpec
from tether.panel_store import SyntheticPanel
from tether.structured_logging import Logger


def _debug(logger: Logger, event: str, **context: object) -> None:
    """Emit a debug event using caller-supplied logging context."""
    logger.debug(event, **context)


def _info(logger: Logger, event: str, **context: object) -> None:
    """Emit an info event using caller-supplied logging context."""
    logger.info(event, **context)


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


class PanelService:
    """Capability surface for Synthetic panels, over a snekql database.

    Mutations own one transaction each and return the resulting row; `execute`
    owns no panel state at all — it recomputes through the Memory search seam
    on every call (ADR 0006).
    """

    def __init__(
        self,
        database: Database,
        executor: PanelExecutionPort,
        tracer: Tracer,
        event_publisher: EventPublisher | None = None,
    ) -> None:
        self.database: Database = database
        self.executor: PanelExecutionPort = executor
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

            async with self.database.transaction(mode="immediate") as tx:
                panel = await _create(tx)
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

        async with self.database.transaction(mode="immediate") as tx:
            fresh = await _update(tx)
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

        async with self.database.transaction(mode="immediate") as tx:
            current = await _delete(tx)
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
        """Run a panel's saved query against the trusted corpus, capped."""
        return await self.executor.execute(panel, now=now, limit=limit, logger=logger)

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
