"""Browser WebSocket event bus for chat and invalidation frames."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Annotated, Any, Literal, cast
from uuid import UUID

from pydantic import BaseModel, StringConstraints, ValidationError
from starlette.routing import WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from tether.auth import SESSION_COOKIE, verify_session_cookie
from tether.conversations import ConversationNotFoundError, JsonValue, MessageDraft
from tether.events import InvalidateEvent
from tether.pi_runtime import PiRuntimeError

_POLICY_VIOLATION = 1008
_AGENT_EVENT_TIMEOUT_SECONDS = 60.0

type InboundType = Literal["prompt", "abort"]


class InboundFrame(BaseModel):
    """Browser-to-host event frame."""

    type: InboundType
    conversation_id: UUID
    content: (
        Annotated[
            str,
            StringConstraints(strip_whitespace=True, min_length=1),
        ]
        | None
    ) = None


def _json_object(value: object) -> dict[str, JsonValue]:
    """Return JSON object values as dictionaries."""
    if isinstance(value, dict):
        return cast("dict[str, JsonValue]", value)
    return {"value": cast("JsonValue", value)}


def _delta_text(assistant_event: dict[str, Any]) -> str:
    """Extract text from pi assistant message update payloads."""
    delta = assistant_event.get("delta")
    if isinstance(delta, str):
        return delta
    if isinstance(delta, dict):
        delta_data = cast("dict[str, object]", delta)
        text = delta_data.get("text")
        if isinstance(text, str):
            return text
    text = assistant_event.get("text")
    if isinstance(text, str):
        return text
    return ""


def _message_text(message: object) -> str:
    """Extract displayed text from a pi message content array."""
    if not isinstance(message, dict):
        return ""
    message_data = cast("dict[str, Any]", message)
    content = message_data.get("content")
    if not isinstance(content, list):
        return ""
    content_items = cast("list[object]", content)
    chunks: list[str] = []
    for raw_item in content_items:
        if not isinstance(raw_item, dict):
            continue
        item = cast("dict[str, Any]", raw_item)
        if item.get("type") == "text" and isinstance(item.get("text"), str):
            chunks.append(cast("str", item["text"]))
    return "".join(chunks)


async def _send_error(
    websocket: WebSocket,
    *,
    detail: str,
    conversation_id: UUID | None = None,
) -> None:
    """Send a tagged chat error frame."""
    frame: dict[str, object] = {"type": "chat", "event": "error", "detail": detail}
    if conversation_id is not None:
        frame["conversation_id"] = str(conversation_id)
    await websocket.send_json(frame)


async def _forward_message_start(
    websocket: WebSocket, *, conversation_id: UUID, event: dict[str, Any]
) -> None:
    """Forward assistant message start events."""
    message = event.get("message")
    if not isinstance(message, dict):
        return
    message_data = cast("dict[str, Any]", message)
    if message_data.get("role") == "assistant":
        await websocket.send_json(
            {
                "type": "chat",
                "conversation_id": str(conversation_id),
                "event": "message_start",
            }
        )


async def _forward_message_update(
    websocket: WebSocket,
    *,
    conversation_id: UUID,
    event: dict[str, Any],
    streamed_text: list[str],
) -> None:
    """Forward assistant stream deltas."""
    assistant_event = event.get("assistantMessageEvent")
    if not isinstance(assistant_event, dict):
        return
    assistant_event_data = cast("dict[str, Any]", assistant_event)
    streamed_text.append(_delta_text(assistant_event_data))
    await websocket.send_json(
        {
            "type": "chat",
            "conversation_id": str(conversation_id),
            "event": assistant_event_data.get("type", "message_update"),
            "delta": assistant_event_data.get("delta"),
        }
    )


async def _settle_message_end(
    websocket: WebSocket,
    *,
    conversation_id: UUID,
    event: dict[str, Any],
    streamed_text: list[str],
) -> None:
    """Persist assistant text once pi settles a message."""
    message = event.get("message")
    if not isinstance(message, dict):
        return
    message_data = cast("dict[str, Any]", message)
    if message_data.get("role") != "assistant":
        return
    content = _message_text(message_data) or "".join(streamed_text)
    if content:
        _ = await websocket.app.state.conversation_service.append_message(
            MessageDraft(
                content=content,
                conversation_id=conversation_id,
                role="assistant",
            )
        )
    await websocket.send_json(
        {
            "type": "chat",
            "conversation_id": str(conversation_id),
            "event": "message_end",
        }
    )


async def _forward_tool_start(
    websocket: WebSocket,
    *,
    conversation_id: UUID,
    event: dict[str, Any],
    pending_tool_args: dict[str, dict[str, Any]],
) -> None:
    """Remember tool args and forward tool-start events."""
    tool_call_id = event.get("toolCallId")
    if isinstance(tool_call_id, str):
        args = event.get("args")
        pending_tool_args[tool_call_id] = args if isinstance(args, dict) else {}
    await websocket.send_json(
        {
            "type": "chat",
            "conversation_id": str(conversation_id),
            "event": "tool_start",
            "tool_name": event.get("toolName"),
        }
    )


async def _settle_tool_end(
    websocket: WebSocket,
    *,
    conversation_id: UUID,
    event: dict[str, Any],
    pending_tool_args: dict[str, dict[str, Any]],
) -> None:
    """Persist tool completion envelopes and forward tool-end events."""
    tool_call_id = event.get("toolCallId")
    tool_name = event.get("toolName")
    if isinstance(tool_call_id, str) and isinstance(tool_name, str):
        _ = await websocket.app.state.conversation_service.append_message(
            MessageDraft(
                content=tool_name,
                conversation_id=conversation_id,
                pi_message_id=tool_call_id,
                role="tool",
                tool_args=pending_tool_args.pop(tool_call_id, {}),
                tool_name=tool_name,
                tool_result=_json_object(event.get("result")),
            )
        )
    await websocket.send_json(
        {
            "type": "chat",
            "conversation_id": str(conversation_id),
            "event": "tool_end",
            "tool_name": tool_name,
        }
    )


async def _stream_runtime(
    websocket: WebSocket,
    *,
    conversation_id: UUID,
    runtime: Any,
) -> None:
    """Forward pi events and persist settled assistant messages."""
    pending_tool_args: dict[str, dict[str, Any]] = {}
    streamed_text: list[str] = []
    while True:
        event = await runtime.next_event(wait_seconds=_AGENT_EVENT_TIMEOUT_SECONDS)
        match event.get("type"):
            case "message_start":
                streamed_text.clear()
                await _forward_message_start(
                    websocket, conversation_id=conversation_id, event=event
                )
            case "message_update":
                await _forward_message_update(
                    websocket,
                    conversation_id=conversation_id,
                    event=event,
                    streamed_text=streamed_text,
                )
            case "message_end":
                await _settle_message_end(
                    websocket,
                    conversation_id=conversation_id,
                    event=event,
                    streamed_text=streamed_text,
                )
                streamed_text.clear()
            case "tool_execution_start":
                await _forward_tool_start(
                    websocket,
                    conversation_id=conversation_id,
                    event=event,
                    pending_tool_args=pending_tool_args,
                )
            case "tool_execution_end":
                await _settle_tool_end(
                    websocket,
                    conversation_id=conversation_id,
                    event=event,
                    pending_tool_args=pending_tool_args,
                )
            case "agent_end":
                await websocket.send_json(
                    {
                        "type": "chat",
                        "conversation_id": str(conversation_id),
                        "event": "agent_end",
                    }
                )
                return
            case _:
                pass


async def _run_prompt(
    websocket: WebSocket,
    *,
    conversation_id: UUID,
    content: str,
) -> None:
    """Forward one prompt to pi, then stream its events."""
    try:
        conversation = (
            await websocket.app.state.conversation_service.fetch_conversation(
                conversation_id
            )
        )
        message = await websocket.app.state.conversation_service.append_message(
            MessageDraft(
                content=content,
                conversation_id=conversation_id,
                role="user",
            )
        )
    except ConversationNotFoundError:
        await _send_error(
            websocket,
            conversation_id=conversation_id,
            detail="conversation not found",
        )
        return
    await websocket.send_json(
        {
            "type": "chat",
            "conversation_id": str(conversation_id),
            "event": "user_message",
            "message_id": str(message.id),
            "seq": message.seq,
        }
    )
    try:
        runtime = await websocket.app.state.conversation_runtime_registry.runtime_for(
            conversation
        )
        prompt_response = await runtime.client.request("prompt", message=content)
        if prompt_response.get("success") is not True:
            await _send_error(
                websocket,
                conversation_id=conversation_id,
                detail="prompt failed",
            )
            return
        await _stream_runtime(
            websocket,
            conversation_id=conversation_id,
            runtime=runtime,
        )
    except PiRuntimeError as error:
        await _send_error(
            websocket,
            conversation_id=conversation_id,
            detail=str(error),
        )


async def _handle_frame(
    websocket: WebSocket,
    frame: InboundFrame,
    running_generations: dict[UUID, asyncio.Task[None]],
) -> None:
    """Run one validated inbound event through the host service layer."""
    match frame.type:
        case "prompt":
            if frame.content is None:
                await _send_error(
                    websocket,
                    conversation_id=frame.conversation_id,
                    detail="prompt content is required",
                )
                return
            running = running_generations.get(frame.conversation_id)
            if running is not None and not running.done():
                await _send_error(
                    websocket,
                    conversation_id=frame.conversation_id,
                    detail="generation already running",
                )
                return
            task = asyncio.create_task(
                _run_prompt(
                    websocket,
                    conversation_id=frame.conversation_id,
                    content=frame.content,
                )
            )
            running_generations[frame.conversation_id] = task
        case "abort":
            runtime = websocket.app.state.conversation_runtime_registry.current_for(
                frame.conversation_id
            )
            if runtime is not None:
                _ = await runtime.client.request("abort")
            await websocket.send_json(
                {
                    "type": "chat",
                    "conversation_id": str(frame.conversation_id),
                    "event": "abort_ack",
                }
            )


async def _event_pump(
    websocket: WebSocket,
    subscription: asyncio.Queue[InvalidateEvent],
) -> None:
    """Forward service-layer invalidations to one browser connection."""
    while True:
        event = await subscription.get()
        await websocket.send_json({"type": "invalidate", "keys": event.keys})


async def websocket_bus(websocket: WebSocket) -> None:
    """Accept one authenticated browser WebSocket connection."""
    principal = verify_session_cookie(
        websocket.cookies.get(SESSION_COOKIE, ""),
        cast("str", websocket.app.state.session_secret),
    )
    if principal is None:
        await websocket.close(code=_POLICY_VIOLATION)
        return
    await websocket.accept()
    subscription = websocket.app.state.event_hub.subscribe()
    event_task = asyncio.create_task(_event_pump(websocket, subscription))
    running_generations: dict[UUID, asyncio.Task[None]] = {}
    try:
        while True:
            try:
                frame = InboundFrame.model_validate(await websocket.receive_json())
            except ValidationError as error:
                await _send_error(
                    websocket,
                    detail=error.errors(include_url=False)[0]["msg"],
                )
                continue
            await _handle_frame(websocket, frame, running_generations)
    except WebSocketDisconnect:
        return
    finally:
        websocket.app.state.event_hub.unsubscribe(subscription)
        _ = event_task.cancel()
        for task in running_generations.values():
            _ = task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await event_task
        for task in running_generations.values():
            with contextlib.suppress(asyncio.CancelledError):
                await task


websocket_routes: list[WebSocketRoute] = [WebSocketRoute("/ws", websocket_bus)]
