"""Structured logging behavior tests for Starlette servers."""

import json
import logging
import subprocess
import sys
import tempfile
from collections.abc import AsyncGenerator, Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from typing import TextIO, cast

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
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route, WebSocketRoute
from starlette.testclient import TestClient
from starlette.websockets import WebSocket

from tether.model_selection import AgentModelCatalog, ModelSelectionConfigError
from tether.structured_logging import (
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
def captured_logging(
    *,
    is_tty: bool,
    logger_names: tuple[str, ...] = (),
) -> Generator[CapturedStdout]:
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
        for name in (*QUIET_LOGGERS, *SILENCED_LOGGERS, *logger_names)
    }
    stream = CapturedStdout(is_tty=is_tty)
    sys.stdout = stream
    try:
        yield stream
    finally:
        sys.stdout = original_stdout
        for handler in list(root_logger.handlers):
            root_logger.removeHandler(handler)
            # configure_logging installs fresh handlers (a file sink owns an open
            # descriptor); close them so the temp log file is released cleanly.
            handler.close()
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


def _raise_boom() -> None:
    """Raise a `RuntimeError` so a caller can log the active exception."""
    error_message = "boom"
    raise RuntimeError(error_message)


def first_json_log(stream: CapturedStdout) -> dict[str, object]:
    """Parse the first structured log line from a captured stream."""
    return json.loads(stream.getvalue().splitlines()[0])


def json_log_for_event(stream: CapturedStdout, event: str) -> dict[str, object]:
    """Parse the first structured log line whose `event` matches `event`.

    Capturing at DEBUG surfaces foreign debug noise (e.g. asyncio's selector
    line) ahead of our own records, so callers that need a specific event must
    select it rather than assuming it is first.
    """
    return next(
        parsed
        for line in stream.getvalue().splitlines()
        if (parsed := json.loads(line))["event"] == event
    )


@test()
def structured_logging_imports_without_opentelemetry() -> None:
    """Tracing support remains optional for applications that do not use it."""
    import_script = """
import builtins

original_import = builtins.__import__
def import_without_opentelemetry(name, *args, **kwargs):
    if name.startswith("opentelemetry"):
        raise ModuleNotFoundError(name=name)
    return original_import(name, *args, **kwargs)

builtins.__import__ = import_without_opentelemetry
import tether.structured_logging
"""

    completed = subprocess.run(
        [sys.executable, "-c", import_script],
        capture_output=True,
        check=False,
        text=True,
    )

    assert_eq(completed.returncode, 0)
    assert_eq(completed.stderr, "")


@test()
def structured_logging_surfaces_broken_opentelemetry_installations() -> None:
    """Missing OpenTelemetry internals are not mistaken for an optional absence."""
    import_script = """
import builtins

original_import = builtins.__import__
def import_with_broken_opentelemetry(name, *args, **kwargs):
    if name == "opentelemetry.trace":
        raise ModuleNotFoundError(name="broken_dependency")
    return original_import(name, *args, **kwargs)

builtins.__import__ = import_with_broken_opentelemetry
import tether.structured_logging
"""

    completed = subprocess.run(
        [sys.executable, "-c", import_script],
        capture_output=True,
        check=False,
        text=True,
    )

    assert_eq(completed.returncode, 1)
    assert_in("broken_dependency", completed.stderr)


@test()
def configure_logging_interpolates_mapping_arguments_without_field_spoofing() -> None:
    """Mapping values remain message arguments, not structured event fields."""
    with captured_logging(is_tty=False) as stream:
        configure_logging(force_tty=False)
        logging.getLogger("third.party").info(
            "payload=%s",
            {"event": "forged", "level": "forged"},
        )

    logged = first_json_log(stream)
    assert_eq(logged["event"], "payload={'event': 'forged', 'level': 'forged'}")
    assert_eq(logged["level"], "info")


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
def configure_logging_emits_utc_iso_8601_timestamps() -> None:
    """Structured timestamps identify UTC unambiguously."""
    with captured_logging(is_tty=False) as stream:
        configure_logging(force_tty=False)
        structlog.get_logger("example").info("Saved")

    logged_at = datetime.fromisoformat(str(first_json_log(stream)["timestamp"]))
    assert_is(logged_at.tzinfo, UTC)


