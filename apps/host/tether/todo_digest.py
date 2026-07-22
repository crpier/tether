"""The standing Todo digest — the first instance of the ADR 0017 digest idiom.

A pure composition seam: a `TodoReadiness` in, a system-prompt digest block out.
The block is appended to the conversation persona so ready Todos surface without
the user asking, and waiting Todos ride along with their conditions for the agent
to raise *only when the conversation makes them relevant* (relevance-gated
mention). Kept free of I/O so it can be tested at the block boundary — todos in,
text out — rather than by inspecting a live prompt.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from snekql.sqlite import Fetched

    from tether.todos import Todo, TodoReadiness


def _ready_line(todo: Todo[Fetched]) -> str:
    """One ready Todo as a digest bullet."""
    return f"- {todo.action}"


def _waiting_line(todo: Todo[Fetched], deadline_iso: str | None) -> str:
    """One waiting Todo as a digest bullet, surfacing what it waits on."""
    waits: list[str] = []
    if todo.condition:
        waits.append(f'condition: "{todo.condition}"')
    if deadline_iso is not None:
        waits.append(f"deadline: {deadline_iso}")
    suffix = f" (waiting on {'; '.join(waits)})" if waits else ""
    return f"- {todo.action}{suffix}"


def render_todo_digest(readiness: TodoReadiness) -> str:
    """Render the Todo digest block, or an empty string when there is nothing.

    Ready Todos are listed as the actionable now; waiting Todos are listed with
    their unmet condition and/or upcoming deadline, under an instruction to raise
    them only when the conversation is contextually relevant.
    """
    if not readiness.ready and not readiness.waiting:
        return ""
    lines = ["## Your standing todos", ""]
    if readiness.ready:
        lines.append(
            "Ready now (surface these proactively when it fits the conversation):"
        )
        lines.extend(_ready_line(todo) for todo in readiness.ready)
        lines.append("")
    if readiness.waiting:
        lines.append(
            " ".join(
                (
                    "Waiting on a condition or a deadline — mention one ONLY when",
                    "the conversation makes it relevant (e.g. the user mentions the",
                    "person, place, or event it waits on), never as an unprompted",
                    "list:",
                )
            )
        )
        for todo in readiness.waiting:
            deadline = readiness.deadlines.get(todo.id)
            deadline_iso = deadline.isoformat() if deadline is not None else None
            lines.append(_waiting_line(todo, deadline_iso))
        lines.append("")
    return "\n".join(lines).rstrip()
