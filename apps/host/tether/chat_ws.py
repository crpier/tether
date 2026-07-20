"""Browser WebSocket event bus for chat and invalidation frames."""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal, cast
from uuid import UUID

from pydantic import BaseModel, StringConstraints, ValidationError
from starlette.routing import WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from tether.agent_trace import AgentTraceRecorder, record_run
from tether.auth import SESSION_COOKIE, verify_session_cookie
from tether.chat_frames import (
    AbortAckFrame,
    AgentEndFrame,
    ErrorFrame,
    InvalidateFrame,
    MessageEndFrame,
    MessageStartFrame,
    NotifyFrame,
    StreamUpdateFrame,
    ToolEndFrame,
    ToolStartFrame,
    UserMessageFrame,
)
from tether.conversations import (
    SESSION_GAP as _SESSION_GAP,
)
from tether.conversations import (
    ConversationNotFoundError,
    MessageDraft,
)
from tether.events import HubEvent, NotifyEvent
from tether.pi_runtime import (
    AgentEnded,
    AssistantStreamNote,
    MessageSettled,
    ModelTurnStarted,
    PiRuntimeError,
    TextDelta,
    ThinkingDelta,
    ToolSettled,
    ToolStarted,
    TurnEvent,
)

_POLICY_VIOLATION = 1008
_AGENT_EVENT_TIMEOUT_SECONDS = 60.0
_LOCALTIME_PATH = Path("/etc/localtime")
_ZONEINFO_MARKER = "zoneinfo/"

type InboundType = Literal["prompt", "abort"]


