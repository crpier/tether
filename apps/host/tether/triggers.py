"""Scheduled trigger domain: a time spec paired with an action, plus state.

A Scheduled trigger is the push half of capture → resurface. It pairs a **time
spec** — `once` at an absolute instant, or `daily`/`weekly` on a wall-clock
recurrence — with an **action**: deliver a fixed `message` verbatim, or run a
`prompt` through the agent and deliver its result.

Two clocks live on every row, kept deliberately apart:

* The **recurrence clock** — `next_fire_at` (UTC) is the next scheduled
  occurrence. Recurring rules store wall-clock time + IANA timezone (and a
  weekday for weekly) and each reschedule materialises the next occurrence as
  UTC, so a 09:00 local fire stays 09:00 local across a DST change.
* The **retry clock** — `next_attempt_at` (UTC), set only after a failed
  dispatch, overrides `next_fire_at` to back the occurrence off and try again.

The service owns the human-facing CRUD (create / list / update / delete, the
last two optimistic-concurrency checked) and the scheduler-facing transitions
(`claim_due`, `record_success`, `record_failure`). Claiming stamps `claimed_at`
before any dispatch, so a row in flight is never picked up twice.

>>> service = TriggerService(database=database, tracer=tracer)
>>> spec = TriggerSpec(
...     recurrence="once",
...     action_kind="message",
...     payload="call the dentist",
...     fire_at=datetime(2030, 1, 1, 15, 0, tzinfo=UTC),
... )
>>> trigger = await service.create(
...     spec, now=datetime(2030, 1, 1, 9, 0, tzinfo=UTC), logger=logger
... )
>>> trigger.status
'active'
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import ClassVar, Literal
from uuid import uuid7
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from opentelemetry.trace import Tracer
from pydantic import UUID7, PositiveInt
from snekql.sqlite import (
    CurrentTimestamp,
    Database,
    Fetched,
    Index,
    Integer,
    Model,
    Pending,
    Predicate,
    Text,
    Transaction,
    UpdateQuery,
    insert,
    select,
    update,
)
from snekql.sqlite._schema_ddl import scaffold_sqlite_statements

from tether.events import EventPublisher, InvalidateEvent, NullEventPublisher
from tether.logging import Logger

type TriggerRecurrence = Literal["once", "daily", "weekly"]
"""How often a trigger fires: a single instant, or a wall-clock recurrence."""

type TriggerActionKind = Literal["message", "prompt"]
"""What a fired trigger does: deliver fixed text, or run a prompt through pi."""

type TriggerStatus = Literal["active", "completed", "failed"]
"""A trigger's firing lifecycle; recurring triggers stay `active` forever."""

_MAX_WEEKDAY = 6
"""Highest valid weekday index (Monday is 0, Sunday is 6)."""

DEFAULT_MAX_ATTEMPTS = 5
"""Dispatch attempts at one occurrence before the scheduler gives up on it."""

DEFAULT_BACKOFF_BASE = timedelta(seconds=30)
"""First retry delay; subsequent retries double it (exponential backoff)."""


class TriggerNotFoundError(Exception):
    """Raised when an operation targets a trigger that does not exist."""


class TriggerConflictError(Exception):
    """Raised when a live trigger cannot accept the requested operation.

    A stale observed version, not absence: the caller acted on a trigger that
    has moved on since it was read.
    """


class InvalidTriggerSpecError(Exception):
    """Raised when a trigger's time spec is malformed for its recurrence."""


def _debug(logger: Logger, event: str, **context: object) -> None:
    """Emit a debug event using caller-supplied logging context."""
    logger.debug(event, **context)


def _info(logger: Logger, event: str, **context: object) -> None:
    """Emit an info event using caller-supplied logging context."""
    logger.info(event, **context)


def _parse_wall_time(value: str) -> time:
    """Parse a `HH:MM` wall-clock time, rejecting anything else.

    Recurring triggers fire at a wall-clock time of day; the stored form is a
    fixed `HH:MM` string so the recurrence can be re-materialised every tick.
    """
    try:
        parsed = time.fromisoformat(value)
    except ValueError as error:
        message = f"wall-clock time must be HH:MM, got {value!r}"
        raise InvalidTriggerSpecError(message) from error
    if parsed.second or parsed.microsecond or parsed.tzinfo is not None:
        message = f"wall-clock time must be HH:MM, got {value!r}"
        raise InvalidTriggerSpecError(message)
    return parsed


