"""Behavior tests for the in-process Scheduled-trigger scheduler.

The scheduler is driven by a controlled `Clock` and fake dispatch collaborators,
so fire and retry behaviour is asserted deterministically without sleeping on
real wall-clock ticks. The `TriggerService` underneath is real (in-memory
SQLite), so claim/settle transitions are exercised end to end.
"""

import asyncio
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

import structlog
from opentelemetry import trace
from opentelemetry.trace import Tracer
from pydantic import UUID7
from snekql.sqlite import Config, Database, Fetched, select
from snektest import (
    assert_eq,
    assert_is_none,
    assert_is_not_none,
    assert_raises,
    assert_true,
    fixture,
    load_fixture,
    test,
)

from tether.agent_trace_model import RunKind
from tether.agent_trace_recorder import AgentTraceRecorder
from tether.conversation_store import create_conversation_schema
from tether.conversations import ConversationService
from tether.notification_delivery import (
    PushDeliveryNotifier,
    PushNotification,
    TriggerDispatchResult,
    TriggerNotifier,
)
from tether.pi_errors import PiRuntimeError
from tether.pi_process import PiRuntimeConfig
from tether.scheduler import (
    EphemeralPiConfig,
    EphemeralPiPromptRunner,
    Scheduler,
    SchedulerConfig,
)
from tether.structured_logging import Logger
from tether.system_prompt import TASK_SYSTEM_PROMPT
from tether.tool_runtime import SessionRegistry
from tether.trigger_schedule import DailyTriggerSpec, OnceTriggerSpec
from tether.trigger_store import (
    ScheduledOccurrence,
    ScheduledTrigger,
    create_trigger_schema,
)
from tether.triggers import ScheduledPromptSnapshot, TriggerService

from .pi_runtime_fakes import FakePiRuntime, RecordingSpawner

LOGGER: Logger = structlog.stdlib.get_logger("test.scheduler")
BASE = datetime(2030, 1, 1, 9, 0, tzinfo=UTC)


def noop_tracer() -> Tracer:
    """A tracer that emits nowhere."""
    return trace.NoOpTracerProvider().get_tracer("test.scheduler")


class ManualClock:
    """A clock whose time only moves when a test advances it."""

    def __init__(self, now: datetime) -> None:
        self._now: datetime = now

    def now(self) -> datetime:
        """Return the current frozen instant."""
        return self._now

    def set(self, now: datetime) -> None:
        """Jump the clock to a specific instant."""
        self._now = now


class RecordingNotifier:
    """Captures every delivered message for assertion."""

    def __init__(self) -> None:
        self.delivered: list[tuple[str, str]] = []

    async def deliver(
        self, *, occurrence: ScheduledOccurrence[Fetched], message: str
    ) -> None:
        """Record one delivered message."""
        self.delivered.append((str(occurrence.trigger_id), message))


class FailingNotifier:
    """A notifier that always raises, to exercise the failure path."""

    async def deliver(
        self, *, occurrence: ScheduledOccurrence[Fetched], message: str
    ) -> None:
        """Fail every delivery."""
        _ = (occurrence, message)
        message_text = "delivery exploded"
        raise RuntimeError(message_text)


class ConcurrencyProbeNotifier:
    """Tracks the peak number of concurrently in-flight deliveries."""

    def __init__(self) -> None:
        self.current: int = 0
        self.peak: int = 0
        self.delivered: int = 0

    async def deliver(
        self, *, occurrence: ScheduledOccurrence[Fetched], message: str
    ) -> None:
        """Bump a live counter, yield, then settle, recording the peak."""
        _ = (occurrence, message)
        self.current += 1
        self.peak = max(self.peak, self.current)
        await asyncio.sleep(0.01)
        self.current -= 1
        self.delivered += 1


class RecordingPushSender:
    """Records Web Push messages sent for fired triggers."""

    def __init__(self) -> None:
        self.sent: list[PushNotification] = []

    async def send(self, notification: PushNotification) -> None:
        """Record one outgoing push body."""
        self.sent.append(notification)


