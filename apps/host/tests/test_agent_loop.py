"""Agent-loop test driven by a deterministic faux model."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from snektest import assert_eq, assert_in, assert_is_none, load_fixture, test

from tests.test_pi_runtime import live_host, pi_session_dir
from tether.pi_process import PiRuntimeConfig
from tether.pi_runtime import PiRuntime

SECRET = "test-secret"
TRIAGE_MODEL_ID = "tether-triage-faux"


def _triage_fixture_path() -> Path:
    """Return the faux provider that scripts the triage_report loop."""
    return Path(__file__).resolve().parents[2] / "agent/tests/fixtures/faux-triage.ts"


def _json_object(value: Any) -> dict[str, Any]:
    assert isinstance(value, dict)
    return cast("dict[str, Any]", value)


def _details_from_tool_end(event: dict[str, Any]) -> dict[str, Any]:
    return _json_object(_json_object(event["result"])["details"])


@test()
async def scripted_model_clusters_duplicate_bucket_items_via_triage_report() -> None:
    """Generated shims still drive a complete typed vertical after Memory cutover."""
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
    details = _details_from_tool_end(triage_end)

    assert_eq(triage_start["toolName"], "triage_report")
    assert_eq(triage_end["isError"], False)
    assert_is_none(details["provenance"])
    duplicates = _json_object(details["result"])["duplicates"]
    assert isinstance(duplicates, list)
    clusters = [
        set(cast("list[str]", _json_object(cluster)["bucket_item_ids"]))
        for cluster in cast("list[Any]", duplicates)
    ]
    assert_in({first_id, second_id}, clusters)
