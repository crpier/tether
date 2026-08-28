"""Authenticated WebSocket transport for chat and host events."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Annotated, Literal, Protocol, cast
from uuid import UUID

from pydantic import BaseModel, Field, StringConstraints, ValidationError
from starlette.routing import WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from tether.attachments import AttachmentSubmissionError
from tether.auth_sessions import SESSION_COOKIE, verify_session_cookie
from tether.chat_engine import ConversationRuntimeRegistry
from tether.chat_frames import AbortAckFrame, ErrorFrame, InvalidateFrame, NotifyFrame
from tether.chat_prompt import ReplyMode
from tether.chat_turn import send_session_status
from tether.conversation_model import ConversationNotFoundError
from tether.conversation_turns import (
    BrowserChatFrameSink,
    CancelTurnRequest,
    ConversationTurnConflictError,
    ConversationTurnNotFoundError,
    ConversationTurns,
    InteractiveTurnRequest,
)
from tether.events import EventHub, HubEvent, NotifyEvent
from tether.pi_errors import PiRuntimeError

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
    request_id: UUID | None = None
    turn_id: UUID | None = None
    attachment_ids: tuple[UUID, ...] = Field(default=(), max_length=4)


class _ChatRuntime(Protocol):
    """Chat modules available while the host serves WebSockets."""

    conversation_runtime_registry: ConversationRuntimeRegistry
    conversation_turns: ConversationTurns
    event_hub: EventHub
    session_secret: str


def _runtime(websocket: WebSocket) -> _ChatRuntime:
    """Read chat dependencies from the canonical host runtime."""
    return cast("_ChatRuntime", websocket.app.state.runtime)


async def _send_input_error(
    sink: BrowserChatFrameSink,
    *,
    detail: str,
    conversation_id: UUID | None = None,
    turn_id: UUID | None = None,
) -> None:
    """Send one stable browser input error through the typed adapter."""
    await sink.send(
        ErrorFrame(
            conversation_id=conversation_id,
            detail=detail,
            turn_id=turn_id,
        )
    )


async def _handle_prompt(
    websocket: WebSocket,
    sink: BrowserChatFrameSink,
    frame: InboundFrame,
) -> None:
    """Validate and translate one browser prompt submission."""
    if frame.content is None and not frame.attachment_ids:
        await _send_input_error(
            sink,
            conversation_id=frame.conversation_id,
            detail="prompt content or an attachment is required",
        )
        return
    if frame.request_id is None:
        await _send_input_error(
            sink,
            conversation_id=frame.conversation_id,
            detail="prompt request_id is required",
        )
        return
    try:
        _ = await _runtime(websocket).conversation_turns.submit(
            InteractiveTurnRequest(
                attachment_ids=frame.attachment_ids,
                conversation_id=frame.conversation_id,
                prompt=frame.content or "",
                reply_mode=frame.reply_mode or "text",
                request_id=frame.request_id,
            ),
            sink,
        )
    except AttachmentSubmissionError as error:
        await _send_input_error(
            sink,
            conversation_id=frame.conversation_id,
            detail=str(error),
        )
    except ConversationTurnConflictError:
        await _send_input_error(
            sink,
            conversation_id=frame.conversation_id,
            detail="request_id conflicts with an existing turn",
        )
    except ConversationNotFoundError:
        await _send_input_error(
            sink,
            conversation_id=frame.conversation_id,
            detail="conversation is unknown or archived",
        )
    except PiRuntimeError:
        await _send_input_error(
            sink,
            conversation_id=frame.conversation_id,
            detail="conversation turn execution is unavailable",
        )


async def _handle_frame(
    websocket: WebSocket,
    sink: BrowserChatFrameSink,
    frame: InboundFrame,
) -> None:
    """Dispatch one browser event without giving the socket execution ownership."""
    match frame.type:
        case "prompt":
            await _handle_prompt(websocket, sink, frame)
        case "abort":
            if frame.turn_id is None:
                await _send_input_error(
                    sink,
                    conversation_id=frame.conversation_id,
                    detail="abort turn_id is required",
                )
                return
            try:
                _ = await _runtime(websocket).conversation_turns.cancel(
                    CancelTurnRequest(
                        conversation_id=frame.conversation_id,
                        turn_id=frame.turn_id,
                    )
                )
            except ConversationTurnNotFoundError:
                await _send_input_error(
                    sink,
                    conversation_id=frame.conversation_id,
                    detail="conversation turn not found",
                    turn_id=frame.turn_id,
                )
                return
            await sink.send(
                AbortAckFrame(
                    conversation_id=frame.conversation_id,
                    turn_id=frame.turn_id,
                )
            )
        case "session_status":
            runtime = _runtime(websocket).conversation_runtime_registry.current_for(
                frame.conversation_id
            )
            if runtime is not None:
                await send_session_status(sink, runtime, frame.conversation_id)


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
    sink = BrowserChatFrameSink(websocket)
    subscription = _runtime(websocket).event_hub.subscribe()
    event_task = asyncio.create_task(_event_pump(websocket, subscription))
    try:
        while True:
            try:
                frame = InboundFrame.model_validate(await websocket.receive_json())
            except ValidationError as error:
                await _send_input_error(
                    sink,
                    detail=error.errors(include_url=False)[0]["msg"],
                )
                continue
            await _handle_frame(websocket, sink, frame)
    except WebSocketDisconnect:
        return
    finally:
        sink.detach()
        _runtime(websocket).event_hub.unsubscribe(subscription)
        _ = event_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await event_task


websocket_routes: list[WebSocketRoute] = [WebSocketRoute("/ws", websocket_bus)]
