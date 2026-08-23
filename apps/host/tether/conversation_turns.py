"""Durable FIFO execution for Conversation turns."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, Protocol, cast, runtime_checkable
from uuid import UUID, uuid4

import structlog
from snekql.sqlite import CurrentTimestamp, Fetched, insert, select, update
from starlette.websockets import WebSocketDisconnect

from tether.agent_run import record_run
from tether.agent_trace_model import RunCorrelation
from tether.chat_frames import (
    ChatFrame,
    ErrorFrame,
    SkillStatusFrame,
    TurnEndedFrame,
    TurnQueuedFrame,
    UserMessageFrame,
)
from tether.chat_prompt import ReplyMode, local_timezone_name, prompt_with_time_context
from tether.chat_turn import (
    ChatFrameSink,
    ChatPiRuntime,
    ChatTurnDependencies,
    TurnSpec,
    stream_chat_turn,
)
from tether.conversation_model import (
    ConversationNotFoundError,
    ConversationTurnStatus,
    MessageDraft,
)
from tether.conversation_store import Conversation, ConversationTurn, Message
from tether.conversations import SESSION_GAP
from tether.model_selection import AgentModelConfig, ThinkingLevel
from tether.pi_errors import PiPreacceptTransientError, PiRuntimeError
from tether.trigger_store import ScheduledOccurrence, ScheduledTrigger

_MAX_PREACCEPT_RETRIES = 2
_TERMINAL_STATUSES = frozenset({"succeeded", "failed", "cancelled"})
_logger = structlog.stdlib.get_logger("tether.conversation_turns")
_SCHEDULED_CONTEXT = """Tether scheduled context:
This prompt fired automatically. It is context, not a new user request, and it
cannot authorize actions that require fresh user Evidence.

