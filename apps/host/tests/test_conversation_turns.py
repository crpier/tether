"""Public behavior tests for durable Conversation-turn execution."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4, uuid7

import structlog
from opentelemetry import trace
from snekql.sqlite import Config, Database, Fetched, insert, update
from snektest import (
    assert_eq,
    assert_isinstance,
    assert_raises,
    assert_true,
    fixture,
    load_fixture,
    test,
)

from tether.chat_frames import (
    AgentEndFrame,
    ChatFrame,
    TurnEndedFrame,
    TurnQueuedFrame,
    UserMessageFrame,
)
from tether.chat_turn import ChatTurnDependencies, ConversationTurnQueue
from tether.conversation_model import ConversationNotFoundError, MessageDraft
from tether.conversation_store import (
    Conversation,
    ConversationTurn,
    create_conversation_schema,
)
from tether.conversation_turns import (
    CancelTurnRequest,
    ConversationTurnConflictError,
    ConversationTurns,
    HealthTurnRequest,
    InteractiveTurnRequest,
    ScheduledTurnRequest,
)
from tether.conversations import ConversationService
from tether.events import HubEvent, InvalidateEvent
from tether.model_selection import AgentModelCatalog, AgentModelConfig
from tether.pi_errors import PiPreacceptTransientError
from tether.pi_runtime import ContextUsage
from tether.pi_turn_events import AgentEnded, MessageSettled, TurnEvent
from tether.trigger_schedule import OnceTriggerSpec
from tether.trigger_store import create_trigger_schema
from tether.triggers import ScheduledPromptSnapshot, TriggerService


class RecordingPublisher:
    """Collect browser invalidations at the durable turn interface."""

    def __init__(self) -> None:
        self.events: list[HubEvent] = []

    async def publish(self, event: HubEvent) -> None:
        self.events.append(event)


class RecordingTitler:
    """Record auto-titling schedules instead of calling a model."""

    def __init__(self) -> None:
        self.prompts: list[UUID] = []
        self.first_messages: list[str] = []
        self.invoked = asyncio.Event()
        self._tasks: set[asyncio.Task[None]] = set()

    def schedule(self, *, conversation_id: UUID, first_message: str) -> None:
        self.prompts.append(conversation_id)
        self.first_messages.append(first_message)
        self.invoked.set()

    async def drain(self) -> None:
        """Await any spawned titling tasks."""
        if self._tasks:
            _ = await asyncio.gather(*self._tasks)


class RecordingSink:
    """Collect typed execution frames in delivery order."""

    def __init__(self) -> None:
        self.frames: list[ChatFrame] = []

    async def send(self, frame: ChatFrame) -> None:
        self.frames.append(frame)


class BlockingUserMessageSink(RecordingSink):
    """Pause delivery after claim so cancellation can win before pi submission."""

    def __init__(self) -> None:
        super().__init__()
        self.release: asyncio.Event = asyncio.Event()
        self.user_message_seen: asyncio.Event = asyncio.Event()

    async def send(self, frame: ChatFrame) -> None:
        await super().send(frame)
        if isinstance(frame, UserMessageFrame):
            self.user_message_seen.set()
            _ = await self.release.wait()


class TerminalObservingSink(RecordingSink):
    """Observe durable settlement from inside terminal frame delivery."""

    def __init__(self, turns: ConversationTurns) -> None:
        super().__init__()
        self.turns: ConversationTurns = turns
        self.terminal_status: str | None = None

    async def send(self, frame: ChatFrame) -> None:
        await super().send(frame)
        if isinstance(frame, AgentEndFrame) and frame.turn_id is not None:
            self.terminal_status = (await self.turns.wait(frame.turn_id)).status


class AcceptingClient:
    """Accept every prompt and record command order."""

    def __init__(self) -> None:
        self.commands: list[str] = []
        self.prompt_messages: list[str] = []

    async def request(self, command_type: str, **fields: Any) -> dict[str, Any]:
        self.commands.append(command_type)
        if command_type == "prompt":
            self.prompt_messages.append(str(fields["message"]))
        return {"success": True}


class TransientClient(AcceptingClient):
    """Fail a known-safe number of prompt attempts before accepting."""

    def __init__(self, transient_failures: int) -> None:
        super().__init__()
        self.transient_failures: int = transient_failures

    async def request(self, command_type: str, **fields: Any) -> dict[str, Any]:
        _ = fields
        self.commands.append(command_type)
        if command_type == "prompt" and self.transient_failures > 0:
            self.transient_failures -= 1
            raise PiPreacceptTransientError("connection failed before write")
        return {"success": True}


class SuccessfulRuntime:
    """Settle one assistant response."""

    def __init__(self) -> None:
        self.applied_models: list[AgentModelConfig] = []
        self.client: AcceptingClient = AcceptingClient()
        self.loaded_skills: tuple[str, ...] = ()
        self.skills_confirmed: bool = False

    async def apply_model(self, model: AgentModelConfig) -> None:
        self.applied_models.append(model)

    async def fetch_context_usage(self) -> ContextUsage | None:
        return None

    def drain_events(self) -> int:
        return 0

    async def stream_turn(
        self,
        *,
        wait_seconds: float = 5.0,
    ) -> AsyncGenerator[TurnEvent]:
        _ = wait_seconds
        yield MessageSettled(reasoning="", text="settled answer")
        yield AgentEnded()


class TimingOutRuntime(SuccessfulRuntime):
    """Raise after prompt acceptance and durable user Message append."""

    async def stream_turn(
        self,
        *,
        wait_seconds: float = 5.0,
    ) -> AsyncGenerator[TurnEvent]:
        _ = wait_seconds
        raise TimeoutError
        yield AgentEnded()


class EmptyRuntime(SuccessfulRuntime):
    """Finish without assistant output or tool work."""

    async def stream_turn(
        self,
        *,
        wait_seconds: float = 5.0,
    ) -> AsyncGenerator[TurnEvent]:
        _ = wait_seconds
        yield AgentEnded()


class BlockingRuntime(SuccessfulRuntime):
    """Hold the FIFO head until a test releases generation."""

    def __init__(self) -> None:
        super().__init__()
        self.release: asyncio.Event = asyncio.Event()
        self.started: asyncio.Event = asyncio.Event()

    async def stream_turn(
        self,
        *,
        wait_seconds: float = 5.0,
    ) -> AsyncGenerator[TurnEvent]:
        _ = wait_seconds
        self.started.set()
        _ = await self.release.wait()
        if "abort" not in self.client.commands:
            yield MessageSettled(reasoning="", text="settled answer")
        yield AgentEnded()


class RuntimeRegistry:
    """Return one runtime and record discards."""

    def __init__(self, runtime: SuccessfulRuntime) -> None:
        self.runtime: SuccessfulRuntime = runtime
        self.discarded: list[object] = []

    async def runtime_for(self, conversation: object) -> SuccessfulRuntime:
        _ = conversation
        return self.runtime

    async def discard(self, conversation_id: object) -> None:
        self.discarded.append(conversation_id)


class SnapshotRuntimeRegistry(RuntimeRegistry):
    """Record exact scope snapshots applied at FIFO execution."""

    def __init__(self, runtime: SuccessfulRuntime) -> None:
        super().__init__(runtime)
        self.snapshots: list[tuple[str | None, int, object]] = []

    async def runtime_for_snapshot(
        self,
        conversation: Conversation[Fetched],
        *,
        scope_brief: str | None,
        scope_revision: int,
    ) -> SuccessfulRuntime:
        self.snapshots.append(
            (
                scope_brief,
                scope_revision,
                conversation.pi_session_id,
            )
        )
        return self.runtime


class DisabledDreaming:
    """Unused Dreaming collaborator for disabled test composition."""


class RecordingDreaming:
    """Record durable post-turn assimilation requests."""

    def __init__(self) -> None:
        self.queued: list[UUID] = []
        self.queued_event: asyncio.Event = asyncio.Event()

    def consume_immediate_assimilation_request(self, conversation_id: UUID) -> bool:
        _ = conversation_id
        return False

    async def queue_assimilation_run(
        self,
        conversation_id: UUID,
        **fields: Any,
    ) -> None:
        _ = fields
        self.queued.append(conversation_id)
        self.queued_event.set()


class CancelBeforeSuccessSettlementTurns(ConversationTurns):
    """Force cancellation after success selection but before its terminal CAS."""

    def __init__(self, dependencies: ChatTurnDependencies) -> None:
        super().__init__(dependencies)
        self.cancel_before_success: bool = False

    async def _settle(
        self,
        turn_id: UUID,
        terminal: Any,
        *,
        lease_id: UUID | None = None,
    ) -> ConversationTurn[Fetched]:
        if self.cancel_before_success and terminal.status == "succeeded":
            self.cancel_before_success = False
            _ = await self.cancel(CancelTurnRequest(turn_id=turn_id))
        return await super()._settle(turn_id, terminal, lease_id=lease_id)


@fixture
async def conversation_turns_fixture() -> AsyncGenerator[
    tuple[
        CancelBeforeSuccessSettlementTurns,
        ConversationService,
        RuntimeRegistry,
    ]
]:
    database = await Database.initialize(backend=Config(database=":memory:"))
    await create_conversation_schema(database)
    await create_trigger_schema(database)
    service = ConversationService(
        database=database,
        model_catalog=AgentModelCatalog(default_model=None, models=()),
    )
    registry = RuntimeRegistry(SuccessfulRuntime())
    turns = CancelBeforeSuccessSettlementTurns(
        ChatTurnDependencies(
            conversation_service=service,
            dreaming_enabled=False,
            dreaming_service=cast("Any", DisabledDreaming()),
            logger=structlog.stdlib.get_logger("test"),
            runtime_registry=cast("Any", registry),
            trace_recorder=None,
            turn_queue=ConversationTurnQueue(),
        )
    )
    try:
        yield turns, service, registry
    finally:
        await turns.shutdown()
        await database.close()


@test()
async def submitted_turn_settles_messages_under_one_contiguous_identity() -> None:
    """`submit` wakes execution and `wait` observes its durable success."""
    turns, service, _ = await load_fixture(conversation_turns_fixture())
    conversation = await service.fetch_main_conversation()
    sink = RecordingSink()

    ticket = await turns.submit(
        InteractiveTurnRequest(
            conversation_id=conversation.id,
            prompt="hello",
            request_id=uuid7(),
        ),
        sink,
    )
    result = await turns.wait(ticket.turn_id)
    messages = await service.fetch_messages(conversation.id)

    assert_eq(result.status, "succeeded")
    assert_eq([message.content for message in messages], ["hello", "settled answer"])
    assert_eq([message.turn_id for message in messages], [ticket.turn_id] * 2)
    assert_eq([message.turn_message_seq for message in messages], [1, 2])
    assert_eq([frame.turn_id for frame in sink.frames], [ticket.turn_id] * 6)


@fixture
async def titling_turns_fixture() -> AsyncGenerator[
    tuple[ConversationTurns, ConversationService, RecordingTitler]
]:
    database = await Database.initialize(backend=Config(database=":memory:"))
    await create_conversation_schema(database)
    await create_trigger_schema(database)
    service = ConversationService(
        database,
        model_catalog=AgentModelCatalog(default_model=None, models=()),
    )
    titler = RecordingTitler()
    turns = ConversationTurns(
        ChatTurnDependencies(
            conversation_service=service,
            dreaming_enabled=False,
            dreaming_service=cast("Any", DisabledDreaming()),
            logger=structlog.stdlib.get_logger("test"),
            runtime_registry=cast("Any", RuntimeRegistry(SuccessfulRuntime())),
            titler=titler,
            trace_recorder=None,
            turn_queue=ConversationTurnQueue(),
        )
    )
    try:
        yield turns, service, titler
    finally:
        await turns.shutdown()
        await database.close()


@test()
async def an_untitled_conversation_is_titled_from_its_first_prompt() -> None:
    """The first user message schedules one auto-titling run."""
    turns, service, titler = await load_fixture(titling_turns_fixture())
    conversation = await service.create_scoped_conversation(
        scope_brief="Plan this year's vegetable garden.",
    )

    ticket = await turns.submit(
        InteractiveTurnRequest(
            conversation_id=conversation.id,
            prompt="When should I start tomato seedlings?",
            request_id=uuid7(),
        ),
        RecordingSink(),
    )
    _ = await turns.wait(ticket.turn_id)
    _ = await asyncio.wait_for(titler.invoked.wait(), timeout=2.0)
    await titler.drain()

    assert_eq(titler.prompts, [conversation.id])
    assert_eq(titler.first_messages, ["When should I start tomato seedlings?"])


@test()
async def turn_submission_and_status_changes_publish_navigation_state() -> None:
    """Sidebar indicators refresh when durable turn lifecycle state changes."""
    turns, service, registry = await load_fixture(conversation_turns_fixture())
    publisher = RecordingPublisher()
    service.event_publisher = publisher
    conversation = await service.fetch_main_conversation()
    blocking_runtime = BlockingRuntime()
    registry.runtime = blocking_runtime

    ticket = await turns.submit(
        InteractiveTurnRequest(
            conversation_id=conversation.id,
            prompt="show work",
            request_id=uuid7(),
        ),
        RecordingSink(),
    )
    _ = await blocking_runtime.started.wait()
    await asyncio.sleep(0.02)
    events_while_running = list(publisher.events)
    blocking_runtime.release.set()
    _ = await turns.wait(ticket.turn_id)
    await asyncio.sleep(0.02)

    assert_true(
        any(
            isinstance(event, InvalidateEvent)
            and "conversations" in event.keys
            and "messages" in event.keys
            for event in events_while_running
        )
    )
    assert_true(
        any(
            isinstance(event, InvalidateEvent)
            and "conversations" in event.keys
            and "messages" in event.keys
            for event in publisher.events[len(events_while_running) :]
        )
    )


@test()
async def scheduled_submission_keeps_exact_content_under_the_scheduled_role() -> None:
    """Pi receives host context while canonical content remains exact non-Evidence."""
    turns, service, registry = await load_fixture(conversation_turns_fixture())
    conversation = await service.fetch_main_conversation()
    trigger_service = TriggerService(
        database=service.database, tracer=trace.get_tracer(__name__)
    )
    trigger = await trigger_service.create(
        OnceTriggerSpec(
            action_kind="prompt",
            fire_at=datetime(2030, 1, 1, tzinfo=UTC),
            payload="summarise my day",
        ),
        logger=structlog.stdlib.get_logger("test"),
        now=datetime(2029, 1, 1, tzinfo=UTC),
        prompt_snapshot=ScheduledPromptSnapshot(
            target_conversation_id=conversation.id,
        ),
    )
    occurrence = (await trigger_service.claim_due(trigger.next_fire_at))[0]

    ticket = await turns.submit(
        ScheduledTurnRequest(
            conversation_id=conversation.id,
            occurrence_id=occurrence.id,
            prompt="summarise my day",
            model_profile=None,
        ),
        RecordingSink(),
    )
    result = await turns.wait(ticket.turn_id)
    messages = await service.fetch_messages(conversation.id)

    assert_eq(result.status, "succeeded")
    assert_eq(messages[0].role, "scheduled")
    assert_eq(messages[0].content, "summarise my day")
    assert_true(
        "Tether scheduled context" in registry.runtime.client.prompt_messages[0]
    )
    assert_true("summarise my day" in registry.runtime.client.prompt_messages[0])


@test()
async def health_submission_keeps_verified_context_distinct_from_user_evidence() -> (
    None
):
    """A Health moment is visible as context while pi receives safety framing."""
    turns, service, registry = await load_fixture(conversation_turns_fixture())
    conversation = await service.fetch_main_conversation()

    ticket = await turns.submit(
        HealthTurnRequest(
            conversation_id=conversation.id,
            moment_id=uuid7(),
            prompt="Primary sleep settled at 07:30.",
        ),
        RecordingSink(),
    )
    result = await turns.wait(ticket.turn_id)
    messages = await service.fetch_messages(conversation.id)

    assert_eq(result.status, "succeeded")
    assert_eq(messages[0].role, "health")
    assert_eq(messages[0].content, "Primary sleep settled at 07:30.")
    assert_true("Tether Health moment" in registry.runtime.client.prompt_messages[0])
    assert_true(
        "Primary sleep settled at 07:30." in registry.runtime.client.prompt_messages[0]
    )


@test()
async def deleted_trigger_cannot_create_a_stale_scheduled_turn() -> None:
    """Occurrence and live-trigger validation shares the turn insertion transaction."""
    turns, service, registry = await load_fixture(conversation_turns_fixture())
    conversation = await service.fetch_main_conversation()
    trigger_service = TriggerService(
        database=service.database, tracer=trace.get_tracer(__name__)
    )
    trigger = await trigger_service.create(
        OnceTriggerSpec(
            action_kind="prompt",
            fire_at=datetime(2030, 1, 1, tzinfo=UTC),
            payload="stale prompt",
        ),
        logger=structlog.stdlib.get_logger("test"),
        now=datetime(2029, 1, 1, tzinfo=UTC),
        prompt_snapshot=ScheduledPromptSnapshot(
            target_conversation_id=conversation.id,
        ),
    )
    occurrence = (await trigger_service.claim_due(trigger.next_fire_at))[0]
    occurrence = await trigger_service.record_running(occurrence)
    _ = await trigger_service.delete(
        trigger,
        logger=structlog.stdlib.get_logger("test"),
        now=trigger.next_fire_at,
    )

    with assert_raises(ConversationTurnConflictError):
        _ = await turns.submit(
            ScheduledTurnRequest(
                conversation_id=conversation.id,
                occurrence_id=occurrence.id,
                prompt=occurrence.payload,
                model_profile=occurrence.model_profile,
            ),
            RecordingSink(),
        )

    assert_eq(registry.runtime.client.commands, [])


@test()
async def repeated_interactive_request_returns_the_existing_turn() -> None:
    """A browser retry cannot create a second execution for one request UUID."""
    turns, service, _ = await load_fixture(conversation_turns_fixture())
    conversation = await service.fetch_main_conversation()
    request = InteractiveTurnRequest(
        conversation_id=conversation.id,
        prompt="hello",
        request_id=uuid4(),
    )

    first = await turns.submit(request, RecordingSink())
    second = await turns.submit(request, RecordingSink())
    _ = await turns.wait(first.turn_id)
    messages = await service.fetch_messages(conversation.id)

    assert_eq(second.turn_id, first.turn_id)
    assert_eq([message.content for message in messages], ["hello", "settled answer"])


@test()
async def duplicate_request_rejects_changed_immutable_input() -> None:
    """One request UUID cannot identify different browser work."""
    turns, service, registry = await load_fixture(conversation_turns_fixture())
    conversation = await service.fetch_main_conversation()
    registry.runtime = BlockingRuntime()
    request_id = uuid4()
    _ = await turns.submit(
        InteractiveTurnRequest(
            conversation_id=conversation.id,
            prompt="first input",
            request_id=request_id,
        ),
        RecordingSink(),
    )

    with assert_raises(ConversationTurnConflictError):
        _ = await turns.submit(
            InteractiveTurnRequest(
                conversation_id=conversation.id,
                prompt="changed input",
                request_id=request_id,
            ),
            RecordingSink(),
        )


@test()
async def duplicate_terminal_request_replays_current_status() -> None:
    """A browser attachment receives the durable ticket and terminal result."""
    turns, service, _ = await load_fixture(conversation_turns_fixture())
    conversation = await service.fetch_main_conversation()
    request = InteractiveTurnRequest(
        conversation_id=conversation.id,
        prompt="replay me",
        request_id=uuid4(),
    )
    first = await turns.submit(request, RecordingSink())
    _ = await turns.wait(first.turn_id)
    duplicate_sink = RecordingSink()

    duplicate = await turns.submit(request, duplicate_sink)

    assert_eq(duplicate.turn_id, first.turn_id)
    assert_true(isinstance(duplicate_sink.frames[0], TurnQueuedFrame))
    terminal = assert_isinstance(duplicate_sink.frames[-1], TurnEndedFrame)
    assert_eq(terminal.status, "succeeded")


@test()
async def archived_conversation_rejects_interactive_submission() -> None:
    """Interactive work cannot enter an archived Conversation."""
    turns, service, _ = await load_fixture(conversation_turns_fixture())
    conversation = await service.create_scoped_conversation(
        display_name="Archive",
        scope_brief="Archived work",
    )
    async with service.database.transaction(mode="immediate") as transaction:
        _ = await transaction.execute(
            update(Conversation)
            .set(Conversation.status.to("archived"))
            .where(Conversation.id.eq(conversation.id))
        )

    with assert_raises(ConversationNotFoundError):
        _ = await turns.submit(
            InteractiveTurnRequest(
                conversation_id=conversation.id,
                prompt="do not run",
                request_id=uuid4(),
            ),
            RecordingSink(),
        )


@test()
async def no_answer_turn_fails_before_its_terminal_frame() -> None:
    """Empty non-tool model output cannot report successful execution."""
    turns, service, registry = await load_fixture(conversation_turns_fixture())
    conversation = await service.fetch_main_conversation()
    registry.runtime = EmptyRuntime()
    sink = RecordingSink()

    ticket = await turns.submit(
        InteractiveTurnRequest(
            conversation_id=conversation.id,
            prompt="answer this",
            request_id=uuid4(),
        ),
        sink,
    )
    outcome = await turns.wait(ticket.turn_id)

    assert_eq(outcome.status, "failed")
    assert_eq(outcome.failure_code, "no_answer")
    terminal = next(frame for frame in sink.frames if isinstance(frame, TurnEndedFrame))
    assert_eq(terminal.status, "failed")
    assert_eq(terminal.failure_code, "no_answer")


@test()
async def equal_creation_timestamps_still_execute_in_durable_fifo_order() -> None:
    """Per-Conversation turn sequence decides order when timestamps tie."""
    turns, service, registry = await load_fixture(conversation_turns_fixture())
    conversation = await service.fetch_main_conversation()
    blocking_runtime = BlockingRuntime()
    registry.runtime = blocking_runtime
    head = await turns.submit(
        InteractiveTurnRequest(
            conversation_id=conversation.id,
            prompt="head",
            request_id=uuid7(),
        ),
        RecordingSink(),
    )
    _ = await blocking_runtime.started.wait()
    second = await turns.submit(
        InteractiveTurnRequest(
            conversation_id=conversation.id,
            prompt="second",
            request_id=uuid7(),
        ),
        RecordingSink(),
    )
    third = await turns.submit(
        InteractiveTurnRequest(
            conversation_id=conversation.id,
            prompt="third",
            request_id=uuid7(),
        ),
        RecordingSink(),
    )
    async with service.database.transaction(mode="immediate") as transaction:
        _ = await transaction.execute(
            update(ConversationTurn)
            .set(ConversationTurn.created_at.to(datetime(2030, 1, 1, tzinfo=UTC)))
            .where(ConversationTurn.id.in_(second.turn_id, third.turn_id))
        )

    blocking_runtime.release.set()
    _ = await turns.wait(head.turn_id)
    _ = await turns.wait(second.turn_id)
    _ = await turns.wait(third.turn_id)
    messages = await service.fetch_messages(conversation.id)

    assert_eq(
        [message.content for message in messages if message.role == "user"],
        ["head", "second", "third"],
    )


@test()
async def cancelling_a_pending_fifo_turn_writes_no_message() -> None:
    """A queued turn can be cancelled without manufacturing transcript Evidence."""
    turns, service, registry = await load_fixture(conversation_turns_fixture())
    conversation = await service.fetch_main_conversation()
    blocking_runtime = BlockingRuntime()
    registry.runtime = blocking_runtime
    first = await turns.submit(
        InteractiveTurnRequest(
            conversation_id=conversation.id,
            prompt="first",
            request_id=uuid7(),
        ),
        RecordingSink(),
    )
    _ = await blocking_runtime.started.wait()
    queued = await turns.submit(
        InteractiveTurnRequest(
            conversation_id=conversation.id,
            prompt="never append",
            request_id=uuid7(),
        ),
        RecordingSink(),
    )

    receipt = await turns.cancel(CancelTurnRequest(turn_id=queued.turn_id))
    messages = await service.fetch_messages(conversation.id)
    blocking_runtime.release.set()
    _ = await turns.wait(first.turn_id)

    assert_eq(receipt.status, "cancelled")
    assert_eq([message.content for message in messages], ["first"])


@test()
async def reconcile_fails_acceptance_uncertain_pending_without_rerunning() -> None:
    """A restart never retries a prompt whose acceptance may have happened."""
    turns, service, registry = await load_fixture(conversation_turns_fixture())
    conversation = await service.fetch_main_conversation()
    async with service.database.transaction(mode="immediate") as transaction:
        uncertain = await transaction.execute(
            insert(
                ConversationTurn(
                    acceptance_started_at=datetime.now(UTC),
                    attempts=1,
                    conversation_id=conversation.id,
                    origin="interactive",
                    prompt_snapshot="possibly accepted",
                    request_id=uuid7(),
                    scope_revision_snapshot=1,
                    status="pending",
                    turn_seq=1,
                )
            ).returning()
        )

    report = await turns.repair(datetime.now(UTC))
    result = await turns.wait(uncertain.id)
    messages = await service.fetch_messages(conversation.id)

    assert_eq(report.acceptance_uncertain_failed, 1)
    assert_eq(result.status, "failed")
    assert_eq(result.failure_code, "acceptance_uncertain")
    assert_eq(messages, [])
    assert_eq(registry.runtime.client.commands, [])


@test()
async def startup_repair_queues_a_terminal_turn_with_durable_user_evidence() -> None:
    """A crash after terminal commit cannot skip the turn's Dreaming queue."""
    turns, service, _ = await load_fixture(conversation_turns_fixture())
    conversation = await service.fetch_main_conversation()
    dreaming = RecordingDreaming()
    object.__setattr__(turns.dependencies, "dreaming_enabled", True)
    object.__setattr__(turns.dependencies, "dreaming_service", dreaming)
    async with service.database.transaction(mode="immediate") as transaction:
        settled = await transaction.execute(
            insert(
                ConversationTurn(
                    completed_at=datetime.now(UTC),
                    conversation_id=conversation.id,
                    origin="interactive",
                    prompt_snapshot="committed before crash",
                    request_id=uuid7(),
                    scope_revision_snapshot=1,
                    status="succeeded",
                    turn_seq=1,
                )
            ).returning()
        )
    _ = await service.append_message(
        MessageDraft(
            content="committed before crash",
            conversation_id=conversation.id,
            role="user",
            turn_id=settled.id,
        )
    )

    _ = await turns.repair(datetime.now(UTC))
    _ = await asyncio.wait_for(dreaming.queued_event.wait(), timeout=0.2)

    assert_eq(dreaming.queued, [conversation.id])


