"""Agent-loop tests driven by a deterministic faux model."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import httpx
from snektest import (
    assert_eq,
    assert_in,
    assert_is_none,
    assert_true,
    load_fixture,
    test,
)

from tests.test_pi_runtime import live_host, pi_session_dir
from tether.model_selection import AgentModelConfig
from tether.pi_runtime import PiRuntime, PiRuntimeConfig
from tether.scheduler import EphemeralPiConfig, EphemeralPiPromptRunner

SECRET = "test-secret"
SECRET_HEADER = "X-Tether-Tool-Secret"
SCRIPTED_MODEL_ID = "tether-agent-loop-faux"
REVIEW_DIGEST_MODEL_ID = "tether-review-digest-faux"
TRIAGE_MODEL_ID = "tether-triage-faux"
SCRIPTED_SESSION_ID = "scripted-session"


def _agent_fixture_path() -> Path:
    """Return the committed faux provider extension used by subprocess tests."""
    return (
        Path(__file__).resolve().parents[2] / "agent/tests/fixtures/faux-agent-loop.ts"
    )


def _review_digest_fixture_path() -> Path:
    """Return the committed faux provider that scripts the review_digest loop."""
    return (
        Path(__file__).resolve().parents[2]
        / "agent/tests/fixtures/faux-review-digest.ts"
    )


def _triage_fixture_path() -> Path:
    """Return the committed faux provider that scripts the triage_report loop."""
    return Path(__file__).resolve().parents[2] / "agent/tests/fixtures/faux-triage.ts"


def _json_object(value: Any) -> dict[str, Any]:
    """Narrow untyped RPC JSON into an object for assertions."""
    assert isinstance(value, dict)
    return cast("dict[str, Any]", value)


def _details_from_tool_end(event: dict[str, Any]) -> dict[str, Any]:
    """Extract the generated shim details carried by a tool completion event."""
    return _json_object(_json_object(event["result"])["details"])


def _assert_success_details(details: dict[str, Any]) -> None:
    """The shim forwards the successful envelope's observable payload fields."""
    assert_in("result", details)
    assert_in("provenance", details)
    assert_in("quota", details)
    assert_is_none(details["quota"])


@test()
async def scripted_model_drives_capture_tether_search_through_generated_shims() -> None:
    """A canned model turn sequence executes real Tether tools end-to-end."""
    session_dir = await load_fixture(pi_session_dir())
    host = await load_fixture(live_host())
    runtime = await PiRuntime.spawn(
        PiRuntimeConfig(
            tool_base_url=host.base_url,
            tool_secret=SECRET,
            session_dir=session_dir,
            extra_extension_paths=[_agent_fixture_path()],
        ),
        session_registry=host.session_registry,
    )

    try:
        set_model = await runtime.client.request(
            "set_model", provider="faux", modelId=SCRIPTED_MODEL_ID
        )
        assert_eq(set_model["success"], True)

        prompt = await runtime.client.request(
            "prompt", message="Capture, tether, and search the scripted memory."
        )
        assert_eq(prompt["success"], True)

        capture_start = await runtime.next_event(
            "tool_execution_start", wait_seconds=15
        )
        capture_end = await runtime.next_event("tool_execution_end", wait_seconds=15)
        tether_start = await runtime.next_event("tool_execution_start", wait_seconds=15)
        tether_end = await runtime.next_event("tool_execution_end", wait_seconds=15)
        search_start = await runtime.next_event("tool_execution_start", wait_seconds=15)
        search_end = await runtime.next_event("tool_execution_end", wait_seconds=15)
        _ = await runtime.next_event("agent_end", wait_seconds=15)
    finally:
        await runtime.shutdown()

    assert_eq(capture_start["toolName"], "capture")
    assert_eq(capture_start["args"], {"content": "agent loop needle memory"})
    assert_eq(capture_end["isError"], False)
    capture_details = _details_from_tool_end(capture_end)
    _assert_success_details(capture_details)
    captured_memory = _json_object(capture_details["result"])
    assert_eq(captured_memory["content"], "agent loop needle memory")
    assert_eq(captured_memory["state"], "loose")
    assert_eq(capture_details["provenance"], {"kind": "manual"})

    assert_eq(tether_start["toolName"], "tether")
    assert_eq(
        tether_start["args"],
        {
            "memory_id": captured_memory["id"],
            "version": captured_memory["version"],
        },
    )
    assert_eq(tether_end["isError"], False)
    tether_details = _details_from_tool_end(tether_end)
    _assert_success_details(tether_details)
    tethered_memory = _json_object(tether_details["result"])
    assert_eq(tethered_memory["id"], captured_memory["id"])
    assert_eq(tethered_memory["state"], "tethered")

    assert_eq(search_start["toolName"], "search")
    assert_eq(search_start["args"], {"q": "needle", "limit": 5})
    assert_eq(search_end["isError"], False)
    search_details = _details_from_tool_end(search_end)
    _assert_success_details(search_details)
    assert_is_none(search_details["provenance"])
    search_hits_json = search_details["result"]
    assert isinstance(search_hits_json, list)
    search_hits = cast("list[Any]", search_hits_json)
    assert_in(
        tethered_memory["id"],
        [_json_object(search_hit)["id"] for search_hit in search_hits],
    )


