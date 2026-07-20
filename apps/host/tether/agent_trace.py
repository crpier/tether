"""Per-run trace over a single agent invocation.

Tether is an LLM-agent system: when the agent misbehaves you need to see *what
it did and why* — the loop iterations, the tool calls it made, the arguments,
and the response envelopes (`tether.tools.ToolEnvelope`) coming back. Structured
logging (`tether.logging`) already records individual lines; this module builds
a coherent **trace view over a single agent run** on top of it.

An *agent run* is one prompt driven to termination: one user/scheduled prompt,
the model turns and tool calls it triggers, and why the loop ended. The host
owns run boundaries (the WebSocket prompt handler and the ephemeral scheduler
runner), so it opens a run there and closes it when the loop terminates. Tool
calls arrive as *separate* loopback HTTP requests from the pi subprocess, keyed
only by their `session_id`; the recorder attributes each to whichever run is
active for that session, giving every tool call a single run id to group under.

Two redaction rules keep traces safe to retain and render:

* **Secrets never land in a trace.** Argument keys that look like a credential
  (`secret`, `token`, `password`, `authorization`) are masked.
* **Bulk corpus content is summarised, not dumped.** A collection result is
  recorded as a count, not its rows; long strings are truncated. A run trace is
  a debugging aid, not a second copy of the corpus.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections import OrderedDict
from collections.abc import Callable, Generator
from dataclasses import dataclass, field
from typing import Any, Final, Literal, cast
from uuid import uuid4

import structlog

type RunKind = Literal["conversation", "scheduled", "recall", "gmail"]
"""Which host entry point opened a run."""

type Termination = Literal["completed", "error", "aborted", "timeout"]
"""Why an agent run's loop stopped."""

_HISTORY_LIMIT: Final = 200
"""Completed runs retained for after-the-fact inspection before eviction."""

_MAX_STRING: Final = 500
"""Longest string value kept verbatim in a trace before truncation."""

_TRUNCATION_SUFFIX: Final = "…(truncated)"

_SENSITIVE_KEY_MARKERS: Final = ("secret", "token", "password", "authorization")
"""Substrings that mark an argument key as a credential to mask."""

_REDACTED: Final = "[redacted]"


def _truncate(value: str) -> str:
    """Cap a string at the trace limit, marking that it was shortened."""
    if len(value) <= _MAX_STRING:
        return value
    return value[:_MAX_STRING] + _TRUNCATION_SUFFIX


def _is_sensitive_key(key: str) -> bool:
    """Report whether an argument key names a credential."""
    lowered = key.lower()
    return any(marker in lowered for marker in _SENSITIVE_KEY_MARKERS)


def _empty_tool_calls() -> list[ToolCallTrace]:
    """Typed default factory for a run's tool-call list."""
    return []


def redact_args(args: dict[str, Any]) -> dict[str, Any]:
    """Mask credential-shaped keys and truncate long string arguments.

    >>> redact_args({"q": "hi", "tool_secret": "abc"})
    {'q': 'hi', 'tool_secret': '[redacted]'}
    """
    redacted: dict[str, Any] = {}
    for key, value in args.items():
        if _is_sensitive_key(key):
            redacted[key] = _REDACTED
        elif isinstance(value, str):
            redacted[key] = _truncate(value)
        else:
            redacted[key] = value
    return redacted


def summarize_result(result: object) -> object:
    """Project a tool result into a trace-safe summary.

    Collections are reduced to a count so a browse/search never dumps the corpus
    into a trace; objects are walked recursively so a long string or a bulk
    collection nested anywhere inside an envelope is truncated/collapsed too, not
    just at the top level; scalars pass through (small, structured envelopes).

    >>> summarize_result([1, 2, 3])
    {'kind': 'collection', 'count': 3}
    >>> summarize_result({"page": {"rows": [1, 2], "id": "m1"}})["page"]
    {'rows': {'kind': 'collection', 'count': 2}, 'id': 'm1'}
    """
    if isinstance(result, list):
        # Narrowed to a list whose element types are unknown; we only count it,
        # so casting away the element type is safe.
        return {"kind": "collection", "count": len(cast("list[Any]", result))}
    if isinstance(result, dict):
        summary: dict[str, Any] = {}
        # Narrowed to a dict with unknown key/value types; each value is re-run
        # through this same summariser, so the cast only feeds that recursion.
        for key, value in cast("dict[Any, Any]", result).items():
            summary[str(key)] = summarize_result(value)
        return summary
    if isinstance(result, str):
        return _truncate(result)
    return result


