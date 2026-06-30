"""Host-spawned pi RPC runtime over strict JSONL stdio."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, Self, cast
from uuid import UUID

from tether.tools import SessionRegistry

_JSONL_READ_LIMIT = 65536
"""Maximum bytes requested from pi stdout per async read."""

_SHUTDOWN_TIMEOUT_SECONDS = 5.0
"""Time to wait for pi to exit after closing stdin before terminating it."""

_UUID_VERSION_7 = 7
"""UUID version emitted by pi for new session identities."""


class PiRuntimeError(Exception):
    """Failure while speaking to or managing a pi RPC subprocess."""


class AsyncByteReader(Protocol):
    """Async source of bytes for JSONL records."""

    async def read(self, n: int = -1) -> bytes:
        """Read at most `n` bytes, returning `b''` at EOF."""
        ...


class AsyncByteWriter(Protocol):
    """Async sink for bytes carrying JSONL commands."""

    def write(self, data: bytes | bytearray | memoryview[int]) -> None:
        """Write bytes to the underlying stream buffer."""
        ...

    async def drain(self) -> None:
        """Flush buffered bytes to the underlying stream."""
        ...


class _JsonlDecoder:
    """Incrementally frame JSONL records using LF as the only delimiter.

    Bytes are buffered until `\n`; a trailing `\r` is accepted for CRLF input.
    Unicode line separators remain ordinary UTF-8 bytes inside a JSON string.
    """

    def __init__(self) -> None:
        self._buffer: bytearray = bytearray()

    def feed(self, chunk: bytes) -> list[dict[str, Any]]:
        """Decode every complete JSON object made available by `chunk`."""
        self._buffer.extend(chunk)
        records: list[dict[str, Any]] = []
        while True:
            try:
                newline_index = self._buffer.index(0x0A)
            except ValueError:
                break
            raw_line = bytes(self._buffer[:newline_index])
            del self._buffer[: newline_index + 1]
            records.append(self._decode_line(raw_line))
        return records

    def finish(self) -> list[dict[str, Any]]:
        """Decode a final unterminated record when stdout closes."""
        if len(self._buffer) == 0:
            return []
        raw_line = bytes(self._buffer)
        self._buffer.clear()
        return [self._decode_line(raw_line)]

    def _decode_line(self, raw_line: bytes) -> dict[str, Any]:
        """Parse one JSON object, accepting CRLF by removing one trailing CR."""
        if raw_line.endswith(b"\r"):
            raw_line = raw_line[:-1]
        try:
            parsed: object = json.loads(raw_line.decode())
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            message = "pi emitted invalid JSONL"
            raise PiRuntimeError(message) from error
        if not isinstance(parsed, dict):
            message = "pi emitted a JSONL record that is not an object"
            raise PiRuntimeError(message)
        return cast("dict[str, Any]", parsed)


@dataclass(frozen=True)
class PiRuntimeConfig:
    """Configuration for a host-owned pi subprocess.

    ```python
    config = PiRuntimeConfig(
        tool_base_url="http://127.0.0.1:8000",
        tool_secret="process-secret",
    )
    ```
    """

    tool_base_url: str
    tool_secret: str
    cwd: Path | None = None
    extra_extension_paths: Sequence[Path] = field(default_factory=tuple)
    extension_path: Path | None = None
    pi_binary: Path | None = None
    session_dir: Path | None = None
    session_id: str | None = None


class PiRpcClient:
    """JSONL RPC client for pi's stdin/stdout protocol.

    ```python
    client = PiRpcClient(reader=stdout, writer=stdin)
    await client.start()
    response = await client.request("get_state")
    await client.close()
    ```
    """

    def __init__(self, *, reader: AsyncByteReader, writer: AsyncByteWriter) -> None:
        self.reader: AsyncByteReader = reader
        self.writer: AsyncByteWriter = writer
        self.events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._closed: bool = False
        self._next_id: int = 0
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._reader_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Start the background stdout reader."""
        if self._reader_task is not None:
            return
        self._reader_task = asyncio.create_task(self._read_loop())

    async def request(self, command_type: str, **fields: Any) -> dict[str, Any]:
        """Send one command and await the matching `type: response` record."""
        if self._closed:
            message = "pi RPC client is closed"
            raise PiRuntimeError(message)
        request_id = self._allocate_request_id()
        loop = asyncio.get_running_loop()
        self._pending[request_id] = loop.create_future()
        await self._write_command({"id": request_id, "type": command_type, **fields})
        return await self._pending[request_id]

    def drain_events(self) -> int:
        """Discard every queued protocol event, returning the count dropped.

        The stdout reader fills `events` autonomously, so a turn that was
        aborted or cut off by a browser disconnect leaves its trailing deltas
        and `agent_end` sitting in the queue. Draining before the next prompt
        keeps stale events from poisoning the next turn's stream.
        """
        dropped = 0
        while True:
            try:
                _ = self.events.get_nowait()
            except asyncio.QueueEmpty:
                break
            dropped += 1
        return dropped

    async def close(self) -> None:
        """Stop the reader task and fail unresolved requests."""
        if self._closed:
            return
        self._closed = True
        if self._reader_task is not None:
            _ = self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader_task
        self._fail_pending(PiRuntimeError("pi RPC client closed"))

    def _allocate_request_id(self) -> str:
        """Return a unique client-side request id."""
        self._next_id += 1
        return f"tether-{self._next_id}"

    async def _write_command(self, command: dict[str, Any]) -> None:
        """Serialize one command as compact JSON plus LF."""
        self.writer.write(
            json.dumps(command, separators=(",", ":"), ensure_ascii=False).encode()
            + b"\n"
        )
        await self.writer.drain()

    async def _read_loop(self) -> None:
        """Read stdout chunks and dispatch responses/events."""
        decoder = _JsonlDecoder()
        try:
            while True:
                chunk = await self.reader.read(_JSONL_READ_LIMIT)
                if chunk == b"":
                    for record in decoder.finish():
                        self._dispatch(record)
                    break
                for record in decoder.feed(chunk):
                    self._dispatch(record)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._fail_pending(error)
            await self.events.put({"type": "rpc_error", "error": str(error)})
        else:
            self._fail_pending(PiRuntimeError("pi RPC stream ended"))

    def _dispatch(self, record: dict[str, Any]) -> None:
        """Resolve matching responses and queue all other protocol events."""
        if record.get("type") == "response" and isinstance(record.get("id"), str):
            request_id = cast("str", record["id"])
            pending = self._pending.pop(request_id, None)
            if pending is not None and not pending.done():
                pending.set_result(record)
                return
        self.events.put_nowait(record)

    def _fail_pending(self, error: BaseException) -> None:
        """Fail all in-flight requests with the same terminal error."""
        for pending in self._pending.values():
            if not pending.done():
                pending.set_exception(error)
        self._pending.clear()