@test()
async def scripted_model_groups_duplicate_captures_via_review_digest() -> None:
    """A canned model captures two near-dups, then review_digest clusters them."""
    session_dir = await load_fixture(pi_session_dir())
    host = await load_fixture(live_host())
    runtime = await PiRuntime.spawn(
        PiRuntimeConfig(
            tool_base_url=host.base_url,
            tool_secret=SECRET,
            session_dir=session_dir,
            extra_extension_paths=[_review_digest_fixture_path()],
        ),
        session_registry=host.session_registry,
    )

    try:
        set_model = await runtime.client.request(
            "set_model", provider="faux", modelId=REVIEW_DIGEST_MODEL_ID
        )
        assert_eq(set_model["success"], True)

        prompt = await runtime.client.request(
            "prompt", message="Capture the duplicates and review the queue."
        )
        assert_eq(prompt["success"], True)

        _ = await runtime.next_event("tool_execution_start", wait_seconds=15)
        first_capture_end = await runtime.next_event(
            "tool_execution_end", wait_seconds=15
        )
        _ = await runtime.next_event("tool_execution_start", wait_seconds=15)
        second_capture_end = await runtime.next_event(
            "tool_execution_end", wait_seconds=15
        )
        digest_start = await runtime.next_event("tool_execution_start", wait_seconds=15)
        digest_end = await runtime.next_event("tool_execution_end", wait_seconds=15)
        _ = await runtime.next_event("agent_end", wait_seconds=15)
    finally:
        await runtime.shutdown()

    first_id = _json_object(_details_from_tool_end(first_capture_end)["result"])["id"]
    second_id = _json_object(_details_from_tool_end(second_capture_end)["result"])["id"]

    assert_eq(digest_start["toolName"], "review_digest")
    assert_eq(digest_end["isError"], False)
    digest_details = _details_from_tool_end(digest_end)
    _assert_success_details(digest_details)
    assert_is_none(digest_details["provenance"])
    digest = _json_object(digest_details["result"])
    dedup_groups_json = digest["dedup_groups"]
    assert isinstance(dedup_groups_json, list)
    dedup_clusters = [
        set(cast("list[str]", _json_object(group)["memory_ids"]))
        for group in cast("list[Any]", dedup_groups_json)
    ]
    assert_in({first_id, second_id}, dedup_clusters)


@test()
async def scripted_model_clusters_duplicate_bucket_items_via_triage_report() -> None:
    """A canned model Adds two duplicate movies, then triage_report clusters them."""
    session_dir = await load_fixture(pi_session_dir())
    host = await load_fixture(live_host())
    runtime = await PiRuntime.spawn(
        PiRuntimeConfig(
            tool_base_url=host.base_url,
            tool_secret=SECRET,
            session_dir=session_dir,
            extra_extension_paths=[_triage_fixture_path()],
        ),
        session_registry=host.session_registry,
    )

    try:
        set_model = await runtime.client.request(
            "set_model", provider="faux", modelId=TRIAGE_MODEL_ID
        )
        assert_eq(set_model["success"], True)

        prompt = await runtime.client.request(
            "prompt", message="Add the duplicates and triage the backlog."
        )
        assert_eq(prompt["success"], True)

        _ = await runtime.next_event("tool_execution_start", wait_seconds=15)
        first_add_end = await runtime.next_event("tool_execution_end", wait_seconds=15)
        _ = await runtime.next_event("tool_execution_start", wait_seconds=15)
        second_add_end = await runtime.next_event("tool_execution_end", wait_seconds=15)
        triage_start = await runtime.next_event("tool_execution_start", wait_seconds=15)
        triage_end = await runtime.next_event("tool_execution_end", wait_seconds=15)
        _ = await runtime.next_event("agent_end", wait_seconds=15)
    finally:
        await runtime.shutdown()

    first_id = _json_object(
        _json_object(_details_from_tool_end(first_add_end)["result"])["item"]
    )["id"]
    second_id = _json_object(
        _json_object(_details_from_tool_end(second_add_end)["result"])["item"]
    )["id"]

    assert_eq(triage_start["toolName"], "triage_report")
    assert_eq(triage_end["isError"], False)
    triage_details = _details_from_tool_end(triage_end)
    _assert_success_details(triage_details)
    assert_is_none(triage_details["provenance"])
    report = _json_object(triage_details["result"])
    duplicates_json = report["duplicates"]
    assert isinstance(duplicates_json, list)
    duplicate_clusters = [
        set(cast("list[str]", _json_object(cluster)["bucket_item_ids"]))
        for cluster in cast("list[Any]", duplicates_json)
    ]
    assert_in({first_id, second_id}, duplicate_clusters)


