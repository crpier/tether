"""Gmail read-only ingestion gate: triaged mail synced into the Commons.

A background sync loop, following the established machine-synced gate pattern
(Readwise, KOReader): it polls Gmail, drops bulk categories deterministically,
triages the remainder in batches through the agent, and turns the results into
Tether constructs. The module is three seams:

- `GmailTransport` — the isolated HTTP boundary (list, get, list-labels), faked
  in tests so no live Gmail call runs.
- `GmailClient` — pagination over `nextPageToken`, message parsing (headers,
  label ids, MIME body extraction), and the `tether` label resolution.
- `GmailSyncService` — the reconciler-shaped worker: an idempotent `sync` pass
  (boot + periodic loop) that pre-filters bulk categories without spending a
  model call, triages the remainder in batches through an injected agent-prompt
  runner, and folds each verdict into a tethered Memory (and, for a future
  deadline, a one-shot `ScheduledTrigger`) against the `gmail_message`
  idempotency table. The `internalDate` watermark (pass start, with a one-day
  overlap window) is persisted only after a fully successful pass.

The gate never mutates the inbox — no label changes, no archiving, no marking
read — and reads only what it needs to triage and ingest.

Batching several emails into one triage prompt is an accepted tradeoff: one
email's content can, in principle, influence another email's verdict within
the same batch (in-batch prompt injection), traded for the reduced model-call
volume batching buys.

>>> service = GmailSyncService(
...     database=database,
...     client=client,
...     memory_service=memory_service,
...     trigger_service=trigger_service,
...     triage_runner=triage_runner,
... )
>>> report = await service.sync(logger=logger)
>>> report.ingested
1
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta, tzinfo
from typing import Literal, Protocol, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import UUID7, BaseModel, ValidationError
from snekql.sqlite import (
    CurrentTimestamp,
    Database,
    Fetched,
    Model,
    Pending,
    Text,
    Transaction,
    insert,
    select,
    update,
)

from tether.chat_ws import local_timezone_name
from tether.db_retry import run_in_transaction
from tether.logging import Logger
from tether.memories import Memory, MemoryProvenance, MemoryService
from tether.todos import TodoService
from tether.triggers import TriggerService, TriggerSpec

DEFAULT_TRIAGE_BATCH_SIZE = 10
"""How many messages are triaged per ephemeral agent prompt run, by default."""

_EXCLUDED_LABELS: frozenset[str] = frozenset({"SPAM", "TRASH", "SENT"})
"""System labels that exclude a message from ingestion entirely, checked
client-side as a backstop to the upstream query exclusion (`-in:spam -in:trash
-in:sent`) so the invariant holds even if the query is ever loosened."""

_PREFILTER_CATEGORY_LABELS: frozenset[str] = frozenset(
    {"CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL", "CATEGORY_FORUMS"}
)
"""Gmail category labels that are recorded as `prefiltered` without spending a
triage call, unless the message also carries the `tether` override label."""

_TETHER_LABEL_NAME = "tether"
"""The user-applied Gmail label that force-ingests a message past both the
category pre-filter and a `noise` triage verdict."""

_WATERMARK_KEY = "gmail_message_watermark"
"""Sync-state key under which the last fully successful pass's start time is
persisted. Absence means no successful pass has run — the next sync is a full
backfill."""

_WATERMARK_OVERLAP = timedelta(days=1)
"""Re-query window subtracted from the watermark on an incremental pass, so a
message that lands just before the previous pass's cutoff is not missed."""

_BODY_TRUNCATE_CHARS = 4_000
"""Longest body text handed to the triage prompt, keeping batch prompts and
token spend bounded regardless of message length."""

_EXCERPT_CHARS = 200
"""Longest excerpt appended to an ingested Memory's content."""

_DEADLINE_FIRE_HOUR = 9
"""The local hour a deadline trigger fires on, the morning before the deadline."""

_PAST_FIRE_CLAMP_MINUTES = 5
"""How far past `now` a deadline trigger's fire time is clamped when the
morning-before slot has already passed."""

_HTTP_OK = 200
"""The Gmail API success status; anything else raises `GmailApiError`."""

_HTTP_NOT_FOUND = 404
"""A `messages.get` for a message that no longer exists — treated as gone (a
soft skip) by the write methods, never an error, since a hygiene action on a
message the user already deleted has simply already happened."""

_HTTP_FORBIDDEN = 403
"""An insufficient-scope write (the token predates the `gmail.modify` scope).
Surfaced verbatim on the `GmailApiError` so a write executor can fail with a
clear re-authorization hint rather than crashing."""

_INBOX_LABEL = "INBOX"
"""The system label whose removal is an archive."""

_TRASH_LABEL = "TRASH"
"""The system label a trashed message carries."""

