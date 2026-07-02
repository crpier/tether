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
from snektest import assert_eq, assert_false, assert_in, assert_true, test
from starlette.applications import Starlette
from starlette.testclient import TestClient

from tether import server
from tether.logging import QUIET_LOGGERS, SILENCED_LOGGERS
from tether.server import (
    AppConfig,
    HostSettings,
    _build_supadata_provider,
    _compose_transcript_provider,
    create_app_from_environment,
    serve,
)
from tether.telemetry import TelemetryExporter, TelemetrySettings
from tether.transcript_supadata import SupadataTranscriptProvider
from tether.youtube import FallbackTranscriptProvider, NullTranscriptProvider


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
            TETHER_APP_PASSWORD="configured-password",
            TETHER_DATABASE_PATH=f"{directory}/host.sqlite3",
            TETHER_HOST="127.0.0.2",
            TETHER_KB_ROOT=f"{directory}/kb",
            TETHER_LOGGING_LEVEL="DEBUG",
            TETHER_DEFAULT_MODEL="cheap",
            TETHER_MODEL_ALLOWLIST=json.dumps(
                [
                    {
                        "display_name": "Cheap Faux",
                        "id": "cheap",
                        "model_id": "tether-chat-cheap-faux",
                        "provider": "faux",
                    }
                ]
            ),
            TETHER_PORT="9001",
            TETHER_RELOAD="true",
            TETHER_SECURE_COOKIES="true",
            TETHER_SESSION_SECRET="configured-session-secret",
            TETHER_TELEMETRY_ENVIRONMENT="test",
            TETHER_TELEMETRY_EXPORTER="none",
            TETHER_TELEMETRY_SERVICE_NAME="tether-test",
            TETHER_TOOL_SECRET="configured-tool-secret",
            TETHER_TRANSCRIPT_SYNC_ENABLED="false",
            TETHER_WEB_DIST=f"{directory}/dist",
            TETHER_YOUTUBE_SYNC_ENABLED="false",
        ),
    ):
        settings = HostSettings()

    assert_eq(settings.app_password, "configured-password")
    assert_true(settings.secure_cookies)
    assert_eq(settings.web_dist, Path(directory) / "dist")
    assert_eq(settings.database_path, Path(directory) / "host.sqlite3")
    assert_eq(settings.host, "127.0.0.2")
    assert_eq(settings.kb_root, Path(directory) / "kb")
    assert_eq(settings.logging_level, "DEBUG")
    assert_eq(settings.default_model, "cheap")
    assert_eq(settings.model_allowlist[0].id, "cheap")
    assert_eq(settings.model_allowlist[0].model_id, "tether-chat-cheap-faux")
    assert_eq(settings.port, 9001)
    assert_true(settings.reload)
    assert_eq(settings.session_secret, "configured-session-secret")
    assert_eq(settings.telemetry.environment, "test")
    assert_eq(settings.telemetry.exporter, TelemetryExporter.NONE)
    assert_eq(settings.telemetry.service_name, "tether-test")
    assert_eq(settings.tool_secret, "configured-tool-secret")
    assert_false(settings.youtube_sync_enabled)
    assert_false(settings.transcript_sync_enabled)


@test()
def sync_enabled_defaults_to_true() -> None:
    """Ingestion syncs are on unless a `TETHER_*_SYNC_ENABLED` flag disables them."""
    settings = HostSettings(
        app_password="test-app-password", session_secret="test-session-secret"
    )
    assert_true(settings.youtube_sync_enabled)
    assert_true(settings.transcript_sync_enabled)


