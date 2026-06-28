"""Unit tests for the per-run agent trace recorder."""

from __future__ import annotations

from collections.abc import Callable

from snektest import assert_eq, assert_is_none, assert_true, test

from tether.agent_trace import (
    AgentTraceRecorder,
    redact_args,
    summarize_result,
)

SESSION = "session-a"


def _tick() -> Callable[[], float]:
    """A monotonic fake clock returning whole seconds on each call."""
    counter = {"t": 0.0}

    def now() -> float:
        counter["t"] += 1.0
        return counter["t"]

    return now


@test()
def begin_run_returns_an_id_and_marks_the_run_active() -> None:
    """A freshly opened run is active and discoverable by id and session."""
    recorder = AgentTraceRecorder()
    run_id = recorder.begin_run(session_id=SESSION, kind="conversation", prompt="hi")

    run = recorder.get_run(run_id)
    assert run is not None
    assert_eq(run.session_id, SESSION)
    assert_eq(run.kind, "conversation")
    assert_eq(run.prompt, "hi")
    assert_true(run.is_active)
    current = recorder.current_run(SESSION)
    assert current is not None
    assert_eq(current.run_id, run_id)


@test()
def tool_calls_are_attributed_to_the_active_run_in_order() -> None:
    """Each recorded tool call lands on the session's active run, sequenced."""
    recorder = AgentTraceRecorder()
    run_id = recorder.begin_run(session_id=SESSION, kind="conversation")

    recorder.record_tool_call(
        session_id=SESSION,
        tool="capture",
        args={"content": "a fact"},
        envelope={"success": True, "result": {"id": "m1", "state": "loose"}},
        duration_ms=4.2,
    )
    recorder.record_tool_call(
        session_id=SESSION,
        tool="search",
        args={"q": "fact"},
        envelope={"success": True, "result": [{"id": "m1"}, {"id": "m2"}]},
        duration_ms=7.0,
    )

    run = recorder.get_run(run_id)
    assert run is not None
    assert_eq([call.tool for call in run.tool_calls], ["capture", "search"])
    assert_eq([call.seq for call in run.tool_calls], [1, 2])
    assert_eq(run.tool_calls[0].result, {"id": "m1", "state": "loose"})
    # A collection result is summarised to a count, never dumped row by row.
    assert_eq(run.tool_calls[1].result, {"kind": "collection", "count": 2})


@test()
def a_tool_call_for_an_unknown_session_is_dropped() -> None:
    """Recording must never raise for a session with no active run."""
    recorder = AgentTraceRecorder()
    recorder.record_tool_call(
        session_id="ghost",
        tool="capture",
        args={"content": "x"},
        envelope={"success": True, "result": None},
        duration_ms=1.0,
    )
    assert_is_none(recorder.current_run("ghost"))


@test()
def end_run_records_termination_and_keeps_the_run_inspectable() -> None:
    """A completed run is closed, retains its trace, and is no longer active."""
    recorder = AgentTraceRecorder(now=_tick())
    run_id = recorder.begin_run(session_id=SESSION, kind="conversation")
    recorder.record_model_turn(session_id=SESSION)
    recorder.record_model_turn(session_id=SESSION)
    ended = recorder.end_run(session_id=SESSION, termination="completed")

    assert ended is not None
    assert_eq(ended.termination, "completed")
    assert_eq(ended.iterations, 2)
    assert_is_none(recorder.current_run(SESSION))
    # Inspectable after the fact.
    run = recorder.get_run(run_id)
    assert run is not None
    assert_true(not run.is_active)
    assert run.duration_ms is not None


@test()
def reopening_a_session_supersedes_a_dangling_run() -> None:
    """A new run for a session times out the previous unfinished run."""
    recorder = AgentTraceRecorder()
    first = recorder.begin_run(session_id=SESSION, kind="conversation")
    second = recorder.begin_run(session_id=SESSION, kind="conversation")

    stale = recorder.get_run(first)
    assert stale is not None
    assert_eq(stale.termination, "timeout")
    current = recorder.current_run(SESSION)
    assert current is not None
    assert_eq(current.run_id, second)


@test()
def history_is_bounded_and_evicts_oldest_first() -> None:
    """Completed runs beyond the history limit are evicted oldest-first."""
    recorder = AgentTraceRecorder(history_limit=2)
    ids = [recorder.begin_run(session_id=f"s{i}", kind="scheduled") for i in range(3)]
    for i in range(3):
        _ = recorder.end_run(session_id=f"s{i}", termination="completed")

    assert_is_none(recorder.get_run(ids[0]))
    assert recorder.get_run(ids[1]) is not None
    assert recorder.get_run(ids[2]) is not None


@test()
def recent_runs_are_returned_newest_first() -> None:
    """The recent-runs view orders by most recently started."""
    recorder = AgentTraceRecorder()
    older = recorder.begin_run(session_id="s1", kind="scheduled")
    newer = recorder.begin_run(session_id="s2", kind="scheduled")
    recent = recorder.recent_runs(limit=2)
    assert_eq([run.run_id for run in recent], [newer, older])


@test()
def secrets_are_masked_and_long_strings_truncated_in_args() -> None:
    """Credential-shaped keys are masked; oversized strings are truncated."""
    redacted = redact_args(
        {"q": "ok", "tool_secret": "abc", "auth_token": "z", "blob": "x" * 600}
    )
    assert_eq(redacted["q"], "ok")
    assert_eq(redacted["tool_secret"], "[redacted]")
    assert_eq(redacted["auth_token"], "[redacted]")
    assert_true(redacted["blob"].endswith("(truncated)"))


@test()
def collection_results_are_summarised_to_a_count() -> None:
    """Bulk corpus content is never copied into a trace verbatim."""
    assert_eq(summarize_result([1, 2, 3]), {"kind": "collection", "count": 3})
    assert_eq(
        summarize_result({"items": [1, 2], "id": "m1"}),
        {"items": {"kind": "collection", "count": 2}, "id": "m1"},
    )
