"""Structured logging setup for Starlette servers.

```python
from starlette.applications import Starlette

from tether.logging import ContextLoggerMiddleware, configure_logging

logger = configure_logging()
app = Starlette()
app.state.logger = logger
app.add_middleware(ContextLoggerMiddleware)
```
"""

from __future__ import annotations

import logging
import sys
import time
from contextvars import ContextVar
from typing import Any, cast
from uuid import uuid4

import structlog
from opentelemetry import trace
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response
from structlog.typing import EventDict, WrappedLogger

type Logger = structlog.stdlib.BoundLogger


QUIET_LOGGERS = ("watchfiles.main", "uvicorn", "uvicorn.error")
"""Server loggers that share the root handler but emit warnings only.

The `uvicorn`/`uvicorn.error` pair is quieted rather than silenced: `serve()`
runs uvicorn with `log_config=None`, so uvicorn configures no logging of its
own. Fully silencing these would discard uvicorn's ERROR-level output —
including the lifespan *startup-failure* traceback it logs on `uvicorn.error`
with `exc_info` — leaving a misconfigured deploy to crash-loop (`restart:` +
exit 3) with nothing in the logs. They must stay quiet *and* propagate: the
parent `uvicorn` keeps `propagate=True` so a child `uvicorn.error` record can
reach the structlog root handler instead of stdlib's last-resort stderr sink.
At WARNING the routine INFO lifecycle chatter ("Application startup complete.")
stays suppressed while genuine failures surface through structlog."""

SILENCED_LOGGERS = ("uvicorn.access",)
"""Uvicorn loggers that are fully disabled because uvicorn owns its formatting."""
_REQUEST_LOGGER: ContextVar[Logger | None] = ContextVar(
    "tether_request_logger",
    default=None,
)


def _clear_handlers(logger: logging.Logger) -> None:
    """Remove and close handlers so reconfiguration owns all output sinks."""
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()


def _capture_bound_context(
    _logger: WrappedLogger,
    _method_name: str,
    event_dict: EventDict,
) -> EventDict:
    """Remember contextvar-bound keys before they merge into the event.

    `_reorder_fields` needs to distinguish request-scoped fields from ordinary
    call-site keywords after `merge_contextvars` has flattened both into one
    event dictionary.
    """
    try:
        event_dict["_bound_context_keys"] = set(structlog.contextvars.get_contextvars())
    except Exception:
        event_dict["_bound_context_keys"] = set()
    return event_dict


def _process_positional_args(
    _logger: WrappedLogger,
    _method_name: str,
    event_dict: EventDict,
) -> EventDict:
    """Preserve stdlib-style positional log arguments as structured output."""
    positional_args = event_dict.pop("positional_args", ())
    if not positional_args:
        return event_dict

    message_args: list[Any] = []
    for positional_arg in positional_args:
        if isinstance(positional_arg, dict):
            event_dict.update(cast("dict[str, Any]", positional_arg))
        else:
            message_args.append(positional_arg)

    if not message_args:
        return event_dict

    event = event_dict.get("event")
    if isinstance(event, str):
        try:
            event_dict["event"] = event % tuple(message_args)
        except Exception:
            event_dict["event"] = " ".join([event, *[str(arg) for arg in message_args]])
    elif event is None:
        event_dict["event"] = " ".join(str(arg) for arg in message_args)
    else:
        event_dict["event"] = " ".join(
            [str(event), *[str(arg) for arg in message_args]]
        )
    return event_dict


def _add_trace_context(
    _logger: WrappedLogger,
    _method_name: str,
    event_dict: EventDict,
) -> EventDict:
    """Attach active span ids so logs can be correlated with traces."""
    span_context = trace.get_current_span().get_span_context()
    if span_context.is_valid:
        event_dict["trace_id"] = f"{span_context.trace_id:032x}"
        event_dict["span_id"] = f"{span_context.span_id:016x}"
    return event_dict


def _reorder_fields(
    _logger: WrappedLogger,
    _method_name: str,
    event_dict: EventDict,
) -> EventDict:
    """Put scanning fields first and request context after call-site details."""
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