class ProfileRunner:
    """Records the profile supplied for an unattended prompt."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []

    async def run(self, prompt: str, model_profile: str | None) -> str:
        """Record one prompt and its pinned profile."""
        self.calls.append((prompt, model_profile))
        return "done"


class StubRunner:
    """A stand-in agent prompt runner returning a canned result."""

    def __init__(self, result: str) -> None:
        self.result: str = result
        self.prompts: list[str] = []

    async def run(self, prompt: str, model_profile: str | None) -> str:
        """Record the prompt and return the canned result."""
        _ = model_profile
        self.prompts.append(prompt)
        return self.result


class FailingRunner(StubRunner):
    """Fail prompt execution stably on every call."""

    async def run(self, prompt: str, model_profile: str | None) -> str:
        self.prompts.append(prompt)
        _ = model_profile
        message = "model rejected prompt"
        raise RuntimeError(message)


class BlockingOccurrenceDispatcher:
    """Hold recovered dispatch until a startup-boundary test releases it."""

    def __init__(self) -> None:
        self.release: asyncio.Event = asyncio.Event()
        self.started: asyncio.Event = asyncio.Event()

    async def dispatch(
        self,
        occurrence: ScheduledOccurrence[Fetched],
    ) -> TriggerDispatchResult:
        _ = occurrence
        self.started.set()
        _ = await self.release.wait()
        return TriggerDispatchResult()

    async def deliver_prompt_push(
        self,
        occurrence: ScheduledOccurrence[Fetched],
        *,
        now: datetime,
    ) -> None:
        _ = occurrence, now


class FakeOccurrenceDispatcher:
    """Resolve occurrences without bypassing Scheduler's public dispatch port."""

    def __init__(
        self,
        *,
        notifier: TriggerNotifier,
        runner: StubRunner | ProfileRunner,
        push_sender: RecordingPushSender | None = None,
    ) -> None:
        self.notifier = notifier
        self.push_sender = push_sender
        self.runner = runner

    async def dispatch(
        self,
        occurrence: ScheduledOccurrence[Fetched],
    ) -> TriggerDispatchResult:
        if occurrence.action_kind == "message":
            await self.notifier.deliver(
                occurrence=occurrence,
                message=occurrence.payload,
            )
            return TriggerDispatchResult()
        answer = await self.runner.run(
            occurrence.payload,
            occurrence.model_profile,
        )
        return TriggerDispatchResult(answer=answer)

    async def deliver_prompt_push(
        self,
        occurrence: ScheduledOccurrence[Fetched],
        *,
        now: datetime,
    ) -> None:
        _ = now
        if self.push_sender is not None and occurrence.answer is not None:
            await self.push_sender.send(PushNotification(body=occurrence.answer))


@fixture
async def scheduler_service() -> AsyncGenerator[TriggerService]:
    """A fresh, isolated trigger database for each scheduler test."""
    db = await Database.initialize(backend=Config(database=":memory:"))
    await create_conversation_schema(db)
    await create_trigger_schema(db)
    _ = await ConversationService(db).fetch_main_conversation()
    yield TriggerService(database=db, tracer=noop_tracer())
    await db.close()


def build_scheduler(
    service: TriggerService,
    *,
    notifier: TriggerNotifier,
    clock: ManualClock,
    runner: StubRunner | None = None,
    config: SchedulerConfig | None = None,
) -> Scheduler:
    """Wire a scheduler over the given collaborators."""
    dispatcher = FakeOccurrenceDispatcher(
        notifier=notifier,
        runner=runner or StubRunner(""),
    )
    return Scheduler(
        service=service,
        dispatcher=dispatcher,
        clock=clock,
        logger=LOGGER,
        config=config,
    )


async def fetch_row(
    service: TriggerService, trigger_id: UUID7
) -> ScheduledTrigger[Fetched] | None:
    """Read one trigger row directly for DB-observable assertions."""
    async with service.database.transaction() as tx:
        return await tx.fetch_one_or_none(
            select(ScheduledTrigger).where(ScheduledTrigger.id.eq(trigger_id))
        )


async def add_due_message(
    service: TriggerService, payload: str
) -> ScheduledTrigger[Fetched]:
    """Create a once message trigger due exactly at BASE."""
    return await service.create(
        OnceTriggerSpec(
            action_kind="message",
            payload=payload,
            fire_at=BASE,
        ),
        now=BASE,
        logger=LOGGER,
    )


@test()
async def tick_fires_a_due_message_trigger_verbatim() -> None:
    """A due fixed-message trigger delivers its payload and then completes."""
    service = await load_fixture(scheduler_service())
    trigger = await add_due_message(service, "call the dentist")
    notifier = RecordingNotifier()
    scheduler = build_scheduler(service, notifier=notifier, clock=ManualClock(BASE))

    claimed = await scheduler.tick()
    await scheduler.drain()

    assert_eq([item.trigger_id for item in claimed], [trigger.id])
    assert_eq(notifier.delivered, [(str(trigger.id), "call the dentist")])
    row = await fetch_row(service, trigger.id)
    assert_is_not_none(row)
    assert_eq(row.status if row else None, "completed")