@test()
async def agent_run_trace_captures_ordered_tool_calls_and_envelopes() -> None:
    """A scripted run is recorded as one trace over its tool calls + envelopes."""
    session_root = await load_fixture(pi_session_dir())
    host = await load_fixture(live_host())
    runner = EphemeralPiPromptRunner(
        EphemeralPiConfig(
            session_registry=host.session_registry,
            session_root=session_root,
            tool_base_url=host.base_url,
            tool_secret=SECRET,
            model=AgentModelConfig(
                id="faux",
                display_name="Faux",
                provider="faux",
                model_id=SCRIPTED_MODEL_ID,
            ),
            extra_extension_paths=[_agent_fixture_path()],
            trace_recorder=host.trace_recorder,
            run_kind="scheduled",
        )
    )

    _ = await runner.run("Capture, tether, and search the scripted memory.")

    runs = host.trace_recorder.recent_runs(limit=5)
    assert_eq(len(runs), 1)
    run = runs[0]
    assert_eq(run.kind, "scheduled")
    assert_eq(run.termination, "completed")
    assert_true(not run.is_active)
    assert_true(run.iterations >= 1)
    # The run is also inspectable by id after it completed.
    fetched = host.trace_recorder.get_run(run.run_id)
    assert fetched is not None
    assert_eq(fetched.run_id, run.run_id)

    assert_eq([call.tool for call in run.tool_calls], ["capture", "tether", "search"])
    assert_true(all(call.success for call in run.tool_calls))

    capture = run.tool_calls[0]
    assert_eq(capture.args, {"content": "agent loop needle memory"})
    captured = _json_object(capture.result)
    assert_eq(captured["content"], "agent loop needle memory")
    assert_eq(captured["state"], "loose")
    # The single-memory envelope's provenance rides along in the trace.
    assert_eq(capture.provenance, {"kind": "manual"})

    # The search envelope's collection result is summarised, not dumped.
    search = run.tool_calls[2]
    assert_eq(search.args, {"q": "needle", "limit": 5})
    search_result = _json_object(search.result)
    assert_eq(search_result["kind"], "collection")


@test()
async def malformed_tool_input_is_enveloped_without_poisoning_later_calls() -> None:
    """Bad tool input is enveloped and does not poison later valid calls."""
    host = await load_fixture(live_host())
    host.session_registry.register(SCRIPTED_SESSION_ID)

    async with httpx.AsyncClient(base_url=host.base_url) as client:
        malformed_response = await client.post(
            "/internal/tools/capture",
            json={"session_id": SCRIPTED_SESSION_ID, "content": "   "},
            headers={SECRET_HEADER: SECRET},
        )
        valid_response = await client.post(
            "/internal/tools/capture",
            json={"session_id": SCRIPTED_SESSION_ID, "content": "valid memory"},
            headers={SECRET_HEADER: SECRET},
        )

    assert_eq(malformed_response.status_code, 200)
    malformed_envelope = malformed_response.json()
    assert_eq(malformed_envelope["success"], False)
    assert_eq(malformed_envelope["error"]["code"], "invalid_input")
    assert_is_none(malformed_envelope["result"])
    assert_true("quota" in malformed_envelope)

    assert_eq(valid_response.status_code, 200)
    valid_envelope = valid_response.json()
    assert_eq(valid_envelope["success"], True)
    assert_eq(valid_envelope["result"]["content"], "valid memory")
    assert_eq(valid_envelope["result"]["state"], "loose")
