"""Domain values rendered by agent-run observability."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

type RunKind = Literal[
    "conversation", "scheduled", "recall", "gmail", "gmail_purge", "dreaming"
]
"""Host entry point that opened an agent run."""
type Termination = Literal["completed", "error", "aborted", "timeout"]
"""Reason an agent run stopped."""


def _empty_tool_calls() -> list[ToolCallTrace]:
    """Create an independently owned tool-call collection for one run."""
    return []


@dataclass(frozen=True, slots=True)
class ToolCallTrace:
    """One redacted tool invocation within an agent run."""

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
        """Render this tool call as a JSON-compatible mapping."""
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
    """Mutable trace state for one active or completed agent run."""

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
        """Report whether the run remains open."""
        return self.ended_at is None

    @property
    def duration_ms(self) -> float | None:
        """Return wall-clock duration after the run has ended."""
        if self.ended_at is None:
            return None
        return round((self.ended_at - self.started_at) * 1000, 3)

    def render(self) -> dict[str, Any]:
        """Render the run as a JSON-compatible inspection value."""
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