@test()
def configure_logging_mirrors_logs_to_a_json_file_while_console_stays_readable() -> (
    None
):
    """A configured `log_file` gets JSON even when the console renders for a TTY.

    The dev loop keeps the colorized console (TTY) *and* a machine-parseable file
    an agent can read back when debugging a reported bug.
    """
    with captured_logging(is_tty=True) as stream, tempfile.TemporaryDirectory() as tmp:
        log_file = Path(tmp) / "logs" / "host.log"
        configure_logging(force_tty=True, log_file=log_file)
        structlog.get_logger("tether.test").info("Saved", memory_id="abc")
        logging.getLogger().handlers[-1].flush()

        # Console: human-readable, not JSON.
        assert_in("Saved", stream.getvalue())
        with assert_raises(json.JSONDecodeError):
            json.loads(stream.getvalue())

        # File: one JSON object per line with the structured fields intact.
        line = log_file.read_text("utf-8").splitlines()[0]
        logged = json.loads(line)
        assert_eq(logged["event"], "Saved")
        assert_eq(logged["memory_id"], "abc")
        assert_eq(logged["level"], "info")
        assert_in("timestamp", logged)


@test()
def configure_logging_records_exception_tracebacks_in_the_log_file() -> None:
    """Exceptions logged to the file carry the rendered traceback for debugging."""
    with captured_logging(is_tty=True) as _stream, tempfile.TemporaryDirectory() as tmp:
        log_file = Path(tmp) / "host.log"
        configure_logging(force_tty=True, log_file=log_file)
        try:
            _raise_boom()
        except RuntimeError:
            logging.getLogger("tether.test").exception("Request failed")
        logging.getLogger().handlers[-1].flush()

        logged = json.loads(log_file.read_text("utf-8").splitlines()[0])
        assert_eq(logged["event"], "Request failed")
        assert_in("RuntimeError", str(logged["exception"]))
        assert_in("boom", str(logged["exception"]))


@test()
def configure_logging_without_log_file_keeps_a_single_console_handler() -> None:
    """No `log_file` leaves the root logger with only the stdout handler."""
    with captured_logging(is_tty=False):
        configure_logging(force_tty=False)

        assert_eq(len(logging.getLogger().handlers), 1)


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
        stream_handler = cast("logging.StreamHandler[TextIO]", handler)
        assert_is(stream_handler.stream, sys.stdout)


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
def configure_logging_accepts_custom_quiet_loggers() -> None:
    """Applications can choose which dependency loggers emit warnings only."""
    with captured_logging(
        is_tty=False,
        logger_names=("example.dependency",),
    ):
        configure_logging(
            force_tty=False,
            quiet_loggers=("example.dependency",),
        )

        dependency_logger = logging.getLogger("example.dependency")
        assert_eq(dependency_logger.level, logging.WARNING)
        assert_true(dependency_logger.propagate)
        assert_false(dependency_logger.disabled)


@test()
def configure_logging_quiets_explicitly_leveled_child_loggers() -> None:
    """Quiet namespaces filter children even when they set their own level."""
    with captured_logging(
        is_tty=False,
        logger_names=("example.dependency", "example.dependency.child"),
    ) as stream:
        configure_logging(
            force_tty=False,
            quiet_loggers=("example.dependency",),
        )
        child_logger = logging.getLogger("example.dependency.child")
        child_logger.setLevel(logging.DEBUG)
        child_logger.info("Dependency chatter")
        child_logger.error("Dependency failed")

    events = [json.loads(line)["event"] for line in stream.getvalue().splitlines()]
    assert_eq(events, ["Dependency failed"])


