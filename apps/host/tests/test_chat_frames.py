"""Wire-shape guards for the outbound chat WebSocket frame models.

Each frame model must serialize to the exact dict the browser expects: the ws
stream stays outside OpenAPI (ADR 0008), so these assertions are the host-side
contract that keeps the shapes byte-identical to what chat_ws historically emitted.
"""

from uuid import UUID

from snektest import assert_eq, test

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

_CONVERSATION_ID = UUID("11111111-1111-1111-1111-111111111111")
_MESSAGE_ID = UUID("22222222-2222-2222-2222-222222222222")


@test()
def user_message_frame_serializes_ids_as_strings() -> None:
    """A settled user turn carries the string ids and monotonic seq."""
    frame = UserMessageFrame(
        conversation_id=_CONVERSATION_ID, message_id=_MESSAGE_ID, seq=7
    )
    assert_eq(
        frame.wire(),
        {
            "type": "chat",
            "conversation_id": str(_CONVERSATION_ID),
            "event": "user_message",
            "message_id": str(_MESSAGE_ID),
            "seq": 7,
        },
    )


@test()
def message_start_frame_has_only_the_bare_event() -> None:
    """message_start carries just the conversation tag and event name."""
    frame = MessageStartFrame(conversation_id=_CONVERSATION_ID)
    assert_eq(
        frame.wire(),
        {
            "type": "chat",
            "conversation_id": str(_CONVERSATION_ID),
            "event": "message_start",
        },
    )


@test()
def message_end_frame_has_only_the_bare_event() -> None:
    """message_end carries just the conversation tag and event name."""
    frame = MessageEndFrame(conversation_id=_CONVERSATION_ID)
    assert_eq(
        frame.wire(),
        {
            "type": "chat",
            "conversation_id": str(_CONVERSATION_ID),
            "event": "message_end",
        },
    )


@test()
def agent_end_frame_has_only_the_bare_event() -> None:
    """agent_end is the terminal turn frame."""
    frame = AgentEndFrame(conversation_id=_CONVERSATION_ID)
    assert_eq(
        frame.wire(),
        {
            "type": "chat",
            "conversation_id": str(_CONVERSATION_ID),
            "event": "agent_end",
        },
    )


@test()
def abort_ack_frame_has_only_the_bare_event() -> None:
    """abort_ack acknowledges an abort request."""
    frame = AbortAckFrame(conversation_id=_CONVERSATION_ID)
    assert_eq(
        frame.wire(),
        {
            "type": "chat",
            "conversation_id": str(_CONVERSATION_ID),
            "event": "abort_ack",
        },
    )


@test()
def stream_update_frame_forwards_a_string_delta_verbatim() -> None:
    """A text delta rides the raw string payload under a dynamic event name."""
    frame = StreamUpdateFrame(
        conversation_id=_CONVERSATION_ID,
        event="text_delta",
        delta="hello",
        content_index=0,
    )
    assert_eq(
        frame.wire(),
        {
            "type": "chat",
            "conversation_id": str(_CONVERSATION_ID),
            "event": "text_delta",
            "delta": "hello",
            "content_index": 0,
        },
    )


@test()
def stream_update_frame_forwards_a_dict_delta_and_null_index() -> None:
    """A structured delta and a missing content index pass through unchanged."""
    frame = StreamUpdateFrame(
        conversation_id=_CONVERSATION_ID,
        event="thinking_delta",
        delta={"text": "thinking"},
        content_index=None,
    )
    assert_eq(
        frame.wire(),
        {
            "type": "chat",
            "conversation_id": str(_CONVERSATION_ID),
            "event": "thinking_delta",
            "delta": {"text": "thinking"},
            "content_index": None,
        },
    )


@test()
def tool_start_frame_keeps_null_tool_id() -> None:
    """A tool start with no call id still emits an explicit null tool_id."""
    frame = ToolStartFrame(
        conversation_id=_CONVERSATION_ID,
        tool_name="remember",
        tool_id=None,
        tool_args={"content": "tool memory"},
    )
    assert_eq(
        frame.wire(),
        {
            "type": "chat",
            "conversation_id": str(_CONVERSATION_ID),
            "event": "tool_start",
            "tool_name": "remember",
            "tool_id": None,
            "tool_args": {"content": "tool memory"},
        },
    )


@test()
def tool_end_frame_carries_the_result_envelope() -> None:
    """A tool end forwards its id and JSON result object."""
    frame = ToolEndFrame(
        conversation_id=_CONVERSATION_ID,
        tool_name="remember",
        tool_id="call-1",
        tool_result={"details": {"result": {"id": "memory-id"}}},
    )
    assert_eq(
        frame.wire(),
        {
            "type": "chat",
            "conversation_id": str(_CONVERSATION_ID),
            "event": "tool_end",
            "tool_name": "remember",
            "tool_id": "call-1",
            "tool_result": {"details": {"result": {"id": "memory-id"}}},
        },
    )


@test()
def error_frame_omits_the_conversation_id_when_absent() -> None:
    """An untagged error drops the conversation_id key entirely."""
    frame = ErrorFrame(detail="prompt failed")
    assert_eq(
        frame.wire(),
        {"type": "chat", "event": "error", "detail": "prompt failed"},
    )


@test()
def error_frame_includes_the_conversation_id_when_tagged() -> None:
    """A tagged error carries the string conversation_id."""
    frame = ErrorFrame(
        detail="conversation not found", conversation_id=_CONVERSATION_ID
    )
    assert_eq(
        frame.wire(),
        {
            "type": "chat",
            "event": "error",
            "detail": "conversation not found",
            "conversation_id": str(_CONVERSATION_ID),
        },
    )


@test()
def notify_frame_carries_trigger_title_and_body() -> None:
    """A notification forwards its trigger id, optional title, and body."""
    frame = NotifyFrame(trigger_id="trig-1", title="Reminder", body="stand up")
    assert_eq(
        frame.wire(),
        {
            "type": "notify",
            "trigger_id": "trig-1",
            "title": "Reminder",
            "body": "stand up",
        },
    )


@test()
def notify_frame_keeps_a_null_title() -> None:
    """A titleless notification still emits an explicit null title."""
    frame = NotifyFrame(trigger_id="trig-1", title=None, body="stand up")
    assert_eq(
        frame.wire(),
        {
            "type": "notify",
            "trigger_id": "trig-1",
            "title": None,
            "body": "stand up",
        },
    )


@test()
def invalidate_frame_carries_the_key_list() -> None:
    """An invalidation forwards the affected query keys."""
    frame = InvalidateFrame(keys=["conversations", "memories"])
    assert_eq(
        frame.wire(),
        {"type": "invalidate", "keys": ["conversations", "memories"]},
    )