type GmailWriteOutcome = Literal["done", "already", "gone"]
"""The result of an idempotent mailbox write: `done` (the change was applied),
`already` (the message was already in the desired state), or `gone` (the
message no longer exists). Both `already` and `gone` map to a `skipped` action
outcome, so an idempotent re-run of an interrupted batch resolves cleanly."""

type GmailMessageStatus = Literal["prefiltered", "noise", "ingested", "pending"]
"""An ingested/reviewed message's resting state in the idempotency table.

`pending` is stamped explicitly for a message whose verdict came back
missing/malformed, so it survives the watermark advancing past it: every sync
pass re-fetches and re-triages every `pending` row regardless of the
watermark window, upgrading it to its final status on a valid verdict. A
message id absent from the table entirely has simply never been listed yet."""


class GmailApiError(Exception):
    """Raised when a Gmail API call returns a non-200 response.

    Propagates out of `sync`, so the pass fails without persisting its
    watermark and every message it touched (recorded or not) is retried on the
    next tick — an API outage causes delay, never a wrong classification.

    Carries the offending `status_code` when one is known, so a write executor
    can tell an insufficient-scope `403` (the token was never re-authorized for
    `gmail.modify`) apart from a generic upstream failure and fail with a clear,
    actionable message instead of crashing."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code: int | None = status_code


class GmailTriageError(Exception):
    """Raised when a triage reply carries no recoverable JSON array."""


@dataclass(frozen=True, slots=True)
class GmailResponse:
    """One Gmail HTTP response, normalized for the pure client logic."""

    status_code: int
    payload: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class GmailWriteResult:
    """The terminal result of one idempotent mailbox write.

    `outcome` is enough for a proposal-action executor to decide `succeeded`
    (`done`) versus `skipped` (`already` / `gone`) without re-inspecting the
    mailbox; `detail` carries a human-readable reason for the skip."""

    outcome: GmailWriteOutcome
    detail: str | None = None


class GmailTransport(Protocol):
    """The isolated Gmail HTTP boundary the client drives.

    Three calls: `list_messages` pulls one page of message ids matching a
    search query, `get_message` fetches one message's full payload, and
    `list_labels` resolves label names to ids (needed to recognize the
    `tether` override, whose id is not the literal label name). Faked in tests
    so the client's pagination and parsing run offline.
    """

    async def list_messages(
        self, *, query: str, page_token: str | None
    ) -> GmailResponse:
        """Fetch one page of message ids matching `query`."""
        ...

    async def get_message(self, message_id: str) -> GmailResponse:
        """Fetch one message's full payload (headers, labels, MIME body)."""
        ...

    async def list_labels(self) -> GmailResponse:
        """Fetch every label on the account, to resolve a name to its id."""
        ...

    async def modify_labels(
        self,
        message_id: str,
        *,
        add_label_ids: Sequence[str],
        remove_label_ids: Sequence[str],
    ) -> GmailResponse:
        """Add and/or remove label ids on one message (`messages.modify`).

        Itself idempotent upstream: removing an absent label or adding a present
        one is a `200` no-op, which is why the write client's staleness checks
        are a soft optimisation, not a correctness requirement."""
        ...

    async def trash_message(self, message_id: str) -> GmailResponse:
        """Move one message to Trash (`messages.trash`); reversible, never a
        permanent delete."""
        ...


class GmailTriageRunner(Protocol):
    """Runs a triage prompt through the agent and returns its final text.

    The same shape as the scheduler's and Recall's prompt runner, declared
    here so the sync service depends on a capability rather than importing
    the scheduler module."""

    async def run(self, prompt: str) -> str:
        """Run `prompt` through the agent and return its final message."""
        ...


@dataclass(frozen=True, slots=True)
class GmailMessage:
    """One Gmail message, parsed from a `messages.get` payload.

    `label_ids` drives the category pre-filter and the `tether` override;
    `body_text` is already MIME-extracted (text/plain preferred, HTML
    tag-stripped otherwise) and truncated, ready for the triage prompt."""

    message_id: str
    thread_id: str
    from_header: str
    subject: str
    date_header: str
    internal_date: datetime
    label_ids: tuple[str, ...]
    body_text: str


@dataclass(frozen=True, slots=True)
class GmailDeadline:
    """A triage verdict's extracted deadline: when, and what it is for."""

    at: datetime
    description: str


@dataclass(frozen=True, slots=True)
class GmailVerdict:
    """One message's triage verdict, validated out of the model's reply."""

    classification: Literal["noise", "interesting"]
    why: str
    deadline: GmailDeadline | None = None
    actionable: bool = False


@dataclass(frozen=True, slots=True)
class GmailSyncReport:
    """The tally of one sync pass: how each eligible message resolved."""

    prefiltered: int = 0
    noise: int = 0
    ingested: int = 0
    pending: int = 0


def _debug(logger: Logger, event: str, **context: object) -> None:
    logger.debug(event, **context)


