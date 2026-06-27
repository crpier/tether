"""Behavior tests for the in-process Scheduled-trigger scheduler.

The scheduler is driven by a controlled `Clock` and fake dispatch collaborators,
so fire and retry behaviour is asserted deterministically without sleeping on
real wall-clock ticks. The `TriggerService` underneath is real (in-memory
SQLite), so claim/settle transitions are exercised end to end.
"""

import asyncio
from datetime import UTC, datetime, timedelta

import structlog
from opentelemetry import trace
from opentelemetry.trace import Tracer
from pydantic import UUID7
from snekql.sqlite import Config, Database, Fetched, select
from snektest import (
    AsyncFixture,
    assert_eq,
    assert_is_none,
    assert_is_not_none,
    assert_true,
    load_fixture,
    test,
)

from tether.logging import Logger
from tether.scheduler import (
    Scheduler,
    SchedulerConfig,
    TriggerDispatcher,
    TriggerNotifier,
)
from tether.triggers import (
    ScheduledTrigger,
    TriggerService,
    TriggerSpec,
    create_trigger_schema,
)

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
        self, *, trigger: ScheduledTrigger[Fetched], message: str
    ) -> None:
        """Record one delivered message."""
        self.delivered.append((str(trigger.id), message))


class FailingNotifier:
    """A notifier that always raises, to exercise the failure path."""

    async def deliver(
        self, *, trigger: ScheduledTrigger[Fetched], message: str
    ) -> None:
        """Fail every delivery."""
        _ = (trigger, message)
        message_text = "delivery exploded"
        raise RuntimeError(message_text)


class ConcurrencyProbeNotifier:
    """Tracks the peak number of concurrently in-flight deliveries."""

    def __init__(self) -> None:
        self.current: int = 0
        self.peak: int = 0
        self.delivered: int = 0

    async def deliver(
        self, *, trigger: ScheduledTrigger[Fetched], message: str
    ) -> None:
        """Bump a live counter, yield, then settle, recording the peak."""
        _ = (trigger, message)
        self.current += 1
        self.peak = max(self.peak, self.current)
        await asyncio.sleep(0.01)
        self.current -= 1
        self.delivered += 1


class StubRunner:
    """A stand-in agent prompt runner returning a canned result."""

    def __init__(self, result: str) -> None:
        self.result: str = result
        self.prompts: list[str] = []

    async def run(self, prompt: str) -> str:
        """Record the prompt and return the canned result."""
        self.prompts.append(prompt)
        return self.result


async def scheduler_service() -> AsyncFixture[TriggerService]:
    """A fresh, isolated trigger database for each scheduler test."""
    db = await Database.initialize(backend=Config(database=":memory:"))
    await create_trigger_schema(db)
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
    dispatcher = TriggerDispatcher(
        notifier=notifier,
        agent_runner=runner or StubRunner(""),
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
        TriggerSpec(
            recurrence="once",
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

    assert_eq([t.id for t in claimed], [trigger.id])
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
async def tick_runs_an_agent_prompt_and_delivers_the_result() -> None:
    """An agent-prompt trigger runs through the runner; its output is delivered."""
    service = await load_fixture(scheduler_service())
    trigger = await service.create(
        TriggerSpec(
            recurrence="once",
            action_kind="prompt",
            payload="summarise my day",
            fire_at=BASE,
        ),
        now=BASE,
        logger=LOGGER,
    )
    notifier = RecordingNotifier()
    runner = StubRunner("you have 3 meetings")
    scheduler = build_scheduler(
        service, notifier=notifier, clock=ManualClock(BASE), runner=runner
    )

    _ = await scheduler.tick()
    await scheduler.drain()

    assert_eq(runner.prompts, ["summarise my day"])
    assert_eq(notifier.delivered, [(str(trigger.id), "you have 3 meetings")])


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
    assert_is_not_none(row)
    assert_eq(row.attempts if row else None, 1)
    assert_eq(row.status if row else None, "active")
    assert_is_none(row.claimed_at if row else None)
    assert_eq(row.next_attempt_at if row else None, BASE + timedelta(seconds=30))

    # Before the backoff elapses, nothing is due.
    clock.set(BASE + timedelta(seconds=15))
    assert_eq(await scheduler.tick(), [])
    # Once it elapses, the occurrence is retried.
    clock.set(BASE + timedelta(seconds=30))
    retried = await scheduler.tick()
    await scheduler.drain()
    assert_eq([t.id for t in retried], [trigger.id])


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
