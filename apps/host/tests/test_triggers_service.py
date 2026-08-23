"""Behavior tests for the Scheduled trigger service layer.

These drive the *service* seam directly against a real (in-memory) SQLite
database — no HTTP, no agent, no scheduler loop — asserting on observable
behavior: created rows, the materialised UTC fire times (including across a DST
boundary), the optimistic-concurrency guards, and the scheduler-facing claim /
success / failure transitions.
"""

from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

import structlog
from opentelemetry import trace
from opentelemetry.trace import Tracer
from snekql.sqlite import Config, Database, Fetched, insert, select, update
from snektest import (
    assert_eq,
    assert_is_none,
    assert_is_not_none,
    assert_raises,
    fixture,
    load_fixture,
    test,
)

from tether.conversation_store import (
    Conversation,
    ConversationTurn,
    create_conversation_schema,
)
from tether.conversations import ConversationService
from tether.events import HubEvent, InvalidateEvent
from tether.model_selection import AgentModelCatalog, AgentModelConfig
from tether.structured_logging import Logger
from tether.trigger_schedule import (
    DailyTriggerSpec,
    InvalidTriggerSpecError,
    OnceTriggerSpec,
    TriggerSpec,
    WeeklyTriggerSpec,
)
from tether.trigger_store import (
    ScheduledOccurrence,
    ScheduledTrigger,
    create_trigger_schema,
)
from tether.triggers import (
    ScheduledPromptSnapshot,
    TriggerConflictError,
    TriggerNotFoundError,
    TriggerService,
)

LOGGER: Logger = structlog.stdlib.get_logger("test.triggers_service")


class RecordingPublisher:
    """Captures every event a service publishes, for assertion in tests."""

    def __init__(self) -> None:
        self.events: list[HubEvent] = []

    async def publish(self, event: HubEvent) -> None:
        """Record a published event."""
        self.events.append(event)


@fixture
async def recording_service() -> AsyncGenerator[
    tuple[TriggerService, RecordingPublisher]
]:
    """A trigger service wired to a recording publisher for invalidate assertions."""
    db = await Database.initialize(backend=Config(database=":memory:"))
    await create_trigger_schema(db)
    publisher = RecordingPublisher()
    yield (
        TriggerService(database=db, tracer=noop_tracer(), event_publisher=publisher),
        publisher,
    )
    await db.close()


def noop_tracer() -> Tracer:
    """A tracer that emits nowhere, for tests that don't assert on spans."""
    return trace.NoOpTracerProvider().get_tracer("test.triggers_service")


@fixture
async def trigger_service() -> AsyncGenerator[TriggerService]:
    """A fresh, isolated trigger database for each test."""
    db = await Database.initialize(backend=Config(database=":memory:"))
    await create_conversation_schema(db)
    await create_trigger_schema(db)
    _ = await ConversationService(db).fetch_main_conversation()
    yield TriggerService(database=db, tracer=noop_tracer())
    await db.close()


async def make_trigger(
    service: TriggerService, spec: TriggerSpec, *, now: datetime
) -> ScheduledTrigger[Fetched]:
    """Create a trigger from a spec, supplying the test logger."""
    return await service.create(spec, now=now, logger=LOGGER)


async def edit_trigger(
    service: TriggerService,
    trigger: ScheduledTrigger[Fetched],
    spec: TriggerSpec,
    *,
    now: datetime,
) -> ScheduledTrigger[Fetched]:
    """Update a trigger from a spec, supplying the test logger."""
    return await service.update(trigger, spec, now=now, logger=LOGGER)


def once(payload: str, fire_at: datetime) -> TriggerSpec:
    """A once message spec firing at `fire_at`."""
    return OnceTriggerSpec(
        action_kind="message",
        payload=payload,
        fire_at=fire_at,
    )


async def fetch_row(
    service: TriggerService, trigger: ScheduledTrigger[Fetched]
) -> ScheduledTrigger[Fetched] | None:
    """Fetch a trigger row directly for DB-observable assertions."""
    async with service.database.transaction() as tx:
        return await tx.fetch_one_or_none(
            select(ScheduledTrigger).where(ScheduledTrigger.id.eq(trigger.id))
        )


