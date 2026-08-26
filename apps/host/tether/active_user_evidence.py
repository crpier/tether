"""Resolve the initiating user Evidence authorized by an active interactive turn."""

from __future__ import annotations

from uuid import UUID

from snekql.sqlite import Fetched

from tether.agent_trace_recorder import AgentTraceRecorder
from tether.conversation_store import Message
from tether.conversations import ConversationService


class ActiveUserEvidenceError(Exception):
    """The current agent run has no initiating interactive user Evidence."""


async def resolve_active_user_evidence(
    *,
    conversation_service: ConversationService,
    trace_recorder: AgentTraceRecorder,
    session_id: str,
) -> Message[Fetched]:
    """Return only the user Message initiating the exact active interactive turn."""
    run = trace_recorder.current_run(session_id)
    if (
        run is None
        or run.kind != "conversation"
        or run.origin != "interactive"
        or run.conversation_id is None
        or run.turn_id is None
    ):
        raise ActiveUserEvidenceError(session_id)
    source = await conversation_service.fetch_turn_user_message(
        conversation_id=UUID(run.conversation_id),
        turn_id=UUID(run.turn_id),
    )
    if source is None:
        raise ActiveUserEvidenceError(session_id)
    return source


__all__ = ["ActiveUserEvidenceError", "resolve_active_user_evidence"]