@test()
async def tick_claims_each_trigger_before_dispatch() -> None:
    """A due trigger is stamped claimed before its dispatch task settles it."""
    service = await load_fixture(scheduler_service())
    trigger = await add_due_message(service, "x")
    scheduler = build_scheduler(
        service, notifier=RecordingNotifier(), clock=ManualClock(BASE)
    )

    _ = await scheduler.tick()
    # Observed between claim and drain: the row is already claimed.
    row = await fetch_row(service, trigger.id)
    await scheduler.drain()

    assert_is_not_none(row)
    assert_is_not_none(row.claimed_at if row else None)


@test()
async def tick_runs_an_agent_prompt_through_the_prompt_runner() -> None:
    """An agent-prompt trigger submits its payload to the chat runner."""
    service = await load_fixture(scheduler_service())
    main = await ConversationService(service.database).fetch_main_conversation()
    _ = await service.create(
        OnceTriggerSpec(
            action_kind="prompt",
            payload="summarise my day",
            fire_at=BASE,
        ),
        now=BASE,
        logger=LOGGER,
        prompt_snapshot=ScheduledPromptSnapshot(
            target_conversation_id=main.id,
        ),
    )
    notifier = RecordingNotifier()
    runner = StubRunner("you have 3 meetings")
    scheduler = build_scheduler(
        service, notifier=notifier, clock=ManualClock(BASE), runner=runner
    )

    _ = await scheduler.tick()
    await scheduler.drain()

    assert_eq(runner.prompts, ["summarise my day"])


@test()
async def prompt_failure_is_terminal_without_scheduler_backoff() -> None:
    """A failed one-off prompt settles once instead of entering broad retries."""
    service = await load_fixture(scheduler_service())
    main = await ConversationService(service.database).fetch_main_conversation()
    trigger = await service.create(
        OnceTriggerSpec(
            action_kind="prompt",
            payload="fail once",
            fire_at=BASE,
        ),
        now=BASE,
        logger=LOGGER,
        prompt_snapshot=ScheduledPromptSnapshot(
            target_conversation_id=main.id,
        ),
    )
    runner = FailingRunner("")
    scheduler = build_scheduler(
        service,
        notifier=RecordingNotifier(),
        clock=ManualClock(BASE),
        runner=runner,
    )

    claimed = await scheduler.tick()
    await scheduler.drain()
    current = await service.fetch(trigger.id)
    occurrence = await service.fetch_latest_occurrence(trigger.id)
    if occurrence is None:
        raise AssertionError("claimed occurrence disappeared")

    assert_eq(claimed[0].id, occurrence.id)
    assert_eq(occurrence.status, "failed")
    assert_eq(current.status, "failed")
    assert_is_none(current.next_attempt_at)
    assert_eq(runner.prompts, ["fail once"])


@test()
async def recurring_prompt_failure_advances_immediately() -> None:
    """A failed recurring prompt skips to its next firing after one submission."""
    service = await load_fixture(scheduler_service())
    main = await ConversationService(service.database).fetch_main_conversation()
    trigger = await service.create(
        DailyTriggerSpec(
            action_kind="prompt",
            payload="fail today",
            timezone="UTC",
            time_of_day="09:00",
        ),
        now=BASE - timedelta(hours=1),
        logger=LOGGER,
        prompt_snapshot=ScheduledPromptSnapshot(
            target_conversation_id=main.id,
        ),
    )
    runner = FailingRunner("")
    scheduler = build_scheduler(
        service,
        notifier=RecordingNotifier(),
        clock=ManualClock(BASE),
        runner=runner,
    )

    _ = await scheduler.tick()
    await scheduler.drain()
    current = await service.fetch(trigger.id)

    assert_eq(current.status, "active")
    assert_eq(current.next_fire_at, BASE + timedelta(days=1))
    assert_is_none(current.next_attempt_at)
    assert_eq(runner.prompts, ["fail today"])


@test()
async def recurring_prompt_dispatch_uses_its_pinned_profile() -> None:
    """Dispatch passes the recurring prompt's stored profile to its runner."""
    service = await load_fixture(scheduler_service())
    runner = ProfileRunner()
    main = await ConversationService(service.database).fetch_main_conversation()
    _ = await service.create(
        DailyTriggerSpec(
            action_kind="prompt",
            payload="summarise my day",
            timezone="UTC",
            time_of_day="09:00",
        ),
        now=BASE,
        logger=LOGGER,
        prompt_snapshot=ScheduledPromptSnapshot(
            model_profile="high-effort",
            target_conversation_id=main.id,
        ),
    )
    occurrence = (await service.claim_due(BASE + timedelta(days=1)))[0]
    dispatcher = FakeOccurrenceDispatcher(
        notifier=RecordingNotifier(),
        runner=runner,
    )

    _ = await dispatcher.dispatch(occurrence)

    assert_eq(runner.calls, [("summarise my day", "high-effort")])


