"""Bounded in-process storage for active and completed agent-run traces."""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Any, Final
from uuid import uuid4

from tether.agent_trace_model import RunKind, RunTrace, Termination, ToolCallTrace
from tether.agent_trace_redaction import (
    redact_args,
    summarize_result,
    truncate_trace_text,
)

_HISTORY_LIMIT: Final = 200


class AgentTraceRecorder:
    """Attribute tool activity to active sessions and retain bounded run history.

    ```python
    recorder = AgentTraceRecorder()
    run_id = recorder.begin_run(session_id="s1", kind="conversation")
    assert recorder.get_run(run_id) is not None
    ```
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
        """Open a run, timing out any dangling run for the same session."""
        stale = self._active.get(session_id)
        if stale is not None and stale.is_active:
            self._close(stale, termination="timeout", error="superseded by a new run")
        run = RunTrace(
            run_id=uuid4().hex,
            session_id=session_id,
            kind=kind,
            started_at=self._now(),
            conversation_id=conversation_id,
            prompt=truncate_trace_text(prompt) if prompt is not None else None,
        )
        self._active[session_id] = run
        self._remember(run)
        return run.run_id

    def record_model_turn(self, *, session_id: str) -> None:
        """Count one model iteration on the session's active run."""
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
        """Append one retention-safe tool call when the session has an active run."""
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
        """Close and return the session's active run, if one exists."""
        run = self._active.pop(session_id, None)
        if run is None:
            return None
        self._close(run, termination=termination, error=error)
        return run

    def current_run(self, session_id: str) -> RunTrace | None:
        """Return the active run for a session, if present."""
        return self._active.get(session_id)

    def get_run(self, run_id: str) -> RunTrace | None:
        """Return one retained active or completed run by id."""
        return self._runs.get(run_id)

    def recent_runs(self, *, limit: int = 50) -> list[RunTrace]:
        """Return retained runs newest-first up to the requested limit."""
        runs = list(self._runs.values())
        runs.reverse()
        return runs[:limit]

    def _close(
        self, run: RunTrace, *, termination: Termination, error: str | None
    ) -> None:
        """Stamp a run's terminal state exactly once at ownership release."""
        run.ended_at = self._now()
        run.termination = termination
        run.error = truncate_trace_text(error) if error is not None else None

    def _remember(self, run: RunTrace) -> None:
        """Retain a run and evict oldest entries beyond the bounded history."""
        self._runs[run.run_id] = run
        while len(self._runs) > self._history_limit:
            _, evicted = self._runs.popitem(last=False)
            if self._active.get(evicted.session_id) is evicted:
                _ = self._active.pop(evicted.session_id, None)