@test()
async def cancelling_a_running_turn_aborts_only_its_runtime_and_keeps_partial_rows() -> (
    None
):
    """Running cancellation preserves its user Message and rotates after stream end."""
    turns, service, registry = await load_fixture(conversation_turns_fixture())
    conversation = await service.fetch_main_conversation()
    blocking_runtime = BlockingRuntime()
    registry.runtime = blocking_runtime
    ticket = await turns.submit(
        InteractiveTurnRequest(
            conversation_id=conversation.id,
            prompt="keep the partial turn",
            request_id=uuid7(),
        ),
        RecordingSink(),
    )
    _ = await blocking_runtime.started.wait()

    receipt = await turns.cancel(
        CancelTurnRequest(
            conversation_id=conversation.id,
            turn_id=ticket.turn_id,
        )
    )
    blocking_runtime.release.set()
    outcome = await turns.wait(ticket.turn_id)
    await asyncio.sleep(0.01)
    messages = await service.fetch_messages(conversation.id)

    assert_eq(receipt.status, "running")
    assert_eq(outcome.status, "cancelled")
    assert_eq(blocking_runtime.client.commands, ["prompt", "abort"])
    assert_eq([message.content for message in messages], ["keep the partial turn"])
    assert_eq(registry.discarded, [conversation.id])


