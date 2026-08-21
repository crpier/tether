"""Gmail message triage parsing, prompts, and deadline policy."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta, tzinfo
from typing import Literal, Protocol, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ValidationError

from tether.gmail.client import GmailMessage

_DEADLINE_FIRE_HOUR = 9
_EXCERPT_CHARS = 200
_PAST_FIRE_CLAMP_MINUTES = 5


class GmailTriageRunner(Protocol):
    """Run one Gmail prompt through the configured model."""

    async def run(self, prompt: str) -> str:
        """Return the model's final text."""
        ...


@dataclass(frozen=True, slots=True)
class GmailDeadline:
    """A triage verdict's extracted deadline."""

    at: datetime
    description: str


@dataclass(frozen=True, slots=True)
class GmailVerdict:
    """One validated message triage verdict."""

    classification: Literal["noise", "interesting"]
    why: str
    actionable: bool = False
    deadline: GmailDeadline | None = None


class _ParsedDeadline(BaseModel):
    """Strict provider reply shape for one deadline."""

    at: str
    description: str = ""


class _ParsedVerdict(BaseModel):
    """Strict provider reply shape for one message verdict."""

    actionable: bool = False
    classification: Literal["noise", "interesting"]
    deadline: _ParsedDeadline | None = None
    message_id: str
    why: str = ""


def _parse_iso_datetime(raw: str) -> datetime | None:
    """Parse an aware ISO timestamp while accepting a trailing `Z`."""
    text = raw.replace("Z", "+00:00") if raw.endswith("Z") else raw
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _extract_json_array(text: str) -> str | None:
    """Extract the outermost JSON array while tolerating surrounding prose."""
    start = text.find("[")
    end = text.rfind("]")
    return None if start == -1 or end == -1 or end < start else text[start : end + 1]


def parse_gmail_verdicts(
    reply: str, *, eligible_ids: frozenset[str]
) -> dict[str, GmailVerdict]:
    """Parse independent verdict entries, dropping malformed or foreign ones."""
    json_array = _extract_json_array(reply)
    if json_array is None:
        return {}
    try:
        raw = json.loads(json_array)
    except json.JSONDecodeError:
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
            actionable=parsed.actionable,
            classification=parsed.classification,
            deadline=deadline,
            why=parsed.why,
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
    """Render one message's triage-relevant fields."""
    return (
        f"id: {message.message_id}\n"
        f"From: {message.from_header}\n"
        f"Subject: {message.subject}\n"
        f"Date: {message.date_header}\n"
        f"Labels: {', '.join(message.label_ids)}\n"
        f"Body:\n{message.body_text}"
    )


def build_gmail_triage_prompt(messages: Sequence[GmailMessage]) -> str:
    """Build one bounded-batch message triage prompt."""
    return _TRIAGE_INSTRUCTIONS.format(
        messages="\n---\n".join(
            _format_message_for_prompt(message) for message in messages
        )
    )


def gmail_message_excerpt(body_text: str) -> str:
    """Build the bounded body excerpt persisted in a Memory."""
    trimmed = body_text.strip()
    if len(trimmed) <= _EXCERPT_CHARS:
        return trimmed
    return trimmed[:_EXCERPT_CHARS].rstrip() + "…"


def _resolve_zone(timezone_name: str) -> tzinfo:
    """Resolve an IANA timezone while degrading unresolvable names to UTC."""
    try:
        return ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError, ValueError:
        return UTC


def gmail_deadline_fire_at(
    deadline_at: datetime, *, now: datetime, timezone_name: str
) -> datetime:
    """Schedule 09:00 local time one day before, clamped just after `now`."""
    zone = _resolve_zone(timezone_name)
    day_before = (deadline_at.astimezone(zone) - timedelta(days=1)).date()
    fire_at = datetime.combine(
        day_before, time(_DEADLINE_FIRE_HOUR), tzinfo=zone
    ).astimezone(UTC)
    if fire_at <= now:
        return now + timedelta(minutes=_PAST_FIRE_CLAMP_MINUTES)
    return fire_at


def gmail_trigger_message(message: GmailMessage, deadline: GmailDeadline) -> str:
    """Build a self-contained reminder from a message deadline."""
    return (
        f"Deadline {deadline.at.isoformat()} — {deadline.description}. "
        f'Email from {message.from_header}: "{message.subject}"'
    )


__all__ = [
    "GmailDeadline",
    "GmailTriageRunner",
    "GmailVerdict",
    "build_gmail_triage_prompt",
    "gmail_deadline_fire_at",
    "gmail_message_excerpt",
    "gmail_trigger_message",
    "parse_gmail_verdicts",
]