def _info(logger: Logger, event: str, **context: object) -> None:
    logger.info(event, **context)


def _parse_iso_datetime(raw: str) -> datetime | None:
    """Parse an ISO-8601 timestamp, tolerating a trailing `Z`, else None."""
    text = raw.replace("Z", "+00:00") if raw.endswith("Z") else raw
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _decode_base64url(data: str) -> str:
    """Decode a Gmail-style unpadded base64url body part into text."""
    padded = data + "=" * (-len(data) % 4)
    try:
        return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")
    except binascii.Error:
        return ""


_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")


def _strip_html(html_body: str) -> str:
    """Strip tags from an HTML body and collapse whitespace, keeping only text."""
    without_tags = _TAG_RE.sub(" ", html_body)
    return _WHITESPACE_RE.sub(" ", without_tags).strip()


def _walk_body_parts(part: Mapping[str, object]) -> tuple[str | None, str | None]:
    """Recursively find the first text/plain and text/html leaves of a MIME part."""
    mime_type = part.get("mimeType")
    body = part.get("body")
    data = (
        cast("Mapping[str, object]", body).get("data")
        if isinstance(body, Mapping)
        else None
    )
    if isinstance(data, str) and data:
        if mime_type == "text/plain":
            return _decode_base64url(data), None
        if mime_type == "text/html":
            return None, _decode_base64url(data)
    plain: str | None = None
    html_body: str | None = None
    sub_parts = part.get("parts")
    if isinstance(sub_parts, list):
        for sub_part in cast("list[object]", sub_parts):
            if isinstance(sub_part, Mapping):
                sub_plain, sub_html = _walk_body_parts(
                    cast("Mapping[str, object]", sub_part)
                )
                plain = plain or sub_plain
                html_body = html_body or sub_html
    return plain, html_body


def _extract_body(payload: Mapping[str, object]) -> str:
    """Extract and truncate a message's plaintext body from its MIME payload.

    Prefers a text/plain part; falls back to tag-stripped text/html when no
    plain part exists. Truncated to a bounded length so triage prompts and
    token spend stay bounded regardless of message size.
    """
    plain, html_body = _walk_body_parts(payload)
    text = plain or _strip_html(html_body or "")
    return text[:_BODY_TRUNCATE_CHARS]


def _header(headers: object, name: str) -> str:
    """Case-insensitive lookup of one RFC 2822 header value, or `''`."""
    if not isinstance(headers, list):
        return ""
    for header in cast("list[object]", headers):
        if not isinstance(header, Mapping):
            continue
        header_mapping = cast("Mapping[str, object]", header)
        header_name = header_mapping.get("name")
        if isinstance(header_name, str) and header_name.lower() == name.lower():
            value = header_mapping.get("value")
            return value if isinstance(value, str) else ""
    return ""


def _parse_internal_date(raw: object) -> datetime:
    """Parse Gmail's `internalDate` (epoch milliseconds, as a string).

    Falls back to the Unix epoch on anything unparseable — internalDate is
    always present on a real message, so this only guards malformed test/fake
    payloads from crashing the parse."""
    if isinstance(raw, str) and raw.isdigit():
        return datetime.fromtimestamp(int(raw) / 1000, tz=UTC)
    return datetime.fromtimestamp(0, tz=UTC)


def _parse_message(payload: Mapping[str, object]) -> GmailMessage:
    """Parse one `messages.get` payload into a `GmailMessage`."""
    label_ids_raw = payload.get("labelIds")
    label_ids = (
        tuple(
            label
            for label in cast("list[object]", label_ids_raw)
            if isinstance(label, str)
        )
        if isinstance(label_ids_raw, list)
        else ()
    )
    mime_payload = payload.get("payload")
    headers = (
        cast("Mapping[str, object]", mime_payload).get("headers")
        if isinstance(mime_payload, Mapping)
        else None
    )
    return GmailMessage(
        message_id=str(payload.get("id", "")),
        thread_id=str(payload.get("threadId", "")),
        from_header=_header(headers, "From"),
        subject=_header(headers, "Subject"),
        date_header=_header(headers, "Date"),
        internal_date=_parse_internal_date(payload.get("internalDate")),
        label_ids=label_ids,
        body_text=(
            _extract_body(cast("Mapping[str, object]", mime_payload))
            if isinstance(mime_payload, Mapping)
            else ""
        ),
    )


