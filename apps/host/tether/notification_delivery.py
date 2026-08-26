"""Scheduled-occurrence execution and independent notification delivery."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, cast
from uuid import UUID

from snekql.sqlite import Fetched

from tether.conversation_store import Conversation
from tether.conversation_turns import (
    ConversationTurns,
    ScheduledTurnRequest,
    SilentChatFrameSink,
)
from tether.conversations import ConversationService
from tether.events import EventPublisher, InvalidateEvent, NotifyEvent
from tether.model_selection import AgentModelConfig, ThinkingLevel
from tether.notification_model import NotificationDraft
from tether.trigger_store import ScheduledOccurrence
from tether.triggers import TriggerService


@dataclass(frozen=True, slots=True)
class PushNotification:
    """Browser notification content plus its in-app destination."""

    body: str
    title: str = "Tether"
    url: str = "/"


@dataclass(frozen=True, slots=True)
class TriggerDispatchResult:
    """Execution output retained before independent delivery begins."""

    answer: str | None = None
    answer_message_id: UUID | None = None


class PromptExecutionError(Exception):
    """A scheduled Conversation turn reached a non-success terminal state."""


class TriggerNotifier(Protocol):
    """Deliver one fixed-message occurrence."""

    async def deliver(
        self,
        *,
        occurrence: ScheduledOccurrence[Fetched],
        message: str,
    ) -> None: ...


class NotificationRecorder(Protocol):
    """Persist resolved notification content for reload recovery."""

    async def record(self, draft: NotificationDraft) -> object: ...


class PushSender(Protocol):
    """Deliver one structured browser notification."""

    async def send(self, notification: PushNotification) -> None: ...


class EventNotifier:
    """Persist a fixed reminder before publishing its live browser frame."""

    def __init__(
        self,
        event_publisher: EventPublisher,
        recorder: NotificationRecorder | None = None,
    ) -> None:
        self.event_publisher: EventPublisher = event_publisher
        self.recorder: NotificationRecorder | None = recorder

    async def deliver(
        self,
        *,
        occurrence: ScheduledOccurrence[Fetched],
        message: str,
    ) -> None:
        if self.recorder is not None:
            _ = await self.recorder.record(
                NotificationDraft(
                    body=message,
                    trigger_id=str(occurrence.trigger_id),
                    action_kind=occurrence.action_kind,
                    source_label=occurrence.payload,
                )
            )
        await self.event_publisher.publish(
            NotifyEvent(body=message, trigger_id=str(occurrence.trigger_id))
        )


class PushDeliveryNotifier:
    """Add Web Push after canonical fixed-reminder delivery."""

    def __init__(self, primary: TriggerNotifier, push_sender: PushSender) -> None:
        self.primary: TriggerNotifier = primary
        self.push_sender: PushSender = push_sender

    async def deliver(
        self,
        *,
        occurrence: ScheduledOccurrence[Fetched],
        message: str,
    ) -> None:
        await self.primary.deliver(occurrence=occurrence, message=message)
        await self.push_sender.send(PushNotification(body=message))


@dataclass(frozen=True, slots=True)
class ScheduledExecutionDependencies:
    """Durable collaborators for targeted prompt execution."""

    conversation_service: ConversationService
    conversation_turns: ConversationTurns
    trigger_service: TriggerService


class TriggerDispatcher:
    """Execute immutable occurrences and deliver stored prompt answers."""

    def __init__(
        self,
        *,
        dependencies: ScheduledExecutionDependencies,
        notifier: TriggerNotifier,
        event_publisher: EventPublisher | None = None,
        prompt_push_sender: PushSender | None = None,
    ) -> None:
        self.conversation_service: ConversationService = (
            dependencies.conversation_service
        )
        self.conversation_turns: ConversationTurns = dependencies.conversation_turns
        self.event_publisher: EventPublisher | None = event_publisher
        self.notifier: TriggerNotifier = notifier
        self.prompt_push_sender: PushSender | None = prompt_push_sender
        self.trigger_service: TriggerService = dependencies.trigger_service

    async def dispatch(
        self,
        occurrence: ScheduledOccurrence[Fetched],
    ) -> TriggerDispatchResult:
        """Deliver fixed text or wait for one idempotent scheduled turn."""
        if occurrence.action_kind == "message":
            await self.notifier.deliver(
                occurrence=occurrence,
                message=occurrence.payload,
            )
            return TriggerDispatchResult()
        if occurrence.target_conversation_id is None:
            message = "Scheduled prompt has no target Conversation."
            raise PromptExecutionError(message)
        ticket = await self.conversation_turns.submit(
            ScheduledTurnRequest(
                conversation_id=occurrence.target_conversation_id,
                occurrence_id=occurrence.id,
                prompt=occurrence.payload,
                model_profile=occurrence.model_profile,
                model_config=(
                    AgentModelConfig(
                        display_name=(
                            occurrence.model_display_name_snapshot
                            or occurrence.model_id_snapshot
                            or "Scheduled model"
                        ),
                        id=(
                            occurrence.model_profile
                            or occurrence.model_id_snapshot
                            or "scheduled"
                        ),
                        model_id=occurrence.model_id_snapshot,
                        provider=occurrence.model_provider_snapshot,
                        thinking_level=cast(
                            "ThinkingLevel | None",
                            occurrence.model_thinking_level_snapshot,
                        ),
                    )
                    if occurrence.model_id_snapshot is not None
                    and occurrence.model_provider_snapshot is not None
                    else None
                ),
            ),
            SilentChatFrameSink(),
        )
        outcome = await self.conversation_turns.wait(ticket.turn_id)
        if self.event_publisher is not None:
            await self.event_publisher.publish(
                InvalidateEvent(keys=["messages", "conversations"])
            )
        if outcome.status != "succeeded":
            raise PromptExecutionError(
                outcome.failure_summary or f"Scheduled turn {outcome.status}."
            )
        answer = await self.trigger_service.fetch_occurrence_answer(occurrence.id)
        if answer is None:
            message = "Scheduled turn produced no assistant answer."
            raise PromptExecutionError(message)
        return TriggerDispatchResult(answer=answer.content, answer_message_id=answer.id)

    async def deliver_prompt_push(
        self,
        occurrence: ScheduledOccurrence[Fetched],
        *,
        now: datetime,
    ) -> None:
        """Retry push from stored output without submitting pi again."""
        if occurrence.action_kind != "prompt" or occurrence.status != "succeeded":
            return
        if self.prompt_push_sender is None:
            _ = await self.trigger_service.record_push_delivered(
                occurrence.id,
                now=now,
            )
            return
        answer = occurrence.answer
        if answer is None:
            message = await self.trigger_service.fetch_occurrence_answer(occurrence.id)
            if message is None:
                _ = await self.trigger_service.record_push_failure(
                    occurrence.id,
                    now=now,
                    error="Stored scheduled answer is unavailable.",
                )
                return
            answer = message.content
        target = await self._fetch_target(occurrence.target_conversation_id)
        target_name = "Main" if target.kind == "main" else target.display_name or "Chat"
        turn_id = await self._turn_id(occurrence.id)
        url = (
            f"/chat?turn={turn_id}"
            if target.kind == "main"
            else f"/chat/{target.id}?turn={turn_id}"
        )
        try:
            await self.prompt_push_sender.send(
                PushNotification(
                    body=answer,
                    title=f"Tether · {target_name}",
                    url=url,
                )
            )
        except Exception as error:
            _ = await self.trigger_service.record_push_failure(
                occurrence.id,
                now=now,
                error=str(error),
            )
        else:
            _ = await self.trigger_service.record_push_delivered(
                occurrence.id,
                now=now,
            )

    async def _fetch_target(
        self, conversation_id: UUID | None
    ) -> Conversation[Fetched]:
        if conversation_id is None:
            message = "Scheduled prompt has no target Conversation."
            raise PromptExecutionError(message)
        return await self.conversation_service.fetch_conversation(conversation_id)

    async def _turn_id(self, occurrence_id: UUID) -> UUID:
        turn = await self.trigger_service.fetch_occurrence_turn(occurrence_id)
        if turn is None:
            message = "Scheduled occurrence has no linked turn."
            raise PromptExecutionError(message)
        return turn.id


__all__ = [
    "EventNotifier",
    "PromptExecutionError",
    "PushDeliveryNotifier",
    "PushNotification",
    "PushSender",
    "ScheduledExecutionDependencies",
    "TriggerDispatchResult",
    "TriggerDispatcher",
    "TriggerNotifier",
]
