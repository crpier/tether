"""The Synthetic panel domain's capability descriptor.

The pieces the REST routes (`tether.panel_routes`) and the internal tools
(`tether.panel_tools`) both need live here once: the `PanelRead` model, the
shared spec body (`PanelSpecBody`), the detached-reference builder, the
domain→code map (`PANEL_ERRORS`), and one execute function per capability —
the service call plus its Read-model rendering. Panel *results* render each
Memory through the Memory domain's own `MemoryRead`, so the two surfaces never
grow a second Memory shape.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast
from uuid import UUID

from pydantic import BaseModel, PositiveInt
from starlette.requests import Request

from tether.capabilities import CapabilityOutcome, ErrorRule
from tether.logging import get_request_logger
from tether.memory_capabilities import MemoryRead
from tether.panels import (
    EXECUTE_DEFAULT_LIMIT,
    Fetched,
    InvalidPanelSpecError,
    PanelConflictError,
    PanelNotFoundError,
    PanelRenderKind,
    PanelSpec,
    SyntheticPanel,
)

PANEL_ERRORS: tuple[ErrorRule, ...] = (
    ErrorRule((PanelNotFoundError,), "not_found", 404, detail="panel not found"),
    ErrorRule((PanelConflictError,), "conflict", 409),
    ErrorRule((InvalidPanelSpecError,), "invalid_input", 422),
)
"""The panel domain→code map both surfaces translate failures through."""


class PanelSpecBody(BaseModel):
    """The shared saved-query + render fields for creating or updating a panel.

    >>> PanelSpecBody(name="finance", facets={"domain": "finance"}).render_kind
    'table'
    """

    name: str
    facets: dict[str, str]
    query: str | None = None
    window_days: PositiveInt | None = None
    columns: list[str] = []
    render_kind: PanelRenderKind = "table"
    vega_lite_spec: str | None = None
    position: int = 0

    def to_spec(self) -> PanelSpec:
        """Project the validated fields onto the service's `PanelSpec`."""
        return PanelSpec(
            name=self.name,
            facets=self.facets,
            query=self.query,
            window_days=self.window_days,
            columns=self.columns,
            render_kind=self.render_kind,
            vega_lite_spec=self.vega_lite_spec,
            position=self.position,
        )


class PanelRead(BaseModel):
    """HTTP representation of a Synthetic panel."""

    id: UUID
    name: str
    facets: dict[str, str]
    query: str | None
    window_days: int | None
    columns: list[str]
    render_kind: PanelRenderKind
    vega_lite_spec: str | None
    position: int
    version: PositiveInt
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_panel(cls, panel: SyntheticPanel[Fetched]) -> PanelRead:
        """Render a stored panel as its HTTP representation."""
        return cls(
            id=panel.id,
            name=panel.name,
            facets=panel.facets,
            query=panel.query,
            window_days=panel.window_days,
            columns=panel.columns,
            render_kind=panel.render_kind,
            vega_lite_spec=panel.vega_lite_spec,
            position=panel.position,
            version=panel.version,
            created_at=panel.created_at,
            updated_at=panel.updated_at,
        )


class PanelResultsRead(BaseModel):
    """One panel execution: the capped rows plus the uncapped match count."""

    memories: list[MemoryRead]
    total: int


def _panel_reference(panel_id: UUID, version: PositiveInt) -> SyntheticPanel[Fetched]:
    """Build a detached panel carrying only the identity a mutation acts on.

    Update/Delete read just `id` and `version` for their optimistic-concurrency
    check; the other columns are required placeholders with no role here.
    """
    return cast(
        "SyntheticPanel[Fetched]",
        SyntheticPanel.construct(
            id=panel_id,
            version=version,
            name="",
            facets={},
            columns=[],
            render_kind="table",
            position=0,
        ),
    )


def _single(panel: SyntheticPanel[Fetched]) -> CapabilityOutcome:
    """Render a single-panel outcome."""
    return CapabilityOutcome(result=PanelRead.from_panel(panel).model_dump(mode="json"))


async def create(request: Request, spec: PanelSpec) -> CapabilityOutcome:
    """Create a Synthetic panel."""
    panel = await request.app.state.panel_service.create(
        spec,
        logger=get_request_logger(request),
    )
    return _single(panel)


async def list_panels(request: Request) -> CapabilityOutcome:
    """List live Synthetic panels in position order."""
    panels = await request.app.state.panel_service.list_panels(
        logger=get_request_logger(request),
    )
    return CapabilityOutcome(
        result=[PanelRead.from_panel(panel).model_dump(mode="json") for panel in panels]
    )


async def update(
    request: Request, panel_id: UUID, spec: PanelSpec, version: PositiveInt
) -> CapabilityOutcome:
    """Replace a panel's definition at an observed version."""
    panel = await request.app.state.panel_service.update(
        _panel_reference(panel_id, version),
        spec,
        logger=get_request_logger(request),
    )
    return _single(panel)


async def delete(
    request: Request, panel_id: UUID, version: PositiveInt
) -> CapabilityOutcome:
    """Delete a Synthetic panel."""
    panel = await request.app.state.panel_service.delete(
        _panel_reference(panel_id, version),
        logger=get_request_logger(request),
    )
    return _single(panel)


async def execute(
    request: Request,
    panel_id: UUID,
    limit: PositiveInt = EXECUTE_DEFAULT_LIMIT,
) -> CapabilityOutcome:
    """Run a panel's saved query, recomputed against the corpus right now."""
    service = request.app.state.panel_service
    panel = await service.fetch(panel_id)
    results = await service.execute(
        panel,
        now=datetime.now(UTC),
        limit=limit,
        logger=get_request_logger(request),
    )
    return CapabilityOutcome(
        result=PanelResultsRead(
            memories=[MemoryRead.from_memory(memory) for memory in results.memories],
            total=results.total,
        ).model_dump(mode="json")
    )