@test()
async def cancellation_while_user_message_delivery_is_blocked_prevents_pi_prompt() -> (
    None
):
    """Cancellation after claim is checked again at the pi write boundary."""
    turns, service, registry = await load_fixture(conversation_turns_fixture())
    conversation = await service.fetch_main_conversation()
    sink = BlockingUserMessageSink()

    ticket = await turns.submit(
        InteractiveTurnRequest(
            conversation_id=conversation.id,
            prompt="do not submit",
            request_id=uuid7(),
        ),
        sink,
    )
    _ = await sink.user_message_seen.wait()
    _ = await turns.cancel(CancelTurnRequest(turn_id=ticket.turn_id))
    sink.release.set()
    outcome = await asyncio.wait_for(turns.wait(ticket.turn_id), timeout=0.5)

    assert_eq(outcome.status, "cancelled")
    assert_eq(registry.runtime.client.commands, ["abort"])


@test()
async def cancellation_between_success_selection_and_terminal_cas_wins() -> None:
    """Terminal selection rechecks cancellation under exact execution ownership."""
    turns, service, _ = await load_fixture(conversation_turns_fixture())
    conversation = await service.fetch_main_conversation()
    turns.cancel_before_success = True

    ticket = await turns.submit(
        InteractiveTurnRequest(
            conversation_id=conversation.id,
            prompt="cancel at settlement",
            request_id=uuid7(),
        ),
        RecordingSink(),
    )
    outcome = await asyncio.wait_for(turns.wait(ticket.turn_id), timeout=0.5)

    assert_eq(outcome.status, "cancelled")


