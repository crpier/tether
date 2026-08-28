"""Durable Health moments reconciled from settled Health observations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal, Protocol, cast
from uuid import UUID, uuid7

import structlog
from pydantic import UUID7
from snekql.sqlite import (
    CurrentTimestamp,
    Database,
    Fetched,
    Integer,
    Model,
    Pending,
    Text,
    UtcDatetime,
    insert,
    select,
    update,
)

from tether.chat_turn import ChatFrameSink
from tether.conversation_store import Conversation, Message
from tether.conversation_turns import (
    HealthTurnRequest,
    SilentChatFrameSink,
    TurnResult,
    TurnTicket,
)
from tether.health_connect.episodes import HealthEpisodeSummarizer
from tether.health_connect.insights import HealthConnectInsightQuery
from tether.health_connect.persistence import (
    HcExerciseEpisodeSummary,
    HcSleepEpisodeSummary,
)
from tether.health_connect.plans import PlannedExerciseMiss
from tether.health_connect.telemetry_values import (
    datetime_from_millis,
    render_exercise_type,
)
from tether.notification_delivery import PushNotification

_logger = structlog.stdlib.get_logger("tether.health_moments")

HealthMomentKind = Literal["exercise", "missed_exercise", "primary_sleep"]
HealthMomentStatus = Literal["pending", "running", "succeeded", "failed"]
HealthMomentPushStatus = Literal["pending", "delivered"]


class HealthMoment[S = Pending](Model[S, "HealthMoment[Fetched]"]):
    """One stable reason to run a context-aware Health briefing."""

    id: HealthMoment.GenCol[UUID7] = Text(primary_key=True, default_factory=uuid7)
    answer_message_id: HealthMoment.Col[UUID | None] = Text(default=None, nullable=True)
    completed_at: HealthMoment.Col[UtcDatetime | None] = Text(
        default=None, nullable=True
    )
    created_at: HealthMoment.GenCol[UtcDatetime] = Text(default=CurrentTimestamp)
    evidence_uri: HealthMoment.Col[str] = Text(nullable=False)
    failure_summary: HealthMoment.Col[str | None] = Text(default=None, nullable=True)
    kind: HealthMoment.Col[HealthMomentKind] = Text(nullable=False)
    observation: HealthMoment.Col[str] = Text(nullable=False)
    observed_at: HealthMoment.Col[UtcDatetime] = Text(nullable=False)
    source_record_uid: HealthMoment.Col[str] = Text(nullable=False)
    push_delivered_at: HealthMoment.Col[UtcDatetime | None] = Text(
        default=None, nullable=True
    )
    push_status: HealthMoment.Col[HealthMomentPushStatus] = Text(
        default=cast("HealthMomentPushStatus", "pending")
    )
    source_version_id: HealthMoment.Col[int] = Integer(nullable=False)
    status: HealthMoment.Col[HealthMomentStatus] = Text(
        default=cast("HealthMomentStatus", "pending")
    )
    turn_id: HealthMoment.Col[UUID | None] = Text(default=None, nullable=True)


@dataclass(frozen=True, slots=True)
class HealthMomentObservation:
    """One settled deterministic observation eligible for a briefing."""

    evidence_uri: str
    kind: HealthMomentKind
    observation: str
    observed_at: datetime
    source_record_uid: str
    source_version_id: int


@dataclass(frozen=True, slots=True)
class HealthMomentRead:
    """Durable Health moment state exposed without persistence details."""

    answer_message_id: UUID | None
    created_at: datetime
    evidence_uri: str
    failure_summary: str | None
    id: UUID
    kind: HealthMomentKind
    observation: str
    observed_at: datetime
    push_status: HealthMomentPushStatus
    source_record_uid: str
    source_version_id: int
    status: HealthMomentStatus
    turn_id: UUID | None


@dataclass(frozen=True, slots=True)
class HealthMomentReconcileReport:
    """New Health moment count produced by one reconciliation pass."""

    created: int


class HealthMomentObservationSource(Protocol):
    """Read settled Health observations eligible at one instant."""

    async def fetch_recent(self, *, now: datetime) -> list[HealthMomentObservation]: ...


class PlannedExerciseSource(Protocol):
    """Converge planned occurrences and return current settled misses."""

    async def reconcile(self, *, now: datetime) -> list[PlannedExerciseMiss]: ...


class HealthConversationPort(Protocol):
    """Conversation reads needed after one Health turn settles."""

    async def fetch_main_conversation(self) -> Conversation[Fetched]: ...

    async def fetch_messages(
        self,
        conversation_id: UUID,
        *,
        limit: int | None = None,
        before_seq: int | None = None,
        turn_id: UUID | None = None,
    ) -> list[Message[Fetched]]: ...


class HealthConversationTurnsPort(Protocol):
    """Durable Conversation execution needed by proactive Health briefings."""

    async def submit(
        self,
        request: HealthTurnRequest,
        sink: ChatFrameSink,
    ) -> TurnTicket: ...

    async def wait(self, turn_id: UUID) -> TurnResult: ...


class HealthPushSender(Protocol):
    """Structured Web Push delivery needed by Health briefings."""

    async def send(self, notification: PushNotification) -> None: ...


@dataclass(frozen=True, slots=True)
class HealthMomentDispatchReport:
    """Briefing and full-preview push counts from one dispatch pass."""

    briefed: int
    pushed: int


def _read(moment: HealthMoment[Fetched]) -> HealthMomentRead:
    """Project persistence into the Health moment interface."""
    return HealthMomentRead(
        answer_message_id=(
            None
            if moment.answer_message_id is None
            else UUID(str(moment.answer_message_id))
        ),
        created_at=moment.created_at,
        evidence_uri=moment.evidence_uri,
        failure_summary=moment.failure_summary,
        id=UUID(str(moment.id)),
        kind=moment.kind,
        observation=moment.observation,
        observed_at=moment.observed_at,
        push_status=moment.push_status,
        source_record_uid=moment.source_record_uid,
        source_version_id=moment.source_version_id,
        status=moment.status,
        turn_id=None if moment.turn_id is None else UUID(str(moment.turn_id)),
    )


@dataclass(frozen=True, slots=True)
class HealthMomentObservationQuery:
    """Read recent settled episode summaries eligible for proactive briefing."""

    database: Database
    lookback: timedelta = timedelta(hours=24)
    planned_exercise: PlannedExerciseSource | None = None

    async def fetch_recent(self, *, now: datetime) -> list[HealthMomentObservation]:
        """Return a bounded window so initial deployment cannot replay history."""
        cutoff_millis = int((now - self.lookback).timestamp() * 1_000)
        now_millis = int(now.timestamp() * 1_000)
        async with self.database.transaction() as transaction:
            exercise_rows = await transaction.fetch_all(
                select(HcExerciseEpisodeSummary)
                .where(HcExerciseEpisodeSummary.end_time.gte(cutoff_millis))
                .where(HcExerciseEpisodeSummary.end_time.lte(now_millis))
                .order_by(HcExerciseEpisodeSummary.end_time.asc())
            )
            sleep_rows = await transaction.fetch_all(
                select(HcSleepEpisodeSummary)
                .where(HcSleepEpisodeSummary.end_time.gte(cutoff_millis))
                .where(HcSleepEpisodeSummary.end_time.lte(now_millis))
                .order_by(HcSleepEpisodeSummary.end_time.asc())
            )
        observations: list[HealthMomentObservation] = []
        for exercise in exercise_rows:
            observed_at = datetime_from_millis(exercise.end_time)
            start_time = datetime_from_millis(exercise.start_time)
            if observed_at is None or start_time is None:
                continue
            exercise_type = render_exercise_type(exercise.exercise_type)
            evidence_uri = (
                "tether://health-connect/exercise/"
                f"{exercise.record_uid}@v{exercise.version_id}"
            )
            observations.append(
                HealthMomentObservation(
                    evidence_uri=evidence_uri,
                    kind="exercise",
                    observation=(
                        "A settled Health Connect exercise episode is ready for "
                        "a useful proactive briefing.\n"
                        f"Exercise: {(exercise_type or 'unspecified').replace('_', ' ')}\n"
                        f"Title: {exercise.title or 'Not provided'}\n"
                        f"Started: {start_time.isoformat()}\n"
                        f"Ended: {observed_at.isoformat()}\n"
                        f"Duration: {exercise.duration_minutes:.1f} minutes\n"
                        f"Evidence: {evidence_uri}\n"
                        "Use broader Memory, plans, Todos, Scheduled triggers, and "
                        "Health tools when relevant."
                    ),
                    observed_at=observed_at,
                    source_record_uid=exercise.record_uid,
                    source_version_id=exercise.version_id,
                )
            )
        latest_primary_sleep = await HealthConnectInsightQuery(
            self.database
        ).fetch_sleep_episode(
            days=max(1, min(31, self.lookback.days + 1)),
            episode_kind="primary_sleep",
        )
        selected_sleep = latest_primary_sleep.selected_episode
        materialized_sleep = {
            (sleep.record_uid, sleep.version_id) for sleep in sleep_rows
        }
        if (
            selected_sleep is not None
            and (selected_sleep.record_id, selected_sleep.source_version)
            in materialized_sleep
            and cutoff_millis
            <= int(selected_sleep.local_end.timestamp() * 1_000)
            <= now_millis
        ):
            sleeping_heart_rate = selected_sleep.sleeping_heart_rate
            sleep_efficiency = (
                "Unavailable"
                if selected_sleep.sleep_efficiency_percent is None
                else f"{selected_sleep.sleep_efficiency_percent:.1f}%"
            )
            sleeping_heart_rate_summary = (
                "Unavailable"
                if sleeping_heart_rate is None
                else (
                    f"{sleeping_heart_rate.average_bpm:.1f} bpm from "
                    f"{sleeping_heart_rate.sample_count} samples"
                )
            )
            time_asleep_minutes = selected_sleep.time_asleep_minutes
            time_in_bed_minutes = selected_sleep.time_in_bed_minutes
            observations.append(
                HealthMomentObservation(
                    evidence_uri=selected_sleep.evidence_uri,
                    kind="primary_sleep",
                    observation="\n".join(
                        [
                            " ".join(
                                [
                                    "A settled Health Connect primary sleep episode",
                                    "is ready for a useful proactive briefing.",
                                ]
                            ),
                            f"Started: {selected_sleep.local_start.isoformat()}",
                            f"Ended: {selected_sleep.local_end.isoformat()}",
                            f"Time in bed: {time_in_bed_minutes:.1f} minutes",
                            f"Time asleep: {time_asleep_minutes:.1f} minutes",
                            f"Sleep efficiency: {sleep_efficiency}",
                            "Sleeping heart rate: " + sleeping_heart_rate_summary,
                            f"Evidence: {selected_sleep.evidence_uri}",
                            " ".join(
                                [
                                    "Use broader Memory, plans, Todos, Scheduled",
                                    "triggers, and Health tools when relevant.",
                                ]
                            ),
                        ]
                    ),
                    observed_at=selected_sleep.local_end,
                    source_record_uid=selected_sleep.record_id,
                    source_version_id=selected_sleep.source_version,
                )
            )
        if self.planned_exercise is not None:
            observations.extend(
                HealthMomentObservation(
                    evidence_uri=miss.evidence_uri,
                    kind="missed_exercise",
                    observation=miss.observation,
                    observed_at=miss.observed_at,
                    source_record_uid=miss.source_record_uid,
                    source_version_id=miss.source_version_id,
                )
                for miss in await self.planned_exercise.reconcile(now=now)
            )
        return sorted(observations, key=lambda observation: observation.observed_at)


class HealthMomentDispatcher:
    """Run each durable Health briefing once, then push its stored full answer."""

    def __init__(
        self,
        *,
        conversation_service: HealthConversationPort,
        conversation_turns: HealthConversationTurnsPort,
        database: Database,
        push_sender: HealthPushSender | None,
    ) -> None:
        self.conversation_service: HealthConversationPort = conversation_service
        self.conversation_turns: HealthConversationTurnsPort = conversation_turns
        self.database: Database = database
        self.push_sender: HealthPushSender | None = push_sender

    async def dispatch_pending(self, *, now: datetime) -> HealthMomentDispatchReport:
        """Settle pending turns and retry only delivery from stored answers."""
        briefed = 0
        main_conversation = await self.conversation_service.fetch_main_conversation()
        async with self.database.transaction() as transaction:
            executable = list(
                await transaction.fetch_all(
                    select(HealthMoment)
                    .where(HealthMoment.status.in_("pending", "running"))
                    .order_by(HealthMoment.observed_at.asc())
                )
            )
        for moment in executable:
            ticket = await self.conversation_turns.submit(
                HealthTurnRequest(
                    conversation_id=main_conversation.id,
                    moment_id=UUID(str(moment.id)),
                    prompt=moment.observation,
                ),
                SilentChatFrameSink(),
            )
            async with self.database.transaction(mode="immediate") as transaction:
                _ = await transaction.execute(
                    update(HealthMoment)
                    .set(
                        HealthMoment.status.to("running"),
                        HealthMoment.turn_id.to(ticket.turn_id),
                    )
                    .where(HealthMoment.id.eq(moment.id))
                    .where(HealthMoment.status.in_("pending", "running"))
                )
            outcome = await self.conversation_turns.wait(ticket.turn_id)
            if outcome.status != "succeeded":
                async with self.database.transaction(mode="immediate") as transaction:
                    _ = await transaction.execute(
                        update(HealthMoment)
                        .set(
                            HealthMoment.completed_at.to(now),
                            HealthMoment.failure_summary.to(
                                outcome.failure_summary
                                or f"Health briefing turn {outcome.status}."
                            ),
                            HealthMoment.status.to("failed"),
                        )
                        .where(HealthMoment.id.eq(moment.id))
                    )
                continue
            messages = await self.conversation_service.fetch_messages(
                main_conversation.id,
                turn_id=ticket.turn_id,
            )
            answer = next(
                (
                    message
                    for message in reversed(messages)
                    if message.role == "assistant"
                ),
                None,
            )
            if answer is None:
                async with self.database.transaction(mode="immediate") as transaction:
                    _ = await transaction.execute(
                        update(HealthMoment)
                        .set(
                            HealthMoment.completed_at.to(now),
                            HealthMoment.failure_summary.to(
                                "Health briefing produced no assistant answer."
                            ),
                            HealthMoment.status.to("failed"),
                        )
                        .where(HealthMoment.id.eq(moment.id))
                    )
                continue
            async with self.database.transaction(mode="immediate") as transaction:
                _ = await transaction.execute(
                    update(HealthMoment)
                    .set(
                        HealthMoment.answer_message_id.to(answer.id),
                        HealthMoment.completed_at.to(now),
                        HealthMoment.failure_summary.to(None),
                        HealthMoment.status.to("succeeded"),
                    )
                    .where(HealthMoment.id.eq(moment.id))
                )
            briefed += 1
        pushed = await self._deliver_pending_pushes(now=now)
        return HealthMomentDispatchReport(briefed=briefed, pushed=pushed)

    async def _deliver_pending_pushes(self, *, now: datetime) -> int:
        """Retry Web Push from canonical assistant Messages without rerunning pi."""
        async with self.database.transaction() as transaction:
            moments = list(
                await transaction.fetch_all(
                    select(HealthMoment)
                    .where(HealthMoment.status.eq("succeeded"))
                    .where(HealthMoment.push_status.eq("pending"))
                    .order_by(HealthMoment.observed_at.asc())
                )
            )
        pushed = 0
        for moment in moments:
            if moment.turn_id is None or moment.answer_message_id is None:
                continue
            messages = await self.conversation_service.fetch_messages(
                (await self.conversation_service.fetch_main_conversation()).id,
                turn_id=moment.turn_id,
            )
            answer = next(
                (
                    message
                    for message in messages
                    if message.id == moment.answer_message_id
                    and message.role == "assistant"
                ),
                None,
            )
            if answer is None:
                continue
            if self.push_sender is not None:
                try:
                    await self.push_sender.send(
                        PushNotification(
                            body=answer.content,
                            title="Tether · Health",
                            url=f"/chat?turn={moment.turn_id}",
                        )
                    )
                except Exception as error:
                    _logger.warning(
                        "Health moment Web Push failed",
                        error_type=type(error).__name__,
                        moment_id=str(moment.id),
                    )
                    continue
            async with self.database.transaction(mode="immediate") as transaction:
                _ = await transaction.execute(
                    update(HealthMoment)
                    .set(
                        HealthMoment.push_delivered_at.to(now),
                        HealthMoment.push_status.to("delivered"),
                    )
                    .where(HealthMoment.id.eq(moment.id))
                    .where(HealthMoment.push_status.eq("pending"))
                )
            pushed += 1
        return pushed


@dataclass(frozen=True, slots=True)
class HealthMomentTickReport:
    """Counts from one complete episode-to-notification pass."""

    briefed: int
    created: int
    pushed: int


@dataclass(frozen=True, slots=True)
class HealthMomentWorker:
    """Run the complete proactive Health pipeline behind one timed interface."""

    dispatcher: HealthMomentDispatcher
    service: HealthMomentService
    summarizer: HealthEpisodeSummarizer

    async def tick(self, *, now: datetime) -> HealthMomentTickReport:
        """Materialize settled episodes before reconciling and dispatching."""
        _ = await self.summarizer.materialize(now=now)
        reconciliation = await self.service.reconcile(now=now)
        dispatch = await self.dispatcher.dispatch_pending(now=now)
        return HealthMomentTickReport(
            briefed=dispatch.briefed,
            created=reconciliation.created,
            pushed=dispatch.pushed,
        )


class HealthMomentService:
    """Convergently detect and retain Health moments.

    `reconcile` identifies a moment by its kind and source record rather than by
    the source version. Upstream corrections therefore update Health projections
    without manufacturing another proactive briefing.
    """

    def __init__(
        self,
        *,
        database: Database,
        observations: HealthMomentObservationSource,
    ) -> None:
        self.database: Database = database
        self.observations: HealthMomentObservationSource = observations

    async def reconcile(self, *, now: datetime) -> HealthMomentReconcileReport:
        """Persist each newly eligible observation exactly once."""
        created = 0
        observations = await self.observations.fetch_recent(now=now)
        async with self.database.transaction(mode="immediate") as transaction:
            for observation in observations:
                existing = await transaction.fetch_one_or_none(
                    select(HealthMoment)
                    .where(HealthMoment.kind.eq(observation.kind))
                    .where(
                        HealthMoment.source_record_uid.eq(observation.source_record_uid)
                    )
                )
                if existing is not None:
                    continue
                _ = await transaction.execute(
                    insert(
                        HealthMoment(
                            evidence_uri=observation.evidence_uri,
                            kind=observation.kind,
                            observation=observation.observation,
                            observed_at=observation.observed_at,
                            source_record_uid=observation.source_record_uid,
                            source_version_id=observation.source_version_id,
                            status="pending",
                        )
                    )
                )
                created += 1
        return HealthMomentReconcileReport(created=created)

    async def list_recent(self, *, limit: int) -> list[HealthMomentRead]:
        """Return newest Health moments for briefing work and presentation."""
        async with self.database.transaction() as transaction:
            moments = await transaction.fetch_all(
                select(HealthMoment)
                .all()
                .order_by(HealthMoment.observed_at.desc())
                .limit(limit)
            )
        return [_read(moment) for moment in moments]


async def create_health_moment_schema(database: Database) -> None:
    """Create durable Health moment state in the request-serving database."""
    await database.migrate(
        {
            "039_create_health_moment": (
                'CREATE TABLE "health_moment" ('
                '"id" TEXT PRIMARY KEY NOT NULL, "answer_message_id" TEXT, '
                '"completed_at" TEXT, '
                '"created_at" TEXT NOT NULL DEFAULT '
                "(strftime('%Y-%m-%dT%H:%M:%fZ', 'now')), "
                '"evidence_uri" TEXT NOT NULL, "failure_summary" TEXT, '
                '"kind" TEXT NOT NULL, "observation" TEXT NOT NULL, '
                '"observed_at" TEXT NOT NULL, "push_delivered_at" TEXT, '
                "\"push_status\" TEXT NOT NULL DEFAULT 'pending', "
                '"source_record_uid" TEXT NOT NULL, '
                '"source_version_id" INTEGER NOT NULL, '
                "\"status\" TEXT NOT NULL DEFAULT 'pending', "
                '"turn_id" TEXT) STRICT'
            ),
            "039_health_moment_source_unique": (
                'CREATE UNIQUE INDEX "health_moment_source_unique" '
                'ON "health_moment" ("kind", "source_record_uid")'
            ),
        }
    )


__all__ = [
    "HealthMomentDispatchReport",
    "HealthMomentDispatcher",
    "HealthMomentKind",
    "HealthMomentObservation",
    "HealthMomentObservationQuery",
    "HealthMomentObservationSource",
    "HealthMomentRead",
    "HealthMomentReconcileReport",
    "HealthMomentService",
    "HealthMomentStatus",
    "HealthMomentTickReport",
    "HealthMomentWorker",
    "create_health_moment_schema",
]