# --- create: time-spec validation and materialisation ---


@test()
async def create_once_stores_the_absolute_instant_as_utc() -> None:
    """A once trigger fires at exactly its supplied instant, normalised to UTC."""
    service = await load_fixture(trigger_service())
    fire_at = datetime(2030, 1, 1, 15, 0, tzinfo=UTC)

    trigger = await make_trigger(
        service,
        once("call the dentist", fire_at),
        now=datetime(2030, 1, 1, 9, 0, tzinfo=UTC),
    )

    assert_eq(trigger.status, "active")
    assert_eq(trigger.next_fire_at, fire_at)
    assert_is_none(trigger.wall_time)


@test()
async def create_daily_materialises_the_next_local_occurrence() -> None:
    """A daily trigger stores its wall time and the next UTC occurrence."""
    service = await load_fixture(trigger_service())

    trigger = await make_trigger(
        service,
        DailyTriggerSpec(
            action_kind="message",
            payload="stand up",
            timezone="UTC",
            time_of_day="09:00",
        ),
        now=datetime(2030, 3, 10, 10, 0, tzinfo=UTC),
    )

    assert_eq(trigger.wall_time, "09:00")
    assert_eq(trigger.next_fire_at, datetime(2030, 3, 11, 9, 0, tzinfo=UTC))


@test()
async def daily_recurrence_survives_a_dst_spring_forward() -> None:
    """A 09:00 local daily fire keeps its wall-clock hour across DST shifts.

    America/New_York is UTC-4 in summer (EDT) and UTC-5 in winter (EST); a 09:00
    local fire therefore lands at 13:00 UTC in summer and 14:00 UTC in winter.
    Materialising per occurrence (rather than adding 24h to an instant) is what
    preserves the wall-clock hour.
    """
    service = await load_fixture(trigger_service())
    daily = DailyTriggerSpec(
        action_kind="message",
        payload="lunch",
        timezone="America/New_York",
        time_of_day="09:00",
    )

    summer = await make_trigger(
        service, daily, now=datetime(2026, 7, 1, 0, 0, tzinfo=UTC)
    )
    winter = await make_trigger(
        service, daily, now=datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    )

    assert_eq(summer.next_fire_at, datetime(2026, 7, 1, 13, 0, tzinfo=UTC))
    assert_eq(winter.next_fire_at, datetime(2026, 1, 1, 14, 0, tzinfo=UTC))


@test()
async def create_weekly_lands_on_the_next_matching_weekday() -> None:
    """A weekly trigger advances to the next occurrence of its weekday."""
    service = await load_fixture(trigger_service())

    # 2030-01-01 is a Tuesday (weekday 1); ask for Friday (weekday 4).
    trigger = await make_trigger(
        service,
        WeeklyTriggerSpec(
            action_kind="message",
            payload="weekly review",
            timezone="UTC",
            time_of_day="08:30",
            weekday=4,
        ),
        now=datetime(2030, 1, 1, 12, 0, tzinfo=UTC),
    )

    assert_eq(trigger.next_fire_at, datetime(2030, 1, 4, 8, 30, tzinfo=UTC))
    assert_eq(trigger.weekday, 4)


@test()
async def create_rejects_a_blank_payload() -> None:
    """A whitespace-only payload is a malformed spec, not a stored row."""
    service = await load_fixture(trigger_service())

    with assert_raises(InvalidTriggerSpecError):
        _ = await make_trigger(
            service,
            once("   ", datetime(2030, 1, 1, tzinfo=UTC)),
            now=datetime(2030, 1, 1, tzinfo=UTC),
        )


@test()
async def create_rejects_a_once_trigger_in_the_past() -> None:
    """A once trigger whose instant has already passed is a malformed spec."""
    service = await load_fixture(trigger_service())

    with assert_raises(InvalidTriggerSpecError):
        _ = await make_trigger(
            service,
            once("x", datetime(2030, 1, 1, 8, 0, tzinfo=UTC)),
            now=datetime(2030, 1, 1, 9, 0, tzinfo=UTC),
        )


