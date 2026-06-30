"""Browser WebSocket event bus for chat and invalidation frames."""

from __future__ import annotations

import asyncio
import contextlib
import os
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal, cast
from uuid import UUID

import structlog
from pydantic import BaseModel, StringConstraints, ValidationError
from starlette.routing import WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from tether.agent_trace import AgentTraceRecorder, Termination
from tether.auth import SESSION_COOKIE, verify_session_cookie
from tether.conversations import ConversationNotFoundError, JsonValue, MessageDraft
from tether.events import HubEvent, NotifyEvent
from tether.pi_runtime import PiRuntimeError

_POLICY_VIOLATION = 1008
_AGENT_EVENT_TIMEOUT_SECONDS = 60.0
_LOCALTIME_PATH = Path("/etc/localtime")
_ZONEINFO_MARKER = "zoneinfo/"

type InboundType = Literal["prompt", "abort"]


def _local_timezone_name(now: datetime) -> str:
    """Best-effort IANA name for the host's local zone, falling back to offset.

    pi injects only the date into its system prompt, but daily/weekly triggers
    want an IANA zone, so surface one when the host can determine it — the `TZ`
    env var or the `/etc/localtime` symlink target — and degrade to the numeric
    UTC offset otherwise. `now` must be timezone-aware so the fallback resolves.
    """
    env_zone = os.environ.get("TZ")
    if env_zone:
        return env_zone
    try:
        if _LOCALTIME_PATH.is_symlink():
            target = str(_LOCALTIME_PATH.readlink())
            index = target.rfind(_ZONEINFO_MARKER)
            if index != -1:
                return target[index + len(_ZONEINFO_MARKER) :]
    except OSError:
        pass
    return now.strftime("%z") or "UTC"


def _prompt_with_time_context(
    content: str, *, now: datetime, timezone_name: str
) -> str:
    """Prefix a user turn with the host's wall-clock time for the agent only.

    pi's system prompt carries `Current date: YYYY-MM-DD` with no time, so the
    agent cannot resolve relative scheduling ("in 3 minutes", "tomorrow 9am")
    and stalls asking the user for the current time. This preamble is sent to
    pi only; the persisted user message and the trace keep the clean text.
    """
    stamp = now.isoformat(timespec="seconds")
    note = (
        f"[Tether note — the current time is {stamp} ({timezone_name}). "
        'Resolve relative times like "in 3 minutes" or "tomorrow at 9am" '
        "against it. This note is system-generated; do not mention it.]"
    )
    return f"{note}\n\n{content}"


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


def _is_assistant_message(message: object) -> bool:
    """Report whether a pi message envelope is an assistant turn."""
    if not isinstance(message, dict):
        return False
    return cast("dict[str, Any]", message).get("role") == "assistant"


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