@test()
async def an_agent_prompt_answer_is_sent_over_web_push() -> None:
    """The chat answer retains closed-tab Web Push delivery."""
    service = await load_fixture(scheduler_service())
    main = await ConversationService(service.database).fetch_main_conversation()
    _ = await service.create(
        OnceTriggerSpec(
            action_kind="prompt",
            payload="summarise my day",
            fire_at=BASE,
        ),
        now=BASE,
        logger=LOGGER,
        prompt_snapshot=ScheduledPromptSnapshot(
            target_conversation_id=main.id,
        ),
    )
    push_sender = RecordingPushSender()
    scheduler = Scheduler(
        service=service,
        dispatcher=FakeOccurrenceDispatcher(
            notifier=RecordingNotifier(),
            runner=StubRunner("you have 3 meetings"),
            push_sender=push_sender,
        ),
        clock=ManualClock(BASE),
        logger=LOGGER,
    )

    _ = await scheduler.tick()
    await scheduler.drain()

    assert_eq(
        push_sender.sent,
        [PushNotification(body="you have 3 meetings")],
    )


@test()
async def push_delivery_notifier_keeps_existing_delivery_and_sends_web_push() -> None:
    """Closed-tab delivery is added without removing the existing notifier path."""
    service = await load_fixture(scheduler_service())
    trigger = await add_due_message(service, "call the dentist")
    browser_notifier = RecordingNotifier()
    push_sender = RecordingPushSender()
    scheduler = build_scheduler(
        service,
        notifier=PushDeliveryNotifier(browser_notifier, push_sender),
        clock=ManualClock(BASE),
    )

    _ = await scheduler.tick()
    await scheduler.drain()

    assert_eq(browser_notifier.delivered, [(str(trigger.id), "call the dentist")])
    assert_eq(push_sender.sent, [PushNotification(body="call the dentist")])


@test()
async def tick_backs_off_a_failed_dispatch_then_retries() -> None:
    """A failed dispatch backs the occurrence off, then a later tick retries it."""
    service = await load_fixture(scheduler_service())
    trigger = await add_due_message(service, "x")
    clock = ManualClock(BASE)
    scheduler = build_scheduler(
        service,
        notifier=FailingNotifier(),
        clock=clock,
        config=SchedulerConfig(backoff_base=timedelta(seconds=30)),
    )

    _ = await scheduler.tick()
    await scheduler.drain()

    row = await fetch_row(service, trigger.id)
    occurrence = await service.fetch_latest_occurrence(trigger.id)
    assert_is_not_none(row)
    assert_is_not_none(occurrence)
    assert_eq(occurrence.dispatch_attempts if occurrence else None, 1)
    assert_eq(row.status if row else None, "active")
    assert_is_not_none(row.claimed_at if row else None)
    assert_eq(
        occurrence.next_attempt_at if occurrence else None,
        BASE + timedelta(seconds=30),
    )

    # Before the backoff elapses, nothing is due.
    clock.set(BASE + timedelta(seconds=15))
    assert_eq(await scheduler.tick(), [])
    # Once it elapses, the occurrence is retried.
    clock.set(BASE + timedelta(seconds=30))
    retried = await scheduler.tick()
    await scheduler.drain()
    assert_eq([item.trigger_id for item in retried], [trigger.id])


@test()
async def startup_repair_never_launches_or_awaits_recovered_dispatch() -> None:
    """Durable startup repair remains bounded before request serving begins."""
    service = await load_fixture(scheduler_service())
    trigger = await add_due_message(service, "recover me")
    occurrence = (await service.claim_due(trigger.next_fire_at))[0]
    _ = await service.record_running(occurrence)
    dispatcher = BlockingOccurrenceDispatcher()
    scheduler = Scheduler(
        service=service,
        dispatcher=dispatcher,
        clock=ManualClock(BASE),
        logger=LOGGER,
    )

    await asyncio.wait_for(scheduler.repair(), timeout=0.2)

    assert_true(not dispatcher.started.is_set())
    await scheduler.dispatch_recovered()
    _ = await asyncio.wait_for(dispatcher.started.wait(), timeout=0.2)
    dispatcher.release.set()
    await scheduler.drain()


