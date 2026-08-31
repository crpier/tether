"""Evidence-backed recurring exercise intentions for the Health vertical."""

from __future__ import annotations

import builtins
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import ClassVar, Literal, cast
from uuid import UUID, uuid7
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, PositiveInt, TypeAdapter
from snekql import sqlite
from snekql.sqlite import (
    CurrentTimestamp,
    Database,
    Fetched,
    Index,
    Integer,
    Model,
    Pending,
    Text,
    UtcDatetime,
    insert,
    select,
    update,
)

from tether.health_connect.persistence import HcExerciseEpisodeSummary

ExercisePlanType = Literal["running", "strength_training", "walking", "weightlifting"]
HealthPlanStatus = Literal["active", "paused"]
PlannedExerciseStatus = Literal["matched", "missed"]
WeekdayName = Literal[
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
]

_EXERCISE_TYPE_CODES: dict[ExercisePlanType, frozenset[int]] = {
    "running": frozenset({56}),
    "strength_training": frozenset({70, 81}),
    "walking": frozenset({79}),
    "weightlifting": frozenset({81}),
}

_DEFAULT_OCCURRENCE_LOOKBACK = timedelta(hours=24)
"""Bound missed-window reconciliation so downtime cannot replay old plans."""

_DEFAULT_PLAN_LIMIT = 50
"""Maximum active plan definitions considered by one ordinary read."""

_OVERVIEW_OCCURRENCE_LIMIT = 1_000
"""Maximum settled occurrence explanations returned by one period read."""

_WEEKDAY_INDEX: dict[WeekdayName, int] = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}


class InvalidHealthPlanError(Exception):
    """A requested Health plan cannot represent a safe recurring intention."""


class HealthPlanNotFoundError(Exception):
    """A referenced Health plan does not exist."""


class HealthPlanConflictError(Exception):
    """A Health plan changed after the caller observed its version."""


class HealthPlan[S = Pending](Model[S, "HealthPlan[Fetched]"]):
    """One persisted weekly exercise intention sourced from user Evidence."""

    id: sqlite.GenCol[UUID] = Text(primary_key=True, default_factory=uuid7)  # ty: ignore[invalid-assignment]
    created_at: sqlite.GenCol[UtcDatetime] = Text(default=CurrentTimestamp)
    effective_at: sqlite.Col[UtcDatetime] = Text(nullable=False)
    exercise_types_json: sqlite.Col[str] = Text(nullable=False)
    grace_minutes: sqlite.Col[int] = Integer(nullable=False)
    source_conversation_id: sqlite.Col[UUID] = Text(nullable=False)
    source_message_id: sqlite.Col[UUID] = Text(nullable=False)
    status: sqlite.Col[HealthPlanStatus] = Text(
        default=cast("HealthPlanStatus", "active")
    )
    timezone: sqlite.Col[str] = Text(nullable=False)
    title: sqlite.Col[str] = Text(nullable=False)
    updated_at: sqlite.GenCol[UtcDatetime] = Text(default=CurrentTimestamp)
    version: sqlite.Col[PositiveInt] = Integer(default=1)
    windows_json: sqlite.Col[str] = Text(nullable=False)

    __indexes__: ClassVar = [Index(status, created_at)]