@test()
async def known_preaccept_transients_retry_at_most_twice_then_succeed() -> None:
    """Only the explicit safe failure type receives the bounded retry policy."""
    turns, service, registry = await load_fixture(conversation_turns_fixture())
    conversation = await service.fetch_main_conversation()
    runtime = SuccessfulRuntime()
    runtime.client = TransientClient(transient_failures=2)
    registry.runtime = runtime

    ticket = await turns.submit(
        InteractiveTurnRequest(
            conversation_id=conversation.id,
            prompt="retry safely",
            request_id=uuid7(),
        ),
        RecordingSink(),
    )
    result = await turns.wait(ticket.turn_id)

    assert_eq(result.status, "succeeded")
    assert_eq(runtime.client.commands, ["prompt", "prompt", "prompt"])


@test()
async def timeout_after_user_message_append_queues_dreaming() -> None:
    """Dreaming eligibility comes from durable user Evidence, not normal return."""
    turns, service, registry = await load_fixture(conversation_turns_fixture())
    conversation = await service.fetch_main_conversation()
    dreaming = RecordingDreaming()
    registry.runtime = TimingOutRuntime()
    object.__setattr__(turns.dependencies, "dreaming_enabled", True)
    object.__setattr__(turns.dependencies, "dreaming_service", dreaming)

    ticket = await turns.submit(
        InteractiveTurnRequest(
            conversation_id=conversation.id,
            prompt="time out after append",
            request_id=uuid7(),
        ),
        RecordingSink(),
    )
    outcome = await turns.wait(ticket.turn_id)
    _ = await asyncio.wait_for(dreaming.queued_event.wait(), timeout=0.5)

    assert_eq(outcome.status, "failed")
    assert_eq(dreaming.queued, [conversation.id])


