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
from collections.abc import Callable, Collection
from contextvars import ContextVar
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import structlog
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from structlog.typing import EventDict, WrappedLogger

type Logger = structlog.stdlib.BoundLogger

try:
    from opentelemetry.trace import get_current_span
except ModuleNotFoundError:
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
failure tracebacks. Applications can replace this collection through
`configure_logging(quiet_loggers=...)`.
"""

SILENCED_LOGGERS = ("uvicorn.access",)
"""Uvicorn loggers that are fully disabled because uvicorn owns its formatting."""
_REQUEST_LOGGER: ContextVar[Logger | None] = ContextVar(
    "request_logger",
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


class ContextLoggerMiddleware:
    """Bind request metadata to logs for each Starlette HTTP request.

    The middleware wraps the ASGI send lifecycle directly, so completion timing
    includes streaming the response body and streaming failures are logged.
    `client_ip` is the ASGI peer address; forwarded headers are not trusted or
    interpreted. Disable sensitive fields when they are unnecessary:

    ```python
    app.state.logger = configure_logging()
    app.add_middleware(
        ContextLoggerMiddleware,
        include_client_ip=False,
        include_user_agent=False,
    )
    ```
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        include_client_ip: bool = True,
        include_user_agent: bool = True,
    ) -> None:
        self.app: ASGIApp = app
        self.include_client_ip: bool = include_client_ip
        self.include_user_agent: bool = include_user_agent

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        """Log one HTTP request while passing other ASGI scopes through."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        base_logger = cast("Logger", request.app.state.logger)
        request_context: dict[str, object] = {
            "request_id": str(uuid4()),
            "method": request.method,
            "path": request.url.path,
        }
        if self.include_client_ip:
            request_context["client_ip"] = (
                request.client.host if request.client is not None else None
            )
        if self.include_user_agent:
            request_context["user_agent"] = request.headers.get("user-agent")

        status_code: int | None = None

        async def send_with_status(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
            await send(message)

        context_tokens = structlog.contextvars.bind_contextvars(**request_context)
        request.state.logger = base_logger
        token = _REQUEST_LOGGER.set(base_logger)
        started_at = time.perf_counter()
        try:
            await self.app(scope, receive, send_with_status)
        except Exception:
            base_logger.exception(
                "Request failed",
                duration_ms=round((time.perf_counter() - started_at) * 1000, 3),
            )
            raise
        else:
            base_logger.debug(
                "Request completed",
                status_code=status_code,
                duration_ms=round((time.perf_counter() - started_at) * 1000, 3),
            )
        finally:
            _REQUEST_LOGGER.reset(token)
            structlog.contextvars.reset_contextvars(**context_tokens)


__all__ = [
    "QUIET_LOGGERS",
    "SILENCED_LOGGERS",
    "ContextLoggerMiddleware",
    "Logger",
    "configure_logging",
    "get_bound_request_logger",
    "get_request_logger",
]