class PlannedExerciseOccurrence[S = Pending](
    Model[S, "PlannedExerciseOccurrence[Fetched]"]
):
    """One settled dated realization of a snapshotted Exercise window."""

    id: sqlite.GenCol[UUID] = Text(  # ty: ignore[invalid-assignment]
        primary_key=True, default_factory=uuid7
    )
    created_at: sqlite.GenCol[UtcDatetime] = Text(default=CurrentTimestamp)
    grace_ended_at: sqlite.Col[UtcDatetime] = Text(nullable=False)
    local_date: sqlite.Col[str] = Text(nullable=False)
    matched_evidence_uri: sqlite.Col[str | None] = Text(default=None, nullable=True)
    plan_id: sqlite.Col[UUID] = Text(nullable=False)
    plan_version: sqlite.Col[int] = Integer(nullable=False)
    source_record_uid: sqlite.Col[str] = Text(nullable=False)
    status: sqlite.Col[PlannedExerciseStatus] = Text(nullable=False)
    timezone: sqlite.Col[str] = Text(nullable=False)
    title: sqlite.Col[str] = Text(nullable=False)
    updated_at: sqlite.GenCol[UtcDatetime] = Text(default=CurrentTimestamp)
    window_ended_at: sqlite.Col[UtcDatetime] = Text(nullable=False)
    window_started_at: sqlite.Col[UtcDatetime] = Text(nullable=False)

    __indexes__: ClassVar = [Index(plan_id, grace_ended_at)]


class ExerciseWindowInput(BaseModel):
    """One explicit local weekly interval supplied by foreground chat."""

    end_local_time: time
    start_local_time: time
    weekday: WeekdayName


class HealthPlanDraft(BaseModel):
    """Create one explicitly requested recurring exercise intention.

    Use an IANA `timezone` and one or more same-day local weekly `windows`.
    `strength_training` accepts both strength-training and weightlifting Health
    Connect episodes. The host rejects creation or revision unless the active
    turn has fresh foreground user Evidence.
    """

    exercise_types: list[ExercisePlanType] = Field(min_length=1, max_length=4)
    grace_minutes: int = Field(default=60, ge=15, le=360)
    timezone: str = Field(min_length=1, max_length=100)
    title: str = Field(min_length=1, max_length=120)
    windows: list[ExerciseWindowInput] = Field(min_length=1, max_length=14)


class ExerciseWindowRead(BaseModel):
    """Canonical numeric-weekday representation of an Exercise window."""

    end_local_time: time
    start_local_time: time
    weekday: int = Field(ge=0, le=6)


class PlannedExerciseOccurrenceRead(BaseModel):
    """Current deterministic adherence state for one settled window."""

    grace_ended_at: datetime
    local_date: date
    matched_evidence_uri: str | None
    plan_id: UUID
    plan_version: PositiveInt
    source_record_uid: str
    status: PlannedExerciseStatus
    timezone: str
    title: str
    window_ended_at: datetime
    window_started_at: datetime


@dataclass(frozen=True, slots=True)
class PlannedExerciseMiss:
    """One unmatched occurrence eligible for a Health moment."""

    evidence_uri: str
    observation: str
    observed_at: datetime
    source_record_uid: str
    source_version_id: int


class HealthPlanRead(BaseModel):
    """One Health plan exposed to chat and Health presentation."""

    created_at: datetime
    effective_at: datetime
    exercise_types: list[ExercisePlanType]
    grace_minutes: int
    id: UUID
    source_evidence_uri: str
    status: HealthPlanStatus
    timezone: str
    title: str
    updated_at: datetime
    version: PositiveInt
    windows: list[ExerciseWindowRead]


@dataclass(frozen=True, slots=True)
class HealthPlanEvidence:
    """The foreground user Message authorizing one plan definition."""

    conversation_id: UUID
    message_id: UUID
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class _NormalizedHealthPlan:
    """Canonical values shared by create and revision persistence."""

    exercise_types_json: str
    grace_minutes: int
    timezone: str
    title: str
    windows_json: str


