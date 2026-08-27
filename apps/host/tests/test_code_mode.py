"""Confined Code Mode integration through the real Pi and host tool boundary."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

from snektest import assert_eq, assert_in, load_fixture, test

from tests.test_pi_runtime import live_host, pi_session_dir
from tether.pi_process import PiRuntimeConfig
from tether.pi_runtime import PiRuntime

SECRET = "test-secret"
CODE_MODE_MODEL_ID = "tether-code-mode-faux"


def _fixture_path() -> Path:
    """Return the faux provider that emits one confined orchestration call."""
    return (
        Path(__file__).resolve().parents[2] / "agent/tests/fixtures/faux-code-mode.ts"
    )


def _object(value: Any) -> dict[str, Any]:
    assert isinstance(value, dict)
    return cast("dict[str, Any]", value)


@test()
async def confined_program_orchestrates_real_tether_tools() -> None:
    """One outer Pi tool call composes host-authorized nested tool requests."""
    session_dir = await load_fixture(pi_session_dir())
    host = await load_fixture(live_host())
    runtime = await PiRuntime.spawn(
        PiRuntimeConfig(
            tool_base_url=host.base_url,
            tool_secret=SECRET,
            session_dir=session_dir,
            extra_extension_paths=[_fixture_path()],
        ),
        session_registry=host.session_registry,
    )

    try:
        set_model = await runtime.client.request(
            "set_model", provider="faux", modelId=CODE_MODE_MODEL_ID
        )
        assert_eq(set_model["success"], True)
        prompt = await runtime.client.request(
            "prompt", message="Add duplicate movies and inspect the backlog."
        )
        assert_eq(prompt["success"], True)
        started = await runtime.next_event("tool_execution_start", wait_seconds=15)
        ended = await runtime.next_event("tool_execution_end", wait_seconds=15)
        _ = await runtime.next_event("agent_end", wait_seconds=15)
    finally:
        await runtime.shutdown()

    assert_eq(started["toolName"], "execute_tools")
    assert_eq(ended["toolName"], "execute_tools")
    assert_eq(ended["isError"], False)

    result = _object(ended["result"])
    details = _object(result["details"])
    calls = cast("list[dict[str, str]]", details["toolCalls"])
    assert_eq(
        calls,
        [
            {"name": "add_movie", "status": "completed"},
            {"name": "add_movie", "status": "completed"},
            {"name": "triage_report", "status": "completed"},
        ],
    )

    content = cast("list[dict[str, str]]", result["content"])
    program_result = _object(json.loads(content[0]["text"]))
    added_ids = cast("list[str]", program_result["addedIds"])
    assert_eq(len(added_ids), 2)
    assert_in(program_result["duplicateCount"], range(1, 100))