@dataclass(frozen=True, slots=True)
class ToolCallTrace:
    """One tool call within a run: name, args, envelope outcome, timing.

    >>> call = ToolCallTrace(
    ...     seq=1, tool="capture", args={"content": "hi"}, success=True, duration_ms=2.0
    ... )
    >>> call.render()["tool"], call.render()["success"]
    ('capture', True)
    """

    seq: int
    tool: str
    args: dict[str, Any]
    success: bool
    duration_ms: float
    error: dict[str, Any] | None = None
    result: object = None
    provenance: dict[str, Any] | None = None
    quota: dict[str, Any] | None = None
    cache: dict[str, Any] | None = None

    def render(self) -> dict[str, Any]:
        """Render this tool call as a JSON-friendly mapping."""
        return {
            "seq": self.seq,
            "tool": self.tool,
            "args": self.args,
            "success": self.success,
            "duration_ms": self.duration_ms,
            "error": self.error,
            "result": self.result,
            "provenance": self.provenance,
            "quota": self.quota,
            "cache": self.cache,
        }


@dataclass(slots=True)
class RunTrace:
    """The trace of one agent run, mutated while the run is live.

    >>> run = RunTrace(run_id="r1", session_id="s1", kind="conversation", started_at=0.0)
    >>> run.is_active
    True
    >>> run.ended_at = 0.5
    >>> run.is_active, run.duration_ms
    (False, 500.0)
    """

    run_id: str
    session_id: str
    kind: RunKind
    started_at: float
    conversation_id: str | None = None
    prompt: str | None = None
    ended_at: float | None = None
    termination: Termination | None = None
    error: str | None = None
    iterations: int = 0
    tool_calls: list[ToolCallTrace] = field(default_factory=_empty_tool_calls)

    @property
    def is_active(self) -> bool:
        """Report whether the run is still open."""
        return self.ended_at is None

    @property
    def duration_ms(self) -> float | None:
        """Wall-clock duration once the run has ended."""
        if self.ended_at is None:
            return None
        return round((self.ended_at - self.started_at) * 1000, 3)

    def render(self) -> dict[str, Any]:
        """Render the whole run as a JSON-friendly mapping for inspection."""
        return {
            "run_id": self.run_id,
            "session_id": self.session_id,
            "kind": self.kind,
            "conversation_id": self.conversation_id,
            "prompt": self.prompt,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "duration_ms": self.duration_ms,
            "termination": self.termination,
            "error": self.error,
            "iterations": self.iterations,
            "tool_calls": [call.render() for call in self.tool_calls],
        }


