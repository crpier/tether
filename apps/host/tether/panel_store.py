"""SQLite model and historical schema chain for Synthetic panels."""

from __future__ import annotations

from typing import ClassVar
from uuid import uuid7

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
    UtcDatetime,
)
from snekql.sqlite._schema_ddl import scaffold_sqlite_statements

from tether.panel_model import PanelRenderKind


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
    created_at: SyntheticPanel.GenCol[UtcDatetime] = Text(default=CurrentTimestamp)
    updated_at: SyntheticPanel.GenCol[UtcDatetime] = Text(default=CurrentTimestamp)
    deleted_at: SyntheticPanel.Col[UtcDatetime | None] = Text(
        default=None,
        nullable=True,
    )

    __indexes__: ClassVar = [Index(deleted_at, position)]


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
