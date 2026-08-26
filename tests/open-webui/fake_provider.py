"""Deterministic OpenAI-compatible model used by the Open WebUI smoke."""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any
from urllib.parse import urlsplit


PROVIDER_TOKEN = os.environ["SMOKE_PROVIDER_TOKEN"]
TODO_ACTION = "Standalone Open WebUI smoke Todo"


class FakeProvider:
    """Serve the minimal model API and an authenticated event journal."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def handle_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Handle one HTTP/1.1 request and close the connection."""
        try:
            headers_bytes = await reader.readuntil(b"\r\n\r\n")
            header_lines = headers_bytes.decode("utf-8").split("\r\n")
            method, target, _ = header_lines[0].split(" ", 2)
            headers = {
                name.strip().lower(): value.strip()
                for line in header_lines[1:]
                if line and ":" in line
                for name, value in [line.split(":", 1)]
            }
            body = await reader.readexactly(int(headers.get("content-length", "0")))
            status, content_type, response_body = self.route(
                method,
                urlsplit(target).path,
                headers,
                body,
            )
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ValueError):
            status, content_type, response_body = self.json_response(
                {"error": "bad request"}, status=400
            )

        reasons = {200: "OK", 400: "Bad Request", 401: "Unauthorized", 404: "Not Found"}
        writer.write(
            (
                f"HTTP/1.1 {status} {reasons[status]}\r\n"
                f"Content-Type: {content_type}\r\n"
                f"Content-Length: {len(response_body)}\r\n"
                "Connection: close\r\n\r\n"
            ).encode("ascii")
            + response_body
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    def route(
        self,
        method: str,
        path: str,
        headers: dict[str, str],
        body: bytes,
    ) -> tuple[int, str, bytes]:
        """Route the provider's deliberately small HTTP surface."""
        if method == "GET" and path == "/health":
            return self.json_response({"status": "ok"})

        authorized = headers.get("authorization") == f"Bearer {PROVIDER_TOKEN}"
        if method == "GET" and path == "/events":
            if not authorized:
                return self.json_response({"error": "unauthorized"}, status=401)
            return self.json_response(self.events)
        if method == "DELETE" and path == "/events":
            if not authorized:
                return self.json_response({"error": "unauthorized"}, status=401)
            self.events.clear()
            return self.json_response({"status": "cleared"})

        if not authorized:
            return self.json_response({"error": "unauthorized"}, status=401)
        if method == "GET" and path == "/v1/models":
            return self.json_response(
                {
                    "object": "list",
                    "data": [
                        {
                            "id": "smoke-model",
                            "object": "model",
                            "created": 0,
                            "owned_by": "tether-smoke",
                        }
                    ],
                }
            )
        if method == "POST" and path == "/v1/chat/completions":
            request_body: dict[str, Any] = json.loads(body or b"{}")
            self.events.append({"kind": "model_call", "request": request_body})
            return self.completion_response(request_body)
        return self.json_response({"error": "not found"}, status=404)

    def completion_response(
        self,
        request_body: dict[str, Any],
    ) -> tuple[int, str, bytes]:
        """Request one Todo operation per user turn, then continue with its result."""
        messages: list[dict[str, Any]] = request_body.get("messages", [])
        last_user = max(
            (index for index, message in enumerate(messages) if message.get("role") == "user"),
            default=-1,
        )
        prompt = json.dumps(messages[last_user].get("content", "")).lower()
        tool_messages = [
            message
            for message in messages[last_user + 1 :]
            if message.get("role") == "tool"
        ]
        operation = "list_todos" if "list" in prompt else "create_todo"

        if request_body.get("tools") and not tool_messages:
            arguments = {} if operation == "list_todos" else {"action": TODO_ACTION}
            chunks = self.tool_call_chunks(operation, arguments)
        else:
            content = (
                f"Todo list confirmed: {TODO_ACTION}"
                if operation == "list_todos"
                else f"Todo created: {TODO_ACTION}"
            )
            chunks = self.text_chunks(content)

        event_stream = (
            "".join(
                f"data: {json.dumps(chunk, separators=(',', ':'))}\n\n"
                for chunk in chunks
            )
            + "data: [DONE]\n\n"
        )
        return 200, "text/event-stream", event_stream.encode("utf-8")

    @staticmethod
    def tool_call_chunks(operation: str, arguments: dict[str, Any]) -> list[dict[str, Any]]:
        """Return one native function call in OpenAI's streaming shape."""
        call_id = f"call_{operation}"
        return [
            {
                "id": f"chatcmpl-{operation}",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": "smoke-model",
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": call_id,
                                    "type": "function",
                                    "function": {
                                        "name": operation,
                                        "arguments": json.dumps(arguments, separators=(",", ":")),
                                    },
                                }
                            ],
                        },
                        "finish_reason": None,
                    }
                ],
            },
            {
                "id": f"chatcmpl-{operation}",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": "smoke-model",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
            },
        ]

    @staticmethod
    def text_chunks(content: str) -> list[dict[str, Any]]:
        """Return one deterministic final assistant response."""
        return [
            {
                "id": "chatcmpl-final",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": "smoke-model",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": content},
                        "finish_reason": None,
                    }
                ],
            },
            {
                "id": "chatcmpl-final",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": "smoke-model",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            },
        ]

    @staticmethod
    def json_response(
        content: Any,
        *,
        status: int = 200,
    ) -> tuple[int, str, bytes]:
        """Encode one JSON response."""
        return status, "application/json", json.dumps(content).encode("utf-8")


async def main() -> None:
    """Run until Compose stops the helper container."""
    provider = FakeProvider()
    server = await asyncio.start_server(
        provider.handle_connection,
        host="0.0.0.0",
        port=8081,
    )
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
