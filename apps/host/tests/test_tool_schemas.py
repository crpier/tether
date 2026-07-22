"""Behavior tests for generated pi tool-schema source documents."""

from typing import Any, cast

from snektest import assert_eq, assert_in, assert_not_in, test

from tether.artifact_tools import internal_artifact_tool_routes
from tether.bucket_tools import internal_bucket_tool_routes
from tether.conversation_history_tools import (
    internal_conversation_history_tool_routes,
)
from tether.kosync_tools import internal_kosync_tool_routes
from tether.panel_tools import internal_panel_tool_routes
from tether.proposal_tools import internal_proposal_tool_routes
from tether.recall_tools import internal_recall_tool_routes
from tether.todo_tools import internal_todo_tool_routes
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
            "facet_overview",
            "rename_facet_key",
            "merge_facet_value",
            "add_movie",
            "add_place",
            "add_book",
            "add_travel",
            "complete_bucket_item",
            "delete_bucket_item",
            "search_bucket_items",
            "set_bucket_item_intent",
            "create_todo",
            "set_todo_status",
            "link_todo_trigger",
            "link_todo_memory",
            "list_todos",
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
            "read_conversation_history",
            "create_artifact",
            "update_artifact",
            "list_artifact_events",
            "create_panel",
            "list_panels",
            "update_panel",
            "delete_panel",
            "label_ebook",
            "match_ebook_filename",
            "list_unlabeled_ebooks",
            "propose",
            "list_proposals",
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
def bulk_facet_curation_tools_require_prior_chat_approval_by_description() -> None:
    """`rename_facet_key`/`merge_facet_value` schemas warn the model to ask first.

    These bulk-rewrite every carrying Memory row; the approval gate is a
    convention enforced by what the model reads in its tool description, not by
    the host, so the description text is the load-bearing artifact under test.
    """
    document = build_tool_schema_document()
    tools = {tool["name"]: tool for tool in document["tools"]}

    rename_schema = cast("dict[str, Any]", tools["rename_facet_key"]["schema"])
    merge_schema = cast("dict[str, Any]", tools["merge_facet_value"]["schema"])

    assert_in("approval", cast("str", rename_schema["description"]).lower())
    assert_in("approval", cast("str", merge_schema["description"]).lower())


@test()
def capture_tool_exposes_an_optional_facets_object() -> None:
    """The `capture` schema carries an optional `facets` string-map field."""
    document = build_tool_schema_document()
    tools = {tool["name"]: tool for tool in document["tools"]}
    capture_schema = cast("dict[str, Any]", tools["capture"]["schema"])

    assert_not_in_required(capture_schema, "facets")
    facets_schema = capture_schema["properties"]["facets"]
    non_null_member = next(
        member for member in facets_schema["anyOf"] if member.get("type") != "null"
    )
    assert_eq(non_null_member["type"], "object")
    assert_eq(non_null_member["additionalProperties"], {"type": "string"})


def assert_not_in_required(schema: dict[str, Any], field: str) -> None:
    """Assert `field` is present as a property but absent from `required`."""
    assert_in(field, schema["properties"])
    assert_not_in(field, schema.get("required", []))


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
            internal_todo_tool_routes(),
            internal_triage_tool_routes(),
            internal_youtube_tool_routes(),
            internal_trigger_tool_routes(),
            internal_recall_tool_routes(),
            internal_conversation_history_tool_routes(),
            internal_artifact_tool_routes(),
            internal_panel_tool_routes(),
            internal_kosync_tool_routes(),
            internal_proposal_tool_routes(),
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
    # `intent_context` and `year` are optional: present as properties, absent
    # from `required` — a Bucket item can be Added without a reason.
    assert_in("intent_context", add_movie_schema["properties"])
    assert_eq("intent_context" in add_movie_schema["required"], False)
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
    # `intent_context` and `author` are optional: present as properties,
    # absent from `required` — a Bucket item can be Added without a reason.
    assert_in("intent_context", add_book_schema["properties"])
    assert_eq("intent_context" in add_book_schema["required"], False)
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
    # `intent_context` and `season` are optional: present as properties,
    # absent from `required` — a Bucket item can be Added without a reason.
    assert_in("intent_context", add_travel_schema["properties"])
    assert_eq("intent_context" in add_travel_schema["required"], False)
    assert_in("season", add_travel_schema["properties"])
    assert_eq("season" in add_travel_schema["required"], False)