class GmailClient:
    """Pagination, message parsing, and label resolution over a `GmailTransport`.

    All Gmail HTTP lives behind the injected transport, so tests drive the
    client with a scripted fake; a non-200 response raises `GmailApiError`,
    which propagates out of a sync pass and fails it (retried next tick).

    >>> client = GmailClient(transport=transport)
    >>> ids = await client.list_message_ids(query="-in:spam", logger=logger)
    >>> ids[0]
    '18abf...'
    """

    def __init__(self, transport: GmailTransport) -> None:
        self.transport: GmailTransport = transport

    async def list_message_ids(self, *, query: str, logger: Logger) -> list[str]:
        """Walk every page of a search query, returning the matched message ids."""
        _debug(logger, "Listing Gmail messages", query=query)
        ids: list[str] = []
        page_token: str | None = None
        while True:
            response = await self.transport.list_messages(
                query=query, page_token=page_token
            )
            self._require_ok(response, context="list_messages")
            messages = response.payload.get("messages")
            if isinstance(messages, list):
                ids.extend(
                    str(cast("Mapping[str, object]", entry)["id"])
                    for entry in cast("list[object]", messages)
                    if isinstance(entry, Mapping) and "id" in entry
                )
            next_token = response.payload.get("nextPageToken")
            page_token = next_token if isinstance(next_token, str) else None
            if not page_token:
                break
        _debug(logger, "Gmail message listing completed", result_count=len(ids))
        return ids

    async def get_message(self, message_id: str) -> GmailMessage:
        """Fetch and parse one message by id."""
        response = await self.transport.get_message(message_id)
        self._require_ok(response, context="get_message")
        return _parse_message(response.payload)

    async def resolve_label_id(self, name: str) -> str | None:
        """Resolve a label's display name to its Gmail id, or None if absent."""
        response = await self.transport.list_labels()
        self._require_ok(response, context="list_labels")
        labels = response.payload.get("labels")
        if not isinstance(labels, list):
            return None
        for label in cast("list[object]", labels):
            if not isinstance(label, Mapping):
                continue
            label_mapping = cast("Mapping[str, object]", label)
            if label_mapping.get("name") == name:
                label_id = label_mapping.get("id")
                return label_id if isinstance(label_id, str) else None
        return None

    async def archive(self, message_id: str) -> GmailWriteResult:
        """Archive a message by removing its `INBOX` label; idempotent.

        Fetches the message first: a `404` means it is gone (soft skip), an
        absent `INBOX` label means it is already archived (soft skip), and
        otherwise the label is removed. `messages.modify` is itself idempotent,
        so a concurrently-archived message still resolves cleanly."""
        message = await self._get_or_none(message_id)
        if message is None:
            return GmailWriteResult("gone", "message no longer exists")
        if _INBOX_LABEL not in message.label_ids:
            return GmailWriteResult("already", "message already archived")
        response = await self.transport.modify_labels(
            message_id, add_label_ids=(), remove_label_ids=(_INBOX_LABEL,)
        )
        self._require_ok(response, context="modify_labels")
        return GmailWriteResult("done")

    async def label(self, message_id: str, label_id: str) -> GmailWriteResult:
        """Add a label id to a message; idempotent.

        A `404` is gone (soft skip); a label already present is a soft skip;
        otherwise the label is added."""
        message = await self._get_or_none(message_id)
        if message is None:
            return GmailWriteResult("gone", "message no longer exists")
        if label_id in message.label_ids:
            return GmailWriteResult("already", "message already carries the label")
        response = await self.transport.modify_labels(
            message_id, add_label_ids=(label_id,), remove_label_ids=()
        )
        self._require_ok(response, context="modify_labels")
        return GmailWriteResult("done")

    async def trash(self, message_id: str) -> GmailWriteResult:
        """Move a message to Trash (never a permanent delete); idempotent.

        A `404` is gone (soft skip); a message already in `TRASH` is a soft
        skip; otherwise it is trashed."""
        message = await self._get_or_none(message_id)
        if message is None:
            return GmailWriteResult("gone", "message no longer exists")
        if _TRASH_LABEL in message.label_ids:
            return GmailWriteResult("already", "message already trashed")
        response = await self.transport.trash_message(message_id)
        self._require_ok(response, context="trash_message")
        return GmailWriteResult("done")

    async def _get_or_none(self, message_id: str) -> GmailMessage | None:
        """Fetch and parse one message, returning None on a `404` (gone)."""
        response = await self.transport.get_message(message_id)
        if response.status_code == _HTTP_NOT_FOUND:
            return None
        self._require_ok(response, context="get_message")
        return _parse_message(response.payload)

    @staticmethod
    def _require_ok(response: GmailResponse, *, context: str) -> None:
        if response.status_code != _HTTP_OK:
            message = f"Gmail {context} returned {response.status_code}"
            raise GmailApiError(message, status_code=response.status_code)


def _build_query(watermark: datetime | None) -> str:
    """Build the Gmail search query: always excludes spam/trash/sent; an
    incremental pass additionally bounds by the watermark minus the overlap."""
    base = "-in:spam -in:trash -in:sent"
    if watermark is None:
        return base
    after_epoch = int((watermark - _WATERMARK_OVERLAP).timestamp())
    return f"{base} after:{after_epoch}"