def _zone(timezone: str) -> ZoneInfo:
    """Resolve an IANA timezone name or raise a domain error."""
    try:
        return ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as error:
        message = f"unknown timezone: {timezone!r}"
        raise InvalidTriggerSpecError(message) from error


def next_recurring_fire(
    recurrence: Literal["daily", "weekly"],
    *,
    timezone: str,
    wall_time: time,
    weekday: int | None,
    after: datetime,
) -> datetime:
    """Materialise the next wall-clock occurrence strictly after `after`, as UTC.

    The arithmetic is done on a *naive* local date so wall-clock time is
    preserved across DST: the date is advanced by whole days, then re-localised
    onto the timezone (which re-derives the UTC offset for that date) and
    converted to UTC. Adding a `timedelta` to an aware instant instead would
    shift the wall-clock hour whenever the offset changed.
    """
    zone = _zone(timezone)
    local_after = after.astimezone(zone)
    target = datetime.combine(local_after.date(), wall_time)
    if recurrence == "weekly":
        if weekday is None:
            message = "weekly recurrence requires a weekday"
            raise InvalidTriggerSpecError(message)
        days_ahead = (weekday - target.weekday()) % 7
        target += timedelta(days=days_ahead)
    candidate = target.replace(tzinfo=zone)
    if candidate <= local_after:
        step = timedelta(days=7 if recurrence == "weekly" else 1)
        candidate = (target + step).replace(tzinfo=zone)
    return candidate.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class TriggerSpec:
    """A trigger's full definition — its time spec plus its action.

    The same shape drives both create and update. Which scheduling fields are
    required versus forbidden depends on `recurrence`, and is validated by
    `_describe_schedule` rather than by the dataclass, so the rules live in one
    place and surface as `InvalidTriggerSpecError`.

    ```python
    spec = TriggerSpec(
        recurrence="weekly",
        action_kind="message",
        payload="weekly review",
        timezone="UTC",
        time_of_day="08:30",
        weekday=4,
    )
    assert spec.weekday == 4
    ```
    """

    recurrence: TriggerRecurrence
    action_kind: TriggerActionKind
    payload: str
    timezone: str | None = None
    time_of_day: str | None = None
    weekday: int | None = None
    fire_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class _ScheduleFacts:
    """The stored schedule columns derived from a validated time spec."""

    timezone: str
    wall_time: str | None
    weekday: int | None
    next_fire_at: datetime


def _describe_once(spec: TriggerSpec) -> _ScheduleFacts:
    """Validate a `once` time spec and derive its stored facts."""
    if spec.fire_at is None:
        message = "a once trigger requires fire_at"
        raise InvalidTriggerSpecError(message)
    if spec.time_of_day is not None or spec.weekday is not None:
        message = "a once trigger takes neither a time of day nor a weekday"
        raise InvalidTriggerSpecError(message)
    if spec.fire_at.tzinfo is None:
        message = "a once trigger's fire_at must be timezone-aware"
        raise InvalidTriggerSpecError(message)
    return _ScheduleFacts(
        timezone=spec.timezone or "UTC",
        wall_time=None,
        weekday=None,
        next_fire_at=spec.fire_at.astimezone(UTC),
    )


def _validate_weekday(weekday: int | None) -> None:
    """Reject a weekly trigger's weekday that is absent or out of 0..6."""
    if weekday is None:
        message = "a weekly trigger requires a weekday"
        raise InvalidTriggerSpecError(message)
    if not 0 <= weekday <= _MAX_WEEKDAY:
        message = f"weekday must be 0..6, got {weekday}"
        raise InvalidTriggerSpecError(message)


