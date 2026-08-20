"""One interactive chat turn from canonical input through settled output."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from json import dumps
from typing import Any, Protocol, cast
from uuid import UUID

import structlog
from snekql.sqlite import Fetched

from tether.agent_run import record_run
from tether.agent_trace_recorder import AgentTraceRecorder
from tether.chat_frames import (
    AgentEndFrame,
    ErrorFrame,
    MessageEndFrame,
    MessageStartFrame,
    SkillStatusFrame,
    StreamUpdateFrame,
    ToolEndFrame,
    ToolStartFrame,
    UserMessageFrame,
)
from tether.chat_prompt import local_timezone_name, prompt_with_time_context
from tether.conversation_model import ConversationNotFoundError, MessageDraft
from tether.conversation_store import Conversation
from tether.conversations import SESSION_GAP, ConversationService
from tether.dreaming import DreamingService
from tether.pi_errors import PiRuntimeError
from tether.pi_turn_events import (
    AgentEnded,
    AssistantStreamNote,
    MessageSettled,
    ModelTurnStarted,
    TextDelta,
    ThinkingDelta,
    ToolSettled,
    ToolStarted,
    TurnEvent,
)
from tether.structured_logging import Logger

_AGENT_EVENT_TIMEOUT_SECONDS = 60.0
_TOOL_ONLY_TURN_MARKER = "Turn ended after tool use without a final answer."
"""Durable marker for a completed turn that ends on a tool row."""
_TOOL_RESULT_FRAME_LIMIT_BYTES = 64 * 1_024
"""Maximum settled tool result retained in the browser transcript."""

_logger = structlog.stdlib.get_logger("tether.chat_turn")


class ChatFrameSink(Protocol):
    """Destination for chat frames produced while running one prompt."""

    async def send_json(self, data: Any) -> None: ...


class ChatRpcClient(Protocol):
    """RPC operation required to submit one prompt."""

    async def request(self, command_type: str, **fields: Any) -> dict[str, Any]: ...


class ChatPiRuntime(Protocol):
    """Live pi operations required while coordinating one turn."""

    @property
    def client(self) -> ChatRpcClient: ...

    @property
    def loaded_skills(self) -> tuple[str, ...]: ...

    @property
    def skills_confirmed(self) -> bool: ...

    def drain_events(self) -> int: ...

    def stream_turn(
        self,
        *,
        wait_seconds: float = 5.0,
    ) -> AsyncGenerator[TurnEvent]: ...


class ChatRuntimeRegistry(Protocol):
    """Conversation-bound runtime lookup required by chat turns."""

    async def runtime_for(
        self,
        conversation: Conversation[Fetched],
    ) -> ChatPiRuntime: ...


class ConversationTurnQueue:
    """Serialize every prompt targeting the same Conversation."""

    def __init__(self) -> None:
        self._locks: dict[UUID, asyncio.Lock] = {}

    @asynccontextmanager
    async def serialize(self, conversation_id: UUID) -> AsyncGenerator[None]:
        """Wait for prior turns, then hold exclusive generation ownership."""
        lock = self._locks.setdefault(conversation_id, asyncio.Lock())
        async with lock:
            yield


@dataclass(frozen=True, slots=True)
class ChatTurnDependencies:
    """Explicit collaborators required to execute and settle one chat turn."""

    conversation_service: ConversationService
    dreaming_service: DreamingService
    dreaming_enabled: bool
    runtime_registry: ChatRuntimeRegistry
    trace_recorder: AgentTraceRecorder | None
    turn_queue: ConversationTurnQueue
    logger: Logger


@dataclass(slots=True)
class _TurnState:
    """Mutable accumulation state scoped to exactly one streamed turn."""

    pending_tool_args: dict[str, dict[str, Any]] = field(
        default_factory=dict[str, dict[str, Any]]
    )
    streamed_reasoning: list[str] = field(default_factory=list[str])
    streamed_text: list[str] = field(default_factory=list[str])
    final_text: str = ""
    needs_final_answer: bool = False


@dataclass(frozen=True, slots=True)
class _TurnContext:
    """Stable collaborators and identity shared by each event in one turn."""

    conversation_id: UUID
    dependencies: ChatTurnDependencies
    session_id: str
    websocket: ChatFrameSink


def _compact_tool_result(tool_result: dict[str, Any]) -> dict[str, Any]:
    """Replace oversized tool data before it can block transcript delivery."""
    result_size_bytes = len(
        dumps(tool_result, ensure_ascii=False, separators=(",", ":")).encode()
    )
    if result_size_bytes <= _TOOL_RESULT_FRAME_LIMIT_BYTES:
        return tool_result
    return {"original_size_bytes": result_size_bytes, "truncated": True}


def _prompt_failure_detail(response: dict[str, object]) -> str:
    """Render pi's failed prompt response for browser presentation."""
    for key in ("error", "detail", "message"):
        field = response.get(key)
        if isinstance(field, str) and field.strip():
            return f"prompt failed: {field}"
    data = response.get("data")
    if isinstance(data, dict):
        data_fields = cast("dict[str, object]", data)
        for key in ("error", "detail", "message"):
            field = data_fields.get(key)
            if isinstance(field, str) and field.strip():
                return f"prompt failed: {field}"
    return "prompt failed"


