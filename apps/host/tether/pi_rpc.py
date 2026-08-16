"""Strict JSONL RPC transport for a host-owned pi subprocess."""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any, Protocol, cast

from tether.pi_errors import PiRuntimeError

_JSONL_READ_LIMIT = 65536
"""Maximum bytes requested from pi stdout per async read."""


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
    """Incrementally frame JSONL records using LF as the only delimiter."""

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
        """Parse one JSON object, accepting CRLF by removing a trailing CR."""
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


class PiRpcClient:
    """Correlated request and event transport over pi's JSONL stdio."""

    def __init__(self, *, reader: AsyncByteReader, writer: AsyncByteWriter) -> None:
        self.reader: AsyncByteReader = reader
        self.writer: AsyncByteWriter = writer
        self.events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._closed: bool = False
        self._next_id: int = 0
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._reader_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Start the background stdout reader exactly once."""
        if self._reader_task is None:
            self._reader_task = asyncio.create_task(self._read_loop())

    async def request(self, command_type: str, **fields: Any) -> dict[str, Any]:
        """Send one command and await the correlated response record."""
        if self._closed:
            message = "pi RPC client is closed"
            raise PiRuntimeError(message)
        request_id = self._allocate_request_id()
        self._pending[request_id] = asyncio.get_running_loop().create_future()
        await self._write_command({"id": request_id, "type": command_type, **fields})
        return await self._pending[request_id]

    def drain_events(self) -> int:
        """Discard queued protocol events and return the count dropped."""
        dropped = 0
        while True:
            try:
                _ = self.events.get_nowait()
            except asyncio.QueueEmpty:
                return dropped
            dropped += 1

    async def close(self) -> None:
        """Stop the reader and fail every unresolved request exactly once."""
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
        """Serialize one command as compact JSON followed by LF."""
        self.writer.write(
            json.dumps(command, separators=(",", ":"), ensure_ascii=False).encode()
            + b"\n"
        )
        await self.writer.drain()

    async def _read_loop(self) -> None:
        """Read stdout chunks and dispatch responses and events."""
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
        """Resolve matching responses and queue every other protocol record."""
        if record.get("type") == "response" and isinstance(record.get("id"), str):
            pending = self._pending.pop(cast("str", record["id"]), None)
            if pending is not None and not pending.done():
                pending.set_result(record)
                return
        self.events.put_nowait(record)

    def _fail_pending(self, error: BaseException) -> None:
        """Fail all in-flight requests with one terminal transport error."""
        for pending in self._pending.values():
            if not pending.done():
                pending.set_exception(error)
        self._pending.clear()


__all__ = ["AsyncByteReader", "AsyncByteWriter", "PiRpcClient"]
