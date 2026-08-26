"""Throwaway OpenAI-compatible model and bearer-authenticated tool server."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any
from urllib.parse import urlsplit


PROVIDER_TOKEN = "fake-provider-key"
TOOL_TOKEN = "fake-tool-key"


class SpikeServices:
    """Serve deterministic model and tool responses for the migration spike."""

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
            status, content_type, response_body = (
                400,
                "application/json",
                b'{"error":"bad request"}',
            )

        reason = {
            200: "OK",
            400: "Bad Request",
            401: "Unauthorized",
            404: "Not Found",
        }[status]
        writer.write(
            (
                f"HTTP/1.1 {status} {reason}\r\n"
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
        """Return a deterministic response for the spike's small HTTP surface."""
        if method == "GET" and path == "/health":
            return self.json_response({"status": "ok"})
        if method == "GET" and path == "/events":
            return self.json_response(self.events)
        if method == "DELETE" and path == "/events":
            self.events.clear()
            return self.json_response({"status": "cleared"})
        if method == "GET" and path == "/tools/openapi.json":
            authorized = headers.get("authorization") == f"Bearer {TOOL_TOKEN}"
            self.events.append({"kind": "schema_fetch", "authorized": authorized})
            if not authorized:
                return self.json_response({"error": "unauthorized"}, status=401)
            return self.json_response(self.openapi_document())
        if method == "POST" and path == "/tools/spike_echo":
            authorized = headers.get("authorization") == f"Bearer {TOOL_TOKEN}"
            arguments = json.loads(body or b"{}")
            self.events.append(
                {
                    "kind": "tool_call",
                    "authorized": authorized,
                    "arguments": arguments,
                }
            )
            if not authorized:
                return self.json_response({"error": "unauthorized"}, status=401)
            return self.json_response(
                {"success": True, "result": {"echo": arguments.get("message")}}
            )
        if path == "/v1/models" and method == "GET":
            if headers.get("authorization") != f"Bearer {PROVIDER_TOKEN}":
                return self.json_response({"error": "unauthorized"}, status=401)
            return self.json_response(
                {
                    "object": "list",
                    "data": [
                        {
                            "id": "spike-model",
                            "object": "model",
                            "created": 0,
                            "owned_by": "spike",
                        }
                    ],
                }
            )
        if path == "/v1/audio/transcriptions" and method == "POST":
            authorized = headers.get("authorization") == f"Bearer {PROVIDER_TOKEN}"
            self.events.append({"kind": "transcription", "authorized": authorized})
            if not authorized:
                return self.json_response({"error": "unauthorized"}, status=401)
            return self.json_response({"text": "voice spike works"})
        if path == "/v1/chat/completions" and method == "POST":
            if headers.get("authorization") != f"Bearer {PROVIDER_TOKEN}":
                return self.json_response({"error": "unauthorized"}, status=401)
            request_body = json.loads(body or b"{}")
            self.events.append({"kind": "model_call", "request": request_body})
            return self.completion_response(request_body)
        return self.json_response({"error": "not found"}, status=404)

    def completion_response(
        self,
        request_body: dict[str, Any],
    ) -> tuple[int, str, bytes]:
        """Ask for one tool call, then finish after its result is returned."""
        messages = request_body.get("messages", [])
        has_tool_result = any(message.get("role") == "tool" for message in messages)
        has_tools = bool(request_body.get("tools"))

        if has_tools and not has_tool_result:
            chunks = [
                {
                    "id": "chatcmpl-spike",
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": "spike-model",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "role": "assistant",
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call_spike_echo",
                                        "type": "function",
                                        "function": {
                                            "name": "spike_echo",
                                            "arguments": '{"message":"hello from tool"}',
                                        },
                                    }
                                ],
                            },
                            "finish_reason": None,
                        }
                    ],
                },
                {
                    "id": "chatcmpl-spike",
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": "spike-model",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {},
                            "finish_reason": "tool_calls",
                        }
                    ],
                },
            ]
        else:
            content = (
                "Tool result received: hello from tool"
                if has_tool_result
                else "Spike Chat"
            )
            chunks = [
                {
                    "id": "chatcmpl-spike-final",
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": "spike-model",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"role": "assistant", "content": content},
                            "finish_reason": None,
                        }
                    ],
                },
                {
                    "id": "chatcmpl-spike-final",
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": "spike-model",
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                },
            ]

        event_stream = (
            "".join(
                f"data: {json.dumps(chunk, separators=(',', ':'))}\n\n"
                for chunk in chunks
            )
            + "data: [DONE]\n\n"
        )
        return 200, "text/event-stream", event_stream.encode("utf-8")

    @staticmethod
    def json_response(
        content: Any,
        *,
        status: int = 200,
    ) -> tuple[int, str, bytes]:
        """Encode one JSON HTTP response."""
        return status, "application/json", json.dumps(content).encode("utf-8")

    @staticmethod
    def openapi_document() -> dict[str, Any]:
        """Describe the single operation Open WebUI should expose to the model."""
        return {
            "openapi": "3.1.0",
            "info": {
                "title": "Tether Spike",
                "version": "1.0.0",
                "description": "Deterministic migration spike tool server.",
            },
            "paths": {
                "/tools/spike_echo": {
                    "post": {
                        "operationId": "spike_echo",
                        "description": "Echo a message through the Tether spike.",
                        "requestBody": {
                            "required": True,
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "message": {
                                                "type": "string",
                                                "description": "Message to echo.",
                                            }
                                        },
                                        "required": ["message"],
                                    }
                                }
                            },
                        },
                        "responses": {"200": {"description": "Echo result."}},
                    }
                }
            },
        }


async def main() -> None:
    """Run the throwaway services until the container stops."""
    services = SpikeServices()
    server = await asyncio.start_server(
        services.handle_connection,
        host="0.0.0.0",
        port=8081,
    )
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