@test()
async def exhausted_preaccept_retry_after_user_message_append_queues_dreaming() -> None:
    """Exhausted safe retries still assimilate the durable initiating Message."""
    turns, service, registry = await load_fixture(conversation_turns_fixture())
    conversation = await service.fetch_main_conversation()
    dreaming = RecordingDreaming()
    runtime = SuccessfulRuntime()
    runtime.client = TransientClient(transient_failures=3)
    registry.runtime = runtime
    object.__setattr__(turns.dependencies, "dreaming_enabled", True)
    object.__setattr__(turns.dependencies, "dreaming_service", dreaming)

    ticket = await turns.submit(
        InteractiveTurnRequest(
            conversation_id=conversation.id,
            prompt="exhaust after append",
            request_id=uuid7(),
        ),
        RecordingSink(),
    )
    outcome = await turns.wait(ticket.turn_id)
    _ = await asyncio.wait_for(dreaming.queued_event.wait(), timeout=0.5)

    assert_eq(outcome.failure_code, "preaccept_retry_exhausted")
    assert_eq(dreaming.queued, [conversation.id])


@test()
async def terminal_status_is_durable_before_the_terminal_frame() -> None:
    """An adapter handling agent_end can already observe settled lifecycle state."""
    turns, service, _ = await load_fixture(conversation_turns_fixture())
    conversation = await service.fetch_main_conversation()
    sink = TerminalObservingSink(turns)

    ticket = await turns.submit(
        InteractiveTurnRequest(
            conversation_id=conversation.id,
            prompt="settle first",
            request_id=uuid7(),
        ),
        sink,
    )
    result = await turns.wait(ticket.turn_id)

    assert_eq(result.status, "succeeded")
    assert_eq(sink.terminal_status, "succeeded")