def local_timezone_name(now: datetime) -> str:
    """Best-effort IANA name for the host's local zone, falling back to offset.

    pi injects only the date into its system prompt, but daily/weekly triggers
    want an IANA zone, so surface one when the host can determine it — the `TZ`
    env var or the `/etc/localtime` symlink target — and degrade to the numeric
    UTC offset otherwise. `now` must be timezone-aware so the fallback resolves.

    Public (not module-private) because `tether.gmail` reuses it as the default
    local-timezone provider for its deadline-trigger fire time.
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
    await websocket.send_json(
        ErrorFrame(detail=detail, conversation_id=conversation_id).wire()
    )


async def _relay_stream_update(
    websocket: WebSocket,
    *,
    conversation_id: UUID,
    update: TextDelta | ThinkingDelta | AssistantStreamNote,
    streamed_text: list[str],
    streamed_reasoning: list[str],
) -> None:
    """Accumulate delta text on its channel and forward the update verbatim.

    Text and reasoning chunks land in separate buffers because they settle
    into distinct transcript rows; the raw provider delta rides along
    unchanged so the browser sees exactly what pi emitted.
    """
    match update:
        case TextDelta(text=chunk):
            event_name = "text_delta"
            streamed_text.append(chunk)
        case ThinkingDelta(text=chunk):
            event_name = "thinking_delta"
            streamed_reasoning.append(chunk)
        case AssistantStreamNote(kind=kind):
            event_name = kind
    await websocket.send_json(
        StreamUpdateFrame(
            conversation_id=conversation_id,
            event=event_name,
            delta=update.raw_delta,
            content_index=update.content_index,
        ).wire()
    )


async def _settle_message_end(
    websocket: WebSocket,
    *,
    conversation_id: UUID,
    settled: MessageSettled,
    streamed_text: list[str],
    streamed_reasoning: list[str],
) -> None:
    """Persist the settled reasoning and answer rows once pi closes a message.

    Reasoning is persisted ahead of the answer so the transcript keeps
    "thinking then reply" order under the monotonic per-thread sequence.
    Either channel falls back to its accumulated stream deltas when the
    settled message carries no content of that kind.
    """
    reasoning = settled.reasoning or "".join(streamed_reasoning)
    if reasoning:
        _ = await websocket.app.state.conversation_service.append_message(
            MessageDraft(
                content=reasoning,
                conversation_id=conversation_id,
                role="reasoning",
            )
        )
    content = settled.text or "".join(streamed_text)
    if content:
        _ = await websocket.app.state.conversation_service.append_message(
            MessageDraft(
                content=content,
                conversation_id=conversation_id,
                role="assistant",
            )
        )
    await websocket.send_json(MessageEndFrame(conversation_id=conversation_id).wire())


async def _forward_tool_start(
    websocket: WebSocket,
    *,
    conversation_id: UUID,
    started: ToolStarted,
    pending_tool_args: dict[str, dict[str, Any]],
) -> None:
    """Remember tool args and forward tool-start events."""
    if started.tool_call_id is not None:
        pending_tool_args[started.tool_call_id] = started.args
    await websocket.send_json(
        ToolStartFrame(
            conversation_id=conversation_id,
            tool_name=started.tool_name,
            tool_id=started.tool_call_id,
            tool_args=started.args,
        ).wire()
    )


async def _settle_tool_end(
    websocket: WebSocket,
    *,
    conversation_id: UUID,
    settled: ToolSettled,
    pending_tool_args: dict[str, dict[str, Any]],
) -> None:
    """Persist tool completion envelopes and forward tool-end events."""
    if settled.tool_call_id is not None and settled.tool_name is not None:
        _ = await websocket.app.state.conversation_service.append_message(
            MessageDraft(
                content=settled.tool_name,
                conversation_id=conversation_id,
                pi_message_id=settled.tool_call_id,
                role="tool",
                tool_args=pending_tool_args.pop(settled.tool_call_id, {}),
                tool_name=settled.tool_name,
                tool_result=settled.result,
            )
        )
    await websocket.send_json(
        ToolEndFrame(
            conversation_id=conversation_id,
            tool_name=settled.tool_name,
            tool_id=settled.tool_call_id,
            tool_result=settled.result,
        ).wire()
    )


async def _stream_runtime(
    websocket: WebSocket,
    *,
    conversation_id: UUID,
    runtime: Any,
    recorder: AgentTraceRecorder | None = None,
    session_id: str | None = None,
) -> None:
    """Relay one turn's typed pi events to the browser and persist settled rows.

    Text and reasoning deltas are accumulated separately and never merged:
    they settle into distinct transcript rows. Encrypted reasoning (codex) is
    masked on the wire, so its deltas carry no plaintext and the reasoning row
    is simply skipped.
    """
    pending_tool_args: dict[str, dict[str, Any]] = {}
    streamed_text: list[str] = []
    streamed_reasoning: list[str] = []
    turn_stream: AsyncGenerator[TurnEvent] = runtime.stream_turn(
        wait_seconds=_AGENT_EVENT_TIMEOUT_SECONDS
    )
    async for turn_event in turn_stream:
        match turn_event:
            case ModelTurnStarted():
                streamed_text.clear()
                streamed_reasoning.clear()
                if recorder is not None and session_id is not None:
                    recorder.record_model_turn(session_id=session_id)
                await websocket.send_json(
                    MessageStartFrame(conversation_id=conversation_id).wire()
                )
            case TextDelta() | ThinkingDelta() | AssistantStreamNote():
                await _relay_stream_update(
                    websocket,
                    conversation_id=conversation_id,
                    update=turn_event,
                    streamed_text=streamed_text,
                    streamed_reasoning=streamed_reasoning,
                )
            case MessageSettled():
                await _settle_message_end(
                    websocket,
                    conversation_id=conversation_id,
                    settled=turn_event,
                    streamed_text=streamed_text,
                    streamed_reasoning=streamed_reasoning,
                )
                streamed_text.clear()
                streamed_reasoning.clear()
            case ToolStarted():
                await _forward_tool_start(
                    websocket,
                    conversation_id=conversation_id,
                    started=turn_event,
                    pending_tool_args=pending_tool_args,
                )
            case ToolSettled():
                await _settle_tool_end(
                    websocket,
                    conversation_id=conversation_id,
                    settled=turn_event,
                    pending_tool_args=pending_tool_args,
                )
            case AgentEnded():
                await websocket.send_json(
                    AgentEndFrame(conversation_id=conversation_id).wire()
                )


async def _run_prompt(
    websocket: WebSocket,
    *,
    conversation_id: UUID,
    content: str,
) -> None:
    """Forward one prompt to pi, then stream its events."""
    try:
        service = websocket.app.state.conversation_service
        conversation = await service.fetch_conversation(conversation_id)
        # Decide which pi session receives this turn *before* recording the new
        # user row, so the gap is measured against the previous turn. A cold gap
        # rotates the conversation onto a fresh pi session.
        conversation = await service.resolve_session(
            conversation, now=datetime.now(UTC), gap=_SESSION_GAP
        )
        message = await service.append_message(
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
        UserMessageFrame(
            conversation_id=conversation_id,
            message_id=message.id,
            seq=message.seq,
        ).wire()
    )
    recorder = cast(
        "AgentTraceRecorder | None",
        getattr(websocket.app.state, "trace_recorder", None),
    )
    session_id = str(conversation.pi_session_id)
    try:
        with record_run(
            recorder,
            session_id=session_id,
            kind="conversation",
            prompt=content,
            conversation_id=str(conversation_id),
        ) as run:
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
                content, now=now, timezone_name=local_timezone_name(now)
            )
            prompt_response = await runtime.client.request("prompt", message=pi_message)
            if prompt_response.get("success") is not True:
                failure_detail = _prompt_failure_detail(prompt_response)
                run.mark("error", failure_detail)
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
        await _send_error(
            websocket,
            conversation_id=conversation_id,
            detail=str(error),
        )
    except TimeoutError:
        # pi went silent past the agent-event timeout. Tell the browser so it
        # can leave the generating state instead of hanging on "Stop" forever.
        await _send_error(
            websocket,
            conversation_id=conversation_id,
            detail=(
                f"agent timed out (no response in {int(_AGENT_EVENT_TIMEOUT_SECONDS)}s)"
            ),
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
                AbortAckFrame(conversation_id=frame.conversation_id).wire()
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
                NotifyFrame(
                    trigger_id=event.trigger_id,
                    title=event.title,
                    body=event.body,
                ).wire()
            )
            continue
        await websocket.send_json(InvalidateFrame(keys=event.keys).wire())


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