async def send_chat_error(
    websocket: ChatFrameSink,
    *,
    detail: str,
    conversation_id: UUID | None = None,
) -> None:
    """Send one optionally conversation-tagged error frame."""
    await websocket.send_json(
        ErrorFrame(detail=detail, conversation_id=conversation_id).wire()
    )


async def _relay_stream_update(
    websocket: ChatFrameSink,
    *,
    conversation_id: UUID,
    update: TextDelta | ThinkingDelta | AssistantStreamNote,
    streamed_text: list[str],
    streamed_reasoning: list[str],
) -> None:
    """Accumulate channel text and forward the provider delta verbatim."""
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
    websocket: ChatFrameSink,
    dependencies: ChatTurnDependencies,
    conversation_id: UUID,
    settled: MessageSettled,
    state: _TurnState,
) -> str:
    """Persist reasoning before answer text and return the settled answer."""
    reasoning = settled.reasoning or "".join(state.streamed_reasoning)
    if reasoning:
        _ = await dependencies.conversation_service.append_message(
            MessageDraft(
                content=reasoning,
                conversation_id=conversation_id,
                role="reasoning",
            )
        )
    content = settled.text or "".join(state.streamed_text)
    if content:
        _ = await dependencies.conversation_service.append_message(
            MessageDraft(
                content=content,
                conversation_id=conversation_id,
                role="assistant",
            )
        )
    await websocket.send_json(MessageEndFrame(conversation_id=conversation_id).wire())
    return content


async def _forward_tool_start(
    websocket: ChatFrameSink,
    *,
    conversation_id: UUID,
    started: ToolStarted,
    pending_tool_args: dict[str, dict[str, Any]],
) -> None:
    """Remember tool arguments and forward the start frame."""
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
    websocket: ChatFrameSink,
    dependencies: ChatTurnDependencies,
    *,
    conversation_id: UUID,
    settled: ToolSettled,
    pending_tool_args: dict[str, dict[str, Any]],
) -> bool:
    """Persist a bounded tool envelope and forward the completion frame."""
    transcript_result = _compact_tool_result(settled.result)
    persisted = False
    if settled.tool_call_id is not None and settled.tool_name is not None:
        _ = await dependencies.conversation_service.append_message(
            MessageDraft(
                content=settled.tool_name,
                conversation_id=conversation_id,
                pi_message_id=settled.tool_call_id,
                role="tool",
                tool_args=pending_tool_args.pop(settled.tool_call_id, {}),
                tool_name=settled.tool_name,
                tool_result=transcript_result,
            )
        )
        persisted = True
    await websocket.send_json(
        ToolEndFrame(
            conversation_id=conversation_id,
            tool_name=settled.tool_name,
            tool_id=settled.tool_call_id,
            tool_result=transcript_result,
        ).wire()
    )
    return persisted


