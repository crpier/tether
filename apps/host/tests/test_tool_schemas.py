"""Behavior tests for generated pi tool-schema source documents."""

from typing import Any, cast

from snektest import assert_eq, assert_in, test

from tether.bucket_tools import internal_bucket_tool_routes
from tether.recall_tools import internal_recall_tool_routes
from tether.tool_schemas import build_tool_schema_document
from tether.tools import internal_tool_routes
from tether.triage_tools import internal_triage_tool_routes
from tether.trigger_tools import internal_trigger_tool_routes
from tether.youtube_tools import internal_youtube_tool_routes


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
            "add_book",
            "add_travel",
            "complete_bucket_item",
            "delete_bucket_item",
            "search_bucket_items",
            "triage_report",
            "browse_youtube",
            "search_youtube",
            "fetch_youtube_transcript",
            "ignore_youtube_video",
            "retry_youtube_video",
            "create_trigger",
            "list_triggers",
            "delete_trigger",
            "start_recall",
            "list_due_recall_prompts",
            "answer_recall_prompt",
            "propose_essay_grade",
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
def schema_document_covers_every_mounted_tool_route() -> None:
    """The codegen document describes exactly the tools the host mounts.

    Routes and schema entries both derive from the one `ToolSpec` registry, so a
    tool can never be mounted without a generated shim (or shimmed without a
    live endpoint) — the drift the split spec list used to permit.
    """
    mounted_endpoints = {
        route.path
        for routes in (
            internal_tool_routes(),
            internal_bucket_tool_routes(),
            internal_triage_tool_routes(),
            internal_youtube_tool_routes(),
            internal_trigger_tool_routes(),
            internal_recall_tool_routes(),
        )
        for route in routes
    }
    document_endpoints = {
        tool["endpoint"] for tool in build_tool_schema_document()["tools"]
    }

    assert_eq(mounted_endpoints, document_endpoints)


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


@test()
def add_book_tool_carries_its_typed_optional_field() -> None:
    """The book Add tool exposes its item type's own (optional) fields."""
    document = build_tool_schema_document()

    tools = {tool["name"]: tool for tool in document["tools"]}
    add_book_schema = cast("dict[str, Any]", tools["add_book"]["schema"])

    assert_eq(tools["add_book"]["endpoint"], "/internal/tools/add_book")
    assert_in("title", add_book_schema["required"])
    assert_in("intent_context", add_book_schema["required"])
    # `author` is optional: present as a property, absent from `required`.
    assert_in("author", add_book_schema["properties"])
    assert_eq("author" in add_book_schema["required"], False)


@test()
def add_travel_tool_carries_its_typed_optional_field() -> None:
    """The travel Add tool exposes its item type's own (optional) fields."""
    document = build_tool_schema_document()

    tools = {tool["name"]: tool for tool in document["tools"]}
    add_travel_schema = cast("dict[str, Any]", tools["add_travel"]["schema"])

    assert_eq(tools["add_travel"]["endpoint"], "/internal/tools/add_travel")
    assert_in("destination", add_travel_schema["required"])
    assert_in("intent_context", add_travel_schema["required"])
    # `season` is optional: present as a property, absent from `required`.
    assert_in("season", add_travel_schema["properties"])
    assert_eq("season" in add_travel_schema["required"], False)