@test()
async def create_allows_a_once_trigger_at_the_current_instant() -> None:
    """A once trigger scheduled for exactly now is due immediately, not rejected."""
    service = await load_fixture(trigger_service())
    now = datetime(2030, 1, 1, 9, 0, tzinfo=UTC)

    trigger = await make_trigger(service, once("x", now), now=now)

    assert_eq(trigger.status, "active")
    assert_eq(trigger.next_fire_at, now)


@test()
async def update_rejects_a_once_trigger_in_the_past() -> None:
    """Editing a trigger to an instant that has passed is rejected too."""
    service = await load_fixture(trigger_service())
    trigger = await make_trigger(
        service,
        once("x", datetime(2030, 1, 1, 15, 0, tzinfo=UTC)),
        now=datetime(2030, 1, 1, 9, 0, tzinfo=UTC),
    )

    with assert_raises(InvalidTriggerSpecError):
        _ = await edit_trigger(
            service,
            trigger,
            once("x", datetime(2030, 1, 1, 8, 0, tzinfo=UTC)),
            now=datetime(2030, 1, 1, 9, 0, tzinfo=UTC),
        )


# --- list / update / delete ---


@test()
async def list_orders_by_next_fire_and_hides_deleted() -> None:
    """Listing returns live triggers soonest-first and omits deleted ones."""
    service = await load_fixture(trigger_service())
    now = datetime(2030, 1, 1, tzinfo=UTC)
    later = await make_trigger(
        service, once("later", datetime(2030, 1, 3, tzinfo=UTC)), now=now
    )
    sooner = await make_trigger(
        service, once("sooner", datetime(2030, 1, 2, tzinfo=UTC)), now=now
    )
    gone = await make_trigger(
        service, once("gone", datetime(2030, 1, 4, tzinfo=UTC)), now=now
    )
    _ = await service.delete(gone, now=now, logger=LOGGER)

    listed = await service.list_triggers(logger=LOGGER)

    assert_eq([t.id for t in listed], [sooner.id, later.id])


@test()
async def list_caps_rows_at_the_limit() -> None:
    """`limit` bounds the listing to the soonest-firing triggers."""
    service = await load_fixture(trigger_service())
    now = datetime(2030, 1, 1, tzinfo=UTC)
    first = await make_trigger(
        service, once("a", datetime(2030, 1, 2, tzinfo=UTC)), now=now
    )
    second = await make_trigger(
        service, once("b", datetime(2030, 1, 3, tzinfo=UTC)), now=now
    )
    _ = await make_trigger(
        service, once("c", datetime(2030, 1, 4, tzinfo=UTC)), now=now
    )

    listed = await service.list_triggers(limit=2, logger=LOGGER)

    assert_eq([t.id for t in listed], [first.id, second.id])


@test()
async def update_replaces_the_definition_and_rearms() -> None:
    """Updating re-materialises the schedule and clears scheduler state."""
    service = await load_fixture(trigger_service())
    now = datetime(2030, 1, 1, 12, 0, tzinfo=UTC)
    trigger = await make_trigger(
        service, once("old", datetime(2030, 1, 2, tzinfo=UTC)), now=now
    )

    main = await ConversationService(service.database).fetch_main_conversation()
    updated = await service.update(
        trigger,
        DailyTriggerSpec(
            action_kind="prompt",
            payload="summarise my day",
            timezone="UTC",
            time_of_day="07:00",
        ),
        now=now,
        logger=LOGGER,
        prompt_snapshot=ScheduledPromptSnapshot(
            target_conversation_id=main.id,
        ),
    )

    assert_eq(updated.recurrence, "daily")
    assert_eq(updated.action_kind, "prompt")
    assert_eq(updated.payload, "summarise my day")
    assert_eq(updated.next_fire_at, datetime(2030, 1, 2, 7, 0, tzinfo=UTC))
    assert_eq(updated.version, trigger.version + 1)


@test()
async def update_with_a_stale_version_conflicts() -> None:
    """An update against an out-of-date version is rejected."""
    service = await load_fixture(trigger_service())
    now = datetime(2030, 1, 1, tzinfo=UTC)
    trigger = await make_trigger(
        service, once("x", datetime(2030, 1, 2, tzinfo=UTC)), now=now
    )
    _ = await edit_trigger(
        service, trigger, once("x", datetime(2030, 1, 3, tzinfo=UTC)), now=now
    )

    with assert_raises(TriggerConflictError):
        _ = await edit_trigger(
            service, trigger, once("x", datetime(2030, 1, 4, tzinfo=UTC)), now=now
        )