@test()
async def pending_turn_keeps_its_exact_model_configuration_across_restart() -> None:
    """Restart uses submitted provider/model settings, not a reused profile ID."""
    turns, service, registry = await load_fixture(conversation_turns_fixture())
    conversation = await service.fetch_main_conversation()
    old_model = AgentModelConfig(
        display_name="Stable",
        id="stable",
        model_id="old-model",
        provider="old-provider",
        thinking_level="low",
    )
    service.model_catalog = AgentModelCatalog(
        default_model="stable",
        models=(old_model,),
    )
    blocking_runtime = BlockingRuntime()
    registry.runtime = blocking_runtime
    _ = await turns.submit(
        InteractiveTurnRequest(
            conversation_id=conversation.id,
            prompt="head",
            request_id=uuid7(),
        ),
        RecordingSink(),
    )
    _ = await blocking_runtime.started.wait()
    pending = await turns.submit(
        InteractiveTurnRequest(
            conversation_id=conversation.id,
            prompt="survive restart",
            request_id=uuid7(),
        ),
        RecordingSink(),
    )
    await turns.shutdown(drain_seconds=0)
    service.model_catalog = AgentModelCatalog(
        default_model="stable",
        models=(
            AgentModelConfig(
                display_name="Changed",
                id="stable",
                model_id="new-model",
                provider="new-provider",
                thinking_level="high",
            ),
        ),
    )
    recovered_runtime = SuccessfulRuntime()
    recovered_registry = RuntimeRegistry(recovered_runtime)
    recovered_turns = ConversationTurns(
        ChatTurnDependencies(
            conversation_service=service,
            dreaming_enabled=False,
            dreaming_service=cast("Any", DisabledDreaming()),
            logger=structlog.stdlib.get_logger("test"),
            runtime_registry=cast("Any", recovered_registry),
            trace_recorder=None,
            turn_queue=ConversationTurnQueue(),
        )
    )

    _ = await recovered_turns.repair(datetime.now(UTC))
    assert_eq(recovered_runtime.client.commands, [])
    _ = await recovered_turns.dispatch_recovered()
    outcome = await recovered_turns.wait(pending.turn_id)
    await recovered_turns.shutdown()

    assert_eq(outcome.status, "succeeded")
    assert_eq(recovered_runtime.applied_models, [old_model])