def _is_excluded_entirely(label_ids: Sequence[str]) -> bool:
    """True when a message carries spam/trash/sent, excluded unconditionally."""
    return any(label in _EXCLUDED_LABELS for label in label_ids)


def _is_category_noise(label_ids: Sequence[str]) -> bool:
    """True when a message carries a bulk category label (promo/social/forum)."""
    return any(label in _PREFILTER_CATEGORY_LABELS for label in label_ids)


class _ParsedDeadline(BaseModel):
    """Strict parse target for one triage verdict's deadline."""

    at: str
    description: str = ""


class _ParsedVerdict(BaseModel):
    """Strict parse target for one triage verdict."""

    message_id: str
    classification: Literal["noise", "interesting"]
    why: str = ""
    deadline: _ParsedDeadline | None = None
    actionable: bool = False


def _extract_json_array(text: str) -> str:
    """Slice the outermost JSON array from a model reply, tolerating stray prose."""
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end < start:
        message = "triage reply contained no JSON array"
        raise GmailTriageError(message)
    return text[start : end + 1]


def _parse_verdicts(
    reply: str, *, eligible_ids: frozenset[str]
) -> dict[str, GmailVerdict]:
    """Defensively parse a triage reply into per-message verdicts.

    Each array entry is validated independently: a malformed entry, an entry
    for an id outside this batch, a duplicate id, or an unparseable deadline
    is dropped rather than failing the whole batch — that message simply stays
    pending (absent from the returned mapping) for the next pass. A reply with
    no JSON array at all, or JSON that isn't an array, yields no verdicts.
    """
    try:
        raw = json.loads(_extract_json_array(reply))
    except GmailTriageError, json.JSONDecodeError:
        return {}
    if not isinstance(raw, list):
        return {}
    verdicts: dict[str, GmailVerdict] = {}
    for entry in cast("list[object]", raw):
        if not isinstance(entry, Mapping):
            continue
        try:
            parsed = _ParsedVerdict.model_validate(entry)
        except ValidationError:
            continue
        if parsed.message_id not in eligible_ids or parsed.message_id in verdicts:
            continue
        deadline: GmailDeadline | None = None
        if parsed.deadline is not None:
            deadline_at = _parse_iso_datetime(parsed.deadline.at)
            if deadline_at is None:
                continue
            deadline = GmailDeadline(
                at=deadline_at, description=parsed.deadline.description
            )
        verdicts[parsed.message_id] = GmailVerdict(
            classification=parsed.classification,
            why=parsed.why,
            deadline=deadline,
            actionable=parsed.actionable,
        )
    return verdicts


_TRIAGE_INSTRUCTIONS = """\
You are triaging a batch of emails for a personal assistant. For each email, \
decide whether it is noise (bulk/automated mail with nothing to act on) or \
interesting (worth remembering or acting on).

Return ONLY a JSON array (no prose, no code fences) with one object per email, \
in this exact shape:
[
  {{
    "message_id": "<the email's id, copied exactly>",
    "classification": "noise" or "interesting",
    "why": "<one line explaining the verdict>",
    "deadline": {{"at": "<ISO 8601 datetime>", "description": "<what it is for>"}},
    "actionable": <true when the email asks the recipient to do something with \
no deadline, else false>
  }}
]

Omit "deadline" entirely when the email carries no deadline. Every email in \
the batch must get exactly one verdict object.

Emails:
{messages}
"""


def _format_message_for_prompt(message: GmailMessage) -> str:
    """Render one message's triage-relevant fields for the batch prompt."""
    return (
        f"id: {message.message_id}\n"
        f"From: {message.from_header}\n"
        f"Subject: {message.subject}\n"
        f"Date: {message.date_header}\n"
        f"Labels: {', '.join(message.label_ids)}\n"
        f"Body:\n{message.body_text}"
    )


def _build_triage_prompt(batch: Sequence[GmailMessage]) -> str:
    """Build the batch triage prompt for a group of eligible messages."""
    return _TRIAGE_INSTRUCTIONS.format(
        messages="\n---\n".join(
            _format_message_for_prompt(message) for message in batch
        )
    )


def _chunk(items: Sequence[GmailMessage], size: int) -> list[list[GmailMessage]]:
    """Split `items` into consecutive groups of at most `size`."""
    return [list(items[start : start + size]) for start in range(0, len(items), size)]


def _excerpt(body_text: str) -> str:
    """A short, ellipsis-terminated excerpt of a message body for a Memory."""
    trimmed = body_text.strip()
    if len(trimmed) <= _EXCERPT_CHARS:
        return trimmed
    return trimmed[:_EXCERPT_CHARS].rstrip() + "…"