def _describe_recurring(
    spec: TriggerSpec,
    recurrence: Literal["daily", "weekly"],
    *,
    now: datetime,
) -> _ScheduleFacts:
    """Validate a `daily`/`weekly` time spec and materialise its first fire."""
    if spec.fire_at is not None:
        message = f"a {recurrence} trigger does not take fire_at"
        raise InvalidTriggerSpecError(message)
    if spec.timezone is None or spec.time_of_day is None:
        message = f"a {recurrence} trigger requires a timezone and a time of day"
        raise InvalidTriggerSpecError(message)
    parsed_time = _parse_wall_time(spec.time_of_day)
    if recurrence == "weekly":
        _validate_weekday(spec.weekday)
    elif spec.weekday is not None:
        message = "a daily trigger does not take a weekday"
        raise InvalidTriggerSpecError(message)
    next_fire_at = next_recurring_fire(
        recurrence,
        timezone=spec.timezone,
        wall_time=parsed_time,
        weekday=spec.weekday,
        after=now,
    )
    return _ScheduleFacts(
        timezone=spec.timezone,
        wall_time=parsed_time.isoformat(timespec="minutes"),
        weekday=spec.weekday,
        next_fire_at=next_fire_at,
    )


def _describe_schedule(spec: TriggerSpec, *, now: datetime) -> _ScheduleFacts:
    """Validate a time spec for its recurrence and derive its stored facts.

    Each recurrence owns which fields it requires and forbids, so a malformed
    spec is a well-formed domain error rather than a corrupt row. `once` carries
    an absolute instant (converted to UTC); `daily`/`weekly` carry a wall-clock
    time plus timezone (and, for weekly, a weekday) from which the first
    occurrence is materialised.
    """
    if spec.recurrence == "once":
        return _describe_once(spec)
    return _describe_recurring(spec, spec.recurrence, now=now)


class ScheduledTrigger[S = Pending](Model[S, "ScheduledTrigger[Fetched]"]):
    """A time-triggered action: a recurrence, an action, plus scheduler state."""

    id: ScheduledTrigger.GenCol[UUID7] = Text(
        primary_key=True,
        default_factory=uuid7,
    )
    recurrence: ScheduledTrigger.Col[TriggerRecurrence] = Text()
    """How often the trigger fires: `once`, `daily`, or `weekly`."""
    action_kind: ScheduledTrigger.Col[TriggerActionKind] = Text()
    """`message` delivers `payload` verbatim; `prompt` runs it through pi."""
    payload: ScheduledTrigger.Col[str] = Text()
    """The fixed message text, or the agent prompt, depending on `action_kind`."""
    timezone: ScheduledTrigger.Col[str] = Text()
    """IANA timezone the wall-clock recurrence is anchored to."""
    wall_time: ScheduledTrigger.Col[str | None] = Text(default=None, nullable=True)
    """`HH:MM` wall-clock fire time for recurring triggers; null for `once`."""
    weekday: ScheduledTrigger.Col[int | None] = Integer(default=None, nullable=True)
    """Weekday (Mon=0) for `weekly`; null otherwise."""
    next_fire_at: ScheduledTrigger.Col[datetime] = Text()
    """The next scheduled occurrence, as UTC."""
    status: ScheduledTrigger.Col[TriggerStatus] = Text()
    """Firing lifecycle; recurring triggers remain `active`."""
    claimed_at: ScheduledTrigger.Col[datetime | None] = Text(
        default=None,
        nullable=True,
    )
    """Stamped when a scheduler tick claims the row for dispatch."""
    attempts: ScheduledTrigger.Col[int] = Integer(default=0)
    """Failed dispatch attempts at the current occurrence."""
    next_attempt_at: ScheduledTrigger.Col[datetime | None] = Text(
        default=None,
        nullable=True,
    )
    """Retry-backoff time; when set it overrides `next_fire_at` for due-ness."""
    last_error: ScheduledTrigger.Col[str | None] = Text(default=None, nullable=True)
    """The most recent dispatch failure message, for diagnostics."""
    version: ScheduledTrigger.Col[PositiveInt] = Integer(default=1)
    """Version number used for optimistic concurrency control."""
    created_at: ScheduledTrigger.GenCol[datetime] = Text(default=CurrentTimestamp)
    updated_at: ScheduledTrigger.GenCol[datetime] = Text(default=CurrentTimestamp)
    deleted_at: ScheduledTrigger.Col[datetime | None] = Text(
        default=None,
        nullable=True,
    )

    __indexes__: ClassVar = [Index(status, next_fire_at)]


