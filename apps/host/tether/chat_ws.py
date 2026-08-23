"""Authenticated WebSocket transport for chat and host events."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Annotated, Literal, Protocol, cast
from uuid import UUID

from pydantic import BaseModel, StringConstraints, ValidationError
from starlette.routing import WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from tether.agent_trace_recorder import AgentTraceRecorder
from tether.auth_sessions import SESSION_COOKIE, verify_session_cookie
from tether.chat_engine import ConversationRuntimeRegistry
from tether.chat_frames import AbortAckFrame, InvalidateFrame, NotifyFrame, ReplyMode
from tether.chat_turn import (
    ChatPromptSpec,
    ChatTurnDependencies,
    ConversationTurnQueue,
    run_chat_prompt,
    send_chat_error,
    send_session_status,
)
from tether.conversations import ConversationService
from tether.dreaming import DreamingService
from tether.events import EventHub, HubEvent, NotifyEvent
from tether.structured_logging import Logger

_POLICY_VIOLATION = 1008

type InboundType = Literal["prompt", "abort", "session_status"]


class InboundFrame(BaseModel):
    """Validated browser-to-host event frame."""

    type: InboundType
    conversation_id: UUID
    content: (
        Annotated[
            str,
            StringConstraints(strip_whitespace=True, min_length=1),
        ]
        | None
    ) = None
    reply_mode: ReplyMode | None = None
    """Turn-level presentation mode; omitted values default to text."""


class _ChatRuntime(Protocol):
    """Chat dependencies available while the host serves WebSockets."""

    conversation_runtime_registry: ConversationRuntimeRegistry
    conversation_service: ConversationService
    conversation_turn_queue: ConversationTurnQueue
    dreaming_service: DreamingService
    dreaming_enabled: bool
    event_hub: EventHub
    logger: Logger
    session_secret: str
    trace_recorder: AgentTraceRecorder


def _runtime(websocket: WebSocket) -> _ChatRuntime:
    """Read chat dependencies from the canonical host runtime."""
    return cast("_ChatRuntime", websocket.app.state.runtime)


def _turn_dependencies(websocket: WebSocket) -> ChatTurnDependencies:
    """Project the host runtime onto the explicit chat-turn dependency bundle."""
    runtime = _runtime(websocket)
    return ChatTurnDependencies(
        conversation_service=runtime.conversation_service,
        dreaming_service=runtime.dreaming_service,
        dreaming_enabled=runtime.dreaming_enabled,
        runtime_registry=runtime.conversation_runtime_registry,
        trace_recorder=runtime.trace_recorder,
        turn_queue=runtime.conversation_turn_queue,
        logger=runtime.logger,
    )


async def _handle_frame(
    websocket: WebSocket,
    frame: InboundFrame,
    running_generations: dict[UUID, asyncio.Task[str]],
) -> None:
    """Dispatch one validated browser event without blocking the socket reader."""
    match frame.type:
        case "prompt":
            if frame.content is None:
                await send_chat_error(
                    websocket,
                    conversation_id=frame.conversation_id,
                    detail="prompt content is required",
                )
                return
            running = running_generations.get(frame.conversation_id)
            if running is not None and not running.done():
                await send_chat_error(
                    websocket,
                    conversation_id=frame.conversation_id,
                    detail="generation already running",
                )
                return
            running_generations[frame.conversation_id] = asyncio.create_task(
                run_chat_prompt(
                    websocket,
                    _turn_dependencies(websocket),
                    ChatPromptSpec(
                        conversation_id=frame.conversation_id,
                        content=frame.content,
                        reply_mode=frame.reply_mode or "text",
                    ),
                )
            )
        case "abort":
            runtime = _runtime(websocket).conversation_runtime_registry.current_for(
                frame.conversation_id
            )
            if runtime is not None:
                _ = await runtime.client.request("abort")
            await websocket.send_json(
                AbortAckFrame(conversation_id=frame.conversation_id).wire()
            )
        case "session_status":
            runtime = _runtime(websocket).conversation_runtime_registry.current_for(
                frame.conversation_id
            )
            if runtime is not None:
                await send_session_status(websocket, runtime, frame.conversation_id)


async def _event_pump(
    websocket: WebSocket,
    subscription: asyncio.Queue[HubEvent],
) -> None:
    """Forward service invalidations and notifications to one browser."""
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
    """Serve one authenticated browser WebSocket connection."""
    principal = verify_session_cookie(
        websocket.cookies.get(SESSION_COOKIE, ""),
        _runtime(websocket).session_secret,
    )
    if principal is None:
        await websocket.close(code=_POLICY_VIOLATION)
        return
    await websocket.accept()
    subscription = _runtime(websocket).event_hub.subscribe()
    event_task = asyncio.create_task(_event_pump(websocket, subscription))
    running_generations: dict[UUID, asyncio.Task[str]] = {}
    try:
        while True:
            try:
                frame = InboundFrame.model_validate(await websocket.receive_json())
            except ValidationError as error:
                await send_chat_error(
                    websocket,
                    detail=error.errors(include_url=False)[0]["msg"],
                )
                continue
            await _handle_frame(websocket, frame, running_generations)
    except WebSocketDisconnect:
        return
    finally:
        _runtime(websocket).event_hub.unsubscribe(subscription)
        _ = event_task.cancel()
        for task in running_generations.values():
            _ = task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await event_task
        for task in running_generations.values():
            with contextlib.suppress(asyncio.CancelledError):
                await task


websocket_routes: list[WebSocketRoute] = [WebSocketRoute("/ws", websocket_bus)]
