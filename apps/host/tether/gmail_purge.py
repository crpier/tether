"""Gmail backlog-purge sweep: propose (never perform) inbox hygiene actions.

A background worker in the same reconciler shape as the ingestion gate
(`tether.gmail.GmailSyncService`), but on the *write* side of ADR 0014: it reads
a bounded chunk of eligible backlog mail, asks the agent to decide a per-message
hygiene action (archive / label / delete / keep) plus a sender-category scope,
and folds the actionable verdicts into a **single Proposal per chunk** through
`ProposalService`. It never touches the mailbox itself — approval-time execution
runs the registered `gmail.*` executors (`tether.gmail_actions`); the sweep only
proposes, so a human (or a standing autonomy grant) always gates the writes.

It keeps its *own* resumable watermark under a separate `gmail_sync_state` key
(`gmail_purge_watermark`), distinct from the ingestion watermark, advanced only
after a fully successful pass — an outage delays the sweep rather than skipping
backlog. A malformed or missing per-message verdict is dropped defensively (that
message is simply re-swept on a later pass), mirroring the ingestion gate's
`_parse_verdicts`.

>>> service = GmailPurgeSweepService(
...     database=database,
...     client=client,
...     proposal_service=proposal_service,
...     triage_runner=triage_runner,
... )
>>> report = await service.sweep(logger=logger)
>>> report.proposed
1
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, cast

from pydantic import BaseModel, ValidationError
from snekql.sqlite import Database, Transaction, insert, select, update

from tether.db_retry import run_in_transaction
from tether.gmail import (
    GmailClient,
    GmailMessage,
    GmailSyncState,
    GmailTriageError,
    GmailTriageRunner,
)
from tether.logging import Logger
from tether.proposals import ActionDraft, ProposalDraft, ProposalService

DEFAULT_PURGE_CHUNK_SIZE = 10
"""How many backlog messages are triaged per sweep chunk (and per proposal), by
default — bounds both the prompt size and how large one proposal gets."""

_PURGE_WATERMARK_KEY = "gmail_purge_watermark"
"""Sync-state key for the sweep's own resumable watermark. Deliberately distinct
from the ingestion gate's `gmail_message_watermark` so the two workers advance
independently over the same `gmail_sync_state` table."""

_WATERMARK_OVERLAP = timedelta(days=1)
"""Re-query window subtracted from the watermark on an incremental sweep, so a
message that landed just before the previous sweep's cutoff is not missed."""

_EXCLUDED_LABELS: frozenset[str] = frozenset({"SPAM", "TRASH", "SENT"})
"""System labels that exclude a message from the sweep entirely (a backstop to
the upstream query exclusion, mirroring the ingestion gate)."""

_CONSUMER = "gmail"
"""The proposal consumer these hygiene proposals are attributed to."""

type GmailPurgeAction = Literal["archive", "label", "delete", "keep"]
"""One message's hygiene verdict. `keep` produces no action; the rest map to a
`gmail.archive` / `gmail.label` / `gmail.delete` proposal action respectively."""


def _debug(logger: Logger, event: str, **context: object) -> None:
    logger.debug(event, **context)


def _info(logger: Logger, event: str, **context: object) -> None:
    logger.info(event, **context)


@dataclass(frozen=True, slots=True)
class GmailPurgeVerdict:
    """One message's parsed hygiene verdict: an action plus its sender category.

    `label_name` is present only for a `label` action (and is guaranteed
    non-empty there by the parser); `sender_category` is the scope segment the
    proposal action is granted under (`sender-category:<category>`)."""

    message_id: str
    action: GmailPurgeAction
    sender_category: str
    label_name: str | None = None


@dataclass(frozen=True, slots=True)
class GmailPurgeReport:
    """The tally of one sweep pass: messages scanned, proposals and actions made."""

    scanned: int = 0
    proposed: int = 0
    actions: int = 0


class _ParsedPurgeVerdict(BaseModel):
    """Strict parse target for one hygiene verdict entry."""

    message_id: str
    action: Literal["archive", "label", "delete", "keep"]
    sender_category: str = "uncategorized"
    label_name: str | None = None


def _build_purge_query(watermark: datetime | None) -> str:
    """Build the sweep's Gmail query: inbox backlog, bounded by the watermark.

    Restricted to `in:inbox` (the backlog the sweep proposes to archive/label/
    delete); an incremental pass additionally bounds by the watermark minus the
    overlap window."""
    base = "in:inbox -in:spam -in:trash -in:sent"
    if watermark is None:
        return base
    after_epoch = int((watermark - _WATERMARK_OVERLAP).timestamp())
    return f"{base} after:{after_epoch}"


def _is_excluded_entirely(label_ids: Sequence[str]) -> bool:
    """True when a message carries spam/trash/sent, excluded unconditionally."""
    return any(label in _EXCLUDED_LABELS for label in label_ids)


def _chunk(items: Sequence[GmailMessage], size: int) -> list[list[GmailMessage]]:
    """Split `items` into consecutive groups of at most `size`."""
    return [list(items[start : start + size]) for start in range(0, len(items), size)]


