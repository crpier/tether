"""Health moment reconciliation and briefing delivery behavior."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from uuid import UUID, uuid7

from snekql.sqlite import Config, Database, insert
from snektest import assert_eq, fixture, load_fixture, test

from tether.conversation_model import MessageDraft
from tether.conversation_store import create_conversation_schema
from tether.conversation_turns import (
    HealthTurnRequest,
    TurnResult,
    TurnTicket,
)
from tether.conversations import ConversationService
from tether.health_connect import (
    HcExerciseEpisodeSummary,
    HcSleepEpisodeSummary,
    HealthMomentDispatcher,
    HealthMomentObservation,
    HealthMomentObservationQuery,
    HealthMomentService,
    create_health_connect_schema,
    create_health_moment_schema,
)
from tether.health_connect.persistence import HcSleepSession, HcSleepStage
from tether.model_selection import AgentModelCatalog
from tether.notification_delivery import PushNotification


class FixedObservationSource:
    """Return settled observations without coupling tests to telemetry tables."""

    def __init__(self, observations: list[HealthMomentObservation]) -> None:
        self.observations = observations

    async def fetch_recent(self, *, now: datetime) -> list[HealthMomentObservation]:
        _ = now
        return self.observations


class SettledHealthTurns:
    """Settle one submitted Health turn with a canonical assistant Message."""

    def __init__(self, conversations: ConversationService) -> None:
        self.conversations = conversations
        self.submissions: list[HealthTurnRequest] = []
        self.turn_id = uuid7()

    async def submit(self, request: HealthTurnRequest, sink: object) -> TurnTicket:
        _ = sink
        self.submissions.append(request)
        _ = await self.conversations.append_message(
            MessageDraft(
                content="You recovered well. Keep today's strength session light.",
                conversation_id=request.conversation_id,
                role="assistant",
                turn_id=self.turn_id,
            )
        )
        return TurnTicket(
            conversation_id=request.conversation_id,
            status="succeeded",
            turn_id=self.turn_id,
        )

    async def wait(self, turn_id: UUID) -> TurnResult:
        return TurnResult(
            failure_code=None,
            failure_summary=None,
            status="succeeded",
            turn_id=turn_id,
        )


class RecordingPushSender:
    """Collect full browser notifications at the delivery interface."""

    def __init__(self) -> None:
        self.notifications: list[PushNotification] = []

    async def send(self, notification: PushNotification) -> None:
        self.notifications.append(notification)


@fixture
async def moment_database() -> AsyncGenerator[Database]:
    database = await Database.initialize(backend=Config(database=":memory:"))
    await create_conversation_schema(database)
    await create_health_moment_schema(database)
    try:
        yield database
    finally:
        await database.close()


@fixture
async def telemetry_database() -> AsyncGenerator[Database]:
    database = await Database.initialize(backend=Config(database=":memory:"))
    await create_health_connect_schema(database)
    try:
        yield database
    finally:
        await database.close()


def primary_sleep_observation() -> HealthMomentObservation:
    """Build one independently specified settled primary sleep."""
    return HealthMomentObservation(
        evidence_uri="tether://health-connect/sleep/sleep-1@v4",
        kind="primary_sleep",
        observation="Primary sleep ended at 2026-08-27T07:30:00+03:00.",
        observed_at=datetime(2026, 8, 27, 4, 30, tzinfo=UTC),
        source_record_uid="sleep-1",
        source_version_id=4,
    )


@test()
async def settled_observation_becomes_a_durable_health_moment() -> None:
    """Reconciliation returns the canonical moment available to later workers."""
    database = await load_fixture(moment_database())
    service = HealthMomentService(
        database=database,
        observations=FixedObservationSource([primary_sleep_observation()]),
    )

    report = await service.reconcile(now=datetime(2026, 8, 27, 5, tzinfo=UTC))
    moments = await service.list_recent(limit=10)

    assert_eq(report.created, 1)
    assert_eq([moment.kind for moment in moments], ["primary_sleep"])
    assert_eq(moments[0].source_version_id, 4)
    assert_eq(moments[0].status, "pending")


@test()
async def settled_exercise_summary_becomes_an_exercise_health_moment() -> None:
    """The production observation query admits a recent settled workout."""
    database = await load_fixture(moment_database())
    telemetry = await load_fixture(telemetry_database())
    start = datetime(2026, 8, 27, 3, tzinfo=UTC)
    end = datetime(2026, 8, 27, 4, tzinfo=UTC)
    async with telemetry.transaction(mode="immediate") as transaction:
        _ = await transaction.execute(
            insert(
                HcExerciseEpisodeSummary(
                    duration_minutes=60.0,
                    end_time=int(end.timestamp() * 1_000),
                    exercise_type=70,
                    lap_count=0,
                    origin_id=None,
                    payload_hash="exercise-hash",
                    processor_version=1,
                    record_uid="exercise-1",
                    segment_count=0,
                    start_time=int(start.timestamp() * 1_000),
                    title="Home weights",
                    total_lap_meters=None,
                    version_id=7,
                )
            )
        )
    service = HealthMomentService(
        database=database,
        observations=HealthMomentObservationQuery(telemetry),
    )

    report = await service.reconcile(now=datetime(2026, 8, 27, 5, tzinfo=UTC))
    moments = await service.list_recent(limit=10)

    assert_eq(report.created, 1)
    assert_eq(moments[0].kind, "exercise")
    assert_eq(moments[0].evidence_uri, "tether://health-connect/exercise/exercise-1@v7")
    assert_eq(moments[0].evidence_uri in moments[0].observation, True)
    assert_eq("strength training" in moments[0].observation, True)


@test()
async def settled_primary_sleep_summary_becomes_a_health_moment() -> None:
    """Only a materialized primary sleep is eligible for proactive briefing."""
    database = await load_fixture(moment_database())
    telemetry = await load_fixture(telemetry_database())
    start = datetime(2026, 8, 26, 20, tzinfo=UTC)
    end = datetime(2026, 8, 27, 4, tzinfo=UTC)
    start_millis = int(start.timestamp() * 1_000)
    end_millis = int(end.timestamp() * 1_000)
    async with telemetry.transaction(mode="immediate") as transaction:
        _ = await transaction.execute(
            insert(
                HcSleepSession(
                    client_record_id=None,
                    client_record_version=None,
                    end_time=end_millis,
                    end_zone_offset_seconds=10_800,
                    is_deleted=False,
                    modified_at=end_millis,
                    notes=None,
                    origin_id=None,
                    payload_hash="sleep-hash",
                    received_at=end_millis,
                    recording_method=2,
                    record_uid="sleep-primary",
                    request_id="sleep-request",
                    start_time=start_millis,
                    start_zone_offset_seconds=10_800,
                    title="Night sleep",
                )
            ).returning()
        )
        _ = await transaction.execute(
            insert(
                HcSleepStage(
                    end_time=end_millis,
                    stage=4,
                    stage_index=0,
                    start_time=start_millis,
                    version_id=1,
                )
            )
        )
        _ = await transaction.execute(
            insert(
                HcSleepEpisodeSummary(
                    duration_minutes=480.0,
                    end_time=end_millis,
                    minutes_awake=0.0,
                    minutes_awake_in_bed=0.0,
                    minutes_deep=0.0,
                    minutes_light=480.0,
                    minutes_other=0.0,
                    minutes_out_of_bed=0.0,
                    minutes_rem=0.0,
                    minutes_sleeping=0.0,
                    origin_id=None,
                    payload_hash="sleep-hash",
                    processor_version=1,
                    record_uid="sleep-primary",
                    start_time=start_millis,
                    title="Night sleep",
                    version_id=1,
                )
            )
        )
    service = HealthMomentService(
        database=database,
        observations=HealthMomentObservationQuery(telemetry),
    )

    report = await service.reconcile(now=datetime(2026, 8, 27, 5, tzinfo=UTC))
    moments = await service.list_recent(limit=10)

    assert_eq(report.created, 1)
    assert_eq(moments[0].kind, "primary_sleep")
    assert_eq(
        moments[0].evidence_uri,
        "tether://health-connect/sleep/sleep-primary@v1",
    )


@test()
async def settled_briefing_is_pushed_once_with_its_full_answer() -> None:
    """Repeated dispatch reuses the durable turn and delivered full preview."""
    database = await load_fixture(moment_database())
    moment_service = HealthMomentService(
        database=database,
        observations=FixedObservationSource([primary_sleep_observation()]),
    )
    _ = await moment_service.reconcile(now=datetime(2026, 8, 27, 5, tzinfo=UTC))
    conversations = ConversationService(
        database=database,
        model_catalog=AgentModelCatalog(default_model=None, models=()),
    )
    turns = SettledHealthTurns(conversations)
    sender = RecordingPushSender()
    dispatcher = HealthMomentDispatcher(
        conversation_service=conversations,
        conversation_turns=turns,
        database=database,
        push_sender=sender,
    )

    first = await dispatcher.dispatch_pending(
        now=datetime(2026, 8, 27, 5, 1, tzinfo=UTC)
    )
    second = await dispatcher.dispatch_pending(
        now=datetime(2026, 8, 27, 5, 2, tzinfo=UTC)
    )
    moments = await moment_service.list_recent(limit=10)

    assert_eq(first.briefed, 1)
    assert_eq(first.pushed, 1)
    assert_eq(second.briefed, 0)
    assert_eq(second.pushed, 0)
    assert_eq(len(turns.submissions), 1)
    assert_eq(
        [notification.body for notification in sender.notifications],
        ["You recovered well. Keep today's strength session light."],
    )
    assert_eq(moments[0].status, "succeeded")


@test()
async def repeated_observation_reuses_its_existing_health_moment() -> None:
    """A retry or source correction cannot create another briefing identity."""
    database = await load_fixture(moment_database())
    source = FixedObservationSource([primary_sleep_observation()])
    service = HealthMomentService(database=database, observations=source)
    _ = await service.reconcile(now=datetime(2026, 8, 27, 5, tzinfo=UTC))
    source.observations = [
        HealthMomentObservation(
            evidence_uri="tether://health-connect/sleep/sleep-1@v5",
            kind="primary_sleep",
            observation="Corrected primary sleep observation.",
            observed_at=datetime(2026, 8, 27, 4, 30, tzinfo=UTC),
            source_record_uid="sleep-1",
            source_version_id=5,
        )
    ]

    report = await service.reconcile(now=datetime(2026, 8, 27, 6, tzinfo=UTC))
    moments = await service.list_recent(limit=10)

    assert_eq(report.created, 0)
    assert_eq(len(moments), 1)
    assert_eq(moments[0].source_version_id, 4)
