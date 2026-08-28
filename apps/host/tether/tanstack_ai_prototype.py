"""Throwaway TanStack AI protocol experiment.

This module exists only on the prototype branch. It must not be merged or deployed.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Annotated, Literal, Protocol, cast
from uuid import UUID, uuid7

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Field
from snekql.sqlite import Fetched, select
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse

from tether.chat_frames import (
    AgentEndFrame,
    ChatFrame,
    ErrorFrame,
    MessageEndFrame,
    MessageStartFrame,
    StreamUpdateFrame,
    ToolEndFrame,
    ToolStartFrame,
    TurnEndedFrame,
    TurnQueuedFrame,
    UserMessageFrame,
)
from tether.chat_prompt import ReplyMode
from tether.conversation_model import ConversationNotFoundError
from tether.conversation_store import ConversationTurn, Message
from tether.conversation_turns import ConversationTurns, InteractiveTurnRequest
from tether.conversations import ConversationService


class _PrototypeRuntime(Protocol):
    """Host dependencies used by the isolated protocol experiment."""

    conversation_service: ConversationService
    conversation_turns: ConversationTurns


class _WireMessage(BaseModel):
    """One AG-UI input Message needed to locate the new user prompt."""

    content: str | list[dict[str, object]] | None = None
    id: str
    role: Literal["assistant", "reasoning", "system", "tool", "user"]


class _RunAgentInput(BaseModel):
    """Small accepted subset of AG-UI `RunAgentInput`."""

    model_config = ConfigDict(populate_by_name=True)

    forwarded_props: dict[str, object] = Field(
        default_factory=dict, alias="forwardedProps"
    )
    messages: list[_WireMessage]
    run_id: str = Field(alias="runId")
    thread_id: UUID = Field(alias="threadId")


def _runtime(request: Request) -> _PrototypeRuntime:
    return cast("_PrototypeRuntime", request.app.state.runtime)


def _text_part(content: str) -> dict[str, object]:
    return {"type": "text", "content": content}


def _message_part(message: Message[Fetched]) -> dict[str, object]:
    if message.role == "reasoning":
        return {"type": "thinking", "content": message.content}
    if message.role == "tool":
        return {
            "type": "tool-call",
            "id": str(message.id),
            "name": message.tool_name or "tool",
            "arguments": json.dumps(message.tool_args or {}, separators=(",", ":")),
            "input": message.tool_args or {},
            "output": message.tool_result,
            "state": "complete",
        }
    return _text_part(message.content)


def _ui_message(message: Message[Fetched]) -> dict[str, object]:
    return {
        "id": str(message.id),
        "role": "assistant"
        if message.role in {"assistant", "reasoning", "tool"}
        else "user",
        "parts": [_message_part(message)],
        "createdAt": message.created_at.isoformat(),
        "metadata": {
            "tether": {
                "role": message.role,
                "seq": message.seq,
                "turnId": None if message.turn_id is None else str(message.turn_id),
            }
        },
    }


def _user_content(message: _WireMessage) -> str:
    """Read text from the strict AG-UI user anchor or multimodal parts."""
    if isinstance(message.content, str):
        return message.content
    if message.content is None:
        return ""
    return "".join(
        str(part.get("text", part.get("content", "")))
        for part in message.content
        if part.get("type") == "text"
    )


class _RunLog:
    """Process-local delivery log used to exercise TanStack replay semantics."""

    def __init__(self) -> None:
        self._complete: bool = False
        self._condition: asyncio.Condition = asyncio.Condition()
        self._events: list[dict[str, object]] = []

    async def append(self, event: dict[str, object]) -> None:
        async with self._condition:
            self._events.append(event)
            self._condition.notify_all()

    async def finish(self) -> None:
        async with self._condition:
            self._complete = True
            self._condition.notify_all()

    async def stream(self, after_offset: int) -> AsyncIterator[bytes]:
        offset = after_offset + 1
        while True:
            async with self._condition:
                _ = await self._condition.wait_for(
                    lambda current_offset=offset: (
                        current_offset < len(self._events) or self._complete
                    )
                )
                if offset >= len(self._events):
                    return
                event = self._events[offset]
            yield (
                f"id: {offset}\ndata: {json.dumps(event, separators=(',', ':'))}\n\n"
            ).encode()
            offset += 1


class _RunRegistry:
    """One host-process map from client run identity to replay log."""

    def __init__(self) -> None:
        self._logs: dict[str, _RunLog] = {}
        self._request_ids: dict[str, UUID] = {}
        self._run_ids_by_turn: dict[UUID, str] = {}

    def bind_turn(self, run_id: str, turn_id: UUID) -> None:
        self._run_ids_by_turn[turn_id] = run_id

    def fetch(self, run_id: str) -> _RunLog | None:
        return self._logs.get(run_id)

    def fetch_or_create(self, run_id: str) -> _RunLog:
        run_log = self._logs.get(run_id)
        if run_log is None:
            run_log = _RunLog()
            self._logs[run_id] = run_log
        return run_log

    def request_id_for(self, run_id: str) -> UUID:
        request_id = self._request_ids.get(run_id)
        if request_id is None:
            request_id = uuid7()
            self._request_ids[run_id] = request_id
        return request_id

    def run_id_for_turn(self, turn_id: UUID) -> str | None:
        return self._run_ids_by_turn.get(turn_id)


def _run_registry(request: Request) -> _RunRegistry:
    registry = getattr(request.app.state, "tanstack_ai_run_registry", None)
    if isinstance(registry, _RunRegistry):
        return registry
    registry = _RunRegistry()
    request.app.state.tanstack_ai_run_registry = registry
    return registry


class _AguiFrameSink:
    """Translate one Tether turn stream into AG-UI events."""

    def __init__(self, *, run_id: str, run_log: _RunLog, thread_id: UUID) -> None:
        self._assistant_message_id: str | None = None
        self._closed: bool = False
        self._run_log: _RunLog = run_log
        self._message_index: int = 0
        self._reasoning_message_id: str | None = None
        self._reasoning_open: bool = False
        self._run_id: str = run_id
        self._run_started: bool = False
        self._text_open: bool = False
        self._thread_id: UUID = thread_id
        self._turn_id: UUID | None = None

    async def send(self, frame: ChatFrame) -> None:
        """Translate one typed host frame without changing execution ownership."""
        if frame.turn_id is not None:
            self._turn_id = frame.turn_id
        if isinstance(
            frame,
            (
                MessageEndFrame,
                MessageStartFrame,
                StreamUpdateFrame,
                TurnQueuedFrame,
                UserMessageFrame,
            ),
        ):
            await self._send_message_frame(frame)
        elif isinstance(frame, (ToolEndFrame, ToolStartFrame)):
            await self._send_tool_frame(frame)
        elif isinstance(frame, (AgentEndFrame, ErrorFrame, TurnEndedFrame)):
            await self._send_terminal_frame(frame)

    async def _send_message_frame(
        self,
        frame: (
            MessageEndFrame
            | MessageStartFrame
            | StreamUpdateFrame
            | TurnQueuedFrame
            | UserMessageFrame
        ),
    ) -> None:
        if isinstance(frame, TurnQueuedFrame):
            await self._send_queued(frame)
        elif isinstance(frame, UserMessageFrame):
            await self._send_user_message(frame)
        elif isinstance(frame, MessageStartFrame):
            await self._send_message_start()
        elif isinstance(frame, StreamUpdateFrame):
            await self._send_stream_update(frame)
        else:
            await self._end_messages()

    async def _send_tool_frame(self, frame: ToolEndFrame | ToolStartFrame) -> None:
        if isinstance(frame, ToolStartFrame):
            await self._send_tool_start(frame)
        else:
            await self._send_tool_end(frame)

    async def _send_terminal_frame(
        self, frame: AgentEndFrame | ErrorFrame | TurnEndedFrame
    ) -> None:
        if isinstance(frame, AgentEndFrame):
            await self._send_agent_end(frame)
        elif isinstance(frame, TurnEndedFrame):
            await self._send_turn_end(frame)
        else:
            await self._send_error(frame)

    async def _send_queued(self, frame: TurnQueuedFrame) -> None:
        if frame.status == "running":
            await self._start_run()

    async def _send_user_message(self, frame: UserMessageFrame) -> None:
        await self._start_run()
        await self._push(
            "CUSTOM",
            name="tether.user-message",
            value={"messageId": str(frame.message_id), "seq": frame.seq},
        )

    async def _send_message_start(self) -> None:
        self._message_index += 1
        self._assistant_message_id = (
            f"live:{self._turn_id or self._run_id}:{self._message_index}"
        )
        self._reasoning_message_id = f"{self._assistant_message_id}:reasoning"
        self._text_open = True
        await self._push(
            "TEXT_MESSAGE_START",
            messageId=self._assistant_message_id,
            role="assistant",
        )

    async def _send_stream_update(self, frame: StreamUpdateFrame) -> None:
        if frame.event == "thinking_delta":
            await self._start_reasoning()
            await self._push(
                "REASONING_MESSAGE_CONTENT",
                messageId=self._reasoning_message_id,
                delta=_frame_text(frame.delta),
            )
        elif frame.event == "text_delta":
            await self._push(
                "TEXT_MESSAGE_CONTENT",
                messageId=self._assistant_message_id,
                delta=_frame_text(frame.delta),
            )

    async def _send_tool_start(self, frame: ToolStartFrame) -> None:
        tool_call_id = frame.tool_id or f"tool:{self._message_index}"
        await self._push(
            "TOOL_CALL_START",
            toolCallId=tool_call_id,
            toolCallName=frame.tool_name or "tool",
            parentMessageId=self._assistant_message_id,
        )
        await self._push(
            "TOOL_CALL_ARGS",
            toolCallId=tool_call_id,
            delta=json.dumps(frame.tool_args, separators=(",", ":")),
        )
        await self._push("TOOL_CALL_END", toolCallId=tool_call_id)

    async def _send_tool_end(self, frame: ToolEndFrame) -> None:
        tool_call_id = frame.tool_id or f"tool:{self._message_index}"
        await self._push(
            "TOOL_CALL_RESULT",
            messageId=f"{tool_call_id}:result",
            toolCallId=tool_call_id,
            role="tool",
            content=json.dumps(frame.tool_result, separators=(",", ":")),
        )

    async def _send_agent_end(self, frame: AgentEndFrame) -> None:
        await self._end_messages()
        await self._push(
            "RUN_FINISHED",
            finishReason="stop",
            metadata={
                "tether": {
                    "finalText": frame.final_text,
                    "replyMode": frame.reply_mode,
                    "toolOnly": frame.tool_only,
                }
            },
            runId=str(self._run_id),
            threadId=str(self._thread_id),
        )

    async def _send_turn_end(self, frame: TurnEndedFrame) -> None:
        if frame.status in {"cancelled", "failed"}:
            await self._end_messages()
            await self._push(
                "RUN_ERROR",
                code=frame.failure_code or frame.status,
                message=frame.failure_summary or frame.status,
                runId=str(self._run_id),
                threadId=str(self._thread_id),
            )
        await self._finish()

    async def _send_error(self, frame: ErrorFrame) -> None:
        await self._push(
            "RUN_ERROR",
            message=frame.detail,
            runId=str(self._run_id),
            threadId=str(self._thread_id),
        )
        await self._finish()

    async def _end_messages(self) -> None:
        if self._reasoning_open:
            await self._push(
                "REASONING_MESSAGE_END",
                messageId=self._reasoning_message_id,
            )
            self._reasoning_open = False
        if self._text_open:
            await self._push("TEXT_MESSAGE_END", messageId=self._assistant_message_id)
            self._text_open = False

    async def _finish(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._run_log.finish()

    async def _push(self, event_type: str, **fields: object) -> None:
        await self._run_log.append({"type": event_type, **fields})

    async def _start_reasoning(self) -> None:
        if self._reasoning_open:
            return
        self._reasoning_open = True
        await self._push(
            "REASONING_MESSAGE_START",
            messageId=self._reasoning_message_id,
            role="reasoning",
        )

    async def _start_run(self) -> None:
        if self._run_started:
            return
        self._run_started = True
        await self._push(
            "RUN_STARTED",
            metadata={
                "tether": {
                    "turnId": None if self._turn_id is None else str(self._turn_id)
                }
            },
            runId=str(self._run_id),
            threadId=str(self._thread_id),
        )


def _frame_text(delta: object) -> str:
    if isinstance(delta, str):
        return delta
    if isinstance(delta, dict):
        text = cast("dict[str, object]", delta).get("text")
        return text if isinstance(text, str) else ""
    return ""


router = APIRouter(include_in_schema=False)


@router.get("/api/prototypes/tanstack-ai/chat")
async def hydrate_or_join_chat(
    request: Request,
    thread_id: Annotated[str | None, Query(alias="threadId")] = None,
    run_id: Annotated[str | None, Query(alias="runId")] = None,
    offset: int = -1,
) -> Response:
    """Hydrate one Conversation or replay one active AG-UI delivery log."""
    if run_id is not None:
        run_log = _run_registry(request).fetch(run_id)
        if run_log is None:
            return JSONResponse({"detail": "run not found"}, status_code=404)
        return StreamingResponse(run_log.stream(offset), media_type="text/event-stream")
    if thread_id is None:
        return JSONResponse({"detail": "threadId is required"}, status_code=422)
    try:
        conversation_id = UUID(thread_id)
        service = _runtime(request).conversation_service
        messages = await service.fetch_messages(conversation_id)
        _ = await service.fetch_conversation(conversation_id)
    except ValueError, ConversationNotFoundError:
        return JSONResponse({"detail": "conversation not found"}, status_code=404)
    async with service.database.transaction() as transaction:
        active_turn = await transaction.fetch_one_or_none(
            select(ConversationTurn)
            .where(ConversationTurn.conversation_id.eq(conversation_id))
            .where(ConversationTurn.status.in_("pending", "running"))
            .order_by(ConversationTurn.turn_seq.asc())
        )
    active_run_id = (
        None
        if active_turn is None
        else _run_registry(request).run_id_for_turn(active_turn.id)
        or str(active_turn.request_id or active_turn.id)
    )
    return JSONResponse(
        {
            "messages": [_ui_message(message) for message in messages],
            "activeRun": (
                None if active_run_id is None else {"runId": str(active_run_id)}
            ),
            "interrupts": None,
        }
    )


@router.post("/api/prototypes/tanstack-ai/chat")
async def stream_chat(request: Request, run_input: _RunAgentInput) -> Response:
    """Submit one AG-UI request to the canonical durable Conversation queue."""
    user_message = next(
        (message for message in reversed(run_input.messages) if message.role == "user"),
        None,
    )
    if user_message is None:
        return JSONResponse({"detail": "user message is required"}, status_code=422)
    reply_mode: ReplyMode = (
        "spoken" if run_input.forwarded_props.get("replyMode") == "spoken" else "text"
    )
    raw_attachment_ids = run_input.forwarded_props.get("attachmentIds", [])
    attachment_ids = (
        tuple(
            UUID(str(attachment_id))
            for attachment_id in cast("list[object]", raw_attachment_ids)
        )
        if isinstance(raw_attachment_ids, list)
        else ()
    )
    registry = _run_registry(request)
    run_log = registry.fetch_or_create(run_input.run_id)
    sink = _AguiFrameSink(
        run_id=run_input.run_id,
        run_log=run_log,
        thread_id=run_input.thread_id,
    )
    try:
        ticket = await _runtime(request).conversation_turns.submit(
            InteractiveTurnRequest(
                attachment_ids=attachment_ids,
                conversation_id=run_input.thread_id,
                prompt=_user_content(user_message),
                reply_mode=reply_mode,
                request_id=registry.request_id_for(run_input.run_id),
            ),
            sink,
        )
        registry.bind_turn(run_input.run_id, ticket.turn_id)
    except ConversationNotFoundError:
        return JSONResponse({"detail": "conversation not found"}, status_code=404)
    raw_last_event_id = request.headers.get("Last-Event-ID", "-1")
    try:
        last_event_id = int(raw_last_event_id)
    except ValueError:
        last_event_id = -1
    return StreamingResponse(
        run_log.stream(last_event_id), media_type="text/event-stream"
    )


__all__ = ["router"]
