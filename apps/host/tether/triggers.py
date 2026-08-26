"""Scheduled trigger lifecycle service over canonical SQLite state."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

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

from tether.conversation_store import Conversation, ConversationTurn, Message
from tether.conversation_turns import ConversationTurns
from tether.events import EventPublisher, InvalidateEvent, NullEventPublisher
from tether.model_selection import AgentModelCatalog
from tether.structured_logging import Logger
from tether.trigger_schedule import (
    DailyTriggerSpec,
    InvalidTriggerSpecError,
    TriggerActionKind,
    TriggerSpec,
    WeeklyTriggerSpec,
    materialize_trigger_schedule,
)
from tether.trigger_store import (
    ScheduledOccurrence,
    ScheduledTrigger,
    due_trigger_predicate,
)

DEFAULT_MAX_ATTEMPTS = 5
"""Dispatch attempts at one occurrence before the scheduler gives up on it."""

DEFAULT_BACKOFF_BASE = timedelta(seconds=30)
"""First retry delay; subsequent retries double it (exponential backoff)."""


@dataclass(frozen=True, slots=True)
class ScheduledPromptSnapshot:
    """Target and optional pinned model saved with a prompt definition."""

    model_profile: str | None = None
    target_conversation_id: UUID | None = None


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
        *,
        conversation_turns: ConversationTurns | None = None,
        model_catalog: AgentModelCatalog | None = None,
    ) -> None:
        self.conversation_turns: ConversationTurns | None = conversation_turns
        self.database: Database = database
        self.event_publisher: EventPublisher = event_publisher or NullEventPublisher()
        self.model_catalog: AgentModelCatalog = model_catalog or AgentModelCatalog(
            default_model=None,
            models=(),
        )
        self.tracer: Tracer = tracer

    async def create(
        self,
        spec: TriggerSpec,
        *,
        now: datetime,
        logger: Logger,
        prompt_snapshot: ScheduledPromptSnapshot | None = None,
    ) -> ScheduledTrigger[Fetched]:
        """Create an active trigger, materialising its first occurrence.

        The time spec is validated against the recurrence before any write, so a
        malformed spec never reaches the table.
        """
        prompt_snapshot = prompt_snapshot or ScheduledPromptSnapshot()
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
                await self._validate_target(
                    tx,
                    action_kind=spec.action_kind,
                    target_conversation_id=prompt_snapshot.target_conversation_id,
                )
                return await tx.execute(
                    insert(
                        ScheduledTrigger(
                            recurrence=spec.recurrence,
                            action_kind=spec.action_kind,
                            payload=normalised_payload,
                            model_profile=prompt_snapshot.model_profile,
                            target_conversation_id=(
                                prompt_snapshot.target_conversation_id
                            ),
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
        prompt_snapshot: ScheduledPromptSnapshot | None = None,
    ) -> ScheduledTrigger[Fetched]:
        """Replace a trigger's definition at an observed version.

        Updating re-arms the trigger: the new time spec is re-materialised and
        the scheduler state (status, claim, retry counters) is reset, so an edit
        to a completed or mid-retry trigger starts cleanly from its next
        occurrence. A stale observed version conflicts; an absent trigger raises.
        """
        prompt_snapshot = prompt_snapshot or ScheduledPromptSnapshot()
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
            await self._validate_target(
                tx,
                action_kind=spec.action_kind,
                target_conversation_id=prompt_snapshot.target_conversation_id,
            )
            current = await self._fetch_live(tx, trigger.id)
            if current.recurrence == "once":
                active_occurrence = await tx.fetch_one_or_none(
                    select(ScheduledOccurrence)
                    .where(ScheduledOccurrence.trigger_id.eq(trigger.id))
                    .where(ScheduledOccurrence.status.in_("pending", "running"))
                    .limit(1)
                )
                if active_occurrence is not None:
                    message = (
                        "a one-off trigger cannot change while its occurrence is active"
                    )
                    raise TriggerConflictError(message)
            matched = await tx.execute(
                update(ScheduledTrigger)
                .set(ScheduledTrigger.recurrence.to(spec.recurrence))
                .set(ScheduledTrigger.action_kind.to(spec.action_kind))
                .set(ScheduledTrigger.payload.to(normalised_payload))
                .set(ScheduledTrigger.model_profile.to(prompt_snapshot.model_profile))
                .set(
                    ScheduledTrigger.target_conversation_id.to(
                        prompt_snapshot.target_conversation_id
                    )
                )
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

        async with self.database.transaction(mode="immediate") as transaction:
            current, fenced_turn_ids = await self._delete_in_transaction(
                transaction,
                trigger,
                now=now,
                logger=logger,
            )
        if self.conversation_turns is not None:
            for turn_id in fenced_turn_ids:
                await self.conversation_turns.observe_committed_cancellation(turn_id)
        _info(
            logger,
            "Scheduled trigger deleted",
            trigger_id=str(current.id),
            version=current.version,
        )
        await self.event_publisher.publish(InvalidateEvent(keys=["triggers"]))
        return current

    async def _delete_in_transaction(
        self,
        transaction: Transaction,
        trigger: ScheduledTrigger[Fetched],
        *,
        now: datetime,
        logger: Logger,
    ) -> tuple[ScheduledTrigger[Fetched], list[UUID]]:
        """Delete the definition and fence all not-yet-accepted prompt work."""
        current = await transaction.fetch_one_or_none(
            select(ScheduledTrigger).where(ScheduledTrigger.id.eq(trigger.id))
        )
        if current is None:
            raise TriggerNotFoundError(trigger.id)
        if current.deleted_at is not None:
            return current, []
        fenced_turn_ids: list[UUID] = []
        active_occurrences = await transaction.fetch_all(
            select(ScheduledOccurrence)
            .where(ScheduledOccurrence.trigger_id.eq(trigger.id))
            .where(ScheduledOccurrence.status.in_("pending", "running"))
        )
        for occurrence in active_occurrences:
            linked_turn = await transaction.fetch_one_or_none(
                select(ConversationTurn).where(
                    ConversationTurn.scheduled_occurrence_id.eq(occurrence.id)
                )
            )
            if linked_turn is not None and linked_turn.status == "pending":
                fenced_turn_ids.append(linked_turn.id)
                if linked_turn.acceptance_started_at is None:
                    _ = await transaction.execute(
                        update(ConversationTurn)
                        .set(
                            ConversationTurn.completed_at.to(now),
                            ConversationTurn.status.to("cancelled"),
                        )
                        .where(ConversationTurn.id.eq(linked_turn.id))
                        .where(ConversationTurn.status.eq("pending"))
                        .where(ConversationTurn.acceptance_started_at.is_null())
                    )
                else:
                    _ = await transaction.execute(
                        update(ConversationTurn)
                        .set(ConversationTurn.cancel_requested_at.to(now))
                        .where(ConversationTurn.id.eq(linked_turn.id))
                        .where(ConversationTurn.status.eq("pending"))
                    )
                await self._cancel_occurrence(transaction, occurrence.id, now=now)
            elif linked_turn is None and (
                occurrence.status == "pending" or occurrence.action_kind == "prompt"
            ):
                await self._cancel_occurrence(transaction, occurrence.id, now=now)
        matched = await transaction.execute(
            update(ScheduledTrigger)
            .set(ScheduledTrigger.deleted_at.to(now))
            .set(ScheduledTrigger.claimed_at.to(None))
            .set(ScheduledTrigger.version.to(trigger.version + 1))
            .set(ScheduledTrigger.updated_at.to(CurrentTimestamp))
            .where(ScheduledTrigger.id.eq(trigger.id))
            .where(ScheduledTrigger.deleted_at.is_null())
            .where(ScheduledTrigger.version.eq(trigger.version))
        )
        current = await transaction.fetch_one(
            select(ScheduledTrigger).where(ScheduledTrigger.id.eq(trigger.id))
        )
        if matched == 0:
            self._raise_version_conflict(trigger, current, logger=logger)
        return current, fenced_turn_ids

    async def _cancel_occurrence(
        self,
        transaction: Transaction,
        occurrence_id: UUID,
        *,
        now: datetime,
    ) -> None:
        """Fence one occurrence before its deleted definition becomes visible."""
        _ = await transaction.execute(
            update(ScheduledOccurrence)
            .set(
                ScheduledOccurrence.completed_at.to(now),
                ScheduledOccurrence.push_status.to("not_applicable"),
                ScheduledOccurrence.status.to("cancelled"),
            )
            .where(ScheduledOccurrence.id.eq(occurrence_id))
            .where(ScheduledOccurrence.status.in_("pending", "running"))
        )

    async def repair_occurrences(self, *, now: datetime) -> int:
        """Cancel deleted or orphaned work that cannot safely execute."""
        fenced_turn_ids: list[UUID] = []
        repaired = 0
        async with self.database.transaction(mode="immediate") as transaction:
            occurrences = await transaction.fetch_all(
                select(ScheduledOccurrence).where(
                    ScheduledOccurrence.status.in_("pending", "running")
                )
            )
            for occurrence in occurrences:
                trigger = await transaction.fetch_one_or_none(
                    select(ScheduledTrigger).where(
                        ScheduledTrigger.id.eq(occurrence.trigger_id)
                    )
                )
                linked_turn = await transaction.fetch_one_or_none(
                    select(ConversationTurn).where(
                        ConversationTurn.scheduled_occurrence_id.eq(occurrence.id)
                    )
                )
                if linked_turn is not None and linked_turn.status == "cancelled":
                    await self._cancel_occurrence(transaction, occurrence.id, now=now)
                    repaired += 1
                    continue
                definition_deleted = trigger is None or trigger.deleted_at is not None
                preserve_running_delivery = (
                    occurrence.action_kind == "message"
                    and occurrence.status == "running"
                )
                preserve_accepted_prompt = (
                    occurrence.action_kind == "prompt"
                    and linked_turn is not None
                    and linked_turn.status == "running"
                )
                if (
                    not definition_deleted
                    or preserve_running_delivery
                    or preserve_accepted_prompt
                ):
                    continue
                if linked_turn is not None and linked_turn.status == "pending":
                    fenced_turn_ids.append(linked_turn.id)
                    if linked_turn.acceptance_started_at is None:
                        _ = await transaction.execute(
                            update(ConversationTurn)
                            .set(
                                ConversationTurn.completed_at.to(now),
                                ConversationTurn.status.to("cancelled"),
                            )
                            .where(ConversationTurn.id.eq(linked_turn.id))
                            .where(ConversationTurn.status.eq("pending"))
                        )
                    else:
                        _ = await transaction.execute(
                            update(ConversationTurn)
                            .set(ConversationTurn.cancel_requested_at.to(now))
                            .where(ConversationTurn.id.eq(linked_turn.id))
                            .where(ConversationTurn.status.eq("pending"))
                        )
                await self._cancel_occurrence(transaction, occurrence.id, now=now)
                repaired += 1
        if self.conversation_turns is not None:
            for turn_id in fenced_turn_ids:
                await self.conversation_turns.observe_committed_cancellation(turn_id)
        return repaired

    async def claim_due(
        self,
        now: datetime,
        *,
        limit: PositiveInt = 32,
    ) -> list[ScheduledOccurrence[Fetched]]:
        """Atomically claim due definitions and materialize firing snapshots."""

        async def _claim_due(
            tx: Transaction,
        ) -> list[ScheduledOccurrence[Fetched]]:
            claimed: list[ScheduledOccurrence[Fetched]] = []
            retry_occurrences = await tx.fetch_all(
                select(ScheduledOccurrence)
                .where(ScheduledOccurrence.action_kind.eq("message"))
                .where(ScheduledOccurrence.status.eq("pending"))
                .where(ScheduledOccurrence.next_attempt_at.is_not_null())
                .where(ScheduledOccurrence.next_attempt_at.lte(now))
                .order_by(ScheduledOccurrence.next_attempt_at.asc())
                .limit(limit)
            )
            for occurrence in retry_occurrences:
                trigger = await tx.fetch_one_or_none(
                    select(ScheduledTrigger).where(
                        ScheduledTrigger.id.eq(occurrence.trigger_id)
                    )
                )
                if trigger is None or trigger.deleted_at is not None:
                    continue
                matched = await tx.execute(
                    update(ScheduledOccurrence)
                    .set(ScheduledOccurrence.next_attempt_at.to(None))
                    .where(ScheduledOccurrence.id.eq(occurrence.id))
                    .where(ScheduledOccurrence.status.eq("pending"))
                    .where(ScheduledOccurrence.next_attempt_at.lte(now))
                )
                if matched == 1:
                    claimed.append(
                        await tx.fetch_one(
                            select(ScheduledOccurrence).where(
                                ScheduledOccurrence.id.eq(occurrence.id)
                            )
                        )
                    )
            remaining = max(0, limit - len(claimed))
            if remaining == 0:
                return claimed
            candidates = await tx.fetch_all(
                select(ScheduledTrigger)
                .where(due_trigger_predicate(now))
                .order_by(ScheduledTrigger.next_fire_at.asc())
                .limit(remaining)
            )
            for candidate in candidates:
                matched = await tx.execute(
                    update(ScheduledTrigger)
                    .set(ScheduledTrigger.claimed_at.to(now))
                    .set(ScheduledTrigger.updated_at.to(CurrentTimestamp))
                    .where(ScheduledTrigger.id.eq(candidate.id))
                    .where(ScheduledTrigger.claimed_at.is_null())
                )
                if matched != 1:
                    continue
                occurrence = await tx.fetch_one_or_none(
                    select(ScheduledOccurrence)
                    .where(ScheduledOccurrence.trigger_id.eq(candidate.id))
                    .where(
                        ScheduledOccurrence.intended_fire_at.eq(candidate.next_fire_at)
                    )
                )
                if occurrence is None:
                    model_profile = candidate.model_profile
                    if (
                        candidate.action_kind == "prompt"
                        and candidate.recurrence == "once"
                        and candidate.target_conversation_id is not None
                    ):
                        target = await tx.fetch_one_or_none(
                            select(Conversation).where(
                                Conversation.id.eq(candidate.target_conversation_id)
                            )
                        )
                        model_profile = (
                            None
                            if target is None
                            else target.selected_model
                            or self.model_catalog.default_model
                        )
                    model_config = (
                        self.model_catalog.resolve(model_profile)
                        if self.model_catalog.models
                        else None
                    )
                    occurrence = await tx.execute(
                        insert(
                            ScheduledOccurrence(
                                action_kind=candidate.action_kind,
                                intended_fire_at=candidate.next_fire_at,
                                model_display_name_snapshot=(
                                    None
                                    if model_config is None
                                    else model_config.display_name
                                ),
                                model_id_snapshot=(
                                    None
                                    if model_config is None
                                    else model_config.model_id
                                ),
                                model_profile=model_profile,
                                model_provider_snapshot=(
                                    None
                                    if model_config is None
                                    else model_config.provider
                                ),
                                model_thinking_level_snapshot=(
                                    None
                                    if model_config is None
                                    else model_config.thinking_level
                                ),
                                payload=candidate.payload,
                                push_status=(
                                    "pending"
                                    if candidate.action_kind == "prompt"
                                    else "not_applicable"
                                ),
                                status="pending",
                                target_conversation_id=(
                                    candidate.target_conversation_id
                                ),
                                trigger_id=candidate.id,
                                trigger_version=candidate.version,
                            )
                        ).returning()
                    )
                claimed.append(occurrence)
            return claimed

        async with self.database.transaction(mode="immediate") as tx:
            claimed = await _claim_due(tx)
        await self._publish_claim_invalidation(claimed)
        return claimed

    async def _publish_claim_invalidation(
        self,
        claimed: list[ScheduledOccurrence[Fetched]],
    ) -> None:
        """Publish only when claiming changed occurrence lifecycle state."""
        if claimed:
            await self.event_publisher.publish(InvalidateEvent(keys=["triggers"]))

    async def record_running(
        self,
        occurrence: ScheduledOccurrence[Fetched],
    ) -> ScheduledOccurrence[Fetched]:
        """Mark a claimed occurrence running without changing its snapshot."""
        async with self.database.transaction(mode="immediate") as transaction:
            matched = await transaction.execute(
                update(ScheduledOccurrence)
                .set(
                    ScheduledOccurrence.started_at.to(CurrentTimestamp),
                    ScheduledOccurrence.status.to("running"),
                )
                .where(ScheduledOccurrence.id.eq(occurrence.id))
                .where(ScheduledOccurrence.status.eq("pending"))
            )
            current = await transaction.fetch_one(
                select(ScheduledOccurrence).where(
                    ScheduledOccurrence.id.eq(occurrence.id)
                )
            )
        if matched == 1:
            await self.event_publisher.publish(InvalidateEvent(keys=["triggers"]))
        return current

    async def record_success(
        self,
        occurrence: ScheduledOccurrence[Fetched],
        *,
        now: datetime,
        answer: str | None = None,
        answer_message_id: UUID | None = None,
    ) -> ScheduledOccurrence[Fetched]:
        """Settle execution once and advance only the snapshotted definition."""
        async with self.database.transaction(mode="immediate") as transaction:
            current = await transaction.fetch_one(
                select(ScheduledOccurrence).where(
                    ScheduledOccurrence.id.eq(occurrence.id)
                )
            )
            if current.status not in {"succeeded", "failed", "cancelled"}:
                _ = await transaction.execute(
                    update(ScheduledOccurrence)
                    .set(
                        ScheduledOccurrence.answer.to(answer),
                        ScheduledOccurrence.answer_message_id.to(answer_message_id),
                        ScheduledOccurrence.completed_at.to(now),
                        ScheduledOccurrence.failure_code.to(None),
                        ScheduledOccurrence.failure_summary.to(None),
                        ScheduledOccurrence.status.to("succeeded"),
                    )
                    .where(ScheduledOccurrence.id.eq(occurrence.id))
                )
                await self._advance_matching_trigger(
                    transaction,
                    occurrence,
                    now=now,
                    failure=None,
                )
            settled = await transaction.fetch_one(
                select(ScheduledOccurrence).where(
                    ScheduledOccurrence.id.eq(occurrence.id)
                )
            )
        await self.event_publisher.publish(InvalidateEvent(keys=["triggers"]))
        return settled

    async def record_failure(
        self,
        occurrence: ScheduledOccurrence[Fetched],
        *,
        now: datetime,
        error: str,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        backoff_base: timedelta = DEFAULT_BACKOFF_BASE,
    ) -> ScheduledOccurrence[Fetched]:
        """Fail prompts once; retain bounded delivery retries for fixed messages."""
        async with self.database.transaction(mode="immediate") as transaction:
            current = await transaction.fetch_one(
                select(ScheduledOccurrence).where(
                    ScheduledOccurrence.id.eq(occurrence.id)
                )
            )
            if current.status in {"succeeded", "failed", "cancelled"}:
                return current
            attempts = current.dispatch_attempts + 1
            terminal = occurrence.action_kind == "prompt" or attempts >= max_attempts
            _ = await transaction.execute(
                update(ScheduledOccurrence)
                .set(
                    ScheduledOccurrence.completed_at.to(now if terminal else None),
                    ScheduledOccurrence.dispatch_attempts.to(attempts),
                    ScheduledOccurrence.failure_code.to("execution_failed"),
                    ScheduledOccurrence.failure_summary.to(error),
                    ScheduledOccurrence.next_attempt_at.to(
                        None if terminal else now + backoff_base * (2 ** (attempts - 1))
                    ),
                    ScheduledOccurrence.push_status.to(
                        "not_applicable"
                        if terminal and occurrence.action_kind == "prompt"
                        else current.push_status
                    ),
                    ScheduledOccurrence.status.to("failed" if terminal else "pending"),
                )
                .where(ScheduledOccurrence.id.eq(occurrence.id))
            )
            if terminal:
                await self._advance_matching_trigger(
                    transaction,
                    occurrence,
                    now=now,
                    failure=error,
                )
            settled = await transaction.fetch_one(
                select(ScheduledOccurrence).where(
                    ScheduledOccurrence.id.eq(occurrence.id)
                )
            )
        await self.event_publisher.publish(InvalidateEvent(keys=["triggers"]))
        return settled

    async def _advance_matching_trigger(
        self,
        transaction: Transaction,
        occurrence: ScheduledOccurrence[Fetched],
        *,
        now: datetime,
        failure: str | None,
    ) -> None:
        """Advance only when no edit or delete superseded the occurrence snapshot."""
        trigger = await transaction.fetch_one_or_none(
            select(ScheduledTrigger).where(
                ScheduledTrigger.id.eq(occurrence.trigger_id)
            )
        )
        if (
            trigger is None
            or trigger.deleted_at is not None
            or trigger.version != occurrence.trigger_version
        ):
            return
        statement = (
            update(ScheduledTrigger)
            .set(
                ScheduledTrigger.attempts.to(0),
                ScheduledTrigger.claimed_at.to(None),
                ScheduledTrigger.last_error.to(failure),
                ScheduledTrigger.next_attempt_at.to(None),
                ScheduledTrigger.updated_at.to(CurrentTimestamp),
                ScheduledTrigger.version.to(trigger.version + 1),
            )
            .where(ScheduledTrigger.id.eq(trigger.id))
            .where(ScheduledTrigger.version.eq(trigger.version))
        )
        if trigger.recurrence == "once":
            statement = statement.set(
                ScheduledTrigger.status.to(
                    "failed" if failure is not None else "completed"
                )
            )
        else:
            statement = statement.set(
                ScheduledTrigger.next_fire_at.to(self._reschedule(trigger, now))
            )
        _ = await transaction.execute(statement)

    async def fetch_occurrence(
        self,
        occurrence_id: UUID,
    ) -> ScheduledOccurrence[Fetched]:
        """Return one immutable firing even after its definition is deleted."""
        async with self.database.transaction() as transaction:
            occurrence = await transaction.fetch_one_or_none(
                select(ScheduledOccurrence).where(
                    ScheduledOccurrence.id.eq(occurrence_id)
                )
            )
        if occurrence is None:
            raise TriggerNotFoundError(occurrence_id)
        return occurrence

    async def fetch_latest_occurrence(
        self,
        trigger_id: UUID,
    ) -> ScheduledOccurrence[Fetched] | None:
        """Return the newest firing for one trigger, if it has fired."""
        async with self.database.transaction() as transaction:
            return await transaction.fetch_one_or_none(
                select(ScheduledOccurrence)
                .where(ScheduledOccurrence.trigger_id.eq(trigger_id))
                .order_by(ScheduledOccurrence.created_at.desc())
                .limit(1)
            )

    async def fetch_occurrence_turn(
        self,
        occurrence_id: UUID,
    ) -> ConversationTurn[Fetched] | None:
        """Return the Conversation turn linked to one prompt occurrence."""
        async with self.database.transaction() as transaction:
            return await transaction.fetch_one_or_none(
                select(ConversationTurn).where(
                    ConversationTurn.scheduled_occurrence_id.eq(occurrence_id)
                )
            )

    async def fetch_occurrence_answer(
        self,
        occurrence_id: UUID,
    ) -> Message[Fetched] | None:
        """Return the durable assistant answer linked to an occurrence."""
        async with self.database.transaction() as transaction:
            turn = await transaction.fetch_one_or_none(
                select(ConversationTurn).where(
                    ConversationTurn.scheduled_occurrence_id.eq(occurrence_id)
                )
            )
            if turn is None:
                return None
            return await transaction.fetch_one_or_none(
                select(Message)
                .where(Message.turn_id.eq(turn.id))
                .where(Message.role.eq("assistant"))
                .order_by(Message.turn_message_seq.desc())
                .limit(1)
            )

    async def list_recoverable_occurrences(
        self,
    ) -> list[ScheduledOccurrence[Fetched]]:
        """List claimed execution that may safely resume by durable identity."""
        async with self.database.transaction() as transaction:
            occurrences = await transaction.fetch_all(
                select(ScheduledOccurrence)
                .where(ScheduledOccurrence.status.in_("pending", "running"))
                .order_by(ScheduledOccurrence.created_at.asc())
            )
        return [
            occurrence
            for occurrence in occurrences
            if occurrence.status == "running"
            or occurrence.action_kind == "prompt"
            or occurrence.next_attempt_at is None
        ]

    async def claim_due_push_occurrences(
        self,
        now: datetime,
    ) -> list[ScheduledOccurrence[Fetched]]:
        """Claim stored answers so repeated ticks cannot duplicate Web Push."""
        async with self.database.transaction(mode="immediate") as transaction:
            candidates = await transaction.fetch_all(
                select(ScheduledOccurrence)
                .where(ScheduledOccurrence.status.eq("succeeded"))
                .where(ScheduledOccurrence.push_status.in_("pending", "failed"))
                .where(
                    ScheduledOccurrence.push_next_attempt_at.is_null()
                    | ScheduledOccurrence.push_next_attempt_at.lte(now)
                )
                .order_by(ScheduledOccurrence.created_at.asc())
            )
            claimed: list[ScheduledOccurrence[Fetched]] = []
            for candidate in candidates:
                matched = await transaction.execute(
                    update(ScheduledOccurrence)
                    .set(ScheduledOccurrence.push_status.to("delivering"))
                    .where(ScheduledOccurrence.id.eq(candidate.id))
                    .where(ScheduledOccurrence.push_status.in_("pending", "failed"))
                )
                if matched == 1:
                    claimed.append(
                        await transaction.fetch_one(
                            select(ScheduledOccurrence).where(
                                ScheduledOccurrence.id.eq(candidate.id)
                            )
                        )
                    )
            return claimed

    async def claim_prompt_push(
        self,
        occurrence_id: UUID,
    ) -> ScheduledOccurrence[Fetched] | None:
        """Claim one freshly succeeded answer before immediate delivery."""
        async with self.database.transaction(mode="immediate") as transaction:
            matched = await transaction.execute(
                update(ScheduledOccurrence)
                .set(ScheduledOccurrence.push_status.to("delivering"))
                .where(ScheduledOccurrence.id.eq(occurrence_id))
                .where(ScheduledOccurrence.status.eq("succeeded"))
                .where(ScheduledOccurrence.push_status.eq("pending"))
            )
            if matched == 0:
                return None
            return await transaction.fetch_one(
                select(ScheduledOccurrence).where(
                    ScheduledOccurrence.id.eq(occurrence_id)
                )
            )

    async def release_interrupted_pushes(self) -> None:
        """Make crash-interrupted stored-answer delivery retryable on startup."""
        async with self.database.transaction(mode="immediate") as transaction:
            _ = await transaction.execute(
                update(ScheduledOccurrence)
                .set(
                    ScheduledOccurrence.push_next_attempt_at.to(None),
                    ScheduledOccurrence.push_status.to("failed"),
                )
                .where(ScheduledOccurrence.status.eq("succeeded"))
                .where(ScheduledOccurrence.push_status.eq("delivering"))
            )

    async def record_push_delivered(
        self,
        occurrence_id: UUID,
        *,
        now: datetime,
    ) -> ScheduledOccurrence[Fetched]:
        """Settle Web Push independently from prompt execution."""
        async with self.database.transaction(mode="immediate") as transaction:
            _ = await transaction.execute(
                update(ScheduledOccurrence)
                .set(
                    ScheduledOccurrence.push_error.to(None),
                    ScheduledOccurrence.push_next_attempt_at.to(None),
                    ScheduledOccurrence.push_status.to("delivered"),
                    ScheduledOccurrence.pushed_at.to(now),
                )
                .where(ScheduledOccurrence.id.eq(occurrence_id))
                .where(ScheduledOccurrence.status.eq("succeeded"))
            )
            return await transaction.fetch_one(
                select(ScheduledOccurrence).where(
                    ScheduledOccurrence.id.eq(occurrence_id)
                )
            )

    async def record_push_failure(
        self,
        occurrence_id: UUID,
        *,
        now: datetime,
        error: str,
    ) -> ScheduledOccurrence[Fetched]:
        """Back off Web Push without making the successful turn retryable."""
        async with self.database.transaction(mode="immediate") as transaction:
            occurrence = await transaction.fetch_one(
                select(ScheduledOccurrence).where(
                    ScheduledOccurrence.id.eq(occurrence_id)
                )
            )
            attempts = occurrence.push_attempts + 1
            _ = await transaction.execute(
                update(ScheduledOccurrence)
                .set(
                    ScheduledOccurrence.push_attempts.to(attempts),
                    ScheduledOccurrence.push_error.to(error),
                    ScheduledOccurrence.push_next_attempt_at.to(
                        now + DEFAULT_BACKOFF_BASE * (2 ** (attempts - 1))
                    ),
                    ScheduledOccurrence.push_status.to("failed"),
                )
                .where(ScheduledOccurrence.id.eq(occurrence_id))
                .where(ScheduledOccurrence.status.eq("succeeded"))
            )
            return await transaction.fetch_one(
                select(ScheduledOccurrence).where(
                    ScheduledOccurrence.id.eq(occurrence_id)
                )
            )

    async def migrate_legacy_targets(self, main_conversation_id: UUID) -> None:
        """Backfill old prompt definitions to Main and clear fixed-message targets."""
        async with self.database.transaction(mode="immediate") as transaction:
            _ = await transaction.execute(
                update(ScheduledTrigger)
                .set(ScheduledTrigger.target_conversation_id.to(None))
                .where(ScheduledTrigger.action_kind.eq("message"))
            )
            _ = await transaction.execute(
                update(ScheduledTrigger)
                .set(ScheduledTrigger.target_conversation_id.to(main_conversation_id))
                .where(ScheduledTrigger.action_kind.eq("prompt"))
                .where(ScheduledTrigger.target_conversation_id.is_null())
            )

    async def _validate_target(
        self,
        transaction: Transaction,
        *,
        action_kind: TriggerActionKind,
        target_conversation_id: UUID | None,
    ) -> None:
        """Require active prompt targets and forbid targets on fixed messages."""
        if action_kind == "message":
            if target_conversation_id is not None:
                message = "a fixed-message trigger cannot target a Conversation"
                raise InvalidTriggerSpecError(message)
            return
        if target_conversation_id is None:
            message = "a prompt trigger requires a target Conversation"
            raise InvalidTriggerSpecError(message)
        target = await transaction.fetch_one_or_none(
            select(Conversation).where(Conversation.id.eq(target_conversation_id))
        )
        if target is None or target.status != "active":
            message = "a prompt trigger requires an active target Conversation"
            raise InvalidTriggerSpecError(message)

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