def _extract_json_array(text: str) -> str:
    """Slice the outermost JSON array from a model reply, tolerating stray prose."""
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end < start:
        message = "purge reply contained no JSON array"
        raise GmailTriageError(message)
    return text[start : end + 1]


def _parse_purge_verdicts(
    reply: str, *, eligible_ids: frozenset[str]
) -> dict[str, GmailPurgeVerdict]:
    """Defensively parse a hygiene reply into per-message verdicts.

    Each entry is validated independently: a malformed entry, an id outside this
    chunk, a duplicate id, or a `label` action missing its `label_name` is
    dropped rather than failing the chunk — that message is simply re-swept on a
    later pass. A reply with no JSON array yields no verdicts.
    """
    try:
        raw = json.loads(_extract_json_array(reply))
    except GmailTriageError, json.JSONDecodeError:
        return {}
    if not isinstance(raw, list):
        return {}
    verdicts: dict[str, GmailPurgeVerdict] = {}
    for entry in cast("list[object]", raw):
        if not isinstance(entry, Mapping):
            continue
        try:
            parsed = _ParsedPurgeVerdict.model_validate(entry)
        except ValidationError:
            continue
        if parsed.message_id not in eligible_ids or parsed.message_id in verdicts:
            continue
        label_name = (parsed.label_name or "").strip()
        if parsed.action == "label" and not label_name:
            continue
        verdicts[parsed.message_id] = GmailPurgeVerdict(
            message_id=parsed.message_id,
            action=parsed.action,
            sender_category=parsed.sender_category.strip() or "uncategorized",
            label_name=label_name or None,
        )
    return verdicts


_PURGE_INSTRUCTIONS = """\
You are triaging a batch of backlog inbox emails for personal-inbox hygiene. \
For each email, decide ONE hygiene action:
- "archive": routine mail worth keeping but not in the inbox (remove from inbox).
- "label": mail to file under a category label (give the label name).
- "delete": junk/expired mail worth moving to Trash (reversible, never permanent).
- "keep": leave it in the inbox untouched.

Also give a short "sender_category" (e.g. "newsletter", "receipts", \
"notifications") used to group similar senders.

Return ONLY a JSON array (no prose, no code fences) with one object per email, \
in this exact shape:
[
  {{
    "message_id": "<the email's id, copied exactly>",
    "action": "archive" | "label" | "delete" | "keep",
    "sender_category": "<short category>",
    "label_name": "<label to apply, only when action is label>"
  }}
]

Omit "label_name" unless the action is "label". Every email in the batch must \
get exactly one verdict object.

Emails:
{messages}
"""


def _format_message_for_prompt(message: GmailMessage) -> str:
    """Render one message's triage-relevant fields for the sweep prompt."""
    return (
        f"id: {message.message_id}\n"
        f"From: {message.from_header}\n"
        f"Subject: {message.subject}\n"
        f"Date: {message.date_header}\n"
        f"Labels: {', '.join(message.label_ids)}\n"
        f"Body:\n{message.body_text}"
    )


def _build_purge_prompt(batch: Sequence[GmailMessage]) -> str:
    """Build the hygiene triage prompt for a group of backlog messages."""
    return _PURGE_INSTRUCTIONS.format(
        messages="\n---\n".join(
            _format_message_for_prompt(message) for message in batch
        )
    )


def _action_draft(
    message: GmailMessage, verdict: GmailPurgeVerdict
) -> ActionDraft | None:
    """Build the proposal action for one non-`keep` verdict, else None.

    The scope is `sender-category:<category>`, the grantable trust unit an
    autonomy grant is keyed on; params carry the message id (and label name for
    a `label` action)."""
    scope = f"sender-category:{verdict.sender_category}"
    if verdict.action == "archive":
        return ActionDraft(
            kind="gmail.archive", scope=scope, params={"message_id": message.message_id}
        )
    if verdict.action == "delete":
        return ActionDraft(
            kind="gmail.delete", scope=scope, params={"message_id": message.message_id}
        )
    if verdict.action == "label" and verdict.label_name is not None:
        return ActionDraft(
            kind="gmail.label",
            scope=scope,
            params={
                "message_id": message.message_id,
                "label_name": verdict.label_name,
            },
        )
    return None


def _build_action_drafts(
    verdicts: Mapping[str, GmailPurgeVerdict], batch: Sequence[GmailMessage]
) -> list[ActionDraft]:
    """Build the ordered proposal actions for a chunk's actionable verdicts."""
    drafts: list[ActionDraft] = []
    for message in batch:
        verdict = verdicts.get(message.message_id)
        if verdict is None:
            continue
        draft = _action_draft(message, verdict)
        if draft is not None:
            drafts.append(draft)
    return drafts


