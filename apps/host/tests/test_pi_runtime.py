"""Behaviour tests for the host-managed pi RPC runtime."""

from __future__ import annotations

import asyncio
import json
import socket
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast

import uvicorn
from snektest import (
    assert_eq,
    assert_in,
    assert_not_in,
    assert_true,
    fixture,
    load_fixture,
    test,
)

from tether.agent_trace import AgentTraceRecorder
from tether.embeddings import FakeEmbedder
from tether.pi_runtime import PiRpcClient, PiRuntime, PiRuntimeConfig
from tether.server import WS_PROTOCOL, AppConfig, create_app
from tether.telemetry import TelemetrySettings
from tether.tools import SessionRegistry


class ControlledByteReader:
    """Async byte source whose chunks are released by the test."""

    def __init__(self) -> None:
        self._chunks: asyncio.Queue[bytes | None] = asyncio.Queue()

    async def read(self, n: int = -1) -> bytes:
        """Return the next queued chunk, or EOF for `None`."""
        _ = n
        chunk = await self._chunks.get()
        return b"" if chunk is None else chunk

    async def feed(self, chunk: bytes) -> None:
        """Make one byte chunk available to the client."""
        await self._chunks.put(chunk)

    async def finish(self) -> None:
        """Close the stream."""
        await self._chunks.put(None)


class MemoryByteWriter:
    """Async byte sink that records outbound JSONL commands."""

    def __init__(self) -> None:
        self.writes: list[bytes] = []
        self._changed: asyncio.Condition = asyncio.Condition()

    def write(self, data: bytes | bytearray | memoryview[int]) -> None:
        """Record bytes synchronously, matching `asyncio.StreamWriter`."""
        self.writes.append(bytes(data))

    async def drain(self) -> None:
        """Wake waiters once a write has been recorded."""
        async with self._changed:
            self._changed.notify_all()

    async def wait_for_writes(self, count: int) -> None:
        """Block until at least `count` writes are present."""
        async with self._changed:
            await self._changed.wait_for(lambda: len(self.writes) >= count)


@dataclass(frozen=True)
class LiveHost:
    """A bound host app reachable by the pi subprocess."""

    base_url: str
    session_registry: SessionRegistry
    trace_recorder: AgentTraceRecorder


@fixture
async def pi_session_dir() -> AsyncGenerator[Path]:
    """Temporary directory for pi session files."""
    with TemporaryDirectory() as directory:
        yield Path(directory)


@fixture
async def live_host() -> AsyncGenerator[LiveHost]:
    """Run the host app on a real loopback port for subprocess callbacks."""
    with TemporaryDirectory() as directory:
        root = Path(directory)
        app = create_app(
            config=AppConfig(
                app_password="test-app-password",
                database_path=root / "tether.sqlite3",
                kb_root=root / ".tether",
                session_secret="test-session-secret",
            ),
            telemetry_settings=TelemetrySettings(install_global_provider=False),
            tool_secret="test-secret",
            embedder=FakeEmbedder(),
        )
        bound_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        bound_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        bound_socket.bind(("127.0.0.1", 0))
        bound_socket.listen()
        port = bound_socket.getsockname()[1]
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                host="127.0.0.1",
                port=port,
                ws=WS_PROTOCOL,
                access_log=False,
                log_config=None,
                log_level="warning",
            )
        )
        server_task = asyncio.create_task(server.serve(sockets=[bound_socket]))
        while not server.started:  # noqa: ASYNC110 - uvicorn exposes startup as state.
            await asyncio.sleep(0.01)
        try:
            yield LiveHost(
                base_url=f"http://127.0.0.1:{port}",
                session_registry=cast("SessionRegistry", app.state.session_registry),
                trace_recorder=cast("AgentTraceRecorder", app.state.trace_recorder),
            )
        finally:
            server.should_exit = True
            await asyncio.wait_for(server_task, timeout=5)