class HealthPlanService:
    """Own Health plan validation, persistence, and read projection."""

    def __init__(self, database: Database) -> None:
        self.database: Database = database

    async def create(
        self,
        draft: HealthPlanDraft,
        *,
        evidence: HealthPlanEvidence,
    ) -> HealthPlanRead:
        """Persist one plan only after validating its local-time definition."""
        normalized = self._normalize(draft)
        pending = HealthPlan(
            effective_at=evidence.occurred_at,
            exercise_types_json=normalized.exercise_types_json,
            grace_minutes=normalized.grace_minutes,
            source_conversation_id=evidence.conversation_id,
            source_message_id=evidence.message_id,
            status="active",
            timezone=normalized.timezone,
            title=normalized.title,
            windows_json=normalized.windows_json,
        )
        async with self.database.transaction(mode="immediate") as transaction:
            plan = await transaction.execute(insert(pending).returning())
        return self._read(plan)

    async def update(
        self,
        plan_id: UUID,
        draft: HealthPlanDraft,
        *,
        evidence: HealthPlanEvidence,
        version: int,
    ) -> HealthPlanRead:
        """Replace one plan definition at its observed version."""
        normalized = self._normalize(draft)
        async with self.database.transaction(mode="immediate") as transaction:
            current = await transaction.fetch_one_or_none(
                select(HealthPlan).where(HealthPlan.id.eq(plan_id))
            )
            if current is None:
                raise HealthPlanNotFoundError(plan_id)
            if current.version != version:
                raise HealthPlanConflictError(plan_id)
            _ = await transaction.execute(
                update(HealthPlan)
                .set(
                    HealthPlan.effective_at.to(evidence.occurred_at),
                    HealthPlan.exercise_types_json.to(normalized.exercise_types_json),
                    HealthPlan.grace_minutes.to(normalized.grace_minutes),
                    HealthPlan.source_conversation_id.to(evidence.conversation_id),
                    HealthPlan.source_message_id.to(evidence.message_id),
                    HealthPlan.timezone.to(normalized.timezone),
                    HealthPlan.title.to(normalized.title),
                    HealthPlan.updated_at.to(CurrentTimestamp),
                    HealthPlan.version.to(version + 1),
                    HealthPlan.windows_json.to(normalized.windows_json),
                )
                .where(HealthPlan.id.eq(plan_id))
                .where(HealthPlan.version.eq(version))
            )
            revised = await transaction.fetch_one(
                select(HealthPlan).where(HealthPlan.id.eq(plan_id))
            )
        return self._read(revised)

    async def set_status(
        self,
        plan_id: UUID,
        *,
        evidence: HealthPlanEvidence,
        status: HealthPlanStatus,
        version: int,
    ) -> HealthPlanRead:
        """Pause or resume one plan at its observed version."""
        async with self.database.transaction(mode="immediate") as transaction:
            current = await transaction.fetch_one_or_none(
                select(HealthPlan).where(HealthPlan.id.eq(plan_id))
            )
            if current is None:
                raise HealthPlanNotFoundError(plan_id)
            if current.version != version:
                raise HealthPlanConflictError(plan_id)
            _ = await transaction.execute(
                update(HealthPlan)
                .set(
                    HealthPlan.effective_at.to(evidence.occurred_at),
                    HealthPlan.source_conversation_id.to(evidence.conversation_id),
                    HealthPlan.source_message_id.to(evidence.message_id),
                    HealthPlan.status.to(status),
                    HealthPlan.updated_at.to(CurrentTimestamp),
                    HealthPlan.version.to(version + 1),
                )
                .where(HealthPlan.id.eq(plan_id))
                .where(HealthPlan.version.eq(version))
            )
            updated = await transaction.fetch_one(
                select(HealthPlan).where(HealthPlan.id.eq(plan_id))
            )
        return self._read(updated)

    async def list(
        self,
        *,
        include_paused: bool = True,
        limit: int = _DEFAULT_PLAN_LIMIT,
    ) -> builtins.list[HealthPlanRead]:
        """Return a bounded set of current plans in creation order."""
        query = (
            (
                select(HealthPlan).all()
                if include_paused
                else select(HealthPlan).where(HealthPlan.status.eq("active"))
            )
            .order_by(HealthPlan.created_at.asc())
            .limit(limit)
        )
        async with self.database.transaction() as transaction:
            plans = await transaction.fetch_all(query)
        return [self._read(plan) for plan in plans]

    async def list_occurrences(
        self, *, after: datetime, before: datetime
    ) -> builtins.list[PlannedExerciseOccurrenceRead]:
        """Return settled adherence explanations inside one bounded period."""
        async with self.database.transaction() as transaction:
            occurrences = await transaction.fetch_all(
                select(PlannedExerciseOccurrence)
                .where(PlannedExerciseOccurrence.grace_ended_at.gte(after))
                .where(PlannedExerciseOccurrence.grace_ended_at.lte(before))
                .order_by(PlannedExerciseOccurrence.grace_ended_at.desc())
                .limit(_OVERVIEW_OCCURRENCE_LIMIT)
            )
        return [self._read_occurrence(occurrence) for occurrence in occurrences]

    @staticmethod
    def _normalize(draft: HealthPlanDraft) -> _NormalizedHealthPlan:
        """Validate and canonicalize one complete weekly plan definition."""
        try:
            _ = ZoneInfo(draft.timezone)
        except ZoneInfoNotFoundError as error:
            message = "timezone must be a known IANA timezone"
            raise InvalidHealthPlanError(message) from error
        title = draft.title.strip()
        if not title:
            message = "title must not be blank"
            raise InvalidHealthPlanError(message)
        exercise_types = sorted(set(draft.exercise_types))
        windows = sorted(
            (
                ExerciseWindowRead(
                    end_local_time=window.end_local_time,
                    start_local_time=window.start_local_time,
                    weekday=_WEEKDAY_INDEX[window.weekday],
                )
                for window in draft.windows
            ),
            key=lambda window: (window.weekday, window.start_local_time),
        )
        for window in windows:
            if window.start_local_time >= window.end_local_time:
                message = "each Exercise window must start and end on the same day"
                raise InvalidHealthPlanError(message)
        window_keys = {
            (window.weekday, window.start_local_time, window.end_local_time)
            for window in windows
        }
        if len(window_keys) != len(windows):
            message = "Exercise windows must not contain duplicates"
            raise InvalidHealthPlanError(message)
        return _NormalizedHealthPlan(
            exercise_types_json=TypeAdapter(list[ExercisePlanType])
            .dump_json(exercise_types)
            .decode(),
            grace_minutes=draft.grace_minutes,
            timezone=draft.timezone,
            title=title,
            windows_json=TypeAdapter(list[ExerciseWindowRead])
            .dump_json(windows)
            .decode(),
        )

    @staticmethod
    def _read_occurrence(
        occurrence: PlannedExerciseOccurrence[Fetched],
    ) -> PlannedExerciseOccurrenceRead:
        """Project persisted occurrence state without exposing storage details."""
        return PlannedExerciseOccurrenceRead(
            grace_ended_at=occurrence.grace_ended_at,
            local_date=date.fromisoformat(occurrence.local_date),
            matched_evidence_uri=occurrence.matched_evidence_uri,
            plan_id=UUID(str(occurrence.plan_id)),
            plan_version=occurrence.plan_version,
            source_record_uid=occurrence.source_record_uid,
            status=occurrence.status,
            timezone=occurrence.timezone,
            title=occurrence.title,
            window_ended_at=occurrence.window_ended_at,
            window_started_at=occurrence.window_started_at,
        )

    @staticmethod
    def _read(plan: HealthPlan[Fetched]) -> HealthPlanRead:
        """Decode the typed plan definition stored in compact JSON columns."""
        return HealthPlanRead(
            created_at=plan.created_at,
            effective_at=plan.effective_at,
            exercise_types=TypeAdapter(list[ExercisePlanType]).validate_json(
                plan.exercise_types_json
            ),
            grace_minutes=plan.grace_minutes,
            id=UUID(str(plan.id)),
            source_evidence_uri=f"tether://message/{plan.source_message_id}",
            status=plan.status,
            timezone=plan.timezone,
            title=plan.title,
            updated_at=plan.updated_at,
            version=plan.version,
            windows=TypeAdapter(list[ExerciseWindowRead]).validate_json(
                plan.windows_json
            ),
        )


