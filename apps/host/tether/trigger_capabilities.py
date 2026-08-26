"""The Scheduled trigger domain's capability descriptor.

The pieces the REST routes (`tether.trigger_routes`) and the internal tools
(`tether.trigger_tools`) both need live here once: the `TriggerRead` model,
the shared time-spec body (`TriggerSpecBody`, which both surfaces' request
models inherit), the detached-reference builder, the domain→code map
(`TRIGGER_ERRORS`), and one execute function per capability — the service call
plus its Read-model rendering.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, PositiveInt
from snekql.sqlite import Fetched
from starlette.requests import Request

from tether.app_runtime import app_runtime
from tether.capability_contracts import CapabilityOutcome, ErrorRule
from tether.conversation_model import ConversationKind, ConversationTurnStatus
from tether.structured_logging import get_request_logger
from tether.trigger_schedule import (
    DailyTriggerSpec,
    InvalidTriggerSpecError,
    OnceTriggerSpec,
    TriggerActionKind,
    TriggerRecurrence,
    TriggerSpec,
    WeeklyTriggerSpec,
)
from tether.trigger_store import (
    OccurrenceStatus,
    PushDeliveryStatus,
    ScheduledOccurrence,
    ScheduledTrigger,
    TriggerStatus,
)
from tether.triggers import (
    ScheduledPromptSnapshot,
    TriggerConflictError,
    TriggerNotFoundError,
)

TRIGGER_ERRORS: tuple[ErrorRule, ...] = (
    ErrorRule((TriggerNotFoundError,), "not_found", 404, detail="trigger not found"),
    ErrorRule((TriggerConflictError,), "conflict", 409),
    ErrorRule((InvalidTriggerSpecError,), "invalid_input", 422),
)
"""The trigger domain→code map both surfaces translate failures through."""


class TriggerSpecBody(BaseModel):
    """The shared time-spec + action fields for creating or updating a trigger.

    >>> TriggerSpecBody(
    ...     recurrence="daily",
    ...     action_kind="message",
    ...     payload="stand up",
    ...     timezone="UTC",
    ...     time_of_day="09:00",
    ... ).recurrence
    'daily'
    """

    recurrence: TriggerRecurrence
    action_kind: TriggerActionKind
    payload: str
    timezone: str | None = None
    time_of_day: str | None = None
    weekday: int | None = None
    fire_at: AwareDatetime | None = None

    def to_spec(self) -> TriggerSpec:
        """Validate recurrence-specific fields into a strict domain definition."""
        if self.recurrence == "once":
            if self.fire_at is None:
                message = "a once trigger requires fire_at"
                raise InvalidTriggerSpecError(message)
            if self.time_of_day is not None or self.weekday is not None:
                message = "a once trigger takes neither a time of day nor a weekday"
                raise InvalidTriggerSpecError(message)
            return OnceTriggerSpec(
                action_kind=self.action_kind,
                payload=self.payload,
                fire_at=self.fire_at,
                timezone=self.timezone,
            )
        if self.fire_at is not None:
            message = f"a {self.recurrence} trigger does not take fire_at"
            raise InvalidTriggerSpecError(message)
        if self.timezone is None or self.time_of_day is None:
            message = (
                f"a {self.recurrence} trigger requires a timezone and a time of day"
            )
            raise InvalidTriggerSpecError(message)
        if self.recurrence == "daily":
            if self.weekday is not None:
                message = "a daily trigger does not take a weekday"
                raise InvalidTriggerSpecError(message)
            return DailyTriggerSpec(
                action_kind=self.action_kind,
                payload=self.payload,
                timezone=self.timezone,
                time_of_day=self.time_of_day,
            )
        if self.weekday is None:
            message = "a weekly trigger requires a weekday"
            raise InvalidTriggerSpecError(message)
        return WeeklyTriggerSpec(
            action_kind=self.action_kind,
            payload=self.payload,
            timezone=self.timezone,
            time_of_day=self.time_of_day,
            weekday=self.weekday,
        )


class OccurrenceTurnRead(BaseModel):
    """Linked Conversation-turn outcome shown with one occurrence."""

    id: UUID
    status: ConversationTurnStatus
    failure_code: str | None
    failure_summary: str | None


class ScheduledOccurrenceRead(BaseModel):
    """Inspectable execution and delivery outcome for one firing."""

    id: UUID
    intended_fire_at: datetime
    trigger_id: UUID
    trigger_version: PositiveInt
    action_kind: TriggerActionKind
    payload: str
    target_conversation_id: UUID | None
    target_conversation_kind: ConversationKind | None
    target_conversation_name: str | None
    model_profile: str | None
    status: OccurrenceStatus
    failure_code: str | None
    failure_summary: str | None
    answer_message_id: UUID | None
    push_status: PushDeliveryStatus
    push_attempts: int
    push_error: str | None
    turn: OccurrenceTurnRead | None


class TriggerRead(BaseModel):
    """HTTP representation of a Scheduled trigger and its latest firing."""

    id: UUID
    recurrence: TriggerRecurrence
    action_kind: TriggerActionKind
    payload: str
    model_profile: str | None
    target_conversation_id: UUID | None
    target_conversation_name: str | None
    latest_occurrence: ScheduledOccurrenceRead | None
    timezone: str
    wall_time: str | None
    weekday: int | None
    next_fire_at: datetime
    status: TriggerStatus
    attempts: int
    next_attempt_at: datetime | None
    last_error: str | None
    version: PositiveInt
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_trigger(
        cls,
        trigger: ScheduledTrigger[Fetched],
        *,
        latest_occurrence: ScheduledOccurrenceRead | None = None,
        target_conversation_name: str | None = None,
    ) -> TriggerRead:
        """Render a stored trigger with optional related summaries."""
        return cls(
            id=trigger.id,
            recurrence=trigger.recurrence,
            action_kind=trigger.action_kind,
            payload=trigger.payload,
            model_profile=trigger.model_profile,
            target_conversation_id=trigger.target_conversation_id,
            target_conversation_name=target_conversation_name,
            latest_occurrence=latest_occurrence,
            timezone=trigger.timezone,
            wall_time=trigger.wall_time,
            weekday=trigger.weekday,
            next_fire_at=trigger.next_fire_at,
            status=trigger.status,
            attempts=trigger.attempts,
            next_attempt_at=trigger.next_attempt_at,
            last_error=trigger.last_error,
            version=trigger.version,
            created_at=trigger.created_at,
            updated_at=trigger.updated_at,
        )


def _trigger_reference(
    trigger_id: UUID, version: PositiveInt
) -> ScheduledTrigger[Fetched]:
    """Build a detached trigger carrying only the identity a mutation acts on.

    Update/Delete read just `id` and `version` to run their optimistic-
    concurrency check and re-fetch the live row, so a hand-built reference is
    enough; the other columns are required placeholders with no role here.
    """
    return cast(
        "ScheduledTrigger[Fetched]",
        ScheduledTrigger.construct(
            id=trigger_id,
            version=version,
            recurrence="once",
            action_kind="message",
            payload="",
            timezone="UTC",
            next_fire_at=datetime(1970, 1, 1, tzinfo=UTC),
            status="active",
            attempts=0,
        ),
    )


async def _occurrence_read(
    request: Request,
    occurrence: ScheduledOccurrence[Fetched],
) -> ScheduledOccurrenceRead:
    runtime = app_runtime(request.app)
    turn = await runtime.trigger_service.fetch_occurrence_turn(occurrence.id)
    target = (
        None
        if occurrence.target_conversation_id is None
        else await runtime.conversation_service.fetch_conversation(
            occurrence.target_conversation_id
        )
    )
    return ScheduledOccurrenceRead(
        id=occurrence.id,
        intended_fire_at=occurrence.intended_fire_at,
        trigger_id=occurrence.trigger_id,
        trigger_version=occurrence.trigger_version,
        action_kind=occurrence.action_kind,
        payload=occurrence.payload,
        target_conversation_id=occurrence.target_conversation_id,
        target_conversation_kind=None if target is None else target.kind,
        target_conversation_name=(
            None
            if target is None
            else "Main"
            if target.kind == "main"
            else target.display_name
        ),
        model_profile=occurrence.model_profile,
        status=occurrence.status,
        failure_code=occurrence.failure_code,
        failure_summary=occurrence.failure_summary,
        answer_message_id=occurrence.answer_message_id,
        push_status=occurrence.push_status,
        push_attempts=occurrence.push_attempts,
        push_error=occurrence.push_error,
        turn=(
            None
            if turn is None
            else OccurrenceTurnRead(
                id=turn.id,
                status=turn.status,
                failure_code=turn.failure_code,
                failure_summary=turn.failure_summary,
            )
        ),
    )


async def _trigger_read(
    request: Request,
    trigger: ScheduledTrigger[Fetched],
) -> TriggerRead:
    runtime = app_runtime(request.app)
    occurrence = await runtime.trigger_service.fetch_latest_occurrence(trigger.id)
    target_name: str | None = None
    if trigger.target_conversation_id is not None:
        target = await runtime.conversation_service.fetch_conversation(
            trigger.target_conversation_id
        )
        target_name = "Main" if target.kind == "main" else target.display_name
    return TriggerRead.from_trigger(
        trigger,
        latest_occurrence=(
            None if occurrence is None else await _occurrence_read(request, occurrence)
        ),
        target_conversation_name=target_name,
    )


async def read_occurrence(
    request: Request,
    occurrence_id: UUID,
) -> CapabilityOutcome:
    """Render one exact immutable Scheduled occurrence."""
    occurrence = await app_runtime(request.app).trigger_service.fetch_occurrence(
        occurrence_id
    )
    return CapabilityOutcome(
        result=(await _occurrence_read(request, occurrence)).model_dump(mode="json")
    )


async def _single(
    request: Request,
    trigger: ScheduledTrigger[Fetched],
) -> CapabilityOutcome:
    """Render a single-trigger outcome."""
    return CapabilityOutcome(
        result=(await _trigger_read(request, trigger)).model_dump(mode="json")
    )


async def _model_profile_for(
    request: Request,
    spec: TriggerSpec,
    target_conversation_id: UUID | None,
) -> str | None:
    """Pin the target's selected profile only for recurring prompts."""
    if spec.action_kind != "prompt" or spec.recurrence == "once":
        return None
    if target_conversation_id is None:
        message = "a prompt trigger requires a target Conversation"
        raise InvalidTriggerSpecError(message)
    runtime = app_runtime(request.app)
    conversation = await runtime.conversation_service.fetch_conversation(
        target_conversation_id
    )
    return conversation.selected_model or runtime.model_catalog.default_model


