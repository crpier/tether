"""Structured logging behavior tests for Starlette servers."""

import json
import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from io import StringIO

import structlog
from snektest import (
    assert_eq,
    assert_false,
    assert_in,
    assert_is,
    assert_is_none,
    assert_is_not_none,
    assert_isinstance,
    assert_raises,
    assert_true,
    test,
)
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from starlette.testclient import TestClient

from tether.logging import (
    QUIET_LOGGERS,
    SILENCED_LOGGERS,
    ContextLoggerMiddleware,
    _capture_bound_context,
    _process_positional_args,
    _reorder_fields,
    configure_logging,
    get_bound_request_logger,
    get_request_logger,
)


class CapturedStdout(StringIO):
    """Writable stdout test double with controllable TTY detection."""

    def __init__(self, *, is_tty: bool) -> None:
        super().__init__()
        self.is_tty: bool = is_tty

    def isatty(self) -> bool:
        """Return the configured terminal-detection result."""
        return self.is_tty


@contextmanager
def captured_logging(*, is_tty: bool) -> Iterator[CapturedStdout]:
    """Isolate global logging state while exercising configuration."""
    original_stdout = sys.stdout
    root_logger = logging.getLogger()
    original_root_level = root_logger.level
    original_root_handlers = list(root_logger.handlers)
    quiet_logger_states = {
        name: (
            logging.getLogger(name).level,
            logging.getLogger(name).propagate,
            logging.getLogger(name).disabled,
            list(logging.getLogger(name).handlers),
        )
        for name in (*QUIET_LOGGERS, *SILENCED_LOGGERS)
    }
    stream = CapturedStdout(is_tty=is_tty)
    sys.stdout = stream
    try:
        yield stream
    finally:
        sys.stdout = original_stdout
        for handler in list(root_logger.handlers):
            root_logger.removeHandler(handler)
        for handler in original_root_handlers:
            root_logger.addHandler(handler)
        root_logger.setLevel(original_root_level)
        for name, (level, propagate, disabled, handlers) in quiet_logger_states.items():
            logger = logging.getLogger(name)
            for handler in list(logger.handlers):
                logger.removeHandler(handler)
            for handler in handlers:
                logger.addHandler(handler)
            logger.setLevel(level)
            logger.propagate = propagate
            logger.disabled = disabled
        structlog.reset_defaults()


def first_json_log(stream: CapturedStdout) -> dict[str, object]:
    """Parse the first structured log line from a captured stream."""
    return json.loads(stream.getvalue().splitlines()[0])


@test()
def process_positional_args_merges_dict_arguments() -> None:
    """Dictionary positional args become structured event fields."""
    event = _process_positional_args(
        None,
        "info",
        {"event": "Saved", "positional_args": ({"memory_id": "abc"},)},
    )

    assert_eq(event, {"event": "Saved", "memory_id": "abc"})


@test()
def process_positional_args_interpolates_string_arguments() -> None:
    """Printf-style positional args are rendered into the event message."""
    event = _process_positional_args(
        None,
        "info",
        {"event": "Saved %s", "positional_args": ("memory",)},
    )

    assert_eq(event, {"event": "Saved memory"})


@test()
def process_positional_args_falls_back_to_space_joining() -> None:
    """Malformed printf args are preserved by appending them to the message."""
    event = _process_positional_args(
        None,
        "info",
        {"event": "Saved %s %s", "positional_args": ("memory",)},
    )

    assert_eq(event, {"event": "Saved %s %s memory"})


@test()
def capture_bound_context_records_contextvar_keys() -> None:
    """Bound context keys are captured before contextvars merge into the event."""
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(request_id="req-1", path="/memories")
    try:
        event = _capture_bound_context(None, "info", {"event": "Saved"})
    finally:
        structlog.contextvars.clear_contextvars()

    assert_eq(event["_bound_context_keys"], {"request_id", "path"})


@test()
def reorder_fields_places_bound_context_after_sorted_keywords() -> None:
    """Common fields lead, ordinary keywords sort, bound context sorts last."""
    event = _reorder_fields(
        None,
        "info",
        {
            "zebra": 1,
            "request_id": "req-1",
            "event": "Saved",
            "logger": "tether",
            "alpha": 2,
            "level": "info",
            "timestamp": "2026-06-26 12:00:00",
            "path": "/memories",
            "_bound_context_keys": {"request_id", "path"},
        },
    )

    assert_eq(
        list(event),
        [
            "timestamp",
            "level",
            "logger",
            "event",
            "alpha",
            "zebra",
            "path",
            "request_id",
        ],
    )
    assert_false("_bound_context_keys" in event)


@test()
def configure_logging_emits_json_when_stdout_is_not_a_tty() -> None:
    """Non-TTY logging renders one JSON object per line."""
    with captured_logging(is_tty=False) as stream:
        configure_logging(force_tty=False)
        logging.getLogger("third.party").info("Saved %s", "memory")

    logged = first_json_log(stream)
    assert_eq(logged["event"], "Saved memory")
    assert_eq(logged["level"], "info")
    assert_eq(logged["logger"], "third.party")
    assert_in("timestamp", logged)


@test()
def configure_logging_returns_the_root_structlog_logger() -> None:
    """The returned logger writes through the stdlib root logger."""
    with captured_logging(is_tty=False) as stream:
        logger = configure_logging(force_tty=False)
        logger.info("Saved")

    logged = first_json_log(stream)
    assert_eq(logged["logger"], "root")
    assert_eq(logged["event"], "Saved")


@test()
def configure_logging_emits_console_output_when_stdout_is_a_tty() -> None:
    """TTY logging renders human-readable console output instead of JSON."""
    with captured_logging(is_tty=True) as stream:
        configure_logging(force_tty=True)
        structlog.get_logger("tether.test").info("Saved", memory_id="abc")

    assert_in("Saved", stream.getvalue())
    with assert_raises(json.JSONDecodeError):
        json.loads(stream.getvalue())