@test()
def environment_app_factory_propagates_sync_flags() -> None:
    """`create_app_from_environment` wires the sync-enabled env flags into AppConfig.

    Regression guard: the flags existed on `AppConfig` but were never read from the
    environment, so `TETHER_*_SYNC_ENABLED=false` was silently ignored and the
    ingestion syncs always ran at boot.
    """
    captured: list[AppConfig] = []

    def fake_create_app(*, config: AppConfig, **_: object) -> Starlette:
        captured.append(config)
        return Starlette()

    original_create_app = server.create_app
    server.create_app = fake_create_app
    try:
        with configured_environment(
            TETHER_APP_PASSWORD="test-app-password",
            TETHER_SESSION_SECRET="test-session-secret",
            TETHER_TRANSCRIPT_SYNC_ENABLED="false",
            TETHER_YOUTUBE_SYNC_ENABLED="false",
        ):
            _ = create_app_from_environment()
    finally:
        server.create_app = original_create_app

    assert_eq(len(captured), 1)
    assert_false(captured[0].youtube_sync_enabled)
    assert_false(captured[0].transcript_sync_enabled)


@test()
def environment_app_factory_requires_app_password_and_session_secret() -> None:
    """The host refuses to start without required auth secrets."""
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "from tether.server import create_app_from_environment; create_app_from_environment()",
        ],
        capture_output=True,
        check=False,
        cwd=Path(__file__).parents[1],
        env={
            name: value
            for name, value in os.environ.items()
            if name not in {"TETHER_APP_PASSWORD", "TETHER_SESSION_SECRET"}
        },
        text=True,
    )

    assert_true(completed.returncode != 0)
    assert_in("app_password", completed.stderr)
    assert_in("session_secret", completed.stderr)


@test()
def request_logs_include_trace_context() -> None:
    """Request logs include OpenTelemetry trace correlation fields."""
    with TemporaryDirectory() as directory, captured_logging() as stream:
        with TestClient(
            server.create_app(
                config=AppConfig(
                    app_password="test-app-password",
                    database_path=":memory:",
                    kb_root=f"{directory}/kb",
                    logging_level="DEBUG",
                    session_secret="test-session-secret",
                ),
                telemetry_settings=TelemetrySettings(
                    exporter=TelemetryExporter.NONE,
                    install_global_provider=False,
                ),
            )
        ) as client:
            login_response = client.post(
                "/api/auth/login", json={"password": "test-app-password"}
            )
            assert_eq(login_response.status_code, 204)
            response = client.get("/api/memories", params={"state": "loose"})

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
                "TETHER_APP_PASSWORD": "test-app-password",
                "TETHER_DATABASE_PATH": f"{directory}/host.sqlite3",
                "TETHER_KB_ROOT": f"{directory}/kb",
                "TETHER_SESSION_SECRET": "test-session-secret",
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
                    app_password="test-app-password",
                    host="127.0.0.2",
                    port=9001,
                    reload=True,
                    logging_level="DEBUG",
                    session_secret="test-session-secret",
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
def the_host_serves_the_built_spa_without_shadowing_the_api() -> None:
    """With a built SPA configured, the host serves index.html and its assets,
    falls back to index.html for unknown client routes, and still routes `/api`."""
    with TemporaryDirectory() as directory:
        dist = Path(directory) / "dist"
        (dist / "assets").mkdir(parents=True)
        _ = (dist / "index.html").write_text("<!doctype html><title>Tether</title>")
        _ = (dist / "assets" / "app.js").write_text("console.log('tether')")
        with TestClient(
            server.create_app(
                config=AppConfig(
                    app_password="test-app-password",
                    database_path=":memory:",
                    kb_root=f"{directory}/kb",
                    session_secret="test-session-secret",
                    web_dist=dist,
                ),
                telemetry_settings=TelemetrySettings(
                    exporter=TelemetryExporter.NONE,
                    install_global_provider=False,
                ),
            )
        ) as client:
            root = client.get("/")
            asset = client.get("/assets/app.js")
            client_route = client.get("/some/spa/route")
            login = client.post(
                "/api/auth/login", json={"password": "test-app-password"}
            )
            api = client.get("/api/memories", params={"state": "loose"})

    assert_eq(root.status_code, 200)
    assert_in("Tether", root.text)
    assert_eq(asset.status_code, 200)
    assert_in("tether", asset.text)
    # An unknown non-API path falls back to the SPA shell for client-side routing.
    assert_eq(client_route.status_code, 200)
    assert_in("Tether", client_route.text)
    # The API is mounted ahead of the SPA catch-all and still responds.
    assert_eq(login.status_code, 204)
    assert_eq(api.status_code, 200)