@test()
async def client_buffers_partial_records_and_splits_only_on_lf() -> None:
    """U+2028 inside JSON strings is data, not a record delimiter."""
    reader = ControlledByteReader()
    writer = MemoryByteWriter()
    client = PiRpcClient(reader=reader, writer=writer)
    await client.start()

    encoded_event = json.dumps(
        {"type": "agent_start", "text": "hello\u2028world"}, ensure_ascii=False
    ).encode()
    await reader.feed(encoded_event[:24])
    await asyncio.sleep(0)
    assert_true(client.events.empty())

    await reader.feed(encoded_event[24:] + b"\n")
    event = await asyncio.wait_for(client.events.get(), timeout=1)
    await client.close()

    assert_eq(event["type"], "agent_start")
    assert_eq(event["text"], "hello\u2028world")


@test()
async def client_drain_events_discards_pending_events() -> None:
    """Leftover events from a prior turn are dropped before the next prompt."""
    reader = ControlledByteReader()
    writer = MemoryByteWriter()
    client = PiRpcClient(reader=reader, writer=writer)
    await client.start()

    for index in range(3):
        await reader.feed(
            json.dumps({"type": "agent_end", "n": index}).encode() + b"\n"
        )
    # Wait until all three stale events have been queued by the reader.
    while client.events.qsize() < 3:  # noqa: ASYNC110 - poll the background reader.
        await asyncio.sleep(0)

    dropped = client.drain_events()
    await client.close()

    assert_eq(dropped, 3)
    assert_true(client.events.empty())


@test()
async def client_correlates_out_of_order_responses_by_id() -> None:
    """Concurrent RPC commands resolve with the response carrying their id."""
    reader = ControlledByteReader()
    writer = MemoryByteWriter()
    client = PiRpcClient(reader=reader, writer=writer)
    await client.start()

    state_task = asyncio.create_task(client.request("get_state"))
    models_task = asyncio.create_task(client.request("get_available_models"))
    await writer.wait_for_writes(2)
    sent_commands = [json.loads(chunk) for chunk in writer.writes]

    await reader.feed(
        json.dumps(
            {
                "id": sent_commands[1]["id"],
                "type": "response",
                "command": "get_available_models",
                "success": True,
                "data": {"models": ["second"]},
            }
        ).encode()
        + b"\n"
    )
    await reader.feed(
        json.dumps(
            {
                "id": sent_commands[0]["id"],
                "type": "response",
                "command": "get_state",
                "success": True,
                "data": {"sessionId": "first"},
            }
        ).encode()
        + b"\n"
    )

    state_response = await asyncio.wait_for(state_task, timeout=1)
    models_response = await asyncio.wait_for(models_task, timeout=1)
    await client.close()

    assert_eq(state_response["data"], {"sessionId": "first"})
    assert_eq(models_response["data"], {"models": ["second"]})


@test()
async def runtime_registers_confirmed_uuid7_session_until_shutdown() -> None:
    """Startup registers the pi session id; shutdown discards it."""
    session_dir = await load_fixture(pi_session_dir())
    registry = SessionRegistry()

    runtime = await PiRuntime.spawn(
        PiRuntimeConfig(
            tool_base_url="http://127.0.0.1:9",
            tool_secret="test-secret",
            session_dir=session_dir,
        ),
        session_registry=registry,
    )

    assert_true(await runtime.health())
    assert_eq(runtime.session_id in registry, True)
    assert_eq(runtime.session_uuid.version, 7)

    await runtime.shutdown()
    assert_eq(runtime.session_id in registry, False)
    assert_true(runtime.process.returncode is not None)


@test()
async def runtime_respawns_with_persisted_session_context() -> None:
    """Respawning with the same id and dir reloads pi's session transcript."""
    session_dir = await load_fixture(pi_session_dir())
    registry = SessionRegistry()
    session_id = "019f08f0-0000-7000-8000-000000000001"
    runtime = await PiRuntime.spawn(
        PiRuntimeConfig(
            tool_base_url="http://127.0.0.1:9",
            tool_secret="test-secret",
            session_dir=session_dir,
            session_id=session_id,
            extra_extension_paths=[
                Path.cwd().parent / "agent/tests/fixtures/faux-chat-text.ts"
            ],
        ),
        session_registry=registry,
    )
    try:
        _ = await runtime.client.request(
            "set_model", provider="faux", modelId="tether-chat-text-faux"
        )
        _ = await runtime.client.request("prompt", message="remember this")
        _ = await runtime.next_event("agent_end", wait_seconds=15)
    finally:
        await runtime.shutdown()

    resumed = await PiRuntime.spawn(
        PiRuntimeConfig(
            tool_base_url="http://127.0.0.1:9",
            tool_secret="test-secret",
            session_dir=session_dir,
            session_id=session_id,
            extra_extension_paths=[
                Path.cwd().parent / "agent/tests/fixtures/faux-chat-text.ts"
            ],
        ),
        session_registry=registry,
    )
    try:
        state = await resumed.client.request("get_state")
    finally:
        await resumed.shutdown()

    assert_eq(state["data"]["messageCount"], 2)