@test()
def configure_logging_replaces_root_handlers_with_stdout_handler() -> None:
    """Root stdlib logging is routed through one stdout stream handler."""
    with captured_logging(is_tty=False):
        configure_logging(log_level="DEBUG", force_tty=False)
        root_logger = logging.getLogger()

        assert_eq(root_logger.level, logging.DEBUG)
        assert_eq(len(root_logger.handlers), 1)
        handler = root_logger.handlers[0]
        assert_isinstance(handler, logging.StreamHandler)
        assert isinstance(handler, logging.StreamHandler)
        assert_is(handler.stream, sys.stdout)


@test()
def configure_logging_quiets_noisy_loggers() -> None:
    """Noisy non-uvicorn server loggers share root formatting at warnings only."""
    logging.getLogger("watchfiles.main").addHandler(logging.StreamHandler(StringIO()))

    with captured_logging(is_tty=False):
        configure_logging(force_tty=False)

        for name in QUIET_LOGGERS:
            logger = logging.getLogger(name)
            assert_eq(logger.level, logging.WARNING)
            assert_true(logger.propagate)
            assert_false(logger.disabled)
            assert_eq(logger.handlers, [])


@test()
def configure_logging_silences_uvicorn_loggers() -> None:
    """Uvicorn must not emit its own lifecycle or access logs."""
    logging.getLogger("uvicorn").addHandler(logging.StreamHandler(StringIO()))
    logging.getLogger("uvicorn.error").addHandler(logging.StreamHandler(StringIO()))
    logging.getLogger("uvicorn.access").addHandler(logging.StreamHandler(StringIO()))

    with captured_logging(is_tty=False) as stream:
        configure_logging(force_tty=False)

        for name in SILENCED_LOGGERS:
            logger = logging.getLogger(name)
            assert_eq(logger.handlers, [])
            assert_false(logger.propagate)
            assert_true(logger.disabled)

        logging.getLogger("uvicorn.error").warning("Started server process")
        logging.getLogger("uvicorn.access").info("GET / HTTP/1.1")

    assert_eq(stream.getvalue(), "")


@test()
def context_logger_middleware_uses_application_logger_from_lifespan() -> None:
    """Middleware can bind requests from `app.state.logger`."""

    async def read(_request: Request) -> Response:
        return JSONResponse({"ok": True})

    with captured_logging(is_tty=False) as stream:
        app = Starlette(routes=[Route("/ok", read)])
        app.state.logger = configure_logging(force_tty=False)
        app.add_middleware(ContextLoggerMiddleware)
        with TestClient(app) as client:
            response = client.get("/ok")

    logged = first_json_log(stream)
    assert_eq(response.status_code, 200)
    assert_eq(logged["event"], "Request completed")


@test()
def context_logger_middleware_logs_completed_requests() -> None:
    """Successful requests get a request logger and completion log."""

    async def read(request: Request) -> Response:
        request_logger = get_request_logger(request)
        assert_is_not_none(request_logger)
        assert_is(get_bound_request_logger(), request_logger)
        return JSONResponse({"ok": True})

    with captured_logging(is_tty=False) as stream:
        base_logger = configure_logging(force_tty=False)
        app = Starlette(routes=[Route("/ok", read)])
        app.add_middleware(ContextLoggerMiddleware, base_logger=base_logger)
        with TestClient(app) as client:
            response = client.get(
                "/ok",
                headers={"user-agent": "snektest"},
            )

    logged = first_json_log(stream)
    assert_eq(response.status_code, 200)
    assert_eq(logged["event"], "Request completed")
    assert_eq(logged["method"], "GET")
    assert_eq(logged["path"], "/ok")
    assert_eq(logged["status_code"], 200)
    assert_in("duration_ms", logged)
    assert_in("request_id", logged)
    assert_eq(logged["user_agent"], "snektest")


@test()
def context_logger_middleware_logs_and_reraises_failures() -> None:
    """Failed requests are logged with exception context before bubbling up."""

    async def fail(_request: Request) -> Response:
        error_message = "boom"
        raise RuntimeError(error_message)

    with captured_logging(is_tty=False) as stream:
        base_logger = configure_logging(force_tty=False)
        app = Starlette(routes=[Route("/fail", fail)])
        app.add_middleware(ContextLoggerMiddleware, base_logger=base_logger)
        with TestClient(app) as client, assert_raises(RuntimeError):
            client.get("/fail")

    logged = first_json_log(stream)
    assert_eq(logged["event"], "Request failed")
    assert_eq(logged["method"], "GET")
    assert_eq(logged["path"], "/fail")
    assert_in("duration_ms", logged)
    assert_in("exception", logged)


@test()
def bound_request_logger_is_cleared_after_request() -> None:
    """Request logger context does not leak beyond middleware dispatch."""

    async def read(_request: Request) -> Response:
        assert_is_not_none(get_bound_request_logger())
        return JSONResponse({"ok": True})

    with captured_logging(is_tty=False):
        base_logger = configure_logging(force_tty=False)
        app = Starlette(routes=[Route("/ok", read)])
        app.add_middleware(ContextLoggerMiddleware, base_logger=base_logger)
        with TestClient(app) as client:
            response = client.get("/ok")

    assert_eq(response.status_code, 200)
    assert_is_none(get_bound_request_logger())


@test()
def get_request_logger_requires_middleware() -> None:
    """Requests without middleware state fail loudly."""

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": b"", "more_body": False}

    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/missing",
            "query_string": b"",
            "headers": [],
        },
        receive,
    )

    with assert_raises(RuntimeError):
        get_request_logger(request)
