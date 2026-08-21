"""Transport-independent contracts shared by host capabilities."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel

from tether.bucket_item_store import BucketItemProvenance
from tether.memory_store import MemoryProvenance


class CacheMeta(BaseModel):
    """Whether a result was served from the local cache or fetched live.

    >>> CacheMeta(hit=False, source="live").source
    'live'
    """

    hit: bool
    source: Literal["live", "cache"]


class QuotaMeta(BaseModel):
    """The day's quota budget snapshot a guarded call reports.

    >>> QuotaMeta(limit=100, used=3, remaining=97).remaining
    97
    """

    limit: int
    used: int
    remaining: int


type ToolErrorCode = Literal[
    "invalid_input",
    "not_found",
    "conflict",
    "quota_exceeded",
    "upstream_error",
    "transcript_needs_review",
    "transcript_unavailable",
]


@dataclass(frozen=True, slots=True)
class ErrorRule:
    """Presentation mapping for one family of expected domain exceptions."""

    exceptions: tuple[type[Exception], ...]
    code: ToolErrorCode
    status: int
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class CapabilityOutcome:
    """JSON-ready capability result before REST or tool presentation."""

    result: Any
    provenance: MemoryProvenance | BucketItemProvenance | None = None
    quota: QuotaMeta | None = None
    cache: CacheMeta | None = None


def catchable_exceptions(rules: tuple[ErrorRule, ...]) -> tuple[type[Exception], ...]:
    """Flatten a rule table into the exception tuple its boundary may catch."""
    return tuple(dict.fromkeys(error for rule in rules for error in rule.exceptions))


def match_rule(rules: tuple[ErrorRule, ...], error: Exception) -> ErrorRule:
    """Return the first rule matching an expected domain exception."""
    for rule in rules:
        if isinstance(error, rule.exceptions):
            return rule
    raise error


__all__ = [
    "CapabilityOutcome",
    "ErrorRule",
    "ToolErrorCode",
    "catchable_exceptions",
    "match_rule",
]