def invoking_conversation_id(request: Request) -> UUID:
    """Resolve a tool's server-owned Conversation from trace correlation."""
    runtime = app_runtime(request.app)
    run = runtime.trace_recorder.current_run(request.state.session_id)
    if run is None or run.conversation_id is None:
        message = "a prompt trigger requires an invoking Conversation"
        raise InvalidTriggerSpecError(message)
    return UUID(run.conversation_id)


async def create(
    request: Request,
    spec: TriggerSpec,
    *,
    target_conversation_id: UUID | None,
) -> CapabilityOutcome:
    """Create a Scheduled trigger."""
    runtime = app_runtime(request.app)
    trigger = await runtime.trigger_service.create(
        spec,
        now=datetime.now(UTC),
        logger=get_request_logger(request),
        prompt_snapshot=ScheduledPromptSnapshot(
            model_profile=await _model_profile_for(
                request,
                spec,
                target_conversation_id,
            ),
            target_conversation_id=target_conversation_id,
        ),
    )
    return await _single(request, trigger)


async def list_triggers(
    request: Request, limit: int | None = None
) -> CapabilityOutcome:
    """List live Scheduled triggers, soonest next fire first."""
    triggers = await app_runtime(request.app).trigger_service.list_triggers(
        limit=limit,
        logger=get_request_logger(request),
    )
    return CapabilityOutcome(
        result=[
            (await _trigger_read(request, trigger)).model_dump(mode="json")
            for trigger in triggers
        ]
    )


async def update(
    request: Request,
    trigger_id: UUID,
    spec: TriggerSpec,
    version: PositiveInt,
    *,
    target_conversation_id: UUID | None,
) -> CapabilityOutcome:
    """Replace a trigger's future definition at its observed version."""
    trigger = await app_runtime(request.app).trigger_service.update(
        _trigger_reference(trigger_id, version),
        spec,
        now=datetime.now(UTC),
        logger=get_request_logger(request),
        prompt_snapshot=ScheduledPromptSnapshot(
            model_profile=await _model_profile_for(
                request,
                spec,
                target_conversation_id,
            ),
            target_conversation_id=target_conversation_id,
        ),
    )
    return await _single(request, trigger)


async def delete(
    request: Request, trigger_id: UUID, version: PositiveInt
) -> CapabilityOutcome:
    """Delete a Scheduled trigger."""
    trigger = await app_runtime(request.app).trigger_service.delete(
        _trigger_reference(trigger_id, version),
        now=datetime.now(UTC),
        logger=get_request_logger(request),
    )
    return await _single(request, trigger)