@test()
def configure_logging_accepts_custom_silenced_loggers() -> None:
    """Applications can choose which dependency loggers emit nothing."""
    with captured_logging(
        is_tty=False,
        logger_names=("example.noisy", "example.noisy.child"),
    ) as stream:
        configure_logging(
            force_tty=False,
            silenced_loggers=("example.noisy",),
        )

        noisy_logger = logging.getLogger("example.noisy")
        assert_false(noisy_logger.propagate)
        assert_true(noisy_logger.disabled)
        noisy_child_logger = logging.getLogger("example.noisy.child")
        noisy_child_logger.setLevel(logging.DEBUG)
        noisy_child_logger.critical("Dependency exploded")

    assert_eq(stream.getvalue(), "")


@test()
def configure_logging_silences_uvicorn_loggers() -> None:
    """Uvicorn must not emit its own routine lifecycle or access logs.

    Access logs are fully disabled; the `uvicorn`/`uvicorn.error` lifecycle
    loggers are quieted to WARNING (see the startup-failure test), so their
    routine INFO chatter is dropped at the level floor rather than disabled.
    """
    logging.getLogger("uvicorn").addHandler(logging.StreamHandler(StringIO()))
    logging.getLogger("uvicorn.access").addHandler(logging.StreamHandler(StringIO()))

    with captured_logging(is_tty=False) as stream:
        configure_logging(force_tty=False)

        for name in SILENCED_LOGGERS:
            logger = logging.getLogger(name)
            assert_eq(logger.handlers, [])
            assert_false(logger.propagate)
            assert_true(logger.disabled)

        logging.getLogger("uvicorn").info("Started server process")
        logging.getLogger("uvicorn.error").info("Application startup complete.")
        logging.getLogger("uvicorn.access").info("GET / HTTP/1.1")

    assert_eq(stream.getvalue(), "")


@test()
def configure_logging_surfaces_uvicorn_startup_failures() -> None:
    """Uvicorn error-level output (e.g. lifespan startup crashes) must be visible.

    `serve()` runs uvicorn with `log_config=None`, so uvicorn configures no
    logging of its own. If `uvicorn.error` were also fully silenced, a lifespan
    startup exception — which uvicorn logs at ERROR on that logger with
    `exc_info` — would vanish, leaving only an unexplained `restart:`/exit-3
    loop. It must instead render through the structlog root handler. Routine
    INFO lifecycle chatter on the same logger stays suppressed (WARNING floor).
    """
    with captured_logging(is_tty=False) as stream:
        configure_logging(force_tty=False)

        uvicorn_error = logging.getLogger("uvicorn.error")
        assert_true(uvicorn_error.propagate)
        assert_false(uvicorn_error.disabled)
        assert_eq(uvicorn_error.level, logging.WARNING)

        # Routine INFO lifecycle line: dropped at the WARNING floor.
        uvicorn_error.info("Application startup complete.")
        assert_eq(stream.getvalue(), "")

        # The startup-failure path uvicorn takes: ERROR + exc_info, with the
        # genuine misconfiguration exception the host lifespan raises.
        try:
            AgentModelCatalog(default_model="default", models=())
        except ModelSelectionConfigError:
            uvicorn_error.error("Exception in 'lifespan' protocol\n", exc_info=True)

    record = json_log_for_event(stream, "Exception in 'lifespan' protocol\n")
    assert_eq(record["level"], "error")
    assert_in("default model is not present in the allowlist", str(record["exception"]))


@test()
def context_logger_middleware_logs_completed_requests_at_default_info_level() -> None:
    """Default logging records successful requests without uvicorn access logs."""

    async def read(_request: Request) -> Response:
        return JSONResponse({"ok": True})

    with captured_logging(is_tty=False) as stream:
        app = Starlette(routes=[Route("/ok", read)])
        app.state.runtime = SimpleNamespace(logger=configure_logging(force_tty=False))
        app.add_middleware(ContextLoggerMiddleware)
        with TestClient(app) as client:
            response = client.get("/ok")

    logged = json_log_for_event(stream, "Request completed")
    assert_eq(response.status_code, 200)
    assert_eq(logged["level"], "info")


