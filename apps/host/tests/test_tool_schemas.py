"""Behavior tests for generated pi tool-schema source documents."""

from typing import Any, cast

from snektest import assert_eq, assert_in, test

from tether.tool_schemas import build_tool_schema_document


@test()
def tool_schema_document_describes_the_internal_tools() -> None:
    """The codegen source document exposes each tool with endpoint and schema."""
    document = build_tool_schema_document()

    tools = {tool["name"]: tool for tool in document["tools"]}

    assert_eq(
        set(tools),
        {
            "capture",
            "browse",
            "search",
            "review_digest",
            "tether",
            "edit",
            "reject",
            "add_movie",
            "add_place",
            "complete_bucket_item",
            "delete_bucket_item",
            "search_bucket_items",
        },
    )
    capture_schema = cast("dict[str, Any]", tools["capture"]["schema"])
    browse_schema = cast("dict[str, Any]", tools["browse"]["schema"])
    search_schema = cast("dict[str, Any]", tools["search"]["schema"])
    tether_schema = cast("dict[str, Any]", tools["tether"]["schema"])

    assert_eq(tools["capture"]["endpoint"], "/internal/tools/capture")
    assert_eq(tools["capture"]["params_model"], "CaptureParams")
    assert_eq(
        capture_schema["properties"]["content"], {"$ref": "#/$defs/MemoryContent"}
    )
    assert_eq(
        browse_schema["$defs"]["MemoryState"]["enum"],
        ["loose", "tethered"],
    )
    assert_eq(search_schema["properties"]["limit"]["default"], 50)
    assert_in("memory_id", tether_schema["required"])


@test()
def add_movie_tool_carries_its_typed_optional_field() -> None:
    """A per-type Add tool exposes its item type's own (optional) fields."""
    document = build_tool_schema_document()

    tools = {tool["name"]: tool for tool in document["tools"]}
    add_movie_schema = cast("dict[str, Any]", tools["add_movie"]["schema"])

    assert_eq(tools["add_movie"]["endpoint"], "/internal/tools/add_movie")
    assert_in("title", add_movie_schema["required"])
    assert_in("intent_context", add_movie_schema["required"])
    # `year` is optional: present as a property, absent from `required`.
    assert_in("year", add_movie_schema["properties"])
    assert_eq("year" in add_movie_schema["required"], False)