@test()
def the_host_serves_no_spa_when_web_dist_is_unset() -> None:
    """Without a configured SPA build the root path is unhandled (dev/test default)."""
    with (
        TemporaryDirectory() as directory,
        TestClient(
            server.create_app(
                config=AppConfig(
                    app_password="test-app-password",
                    database_path=":memory:",
                    kb_root=f"{directory}/kb",
                    session_secret="test-session-secret",
                ),
                telemetry_settings=TelemetrySettings(
                    exporter=TelemetryExporter.NONE,
                    install_global_provider=False,
                ),
            )
        ) as client,
    ):
        root = client.get("/")

    assert_eq(root.status_code, 404)


@test()
def environment_app_factory_wires_settings_and_request_logging() -> None:
    """The app factory reads env config and installs request logging."""
    with (
        TemporaryDirectory() as directory,
        captured_logging() as stream,
        configured_environment(
            TETHER_APP_PASSWORD="test-app-password",
            TETHER_DATABASE_PATH=f"{directory}/configured.sqlite3",
            TETHER_KB_ROOT=f"{directory}/kb",
            TETHER_LOGGING_LEVEL="DEBUG",
            TETHER_SESSION_SECRET="test-session-secret",
        ),
    ):
        with TestClient(create_app_from_environment()) as client:
            login_response = client.post(
                "/api/auth/login", json={"password": "test-app-password"}
            )
            assert_eq(login_response.status_code, 204)
            response = client.get("/api/memories", params={"state": "loose"})

        log_events = [
            json.loads(line)["event"] for line in stream.getvalue().splitlines()
        ]
        database_exists = (Path(directory) / "configured.sqlite3").exists()

    assert_eq(response.status_code, 200)
    assert_true(database_exists)
    assert_in("Request completed", log_events)


def _supadata_settings(*, enabled: bool, api_key: str) -> HostSettings:
    """HostSettings with the always-required secrets plus the Supadata gating."""
    return HostSettings(
        app_password="test-app-password",
        session_secret="test-session-secret",
        supadata_enabled=enabled,
        supadata_api_key=api_key,
    )


@test()
def supadata_is_omitted_when_the_flag_is_off() -> None:
    """A configured key with the flag off keeps Supadata out of the chain (no spend)."""
    provider = _build_supadata_provider(
        _supadata_settings(enabled=False, api_key="sk-secret")
    )
    assert_true(provider is None)


@test()
def supadata_is_omitted_when_the_key_is_absent() -> None:
    """The flag on but no key is still a no-op — paid transcription needs credentials."""
    provider = _build_supadata_provider(_supadata_settings(enabled=True, api_key=""))
    assert_true(provider is None)


@test()
def supadata_is_built_when_key_and_flag_are_both_set() -> None:
    """Key + flag together build the Supadata provider for the fallback chain."""
    provider = _build_supadata_provider(
        _supadata_settings(enabled=True, api_key="sk-secret")
    )
    assert_true(isinstance(provider, SupadataTranscriptProvider))


@test()
def supadata_leads_and_captions_trails_when_it_is_present() -> None:
    """With Supadata configured it is the primary; captions/library become fallbacks."""
    captions = NullTranscriptProvider()
    library = NullTranscriptProvider()
    supadata = NullTranscriptProvider()

    provider = _compose_transcript_provider(
        captions=captions, library=library, supadata=supadata
    )

    assert isinstance(provider, FallbackTranscriptProvider)
    assert_true(provider.leaf_providers()[0] is supadata)
    assert_true(captions in provider.leaf_providers()[1:])
    assert_true(library in provider.leaf_providers()[1:])


@test()
def captions_leads_when_supadata_is_absent() -> None:
    """No Supadata falls back to the prior order: captions primary, library behind."""
    captions = NullTranscriptProvider()
    library = NullTranscriptProvider()

    provider = _compose_transcript_provider(
        captions=captions, library=library, supadata=None
    )

    assert isinstance(provider, FallbackTranscriptProvider)
    assert_true(provider.leaf_providers()[0] is captions)
