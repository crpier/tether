"""The fixed first-release tool selection exposed to Open WebUI."""

from tether.bucket_tools import BUCKET_TOOL_SPECS
from tether.health_connect import HEALTH_CONNECT_TOOL_SPECS
from tether.todo_tools import TODO_TOOL_SPECS
from tether.tool_runtime import ToolSpec
from tether.triage_tools import TRIAGE_TOOL_SPECS

SELECTED_TOOL_NAMES = (
    "add_movie",
    "add_place",
    "add_book",
    "add_travel",
    "add_purchase",
    "complete_bucket_item",
    "search_bucket_items",
    "set_purchase_decision",
    "set_bucket_item_intent",
    "triage_report",
    "create_todo",
    "list_todos",
    "set_todo_status",
    "analyze_health_connect",
    "health_connect_inventory",
    "query_health_connect",
    "summarize_health_connect",
)
"""Tool names published by the Open WebUI OpenAPI document, in domain order."""


def selected_tool_specs() -> tuple[ToolSpec, ...]:
    """Return the existing specs selected for the Open WebUI integration."""
    available_specs = {
        spec.name: spec
        for spec in (
            *BUCKET_TOOL_SPECS,
            *TRIAGE_TOOL_SPECS,
            *TODO_TOOL_SPECS,
            *HEALTH_CONNECT_TOOL_SPECS,
        )
    }
    return tuple(available_specs[name] for name in SELECTED_TOOL_NAMES)


__all__ = ["SELECTED_TOOL_NAMES", "selected_tool_specs"]
