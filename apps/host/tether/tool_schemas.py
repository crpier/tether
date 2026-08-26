"""Emit the Pydantic schema document used by pi tool-shim codegen."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import TypedDict, cast

from tether.tool_registry import all_tool_specs

type JsonValue = (
    None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]
)


class ToolSchema(TypedDict):
    """One internal tool's schema and loopback endpoint."""

    endpoint: str
    name: str
    params_model: str
    schema: dict[str, JsonValue]


class ToolSchemaDocument(TypedDict):
    """The JSON artifact consumed by the TypeScript shim generator."""

    tools: list[ToolSchema]


def build_tool_schema_document() -> ToolSchemaDocument:
    """Build the schema artifact from the live `ToolSpec` registry.

    Each tool's endpoint, name, and params come straight off the same
    `ToolSpec` that mounts its route, so the generated shims can't drift from
    the surface they call.
    """
    return {
        "tools": [
            {
                "endpoint": spec.endpoint,
                "name": spec.name,
                "params_model": spec.params_model.__name__,
                "schema": cast(
                    "dict[str, JsonValue]", spec.params_model.model_json_schema()
                ),
            }
            for spec in all_tool_specs()
        ]
    }


def main(argv: list[str] | None = None) -> int:
    """Write the tool schema document to stdout or a target path."""
    args = sys.argv[1:] if argv is None else argv
    encoded = json.dumps(build_tool_schema_document(), indent=2) + "\n"
    if len(args) == 0:
        _ = sys.stdout.write(encoded)
        return 0
    if len(args) == 1:
        target_path = Path(args[0])
        target_path.parent.mkdir(parents=True, exist_ok=True)
        _ = target_path.write_text(encoded)
        return 0
    _ = sys.stderr.write("usage: python -m tether.tool_schemas [output-path]\n")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
