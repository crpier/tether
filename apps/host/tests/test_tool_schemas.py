"""Behavior tests for generated pi tool-schema source documents."""

from typing import Any, cast

from snektest import assert_eq, assert_in, assert_not_in, test

from tether.artifact_tools import internal_artifact_tool_routes
from tether.bucket_tools import internal_bucket_tool_routes
from tether.conversation_history_tools import (
    internal_conversation_history_tool_routes,
)
from tether.gmail.tools import internal_gmail_tool_routes
from tether.health_connect.tools import internal_health_connect_tool_routes
from tether.kosync_tools import internal_kosync_tool_routes
from tether.panel_tools import internal_panel_tool_routes
from tether.proposal_tools import internal_proposal_tool_routes
from tether.recall_tools import internal_recall_tool_routes
from tether.search_tools import internal_search_tool_routes
from tether.todo_tools import internal_todo_tool_routes
from tether.tool_schemas import build_tool_schema_document
from tether.tools import internal_tool_routes
from tether.triage_tools import internal_triage_tool_routes
from tether.trigger_tools import internal_trigger_tool_routes
from tether.youtube.tools import internal_youtube_tool_routes


@test()
def tool_schema_document_describes_the_internal_tools() -> None:
    """The codegen source document exposes each tool with endpoint and schema."""
    document = build_tool_schema_document()

    tools = {tool["name"]: tool for tool in document["tools"]}

    assert_eq(
        set(tools),
        {
            "search",
            "queue_memory_assimilation",
            "add_movie",
            "add_place",
            "add_book",
            "add_travel",
            "add_purchase",
            "complete_bucket_item",
            "delete_bucket_item",
            "search_bucket_items",
            "set_purchase_decision",
            "set_bucket_item_intent",
            "create_todo",
            "set_todo_status",
            "link_todo_trigger",
            "list_todos",
            "triage_report",
            "browse_youtube",
            "search_youtube",
            "fetch_youtube_transcript",
            "ignore_youtube_video",
            "retry_youtube_video",
            "web_search",
            "archive_gmail_message",
            "search_gmail",
            "read_gmail_message",
            "list_gmail_labels",
            "trash_gmail_message",
            "update_gmail_labels",
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
            "health_connect_inventory",
            "query_health_connect",
            "summarize_health_connect",
            "propose",
            "list_proposals",
        },
    )
    search_schema = cast("dict[str, Any]", tools["search"]["schema"])

    assert_eq(tools["search"]["endpoint"], "/internal/tools/search")
    assert_eq(tools["search"]["params_model"], "SearchParams")
    assert_eq(search_schema["properties"]["limit"]["default"], 50)
    assert_eq(search_schema["required"], ["q"])


@test()
def legacy_memory_mutation_and_review_tools_are_absent() -> None:
    """Dreaming leaves foreground agents only a read-only Memory Search seam."""
    tools = {tool["name"] for tool in build_tool_schema_document()["tools"]}

    for removed in (
        "append",
        "browse",
        "capture",
        "edit",
        "facet_overview",
        "merge_facet_value",
        "reject",
        "rename_facet_key",
        "review_digest",
        "tether",
    ):
        assert_not_in(removed, tools)
    assert_in("search", tools)


@test()
def health_connect_query_schema_is_typed_and_bounded() -> None:
    """The generated query tool offers every record type and a large fixed limit."""
    document = build_tool_schema_document()
    tools = {tool["name"]: tool for tool in document["tools"]}
    query_schema = cast("dict[str, Any]", tools["query_health_connect"]["schema"])
    record_type_schema = query_schema["properties"]["record_type"]

    assert_eq(record_type_schema["enum"][0], "active_calories_burned")
    assert_in("weight", record_type_schema["enum"])
    assert_eq(query_schema["properties"]["limit"]["default"], 5)
    assert_eq(query_schema["properties"]["limit"]["maximum"], 1_000)
    assert_in("individual", cast("str", query_schema["description"]).lower())
    summary_schema = cast("dict[str, Any]", tools["summarize_health_connect"]["schema"])
    assert_eq(summary_schema["required"], ["after", "before"])
    assert_in("overview", cast("str", summary_schema["description"]).lower())


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
            internal_search_tool_routes(),
            internal_gmail_tool_routes(),
            internal_trigger_tool_routes(),
            internal_recall_tool_routes(),
            internal_conversation_history_tool_routes(),
            internal_artifact_tool_routes(),
            internal_panel_tool_routes(),
            internal_kosync_tool_routes(),
            internal_health_connect_tool_routes(),
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


@test()
def search_gmail_schema_exposes_pagination_bounds_and_optional_page_token() -> None:
    """`search_gmail` uses safe pagination defaults and a token cursor."""
    tools = {tool["name"]: tool for tool in build_tool_schema_document()["tools"]}
    schema = cast("dict[str, Any]", tools["search_gmail"]["schema"])

    assert_eq(schema["properties"]["max_results"]["default"], 20)
    assert_eq(schema["properties"]["max_results"]["maximum"], 50)
    assert_in("page_token", schema["properties"])
    assert_in("query", cast("str", schema["description"]).lower())


@test()
def read_gmail_message_schema_carries_strict_char_limiting() -> None:
    """`read_gmail_message` caps raw text truncation at safe bounds."""
    tools = {tool["name"]: tool for tool in build_tool_schema_document()["tools"]}
    schema = cast("dict[str, Any]", tools["read_gmail_message"]["schema"])

    assert_eq(schema["properties"]["max_chars"]["default"], 50_000)
    assert_eq(schema["properties"]["max_chars"]["minimum"], 1_000)
    assert_eq(schema["properties"]["max_chars"]["maximum"], 200_000)
    assert_eq(schema["required"], ["message_id"])
