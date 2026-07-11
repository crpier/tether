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
from starlette.requests import Request

from tether.capabilities import CapabilityOutcome, ErrorRule
from tether.logging import get_request_logger
from tether.triggers import (
    Fetched,
    InvalidTriggerSpecError,
    ScheduledTrigger,
    TriggerActionKind,
    TriggerConflictError,
    TriggerNotFoundError,
    TriggerRecurrence,
    TriggerSpec,
    TriggerStatus,
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
        """Project the validated fields onto the service's `TriggerSpec`."""
        return TriggerSpec(
            recurrence=self.recurrence,
            action_kind=self.action_kind,
            payload=self.payload,
            timezone=self.timezone,
            time_of_day=self.time_of_day,
            weekday=self.weekday,
            fire_at=self.fire_at,
        )


class TriggerRead(BaseModel):
    """HTTP representation of a Scheduled trigger."""

    id: UUID
    recurrence: TriggerRecurrence
    action_kind: TriggerActionKind
    payload: str
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
    def from_trigger(cls, trigger: ScheduledTrigger[Fetched]) -> TriggerRead:
        """Render a stored trigger as its HTTP representation."""
        return cls(
            id=trigger.id,
            recurrence=trigger.recurrence,
            action_kind=trigger.action_kind,
            payload=trigger.payload,
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


def _single(trigger: ScheduledTrigger[Fetched]) -> CapabilityOutcome:
    """Render a single-trigger outcome."""
    return CapabilityOutcome(
        result=TriggerRead.from_trigger(trigger).model_dump(mode="json")
    )


async def create(request: Request, spec: TriggerSpec) -> CapabilityOutcome:
    """Create a Scheduled trigger."""
    trigger = await request.app.state.trigger_service.create(
        spec,
        now=datetime.now(UTC),
        logger=get_request_logger(request),
    )
    return _single(trigger)


async def list_triggers(
    request: Request, limit: int | None = None
) -> CapabilityOutcome:
    """List live Scheduled triggers, soonest next fire first."""
    triggers = await request.app.state.trigger_service.list_triggers(
        limit=limit,
        logger=get_request_logger(request),
    )
    return CapabilityOutcome(
        result=[
            TriggerRead.from_trigger(trigger).model_dump(mode="json")
            for trigger in triggers
        ]
    )


async def update(
    request: Request, trigger_id: UUID, spec: TriggerSpec, version: PositiveInt
) -> CapabilityOutcome:
    """Replace a trigger's definition, re-arming it from its next occurrence."""
    trigger = await request.app.state.trigger_service.update(
        _trigger_reference(trigger_id, version),
        spec,
        now=datetime.now(UTC),
        logger=get_request_logger(request),
    )
    return _single(trigger)


async def delete(
    request: Request, trigger_id: UUID, version: PositiveInt
) -> CapabilityOutcome:
    """Delete a Scheduled trigger."""
    trigger = await request.app.state.trigger_service.delete(
        _trigger_reference(trigger_id, version),
        now=datetime.now(UTC),
        logger=get_request_logger(request),
    )
    return _single(trigger)