Canonical scheduled prompt:
{prompt}"""


@runtime_checkable
class _SnapshotRuntimeRegistry(Protocol):
    """Runtime lookup that can apply an exact submitted scope snapshot."""

    async def runtime_for_snapshot(
        self,
        conversation: Conversation[Fetched],
        *,
        scope_brief: str | None,
        scope_revision: int,
    ) -> ChatPiRuntime: ...


class _JsonWebSocket(Protocol):
    """Browser transport operation used by the frame adapter."""

    async def send_json(self, data: Any) -> None: ...


class BrowserChatFrameSink:
    """Serialize typed chat frames while a browser remains attached."""

    def __init__(self, websocket: _JsonWebSocket) -> None:
        self.websocket: _JsonWebSocket = websocket
        self.detached: bool = False

    async def send(self, frame: ChatFrame) -> None:
        """Write one frame, detaching rather than cancelling on disconnect."""
        if self.detached:
            return
        try:
            await self.websocket.send_json(frame.wire())
        except RuntimeError, WebSocketDisconnect:
            self.detached = True

    def detach(self) -> None:
        """Stop browser delivery without affecting durable execution."""
        self.detached = True


class SilentChatFrameSink:
    """Discard transient frames for unattended or recovered execution."""

    async def send(self, frame: ChatFrame) -> None:
        """Drop one typed frame."""
        _ = frame


@dataclass(frozen=True, slots=True)
class InteractiveTurnRequest:
    """Idempotent browser prompt accepted into one Conversation FIFO."""

    conversation_id: UUID
    prompt: str
    request_id: UUID
    reply_mode: ReplyMode = "text"


@dataclass(frozen=True, slots=True)
class ScheduledTurnRequest:
    """Immutable Scheduled prompt and concrete model snapshot."""

    conversation_id: UUID
    occurrence_id: UUID
    prompt: str
    model_profile: str | None
    model_config: AgentModelConfig | None = None


@dataclass(frozen=True, slots=True)
class CaptureTurnRequest:
    """A durable Voice capture serialized into one Conversation transcript."""

    conversation_id: UUID
    prompt: str
    request_id: UUID


type ConversationTurnRequest = (
    CaptureTurnRequest | InteractiveTurnRequest | ScheduledTurnRequest
)


@dataclass(frozen=True, slots=True)
class TurnTicket:
    """Stable identity returned once a durable turn exists."""

    conversation_id: UUID
    status: ConversationTurnStatus
    turn_id: UUID


@dataclass(frozen=True, slots=True)
class TurnResult:
    """Durable terminal outcome observed by `wait`."""

    failure_code: str | None
    failure_summary: str | None
    status: ConversationTurnStatus
    turn_id: UUID


@dataclass(frozen=True, slots=True)
class CancelTurnRequest:
    """Cancellation target for exactly one durable turn."""

    turn_id: UUID
    conversation_id: UUID | None = None
    pending_only: bool = False


@dataclass(frozen=True, slots=True)
class CancellationReceipt:
    """Observed cancellation outcome."""

    status: ConversationTurnStatus
    turn_id: UUID


@dataclass(frozen=True, slots=True)
class ReconciliationReport:
    """Startup repairs applied without rerunning uncertain accepted work."""

    acceptance_uncertain_failed: int
    pending_recovered: int
    running_failed: int


@dataclass(frozen=True, slots=True)
class _TerminalUpdate:
    """One typed terminal CAS assignment."""

    failure_code: str | None
    failure_phase: str | None
    failure_summary: str | None
    status: Literal["succeeded", "failed", "cancelled"]


class ConversationTurnNotFoundError(Exception):
    """A requested durable Conversation turn does not exist."""


class ConversationTurnConflictError(Exception):
    """An idempotency identity was reused for different immutable input."""


class _ExecutionOwnershipLostError(Exception):
    """A stale worker may not settle work owned by another execution lease."""


class ConversationTurns:
    """Own durable submission, FIFO execution, cancellation, and recovery.

    Callers submit a snapshot and may independently wait for settlement. Browser
    delivery is an attachment, never execution ownership.
    """

    def __init__(self, dependencies: ChatTurnDependencies) -> None:
        self.dependencies: ChatTurnDependencies = dependencies
        self._active_runtimes: dict[UUID, tuple[UUID, ChatPiRuntime]] = {}
        self._cancel_requested: set[UUID] = set()
        self._maintenance_tasks: set[asyncio.Task[None]] = set()
        self._settled: dict[UUID, asyncio.Event] = {}
        self._sinks: dict[UUID, ChatFrameSink] = {}
        self._terminal_deliveries: set[tuple[UUID, int]] = set()
        self._stopping: bool = False
        self._workers: dict[UUID, asyncio.Task[None]] = {}

    async def submit(
        self,
        request: ConversationTurnRequest,
        sink: ChatFrameSink,
    ) -> TurnTicket:
        """Validate, snapshot, and enqueue under one immediate transaction."""
        if self._stopping:
            message = "Conversation turn execution is shutting down"
            raise PiRuntimeError(message)
        request_id = (
            request.request_id
            if isinstance(request, InteractiveTurnRequest | CaptureTurnRequest)
            else None
        )
        occurrence_id = (
            request.occurrence_id if isinstance(request, ScheduledTurnRequest) else None
        )
        async with self.dependencies.conversation_service.database.transaction(
            mode="immediate"
        ) as transaction:
            conversation = await transaction.fetch_one_or_none(
                select(Conversation).where(Conversation.id.eq(request.conversation_id))
            )
            if conversation is None or conversation.status != "active":
                raise ConversationNotFoundError(request.conversation_id)
            existing = await transaction.fetch_one_or_none(
                select(ConversationTurn).where(
                    ConversationTurn.request_id.eq(request_id)
                    if request_id is not None
                    else ConversationTurn.scheduled_occurrence_id.eq(occurrence_id)
                )
            )
            origin = (
                "capture"
                if isinstance(request, CaptureTurnRequest)
                else "interactive"
                if isinstance(request, InteractiveTurnRequest)
                else "scheduled"
            )
            reply_mode = (
                request.reply_mode
                if isinstance(request, InteractiveTurnRequest)
                else "text"
            )
            model_profile = (
                request.model_profile
                if isinstance(request, ScheduledTurnRequest)
                else conversation.selected_model
            )
            model_config = (
                request.model_config
                if isinstance(request, ScheduledTurnRequest)
                and request.model_config is not None
                else self.dependencies.conversation_service.model_catalog.resolve(
                    model_profile
                )
            )
            if existing is None:
                if isinstance(request, ScheduledTurnRequest):
                    occurrence = await transaction.fetch_one_or_none(
                        select(ScheduledOccurrence).where(
                            ScheduledOccurrence.id.eq(request.occurrence_id)
                        )
                    )
                    trigger = (
                        None
                        if occurrence is None
                        else await transaction.fetch_one_or_none(
                            select(ScheduledTrigger).where(
                                ScheduledTrigger.id.eq(occurrence.trigger_id)
                            )
                        )
                    )
                    if (
                        occurrence is None
                        or occurrence.action_kind != "prompt"
                        or occurrence.status not in {"pending", "running"}
                        or occurrence.target_conversation_id != conversation.id
                        or occurrence.payload != request.prompt
                        or occurrence.model_profile != request.model_profile
                        or (
                            request.model_config is not None
                            and (
                                occurrence.model_id_snapshot
                                != request.model_config.model_id
                                or occurrence.model_provider_snapshot
                                != request.model_config.provider
                                or occurrence.model_thinking_level_snapshot
                                != request.model_config.thinking_level
                            )
                        )
                        or trigger is None
                        or trigger.deleted_at is not None
                    ):
                        raise ConversationTurnConflictError(request.occurrence_id)
                latest = await transaction.fetch_one_or_none(
                    select(ConversationTurn)
                    .where(ConversationTurn.conversation_id.eq(conversation.id))
                    .order_by(ConversationTurn.turn_seq.desc())
                    .limit(1)
                )
                existing = await transaction.execute(
                    insert(
                        ConversationTurn(
                            conversation_id=conversation.id,
                            model_display_name_snapshot=(
                                None
                                if model_config is None
                                else model_config.display_name
                            ),
                            model_id_snapshot=(
                                None if model_config is None else model_config.model_id
                            ),
                            model_provider_snapshot=(
                                None if model_config is None else model_config.provider
                            ),
                            model_snapshot=(
                                model_profile
                                if model_profile is not None
                                else None
                                if model_config is None
                                else model_config.id
                            ),
                            model_thinking_level_snapshot=(
                                None
                                if model_config is None
                                else model_config.thinking_level
                            ),
                            origin=origin,
                            prompt_snapshot=request.prompt,
                            reply_mode=reply_mode,
                            request_id=request_id,
                            scope_brief_snapshot=conversation.scope_brief,
                            scope_revision_snapshot=conversation.scope_revision,
                            scheduled_occurrence_id=occurrence_id,
                            status="pending",
                            turn_seq=1 if latest is None else latest.turn_seq + 1,
                        )
                    ).returning()
                )
            else:
                self._validate_duplicate(
                    existing,
                    request=request,
                    origin=origin,
                    reply_mode=reply_mode,
                )
        self._sinks[existing.id] = sink
        await sink.send(
            TurnQueuedFrame(
                conversation_id=existing.conversation_id,
                status=existing.status,
                turn_id=existing.id,
            )
        )
        if existing.status in _TERMINAL_STATUSES:
            await self._send_terminal(existing, sink)
            _ = self._sinks.pop(existing.id, None)
            self._terminal_deliveries.discard((existing.id, id(sink)))
        else:
            self._wake(existing.conversation_id)
        self._publish_navigation_state_later()
        return TurnTicket(
            conversation_id=existing.conversation_id,
            status=existing.status,
            turn_id=existing.id,
        )

    def _validate_duplicate(
        self,
        turn: ConversationTurn[Fetched],
        *,
        request: ConversationTurnRequest,
        origin: str,
        reply_mode: str,
    ) -> None:
        """Reject idempotency reuse when any immutable request field differs."""
        scheduled_model_conflict = isinstance(request, ScheduledTurnRequest) and (
            turn.model_snapshot != request.model_profile
            or (
                request.model_config is not None
                and (
                    turn.model_id_snapshot != request.model_config.model_id
                    or turn.model_provider_snapshot != request.model_config.provider
                    or turn.model_thinking_level_snapshot
                    != request.model_config.thinking_level
                )
            )
        )
        if (
            turn.conversation_id != request.conversation_id
            or turn.scheduled_occurrence_id
            != (
                request.occurrence_id
                if isinstance(request, ScheduledTurnRequest)
                else None
            )
            or turn.origin != origin
            or turn.prompt_snapshot != request.prompt
            or turn.reply_mode != reply_mode
            or scheduled_model_conflict
        ):
            raise ConversationTurnConflictError(turn.id)

    async def _send_terminal(
        self,
        turn: ConversationTurn[Fetched],
        sink: ChatFrameSink,
    ) -> None:
        """Emit one typed terminal result to each attached adapter."""
        target_sink = self._sinks.get(turn.id, sink)
        delivery = (turn.id, id(target_sink))
        if delivery in self._terminal_deliveries:
            return
        self._terminal_deliveries.add(delivery)
        await target_sink.send(
            TurnEndedFrame(
                conversation_id=turn.conversation_id,
                failure_code=turn.failure_code,
                failure_summary=turn.failure_summary,
                status=turn.status,
                turn_id=turn.id,
            )
        )

    async def wait(self, turn_id: UUID) -> TurnResult:
        """Wait for terminal settlement without dispatching execution."""
        turn = await self._fetch_turn(turn_id)
        if turn.status not in _TERMINAL_STATUSES:
            event = self._settled.setdefault(turn_id, asyncio.Event())
            turn = await self._fetch_turn(turn_id)
            if turn.status not in _TERMINAL_STATUSES:
                _ = await event.wait()
                turn = await self._fetch_turn(turn_id)
        return TurnResult(
            failure_code=turn.failure_code,
            failure_summary=turn.failure_summary,
            status=turn.status,
            turn_id=turn.id,
        )

    async def cancel(self, request: CancelTurnRequest) -> CancellationReceipt:
        """CAS cancellation and abort only the runtime holding the durable lease."""
        async with self.dependencies.conversation_service.database.transaction(
            mode="immediate"
        ) as transaction:
            turn = await transaction.fetch_one_or_none(
                select(ConversationTurn).where(ConversationTurn.id.eq(request.turn_id))
            )
            if turn is None or (
                request.conversation_id is not None
                and turn.conversation_id != request.conversation_id
            ):
                raise ConversationTurnNotFoundError(request.turn_id)
            if turn.status in _TERMINAL_STATUSES or (
                request.pending_only and turn.status != "pending"
            ):
                return CancellationReceipt(status=turn.status, turn_id=turn.id)
            lease_id = turn.execution_lease_id
            if turn.status == "pending" and turn.acceptance_started_at is None:
                matched = await transaction.execute(
                    update(ConversationTurn)
                    .set(
                        ConversationTurn.completed_at.to(CurrentTimestamp),
                        ConversationTurn.status.to("cancelled"),
                    )
                    .where(ConversationTurn.id.eq(turn.id))
                    .where(ConversationTurn.status.eq("pending"))
                    .where(ConversationTurn.acceptance_started_at.is_null())
                )
            else:
                matched = await transaction.execute(
                    update(ConversationTurn)
                    .set(ConversationTurn.cancel_requested_at.to(CurrentTimestamp))
                    .where(ConversationTurn.id.eq(turn.id))
                    .where(ConversationTurn.status.in_("pending", "running"))
                    .where(ConversationTurn.execution_lease_id.eq(lease_id))
                )
        if matched == 1 and lease_id is None:
            self._publish_navigation_state_later(include_messages=True)
            self._notify_settled(turn.id)
            attached_sink = self._sinks.get(turn.id, SilentChatFrameSink())
            await self._send_terminal(await self._fetch_turn(turn.id), attached_sink)
            _ = self._sinks.pop(turn.id, None)
            self._terminal_deliveries.discard((turn.id, id(attached_sink)))
        elif matched == 1:
            self._cancel_requested.add(turn.id)
            active = self._active_runtimes.get(turn.id)
            if active is not None and active[0] == lease_id:
                _ = await active[1].client.request("abort")
        current = await self._fetch_turn(turn.id)
        return CancellationReceipt(status=current.status, turn_id=current.id)

    async def repair(
        self,
        now: datetime,
    ) -> ReconciliationReport:
        """Repair durable interrupted state without launching agent work."""
        acceptance_uncertain_failed = 0
        pending_recovered = 0
        running_failed = 0
        dream_conversations: set[UUID] = set()
        interrupted_conversations: set[UUID] = set()
        async with self.dependencies.conversation_service.database.transaction(
            mode="immediate"
        ) as transaction:
            turns = await transaction.fetch_all(select(ConversationTurn).all())
            for turn in turns:
                scheduled_work_is_executable = True
                if (
                    turn.origin == "scheduled"
                    and turn.scheduled_occurrence_id is not None
                ):
                    occurrence = await transaction.fetch_one_or_none(
                        select(ScheduledOccurrence).where(
                            ScheduledOccurrence.id.eq(turn.scheduled_occurrence_id)
                        )
                    )
                    trigger = (
                        None
                        if occurrence is None
                        else await transaction.fetch_one_or_none(
                            select(ScheduledTrigger).where(
                                ScheduledTrigger.id.eq(occurrence.trigger_id)
                            )
                        )
                    )
                    scheduled_work_is_executable = (
                        occurrence is not None
                        and occurrence.status in {"pending", "running"}
                        and trigger is not None
                        and trigger.deleted_at is None
                    )
                if turn.status == "pending" and not scheduled_work_is_executable:
                    _ = await transaction.execute(
                        update(ConversationTurn)
                        .set(
                            ConversationTurn.completed_at.to(now),
                            ConversationTurn.status.to("cancelled"),
                        )
                        .where(ConversationTurn.id.eq(turn.id))
                        .where(ConversationTurn.status.eq("pending"))
                    )
                elif turn.status == "running":
                    running_failed += 1
                    interrupted_conversations.add(turn.conversation_id)
                    _ = await transaction.execute(
                        update(ConversationTurn)
                        .set(
                            ConversationTurn.completed_at.to(now),
                            ConversationTurn.failure_code.to("host_restarted"),
                            ConversationTurn.failure_phase.to("postaccept"),
                            ConversationTurn.failure_summary.to(
                                "Execution stopped when the host restarted."
                            ),
                            ConversationTurn.status.to("failed"),
                        )
                        .where(ConversationTurn.id.eq(turn.id))
                        .where(ConversationTurn.status.eq("running"))
                    )
                elif (
                    turn.status == "pending" and turn.acceptance_started_at is not None
                ):
                    acceptance_uncertain_failed += 1
                    interrupted_conversations.add(turn.conversation_id)
                    _ = await transaction.execute(
                        update(ConversationTurn)
                        .set(
                            ConversationTurn.completed_at.to(now),
                            ConversationTurn.failure_code.to("acceptance_uncertain"),
                            ConversationTurn.failure_phase.to("acceptance"),
                            ConversationTurn.failure_summary.to(
                                "Prompt acceptance was uncertain after restart."
                            ),
                            ConversationTurn.status.to("failed"),
                        )
                        .where(ConversationTurn.id.eq(turn.id))
                        .where(ConversationTurn.status.eq("pending"))
                        .where(ConversationTurn.acceptance_started_at.is_not_null())
                    )
                elif turn.status == "pending":
                    pending_recovered += 1
                if (
                    (
                        turn.status in _TERMINAL_STATUSES
                        or turn.status == "running"
                        or turn.acceptance_started_at is not None
                    )
                    and turn.origin in {"capture", "interactive"}
                    and await transaction.fetch_one_or_none(
                        select(Message)
                        .where(Message.turn_id.eq(turn.id))
                        .where(Message.role.eq("user"))
                        .limit(1)
                    )
                    is not None
                ):
                    dream_conversations.add(turn.conversation_id)
        for conversation_id in interrupted_conversations:
            _ = await self.dependencies.conversation_service.rotate_pi_session(
                conversation_id
            )
            await self.dependencies.runtime_registry.discard(conversation_id)
        for conversation_id in dream_conversations:
            await self._queue_dreaming(conversation_id)
        return ReconciliationReport(
            acceptance_uncertain_failed=acceptance_uncertain_failed,
            pending_recovered=pending_recovered,
            running_failed=running_failed,
        )

    async def dispatch_recovered(self) -> int:
        """Wake safe pending work after request-serving dependencies are ready."""
        async with (
            self.dependencies.conversation_service.database.transaction() as transaction
        ):
            pending = await transaction.fetch_all(
                select(ConversationTurn)
                .where(ConversationTurn.status.eq("pending"))
                .where(ConversationTurn.acceptance_started_at.is_null())
            )
        for conversation_id in {turn.conversation_id for turn in pending}:
            self._wake(conversation_id)
        return len(pending)

    async def observe_committed_cancellation(self, turn_id: UUID) -> None:
        """Notify process-local owners after another module fenced a turn."""
        current = await self._fetch_turn(turn_id)
        if current.status == "cancelled":
            self._publish_navigation_state_later(include_messages=True)
            self._notify_settled(turn_id)
            attached_sink = self._sinks.get(turn_id, SilentChatFrameSink())
            await self._send_terminal(current, attached_sink)
            _ = self._sinks.pop(turn_id, None)
            self._terminal_deliveries.discard((turn_id, id(attached_sink)))
            return
        if current.cancel_requested_at is None:
            return
        self._cancel_requested.add(turn_id)
        active = self._active_runtimes.get(turn_id)
        if active is not None and active[0] == current.execution_lease_id:
            try:
                _ = await active[1].client.request("abort")
            except Exception as error:
                _logger.warning(
                    "Pi abort failed after durable Conversation cancellation",
                    error_type=type(error).__name__,
                    turn_id=str(turn_id),
                )

    async def shutdown(self, *, drain_seconds: float = 1.0) -> None:
        """Briefly drain accepted work, then fail unresolved running turns."""
        self._stopping = True
        tasks = [*self._workers.values(), *self._maintenance_tasks]
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=drain_seconds)
            for task in pending:
                _ = task.cancel()
            for task in pending:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        interrupted_conversations: set[UUID] = set()
        async with self.dependencies.conversation_service.database.transaction(
            mode="immediate"
        ) as transaction:
            nonterminal = await transaction.fetch_all(
                select(ConversationTurn).where(
                    ConversationTurn.status.in_("pending", "running")
                )
            )
            interrupted: list[ConversationTurn[Fetched]] = []
            for turn in nonterminal:
                if turn.status == "pending" and turn.acceptance_started_at is None:
                    continue
                interrupted_conversations.add(turn.conversation_id)
                failure_code = (
                    "host_shutdown"
                    if turn.status == "running"
                    else "acceptance_uncertain"
                )
                failure_phase = (
                    "postaccept" if turn.status == "running" else "acceptance"
                )
                failure_summary = (
                    "Execution stopped during host shutdown."
                    if turn.status == "running"
                    else "Prompt acceptance was uncertain during host shutdown."
                )
                matched = await transaction.execute(
                    update(ConversationTurn)
                    .set(
                        ConversationTurn.completed_at.to(CurrentTimestamp),
                        ConversationTurn.failure_code.to(failure_code),
                        ConversationTurn.failure_phase.to(failure_phase),
                        ConversationTurn.failure_summary.to(failure_summary),
                        ConversationTurn.status.to("failed"),
                    )
                    .where(ConversationTurn.id.eq(turn.id))
                    .where(ConversationTurn.status.eq(turn.status))
                )
                if matched == 1:
                    interrupted.append(turn)
        for turn in interrupted:
            self._notify_settled(turn.id)
            await self._send_terminal(
                await self._fetch_turn(turn.id),
                self._sinks.get(turn.id, SilentChatFrameSink()),
            )
        for conversation_id in interrupted_conversations:
            _ = await self.dependencies.conversation_service.rotate_pi_session(
                conversation_id
            )
            await self.dependencies.runtime_registry.discard(conversation_id)

    def _wake(self, conversation_id: UUID) -> None:
        """Keep at most one worker per Conversation while allowing cross-chat work."""
        worker = self._workers.get(conversation_id)
        if worker is not None and not worker.done():
            return
        worker = asyncio.create_task(self._work_conversation(conversation_id))
        self._workers[conversation_id] = worker
        worker.add_done_callback(
            lambda completed, target=conversation_id: self._worker_done(
                target,
                completed,
            )
        )

    def _worker_done(
        self,
        conversation_id: UUID,
        worker: asyncio.Task[None],
    ) -> None:
        """Release worker ownership after observing any unexpected defect."""
        if self._workers.get(conversation_id) is worker:
            _ = self._workers.pop(conversation_id, None)
        if worker.cancelled():
            return
        if worker.exception() is not None:
            _logger.error(
                "Conversation worker stopped unexpectedly",
                conversation_id=str(conversation_id),
                error=str(worker.exception()),
            )
            return
        if not self._stopping:
            restart = asyncio.create_task(self._restart_pending(conversation_id))
            self._maintenance_tasks.add(restart)
            restart.add_done_callback(self._maintenance_tasks.discard)

    async def _restart_pending(self, conversation_id: UUID) -> None:
        """Close the submit-versus-worker-exit race without polling."""
        async with (
            self.dependencies.conversation_service.database.transaction() as transaction
        ):
            pending = await transaction.fetch_one_or_none(
                select(ConversationTurn)
                .where(ConversationTurn.conversation_id.eq(conversation_id))
                .where(ConversationTurn.status.eq("pending"))
                .limit(1)
            )
        if pending is not None:
            self._wake(conversation_id)

    async def _work_conversation(self, conversation_id: UUID) -> None:
        """Execute durable pending turns in creation order for one Conversation."""
        while not self._stopping:
            async with (
                self.dependencies.conversation_service.database.transaction() as transaction
            ):
                turn = await transaction.fetch_one_or_none(
                    select(ConversationTurn)
                    .where(ConversationTurn.conversation_id.eq(conversation_id))
                    .where(ConversationTurn.status.eq("pending"))
                    .order_by(ConversationTurn.turn_seq.asc())
                    .limit(1)
                )
            if turn is None:
                return
            async with self.dependencies.turn_queue.serialize(conversation_id):
                current = await self._fetch_turn(turn.id)
                if current.status == "pending":
                    await self._execute(current)

    async def _execute(self, turn: ConversationTurn[Fetched]) -> None:
        """Claim one FIFO head and drive it without unconditional lifecycle writes."""
        sink = self._sinks.get(turn.id, SilentChatFrameSink())
        try:
            await self._drive_execution(turn, sink)
        except PiPreacceptTransientError:
            await self._fail(
                turn,
                code="preaccept_retry_exhausted",
                phase="preaccept",
                summary="Agent was unavailable before accepting the prompt.",
                sink=sink,
            )
        except TimeoutError:
            await self._fail(
                turn,
                code="agent_timeout",
                phase="postaccept",
                summary="Agent stopped responding during generation.",
                sink=sink,
            )
        except ConversationNotFoundError:
            await self._fail(
                turn,
                code="conversation_not_found",
                phase="dispatch",
                summary="Conversation no longer exists.",
                sink=sink,
            )
        except _ExecutionOwnershipLostError:
            _logger.warning(
                "Conversation execution lease changed before settlement",
                conversation_id=str(turn.conversation_id),
                turn_id=str(turn.id),
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            _logger.exception(
                "Conversation turn execution failed",
                conversation_id=str(turn.conversation_id),
                turn_id=str(turn.id),
                error_type=type(error).__name__,
            )
            await self._fail(
                turn,
                code="execution_failed",
                phase="unknown",
                summary="Agent execution failed.",
                sink=sink,
            )
        finally:
            _ = self._active_runtimes.pop(turn.id, None)
            current = await self._fetch_turn(turn.id)
            if turn.id in self._cancel_requested or current.status == "failed":
                _ = await self.dependencies.conversation_service.rotate_pi_session(
                    turn.conversation_id
                )
                await self.dependencies.runtime_registry.discard(turn.conversation_id)
                _ = self._cancel_requested.discard(turn.id)
            if await self._has_terminal_user_message(turn.id):
                await self._queue_dreaming(turn.conversation_id)
            attached_sink = self._sinks.pop(turn.id, None)
            if attached_sink is not None:
                self._terminal_deliveries.discard((turn.id, id(attached_sink)))

    async def _drive_execution(
        self,
        turn: ConversationTurn[Fetched],
        sink: ChatFrameSink,
    ) -> None:
        """Resolve a turn's durable execution context before its owned drive."""
        if turn.origin == "capture":
            _ = await self._execute_capture(turn, sink)
            return
        conversation = await self.dependencies.conversation_service.fetch_conversation(
            turn.conversation_id
        )
        conversation = await self.dependencies.conversation_service.activate_turn_scope(
            conversation,
            scope_revision=turn.scope_revision_snapshot,
        )
        conversation = await self.dependencies.conversation_service.resolve_session(
            conversation,
            now=datetime.now(UTC),
            gap=SESSION_GAP,
        )
        _ = await self._drive_turn(turn, conversation, sink)

    async def _execute_capture(
        self,
        turn: ConversationTurn[Fetched],
        sink: ChatFrameSink,
    ) -> bool:
        """Append and settle a capture atomically at its FIFO position."""
        async with self.dependencies.conversation_service.database.transaction(
            mode="immediate"
        ) as transaction:
            current = await transaction.fetch_one(
                select(ConversationTurn).where(ConversationTurn.id.eq(turn.id))
            )
            if current.status != "pending":
                return False
            message = (
                await self.dependencies.conversation_service.append_initiating_message(
                    transaction,
                    MessageDraft(
                        content=turn.prompt_snapshot or "",
                        conversation_id=turn.conversation_id,
                        role="user",
                        turn_id=turn.id,
                    ),
                )
            )
            matched = await transaction.execute(
                update(ConversationTurn)
                .set(
                    ConversationTurn.completed_at.to(CurrentTimestamp),
                    ConversationTurn.started_at.to(CurrentTimestamp),
                    ConversationTurn.status.to("succeeded"),
                )
                .where(ConversationTurn.id.eq(turn.id))
                .where(ConversationTurn.status.eq("pending"))
            )
        if matched != 1:
            return False
        await sink.send(
            UserMessageFrame(
                conversation_id=turn.conversation_id,
                message_id=message.id,
                seq=message.seq,
                turn_id=turn.id,
            )
        )
        settled = await self._fetch_turn(turn.id)
        self._notify_settled(turn.id)
        await self._send_terminal(settled, sink)
        self._publish_navigation_state_later(include_messages=True)
        return True

    async def _drive_turn(
        self,
        turn: ConversationTurn[Fetched],
        conversation: Conversation[Fetched],
        sink: ChatFrameSink,
    ) -> bool:
        """Apply submitted snapshots, accept once safely, and stream settlement."""
        session_id = str(conversation.pi_session_id)
        lease_id = uuid4()
        with record_run(
            self.dependencies.trace_recorder,
            session_id=session_id,
            kind=("conversation" if turn.origin == "interactive" else "scheduled"),
            prompt=turn.prompt_snapshot,
            correlation=RunCorrelation(
                conversation_id=str(turn.conversation_id),
                origin=turn.origin,
                turn_id=str(turn.id),
            ),
        ) as run:
            if run.run_id is not None:
                await self._set_trace_run_id(turn.id, run.run_id)
            runtime = await self._runtime_for_snapshot(turn, conversation)
            self._active_runtimes[turn.id] = (lease_id, runtime)
            message = await self._claim_and_append(turn, lease_id)
            if message is None:
                return False
            self._publish_navigation_state_later(include_messages=True)
            await sink.send(
                UserMessageFrame(
                    conversation_id=turn.conversation_id,
                    message_id=message.id,
                    seq=message.seq,
                    turn_id=turn.id,
                )
            )
            await self._send_skill_status(turn, runtime, sink)
            selected_model = (
                AgentModelConfig(
                    display_name=(
                        turn.model_display_name_snapshot
                        or turn.model_id_snapshot
                        or "Submitted model"
                    ),
                    id=turn.model_snapshot or turn.model_id_snapshot or "submitted",
                    model_id=turn.model_id_snapshot,
                    provider=turn.model_provider_snapshot,
                    thinking_level=cast(
                        "ThinkingLevel | None",
                        turn.model_thinking_level_snapshot,
                    ),
                )
                if turn.model_id_snapshot is not None
                and turn.model_provider_snapshot is not None
                else None
            )
            if selected_model is not None:
                await runtime.apply_model(selected_model)
            _ = runtime.drain_events()
            prompt_response = await self._accept_with_retry(
                turn, runtime, lease_id=lease_id
            )
            if prompt_response is None:
                await self._send_terminal(await self._fetch_turn(turn.id), sink)
                return True
            if prompt_response.get("success") is not True:
                raw_failure = str(prompt_response)
                run.mark("error", raw_failure)
                _logger.warning(
                    "Pi rejected Conversation prompt",
                    conversation_id=str(turn.conversation_id),
                    turn_id=str(turn.id),
                )
                await self._fail(
                    turn,
                    code="prompt_rejected",
                    phase="preaccept",
                    summary="Agent could not accept the prompt.",
                    sink=sink,
                )
                return True
            if not await self._set_running(turn.id, lease_id=lease_id):
                await self._send_terminal(await self._fetch_turn(turn.id), sink)
                return True

            async def settle_before_terminal(
                final_text: str,
                provider_error: str | None,
            ) -> None:
                if turn.id in self._cancel_requested:
                    run.mark("aborted", "turn cancelled")
                elif provider_error is not None:
                    run.mark("error", provider_error)
                await self._settle_stream_outcome(
                    turn,
                    final_text=final_text,
                    provider_error=provider_error,
                    sink=sink,
                    lease_id=lease_id,
                )

            _ = await stream_chat_turn(
                sink,
                self.dependencies,
                runtime=runtime,
                spec=TurnSpec(
                    before_terminal=settle_before_terminal,
                    conversation_id=turn.conversation_id,
                    reply_mode=cast("ReplyMode", turn.reply_mode),
                    session_id=session_id,
                    turn_id=turn.id,
                ),
            )
            await self._send_terminal(await self._fetch_turn(turn.id), sink)
            return True

    async def _claim_and_append(
        self,
        turn: ConversationTurn[Fetched],
        lease_id: UUID,
    ) -> Message[Fetched] | None:
        """Atomically lease the FIFO head, mark acceptance, and append once."""
        async with self.dependencies.conversation_service.database.transaction(
            mode="immediate"
        ) as transaction:
            current = await transaction.fetch_one(
                select(ConversationTurn).where(ConversationTurn.id.eq(turn.id))
            )
            if current.status != "pending" or current.cancel_requested_at is not None:
                return None
            head = await transaction.fetch_one_or_none(
                select(ConversationTurn)
                .where(ConversationTurn.conversation_id.eq(turn.conversation_id))
                .where(ConversationTurn.status.eq("pending"))
                .order_by(ConversationTurn.turn_seq.asc())
                .limit(1)
            )
            if head is None or head.id != turn.id:
                return None
            message = (
                await self.dependencies.conversation_service.append_initiating_message(
                    transaction,
                    MessageDraft(
                        content=turn.prompt_snapshot or "",
                        conversation_id=turn.conversation_id,
                        role="user" if turn.origin == "interactive" else "scheduled",
                        turn_id=turn.id,
                    ),
                )
            )
            matched = await transaction.execute(
                update(ConversationTurn)
                .set(
                    ConversationTurn.acceptance_started_at.to(CurrentTimestamp),
                    ConversationTurn.attempts.to(max(1, current.attempts)),
                    ConversationTurn.execution_lease_id.to(lease_id),
                )
                .where(ConversationTurn.id.eq(turn.id))
                .where(ConversationTurn.status.eq("pending"))
                .where(ConversationTurn.cancel_requested_at.is_null())
            )
            return message if matched == 1 else None

    async def _runtime_for_snapshot(
        self,
        turn: ConversationTurn[Fetched],
        conversation: Conversation[Fetched],
    ) -> ChatPiRuntime:
        """Use the production snapshot adapter while preserving simple test fakes."""
        registry = self.dependencies.runtime_registry
        if isinstance(registry, _SnapshotRuntimeRegistry):
            return await registry.runtime_for_snapshot(
                conversation,
                scope_brief=turn.scope_brief_snapshot,
                scope_revision=turn.scope_revision_snapshot,
            )
        return await registry.runtime_for(conversation)

    async def _send_skill_status(
        self,
        turn: ConversationTurn[Fetched],
        runtime: ChatPiRuntime,
        sink: ChatFrameSink,
    ) -> None:
        """Expose only the confirmed generic loaded-skill count."""
        if not getattr(runtime, "skills_confirmed", False):
            return
        loaded_skills = cast(
            "tuple[object, ...]",
            getattr(runtime, "loaded_skills", ()),
        )
        await sink.send(
            SkillStatusFrame(
                conversation_id=turn.conversation_id,
                loaded_count=len(loaded_skills),
                turn_id=turn.id,
            )
        )

    async def _settle_stream_outcome(
        self,
        turn: ConversationTurn[Fetched],
        *,
        final_text: str,
        provider_error: str | None,
        sink: ChatFrameSink,
        lease_id: UUID,
    ) -> None:
        """CAS terminal lifecycle before `stream_chat_turn` sends agent_end."""
        if turn.id in self._cancel_requested:
            settled = await self._settle(
                turn.id,
                _TerminalUpdate(
                    failure_code=None,
                    failure_phase=None,
                    failure_summary=None,
                    status="cancelled",
                ),
                lease_id=lease_id,
            )
        elif provider_error is not None:
            settled = await self._settle(
                turn.id,
                _TerminalUpdate(
                    failure_code="provider_failed",
                    failure_phase="postaccept",
                    failure_summary="The model failed while generating a response.",
                    status="failed",
                ),
                lease_id=lease_id,
            )
        elif not final_text.strip():
            settled = await self._settle(
                turn.id,
                _TerminalUpdate(
                    failure_code="no_answer",
                    failure_phase="postaccept",
                    failure_summary="The model completed without an answer.",
                    status="failed",
                ),
                lease_id=lease_id,
            )
        else:
            settled = await self._settle(
                turn.id,
                _TerminalUpdate(
                    failure_code=None,
                    failure_phase=None,
                    failure_summary=None,
                    status="succeeded",
                ),
                lease_id=lease_id,
            )
        if settled.status == "failed":
            await sink.send(
                ErrorFrame(
                    conversation_id=turn.conversation_id,
                    detail=settled.failure_summary or "Chat turn failed.",
                    turn_id=turn.id,
                )
            )

    async def _accept_with_retry(
        self,
        turn: ConversationTurn[Fetched],
        runtime: ChatPiRuntime,
        *,
        lease_id: UUID,
    ) -> dict[str, Any] | None:
        """Retry only typed known-preaccept transients, at most twice."""
        retries = 0
        while True:
            if retries > 0 and not await self._mark_acceptance_started(
                turn.id, retries + 1, lease_id=lease_id
            ):
                return {"success": False}
            now = datetime.now().astimezone()
            canonical_prompt = turn.prompt_snapshot or ""
            pi_prompt = (
                canonical_prompt
                if turn.origin == "interactive"
                else _SCHEDULED_CONTEXT.format(prompt=canonical_prompt)
            )
            if not await self._prompt_attempt_is_executable(
                turn.id,
                lease_id=lease_id,
            ):
                return None
            try:
                return await runtime.client.request(
                    "prompt",
                    message=prompt_with_time_context(
                        pi_prompt,
                        now=now,
                        timezone_name=local_timezone_name(now),
                        reply_mode=cast("ReplyMode", turn.reply_mode),
                    ),
                )
            except PiPreacceptTransientError:
                if retries >= _MAX_PREACCEPT_RETRIES:
                    raise
                retries += 1
                await self._clear_acceptance_started(turn.id, lease_id=lease_id)
                await asyncio.sleep(0)

    async def _prompt_attempt_is_executable(
        self,
        turn_id: UUID,
        *,
        lease_id: UUID,
    ) -> bool:
        """Check cancellation immediately before each external pi prompt write."""
        current = await self._fetch_turn(turn_id)
        if (
            current.status == "pending"
            and current.execution_lease_id == lease_id
            and current.cancel_requested_at is None
        ):
            return True
        if current.status in {"pending", "running"}:
            _ = await self._settle(
                turn_id,
                _TerminalUpdate(
                    failure_code=None,
                    failure_phase=None,
                    failure_summary=None,
                    status="cancelled",
                ),
                lease_id=lease_id,
            )
        return False

    async def _fail(
        self,
        turn: ConversationTurn[Fetched],
        *,
        code: str,
        phase: str,
        summary: str,
        sink: ChatFrameSink,
    ) -> None:
        """Persist stable failure state before notifying an attached sink."""
        current = await self._fetch_turn(turn.id)
        if current.status not in _TERMINAL_STATUSES:
            current = await self._settle(
                turn.id,
                _TerminalUpdate(
                    failure_code=code,
                    failure_phase=phase,
                    failure_summary=summary,
                    status="failed",
                ),
                lease_id=current.execution_lease_id,
            )
        if current.status == "failed":
            await sink.send(
                ErrorFrame(
                    conversation_id=turn.conversation_id,
                    detail=summary,
                    turn_id=turn.id,
                )
            )
        await self._send_terminal(current, sink)

    async def _set_running(self, turn_id: UUID, *, lease_id: UUID) -> bool:
        async with self.dependencies.conversation_service.database.transaction(
            mode="immediate"
        ) as transaction:
            matched = await transaction.execute(
                update(ConversationTurn)
                .set(
                    ConversationTurn.started_at.to(CurrentTimestamp),
                    ConversationTurn.status.to("running"),
                )
                .where(ConversationTurn.id.eq(turn_id))
                .where(ConversationTurn.status.eq("pending"))
                .where(ConversationTurn.execution_lease_id.eq(lease_id))
                .where(ConversationTurn.cancel_requested_at.is_null())
            )
        if matched == 1:
            self._publish_navigation_state_later()
            return True
        current = await self._fetch_turn(turn_id)
        if current.cancel_requested_at is not None and current.status == "pending":
            _ = await self._settle(
                turn_id,
                _TerminalUpdate(
                    failure_code=None,
                    failure_phase=None,
                    failure_summary=None,
                    status="cancelled",
                ),
                lease_id=lease_id,
            )
        return False

    async def _mark_acceptance_started(
        self,
        turn_id: UUID,
        attempts: int,
        *,
        lease_id: UUID,
    ) -> bool:
        async with self.dependencies.conversation_service.database.transaction(
            mode="immediate"
        ) as transaction:
            matched = await transaction.execute(
                update(ConversationTurn)
                .set(
                    ConversationTurn.acceptance_started_at.to(CurrentTimestamp),
                    ConversationTurn.attempts.to(attempts),
                )
                .where(ConversationTurn.id.eq(turn_id))
                .where(ConversationTurn.status.eq("pending"))
                .where(ConversationTurn.execution_lease_id.eq(lease_id))
                .where(ConversationTurn.cancel_requested_at.is_null())
            )
        return matched == 1

    async def _clear_acceptance_started(
        self,
        turn_id: UUID,
        *,
        lease_id: UUID,
    ) -> None:
        async with self.dependencies.conversation_service.database.transaction(
            mode="immediate"
        ) as transaction:
            _ = await transaction.execute(
                update(ConversationTurn)
                .set(ConversationTurn.acceptance_started_at.to(None))
                .where(ConversationTurn.id.eq(turn_id))
                .where(ConversationTurn.status.eq("pending"))
                .where(ConversationTurn.execution_lease_id.eq(lease_id))
            )

    async def _set_trace_run_id(self, turn_id: UUID, run_id: str) -> None:
        async with self.dependencies.conversation_service.database.transaction(
            mode="immediate"
        ) as transaction:
            _ = await transaction.execute(
                update(ConversationTurn)
                .set(ConversationTurn.trace_run_id.to(run_id))
                .where(ConversationTurn.id.eq(turn_id))
            )

    async def _settle(
        self,
        turn_id: UUID,
        terminal: _TerminalUpdate,
        *,
        lease_id: UUID | None = None,
    ) -> ConversationTurn[Fetched]:
        """Choose one terminal state atomically under exact execution ownership."""
        async with self.dependencies.conversation_service.database.transaction(
            mode="immediate"
        ) as transaction:
            current = await transaction.fetch_one(
                select(ConversationTurn).where(ConversationTurn.id.eq(turn_id))
            )
            if current.status in _TERMINAL_STATUSES:
                return current
            if current.execution_lease_id != lease_id:
                raise _ExecutionOwnershipLostError(turn_id)
            selected = (
                _TerminalUpdate(
                    failure_code=None,
                    failure_phase=None,
                    failure_summary=None,
                    status="cancelled",
                )
                if current.cancel_requested_at is not None
                else terminal
            )
            statement = (
                update(ConversationTurn)
                .set(
                    ConversationTurn.completed_at.to(CurrentTimestamp),
                    ConversationTurn.failure_code.to(selected.failure_code),
                    ConversationTurn.failure_phase.to(selected.failure_phase),
                    ConversationTurn.failure_summary.to(selected.failure_summary),
                    ConversationTurn.status.to(selected.status),
                )
                .where(ConversationTurn.id.eq(turn_id))
                .where(ConversationTurn.status.eq(current.status))
            )
            statement = (
                statement.where(ConversationTurn.execution_lease_id.is_null())
                if lease_id is None
                else statement.where(ConversationTurn.execution_lease_id.eq(lease_id))
            )
            matched = await transaction.execute(statement)
            settled = await transaction.fetch_one(
                select(ConversationTurn).where(ConversationTurn.id.eq(turn_id))
            )
        if matched != 1 and settled.status not in _TERMINAL_STATUSES:
            raise _ExecutionOwnershipLostError(turn_id)
        if settled.status in _TERMINAL_STATUSES:
            self._publish_navigation_state_later(include_messages=True)
            self._notify_settled(turn_id)
        return settled

    async def _has_terminal_user_message(self, turn_id: UUID) -> bool:
        """Derive Dreaming eligibility from durable Evidence and lifecycle state."""
        async with (
            self.dependencies.conversation_service.database.transaction() as transaction
        ):
            turn = await transaction.fetch_one(
                select(ConversationTurn).where(ConversationTurn.id.eq(turn_id))
            )
            if turn.status not in _TERMINAL_STATUSES or turn.origin not in {
                "capture",
                "interactive",
            }:
                return False
            return (
                await transaction.fetch_one_or_none(
                    select(Message)
                    .where(Message.turn_id.eq(turn_id))
                    .where(Message.role.eq("user"))
                    .limit(1)
                )
                is not None
            )

    def _publish_navigation_state_later(
        self,
        *,
        include_messages: bool = False,
    ) -> None:
        """Publish after attached chat frames so lifecycle signals do not reorder them."""

        async def publish() -> None:
            await asyncio.sleep(0.01)
            await self.dependencies.conversation_service.publish_navigation_state(
                include_messages=include_messages
            )

        task = asyncio.create_task(publish())
        self._maintenance_tasks.add(task)
        task.add_done_callback(self._maintenance_tasks.discard)

    async def _fetch_turn(self, turn_id: UUID) -> ConversationTurn[Fetched]:
        """Fetch one durable turn or raise its domain error."""
        async with (
            self.dependencies.conversation_service.database.transaction() as transaction
        ):
            turn = await transaction.fetch_one_or_none(
                select(ConversationTurn).where(ConversationTurn.id.eq(turn_id))
            )
        if turn is None:
            raise ConversationTurnNotFoundError(turn_id)
        return turn

    def _notify_settled(self, turn_id: UUID) -> None:
        event = self._settled.get(turn_id)
        if event is not None:
            event.set()

    async def _queue_dreaming(self, conversation_id: UUID) -> None:
        """Queue Dreaming after any terminal interactive turn with user Evidence."""
        if not self.dependencies.dreaming_enabled:
            return
        try:
            queue = (
                self.dependencies.dreaming_service.queue_manual_run
                if self.dependencies.dreaming_service.consume_immediate_assimilation_request(
                    conversation_id
                )
                else self.dependencies.dreaming_service.queue_assimilation_run
            )
            _ = await queue(
                conversation_id,
                logger=self.dependencies.logger,
                now=datetime.now(UTC),
            )
        except Exception as error:
            _logger.warning(
                "Dream assimilation queueing failed after Conversation turn",
                conversation_id=str(conversation_id),
                error_type=type(error).__name__,
            )


__all__ = [
    "BrowserChatFrameSink",
    "CancelTurnRequest",
    "CancellationReceipt",
    "CaptureTurnRequest",
    "ChatFrameSink",
    "ConversationTurnConflictError",
    "ConversationTurnNotFoundError",
    "ConversationTurnRequest",
    "ConversationTurns",
    "InteractiveTurnRequest",
    "ReconciliationReport",
    "ScheduledTurnRequest",
    "SilentChatFrameSink",
    "TurnResult",
    "TurnTicket",
]
