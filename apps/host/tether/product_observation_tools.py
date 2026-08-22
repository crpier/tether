"""Foreground tools for recording and listing Product observations."""

from pydantic import BaseModel
from starlette.routing import Route

from tether.capabilities import bind_params
from tether.product_observation_capabilities import (
    PRODUCT_OBSERVATION_ERRORS,
    list_open,
    record,
)
from tether.tool_runtime import ToolSpec


class RecordProductObservationParams(BaseModel):
    """Interpretation of product feedback explicitly requested by the user.

    State the expected product behavior concisely. The host preserves the exact
    active user Message and Conversation provenance without trusting copied text.
    Never call this tool merely because an interaction might be improved; the
    user must explicitly ask to record product feedback.
    """

    interpretation: str


class ListProductObservationsParams(BaseModel):
    """No-input request for unresolved Product observations."""


PRODUCT_OBSERVATION_TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        "record_product_observation",
        RecordProductObservationParams,
        bind_params(record),
        PRODUCT_OBSERVATION_ERRORS,
    ),
    ToolSpec(
        "list_product_observations",
        ListProductObservationsParams,
        bind_params(list_open),
        PRODUCT_OBSERVATION_ERRORS,
    ),
)
"""Product-observation capabilities exposed to foreground chat."""


def internal_product_observation_tool_routes() -> list[Route]:
    """Mount Product-observation tools under `/internal/tools/*`."""
    return [spec.route() for spec in PRODUCT_OBSERVATION_TOOL_SPECS]