def _resolve_zone(tz_name: str) -> tzinfo:
    """Resolve an IANA zone name, degrading to UTC for an unresolvable one.

    `tz_name` may be the numeric-offset fallback `local_timezone_name` returns
    when no IANA name is determinable (e.g. `"+0200"`), which `ZoneInfo` cannot
    parse — treated the same as an unknown zone, since a best-effort local fire
    time is still better than crashing the sync pass over it.
    """
    try:
        return ZoneInfo(tz_name)
    except ZoneInfoNotFoundError, ValueError:
        return UTC


def _trigger_fire_at(deadline_at: datetime, *, now: datetime, tz_name: str) -> datetime:
    """The morning-before fire time for a deadline trigger, clamped to near-now.

    Fires at 09:00 **local** time (`tz_name`) the day before the deadline —
    the deadline is converted into the local zone first so "the day before"
    matches the local calendar date, then the local 09:00 slot is converted
    back to UTC for storage. When that slot has already passed (a near or
    same-day deadline), the fire time is clamped to shortly after `now`
    instead, so the reminder still arrives rather than being silently
    rejected as a past `fire_at`.
    """
    zone = _resolve_zone(tz_name)
    local_deadline = deadline_at.astimezone(zone)
    day_before = (local_deadline - timedelta(days=1)).date()
    local_fire_at = datetime.combine(
        day_before, time(_DEADLINE_FIRE_HOUR, 0), tzinfo=zone
    )
    fire_at = local_fire_at.astimezone(UTC)
    if fire_at <= now:
        return now + timedelta(minutes=_PAST_FIRE_CLAMP_MINUTES)
    return fire_at


def _trigger_message(message: GmailMessage, deadline: GmailDeadline) -> str:
    """The self-contained reminder text: the deadline, its purpose, and the email."""
    return (
        f"Deadline {deadline.at.isoformat()} — {deadline.description}. "
        f'Email from {message.from_header}: "{message.subject}"'
    )


class GmailMessageRecord[S = Pending](Model[S, "GmailMessageRecord[Fetched]"]):
    """Idempotency + audit row: one Gmail message id to its resting status.

    A message id absent from this table is implicitly pending. `memory_id` and
    `trigger_id` are populated only for `ingested` rows that produced them."""

    message_id: GmailMessageRecord.Col[str] = Text(primary_key=True)
    status: GmailMessageRecord.Col[GmailMessageStatus] = Text()
    memory_id: GmailMessageRecord.Col[str | None] = Text(default=None, nullable=True)
    trigger_id: GmailMessageRecord.Col[str | None] = Text(default=None, nullable=True)
    internal_date: GmailMessageRecord.Col[str] = Text()
    created_at: GmailMessageRecord.GenCol[datetime] = Text(default=CurrentTimestamp)


class GmailSyncState[S = Pending](Model[S, "GmailSyncState[Fetched]"]):
    """Durable key/value sync state (the pass watermark), across restarts."""

    key: GmailSyncState.Col[str] = Text(primary_key=True)
    value: GmailSyncState.Col[str] = Text(nullable=False)