def _shared_processors(*, is_tty: bool) -> list[structlog.types.Processor]:
    processors: list[structlog.types.Processor] = [
        _capture_bound_context,
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(
            fmt="%Y-%m-%d %H:%M:%S",
            key="timestamp",
        ),
        structlog.processors.StackInfoRenderer(),
    ]
    if not is_tty:
        processors.append(structlog.processors.format_exc_info)
    processors.extend(
        [
            structlog.processors.UnicodeDecoder(),
            _process_positional_args,
            _add_trace_context,
            _reorder_fields,
        ],
    )
    return processors


def _configure_quiet_loggers() -> None:
    """Route non-uvicorn logs through root and disable uvicorn output."""
    for logger_name in QUIET_LOGGERS:
        logger = logging.getLogger(logger_name)
        _clear_handlers(logger)
        logger.setLevel(logging.WARNING)
        logger.propagate = True
        logger.disabled = False

    for logger_name in SILENCED_LOGGERS:
        logger = logging.getLogger(logger_name)
        _clear_handlers(logger)
        logger.propagate = False
        logger.disabled = True


def configure_logging(
    log_level: str = "INFO", *, force_tty: bool | None = None
) -> Logger:
    """Configure structlog and stdlib logging for a Starlette process.

    ```python
    logger = configure_logging("DEBUG", force_tty=True)
    logger.info("Server starting")
    ```
    """
    is_tty = sys.stdout.isatty() if force_tty is None else force_tty
    renderer: structlog.types.Processor
    if is_tty:
        renderer = structlog.dev.ConsoleRenderer(colors=True)
    else:
        renderer = structlog.processors.JSONRenderer()

    processors = _shared_processors(is_tty=is_tty)
    structlog.configure(
        processors=[
            *processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=processors,
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                renderer,
            ],
            pass_foreign_args=True,
            use_get_message=False,
        ),
    )

    root_logger = logging.getLogger()
    _clear_handlers(root_logger)
    root_logger.addHandler(handler)
    root_logger.setLevel(log_level)
    _configure_quiet_loggers()
    return cast("Logger", structlog.wrap_logger(logging.getLogger()))


def get_bound_request_logger() -> Logger | None:
    """Return the logger bound to the active request, if any.

    ```python
    logger = get_bound_request_logger()
    if logger is not None:
        logger.info("Inside request")
    ```
    """
    return _REQUEST_LOGGER.get()


def get_request_logger(request: Request) -> Logger:
    """Return the logger installed on a request by `ContextLoggerMiddleware`.

    ```python
    async def endpoint(request):
        get_request_logger(request).info("Handling request")
    ```
    """
    logger = getattr(request.state, "logger", None)
    if logger is None:
        error_message = "ContextLoggerMiddleware is not configured for this request."
        raise RuntimeError(error_message)
    return cast("Logger", logger)


class ContextLoggerMiddleware(BaseHTTPMiddleware):
    """Bind request metadata to logs for each Starlette request.

    ```python
    app.state.logger = configure_logging()
    app.add_middleware(ContextLoggerMiddleware)
    ```
    """

    def __init__(self, app: Any) -> None:
        super().__init__(app)

    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        base_logger = cast("Logger", request.app.state.logger)
        request_logger = base_logger.bind(
            request_id=str(uuid4()),
            method=request.method,
            path=request.url.path,
            client_ip=request.client.host if request.client is not None else None,
            user_agent=request.headers.get("user-agent"),
        )
        request.state.logger = request_logger
        token = _REQUEST_LOGGER.set(request_logger)
        started_at = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            request_logger.exception(
                "Request failed",
                duration_ms=round((time.perf_counter() - started_at) * 1000, 3),
            )
            raise
        else:
            request_logger.debug(
                "Request completed",
                status_code=response.status_code,
                duration_ms=round((time.perf_counter() - started_at) * 1000, 3),
            )
            return response
        finally:
            _REQUEST_LOGGER.reset(token)


__all__ = [
    "QUIET_LOGGERS",
    "SILENCED_LOGGERS",
    "ContextLoggerMiddleware",
    "Logger",
    "configure_logging",
    "get_bound_request_logger",
    "get_request_logger",
]
