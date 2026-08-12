"""Structured logging for Starlette and FastAPI applications.

Required packages: `starlette` and `structlog`. Install `opentelemetry-api` only
when trace/span correlation is wanted.

After copying this file as `structured_logging.py`, configure logging before the
application starts handling requests. The middleware requires
`app.state.logger`:

```python
from fastapi import FastAPI

from structured_logging import ContextLoggerMiddleware, configure_logging

app = FastAPI()
app.state.logger = configure_logging()
app.add_middleware(ContextLoggerMiddleware)
```
"""

from __future__ import annotations

import logging
import sys
import time
from collections.abc import Callable, Collection, Mapping
from contextvars import ContextVar
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import structlog
from starlette.requests import HTTPConnection
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from structlog.typing import EventDict, WrappedLogger

type Logger = structlog.stdlib.BoundLogger

try:
    from opentelemetry.trace import get_current_span
except ModuleNotFoundError as error:
    if error.name not in {"opentelemetry", "opentelemetry.trace"}:
        raise
    _get_current_span: Callable[[], Any] | None = None
else:
    _get_current_span = get_current_span


QUIET_LOGGERS = (
    "watchfiles.main",
    "uvicorn",
    "uvicorn.error",
)
"""Common development-server loggers that emit warnings only by default.

The `uvicorn`/`uvicorn.error` pair remains enabled and propagates to the root
handler. This suppresses routine lifecycle chatter while retaining startup
failure tracebacks. Policies apply to each named logger and its descendants.
Applications can replace this collection through
`configure_logging(quiet_loggers=...)`.
"""

SILENCED_LOGGERS = ("uvicorn.access",)
"""Loggers disabled because the middleware emits structured access events."""
_REQUEST_LOGGER: ContextVar[Logger | None] = ContextVar(
    "request_logger",
    default=None,
)


class _NamespaceFilter(logging.Filter):
    """Apply quiet and silenced policies to logger namespaces at each sink."""

    def __init__(
        self,
        *,
        quiet_loggers: Collection[str],
        silenced_loggers: Collection[str],
    ) -> None:
        super().__init__()
        self.quiet_loggers: tuple[str, ...] = tuple(quiet_loggers)
        self.silenced_loggers: tuple[str, ...] = tuple(silenced_loggers)

    def filter(self, record: logging.LogRecord) -> bool:
        """Reject silenced records and quiet records below WARNING."""
        if any(
            record.name == namespace or record.name.startswith(f"{namespace}.")
            for namespace in self.silenced_loggers
        ):
            return False
        return record.levelno >= logging.WARNING or not any(
            record.name == namespace or record.name.startswith(f"{namespace}.")
            for namespace in self.quiet_loggers
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
        except Exception:
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
    """Attach active span ids so logs can be correlated with traces."""
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
        ],
    )
    return processors