class GmailSyncService:
    """Reconciler-shaped Gmail ingestion worker over `MemoryService`/`TriggerService`.

    An idempotent `sync` pass (run at boot and on a periodic loop): lists
    eligible message ids for the watermark window, pre-filters bulk categories
    without spending a model call, triages the remainder in batches through the
    injected `GmailTriageRunner`, and folds each verdict into a tethered Memory
    (and, for a future deadline, a one-shot trigger) against the
    `gmail_message` idempotency table. Any exception (a Gmail API failure, an
    unusable triage reply) propagates out of `sync`, so the watermark is
    persisted only when the whole pass completes — an outage delays ingestion
    rather than risking a wrong classification.
    """

    def __init__(  # noqa: PLR0913 - each param is an independent collaborator/knob
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
        self.database: Database = database
        self.client: GmailClient = client
        self.memory_service: MemoryService = memory_service
        self.trigger_service: TriggerService = trigger_service
        self.todo_service: TodoService = todo_service
        self.triage_runner: GmailTriageRunner = triage_runner
        self.triage_batch_size: int = triage_batch_size
        self.timezone_name_provider: Callable[[datetime], str] = timezone_name_provider
        """Resolves the OS-local IANA zone a deadline trigger's 09:00 fire time is
        computed in. Defaults to `local_timezone_name` (the same OS probe
        `chat_ws` uses); tests inject a fixed callable for a deterministic zone
        instead of depending on the host's real local timezone."""

    async def sync(self, *, logger: Logger) -> GmailSyncReport:
        """Run one idempotent pass; persist the watermark only if it completes."""
        started_at = datetime.now(UTC)
        watermark = await self._read_watermark()
        _debug(
            logger,
            "Gmail sync starting",
            incremental=watermark is not None,
            watermark=watermark.isoformat() if watermark is not None else None,
        )
        tether_label_id = await self.client.resolve_label_id(_TETHER_LABEL_NAME)

        # Every pass first re-fetches and re-triages every `pending` row,
        # regardless of the watermark window — a malformed/missing verdict
        # must never be lost to the watermark advancing past it. Any of these
        # ids still present in the watermark-window listing below are simply
        # skipped there (already recorded), so each is triaged at most once.
        eligible: list[GmailMessage] = [
            await self.client.get_message(record.message_id)
            for record in await self._fetch_pending_records()
        ]

        message_ids = await self.client.list_message_ids(
            query=_build_query(watermark), logger=logger
        )
        prefiltered = 0
        for message_id in message_ids:
            if await self._already_recorded(message_id):
                continue
            message = await self.client.get_message(message_id)
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
        noise = ingested = pending = 0
        for batch in _chunk(eligible, self.triage_batch_size):
            eligible_ids = frozenset(message.message_id for message in batch)
            reply = await self.triage_runner.run(_build_triage_prompt(batch))
            verdicts = _parse_verdicts(reply, eligible_ids=eligible_ids)
            by_id = {message.message_id: message for message in batch}
            for message_id in eligible_ids:
                verdict = verdicts.get(message_id)
                if verdict is None:
                    # Missing/malformed verdict: stamp an explicit `pending`
                    # row (upserting over any prior one) so this message is
                    # retried by the pending-retry path above on every future
                    # pass, however far the watermark has since moved.
                    await self._record_status(by_id[message_id], status="pending")
                    pending += 1
                    continue
                tether_override = (
                    tether_label_id is not None
                    and tether_label_id in by_id[message_id].label_ids
                )
                outcome = await self._apply_verdict(
                    by_id[message_id],
                    verdict,
                    force_interesting=tether_override,
                    now=started_at,
                    logger=logger,
                )
                if outcome == "noise":
                    noise += 1
                else:
                    ingested += 1
        await self._store_watermark(started_at)
        _info(
            logger,
            "Gmail sync completed",
            prefiltered=prefiltered,
            noise=noise,
            ingested=ingested,
            pending=pending,
        )
        return GmailSyncReport(
            prefiltered=prefiltered, noise=noise, ingested=ingested, pending=pending
        )

    async def sync_forever(self, *, interval_seconds: float, logger: Logger) -> None:
        """Run sync passes on the given interval until cancelled.

        Mirrors the other ingestion workers: a failed pass is logged with its
        traceback and the loop survives, so a transient Gmail or model outage
        does not take the worker down.
        """
        while True:
            await asyncio.sleep(interval_seconds)
            try:
                _ = await self.sync(logger=logger)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Gmail sync pass failed")

    async def _apply_verdict(
        self,
        message: GmailMessage,
        verdict: GmailVerdict,
        *,
        force_interesting: bool,
        now: datetime,
        logger: Logger,
    ) -> str:
        """Fold one verdict into a noise record or an ingested Memory (+ trigger).

        A `tether`-labeled message cannot resolve `noise`: its verdict is
        overridden to `interesting` while its extracted deadline/action still
        apply, per the force-ingest override.
        """
        classification = "interesting" if force_interesting else verdict.classification
        if classification == "noise":
            await self._record_status(message, status="noise")
            return "noise"
        memory = await self._capture_memory(message, verdict, logger=logger)
        # Record the idempotency row immediately after capture — before the
        # trigger or Todo is attempted — so a failure past this point (trigger
        # creation raising, failing the whole pass) never re-captures a
        # second Memory on retry: `_already_recorded` already sees this row
        # and skips the message entirely next time. The tradeoff is that a
        # deadline trigger (or the Todo) that fails to create here does not get
        # a second attempt; the Memory itself is never duplicated, which is the
        # invariant that matters (mirrors the record-then-link ordering
        # `readwise.py._create_highlight` uses for its own mapping row).
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
        # An actionable email becomes a Todo (the construct that replaces the
        # old `action: pending` facet), linked back to the captured Memory and
        # to the deadline trigger when one was created. Un-gated, at parity with
        # the gate's shipped Memory/trigger writes.
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
        """Capture an interesting email as a tethered Memory with gmail provenance."""
        content = f"{verdict.why}\n\n{_excerpt(message.body_text)}"
        facets: dict[str, str] = {
            "source": "gmail",
            "sender": message.from_header,
            "subject": message.subject,
            "date": message.internal_date.isoformat(),
        }
        if verdict.deadline is not None:
            facets["deadline"] = verdict.deadline.at.isoformat()
        return await self.memory_service.capture_tethered(
            content,
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
        """Create the one-shot deadline trigger, firing the morning before."""
        tz_name = self.timezone_name_provider(now)
        trigger = await self.trigger_service.create(
            TriggerSpec(
                recurrence="once",
                action_kind="message",
                payload=_trigger_message(message, deadline),
                fire_at=_trigger_fire_at(deadline.at, now=now, tz_name=tz_name),
            ),
            now=now,
            logger=logger,
        )
        return trigger.id

    async def _already_recorded(self, message_id: str) -> bool:
        """True when a message id already has an idempotency row (any status).

        A `pending` row counts as recorded here too: it is retried exactly
        once per pass, via the dedicated pending-retry step at the top of
        `sync` — never a second time by also falling through this
        watermark-window listing loop.
        """
        async with self.database.transaction() as tx:
            row = await tx.fetch_one_or_none(
                select(GmailMessageRecord).where(
                    GmailMessageRecord.message_id.eq(message_id)
                )
            )
        return row is not None

    async def _fetch_pending_records(self) -> list[GmailMessageRecord[Fetched]]:
        """Every message currently resting in `pending` status, any pass."""
        async with self.database.transaction() as tx:
            return await tx.fetch_all(
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
        """Upsert the idempotency row for one processed message.

        An insert for a message id with no prior row; an update otherwise —
        a message can cycle `pending` -> `pending` (still unresolved) or
        `pending` -> a final status, and an already-`ingested` row is updated
        a second time once its trigger is created (see `_apply_verdict`).
        """

        async def _upsert(tx: Transaction) -> None:
            existing = await tx.fetch_one_or_none(
                select(GmailMessageRecord).where(
                    GmailMessageRecord.message_id.eq(message.message_id)
                )
            )
            if existing is None:
                _ = await tx.execute(
                    insert(
                        GmailMessageRecord(
                            message_id=message.message_id,
                            status=status,
                            memory_id=memory_id,
                            trigger_id=trigger_id,
                            internal_date=message.internal_date.isoformat(),
                        )
                    )
                )
            else:
                _ = await tx.execute(
                    update(GmailMessageRecord)
                    .set(
                        GmailMessageRecord.status.to(status),
                        GmailMessageRecord.memory_id.to(memory_id),
                        GmailMessageRecord.trigger_id.to(trigger_id),
                    )
                    .where(GmailMessageRecord.message_id.eq(message.message_id))
                )

        await run_in_transaction(self.database, _upsert)

    async def _read_watermark(self) -> datetime | None:
        """The last fully successful pass's start time, or None on first sync."""
        async with self.database.transaction() as tx:
            row = await tx.fetch_one_or_none(
                select(GmailSyncState).where(GmailSyncState.key.eq(_WATERMARK_KEY))
            )
        return _parse_iso_datetime(row.value) if row is not None else None

    async def _store_watermark(self, watermark: datetime) -> None:
        """Persist the watermark, upserting the single sync-state row."""

        async def _set(tx: Transaction) -> None:
            existing = await tx.fetch_one_or_none(
                select(GmailSyncState).where(GmailSyncState.key.eq(_WATERMARK_KEY))
            )
            if existing is None:
                _ = await tx.execute(
                    insert(
                        GmailSyncState(key=_WATERMARK_KEY, value=watermark.isoformat())
                    )
                )
            else:
                _ = await tx.execute(
                    update(GmailSyncState)
                    .set(GmailSyncState.value.to(watermark.isoformat()))
                    .where(GmailSyncState.key.eq(_WATERMARK_KEY))
                )

        await run_in_transaction(self.database, _set)


_GMAIL_MIGRATIONS: dict[str, str] = {
    # Idempotency + audit table, keyed by Gmail's stable string message id.
    "001_create_gmail_message": (
        'CREATE TABLE "gmail_message_record" ('
        '"message_id" TEXT PRIMARY KEY NOT NULL, '
        '"status" TEXT NOT NULL, '
        '"memory_id" TEXT, '
        '"trigger_id" TEXT, '
        '"internal_date" TEXT NOT NULL, '
        "\"created_at\" TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))"
        ") STRICT"
    ),
    # Sync-state key/value store (the pass watermark).
    "002_create_gmail_sync_state": (
        'CREATE TABLE "gmail_sync_state" ('
        '"key" TEXT PRIMARY KEY NOT NULL, "value" TEXT NOT NULL'
        ") STRICT"
    ),
}


async def create_gmail_schema(database: Database) -> None:
    """Bring the Gmail ingestion schema to current on an initialized database.

    >>> from snekql.sqlite import Config
    >>> database = await Database.initialize(backend=Config(database=":memory:"))
    >>> await create_gmail_schema(database)
    """
    await database.migrate(_GMAIL_MIGRATIONS)


__all__ = [
    "DEFAULT_TRIAGE_BATCH_SIZE",
    "GmailApiError",
    "GmailClient",
    "GmailDeadline",
    "GmailMessage",
    "GmailMessageRecord",
    "GmailMessageStatus",
    "GmailResponse",
    "GmailSyncReport",
    "GmailSyncService",
    "GmailSyncState",
    "GmailTransport",
    "GmailTriageError",
    "GmailTriageRunner",
    "GmailVerdict",
    "GmailWriteOutcome",
    "GmailWriteResult",
    "create_gmail_schema",
]