@test()
async def delete_is_convergent_on_an_already_deleted_trigger() -> None:
    """Re-deleting a deleted trigger is a no-op, and it leaves the list."""
    service = await load_fixture(trigger_service())
    now = datetime(2030, 1, 1, tzinfo=UTC)
    trigger = await make_trigger(
        service, once("x", datetime(2030, 1, 2, tzinfo=UTC)), now=now
    )

    deleted = await service.delete(trigger, now=now, logger=LOGGER)
    again = await service.delete(deleted, now=now, logger=LOGGER)

    assert_is_not_none(deleted.deleted_at)
    assert_eq(again.id, trigger.id)
    assert_eq(await service.list_triggers(logger=LOGGER), [])


@test()
async def fetch_missing_trigger_raises() -> None:
    """Fetching a deleted trigger surfaces absence, not a deleted row."""
    service = await load_fixture(trigger_service())
    now = datetime(2030, 1, 1, tzinfo=UTC)
    trigger = await make_trigger(
        service, once("x", datetime(2030, 1, 2, tzinfo=UTC)), now=now
    )
    _ = await service.delete(trigger, now=now, logger=LOGGER)

    with assert_raises(TriggerNotFoundError):
        _ = await service.fetch(trigger.id)


@test()
async def prompt_creation_requires_and_stores_an_active_target() -> None:
    """Prompt definitions cannot exist without an explicit active Conversation."""
    service = await load_fixture(trigger_service())
    main = await ConversationService(service.database).fetch_main_conversation()
    spec = OnceTriggerSpec(
        action_kind="prompt",
        payload="summarise",
        fire_at=datetime(2030, 1, 2, tzinfo=UTC),
    )

    with assert_raises(InvalidTriggerSpecError):
        _ = await service.create(
            spec,
            now=datetime(2030, 1, 1, tzinfo=UTC),
            logger=LOGGER,
        )
    trigger = await service.create(
        spec,
        now=datetime(2030, 1, 1, tzinfo=UTC),
        logger=LOGGER,
        prompt_snapshot=ScheduledPromptSnapshot(
            target_conversation_id=main.id,
        ),
    )

    assert_eq(trigger.target_conversation_id, main.id)


@test()
async def one_off_prompt_snapshots_the_target_profile_when_claimed() -> None:
    """One-off prompts stay unpinned until their immutable occurrence exists."""
    service = await load_fixture(trigger_service())
    main = await ConversationService(service.database).fetch_main_conversation()
    async with service.database.transaction(mode="immediate") as transaction:
        _ = await transaction.execute(
            update(Conversation)
            .set(Conversation.selected_model.to("careful"))
            .where(Conversation.id.eq(main.id))
        )
    trigger = await service.create(
        OnceTriggerSpec(
            action_kind="prompt",
            payload="summarise",
            fire_at=datetime(2030, 1, 2, tzinfo=UTC),
        ),
        now=datetime(2030, 1, 1, tzinfo=UTC),
        logger=LOGGER,
        prompt_snapshot=ScheduledPromptSnapshot(
            target_conversation_id=main.id,
        ),
    )

    occurrence = (await service.claim_due(trigger.next_fire_at))[0]

    assert_is_none(trigger.model_profile)
    assert_eq(occurrence.model_profile, "careful")


