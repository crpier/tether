"""Scheduled trigger lifecycle service over canonical SQLite state."""

from __future__ import annotations

from datetime import datetime, timedelta

from opentelemetry.trace import Tracer
from pydantic import UUID7, PositiveInt
from snekql.sqlite import (
    CurrentTimestamp,
    Database,
    Fetched,
    Pending,
    Transaction,
    UpdateQuery,
    insert,
    select,
    update,
)

from tether.events import EventPublisher, InvalidateEvent, NullEventPublisher
from tether.structured_logging import Logger
from tether.trigger_schedule import (
    DailyTriggerSpec,
    InvalidTriggerSpecError,
    TriggerSpec,
    WeeklyTriggerSpec,
    materialize_trigger_schedule,
)
from tether.trigger_store import (
    ScheduledTrigger,
    due_trigger_predicate,
)

DEFAULT_MAX_ATTEMPTS = 5
"""Dispatch attempts at one occurrence before the scheduler gives up on it."""

DEFAULT_BACKOFF_BASE = timedelta(seconds=30)
"""First retry delay; subsequent retries double it (exponential backoff)."""


class TriggerNotFoundError(Exception):
    """Raised when an operation targets a trigger that does not exist."""


class TriggerConflictError(Exception):
    """Raised when an observed trigger version is stale."""


def _debug(logger: Logger, event: str, **context: object) -> None:
    """Emit a debug event using caller-supplied logging context."""
    logger.debug(event, **context)


def _info(logger: Logger, event: str, **context: object) -> None:
    """Emit an info event using caller-supplied logging context."""
    logger.info(event, **context)


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
        model_profile: str | None = None,
    ) -> ScheduledTrigger[Fetched]:
        """Create an active trigger, materialising its first occurrence.

        The time spec is validated against the recurrence before any write, so a
        malformed spec never reaches the table.
        """
        normalised_payload = spec.payload.strip()
        if not normalised_payload:
            message = "trigger payload must not be blank"
            raise InvalidTriggerSpecError(message)
        facts = materialize_trigger_schedule(spec, now=now)
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

            async def _create(tx: Transaction) -> ScheduledTrigger[Fetched]:
                return await tx.execute(
                    insert(
                        ScheduledTrigger(
                            recurrence=spec.recurrence,
                            action_kind=spec.action_kind,
                            payload=normalised_payload,
                            model_profile=model_profile,
                            timezone=facts.timezone,
                            wall_time=facts.wall_time,
                            weekday=facts.weekday,
                            next_fire_at=facts.next_fire_at,
                            status="active",
                        )
                    ).returning()
                )

            async with self.database.transaction(mode="immediate") as tx:
                trigger = await _create(tx)
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
        model_profile: str | None = None,
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
        facts = materialize_trigger_schedule(spec, now=now)
        _debug(
            logger,
            "Updating Scheduled trigger",
            trigger_id=str(trigger.id),
            observed_version=trigger.version,
        )

        async def _update(tx: Transaction) -> ScheduledTrigger[Fetched]:
            matched = await tx.execute(
                update(ScheduledTrigger)
                .set(ScheduledTrigger.recurrence.to(spec.recurrence))
                .set(ScheduledTrigger.action_kind.to(spec.action_kind))
                .set(ScheduledTrigger.payload.to(normalised_payload))
                .set(ScheduledTrigger.model_profile.to(model_profile))
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
            return fresh

        async with self.database.transaction(mode="immediate") as tx:
            fresh = await _update(tx)
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

        async def _delete(tx: Transaction) -> ScheduledTrigger[Fetched]:
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
            return current

        async with self.database.transaction(mode="immediate") as tx:
            current = await _delete(tx)
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

        async def _claim_due(tx: Transaction) -> list[ScheduledTrigger[Fetched]]:
            claimed: list[ScheduledTrigger[Fetched]] = []
            candidates = await tx.fetch_all(
                select(ScheduledTrigger)
                .where(due_trigger_predicate(now))
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

        async with self.database.transaction(mode="immediate") as tx:
            return await _claim_due(tx)

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
        settled = await self._apply_scheduler_update(trigger.id, statement)
        # Firing bumps the version and (for `once`) the status, so a browser
        # holding the pre-fire row must refetch — else its stale version 409s on
        # delete and its list never flips to `completed`.
        await self.event_publisher.publish(InvalidateEvent(keys=["triggers"]))
        return settled

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
        settled = await self._apply_scheduler_update(trigger.id, statement)
        # A failed dispatch also bumps the version, so a browser holding the
        # pre-fire row must refetch to stay deletable.
        await self.event_publisher.publish(InvalidateEvent(keys=["triggers"]))
        return settled

    def _reschedule(
        self, trigger: ScheduledTrigger[Fetched], now: datetime
    ) -> datetime:
        """Materialise a recurring trigger's next occurrence after `now`."""
        if trigger.wall_time is None:
            message = f"recurring trigger {trigger.id} has no wall time"
            raise InvalidTriggerSpecError(message)
        if trigger.recurrence == "daily":
            spec: TriggerSpec = DailyTriggerSpec(
                action_kind=trigger.action_kind,
                payload=trigger.payload,
                timezone=trigger.timezone,
                time_of_day=trigger.wall_time,
            )
        elif trigger.recurrence == "weekly" and trigger.weekday is not None:
            spec = WeeklyTriggerSpec(
                action_kind=trigger.action_kind,
                payload=trigger.payload,
                timezone=trigger.timezone,
                time_of_day=trigger.wall_time,
                weekday=trigger.weekday,
            )
        else:
            message = f"trigger {trigger.id} has invalid recurring state"
            raise InvalidTriggerSpecError(message)
        return materialize_trigger_schedule(spec, now=now).next_fire_at

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

        async def _apply(tx: Transaction) -> ScheduledTrigger[Fetched]:
            _ = await tx.execute(
                statement.where(ScheduledTrigger.id.eq(trigger_id)).where(
                    ScheduledTrigger.deleted_at.is_null()
                )
            )
            return await self._fetch_any(tx, trigger_id)

        async with self.database.transaction(mode="immediate") as tx:
            return await _apply(tx)

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