class PiRuntime:
    """A spawned pi RPC process registered with the host session registry."""

    def __init__(
        self,
        *,
        client: PiRpcClient,
        process: asyncio.subprocess.Process,
        session_id: str,
        session_registry: SessionRegistry,
    ) -> None:
        self.client: PiRpcClient = client
        self.process: asyncio.subprocess.Process = process
        self.session_id: str = session_id
        self.session_registry: SessionRegistry = session_registry
        self.session_uuid: UUID = UUID(session_id)
        self._shutdown_complete: bool = False

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _traceback: object,
    ) -> None:
        await self.shutdown()

    @classmethod
    async def spawn(
        cls,
        config: PiRuntimeConfig,
        *,
        session_registry: SessionRegistry,
    ) -> Self:
        """Start pi, confirm its session id with `get_state`, and register it."""
        session_id = config.session_id or str(uuid.uuid7())
        process = await asyncio.create_subprocess_exec(
            *_spawn_command(config, session_id),
            cwd=config.cwd,
            env=_spawn_environment(config, session_id),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        if process.stdin is None or process.stdout is None:
            process.kill()
            _ = await process.wait()
            message = "pi RPC stdio pipes were not created"
            raise PiRuntimeError(message)
        client = PiRpcClient(reader=process.stdout, writer=process.stdin)
        await client.start()
        runtime = cls(
            client=client,
            process=process,
            session_id=session_id,
            session_registry=session_registry,
        )
        try:
            resolved_session_id = await runtime._resolve_session_id()
            runtime._confirm_session_id(resolved_session_id)
            session_registry.register(resolved_session_id)
        except Exception:
            await runtime.shutdown()
            raise
        return runtime

    async def health(self) -> bool:
        """Return true when the process responds to a state request."""
        if self.process.returncode is not None:
            return False
        response = await self.client.request("get_state")
        return response.get("success") is True

    def drain_events(self) -> int:
        """Discard pending events left over from a previous turn."""
        return self.client.drain_events()

    async def next_event(
        self, event_type: str | None = None, *, wait_seconds: float = 5.0
    ) -> dict[str, Any]:
        """Read queued events until one with `event_type` is found."""
        while True:
            event = await asyncio.wait_for(
                self.client.events.get(), timeout=wait_seconds
            )
            if event_type is None or event.get("type") == event_type:
                return event

    async def shutdown(self) -> None:
        """Close RPC stdio, stop pi, and unregister the session id."""
        if self._shutdown_complete:
            return
        self._shutdown_complete = True
        await self.client.close()
        if self.process.stdin is not None and not self.process.stdin.is_closing():
            self.process.stdin.close()
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                await self.process.stdin.wait_closed()
        await self._wait_or_terminate()
        self.session_registry.discard(self.session_id)

    async def _resolve_session_id(self) -> str:
        """Fetch pi's own session id from `get_state`."""
        response = await self.client.request("get_state")
        if response.get("success") is not True:
            message = f"pi get_state failed: {response.get('error', 'unknown error')}"
            raise PiRuntimeError(message)
        data = response.get("data")
        if not isinstance(data, dict):
            message = "pi get_state response did not include a sessionId"
            raise PiRuntimeError(message)
        state = cast("dict[str, object]", data)
        session_id = state.get("sessionId")
        if not isinstance(session_id, str):
            message = "pi get_state response did not include a sessionId"
            raise PiRuntimeError(message)
        return session_id

    def _confirm_session_id(self, resolved_session_id: str) -> None:
        """Ensure env, CLI session, and pi-reported identity agree."""
        try:
            resolved_uuid = UUID(resolved_session_id)
        except ValueError as error:
            message = "pi session id is not a UUID"
            raise PiRuntimeError(message) from error
        if resolved_uuid.version != _UUID_VERSION_7:
            message = "pi session id is not UUIDv7"
            raise PiRuntimeError(message)
        if resolved_session_id != self.session_id:
            message = "pi reported a different session id than the host injected"
            raise PiRuntimeError(message)

    async def _wait_or_terminate(self) -> None:
        """Prefer EOF shutdown; terminate then kill if pi does not exit."""
        if self.process.returncode is not None:
            return
        with contextlib.suppress(asyncio.TimeoutError):
            _ = await asyncio.wait_for(
                self.process.wait(), timeout=_SHUTDOWN_TIMEOUT_SECONDS
            )
            return
        self.process.terminate()
        with contextlib.suppress(asyncio.TimeoutError):
            _ = await asyncio.wait_for(
                self.process.wait(), timeout=_SHUTDOWN_TIMEOUT_SECONDS
            )
            return
        self.process.kill()
        _ = await self.process.wait()


def _repo_root() -> Path:
    """Return the repository root from the installed host package layout."""
    return Path(__file__).resolve().parents[3]


def _spawn_command(config: PiRuntimeConfig, session_id: str) -> list[str]:
    """Build the closed-tool-world pi command line."""
    command = [
        str(config.pi_binary or _repo_root() / "apps/agent/node_modules/.bin/pi"),
        "--mode",
        "rpc",
        "--no-builtin-tools",
        "--approve",
        "--session-id",
        session_id,
    ]
    if config.session_dir is not None:
        command.extend(["--session-dir", str(config.session_dir)])
    for extension_path in [
        config.extension_path or _repo_root() / "apps/agent/src/generated/index.ts",
        *config.extra_extension_paths,
    ]:
        command.extend(["-e", str(extension_path)])
    return command


def _spawn_environment(config: PiRuntimeConfig, session_id: str) -> dict[str, str]:
    """Inject loopback tool credentials and session identity into pi."""
    environment = os.environ.copy()
    environment.update(
        {
            "TETHER_TOOL_BASE_URL": config.tool_base_url,
            "TETHER_TOOL_SECRET": config.tool_secret,
            "TETHER_TOOL_SESSION_ID": session_id,
        }
    )
    return environment