@test()
async def one_off_prompt_snapshots_the_effective_default_model_configuration() -> None:
    """A default-profile occurrence survives restart configuration changes exactly."""
    service = await load_fixture(trigger_service())
    old_model = AgentModelConfig(
        display_name="Old default",
        id="default",
        model_id="old-model",
        provider="old-provider",
        thinking_level="low",
    )
    service.model_catalog = AgentModelCatalog(
        default_model="default",
        models=(old_model,),
    )
    main = await ConversationService(service.database).fetch_main_conversation()
    trigger = await service.create(
        OnceTriggerSpec(
            action_kind="prompt",
            payload="summarise",
            fire_at=datetime(2030, 1, 2, tzinfo=UTC),
        ),
        now=datetime(2030, 1, 1, tzinfo=UTC),
        logger=LOGGER,
        prompt_snapshot=ScheduledPromptSnapshot(target_conversation_id=main.id),
    )

    occurrence = (await service.claim_due(trigger.next_fire_at))[0]
    service.model_catalog = AgentModelCatalog(
        default_model="default",
        models=(
            AgentModelConfig(
                display_name="New default",
                id="default",
                model_id="new-model",
                provider="new-provider",
                thinking_level="high",
            ),
        ),
    )

    assert_eq(occurrence.model_profile, "default")
    assert_eq(occurrence.model_id_snapshot, "old-model")
    assert_eq(occurrence.model_provider_snapshot, "old-provider")
    assert_eq(occurrence.model_thinking_level_snapshot, "low")


@test()
async def recurring_edit_does_not_mutate_an_already_claimed_occurrence() -> None:
    """Definition edits change later firings while the claimed snapshot stays exact."""
    service = await load_fixture(trigger_service())
    trigger = await make_trigger(
        service,
        DailyTriggerSpec(
            action_kind="message",
            payload="old",
            timezone="UTC",
            time_of_day="09:00",
        ),
        now=datetime(2030, 1, 1, 8, tzinfo=UTC),
    )
    occurrence = (await service.claim_due(trigger.next_fire_at))[0]

    updated = await edit_trigger(
        service,
        trigger,
        DailyTriggerSpec(
            action_kind="message",
            payload="new",
            timezone="UTC",
            time_of_day="10:00",
        ),
        now=datetime(2030, 1, 1, 9, tzinfo=UTC),
    )

    persisted = await service.fetch_latest_occurrence(trigger.id)
    if persisted is None:
        raise AssertionError("claimed occurrence disappeared")
    assert_eq(updated.payload, "new")
    assert_eq(occurrence.payload, "old")
    assert_eq(persisted.payload, "old")


@test()
async def one_off_edit_is_blocked_after_its_occurrence_is_claimed() -> None:
    """A one-off cannot be redefined while its sole firing is pending."""
    service = await load_fixture(trigger_service())
    trigger = await make_trigger(
        service,
        once("old", datetime(2030, 1, 2, tzinfo=UTC)),
        now=datetime(2030, 1, 1, tzinfo=UTC),
    )
    _ = await service.claim_due(trigger.next_fire_at)

    with assert_raises(TriggerConflictError):
        _ = await edit_trigger(
            service,
            trigger,
            once("new", datetime(2030, 1, 3, tzinfo=UTC)),
            now=datetime(2030, 1, 2, tzinfo=UTC),
        )


@test()
async def delete_cancels_a_pending_occurrence_and_its_pending_turn() -> None:
    """Deleting future work removes queued execution without touching accepted work."""
    service = await load_fixture(trigger_service())
    main = await ConversationService(service.database).fetch_main_conversation()
    trigger = await service.create(
        OnceTriggerSpec(
            action_kind="prompt",
            payload="x",
            fire_at=datetime(2030, 1, 2, tzinfo=UTC),
        ),
        now=datetime(2030, 1, 1, tzinfo=UTC),
        logger=LOGGER,
        prompt_snapshot=ScheduledPromptSnapshot(target_conversation_id=main.id),
    )
    occurrence = (await service.claim_due(trigger.next_fire_at))[0]
    async with service.database.transaction(mode="immediate") as transaction:
        _ = await transaction.execute(
            insert(
                ConversationTurn(
                    conversation_id=main.id,
                    origin="scheduled",
                    prompt_snapshot="x",
                    scheduled_occurrence_id=occurrence.id,
                    status="pending",
                    turn_seq=1,
                )
            ).returning()
        )

    _ = await service.delete(trigger, now=trigger.next_fire_at, logger=LOGGER)
    occurrence = await service.fetch_latest_occurrence(trigger.id)
    if occurrence is None:
        raise AssertionError("claimed occurrence disappeared")
    linked_turn = await service.fetch_occurrence_turn(occurrence.id)

    assert_eq(occurrence.status, "cancelled")
    assert_eq(linked_turn.status if linked_turn is not None else None, "cancelled")