@test()
async def concurrency_cap_bounds_in_flight_dispatches() -> None:
    """The concurrency cap limits how many dispatches run at once (backpressure)."""
    service = await load_fixture(scheduler_service())
    for index in range(5):
        _ = await add_due_message(service, f"reminder {index}")
    notifier = ConcurrencyProbeNotifier()
    scheduler = build_scheduler(
        service,
        notifier=notifier,
        clock=ManualClock(BASE),
        config=SchedulerConfig(concurrency=2),
    )

    _ = await scheduler.tick()
    await scheduler.drain()

    assert_eq(notifier.delivered, 5)
    assert_true(notifier.peak <= 2)


@fixture
async def ephemeral_session_root() -> AsyncGenerator[Path]:
    """Temporary directory for the ephemeral runner's pi session files."""
    with TemporaryDirectory() as directory:
        yield Path(directory)


def build_ephemeral_runner(
    session_root: Path,
    recorder: AgentTraceRecorder,
    spawner: RecordingSpawner,
    run_kind: RunKind = "scheduled",
) -> EphemeralPiPromptRunner:
    """Wire an ephemeral runner over an injected, type-compatible spawn seam.

    `EphemeralPiPromptRunner` takes its `PiSpawner` seam through the
    constructor (mirroring `ConversationRuntimeRegistry`'s `spawn` parameter),
    so the fake never needs a `setattr` module-global monkeypatch.
    """
    return EphemeralPiPromptRunner(
        EphemeralPiConfig(
            session_registry=SessionRegistry(),
            session_root=session_root,
            tool_base_url="http://127.0.0.1:0",
            tool_secret="test-secret",
            trace_recorder=recorder,
            run_kind=run_kind,
        ),
        spawn=spawner,
    )


@test()
async def a_scheduled_run_spawns_pi_with_the_task_system_prompt() -> None:
    """A scheduled-trigger run replaces pi's default prompt with the task one."""
    session_root = await load_fixture(ephemeral_session_root())
    spawner = RecordingSpawner(runtime=FakePiRuntime(accepts_prompt=False))
    runner = build_ephemeral_runner(session_root, AgentTraceRecorder(), spawner)

    with assert_raises(PiRuntimeError):
        _ = await runner.run("summarise the day")

    assert_eq(len(spawner.configs), 1)
    spawn_config = spawner.configs[0]
    assert isinstance(spawn_config, PiRuntimeConfig)
    assert_eq(spawn_config.system_prompt, TASK_SYSTEM_PROMPT)


@test()
async def a_recall_run_spawns_pi_with_the_task_system_prompt() -> None:
    """A Recall model step also carries the short unattended-task prompt."""
    session_root = await load_fixture(ephemeral_session_root())
    spawner = RecordingSpawner(runtime=FakePiRuntime(accepts_prompt=False))
    runner = build_ephemeral_runner(
        session_root, AgentTraceRecorder(), spawner, run_kind="recall"
    )

    with assert_raises(PiRuntimeError):
        _ = await runner.run("grade this answer")

    assert_eq(len(spawner.configs), 1)
    spawn_config = spawner.configs[0]
    assert isinstance(spawn_config, PiRuntimeConfig)
    assert_eq(spawn_config.system_prompt, TASK_SYSTEM_PROMPT)


@test()
async def a_rejected_ephemeral_prompt_ends_its_run_as_error_and_shuts_pi_down() -> None:
    """A drive failure stamps the run `error` and still shuts the runtime down."""
    session_root = await load_fixture(ephemeral_session_root())
    recorder = AgentTraceRecorder()
    runtime = FakePiRuntime(accepts_prompt=False)
    runner = build_ephemeral_runner(
        session_root, recorder, RecordingSpawner(runtime=runtime)
    )

    with assert_raises(PiRuntimeError):
        _ = await runner.run("summarise the day")

    runs = recorder.recent_runs(limit=1)
    assert_eq(len(runs), 1)
    assert_eq(runs[0].termination, "error")
    assert_eq(runs[0].error, "agent prompt was rejected by pi")
    assert_true(runtime.shutdown_count > 0)


@test()
async def an_ephemeral_prompt_gone_silent_ends_its_run_as_timeout() -> None:
    """A pi that stops emitting events ends the run as `timeout`, not `error`."""
    session_root = await load_fixture(ephemeral_session_root())
    recorder = AgentTraceRecorder()
    runtime = FakePiRuntime(accepts_prompt=True, goes_silent=True)
    runner = build_ephemeral_runner(
        session_root, recorder, RecordingSpawner(runtime=runtime)
    )

    with assert_raises(TimeoutError):
        _ = await runner.run("summarise the day")

    runs = recorder.recent_runs(limit=1)
    assert_eq(len(runs), 1)
    assert_eq(runs[0].termination, "timeout")
    assert_true(runtime.shutdown_count > 0)