@test()
def context_logger_middleware_accepts_a_custom_completion_level() -> None:
    """Applications can move successful request logs below their level floor."""

    async def read(_request: Request) -> Response:
        return JSONResponse({"ok": True})

    with captured_logging(is_tty=False) as stream:
        app = Starlette(routes=[Route("/ok", read)])
        app.state.runtime = SimpleNamespace(logger=configure_logging(force_tty=False))
        app.add_middleware(
            ContextLoggerMiddleware,
            completion_log_level=logging.DEBUG,
        )
        with TestClient(app) as client:
            response = client.get("/ok")

    events = [json.loads(line)["event"] for line in stream.getvalue().splitlines()]
    assert_eq(response.status_code, 200)
    assert_false("Request completed" in events)


@test()
def context_logger_middleware_uses_application_logger_from_lifespan() -> None:
    """Middleware binds requests from the canonical application runtime."""

    async def read(_request: Request) -> Response:
        return JSONResponse({"ok": True})

    with captured_logging(is_tty=False) as stream:
        app = Starlette(routes=[Route("/ok", read)])
        app.state.runtime = SimpleNamespace(
            logger=configure_logging("DEBUG", force_tty=False)
        )
        app.add_middleware(ContextLoggerMiddleware)
        with TestClient(app) as client:
            response = client.get("/ok")

    logged = json_log_for_event(stream, "Request completed")
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
        app = Starlette(routes=[Route("/ok", read)])
        app.state.runtime = SimpleNamespace(
            logger=configure_logging("DEBUG", force_tty=False)
        )
        app.add_middleware(
            ContextLoggerMiddleware,
            include_client_ip=True,
            include_user_agent=True,
        )
        with TestClient(app) as client:
            response = client.get(
                "/ok",
                headers={"user-agent": "snektest"},
            )

    logged = json_log_for_event(stream, "Request completed")
    assert_eq(response.status_code, 200)
    assert_eq(logged["event"], "Request completed")
    assert_eq(logged["method"], "GET")
    assert_eq(logged["path"], "/ok")
    assert_eq(logged["status_code"], 200)
    assert_in("duration_ms", logged)
    assert_in("request_id", logged)
    assert_eq(logged["user_agent"], "snektest")


@test()
def context_logger_middleware_binds_context_for_websockets() -> None:
    """WebSocket handlers receive connection-scoped logging context."""

    async def websocket_endpoint(websocket: WebSocket) -> None:
        await websocket.accept()
        assert_is(get_request_logger(websocket), get_bound_request_logger())
        get_request_logger(websocket).info("WebSocket message")
        await websocket.close()

    with captured_logging(is_tty=False) as stream:
        app = Starlette(routes=[WebSocketRoute("/ws", websocket_endpoint)])
        app.state.runtime = SimpleNamespace(logger=configure_logging(force_tty=False))
        app.add_middleware(ContextLoggerMiddleware)
        with TestClient(app) as client, client.websocket_connect("/ws"):
            pass

    logged = json_log_for_event(stream, "WebSocket message")
    assert_eq(logged["connection_type"], "websocket")
    assert_eq(logged["path"], "/ws")
    assert_in("request_id", logged)
    assert_is_none(get_bound_request_logger())