@dataclass(frozen=True, slots=True)
class _OccurrenceCandidate:
    """One ended window and its current matching exercise, if present."""

    grace_ended_at: datetime
    local_date: date
    match: HcExerciseEpisodeSummary[Fetched] | None
    plan: HealthPlanRead
    source_record_uid: str
    window_ended_at: datetime
    window_started_at: datetime


class HealthPlanOccurrenceReconciler:
    """Settle recent Exercise windows against current exercise projections."""

    def __init__(
        self,
        *,
        database: Database,
        telemetry_database: Database,
        lookback: timedelta = _DEFAULT_OCCURRENCE_LOOKBACK,
    ) -> None:
        self.database: Database = database
        self.lookback: timedelta = lookback
        self.telemetry_database: Database = telemetry_database

    async def reconcile(self, *, now: datetime) -> list[PlannedExerciseMiss]:
        """Return current misses after converging recent occurrence state."""
        plans = await HealthPlanService(self.database).list(
            include_paused=False,
            limit=_DEFAULT_PLAN_LIMIT,
        )
        exercise_cutoff = int(
            (now - self.lookback - timedelta(days=1)).timestamp() * 1_000
        )
        async with self.telemetry_database.transaction() as transaction:
            exercises = await transaction.fetch_all(
                select(HcExerciseEpisodeSummary)
                .where(HcExerciseEpisodeSummary.end_time.gte(exercise_cutoff))
                .where(
                    HcExerciseEpisodeSummary.start_time.lte(
                        int(now.timestamp() * 1_000)
                    )
                )
            )
        misses: list[PlannedExerciseMiss] = []
        for plan in plans:
            zone = ZoneInfo(plan.timezone)
            first_date = (now - self.lookback).astimezone(zone).date()
            last_date = now.astimezone(zone).date()
            for day_offset in range((last_date - first_date).days + 1):
                local_date = first_date + timedelta(days=day_offset)
                for window in plan.windows:
                    if window.weekday != local_date.weekday():
                        continue
                    window_started_at = datetime.combine(
                        local_date,
                        window.start_local_time,
                        tzinfo=zone,
                    ).astimezone(UTC)
                    window_ended_at = datetime.combine(
                        local_date,
                        window.end_local_time,
                        tzinfo=zone,
                    ).astimezone(UTC)
                    grace_ended_at = window_ended_at + timedelta(
                        minutes=plan.grace_minutes
                    )
                    if (
                        window_started_at < plan.effective_at
                        or grace_ended_at > now
                        or grace_ended_at < now - self.lookback
                    ):
                        continue
                    source_record_uid = ":".join(
                        [
                            str(plan.id),
                            local_date.isoformat(),
                            str(window.weekday),
                            window.start_local_time.isoformat(timespec="minutes"),
                            window.end_local_time.isoformat(timespec="minutes"),
                        ]
                    )
                    match = self._match(
                        exercises,
                        exercise_types=plan.exercise_types,
                        window_ended_at=window_ended_at,
                        window_started_at=window_started_at,
                    )
                    occurrence = await self._converge_occurrence(
                        _OccurrenceCandidate(
                            grace_ended_at=grace_ended_at,
                            local_date=local_date,
                            match=match,
                            plan=plan,
                            source_record_uid=source_record_uid,
                            window_ended_at=window_ended_at,
                            window_started_at=window_started_at,
                        )
                    )
                    if occurrence.status == "missed":
                        misses.append(self._miss(plan, occurrence))
        return sorted(misses, key=lambda miss: miss.observed_at)

    async def _converge_occurrence(
        self,
        candidate: _OccurrenceCandidate,
    ) -> PlannedExerciseOccurrence[Fetched]:
        """Persist one stable occurrence and accept late matching Evidence."""
        matched_evidence_uri = (
            None
            if candidate.match is None
            else (
                "tether://health-connect/exercise/"
                f"{candidate.match.record_uid}@v{candidate.match.version_id}"
            )
        )
        async with self.database.transaction(mode="immediate") as transaction:
            occurrence = await transaction.fetch_one_or_none(
                select(PlannedExerciseOccurrence).where(
                    PlannedExerciseOccurrence.source_record_uid.eq(
                        candidate.source_record_uid
                    )
                )
            )
            if occurrence is None:
                return await transaction.execute(
                    insert(
                        PlannedExerciseOccurrence(
                            grace_ended_at=candidate.grace_ended_at,
                            local_date=candidate.local_date.isoformat(),
                            matched_evidence_uri=matched_evidence_uri,
                            plan_id=candidate.plan.id,
                            plan_version=candidate.plan.version,
                            source_record_uid=candidate.source_record_uid,
                            status=("missed" if candidate.match is None else "matched"),
                            timezone=candidate.plan.timezone,
                            title=candidate.plan.title,
                            window_ended_at=candidate.window_ended_at,
                            window_started_at=candidate.window_started_at,
                        )
                    ).returning()
                )
            if occurrence.status == "missed" and matched_evidence_uri is not None:
                _ = await transaction.execute(
                    update(PlannedExerciseOccurrence)
                    .set(
                        PlannedExerciseOccurrence.matched_evidence_uri.to(
                            matched_evidence_uri
                        ),
                        PlannedExerciseOccurrence.status.to("matched"),
                        PlannedExerciseOccurrence.updated_at.to(CurrentTimestamp),
                    )
                    .where(PlannedExerciseOccurrence.id.eq(occurrence.id))
                )
                return await transaction.fetch_one(
                    select(PlannedExerciseOccurrence).where(
                        PlannedExerciseOccurrence.id.eq(occurrence.id)
                    )
                )
            return occurrence

    @staticmethod
    def _match(
        exercises: list[HcExerciseEpisodeSummary[Fetched]],
        *,
        exercise_types: list[ExercisePlanType],
        window_ended_at: datetime,
        window_started_at: datetime,
    ) -> HcExerciseEpisodeSummary[Fetched] | None:
        """Match only typed settled episodes that overlap the explicit window."""
        accepted_codes = {
            code
            for exercise_type in exercise_types
            for code in _EXERCISE_TYPE_CODES[exercise_type]
        }
        window_end_millis = int(window_ended_at.timestamp() * 1_000)
        window_start_millis = int(window_started_at.timestamp() * 1_000)
        return next(
            (
                exercise
                for exercise in exercises
                if exercise.exercise_type in accepted_codes
                and exercise.start_time < window_end_millis
                and exercise.end_time > window_start_millis
            ),
            None,
        )

    @staticmethod
    def _miss(
        plan: HealthPlanRead,
        occurrence: PlannedExerciseOccurrence[Fetched],
    ) -> PlannedExerciseMiss:
        """Describe deterministic absence without assigning blame or diagnosis."""
        evidence_uri = plan.source_evidence_uri
        expected = ", ".join(
            exercise_type.replace("_", " ") for exercise_type in plan.exercise_types
        )
        return PlannedExerciseMiss(
            evidence_uri=evidence_uri,
            observation="\n".join(
                [
                    "A planned Exercise window ended without a matching settled workout.",
                    f"Plan: {occurrence.title}",
                    f"Expected exercise: {expected}",
                    f"Window started: {occurrence.window_started_at.isoformat()}",
                    f"Window ended: {occurrence.window_ended_at.isoformat()}",
                    f"Sync grace ended: {occurrence.grace_ended_at.isoformat()}",
                    "No matching settled Health Connect exercise overlaps this window.",
                    "Absence may reflect rest, changed plans, or delayed source data.",
                    "Ask one brief, non-judgmental check-in when useful.",
                    "Do not use guilt, streak pressure, diagnosis, or an opaque score.",
                    "Use broader Memory, Todos, and Scheduled triggers when relevant.",
                    f"Plan Evidence: {evidence_uri}",
                ]
            ),
            observed_at=occurrence.grace_ended_at,
            source_record_uid=occurrence.source_record_uid,
            source_version_id=occurrence.plan_version,
        )