def _prompt_failure_detail(response: dict[str, object]) -> str:
    """Render pi's failed prompt response for the browser."""
    for key in ("error", "detail", "message"):
        value = response.get(key)
        if isinstance(value, str) and value.strip():
            return f"prompt failed: {value}"
    data = response.get("data")
    if isinstance(data, dict):
        data_dict = cast("dict[str, object]", data)
        for key in ("error", "detail", "message"):
            value = data_dict.get(key)
            if isinstance(value, str) and value.strip():
                return f"prompt failed: {value}"
    return "prompt failed"


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
    """Forward assistant stream deltas, keeping reasoning out of the answer."""
    assistant_event = event.get("assistantMessageEvent")
    if not isinstance(assistant_event, dict):
        return
    assistant_event_data = cast("dict[str, Any]", assistant_event)
    sub_type = assistant_event_data.get("type")
    # Only the text channel feeds the persisted answer. Some providers stream
    # plaintext `thinking_delta` reasoning (codex encrypts it, so the leak is
    # masked there) — merging it into `streamed_text` would corrupt the saved
    # assistant message, so persistence is gated on the text sub-type.
    if sub_type == "text_delta":
        streamed_text.append(_delta_text(assistant_event_data))
    await websocket.send_json(
        {
            "type": "chat",
            "conversation_id": str(conversation_id),
            "event": sub_type or "message_update",
            "delta": assistant_event_data.get("delta"),
            "content_index": assistant_event_data.get("contentIndex"),
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
    args = event.get("args")
    tool_args: dict[str, Any] = args if isinstance(args, dict) else {}
    if isinstance(tool_call_id, str):
        pending_tool_args[tool_call_id] = tool_args
    await websocket.send_json(
        {
            "type": "chat",
            "conversation_id": str(conversation_id),
            "event": "tool_start",
            "tool_name": event.get("toolName"),
            "tool_id": tool_call_id if isinstance(tool_call_id, str) else None,
            "tool_args": tool_args,
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
    tool_result = _json_object(event.get("result"))
    if isinstance(tool_call_id, str) and isinstance(tool_name, str):
        _ = await websocket.app.state.conversation_service.append_message(
            MessageDraft(
                content=tool_name,
                conversation_id=conversation_id,
                pi_message_id=tool_call_id,
                role="tool",
                tool_args=pending_tool_args.pop(tool_call_id, {}),
                tool_name=tool_name,
                tool_result=tool_result,
            )
        )
    await websocket.send_json(
        {
            "type": "chat",
            "conversation_id": str(conversation_id),
            "event": "tool_end",
            "tool_name": tool_name,
            "tool_id": tool_call_id if isinstance(tool_call_id, str) else None,
            "tool_result": tool_result,
        }
    )


async def _stream_runtime(
    websocket: WebSocket,
    *,
    conversation_id: UUID,
    runtime: Any,
    recorder: AgentTraceRecorder | None = None,
    session_id: str | None = None,
) -> None:
    """Forward pi events and persist settled assistant messages."""
    pending_tool_args: dict[str, dict[str, Any]] = {}
    streamed_text: list[str] = []
    while True:
        event = await runtime.next_event(wait_seconds=_AGENT_EVENT_TIMEOUT_SECONDS)
        match event.get("type"):
            case "message_start":
                streamed_text.clear()
                if (
                    recorder is not None
                    and session_id is not None
                    and _is_assistant_message(event.get("message"))
                ):
                    recorder.record_model_turn(session_id=session_id)
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
    recorder = cast(
        "AgentTraceRecorder | None",
        getattr(websocket.app.state, "trace_recorder", None),
    )
    session_id = str(conversation.pi_session_id)
    termination: Termination = "completed"
    run_error: str | None = None
    run_id = (
        recorder.begin_run(
            session_id=session_id,
            kind="conversation",
            prompt=content,
            conversation_id=str(conversation_id),
        )
        if recorder is not None
        else None
    )
    # Correlate every host log line emitted while driving this prompt with the
    # run id the tool seam also stamps onto loopback tool calls.
    log_context = {"run_id": run_id} if run_id is not None else {}
    try:
        with structlog.contextvars.bound_contextvars(**log_context):
            runtime = (
                await websocket.app.state.conversation_runtime_registry.runtime_for(
                    conversation
                )
            )
            # Drop any events left in the shared queue by a prior turn that was
            # aborted or cut off by a disconnect, so this prompt streams clean.
            _ = runtime.drain_events()
            # pi only knows the date; stamp the wall-clock time onto the turn so
            # the agent can resolve relative scheduling without asking for it.
            now = datetime.now().astimezone()
            pi_message = _prompt_with_time_context(
                content, now=now, timezone_name=_local_timezone_name(now)
            )
            prompt_response = await runtime.client.request("prompt", message=pi_message)
            if prompt_response.get("success") is not True:
                failure_detail = _prompt_failure_detail(prompt_response)
                termination, run_error = "error", failure_detail
                await _send_error(
                    websocket,
                    conversation_id=conversation_id,
                    detail=failure_detail,
                )
                return
            await _stream_runtime(
                websocket,
                conversation_id=conversation_id,
                runtime=runtime,
                recorder=recorder,
                session_id=session_id,
            )
    except PiRuntimeError as error:
        termination, run_error = "error", str(error)
        await _send_error(
            websocket,
            conversation_id=conversation_id,
            detail=str(error),
        )
    except TimeoutError:
        # pi went silent past the agent-event timeout. Tell the browser so it
        # can leave the generating state instead of hanging on "Stop" forever.
        detail = (
            f"agent timed out (no response in {int(_AGENT_EVENT_TIMEOUT_SECONDS)}s)"
        )
        termination, run_error = "timeout", detail
        await _send_error(
            websocket,
            conversation_id=conversation_id,
            detail=detail,
        )
    except asyncio.CancelledError:
        termination, run_error = "aborted", "generation cancelled"
        raise
    finally:
        if recorder is not None:
            _ = recorder.end_run(
                session_id=session_id, termination=termination, error=run_error
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
    subscription: asyncio.Queue[HubEvent],
) -> None:
    """Forward service-layer invalidation and notification frames to a browser."""
    while True:
        event = await subscription.get()
        if isinstance(event, NotifyEvent):
            await websocket.send_json(
                {
                    "type": "notify",
                    "trigger_id": event.trigger_id,
                    "title": event.title,
                    "body": event.body,
                }
            )
            continue
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
