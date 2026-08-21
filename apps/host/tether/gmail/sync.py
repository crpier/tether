"""Gmail message synchronization into Tether domain services."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from pydantic import UUID7
from snekok import Err, Ok, Result
from snekql.sqlite import Database, Fetched, Transaction, insert, select, update

from tether.chat_prompt import local_timezone_name
from tether.gmail.client import GmailClient, GmailFailure, GmailMessage
from tether.gmail.store import (
    GMAIL_WATERMARK_KEY,
    GmailMessageRecord,
    GmailMessageStatus,
    read_sync_watermark,
    write_sync_watermark,
)
from tether.gmail.triage import (
    GmailDeadline,
    GmailTriageRunner,
    GmailVerdict,
    build_gmail_triage_prompt,
    gmail_deadline_fire_at,
    gmail_message_excerpt,
    gmail_trigger_message,
    parse_gmail_verdicts,
)
from tether.memories import MemoryService
from tether.memory_store import (
    Memory,
    MemoryProvenance,
)
from tether.structured_logging import Logger
from tether.todos import TodoService
from tether.trigger_schedule import OnceTriggerSpec
from tether.triggers import TriggerService

DEFAULT_TRIAGE_BATCH_SIZE = 10
"""Maximum messages included in one triage prompt."""

_EXCLUDED_LABELS = frozenset({"SPAM", "TRASH", "SENT"})
_PREFILTER_CATEGORY_LABELS = frozenset(
    {"CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL", "CATEGORY_FORUMS"}
)
_TETHER_LABEL_NAME = "tether"
_WATERMARK_OVERLAP = timedelta(days=1)


@dataclass(frozen=True, slots=True)
class GmailSyncReport:
    """How eligible messages resolved during one synchronization pass."""

    ingested: int = 0
    noise: int = 0
    pending: int = 0
    prefiltered: int = 0


@dataclass(frozen=True, slots=True)
class _EligibleMessages:
    """Messages requiring triage plus deterministic prefilter count."""

    messages: tuple[GmailMessage, ...]
    prefiltered: int


@dataclass(frozen=True, slots=True)
class _TriageCounts:
    """Terminal triage counts for one synchronization pass."""

    ingested: int
    noise: int
    pending: int


def _debug(logger: Logger, event: str, **context: object) -> None:
    logger.debug(event, **context)


def _info(logger: Logger, event: str, **context: object) -> None:
    logger.info(event, **context)


def _build_query(watermark: datetime | None) -> str:
    """Exclude ineligible mail and overlap incremental cursor windows."""
    base = "-in:spam -in:trash -in:sent"
    if watermark is None:
        return base
    return f"{base} after:{int((watermark - _WATERMARK_OVERLAP).timestamp())}"


def _is_excluded_entirely(label_ids: Sequence[str]) -> bool:
    return any(label in _EXCLUDED_LABELS for label in label_ids)


def _is_category_noise(label_ids: Sequence[str]) -> bool:
    return any(label in _PREFILTER_CATEGORY_LABELS for label in label_ids)


def _chunk(items: Sequence[GmailMessage], size: int) -> list[list[GmailMessage]]:
    return [list(items[start : start + size]) for start in range(0, len(items), size)]


class GmailSyncService:
    """Synchronize Gmail messages into Memories, Todos, and triggers."""

    def __init__(  # noqa: PLR0913 - each argument is an independent collaborator
        self,
        database: Database,
        client: GmailClient,
        memory_service: MemoryService,
        trigger_service: TriggerService,
        todo_service: TodoService,
        triage_runner: GmailTriageRunner,
        *,
        triage_batch_size: int = DEFAULT_TRIAGE_BATCH_SIZE,
        timezone_name_provider: Callable[[datetime], str] = local_timezone_name,
    ) -> None:
        self.client: GmailClient = client
        self.database: Database = database
        self.memory_service: MemoryService = memory_service
        self.todo_service: TodoService = todo_service
        self.triage_batch_size: int = triage_batch_size
        self.triage_runner: GmailTriageRunner = triage_runner
        self.trigger_service: TriggerService = trigger_service
        self.timezone_name_provider: Callable[[datetime], str] = timezone_name_provider

    async def sync(self, *, logger: Logger) -> Result[GmailSyncReport, GmailFailure]:
        """Run one pass and advance the watermark only after full success."""
        started_at = datetime.now(UTC)
        watermark = await read_sync_watermark(self.database, GMAIL_WATERMARK_KEY)
        _debug(
            logger,
            "Gmail sync starting",
            incremental=watermark is not None,
            watermark=watermark.isoformat() if watermark is not None else None,
        )
        label_resolution = await self.client.resolve_label_id(_TETHER_LABEL_NAME)
        if isinstance(label_resolution, Err):
            return Err(label_resolution.error)
        eligible = await self._fetch_eligible_messages(
            watermark=watermark,
            tether_label_id=label_resolution.value,
            logger=logger,
        )
        if isinstance(eligible, Err):
            return Err(eligible.error)
        counts = await self._triage_messages(
            eligible.value.messages,
            tether_label_id=label_resolution.value,
            now=started_at,
            logger=logger,
        )
        await write_sync_watermark(self.database, GMAIL_WATERMARK_KEY, started_at)
        report = GmailSyncReport(
            ingested=counts.ingested,
            noise=counts.noise,
            pending=counts.pending,
            prefiltered=eligible.value.prefiltered,
        )
        _info(
            logger,
            "Gmail sync completed",
            ingested=report.ingested,
            noise=report.noise,
            pending=report.pending,
            prefiltered=report.prefiltered,
        )
        return Ok(report)

    async def sync_forever(self, *, interval_seconds: float, logger: Logger) -> None:
        """Run periodic passes until cancellation."""
        while True:
            await asyncio.sleep(interval_seconds)
            report = await self.sync(logger=logger)
            if isinstance(report, Err):
                logger.warning(
                    "Gmail sync pass failed",
                    failure=type(report.error).__name__,
                    operation=report.error.operation,
                )

    async def _fetch_eligible_messages(
        self,
        *,
        watermark: datetime | None,
        tether_label_id: str | None,
        logger: Logger,
    ) -> Result[_EligibleMessages, GmailFailure]:
        """Collect pending and newly listed messages requiring model triage."""
        eligible: list[GmailMessage] = []
        for record in await self._fetch_pending_records():
            fetched = await self.client.get_message(record.message_id)
            if isinstance(fetched, Err):
                return Err(fetched.error)
            eligible.append(fetched.value)
        listing = await self.client.list_message_ids(
            query=_build_query(watermark), logger=logger
        )
        if isinstance(listing, Err):
            return Err(listing.error)
        prefiltered = 0
        for message_id in listing.value:
            if await self._already_recorded(message_id):
                continue
            fetched = await self.client.get_message(message_id)
            if isinstance(fetched, Err):
                return Err(fetched.error)
            message = fetched.value
            if _is_excluded_entirely(message.label_ids):
                continue
            has_tether_label = (
                tether_label_id is not None and tether_label_id in message.label_ids
            )
            if _is_category_noise(message.label_ids) and not has_tether_label:
                await self._record_status(message, status="prefiltered")
                prefiltered += 1
                continue
            eligible.append(message)
        return Ok(_EligibleMessages(messages=tuple(eligible), prefiltered=prefiltered))

    async def _triage_messages(
        self,
        messages: Sequence[GmailMessage],
        *,
        tether_label_id: str | None,
        now: datetime,
        logger: Logger,
    ) -> _TriageCounts:
        """Apply model verdicts while leaving malformed entries pending."""
        noise = ingested = pending = 0
        for batch in _chunk(messages, self.triage_batch_size):
            eligible_ids = frozenset(message.message_id for message in batch)
            verdicts = parse_gmail_verdicts(
                await self.triage_runner.run(build_gmail_triage_prompt(batch)),
                eligible_ids=eligible_ids,
            )
            by_id = {message.message_id: message for message in batch}
            for message_id in eligible_ids:
                verdict = verdicts.get(message_id)
                if verdict is None:
                    await self._record_status(by_id[message_id], status="pending")
                    pending += 1
                    continue
                outcome = await self._apply_verdict(
                    by_id[message_id],
                    verdict,
                    force_interesting=(
                        tether_label_id is not None
                        and tether_label_id in by_id[message_id].label_ids
                    ),
                    now=now,
                    logger=logger,
                )
                if outcome == "noise":
                    noise += 1
                else:
                    ingested += 1
        return _TriageCounts(ingested=ingested, noise=noise, pending=pending)

    async def _apply_verdict(
        self,
        message: GmailMessage,
        verdict: GmailVerdict,
        *,
        force_interesting: bool,
        now: datetime,
        logger: Logger,
    ) -> str:
        """Persist one verdict while preventing duplicate captured Memories."""
        classification = "interesting" if force_interesting else verdict.classification
        if classification == "noise":
            await self._record_status(message, status="noise")
            return "noise"
        memory = await self._capture_memory(message, verdict, logger=logger)
        await self._record_status(message, status="ingested", memory_id=str(memory.id))
        trigger_id: UUID7 | None = None
        if verdict.deadline is not None and verdict.deadline.at > now:
            trigger_id = await self._create_deadline_trigger(
                message, verdict.deadline, now=now, logger=logger
            )
            await self._record_status(
                message,
                status="ingested",
                memory_id=str(memory.id),
                trigger_id=str(trigger_id),
            )
        if verdict.actionable:
            todo = await self.todo_service.create(message.subject, logger=logger)
            await self.todo_service.link_memory(todo.id, memory.id, logger=logger)
            if trigger_id is not None:
                _ = await self.todo_service.link_trigger(
                    todo, str(trigger_id), logger=logger
                )
        return "ingested"

    async def _capture_memory(
        self, message: GmailMessage, verdict: GmailVerdict, *, logger: Logger
    ) -> Memory[Fetched]:
        facets: dict[str, str] = {
            "source": "gmail",
            "sender": message.from_header,
            "subject": message.subject,
            "date": message.internal_date.isoformat(),
        }
        if verdict.deadline is not None:
            facets["deadline"] = verdict.deadline.at.isoformat()
        return await self.memory_service.capture_tethered(
            f"{verdict.why}\n\n{gmail_message_excerpt(message.body_text)}",
            provenance=MemoryProvenance(kind="gmail"),
            facets=facets,
            logger=logger,
        )

    async def _create_deadline_trigger(
        self,
        message: GmailMessage,
        deadline: GmailDeadline,
        *,
        now: datetime,
        logger: Logger,
    ) -> UUID7:
        trigger = await self.trigger_service.create(
            OnceTriggerSpec(
                action_kind="message",
                payload=gmail_trigger_message(message, deadline),
                fire_at=gmail_deadline_fire_at(
                    deadline.at,
                    now=now,
                    timezone_name=self.timezone_name_provider(now),
                ),
            ),
            now=now,
            logger=logger,
        )
        return trigger.id

    async def _already_recorded(self, message_id: str) -> bool:
        async with self.database.transaction() as transaction:
            stored = await transaction.fetch_one_or_none(
                select(GmailMessageRecord).where(
                    GmailMessageRecord.message_id.eq(message_id)
                )
            )
        return stored is not None

    async def _fetch_pending_records(self) -> list[GmailMessageRecord[Fetched]]:
        async with self.database.transaction() as transaction:
            return await transaction.fetch_all(
                select(GmailMessageRecord).where(
                    GmailMessageRecord.status.eq("pending")
                )
            )

    async def _record_status(
        self,
        message: GmailMessage,
        *,
        status: GmailMessageStatus,
        memory_id: str | None = None,
        trigger_id: str | None = None,
    ) -> None:
        """Insert or update one message's idempotency state."""

        async def _upsert(transaction: Transaction) -> None:
            stored = await transaction.fetch_one_or_none(
                select(GmailMessageRecord).where(
                    GmailMessageRecord.message_id.eq(message.message_id)
                )
            )
            if stored is None:
                _ = await transaction.execute(
                    insert(
                        GmailMessageRecord(
                            internal_date=message.internal_date.isoformat(),
                            memory_id=memory_id,
                            message_id=message.message_id,
                            status=status,
                            trigger_id=trigger_id,
                        )
                    )
                )
                return
            _ = await transaction.execute(
                update(GmailMessageRecord)
                .set(
                    GmailMessageRecord.memory_id.to(memory_id),
                    GmailMessageRecord.status.to(status),
                    GmailMessageRecord.trigger_id.to(trigger_id),
                )
                .where(GmailMessageRecord.message_id.eq(message.message_id))
            )

        async with self.database.transaction(mode="immediate") as transaction:
            await _upsert(transaction)


__all__ = ["DEFAULT_TRIAGE_BATCH_SIZE", "GmailSyncReport", "GmailSyncService"]