@test()
def context_logger_middleware_logs_streaming_failures() -> None:
    """Exceptions raised while sending a response are logged as failures."""

    async def broken_body() -> AsyncGenerator[bytes]:
        yield b"partial"
        raise RuntimeError("stream failed")

    async def read(_request: Request) -> Response:
        return StreamingResponse(broken_body())

    with captured_logging(is_tty=False) as stream:
        app = Starlette(routes=[Route("/stream", read)])
        app.state.runtime = SimpleNamespace(
            logger=configure_logging("DEBUG", force_tty=False)
        )
        app.add_middleware(ContextLoggerMiddleware)
        with TestClient(app) as client, assert_raises(RuntimeError):
            client.get("/stream")

    logs = [json.loads(line) for line in stream.getvalue().splitlines()]
    failed_request = next(log for log in logs if log["event"] == "Request failed")
    assert_eq(failed_request["status_code"], 200)
    assert_false(any(log["event"] == "Request completed" for log in logs))


@test()
def context_logger_middleware_logs_completion_after_streaming_finishes() -> None:
    """Request duration covers delivery of the complete streaming response."""

    async def body() -> AsyncGenerator[bytes]:
        structlog.get_logger("example").info("Stream yielded")
        yield b"done"

    async def read(_request: Request) -> Response:
        return StreamingResponse(body())

    with captured_logging(is_tty=False) as stream:
        app = Starlette(routes=[Route("/stream", read)])
        app.state.runtime = SimpleNamespace(
            logger=configure_logging("DEBUG", force_tty=False)
        )
        app.add_middleware(ContextLoggerMiddleware)
        with TestClient(app) as client:
            response = client.get("/stream")

    events = [json.loads(line)["event"] for line in stream.getvalue().splitlines()]
    assert_eq(response.content, b"done")
    assert_true(events.index("Stream yielded") < events.index("Request completed"))


@test()
def context_logger_middleware_places_request_context_after_event_fields() -> None:
    """Request metadata follows call-site details in structured output."""

    async def read(_request: Request) -> Response:
        return JSONResponse({"ok": True})

    with captured_logging(is_tty=False) as stream:
        app = Starlette(routes=[Route("/ok", read)])
        app.state.runtime = SimpleNamespace(
            logger=configure_logging("DEBUG", force_tty=False)
        )
        app.add_middleware(
            ContextLoggerMiddleware,
            include_client_ip=True,
            include_user_agent=True,
        )
        with TestClient(app) as client:
            client.get("/ok")

    logged = json_log_for_event(stream, "Request completed")
    assert_eq(
        list(logged),
        [
            "timestamp",
            "level",
            "logger",
            "event",
            "duration_ms",
            "status_code",
            "client_ip",
            "method",
            "path",
            "request_id",
            "user_agent",
        ],
    )


@test()
def context_logger_middleware_omits_sensitive_request_fields_by_default() -> None:
    """Client addresses and user-agent values require explicit opt-in."""

    async def read(_request: Request) -> Response:
        return JSONResponse({"ok": True})

    with captured_logging(is_tty=False) as stream:
        app = Starlette(routes=[Route("/ok", read)])
        app.state.runtime = SimpleNamespace(
            logger=configure_logging("DEBUG", force_tty=False)
        )
        app.add_middleware(ContextLoggerMiddleware)
        with TestClient(app) as client:
            response = client.get("/ok", headers={"user-agent": "snektest"})

    logged = json_log_for_event(stream, "Request completed")
    assert_eq(response.status_code, 200)
    assert_false("client_ip" in logged)
    assert_false("user_agent" in logged)


@test()
def context_logger_middleware_logs_and_reraises_failures() -> None:
    """Failed requests are logged with exception context before bubbling up."""

    async def fail(_request: Request) -> Response:
        error_message = "boom"
        raise RuntimeError(error_message)

    with captured_logging(is_tty=False) as stream:
        app = Starlette(routes=[Route("/fail", fail)])
        app.state.runtime = SimpleNamespace(logger=configure_logging(force_tty=False))
        app.add_middleware(ContextLoggerMiddleware)
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
        app = Starlette(routes=[Route("/ok", read)])
        app.state.runtime = SimpleNamespace(logger=configure_logging(force_tty=False))
        app.add_middleware(ContextLoggerMiddleware)
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