async def create_health_plan_schema(database: Database) -> None:
    """Create durable Health plan persistence in the host database."""
    await database.migrate(
        {
            "040_create_health_plan": (
                'CREATE TABLE "health_plan" ('
                '"id" TEXT PRIMARY KEY NOT NULL, '
                '"created_at" TEXT NOT NULL DEFAULT '
                "(strftime('%Y-%m-%dT%H:%M:%fZ', 'now')), "
                '"effective_at" TEXT NOT NULL, '
                '"exercise_types_json" TEXT NOT NULL, '
                '"grace_minutes" INTEGER NOT NULL, '
                '"source_conversation_id" TEXT NOT NULL, '
                '"source_message_id" TEXT NOT NULL, '
                "\"status\" TEXT NOT NULL DEFAULT 'active', "
                '"timezone" TEXT NOT NULL, "title" TEXT NOT NULL, '
                '"updated_at" TEXT NOT NULL DEFAULT '
                "(strftime('%Y-%m-%dT%H:%M:%fZ', 'now')), "
                '"version" INTEGER NOT NULL DEFAULT 1, '
                '"windows_json" TEXT NOT NULL) STRICT'
            ),
            "040_health_plan_status_created_index": (
                'CREATE INDEX "health_plan_status_created_index" '
                'ON "health_plan" ("status", "created_at")'
            ),
            "040_create_planned_exercise_occurrence": (
                'CREATE TABLE "planned_exercise_occurrence" ('
                '"id" TEXT PRIMARY KEY NOT NULL, '
                '"created_at" TEXT NOT NULL DEFAULT '
                "(strftime('%Y-%m-%dT%H:%M:%fZ', 'now')), "
                '"grace_ended_at" TEXT NOT NULL, "local_date" TEXT NOT NULL, '
                '"matched_evidence_uri" TEXT, "plan_id" TEXT NOT NULL, '
                '"plan_version" INTEGER NOT NULL, '
                '"source_record_uid" TEXT NOT NULL, "status" TEXT NOT NULL, '
                '"timezone" TEXT NOT NULL, "title" TEXT NOT NULL, '
                '"updated_at" TEXT NOT NULL DEFAULT '
                "(strftime('%Y-%m-%dT%H:%M:%fZ', 'now')), "
                '"window_ended_at" TEXT NOT NULL, '
                '"window_started_at" TEXT NOT NULL) STRICT'
            ),
            "040_planned_exercise_occurrence_source_unique": (
                "CREATE UNIQUE INDEX "
                '"planned_exercise_occurrence_source_unique" ON '
                '"planned_exercise_occurrence" ("source_record_uid")'
            ),
            "040_planned_exercise_occurrence_plan_grace_index": (
                'CREATE INDEX "planned_exercise_occurrence_plan_grace_index" '
                'ON "planned_exercise_occurrence" ("plan_id", "grace_ended_at")'
            ),
        }
    )


__all__ = [
    "ExercisePlanType",
    "ExerciseWindowInput",
    "ExerciseWindowRead",
    "HealthPlanConflictError",
    "HealthPlanDraft",
    "HealthPlanEvidence",
    "HealthPlanNotFoundError",
    "HealthPlanOccurrenceReconciler",
    "HealthPlanRead",
    "HealthPlanService",
    "HealthPlanStatus",
    "InvalidHealthPlanError",
    "PlannedExerciseMiss",
    "PlannedExerciseOccurrenceRead",
    "PlannedExerciseStatus",
    "create_health_plan_schema",
]
