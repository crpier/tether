"""Emit the Pydantic schema document used by pi tool-shim codegen."""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict, cast

from pydantic import BaseModel

from tether.bucket_tools import (
    AddMovieParams,
    AddPlaceParams,
    CompleteBucketItemParams,
    DeleteBucketItemParams,
    SearchBucketItemsParams,
)
from tether.tools import (
    BrowseParams,
    CaptureParams,
    EditParams,
    RejectParams,
    ReviewDigestParams,
    SearchParams,
    TetherParams,
)

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


@dataclass(frozen=True, slots=True)
class ToolSchemaSpec:
    """The stable metadata that Pydantic does not know about."""

    endpoint: str
    name: str
    params_model: type[BaseModel]


TOOL_SCHEMA_SPECS = (
    ToolSchemaSpec("/internal/tools/capture", "capture", CaptureParams),
    ToolSchemaSpec("/internal/tools/browse", "browse", BrowseParams),
    ToolSchemaSpec("/internal/tools/search", "search", SearchParams),
    ToolSchemaSpec(
        "/internal/tools/review_digest", "review_digest", ReviewDigestParams
    ),
    ToolSchemaSpec("/internal/tools/tether", "tether", TetherParams),
    ToolSchemaSpec("/internal/tools/edit", "edit", EditParams),
    ToolSchemaSpec("/internal/tools/reject", "reject", RejectParams),
    ToolSchemaSpec("/internal/tools/add_movie", "add_movie", AddMovieParams),
    ToolSchemaSpec("/internal/tools/add_place", "add_place", AddPlaceParams),
    ToolSchemaSpec(
        "/internal/tools/complete_bucket_item",
        "complete_bucket_item",
        CompleteBucketItemParams,
    ),
    ToolSchemaSpec(
        "/internal/tools/delete_bucket_item",
        "delete_bucket_item",
        DeleteBucketItemParams,
    ),
    ToolSchemaSpec(
        "/internal/tools/search_bucket_items",
        "search_bucket_items",
        SearchBucketItemsParams,
    ),
)
"""Internal Memory and Bucket item tools exposed to pi, in generated-file order."""


def build_tool_schema_document() -> ToolSchemaDocument:
    """Build the schema artifact from the live Pydantic param models."""
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
            for spec in TOOL_SCHEMA_SPECS
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
