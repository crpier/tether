"""Host server configuration and process entrypoint tests."""

import json
import logging
import os
import subprocess
import sys
from collections.abc import Generator
from contextlib import contextmanager
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

import structlog
from snektest import assert_eq, assert_in, assert_true, test
from starlette.testclient import TestClient

from tether import server
from tether.logging import QUIET_LOGGERS, SILENCED_LOGGERS
from tether.server import HostSettings, create_app_from_environment, serve
from tether.telemetry import TelemetryExporter, TelemetrySettings


class CapturedStdout(StringIO):
    """Writable stdout test double with controllable TTY detection."""

    def isatty(self) -> bool:
        """Force non-TTY rendering in server integration tests."""
        return False


@contextmanager
def configured_environment(**updates: str) -> Generator[None]:
    """Temporarily set environment variables for settings loading."""
    previous = {name: os.environ.get(name) for name in updates}
    os.environ.update(updates)
    try:
        yield
    finally:
        for name, old_value in previous.items():
            if old_value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = old_value


@contextmanager
def captured_logging() -> Generator[CapturedStdout]:
    """Capture stdout and restore process-global logging state."""
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
    stream = CapturedStdout()
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


@test()
def host_settings_read_tether_environment_variables() -> None:
    """`TETHER_` variables configure the host process."""
    with (
        TemporaryDirectory() as directory,
        configured_environment(
            TETHER_DATABASE_PATH=f"{directory}/host.sqlite3",
            TETHER_HOST="127.0.0.2",
            TETHER_KB_ROOT=f"{directory}/kb",
            TETHER_LOGGING_LEVEL="DEBUG",
            TETHER_PORT="9001",
            TETHER_RELOAD="true",
            TETHER_TELEMETRY_ENVIRONMENT="test",
            TETHER_TELEMETRY_EXPORTER="none",
            TETHER_TELEMETRY_SERVICE_NAME="tether-test",
        ),
    ):
        settings = HostSettings()

    assert_eq(settings.database_path, Path(directory) / "host.sqlite3")
    assert_eq(settings.host, "127.0.0.2")
    assert_eq(settings.kb_root, Path(directory) / "kb")
    assert_eq(settings.logging_level, "DEBUG")
    assert_eq(settings.port, 9001)
    assert_true(settings.reload)
    assert_eq(settings.telemetry.environment, "test")
    assert_eq(settings.telemetry.exporter, TelemetryExporter.NONE)
    assert_eq(settings.telemetry.service_name, "tether-test")


@test()
def request_logs_include_trace_context() -> None:
    """Request logs include OpenTelemetry trace correlation fields."""
    with TemporaryDirectory() as directory, captured_logging() as stream:
        with TestClient(
            server.create_app(
                database_path=":memory:",
                kb_root=f"{directory}/kb",
                telemetry_settings=TelemetrySettings(
                    exporter=TelemetryExporter.NONE,
                    install_global_provider=False,
                ),
            )
        ) as client:
            response = client.get("/memories", params={"state": "loose"})

        logged = next(
            json.loads(line)
            for line in stream.getvalue().splitlines()
            if json.loads(line)["event"] == "Request completed"
        )

    assert_eq(response.status_code, 200)
    assert_in("trace_id", logged)
    assert_in("span_id", logged)


@test()
def environment_app_factory_installs_global_tracer_provider() -> None:
    """Environment startup installs OpenTelemetry for global API users."""
    with TemporaryDirectory() as directory:
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                """
from opentelemetry import trace
from starlette.testclient import TestClient

from tether.server import create_app_from_environment

with TestClient(create_app_from_environment()):
    with trace.get_tracer("probe").start_as_current_span("probe") as span:
        if not span.get_span_context().is_valid:
            raise SystemExit("global tracer provider did not create recording spans")
""",
            ],
            capture_output=True,
            check=False,
            cwd=Path(__file__).parents[1],
            env={
                **os.environ,
                "TETHER_DATABASE_PATH": f"{directory}/host.sqlite3",
                "TETHER_KB_ROOT": f"{directory}/kb",
            },
            text=True,
        )

    assert_eq(completed.returncode, 0)
    assert_eq(completed.stderr, "")


@test()
def serve_runs_uvicorn_against_the_environment_app_factory() -> None:
    """The CLI uses an import-string app factory so reload mode works."""
    calls: list[dict[str, object]] = []

    def fake_run(app: object, **kwargs: object) -> None:
        calls.append({"app": app, **kwargs})

    original_run = server.uvicorn.run
    server.uvicorn.run = fake_run
    try:
        with captured_logging():
            serve(
                HostSettings(
                    host="127.0.0.2",
                    port=9001,
                    reload=True,
                    logging_level="DEBUG",
                )
            )
    finally:
        server.uvicorn.run = original_run

    assert_eq(calls[0]["app"], "tether.server:create_app_from_environment")
    assert_eq(calls[0]["factory"], True)
    assert_eq(calls[0]["host"], "127.0.0.2")
    assert_eq(calls[0]["port"], 9001)
    assert_eq(calls[0]["reload"], True)
    assert_eq(calls[0]["log_config"], None)
    assert_eq(calls[0]["access_log"], False)


@test()
def environment_app_factory_wires_settings_and_request_logging() -> None:
    """The app factory reads env config and installs request logging."""
    with (
        TemporaryDirectory() as directory,
        captured_logging() as stream,
        configured_environment(
            TETHER_DATABASE_PATH=f"{directory}/configured.sqlite3",
            TETHER_KB_ROOT=f"{directory}/kb",
            TETHER_LOGGING_LEVEL="INFO",
        ),
    ):
        with TestClient(create_app_from_environment()) as client:
            response = client.get("/memories", params={"state": "loose"})

        log_events = [
            json.loads(line)["event"] for line in stream.getvalue().splitlines()
        ]
        database_exists = (Path(directory) / "configured.sqlite3").exists()

    assert_eq(response.status_code, 200)
    assert_true(database_exists)
    assert_in("Request completed", log_events)