@test()
async def delete_preserves_a_running_fixed_message_delivery() -> None:
    """Deletion cannot relabel a fixed delivery whose side effects may have begun."""
    service = await load_fixture(trigger_service())
    trigger = await make_trigger(
        service,
        once("deliver me", datetime(2030, 1, 2, tzinfo=UTC)),
        now=datetime(2030, 1, 1, tzinfo=UTC),
    )
    occurrence = (await service.claim_due(trigger.next_fire_at))[0]
    occurrence = await service.record_running(occurrence)

    _ = await service.delete(trigger, now=trigger.next_fire_at, logger=LOGGER)
    persisted = await service.fetch_occurrence(occurrence.id)

    assert_eq(persisted.status, "running")


@test()
async def startup_repair_cancels_deleted_prompt_work_without_a_linked_turn() -> None:
    """A crash between occurrence claim and turn insertion leaves no stale prompt."""
    service = await load_fixture(trigger_service())
    main = await ConversationService(service.database).fetch_main_conversation()
    trigger = await service.create(
        OnceTriggerSpec(
            action_kind="prompt",
            payload="orphaned prompt",
            fire_at=datetime(2030, 1, 2, tzinfo=UTC),
        ),
        now=datetime(2030, 1, 1, tzinfo=UTC),
        logger=LOGGER,
        prompt_snapshot=ScheduledPromptSnapshot(target_conversation_id=main.id),
    )
    occurrence = (await service.claim_due(trigger.next_fire_at))[0]
    occurrence = await service.record_running(occurrence)
    async with service.database.transaction(mode="immediate") as transaction:
        _ = await transaction.execute(
            update(ScheduledTrigger)
            .set(ScheduledTrigger.deleted_at.to(trigger.next_fire_at))
            .where(ScheduledTrigger.id.eq(trigger.id))
        )

    await service.repair_occurrences(now=trigger.next_fire_at)
    persisted = await service.fetch_occurrence(occurrence.id)

    assert_eq(persisted.status, "cancelled")


# --- scheduler-facing: claim / success / failure ---


@test()
async def claiming_a_due_trigger_atomically_creates_its_immutable_occurrence() -> None:
    """The claimed unit is a firing snapshot, not the mutable trigger row."""
    service = await load_fixture(trigger_service())
    trigger = await make_trigger(
        service,
        once("x", datetime(2030, 1, 1, 9, 0, tzinfo=UTC)),
        now=datetime(2030, 1, 1, 8, 0, tzinfo=UTC),
    )
    tick = datetime(2030, 1, 1, 9, 0, tzinfo=UTC)

    first = await service.claim_due(tick)
    second = await service.claim_due(tick)

    assert_eq(len(first), 1)
    assert_eq(first[0].trigger_id, trigger.id)
    assert_eq(first[0].trigger_version, trigger.version)
    assert_eq(first[0].intended_fire_at, tick)
    assert_eq(first[0].payload, "x")
    assert_eq(first[0].status, "pending")
    assert_eq(second, [])
    async with service.database.transaction() as transaction:
        stored = await transaction.fetch_all(select(ScheduledOccurrence).all())
    assert_eq([item.id for item in stored], [first[0].id])


@test()
async def claim_due_skips_a_future_trigger() -> None:
    """A trigger whose fire time has not arrived is not claimed."""
    service = await load_fixture(trigger_service())
    _ = await make_trigger(
        service,
        once("x", datetime(2030, 1, 1, 9, 0, tzinfo=UTC)),
        now=datetime(2030, 1, 1, 8, 0, tzinfo=UTC),
    )

    claimed = await service.claim_due(datetime(2030, 1, 1, 8, 30, tzinfo=UTC))

    assert_eq(claimed, [])


@test()
async def record_success_completes_a_once_trigger() -> None:
    """A successfully fired once trigger becomes completed and unclaims."""
    service = await load_fixture(trigger_service())
    now = datetime(2030, 1, 1, 9, 0, tzinfo=UTC)
    _ = await make_trigger(
        service, once("x", now), now=datetime(2030, 1, 1, 8, 0, tzinfo=UTC)
    )
    claimed = (await service.claim_due(now))[0]

    occurrence = await service.record_success(claimed, now=now)
    trigger = await service.fetch(occurrence.trigger_id)

    assert_eq(occurrence.status, "succeeded")
    assert_eq(trigger.status, "completed")
    assert_is_none(trigger.claimed_at)


