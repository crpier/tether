"""Scheduled-trigger message resolution and multi-channel notification delivery."""

from typing import Protocol

from snekql.sqlite import Fetched

from tether.events import EventPublisher, NotifyEvent
from tether.notification_model import NotificationDraft
from tether.trigger_store import ScheduledTrigger


class TriggerNotifier(Protocol):
    """Deliver one resolved Scheduled-trigger message."""

    async def deliver(
        self,
        *,
        trigger: ScheduledTrigger[Fetched],
        message: str,
    ) -> None:
        """Deliver one message or propagate a delivery defect."""
        ...


class NotificationRecorder(Protocol):
    """Persist resolved notification content for reload recovery."""

    async def record(self, draft: NotificationDraft) -> object:
        """Record one durable notification."""
        ...


class PushSender(Protocol):
    """Deliver a resolved message through stored Web Push subscriptions."""

    async def send(self, body: str) -> None:
        """Send one body or propagate an unexpected delivery defect."""
        ...


class AgentPromptRunner(Protocol):
    """Resolve an unattended agent prompt to final message text."""

    async def run(self, prompt: str) -> str:
        """Run a prompt and return its final message."""
        ...


class EventNotifier:
    """Persist a notification before publishing its live browser frame."""

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
        trigger: ScheduledTrigger[Fetched],
        message: str,
    ) -> None:
        """Persist first, then publish to currently connected browsers."""
        if self.recorder is not None:
            _ = await self.recorder.record(
                NotificationDraft(
                    body=message,
                    trigger_id=str(trigger.id),
                    action_kind=trigger.action_kind,
                    source_label=trigger.payload,
                )
            )
        await self.event_publisher.publish(
            NotifyEvent(body=message, trigger_id=str(trigger.id))
        )


class PushDeliveryNotifier:
    """Add closed-tab Web Push after the canonical durable/live delivery."""

    def __init__(self, primary: TriggerNotifier, push_sender: PushSender) -> None:
        self.primary: TriggerNotifier = primary
        self.push_sender: PushSender = push_sender

    async def deliver(
        self,
        *,
        trigger: ScheduledTrigger[Fetched],
        message: str,
    ) -> None:
        """Complete primary delivery before attempting Web Push fan-out."""
        await self.primary.deliver(trigger=trigger, message=message)
        await self.push_sender.send(message)


class TriggerDispatcher:
    """Resolve a trigger action and hand the resulting message to delivery."""

    def __init__(
        self,
        *,
        notifier: TriggerNotifier,
        agent_runner: AgentPromptRunner,
    ) -> None:
        self.agent_runner: AgentPromptRunner = agent_runner
        self.notifier: TriggerNotifier = notifier

    async def dispatch(self, trigger: ScheduledTrigger[Fetched]) -> None:
        """Deliver a message payload or first resolve an agent prompt."""
        message = (
            trigger.payload
            if trigger.action_kind == "message"
            else await self.agent_runner.run(trigger.payload)
        )
        await self.notifier.deliver(trigger=trigger, message=message)
