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
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import structlog
from opentelemetry import trace
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response
from structlog.typing import EventDict, WrappedLogger

type Logger = structlog.stdlib.BoundLogger


QUIET_LOGGERS = (
    "watchfiles.main",
    "uvicorn",
    "uvicorn.error",
    "aiosqlite",
    "snekql",
    "httpcore2",
)
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
stays suppressed while genuine failures surface through structlog.

`aiosqlite` and `snekql` are quieted for a different reason: at DEBUG they emit
a line per cursor/commit/SQL statement (and child loggers `snekql.runtime`,
`snekql.sqlite.pool` inherit the parent level), which drowns the app's own DEBUG
logs. WARNING keeps their genuine errors while dropping the per-query noise.

`httpcore2` is quieted for the same reason: at DEBUG its connection backend
(`httpcore2.connection`, `httpcore2.http11` children inherit the level) logs a
line per TCP connect / TLS handshake / request-header/body frame for every
outbound HTTP call, which buries the app's own logs. WARNING drops that spam;
the `httpx2` request/response summary (one INFO line per call, e.g. the Supadata
transcript fetch and its status) is a separate logger and stays."""

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


def _shared_processors(*, format_exceptions: bool) -> list[structlog.types.Processor]:
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
    if format_exceptions:
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


def _make_processor_formatter(
    *,
    shared_processors: list[structlog.types.Processor],
    renderer: structlog.types.Processor,
) -> structlog.stdlib.ProcessorFormatter:
    """Build a `ProcessorFormatter` that renders records through `renderer`."""
    return structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
        pass_foreign_args=True,
        use_get_message=False,
    )


def configure_logging(
    log_level: str = "INFO",
    *,
    force_tty: bool | None = None,
    log_file: str | Path | None = None,
) -> Logger:
    """Configure structlog and stdlib logging for a Starlette process.

    ```python
    logger = configure_logging("DEBUG", force_tty=True)
    logger.info("Server starting")
    ```

    When `log_file` is given, logs are *also* written there as one JSON object
    per line, regardless of the console's TTY state. The dev loop uses this to
    keep the colorized terminal output while persisting a machine-parseable
    record an agent can read back when debugging a reported bug (see
    `docs/development.md`). The file is opened in append mode, so a process
    reload keeps the running session's history; `just dev` truncates it once at
    launch. The directory is created if missing.
    """
    is_tty = sys.stdout.isatty() if force_tty is None else force_tty
    console_renderer: structlog.types.Processor
    if is_tty:
        console_renderer = structlog.dev.ConsoleRenderer(colors=True)
    else:
        console_renderer = structlog.processors.JSONRenderer()

    # With a file sink the exceptions must be pre-rendered into the `exception`
    # string so the JSON file carries the traceback; a non-TTY console already
    # needs this too. The only cost is that a TTY console then prints that
    # pre-rendered string instead of `ConsoleRenderer`'s colorized traceback —
    # an acceptable trade in dev, where the file is the traceback of record.
    format_exceptions = (not is_tty) or log_file is not None
    processors = _shared_processors(format_exceptions=format_exceptions)
    structlog.configure(
        processors=[
            *processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )

    handlers: list[logging.Handler] = []
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(
        _make_processor_formatter(
            shared_processors=processors, renderer=console_renderer
        )
    )
    handlers.append(console_handler)

    if log_file is not None:
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_path, mode="a", encoding="utf-8")
        file_handler.setFormatter(
            _make_processor_formatter(
                shared_processors=processors,
                renderer=structlog.processors.JSONRenderer(),
            )
        )
        handlers.append(file_handler)

    root_logger = logging.getLogger()
    _clear_handlers(root_logger)
    for handler in handlers:
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
