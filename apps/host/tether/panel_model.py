"""Domain values and saved-query input for Synthetic panels."""

from dataclasses import dataclass, field
from typing import Literal

type PanelRenderKind = Literal["table", "vega-lite"]
"""How a panel's results render: a table or stored Vega-Lite template."""

EXECUTE_DEFAULT_LIMIT = 20
"""Rows a panel shows by default; `total` still counts every match."""


@dataclass(frozen=True)
class PanelSpec:
    """The saved query and render choice carried by create and update."""

    name: str
    facets: dict[str, str]
    query: str | None = None
    window_days: int | None = None
    columns: list[str] = field(default_factory=list[str])
    render_kind: PanelRenderKind = "table"
    vega_lite_spec: str | None = None
    position: int = 0