@test()
async def runtime_keeps_builtin_tools_inactive() -> None:
    """The spawned process exposes generated Tether tools, not built-ins."""
    session_dir = await load_fixture(pi_session_dir())
    registry = SessionRegistry()
    inspector_extension = session_dir / "inspect-tools.ts"
    inspector_extension.write_text(
        """
export default function inspectTools(pi) {
  pi.registerCommand("tether-tools", {
    description: "Inspect active tools",
    handler: async (_args, ctx) => {
      ctx.ui.notify(JSON.stringify({ active: pi.getActiveTools() }), "info");
    },
  });
}
""",
        encoding="utf-8",
    )

    runtime = await PiRuntime.spawn(
        PiRuntimeConfig(
            tool_base_url="http://127.0.0.1:9",
            tool_secret="test-secret",
            session_dir=session_dir,
            extra_extension_paths=[inspector_extension],
        ),
        session_registry=registry,
    )

    await runtime.client.request("prompt", message="/tether-tools")
    event = await runtime.next_event("extension_ui_request", wait_seconds=5)
    await runtime.shutdown()

    active_tools = json.loads(event["message"])["active"]
    assert_eq(
        sorted(active_tools),
        [
            "add_movie",
            "add_place",
            "answer_recall_prompt",
            "browse",
            "browse_youtube",
            "capture",
            "complete_bucket_item",
            "create_trigger",
            "delete_bucket_item",
            "delete_trigger",
            "edit",
            "fetch_youtube_transcript",
            "ignore_youtube_video",
            "list_due_recall_prompts",
            "list_triggers",
            "reject",
            "retry_youtube_video",
            "review_digest",
            "search",
            "search_bucket_items",
            "search_youtube",
            "start_recall",
            "tether",
            "triage_report",
        ],
    )
    assert_not_in("bash", active_tools)
    assert_not_in("read", active_tools)
    assert_in("capture", active_tools)


@test()
async def generated_shim_tool_call_reaches_loopback_and_returns_envelope() -> None:
    """A pi command can execute a generated shim that calls the host API."""
    session_dir = await load_fixture(pi_session_dir())
    host = await load_fixture(live_host())
    smoke_extension = session_dir / "capture-smoke.ts"
    smoke_extension.write_text(
        f"""
import {{ captureTool }} from "{(Path.cwd().parent / "agent/src/generated/capture.ts").as_posix()}";

export default function captureSmoke(pi) {{
  pi.registerCommand("tether-capture-smoke", {{
    description: "Execute the generated capture shim",
    handler: async (_args, ctx) => {{
      const result = await captureTool.execute(
        "smoke-call",
        {{ content: "shim e2e memory" }},
        undefined,
      );
      ctx.ui.notify(JSON.stringify(result.details), "info");
    }},
  }});
}}
""",
        encoding="utf-8",
    )

    runtime = await PiRuntime.spawn(
        PiRuntimeConfig(
            tool_base_url=host.base_url,
            tool_secret="test-secret",
            session_dir=session_dir,
            extra_extension_paths=[smoke_extension],
        ),
        session_registry=host.session_registry,
    )

    await runtime.client.request("prompt", message="/tether-capture-smoke")
    event = await runtime.next_event("extension_ui_request", wait_seconds=5)
    await runtime.shutdown()

    details = json.loads(event["message"])
    assert_eq(details["result"]["content"], "shim e2e memory")
    assert_eq(details["result"]["state"], "loose")
    assert_eq(details["provenance"], {"kind": "manual"})
