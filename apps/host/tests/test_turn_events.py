"""Focused decode tests for the typed pi turn-event vocabulary.

`decode_turn_event` is the single seam where pi's raw RPC wire shape becomes
the host's typed turn events; these tests are the only place the raw dict
shapes are spelled out.
"""

from snektest import assert_eq, assert_is_none, test

from tether.pi_runtime import (
    AgentEnded,
    AssistantStreamNote,
    MessageSettled,
    ModelTurnStarted,
    TextDelta,
    ThinkingDelta,
    ToolSettled,
    ToolStarted,
    decode_turn_event,
)


@test()
def tool_execution_start_carries_call_identity_and_args() -> None:
    """A `tool_execution_start` yields the call id, tool name, and its args."""
    assert_eq(
        decode_turn_event(
            {
                "type": "tool_execution_start",
                "toolCallId": "call-capture",
                "toolName": "capture",
                "args": {"content": "tool memory"},
            }
        ),
        ToolStarted(
            args={"content": "tool memory"},
            tool_call_id="call-capture",
            tool_name="capture",
        ),
    )


@test()
def tool_execution_start_without_args_defaults_to_an_empty_object() -> None:
    """Missing or malformed tool args settle as an empty argument object."""
    assert_eq(
        decode_turn_event(
            {
                "type": "tool_execution_start",
                "toolCallId": "call-capture",
                "toolName": "capture",
            }
        ),
        ToolStarted(args={}, tool_call_id="call-capture", tool_name="capture"),
    )


@test()
def tool_execution_end_carries_the_result_object() -> None:
    """A `tool_execution_end` yields the call identity plus its result object."""
    assert_eq(
        decode_turn_event(
            {
                "type": "tool_execution_end",
                "toolCallId": "call-capture",
                "toolName": "capture",
                "result": {"details": {"ok": True}},
                "isError": False,
            }
        ),
        ToolSettled(
            result={"details": {"ok": True}},
            tool_call_id="call-capture",
            tool_name="capture",
        ),
    )


@test()
def a_non_object_tool_result_is_wrapped_as_a_value_object() -> None:
    """Scalar tool results are wrapped so the result is always a JSON object."""
    assert_eq(
        decode_turn_event(
            {
                "type": "tool_execution_end",
                "toolCallId": "call-capture",
                "toolName": "capture",
                "result": "plain outcome",
            }
        ),
        ToolSettled(
            result={"value": "plain outcome"},
            tool_call_id="call-capture",
            tool_name="capture",
        ),
    )


@test()
def agent_end_closes_the_turn() -> None:
    """An `agent_end` record decodes to the turn's terminal event."""
    assert_eq(decode_turn_event({"type": "agent_end"}), AgentEnded())


@test()
def records_outside_the_turn_vocabulary_decode_to_nothing() -> None:
    """Unrelated protocol records (for example `rpc_error`) are skipped."""
    assert_is_none(decode_turn_event({"type": "rpc_error", "error": "boom"}))


@test()
def text_delta_updates_extract_text_and_keep_the_raw_delta() -> None:
    """A `text_delta` yields its text plus the raw payload for forwarding."""
    assert_eq(
        decode_turn_event(
            {
                "type": "message_update",
                "assistantMessageEvent": {
                    "type": "text_delta",
                    "delta": {"text": "streamed"},
                    "contentIndex": 1,
                },
            }
        ),
        TextDelta(content_index=1, raw_delta={"text": "streamed"}, text="streamed"),
    )


@test()
def text_delta_accepts_a_plain_string_delta() -> None:
    """Providers that stream the delta as a bare string still yield its text."""
    assert_eq(
        decode_turn_event(
            {
                "type": "message_update",
                "assistantMessageEvent": {"type": "text_delta", "delta": "chunk"},
            }
        ),
        TextDelta(content_index=None, raw_delta="chunk", text="chunk"),
    )


@test()
def thinking_delta_updates_land_on_the_reasoning_channel() -> None:
    """A `thinking_delta` is its own event so reasoning never merges into text."""
    assert_eq(
        decode_turn_event(
            {
                "type": "message_update",
                "assistantMessageEvent": {
                    "type": "thinking_delta",
                    "delta": {"text": "pondering"},
                    "contentIndex": 0,
                },
            }
        ),
        ThinkingDelta(
            content_index=0, raw_delta={"text": "pondering"}, text="pondering"
        ),
    )


@test()
def other_assistant_updates_become_uninterpreted_stream_notes() -> None:
    """Non-delta stream updates keep their kind for verbatim forwarding."""
    assert_eq(
        decode_turn_event(
            {
                "type": "message_update",
                "assistantMessageEvent": {"type": "thinking_start", "contentIndex": 0},
            }
        ),
        AssistantStreamNote(content_index=0, kind="thinking_start", raw_delta=None),
    )


@test()
def message_update_without_an_event_object_is_skipped() -> None:
    """A `message_update` missing its assistant event payload decodes to nothing."""
    assert_is_none(decode_turn_event({"type": "message_update"}))


@test()
def assistant_message_start_opens_a_model_turn() -> None:
    """An assistant `message_start` marks the start of one model turn."""
    assert_eq(
        decode_turn_event({"type": "message_start", "message": {"role": "assistant"}}),
        ModelTurnStarted(),
    )


@test()
def non_assistant_message_start_is_outside_the_turn_vocabulary() -> None:
    """User and tool-result message starts decode to nothing."""
    assert_is_none(
        decode_turn_event({"type": "message_start", "message": {"role": "user"}})
    )


@test()
def assistant_message_end_settles_text_and_reasoning() -> None:
    """An assistant `message_end` joins text and thinking chunks separately."""
    settled = decode_turn_event(
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "chain of "},
                    {"type": "thinking", "thinking": "thought"},
                    {"type": "text", "text": "final "},
                    {"type": "text", "text": "answer"},
                ],
            },
        }
    )

    assert_eq(
        settled, MessageSettled(reasoning="chain of thought", text="final answer")
    )


@test()
def non_assistant_message_end_is_outside_the_turn_vocabulary() -> None:
    """A user `message_end` decodes to nothing; only assistant turns settle."""
    assert_is_none(
        decode_turn_event({"type": "message_end", "message": {"role": "user"}})
    )