def _configure_quiet_loggers(
    *,
    quiet_loggers: Collection[str],
    silenced_loggers: Collection[str],
) -> None:
    """Route quiet loggers through root and fully disable silenced loggers."""
    for logger_name in quiet_loggers:
        logger = logging.getLogger(logger_name)
        _clear_handlers(logger)
        logger.setLevel(logging.WARNING)
        logger.propagate = True
        logger.disabled = False

    for logger_name in silenced_loggers:
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
    quiet_loggers: Collection[str] = QUIET_LOGGERS,
    silenced_loggers: Collection[str] = SILENCED_LOGGERS,
) -> Logger:
    """Configure structlog and stdlib logging for a Starlette process.

    ```python
    logger = configure_logging("DEBUG", force_tty=True)
    logger.info("Server starting")
    ```

    This function takes ownership of process-wide logging: it removes and
    closes existing root handlers, installs its own handlers, and reconfigures
    structlog. Call it once during process startup, not from a reusable library.

    When `log_file` is given, logs are also appended there as one JSON object
    per line, regardless of the console's TTY state. Its parent directory is
    created when missing.
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
    # pre-rendered string instead of `ConsoleRenderer`'s colorized traceback.
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
    namespace_filter = _NamespaceFilter(
        quiet_loggers=quiet_loggers,
        silenced_loggers=silenced_loggers,
    )
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.addFilter(namespace_filter)
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
        file_handler.addFilter(namespace_filter)
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
    _configure_quiet_loggers(
        quiet_loggers=quiet_loggers,
        silenced_loggers=silenced_loggers,
    )
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


def get_request_logger(connection: HTTPConnection) -> Logger:
    """Return the logger installed on an HTTP or WebSocket connection.

    ```python
    async def endpoint(request):
        get_request_logger(request).info("Handling request")
    ```
    """
    logger = getattr(connection.state, "logger", None)
    if logger is None:
        error_message = "ContextLoggerMiddleware is not configured for this request."
        raise RuntimeError(error_message)
    return cast("Logger", logger)


class ContextLoggerMiddleware:
    """Bind context to logs for each Starlette HTTP or WebSocket connection.

    The middleware wraps the ASGI lifecycle directly, so HTTP completion timing
    includes streaming the response body and streaming failures are logged.
    Successful lifecycle events use INFO by default; `completion_log_level` can
    move them to another stdlib logging level. Client addresses and user-agent
    values are omitted by default. When enabled, `client_ip` is the ASGI peer
    address; forwarded headers are not interpreted:

    ```python
    app.state.logger = configure_logging()
    app.add_middleware(
        ContextLoggerMiddleware,
        include_client_ip=True,
        include_user_agent=True,
    )
    ```
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        completion_log_level: int = logging.INFO,
        include_client_ip: bool = False,
        include_user_agent: bool = False,
    ) -> None:
        self.app: ASGIApp = app
        self.completion_log_level: int = completion_log_level
        self.include_client_ip: bool = include_client_ip
        self.include_user_agent: bool = include_user_agent

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        """Log HTTP and WebSocket scopes while passing other scopes through."""
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return

        connection = HTTPConnection(scope)
        base_logger = cast("Logger", connection.app.state.logger)
        request_context = self._connection_context(connection, scope)
        status_code: int | None = None

        async def send_with_status(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
            await send(message)

        context_tokens = structlog.contextvars.bind_contextvars(**request_context)
        connection.state.logger = base_logger
        token = _REQUEST_LOGGER.set(base_logger)
        started_at = time.perf_counter()
        is_http = scope["type"] == "http"
        try:
            await self.app(scope, receive, send_with_status)
        except Exception:
            failure_context: dict[str, object] = {
                "duration_ms": round((time.perf_counter() - started_at) * 1000, 3)
            }
            if status_code is not None:
                failure_context["status_code"] = status_code
            base_logger.exception(
                "Request failed" if is_http else "WebSocket failed",
                **failure_context,
            )
            raise
        else:
            completion_context: dict[str, object] = {
                "duration_ms": round((time.perf_counter() - started_at) * 1000, 3)
            }
            if status_code is not None:
                completion_context["status_code"] = status_code
            base_logger.log(
                self.completion_log_level,
                "Request completed" if is_http else "WebSocket completed",
                **completion_context,
            )
        finally:
            _REQUEST_LOGGER.reset(token)
            structlog.contextvars.reset_contextvars(**context_tokens)

    def _connection_context(
        self,
        connection: HTTPConnection,
        scope: Scope,
    ) -> dict[str, object]:
        """Build protocol-specific context without collecting sensitive defaults."""
        request_context: dict[str, object] = {
            "path": connection.url.path,
            "request_id": str(uuid4()),
        }
        if scope["type"] == "http":
            request_context["method"] = scope["method"]
        else:
            request_context["connection_type"] = "websocket"
        if self.include_client_ip:
            request_context["client_ip"] = (
                connection.client.host if connection.client is not None else None
            )
        if self.include_user_agent:
            request_context["user_agent"] = connection.headers.get("user-agent")
        return request_context


__all__ = [
    "QUIET_LOGGERS",
    "SILENCED_LOGGERS",
    "ContextLoggerMiddleware",
    "Logger",
    "configure_logging",
    "get_bound_request_logger",
    "get_request_logger",
]