@test()
async def record_success_reschedules_a_daily_trigger() -> None:
    """A successfully fired daily trigger re-arms onto tomorrow and stays active."""
    service = await load_fixture(trigger_service())
    _ = await make_trigger(
        service,
        DailyTriggerSpec(
            action_kind="message",
            payload="x",
            timezone="UTC",
            time_of_day="09:00",
        ),
        now=datetime(2030, 1, 1, 8, 0, tzinfo=UTC),
    )
    fire = datetime(2030, 1, 1, 9, 0, tzinfo=UTC)
    claimed = (await service.claim_due(fire))[0]

    occurrence = await service.record_success(claimed, now=fire)
    trigger = await service.fetch(occurrence.trigger_id)

    assert_eq(occurrence.status, "succeeded")
    assert_eq(trigger.status, "active")
    assert_eq(trigger.next_fire_at, datetime(2030, 1, 2, 9, 0, tzinfo=UTC))
    assert_is_none(trigger.claimed_at)


@test()
async def record_failure_backs_off_then_stays_due() -> None:
    """A failed dispatch sets a retry time and the trigger is due again then."""
    service = await load_fixture(trigger_service())
    now = datetime(2030, 1, 1, 9, 0, tzinfo=UTC)
    trigger = await make_trigger(
        service, once("x", now), now=datetime(2030, 1, 1, 8, 0, tzinfo=UTC)
    )
    claimed = (await service.claim_due(now))[0]

    occurrence = await service.record_failure(
        claimed,
        now=now,
        error="boom",
        backoff_base=timedelta(seconds=30),
    )
    trigger = await service.fetch(trigger.id)

    assert_eq(occurrence.status, "pending")
    assert_eq(occurrence.dispatch_attempts, 1)
    assert_eq(occurrence.failure_summary, "boom")
    assert_is_not_none(trigger.claimed_at)
    assert_eq(occurrence.next_attempt_at, now + timedelta(seconds=30))
    assert_eq(await service.claim_due(now + timedelta(seconds=15)), [])
    retried = await service.claim_due(now + timedelta(seconds=30))
    assert_eq([item.id for item in retried], [occurrence.id])


@test()
async def fixed_message_retry_reclaims_its_occurrence_after_definition_edit() -> None:
    """An edit cannot strand or replace the immutable failed delivery occurrence."""
    service = await load_fixture(trigger_service())
    fire = datetime(2030, 1, 1, 9, 0, tzinfo=UTC)
    trigger = await make_trigger(
        service,
        DailyTriggerSpec(
            action_kind="message",
            payload="old message",
            timezone="UTC",
            time_of_day="09:00",
        ),
        now=fire - timedelta(hours=1),
    )
    occurrence = (await service.claim_due(fire))[0]
    occurrence = await service.record_failure(
        occurrence,
        now=fire,
        error="delivery failed",
        backoff_base=timedelta(seconds=30),
    )
    _ = await edit_trigger(
        service,
        trigger,
        DailyTriggerSpec(
            action_kind="message",
            payload="new message",
            timezone="UTC",
            time_of_day="10:00",
        ),
        now=fire,
    )

    retried = await service.claim_due(fire + timedelta(seconds=30))

    assert_eq([item.id for item in retried], [occurrence.id])
    assert_eq(retried[0].payload, "old message")
    assert_eq(retried[0].trigger_version, occurrence.trigger_version)


@test()
async def record_failure_exhausts_a_once_trigger_to_failed() -> None:
    """Once retries are spent, a once trigger lands in the failed state."""
    service = await load_fixture(trigger_service())
    now = datetime(2030, 1, 1, 9, 0, tzinfo=UTC)
    _ = await make_trigger(
        service, once("x", now), now=datetime(2030, 1, 1, 8, 0, tzinfo=UTC)
    )
    settled = (await service.claim_due(now))[0]

    for _attempt in range(3):
        settled = await service.record_failure(
            settled, now=now, error="boom", max_attempts=3
        )
        if settled.status == "pending":
            settled = (await service.claim_due(now + timedelta(hours=1)))[0]

    assert_eq(settled.status, "failed")


