"""Structlog processors for stable fields, exception, and trace correlation."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, cast

import structlog
from structlog.typing import EventDict, WrappedLogger

try:
    from opentelemetry.trace import get_current_span
except ModuleNotFoundError as error:
    if error.name not in {"opentelemetry", "opentelemetry.trace"}:
        raise
    _get_current_span: Callable[[], Any] | None = None
else:
    _get_current_span = get_current_span


def _capture_bound_context(
    _logger: WrappedLogger,
    _method_name: str,
    event_dict: EventDict,
) -> EventDict:
    """Remember context-bound fields before structlog merges them into the event."""
    event_dict["_bound_context_keys"] = set(structlog.contextvars.get_contextvars())
    return event_dict


def _process_positional_args(
    _logger: WrappedLogger,
    _method_name: str,
    event_dict: EventDict,
) -> EventDict:
    """Preserve stdlib positional logging arguments as rendered event text."""
    positional_args = event_dict.pop("positional_args", ())
    if not positional_args:
        return event_dict

    message_args: list[Any]
    format_args: object
    if isinstance(positional_args, Mapping):
        mapping_args = cast("Mapping[object, object]", positional_args)
        format_args = mapping_args
        message_args = [mapping_args]
    else:
        message_args = list(cast("tuple[Any, ...]", positional_args))
        if len(message_args) == 1 and isinstance(message_args[0], Mapping):
            format_args = cast("Mapping[object, object]", message_args[0])
        else:
            format_args = tuple(message_args)

    event = event_dict.get("event")
    if isinstance(event, str):
        try:
            event_dict["event"] = event % format_args
        except KeyError, TypeError, ValueError:
            event_dict["event"] = " ".join([event, *map(str, message_args)])
    elif event is None:
        event_dict["event"] = " ".join(map(str, message_args))
    else:
        event_dict["event"] = " ".join([str(event), *map(str, message_args)])
    return event_dict


def _add_trace_context(
    _logger: WrappedLogger,
    _method_name: str,
    event_dict: EventDict,
) -> EventDict:
    """Attach active OpenTelemetry ids for log and span correlation."""
    if _get_current_span is None:
        return event_dict
    span_context = _get_current_span().get_span_context()
    if span_context.is_valid:
        event_dict["trace_id"] = f"{span_context.trace_id:032x}"
        event_dict["span_id"] = f"{span_context.span_id:016x}"
    return event_dict


def _reorder_fields(
    _logger: WrappedLogger,
    _method_name: str,
    event_dict: EventDict,
) -> EventDict:
    """Place scanning fields first and request context after call-site details."""
    bound_context_keys = set(event_dict.pop("_bound_context_keys", set()))
    reordered: EventDict = {}
    for field_name in ("timestamp", "level", "logger", "event"):
        if field_name in event_dict:
            reordered[field_name] = event_dict.pop(field_name)
    for field_name in sorted(
        key for key in event_dict if key not in bound_context_keys
    ):
        reordered[field_name] = event_dict[field_name]
    for field_name in sorted(key for key in event_dict if key in bound_context_keys):
        reordered[field_name] = event_dict[field_name]
    return reordered


def shared_logging_processors(
    *, format_exceptions: bool
) -> list[structlog.types.Processor]:
    """Build processors shared by structlog and foreign stdlib records."""
    processors: list[structlog.types.Processor] = [
        _capture_bound_context,
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True, key="timestamp"),
        structlog.processors.StackInfoRenderer(),
    ]
    if format_exceptions:
        processors.append(structlog.processors.format_exc_info)
    processors.extend(
        [
            structlog.processors.UnicodeDecoder(),
            _process_positional_args,
            _add_trace_context,
            _reorder_fields,
        ]
    )
    return processors