@test()
async def pending_turns_apply_the_scope_snapshot_from_submission() -> None:
    """A scope edit affects later submissions, not a turn already in the FIFO."""
    turns, service, _ = await load_fixture(conversation_turns_fixture())
    conversation = await service.create_scoped_conversation(
        display_name="Garden",
        scope_brief="old scope",
    )
    blocking_runtime = BlockingRuntime()
    registry = SnapshotRuntimeRegistry(blocking_runtime)
    object.__setattr__(turns.dependencies, "runtime_registry", registry)
    head = await turns.submit(
        InteractiveTurnRequest(
            conversation_id=conversation.id,
            prompt="head",
            request_id=uuid7(),
        ),
        RecordingSink(),
    )
    _ = await blocking_runtime.started.wait()
    pending = await turns.submit(
        InteractiveTurnRequest(
            conversation_id=conversation.id,
            prompt="submitted before edit",
            request_id=uuid7(),
        ),
        RecordingSink(),
    )
    _ = await service.update_scoped_conversation(
        conversation.id,
        scope_brief="new scope",
    )
    later = await turns.submit(
        InteractiveTurnRequest(
            conversation_id=conversation.id,
            prompt="submitted after edit",
            request_id=uuid7(),
        ),
        RecordingSink(),
    )

    blocking_runtime.release.set()
    _ = await turns.wait(head.turn_id)
    _ = await turns.wait(pending.turn_id)
    _ = await turns.wait(later.turn_id)

    assert_eq(
        [(brief, revision) for brief, revision, _ in registry.snapshots],
        [("old scope", 1), ("old scope", 1), ("new scope", 2)],
    )
    assert_eq(registry.snapshots[0][2], registry.snapshots[1][2])
    assert_true(registry.snapshots[2][2] != registry.snapshots[1][2])