async def _relay_tool_event(
    websocket: ChatFrameSink,
    dependencies: ChatTurnDependencies,
    *,
    conversation_id: UUID,
    tool_event: ToolStarted | ToolSettled,
    pending_tool_args: dict[str, dict[str, Any]],
) -> bool:
    """Relay ordinary tools while keeping bundled skill reads operational-only."""
    match tool_event:
        case ToolStarted(tool_name="read"):
            _logger.info(
                "Bundled skill read started",
                conversation_id=str(conversation_id),
                tool_call_id=tool_event.tool_call_id,
            )
            return False
        case ToolSettled(tool_name="read"):
            _logger.info(
                "Bundled skill read settled",
                conversation_id=str(conversation_id),
                tool_call_id=tool_event.tool_call_id,
            )
            return False
        case ToolStarted():
            await _forward_tool_start(
                websocket,
                conversation_id=conversation_id,
                started=tool_event,
                pending_tool_args=pending_tool_args,
            )
            return False
        case ToolSettled():
            return await _settle_tool_end(
                websocket,
                dependencies,
                conversation_id=conversation_id,
                settled=tool_event,
                pending_tool_args=pending_tool_args,
            )


async def _settle_tool_only_turn_marker(
    dependencies: ChatTurnDependencies,
    *,
    conversation_id: UUID,
) -> None:
    """Append a durable marker when pi ends after tools without answering."""
    _ = await dependencies.conversation_service.append_message(
        MessageDraft(
            content=_TOOL_ONLY_TURN_MARKER,
            conversation_id=conversation_id,
            role="assistant",
        )
    )


async def _queue_dreaming_run(
    dependencies: ChatTurnDependencies,
    *,
    conversation_id: UUID,
) -> None:
    """Queue a post-turn Dream assimilation run, ignoring malformed states."""
    if not dependencies.dreaming_enabled:
        return
    try:
        _ = await dependencies.dreaming_service.queue_assimilation_run(
            conversation_id,
            logger=dependencies.logger,
            now=datetime.now(UTC),
        )
    except Exception as error:
        _logger.warning(
            "Dream assimilation queueing failed after chat turn",
            conversation_id=str(conversation_id),
            error=str(error),
        )


async def _handle_turn_event(
    context: _TurnContext,
    state: _TurnState,
    turn_event: TurnEvent,
) -> None:
    """Apply one typed pi event to browser and transcript state."""
    match turn_event:
        case ModelTurnStarted():
            state.streamed_text.clear()
            state.streamed_reasoning.clear()
            if context.dependencies.trace_recorder is not None:
                context.dependencies.trace_recorder.record_model_turn(
                    session_id=context.session_id
                )
            await context.websocket.send_json(
                MessageStartFrame(conversation_id=context.conversation_id).wire()
            )
        case AssistantStreamNote(kind=kind) if kind.startswith("toolcall_"):
            return
        case TextDelta() | ThinkingDelta() | AssistantStreamNote():
            await _relay_stream_update(
                context.websocket,
                conversation_id=context.conversation_id,
                update=turn_event,
                streamed_text=state.streamed_text,
                streamed_reasoning=state.streamed_reasoning,
            )
        case MessageSettled(error=str() as error):
            await send_chat_error(
                context.websocket,
                conversation_id=context.conversation_id,
                detail=error,
            )
        case MessageSettled():
            settled_text = await _settle_message_end(
                context.websocket,
                context.dependencies,
                context.conversation_id,
                turn_event,
                state,
            )
            state.final_text = settled_text or state.final_text
            state.needs_final_answer = state.needs_final_answer and not settled_text
            state.streamed_text.clear()
            state.streamed_reasoning.clear()
        case ToolStarted() | ToolSettled():
            state.needs_final_answer = (
                await _relay_tool_event(
                    context.websocket,
                    context.dependencies,
                    conversation_id=context.conversation_id,
                    tool_event=turn_event,
                    pending_tool_args=state.pending_tool_args,
                )
                or state.needs_final_answer
            )
        case AgentEnded():
            if state.needs_final_answer:
                await _settle_tool_only_turn_marker(
                    context.dependencies,
                    conversation_id=context.conversation_id,
                )
                state.final_text = _TOOL_ONLY_TURN_MARKER
            await context.websocket.send_json(
                AgentEndFrame(conversation_id=context.conversation_id).wire()
            )