def _proposal_summary(drafts: Sequence[ActionDraft]) -> str:
    """A one-line human summary of a hygiene proposal's actions."""
    counts: dict[str, int] = {}
    for draft in drafts:
        counts[draft.kind] = counts.get(draft.kind, 0) + 1
    parts = ", ".join(f"{count} {kind}" for kind, count in sorted(counts.items()))
    return f"Inbox hygiene: {parts}."


class GmailPurgeSweepService:
    """Reconciler-shaped backlog-purge worker that proposes Gmail hygiene actions.

    An idempotent `sweep` pass (run at boot and on a periodic loop): lists
    eligible inbox backlog for the watermark window, triages each chunk through
    the injected `GmailTriageRunner`, and composes one Proposal per chunk of the
    actionable verdicts via `ProposalService`. It performs no mailbox writes —
    that is deferred to approval-time executors. The watermark advances only when
    the whole pass completes, so an outage delays the sweep rather than skipping
    backlog.
    """

    def __init__(
        self,
        database: Database,
        client: GmailClient,
        proposal_service: ProposalService,
        triage_runner: GmailTriageRunner,
        *,
        chunk_size: int = DEFAULT_PURGE_CHUNK_SIZE,
    ) -> None:
        self.database: Database = database
        self.client: GmailClient = client
        self.proposal_service: ProposalService = proposal_service
        self.triage_runner: GmailTriageRunner = triage_runner
        self.chunk_size: int = chunk_size

    async def sweep(self, *, logger: Logger) -> GmailPurgeReport:
        """Run one idempotent sweep; persist the watermark only if it completes."""
        started_at = datetime.now(UTC)
        watermark = await self._read_watermark()
        _debug(
            logger,
            "Gmail purge sweep starting",
            incremental=watermark is not None,
            watermark=watermark.isoformat() if watermark is not None else None,
        )
        message_ids = await self.client.list_message_ids(
            query=_build_purge_query(watermark), logger=logger
        )
        messages: list[GmailMessage] = []
        for message_id in message_ids:
            message = await self.client.get_message(message_id)
            if _is_excluded_entirely(message.label_ids):
                continue
            messages.append(message)
        proposed = 0
        actions_total = 0
        for batch in _chunk(messages, self.chunk_size):
            eligible_ids = frozenset(message.message_id for message in batch)
            reply = await self.triage_runner.run(_build_purge_prompt(batch))
            verdicts = _parse_purge_verdicts(reply, eligible_ids=eligible_ids)
            drafts = _build_action_drafts(verdicts, batch)
            if not drafts:
                continue
            _ = await self.proposal_service.create(
                ProposalDraft(
                    consumer=_CONSUMER,
                    title=f"Inbox hygiene: {len(drafts)} actions",
                    summary=_proposal_summary(drafts),
                    actions=drafts,
                ),
                now=started_at,
                logger=logger,
            )
            proposed += 1
            actions_total += len(drafts)
        await self._store_watermark(started_at)
        _info(
            logger,
            "Gmail purge sweep completed",
            scanned=len(messages),
            proposed=proposed,
            actions=actions_total,
        )
        return GmailPurgeReport(
            scanned=len(messages), proposed=proposed, actions=actions_total
        )

    async def sync_forever(self, *, interval_seconds: float, logger: Logger) -> None:
        """Run sweep passes on the given interval until cancelled.

        Mirrors the ingestion worker: a failed pass is logged with its traceback
        and the loop survives, so a transient Gmail or model outage does not take
        the worker down.
        """
        while True:
            await asyncio.sleep(interval_seconds)
            try:
                _ = await self.sweep(logger=logger)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Gmail purge sweep failed")

    async def _read_watermark(self) -> datetime | None:
        """The last fully successful sweep's start time, or None on first sweep."""
        async with self.database.transaction() as tx:
            row = await tx.fetch_one_or_none(
                select(GmailSyncState).where(
                    GmailSyncState.key.eq(_PURGE_WATERMARK_KEY)
                )
            )
        if row is None:
            return None
        parsed = datetime.fromisoformat(row.value)
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)

    async def _store_watermark(self, watermark: datetime) -> None:
        """Persist the sweep watermark, upserting its single sync-state row."""

        async def _set(tx: Transaction) -> None:
            existing = await tx.fetch_one_or_none(
                select(GmailSyncState).where(
                    GmailSyncState.key.eq(_PURGE_WATERMARK_KEY)
                )
            )
            if existing is None:
                _ = await tx.execute(
                    insert(
                        GmailSyncState(
                            key=_PURGE_WATERMARK_KEY, value=watermark.isoformat()
                        )
                    )
                )
            else:
                _ = await tx.execute(
                    update(GmailSyncState)
                    .set(GmailSyncState.value.to(watermark.isoformat()))
                    .where(GmailSyncState.key.eq(_PURGE_WATERMARK_KEY))
                )

        await run_in_transaction(self.database, _set)


__all__ = [
    "DEFAULT_PURGE_CHUNK_SIZE",
    "GmailPurgeAction",
    "GmailPurgeReport",
    "GmailPurgeSweepService",
    "GmailPurgeVerdict",
]
