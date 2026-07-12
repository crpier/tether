"""The internal Scheduled-trigger tool surface, over the shared envelope.

These mount alongside the Memory and Bucket tools under `/internal/tools/*` —
the loopback seam a pi process calls back into — reusing the same auth gate,
params-to-envelope validation, and rule-driven domain-error translation
(`tether.tools`). The capability executes live in
`tether.trigger_capabilities`, shared with the REST routes; this module only
names each tool's params model and mounts it.

The agent can set up a reminder (`create_trigger`), see what is scheduled
(`list_triggers`), and cancel one (`delete_trigger`). Editing an existing
trigger's definition is left to the REST/UI surface, where optimistic-
concurrency on a freshly-read version is natural.
"""

from __future__ import annotations

from pydantic import UUID7, BaseModel, PositiveInt
from starlette.requests import Request
from starlette.routing import Route

from tether import trigger_capabilities
from tether.capabilities import CapabilityOutcome, bind_params
from tether.tools import ToolSpec
from tether.trigger_capabilities import TRIGGER_ERRORS, TriggerSpecBody


class CreateTriggerParams(TriggerSpecBody):
    """Params for scheduling a trigger.

    `once` carries an absolute `fire_at`; `daily`/`weekly` carry `timezone` and
    `time_of_day` (and a `weekday` for weekly). Mismatched fields are rejected
    as a well-formed `invalid_input` envelope, never a corrupt row.
    """


class ListTriggersParams(BaseModel):
    """Params for listing live triggers, capped at `limit` (soonest first)."""

    limit: PositiveInt = 50


class DeleteTriggerParams(BaseModel):
    """Params for deleting a trigger at an observed version."""

    trigger_id: UUID7
    version: PositiveInt


async def _create_trigger(
    request: Request, params: CreateTriggerParams
) -> CapabilityOutcome:
    """Project the flat tool params onto the shared create capability."""
    return await trigger_capabilities.create(request, params.to_spec())


TRIGGER_TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec("create_trigger", CreateTriggerParams, _create_trigger, TRIGGER_ERRORS),
    ToolSpec(
        "list_triggers",
        ListTriggersParams,
        bind_params(trigger_capabilities.list_triggers),
        TRIGGER_ERRORS,
    ),
    ToolSpec(
        "delete_trigger",
        DeleteTriggerParams,
        bind_params(trigger_capabilities.delete),
        TRIGGER_ERRORS,
    ),
)
"""The Scheduled-trigger capabilities exposed as internal tools, in order."""


def internal_trigger_tool_routes() -> list[Route]:
    """Mount the trigger capabilities as `/internal/tools/*` POST endpoints.

    Returned separately from the public trigger routes so they stay absent from
    the public OpenAPI document and generated client.
    """
    return [spec.route() for spec in TRIGGER_TOOL_SPECS]
