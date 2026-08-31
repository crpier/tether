"""Connection-scoped logger access and ASGI lifecycle logging."""

from __future__ import annotations

import logging
import time
from contextvars import ContextVar
from typing import Protocol, cast
from uuid import uuid4

import structlog
from starlette.requests import HTTPConnection
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from tether.logging_config import Logger


class _LoggingRuntime(Protocol):
    """Logger available while the host serves requests."""

    logger: Logger


def _runtime(connection: HTTPConnection) -> _LoggingRuntime:
    """Read logging from the canonical host runtime."""
    return cast("_LoggingRuntime", connection.app.state.runtime)


class RequestLoggingNotConfiguredError(RuntimeError):
    """Request-scoped logging was accessed outside the owning middleware."""


_REQUEST_LOGGER: ContextVar[Logger | None] = ContextVar(
    "request_logger",
    default=None,
)


def get_bound_request_logger() -> Logger | None:
    """Return the logger bound to the active request, if any."""
    return _REQUEST_LOGGER.get()


def get_request_logger(connection: HTTPConnection) -> Logger:
    """Return the logger installed on an HTTP or WebSocket connection."""
    logger = getattr(connection.state, "logger", None)
    if logger is None:
        message = "ContextLoggerMiddleware is not configured for this request."
        raise RequestLoggingNotConfiguredError(message)
    return cast("Logger", logger)


class ContextLoggerMiddleware:
    """Bind request context and log completion or failure over the full ASGI flow."""

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
        """Observe HTTP and WebSocket scopes while passing other scopes through."""
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return

        connection = HTTPConnection(scope)
        base_logger = _runtime(connection).logger
        status_code: int | None = None

        async def send_with_status(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                raw_status = message.get("status")
                status_code = raw_status if isinstance(raw_status, int) else None
            await send(message)

        context_tokens = structlog.contextvars.bind_contextvars(
            **self._connection_context(connection, scope)
        )
        connection.state.logger = base_logger
        request_logger_token = _REQUEST_LOGGER.set(base_logger)
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
            _REQUEST_LOGGER.reset(request_logger_token)
            structlog.contextvars.reset_contextvars(**context_tokens)

    def _connection_context(
        self,
        connection: HTTPConnection,
        scope: Scope,
    ) -> dict[str, object]:
        """Build protocol context without collecting sensitive values by default."""
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
