"""Typed turn events decoded from pi's raw RPC protocol."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast


@dataclass(frozen=True, slots=True)
class ModelTurnStarted:
    """pi opened a new assistant message, so one model turn began."""


@dataclass(frozen=True, slots=True)
class TextDelta:
    """One streamed chunk of the assistant's answer text."""

    content_index: int | None
    raw_delta: object
    text: str


@dataclass(frozen=True, slots=True)
class ThinkingDelta:
    """One streamed chunk of the assistant's reasoning text."""

    content_index: int | None
    raw_delta: object
    text: str


@dataclass(frozen=True, slots=True)
class AssistantStreamNote:
    """An assistant-stream update the host relays without interpreting."""

    content_index: int | None
    kind: str
    raw_delta: object


@dataclass(frozen=True, slots=True)
class MessageSettled:
    """pi closed an assistant message with its settled text channels."""

    reasoning: str
    text: str
    error: str | None = None


@dataclass(frozen=True, slots=True)
class ToolStarted:
    """pi began executing one tool call."""

    args: dict[str, Any]
    tool_call_id: str | None
    tool_name: str | None


@dataclass(frozen=True, slots=True)
class ToolSettled:
    """pi finished one tool call with a JSON-object result."""

    result: dict[str, Any]
    tool_call_id: str | None
    tool_name: str | None


@dataclass(frozen=True, slots=True)
class AgentEnded:
    """pi finished the whole turn; the terminal event of a turn stream."""


type TurnEvent = (
    AgentEnded
    | AssistantStreamNote
    | MessageSettled
    | ModelTurnStarted
    | TextDelta
    | ThinkingDelta
    | ToolSettled
    | ToolStarted
)
"""The typed vocabulary of one pi turn as its stream settles."""


def _string_or_none(value: object) -> str | None:
    """Narrow an optional wire field to a string, dropping malformed values."""
    return value if isinstance(value, str) else None


def _is_assistant_message(message: object) -> bool:
    """Report whether a pi message envelope is an assistant turn."""
    if not isinstance(message, dict):
        return False
    return cast("dict[str, Any]", message).get("role") == "assistant"


def _joined_content_text(message: dict[str, Any], *, item_type: str) -> str:
    """Join one content channel of a settled assistant message."""
    content = message.get("content")
    if not isinstance(content, list):
        return ""
    chunks: list[str] = []
    for raw_item in cast("list[object]", content):
        if not isinstance(raw_item, dict):
            continue
        item = cast("dict[str, Any]", raw_item)
        if item.get("type") == item_type and isinstance(item.get(item_type), str):
            chunks.append(cast("str", item[item_type]))
    return "".join(chunks)


def _delta_text(assistant_event: dict[str, Any]) -> str:
    """Extract text from current and legacy assistant delta shapes."""
    delta = assistant_event.get("delta")
    if isinstance(delta, str):
        return delta
    if isinstance(delta, dict):
        text = cast("dict[str, object]", delta).get("text")
        if isinstance(text, str):
            return text
    text = assistant_event.get("text")
    return text if isinstance(text, str) else ""


def _decode_assistant_update(assistant_event: object) -> TurnEvent | None:
    """Decode an assistant update while preserving unknown update kinds."""
    if not isinstance(assistant_event, dict):
        return None
    assistant_event_data = cast("dict[str, Any]", assistant_event)
    raw_content_index = assistant_event_data.get("contentIndex")
    content_index = raw_content_index if isinstance(raw_content_index, int) else None
    raw_delta = cast("object", assistant_event_data.get("delta"))
    match assistant_event_data.get("type"):
        case "text_delta":
            return TextDelta(
                content_index=content_index,
                raw_delta=raw_delta,
                text=_delta_text(assistant_event_data),
            )
        case "thinking_delta":
            return ThinkingDelta(
                content_index=content_index,
                raw_delta=raw_delta,
                text=_delta_text(assistant_event_data),
            )
        case str() as kind:
            return AssistantStreamNote(
                content_index=content_index,
                kind=kind,
                raw_delta=raw_delta,
            )
        case _:
            return AssistantStreamNote(
                content_index=content_index,
                kind="message_update",
                raw_delta=raw_delta,
            )


def _decode_tool_execution(event: dict[str, Any]) -> ToolStarted | ToolSettled:
    """Decode a tool boundary into stable host-owned shapes."""
    tool_call_id = _string_or_none(event.get("toolCallId"))
    tool_name = _string_or_none(event.get("toolName"))
    if event.get("type") == "tool_execution_start":
        args = event.get("args")
        return ToolStarted(
            args=cast("dict[str, Any]", args) if isinstance(args, dict) else {},
            tool_call_id=tool_call_id,
            tool_name=tool_name,
        )
    result = cast("object", event.get("result"))
    return ToolSettled(
        result=(
            cast("dict[str, Any]", result)
            if isinstance(result, dict)
            else {"value": result}
        ),
        tool_call_id=tool_call_id,
        tool_name=tool_name,
    )


def decode_turn_event(event: dict[str, Any]) -> TurnEvent | None:
    """Decode one raw pi record into the host-owned turn vocabulary."""
    match event.get("type"):
        case "message_start" if _is_assistant_message(event.get("message")):
            return ModelTurnStarted()
        case "message_update":
            return _decode_assistant_update(event.get("assistantMessageEvent"))
        case "message_end" if _is_assistant_message(event.get("message")):
            message_data = cast("dict[str, Any]", event["message"])
            return MessageSettled(
                reasoning=_joined_content_text(message_data, item_type="thinking"),
                text=_joined_content_text(message_data, item_type="text"),
                error=(
                    _string_or_none(message_data.get("errorMessage"))
                    if message_data.get("stopReason") == "error"
                    else None
                ),
            )
        case "tool_execution_start" | "tool_execution_end":
            return _decode_tool_execution(event)
        case "agent_end":
            return AgentEnded()
        case _:
            return None


__all__ = [
    "AgentEnded",
    "AssistantStreamNote",
    "MessageSettled",
    "ModelTurnStarted",
    "TextDelta",
    "ThinkingDelta",
    "ToolSettled",
    "ToolStarted",
    "TurnEvent",
    "decode_turn_event",
]
