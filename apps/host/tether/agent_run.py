"""Context-managed lifecycle ownership for one observable agent run."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Generator
from dataclasses import dataclass

import structlog

from tether.agent_trace_model import RunCorrelation, RunKind, Termination
from tether.agent_trace_recorder import AgentTraceRecorder


@dataclass(slots=True)
class RunHandle:
    """Caller-controlled terminal outcome for one context-managed run."""

    run_id: str | None
    termination: Termination = "completed"
    error: str | None = None

    def mark(self, termination: Termination, error: str | None = None) -> None:
        """Settle a non-raising outcome before leaving the owned run context."""
        self.termination = termination
        self.error = error


@contextlib.contextmanager
def record_run(
    recorder: AgentTraceRecorder | None,
    *,
    session_id: str,
    kind: RunKind,
    prompt: str | None = None,
    correlation: RunCorrelation | None = None,
) -> Generator[RunHandle]:
    """Open, correlate, settle, and close exactly one agent run.

    Cancellation, timeout, and unexpected defects are recorded and re-raised.
    """
    run_id = (
        recorder.begin_run(
            session_id=session_id,
            kind=kind,
            prompt=prompt,
            conversation_id=(
                None if correlation is None else correlation.conversation_id
            ),
            correlation=correlation,
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