async def stream_chat_turn(
    websocket: ChatFrameSink,
    dependencies: ChatTurnDependencies,
    *,
    conversation_id: UUID,
    runtime: ChatPiRuntime,
    session_id: str,
) -> str:
    """Relay one ordered pi turn and return its final assistant text."""
    context = _TurnContext(
        conversation_id=conversation_id,
        dependencies=dependencies,
        session_id=session_id,
        websocket=websocket,
    )
    state = _TurnState()
    async for turn_event in runtime.stream_turn(
        wait_seconds=_AGENT_EVENT_TIMEOUT_SECONDS
    ):
        await _handle_turn_event(context, state, turn_event)
    return state.final_text


async def _run_chat_prompt(
    websocket: ChatFrameSink,
    dependencies: ChatTurnDependencies,
    *,
    conversation_id: UUID,
    content: str,
) -> str:
    """Persist, submit, stream, and settle one prompt with ownership held."""
    try:
        conversation = await dependencies.conversation_service.fetch_conversation(
            conversation_id
        )
        conversation = await dependencies.conversation_service.resolve_session(
            conversation,
            now=datetime.now(UTC),
            gap=SESSION_GAP,
        )
        message = await dependencies.conversation_service.append_message(
            MessageDraft(
                content=content,
                conversation_id=conversation_id,
                role="user",
            )
        )
    except ConversationNotFoundError:
        await send_chat_error(
            websocket,
            conversation_id=conversation_id,
            detail="conversation not found",
        )
        return ""
    await websocket.send_json(
        UserMessageFrame(
            conversation_id=conversation_id,
            message_id=message.id,
            seq=message.seq,
        ).wire()
    )
    session_id = str(conversation.pi_session_id)
    try:
        with record_run(
            dependencies.trace_recorder,
            session_id=session_id,
            kind="conversation",
            prompt=content,
            conversation_id=str(conversation_id),
        ) as run:
            runtime = await dependencies.runtime_registry.runtime_for(conversation)
            if getattr(runtime, "skills_confirmed", False):
                loaded_skills = getattr(runtime, "loaded_skills", ())
                await websocket.send_json(
                    SkillStatusFrame(
                        conversation_id=conversation_id,
                        loaded_count=(
                            len(cast("tuple[object, ...]", loaded_skills))
                            if isinstance(loaded_skills, tuple)
                            else 0
                        ),
                    ).wire()
                )
            _ = runtime.drain_events()
            now = datetime.now().astimezone()
            prompt_response = await runtime.client.request(
                "prompt",
                message=prompt_with_time_context(
                    content,
                    now=now,
                    timezone_name=local_timezone_name(now),
                ),
            )
            if prompt_response.get("success") is not True:
                failure_detail = _prompt_failure_detail(prompt_response)
                run.mark("error", failure_detail)
                await send_chat_error(
                    websocket,
                    conversation_id=conversation_id,
                    detail=failure_detail,
                )
                return ""
            final_text = await stream_chat_turn(
                websocket,
                dependencies,
                conversation_id=conversation_id,
                runtime=runtime,
                session_id=session_id,
            )
            await _queue_dreaming_run(dependencies, conversation_id=conversation_id)
            return final_text
    except PiRuntimeError as error:
        await send_chat_error(
            websocket,
            conversation_id=conversation_id,
            detail=str(error),
        )
        return ""
    except TimeoutError:
        await send_chat_error(
            websocket,
            conversation_id=conversation_id,
            detail=(
                f"agent timed out (no response in {int(_AGENT_EVENT_TIMEOUT_SECONDS)}s)"
            ),
        )
        return ""


async def run_chat_prompt(
    websocket: ChatFrameSink,
    dependencies: ChatTurnDependencies,
    *,
    conversation_id: UUID,
    content: str,
) -> str:
    """Queue, persist, submit, stream, and settle one user prompt."""
    async with dependencies.turn_queue.serialize(conversation_id):
        return await _run_chat_prompt(
            websocket,
            dependencies,
            conversation_id=conversation_id,
            content=content,
        )


__all__ = [
    "ChatFrameSink",
    "ChatPiRuntime",
    "ChatRuntimeRegistry",
    "ChatTurnDependencies",
    "ConversationTurnQueue",
    "run_chat_prompt",
    "send_chat_error",
    "stream_chat_turn",
]
