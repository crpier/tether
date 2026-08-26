"""Transport-independent contracts shared by host capabilities."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from tether.bucket_item_store import BucketItemProvenance

type ToolErrorCode = Literal[
    "invalid_input",
    "not_found",
    "conflict",
]


@dataclass(frozen=True, slots=True)
class ErrorRule:
    """Tool error code for one family of expected domain exceptions."""

    exceptions: tuple[type[Exception], ...]
    code: ToolErrorCode


@dataclass(frozen=True, slots=True)
class CapabilityOutcome:
    """JSON-ready capability result before REST or tool presentation."""

    result: Any
    provenance: BucketItemProvenance | None = None


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