@test()
async def record_failure_exhausts_a_daily_trigger_to_next_occurrence() -> None:
    """A recurring trigger that exhausts retries skips ahead, not wedges."""
    service = await load_fixture(trigger_service())
    trigger = await make_trigger(
        service,
        DailyTriggerSpec(
            action_kind="message",
            payload="x",
            timezone="UTC",
            time_of_day="09:00",
        ),
        now=datetime(2030, 1, 1, 8, 0, tzinfo=UTC),
    )
    fire = datetime(2030, 1, 1, 9, 0, tzinfo=UTC)
    settled = (await service.claim_due(fire))[0]

    for _attempt in range(2):
        settled = await service.record_failure(
            settled, now=fire, error="boom", max_attempts=2
        )
        if settled.status == "pending":
            assert_is_not_none(settled.next_attempt_at)
            if settled.next_attempt_at is not None:
                settled = (await service.claim_due(settled.next_attempt_at))[0]

    trigger = await service.fetch(trigger.id)
    assert_eq(settled.status, "failed")
    assert_eq(trigger.status, "active")
    assert_eq(trigger.attempts, 0)
    assert_eq(trigger.next_fire_at, datetime(2030, 1, 2, 9, 0, tzinfo=UTC))


# --- occurrence lifecycle publishes trigger invalidation ---


@test()
async def claiming_due_occurrence_publishes_a_triggers_invalidate() -> None:
    """Open reminder views see a firing as soon as its snapshot is claimed."""
    service, publisher = await load_fixture(recording_service())
    now = datetime(2030, 1, 1, 9, 0, tzinfo=UTC)
    _ = await make_trigger(
        service, once("x", now), now=datetime(2030, 1, 1, 8, 0, tzinfo=UTC)
    )
    publisher.events.clear()

    _ = await service.claim_due(now)

    assert_eq(publisher.events, [InvalidateEvent(keys=["triggers"])])


@test()
async def recording_occurrence_running_publishes_a_triggers_invalidate() -> None:
    """Open reminder views see the running transition before settlement."""
    service, publisher = await load_fixture(recording_service())
    now = datetime(2030, 1, 1, 9, 0, tzinfo=UTC)
    _ = await make_trigger(
        service, once("x", now), now=datetime(2030, 1, 1, 8, 0, tzinfo=UTC)
    )
    occurrence = (await service.claim_due(now))[0]
    publisher.events.clear()

    _ = await service.record_running(occurrence)

    assert_eq(publisher.events, [InvalidateEvent(keys=["triggers"])])


@test()
async def record_success_publishes_a_triggers_invalidate() -> None:
    """Settling a fired trigger tells connected browsers to refetch the list.

    Without this the client keeps the stale (active, version=1) row: the list
    never flips to `completed` and a Delete sends the pre-fire version, 409ing.
    """
    service, publisher = await load_fixture(recording_service())
    now = datetime(2030, 1, 1, 9, 0, tzinfo=UTC)
    _ = await make_trigger(
        service, once("x", now), now=datetime(2030, 1, 1, 8, 0, tzinfo=UTC)
    )
    claimed = (await service.claim_due(now))[0]
    publisher.events.clear()

    _ = await service.record_success(claimed, now=now)

    assert_eq(publisher.events, [InvalidateEvent(keys=["triggers"])])


@test()
async def record_failure_publishes_a_triggers_invalidate() -> None:
    """A failed dispatch also bumps the version, so the client must refetch."""
    service, publisher = await load_fixture(recording_service())
    now = datetime(2030, 1, 1, 9, 0, tzinfo=UTC)
    _ = await make_trigger(
        service, once("x", now), now=datetime(2030, 1, 1, 8, 0, tzinfo=UTC)
    )
    claimed = (await service.claim_due(now))[0]
    publisher.events.clear()

    _ = await service.record_failure(claimed, now=now, error="boom")

    assert_eq(publisher.events, [InvalidateEvent(keys=["triggers"])])