def _due_predicate(now: datetime) -> Predicate[ScheduledTrigger[Pending]]:
    """Build the WHERE predicate selecting live, unclaimed, due triggers.

    A trigger is due when it is active, not soft-deleted, not already claimed,
    and its effective fire time — the retry clock when a retry is pending, else
    the recurrence clock — has arrived.
    """
    retry_due = ScheduledTrigger.next_attempt_at.is_not_null() & (
        ScheduledTrigger.next_attempt_at.lte(now)
    )
    fire_due = ScheduledTrigger.next_attempt_at.is_null() & (
        ScheduledTrigger.next_fire_at.lte(now)
    )
    return (
        ScheduledTrigger.deleted_at.is_null()
        & ScheduledTrigger.status.eq("active")
        & ScheduledTrigger.claimed_at.is_null()
        & (retry_due | fire_due)
    )


class TriggerService:
    """Capability surface for Scheduled triggers, over a snekql database.

    Human-facing mutations own one transaction each and return the resulting
    row. Scheduler-facing methods (`claim_due`, `record_success`,
    `record_failure`) drive the dispatch state machine and are the only writers
    of `claimed_at`, `attempts`, and `next_attempt_at`.
    """

    def __init__(
        self,
        database: Database,
        tracer: Tracer,
        event_publisher: EventPublisher | None = None,
    ) -> None:
        self.database: Database = database
        self.event_publisher: EventPublisher = event_publisher or NullEventPublisher()
        self.tracer: Tracer = tracer

    async def create(
        self,
        spec: TriggerSpec,
        *,
        now: datetime,
        logger: Logger,
    ) -> ScheduledTrigger[Fetched]:
        """Create an active trigger, materialising its first occurrence.

        The time spec is validated against the recurrence before any write, so a
        malformed spec never reaches the table.
        """
        normalised_payload = spec.payload.strip()
        if not normalised_payload:
            message = "trigger payload must not be blank"
            raise InvalidTriggerSpecError(message)
        facts = _describe_schedule(spec, now=now)
        with self.tracer.start_as_current_span(
            "TriggerService.create",
            attributes={
                "trigger.recurrence": spec.recurrence,
                "trigger.action_kind": spec.action_kind,
            },
        ) as span:
            _debug(
                logger,
                "Creating Scheduled trigger",
                recurrence=spec.recurrence,
                action_kind=spec.action_kind,
            )
            async with self.database.transaction() as tx:
                trigger = await tx.execute(
                    insert(
                        ScheduledTrigger(
                            recurrence=spec.recurrence,
                            action_kind=spec.action_kind,
                            payload=normalised_payload,
                            timezone=facts.timezone,
                            wall_time=facts.wall_time,
                            weekday=facts.weekday,
                            next_fire_at=facts.next_fire_at,
                            status="active",
                        )
                    ).returning()
                )
            span.set_attribute("trigger.id", str(trigger.id))
            _info(
                logger,
                "Scheduled trigger created",
                trigger_id=str(trigger.id),
                recurrence=spec.recurrence,
                action_kind=spec.action_kind,
                next_fire_at=trigger.next_fire_at.isoformat(),
            )
        await self.event_publisher.publish(InvalidateEvent(keys=["triggers"]))
        return trigger

    async def list_triggers(
        self, *, limit: int | None = None, logger: Logger
    ) -> list[ScheduledTrigger[Fetched]]:
        """List live (non-deleted) triggers, soonest next fire first.

        `limit` caps the rows returned (`None` is unbounded); assistant-facing
        callers pass a bound so a crowded schedule can't flood the model.
        """
        _debug(logger, "Listing Scheduled triggers")
        query = (
            select(ScheduledTrigger)
            .where(ScheduledTrigger.deleted_at.is_null())
            .order_by(ScheduledTrigger.next_fire_at.asc())
        )
        if limit is not None:
            query = query.limit(limit)
        async with self.database.transaction() as tx:
            triggers = await tx.fetch_all(query)
        _debug(logger, "Scheduled trigger list completed", result_count=len(triggers))
        return triggers

    async def fetch(self, trigger_id: UUID7) -> ScheduledTrigger[Fetched]:
        """Fetch a live trigger by id, or raise when absent or deleted."""
        async with self.database.transaction() as tx:
            return await self._fetch_live(tx, trigger_id)

    async def update(
        self,
        trigger: ScheduledTrigger[Fetched],
        spec: TriggerSpec,
        *,
        now: datetime,
        logger: Logger,
    ) -> ScheduledTrigger[Fetched]:
        """Replace a trigger's definition at an observed version.

        Updating re-arms the trigger: the new time spec is re-materialised and
        the scheduler state (status, claim, retry counters) is reset, so an edit
        to a completed or mid-retry trigger starts cleanly from its next
        occurrence. A stale observed version conflicts; an absent trigger raises.
        """
        normalised_payload = spec.payload.strip()
        if not normalised_payload:
            message = "trigger payload must not be blank"
            raise InvalidTriggerSpecError(message)
        facts = _describe_schedule(spec, now=now)
        _debug(
            logger,
            "Updating Scheduled trigger",
            trigger_id=str(trigger.id),
            observed_version=trigger.version,
        )
        async with self.database.transaction() as tx:
            matched = await tx.execute(
                update(ScheduledTrigger)
                .set(ScheduledTrigger.recurrence.to(spec.recurrence))
                .set(ScheduledTrigger.action_kind.to(spec.action_kind))
                .set(ScheduledTrigger.payload.to(normalised_payload))
                .set(ScheduledTrigger.timezone.to(facts.timezone))
                .set(ScheduledTrigger.wall_time.to(facts.wall_time))
                .set(ScheduledTrigger.weekday.to(facts.weekday))
                .set(ScheduledTrigger.next_fire_at.to(facts.next_fire_at))
                .set(ScheduledTrigger.status.to("active"))
                .set(ScheduledTrigger.claimed_at.to(None))
                .set(ScheduledTrigger.attempts.to(0))
                .set(ScheduledTrigger.next_attempt_at.to(None))
                .set(ScheduledTrigger.last_error.to(None))
                .set(ScheduledTrigger.version.to(trigger.version + 1))
                .set(ScheduledTrigger.updated_at.to(CurrentTimestamp))
                .where(ScheduledTrigger.id.eq(trigger.id))
                .where(ScheduledTrigger.deleted_at.is_null())
                .where(ScheduledTrigger.version.eq(trigger.version))
            )
            fresh = await self._fetch_live(tx, trigger.id)
            if matched == 0:
                self._raise_version_conflict(trigger, fresh, logger=logger)
        _info(
            logger,
            "Scheduled trigger updated",
            trigger_id=str(fresh.id),
            version=fresh.version,
            next_fire_at=fresh.next_fire_at.isoformat(),
        )
        await self.event_publisher.publish(InvalidateEvent(keys=["triggers"]))
        return fresh

    async def delete(
        self,
        trigger: ScheduledTrigger[Fetched],
        *,
        now: datetime,
        logger: Logger,
    ) -> ScheduledTrigger[Fetched]:
        """Soft-delete a trigger at an observed version, convergently.

        Deleting an already-deleted trigger is a no-op, not an error (re-asserting
        the end-state converges). A stale observed version on a still-live trigger
        conflicts; an absent trigger raises.
        """
        _debug(
            logger,
            "Deleting Scheduled trigger",
            trigger_id=str(trigger.id),
            observed_version=trigger.version,
        )
        async with self.database.transaction() as tx:
            current = await tx.fetch_one_or_none(
                select(ScheduledTrigger).where(ScheduledTrigger.id.eq(trigger.id))
            )
            if current is None:
                raise TriggerNotFoundError(trigger.id)
            if current.deleted_at is not None:
                return current
            matched = await tx.execute(
                update(ScheduledTrigger)
                .set(ScheduledTrigger.deleted_at.to(now))
                .set(ScheduledTrigger.claimed_at.to(None))
                .set(ScheduledTrigger.version.to(trigger.version + 1))
                .set(ScheduledTrigger.updated_at.to(CurrentTimestamp))
                .where(ScheduledTrigger.id.eq(trigger.id))
                .where(ScheduledTrigger.deleted_at.is_null())
                .where(ScheduledTrigger.version.eq(trigger.version))
            )
            current = await tx.fetch_one_or_none(
                select(ScheduledTrigger).where(ScheduledTrigger.id.eq(trigger.id))
            )
            assert current is not None
            if matched == 0:
                self._raise_version_conflict(trigger, current, logger=logger)
        _info(
            logger,
            "Scheduled trigger deleted",
            trigger_id=str(current.id),
            version=current.version,
        )
        await self.event_publisher.publish(InvalidateEvent(keys=["triggers"]))
        return current

    async def claim_due(
        self,
        now: datetime,
        *,
        limit: PositiveInt = 32,
    ) -> list[ScheduledTrigger[Fetched]]:
        """Atomically claim up to `limit` due triggers, stamping `claimed_at`.

        Each candidate is claimed with a conditional update guarded on
        `claimed_at IS NULL`, so a row already in flight is skipped — the claim,
        not the dispatch, is what makes at-least-once delivery safe.
        """
        claimed: list[ScheduledTrigger[Fetched]] = []
        async with self.database.transaction() as tx:
            candidates = await tx.fetch_all(
                select(ScheduledTrigger)
                .where(_due_predicate(now))
                .order_by(ScheduledTrigger.next_fire_at.asc())
                .limit(limit)
            )
            for candidate in candidates:
                matched = await tx.execute(
                    update(ScheduledTrigger)
                    .set(ScheduledTrigger.claimed_at.to(now))
                    .set(ScheduledTrigger.updated_at.to(CurrentTimestamp))
                    .where(ScheduledTrigger.id.eq(candidate.id))
                    .where(ScheduledTrigger.claimed_at.is_null())
                )
                if matched == 1:
                    claimed.append(await self._fetch_live(tx, candidate.id))
        return claimed

    async def record_success(
        self,
        trigger: ScheduledTrigger[Fetched],
        *,
        now: datetime,
    ) -> ScheduledTrigger[Fetched]:
        """Settle a claimed trigger after a successful dispatch.

        A `once` trigger becomes `completed`; a recurring trigger re-arms onto
        its next occurrence. Either way the claim and retry counters are cleared.
        """
        statement = (
            update(ScheduledTrigger)
            .set(ScheduledTrigger.claimed_at.to(None))
            .set(ScheduledTrigger.next_attempt_at.to(None))
            .set(ScheduledTrigger.attempts.to(0))
            .set(ScheduledTrigger.last_error.to(None))
            .set(ScheduledTrigger.version.to(trigger.version + 1))
            .set(ScheduledTrigger.updated_at.to(CurrentTimestamp))
        )
        if trigger.recurrence == "once":
            statement = statement.set(ScheduledTrigger.status.to("completed"))
        else:
            statement = statement.set(
                ScheduledTrigger.next_fire_at.to(self._reschedule(trigger, now))
            )
        return await self._apply_scheduler_update(trigger.id, statement)

    async def record_failure(
        self,
        trigger: ScheduledTrigger[Fetched],
        *,
        now: datetime,
        error: str,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        backoff_base: timedelta = DEFAULT_BACKOFF_BASE,
    ) -> ScheduledTrigger[Fetched]:
        """Settle a claimed trigger after a failed dispatch.

        Below `max_attempts` the occurrence is backed off via `next_attempt_at`
        (exponential in the attempt count) and retried. Once attempts are
        exhausted the scheduler gives up on the occurrence: a `once` trigger
        becomes `failed`; a recurring trigger skips ahead to its next occurrence
        rather than wedging on a bad one.
        """
        attempts = trigger.attempts + 1
        statement = (
            update(ScheduledTrigger)
            .set(ScheduledTrigger.claimed_at.to(None))
            .set(ScheduledTrigger.last_error.to(error))
            .set(ScheduledTrigger.version.to(trigger.version + 1))
            .set(ScheduledTrigger.updated_at.to(CurrentTimestamp))
        )
        if attempts >= max_attempts:
            statement = statement.set(ScheduledTrigger.attempts.to(0)).set(
                ScheduledTrigger.next_attempt_at.to(None)
            )
            if trigger.recurrence == "once":
                statement = statement.set(ScheduledTrigger.status.to("failed"))
            else:
                statement = statement.set(
                    ScheduledTrigger.next_fire_at.to(self._reschedule(trigger, now))
                )
        else:
            backoff = backoff_base * (2 ** (attempts - 1))
            statement = statement.set(ScheduledTrigger.attempts.to(attempts)).set(
                ScheduledTrigger.next_attempt_at.to(now + backoff)
            )
        return await self._apply_scheduler_update(trigger.id, statement)

    def _reschedule(
        self, trigger: ScheduledTrigger[Fetched], now: datetime
    ) -> datetime:
        """Materialise a recurring trigger's next occurrence after `now`."""
        assert trigger.recurrence in ("daily", "weekly")
        assert trigger.wall_time is not None
        return next_recurring_fire(
            trigger.recurrence,
            timezone=trigger.timezone,
            wall_time=_parse_wall_time(trigger.wall_time),
            weekday=trigger.weekday,
            after=now,
        )

    async def _apply_scheduler_update(
        self,
        trigger_id: UUID7,
        statement: UpdateQuery[ScheduledTrigger[Pending]],
    ) -> ScheduledTrigger[Fetched]:
        """Run a scheduler-state update against one live row, returning it fresh.

        The update is scoped to the target id and skips a row a concurrent delete
        has already retired, so settling a claimed trigger never resurrects one
        the human removed mid-dispatch.
        """
        async with self.database.transaction() as tx:
            _ = await tx.execute(
                statement.where(ScheduledTrigger.id.eq(trigger_id)).where(
                    ScheduledTrigger.deleted_at.is_null()
                )
            )
            return await self._fetch_any(tx, trigger_id)

    def _raise_version_conflict(
        self,
        observed: ScheduledTrigger[Fetched],
        current: ScheduledTrigger[Fetched],
        *,
        logger: Logger,
    ) -> None:
        """Raise the optimistic-concurrency conflict for a stale write."""
        _debug(
            logger,
            "Scheduled trigger version conflict",
            trigger_id=str(observed.id),
            observed_version=observed.version,
            current_version=current.version,
        )
        message = (
            f"Tried to update trigger {observed.id} with version "
            f"{observed.version} but it had version {current.version}"
        )
        raise TriggerConflictError(message)

    async def _fetch_live(
        self,
        tx: Transaction,
        trigger_id: UUID7,
    ) -> ScheduledTrigger[Fetched]:
        """Fetch a non-deleted trigger by id or raise."""
        trigger = await tx.fetch_one_or_none(
            select(ScheduledTrigger)
            .where(ScheduledTrigger.id.eq(trigger_id))
            .where(ScheduledTrigger.deleted_at.is_null())
        )
        if trigger is None:
            raise TriggerNotFoundError(trigger_id)
        return trigger

    async def _fetch_any(
        self,
        tx: Transaction,
        trigger_id: UUID7,
    ) -> ScheduledTrigger[Fetched]:
        """Fetch a trigger by id in any state, or raise when genuinely absent."""
        trigger = await tx.fetch_one_or_none(
            select(ScheduledTrigger).where(ScheduledTrigger.id.eq(trigger_id))
        )
        if trigger is None:
            raise TriggerNotFoundError(trigger_id)
        return trigger


async def create_trigger_schema(database: Database) -> None:
    """Create the Scheduled trigger table and its index on an initialized DB.

    Applied as its own ordered migrations after the earlier schemas. Scaffolding
    emits one statement per table/index, and a snekql migration body runs exactly
    one statement, so each becomes its own ordered migration.

    >>> database = await Database.initialize(backend=Config(database=":memory:"))
    >>> await create_trigger_schema(database)
    """
    migrations = {
        f"005_{label}": sql
        for label, sql in scaffold_sqlite_statements([ScheduledTrigger])
    }
    await database.migrate(migrations)