class AgentTraceRecorder:
    """In-process recorder of agent runs and their tool calls.

    One recorder lives per host process. It tracks the run currently active for
    each pi session (so loopback tool calls can be attributed by `session_id`)
    and retains a bounded history of completed runs for after-the-fact
    inspection. Recording is best-effort and must never break the agent: a tool
    call for an unknown session is dropped, not raised.

    >>> recorder = AgentTraceRecorder()
    >>> run_id = recorder.begin_run(session_id="s1", kind="conversation", prompt="hi")
    >>> recorder.record_tool_call(
    ...     session_id="s1",
    ...     tool="capture",
    ...     args={"content": "a fact"},
    ...     envelope={"success": True, "result": {"id": "m1"}},
    ...     duration_ms=2.0,
    ... )
    >>> ended = recorder.end_run(session_id="s1", termination="completed")
    >>> recorder.get_run(run_id) is ended  # inspectable after the fact
    True
    >>> [call.tool for call in ended.tool_calls]
    ['capture']
    """

    def __init__(
        self,
        *,
        history_limit: int = _HISTORY_LIMIT,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._history_limit: int = history_limit
        self._now: Callable[[], float] = now
        self._active: dict[str, RunTrace] = {}
        self._runs: OrderedDict[str, RunTrace] = OrderedDict()

    def begin_run(
        self,
        *,
        session_id: str,
        kind: RunKind,
        prompt: str | None = None,
        conversation_id: str | None = None,
    ) -> str:
        """Open a run for a session and return its run id.

        A session runs prompts sequentially, so opening a new run supersedes any
        run still marked active for that session (a previous run that never saw a
        clean termination is closed as a timeout).
        """
        stale = self._active.get(session_id)
        if stale is not None and stale.is_active:
            self._close(stale, termination="timeout", error="superseded by a new run")
        run = RunTrace(
            run_id=uuid4().hex,
            session_id=session_id,
            kind=kind,
            started_at=self._now(),
            conversation_id=conversation_id,
            prompt=_truncate(prompt) if prompt is not None else None,
        )
        self._active[session_id] = run
        self._remember(run)
        return run.run_id

    def record_model_turn(self, *, session_id: str) -> None:
        """Count one model turn (loop iteration) for a session's active run."""
        run = self._active.get(session_id)
        if run is not None:
            run.iterations += 1

    def record_tool_call(
        self,
        *,
        session_id: str,
        tool: str,
        args: dict[str, Any],
        envelope: dict[str, Any],
        duration_ms: float,
    ) -> None:
        """Append a redacted tool call to the session's active run.

        `envelope` is the JSON-dumped `ToolEnvelope`. A call for a session with
        no active run is silently dropped: tracing is observational and must not
        perturb the tool path.
        """
        run = self._active.get(session_id)
        if run is None:
            return
        run.tool_calls.append(
            ToolCallTrace(
                seq=len(run.tool_calls) + 1,
                tool=tool,
                args=redact_args(args),
                success=bool(envelope.get("success")),
                duration_ms=duration_ms,
                error=envelope.get("error"),
                result=summarize_result(envelope.get("result")),
                provenance=envelope.get("provenance"),
                quota=envelope.get("quota"),
                cache=envelope.get("cache"),
            )
        )

    def end_run(
        self,
        *,
        session_id: str,
        termination: Termination,
        error: str | None = None,
    ) -> RunTrace | None:
        """Close the session's active run and return it, if one was open."""
        run = self._active.pop(session_id, None)
        if run is None:
            return None
        self._close(run, termination=termination, error=error)
        return run

    def current_run(self, session_id: str) -> RunTrace | None:
        """Return the run currently active for a session, if any."""
        return self._active.get(session_id)

    def get_run(self, run_id: str) -> RunTrace | None:
        """Return a run (active or completed) by id."""
        return self._runs.get(run_id)

    def recent_runs(self, *, limit: int = 50) -> list[RunTrace]:
        """Return the most recently started runs, newest first."""
        runs = list(self._runs.values())
        runs.reverse()
        return runs[:limit]

    def _close(
        self, run: RunTrace, *, termination: Termination, error: str | None
    ) -> None:
        """Stamp a run's terminal state."""
        run.ended_at = self._now()
        run.termination = termination
        run.error = _truncate(error) if error is not None else None

    def _remember(self, run: RunTrace) -> None:
        """Index a run and evict the oldest once history is full."""
        self._runs[run.run_id] = run
        while len(self._runs) > self._history_limit:
            _, evicted = self._runs.popitem(last=False)
            if self._active.get(evicted.session_id) is evicted:
                _ = self._active.pop(evicted.session_id, None)


@dataclass(slots=True)
class RunHandle:
    """The caller's view of one run opened by `record_run`.

    `run_id` is `None` when tracing is off (no recorder wired). A caller that
    settles a failure without raising — e.g. answering a rejected prompt over
    the WebSocket — stamps the outcome with `mark` before leaving the block.

    >>> handle = RunHandle(run_id="r1")
    >>> handle.mark("error", "prompt rejected")
    >>> handle.termination, handle.error
    ('error', 'prompt rejected')
    """

    run_id: str | None
    termination: Termination = "completed"
    error: str | None = None

    def mark(self, termination: Termination, error: str | None = None) -> None:
        """Record why the run ended without raising out of the block."""
        self.termination = termination
        self.error = error


@contextlib.contextmanager
def record_run(
    recorder: AgentTraceRecorder | None,
    *,
    session_id: str,
    kind: RunKind,
    prompt: str | None = None,
    conversation_id: str | None = None,
) -> Generator[RunHandle]:
    """Trace one agent run over the wrapped block.

    Opens a run on the recorder (when one is wired), binds the run id into the
    structlog contextvars so every host log line emitted while driving the
    prompt correlates with the loopback tool calls the tool seam stamps, and
    closes the run on exit. An exception escaping the block maps to its
    termination — `CancelledError` to `aborted`, `TimeoutError` to `timeout`,
    anything else to `error` — and is re-raised, so rendering the failure
    (e.g. a WebSocket error frame) stays with the caller.

    >>> recorder = AgentTraceRecorder()
    >>> with record_run(recorder, session_id="s1", kind="conversation") as run:
    ...     pass
    >>> recorder.recent_runs(limit=1)[0].termination
    'completed'
    """
    run_id = (
        recorder.begin_run(
            session_id=session_id,
            kind=kind,
            prompt=prompt,
            conversation_id=conversation_id,
        )
        if recorder is not None
        else None
    )
    handle = RunHandle(run_id=run_id)
    try:
        with structlog.contextvars.bound_contextvars(
            **({"run_id": run_id} if run_id is not None else {})
        ):
            yield handle
    except asyncio.CancelledError:
        handle.mark("aborted", "generation cancelled")
        raise
    except TimeoutError as error:
        handle.mark("timeout", str(error) or None)
        raise
    except Exception as error:
        handle.mark("error", str(error))
        raise
    finally:
        if recorder is not None:
            _ = recorder.end_run(
                session_id=session_id,
                termination=handle.termination,
                error=handle.error,
            )


__all__ = [
    "AgentTraceRecorder",
    "RunHandle",
    "RunKind",
    "RunTrace",
    "Termination",
    "ToolCallTrace",
    "record_run",
    "redact_args",
    "summarize_result",
]
