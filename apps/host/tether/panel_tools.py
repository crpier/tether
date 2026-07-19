"""The internal Synthetic-panel tool surface, over the shared envelope.

These mount alongside the Memory and Trigger tools under `/internal/tools/*` —
the loopback seam a pi process calls back into — reusing the same auth gate,
params-to-envelope validation, and rule-driven domain-error translation
(`tether.tools`). The capability executes live in `tether.panel_capabilities`,
shared with the REST routes; this module only names each tool's params model
and mounts it.

The agent can assemble a panel from the facet vocabulary it already invented
(`create_panel`, informed by `facet_overview`), see what exists
(`list_panels`), adjust one (`update_panel`), and scrap one (`delete_panel`) —
Proposal-lite, like facet curation: presentation-only state the human can
always delete. Executing a panel is left to the web surface; the agent
searches the Commons directly instead of reading a panel back.
"""

from __future__ import annotations

from pydantic import UUID7, BaseModel, PositiveInt
from starlette.requests import Request
from starlette.routing import Route

from tether import panel_capabilities
from tether.capabilities import CapabilityOutcome, bind_params
from tether.panel_capabilities import PANEL_ERRORS, PanelSpecBody
from tether.tools import ToolSpec


class CreatePanelParams(PanelSpecBody):
    """Params for saving a Synthetic panel.

    A panel must be scoped by `facets` and/or a text `query`; a `vega-lite`
    render kind must carry its `vega_lite_spec` template. A malformed spec is
    rejected as a well-formed `invalid_input` envelope, never a corrupt row.
    """


class ListPanelsParams(BaseModel):
    """Params for listing live panels, in position order."""


class UpdatePanelParams(PanelSpecBody):
    """Params for replacing a panel's definition at an observed version."""

    panel_id: UUID7
    version: PositiveInt


class DeletePanelParams(BaseModel):
    """Params for deleting a panel at an observed version."""

    panel_id: UUID7
    version: PositiveInt


async def _create_panel(
    request: Request, params: CreatePanelParams
) -> CapabilityOutcome:
    """Project the flat tool params onto the shared create capability."""
    return await panel_capabilities.create(request, params.to_spec())


async def _update_panel(
    request: Request, params: UpdatePanelParams
) -> CapabilityOutcome:
    """Project the flat tool params onto the shared update capability."""
    return await panel_capabilities.update(
        request, params.panel_id, params.to_spec(), params.version
    )


PANEL_TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec("create_panel", CreatePanelParams, _create_panel, PANEL_ERRORS),
    ToolSpec(
        "list_panels",
        ListPanelsParams,
        bind_params(panel_capabilities.list_panels),
        PANEL_ERRORS,
    ),
    ToolSpec("update_panel", UpdatePanelParams, _update_panel, PANEL_ERRORS),
    ToolSpec(
        "delete_panel",
        DeletePanelParams,
        bind_params(panel_capabilities.delete),
        PANEL_ERRORS,
    ),
)
"""The Synthetic-panel capabilities exposed as internal tools, in order."""


def internal_panel_tool_routes() -> list[Route]:
    """Mount the panel capabilities as `/internal/tools/*` POST endpoints.

    Returned separately from the public panel routes so they stay absent from
    the public OpenAPI document and generated client.
    """
    return [spec.route() for spec in PANEL_TOOL_SPECS]
