"""Host server configuration and process entrypoint tests."""

import asyncio
import contextlib
import json
import logging
import os
import subprocess
import sys
import time
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from datetime import timedelta
from io import StringIO
from pathlib import Path
from socket import AF_INET, SOCK_STREAM, socket
from tempfile import TemporaryDirectory

import structlog
from fastapi import FastAPI
from httpx2 import AsyncClient
from snekok import Err, Ok, Result
from snektest import (
    assert_eq,
    assert_false,
    assert_in,
    assert_raises,
    assert_true,
    test,
)
from starlette.applications import Starlette
from starlette.testclient import TestClient

from tether import server
from tether.app_runtime import app_runtime
from tether.gmail_client import GmailNetworkFailure, GmailResponse
from tether.host_resources import shutdown_background_tasks
from tether.model_selection import AgentModelConfig
from tether.server import (
    AppConfig,
    HostSettings,
    create_app_from_environment,
    serve,
)
from tether.structured_logging import QUIET_LOGGERS, SILENCED_LOGGERS
from tether.telemetry import TelemetryExporter, TelemetrySettings
from tether.transcripts.contracts import (
    TranscriptFetchResult,
    TranscriptProviderChain,
    TranscriptUnavailableFailure,
)


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
                        "thinking_level": "medium",
                    }
                ]
            ),
            TETHER_PORT="9001",
            TETHER_RELOAD="true",
            TETHER_SECURE_COOKIES="true",
            TETHER_SESSION_SECRET="configured-session-secret",
            TETHER_STT_API_KEY="configured-stt-key",
            TETHER_TELEMETRY_DATABASE_PATH=f"{directory}/health.sqlite3",
            TETHER_TELEMETRY_ENVIRONMENT="test",
            TETHER_TELEMETRY_EXPORTER="none",
            TETHER_TELEMETRY_SERVICE_NAME="tether-test",
            TETHER_TOOL_SECRET="configured-tool-secret",
            TETHER_TRANSCRIPT_SYNC_ENABLED="false",
            TETHER_VAPID_PRIVATE_KEY="private-key",
            TETHER_VAPID_PUBLIC_KEY="public-key",
            TETHER_VAPID_SUBJECT="mailto:test@example.com",
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
    assert_eq(settings.model_allowlist[0].thinking_level, "medium")
    assert_eq(settings.port, 9001)
    assert_true(settings.reload)
    assert_eq(settings.session_secret, "configured-session-secret")
    assert_eq(settings.stt_api_key, "configured-stt-key")
    assert_eq(settings.telemetry_database_path, Path(directory) / "health.sqlite3")
    assert_eq(settings.telemetry.environment, "test")
    assert_eq(settings.telemetry.exporter, TelemetryExporter.NONE)
    assert_eq(settings.telemetry.service_name, "tether-test")
    assert_eq(settings.tool_secret, "configured-tool-secret")
    assert_eq(settings.vapid_private_key, "private-key")
    assert_eq(settings.vapid_public_key, "public-key")
    assert_eq(settings.vapid_subject, "mailto:test@example.com")
    assert_false(settings.youtube_sync_enabled)
    assert_false(settings.transcript_sync_enabled)


@test()
def local_dependency_profile_isolates_state_and_external_boundaries() -> None:
    """Local startup ignores production state, credentials, and integration gates."""
    with TemporaryDirectory() as directory:
        root = Path(directory)
        youtube_token = root / "youtube-token.json"
        gmail_token = root / "gmail-token.json"
        _ = youtube_token.write_text("production-token")
        _ = gmail_token.write_text("production-token")
        settings = HostSettings(
            app_password="test-app-password",
            database_path=root / "production.sqlite3",
            default_model="production",
            dependency_profile="local",
            ebook_statistics_db_path=str(root / "production-ebooks.sqlite3"),
            gmail_purge_enabled=True,
            gmail_sync_enabled=True,
            gmail_token_path=gmail_token,
            kb_root=root / "production-kb",
            kosync_enabled=True,
            kosync_username="production-user",
            kosync_userkey="production-key",
            local_data_root=root / "local",
            model_allowlist=(
                AgentModelConfig(
                    display_name="Production model",
                    id="production",
                    model_id="production-model",
                    provider="production-provider",
                ),
            ),
            readwise_api_key="production-readwise-key",
            readwise_reader_sync_enabled=True,
            readwise_sync_enabled=True,
            search_api_key="production-search-key",
            session_secret="test-session-secret",
            stt_api_key="production-stt-key",
            supadata_api_key="production-supadata-key",
            supadata_enabled=True,
            transcript_sync_enabled=True,
            vapid_private_key="production-private-key",
            vapid_public_key="production-public-key",
            vapid_subject="mailto:production@example.com",
            youtube_sync_enabled=True,
            youtube_token_path=youtube_token,
        )

        config = server._app_config_from_settings(settings)

    assert_eq(config.database_path, root / "local/tether.sqlite3")
    assert_eq(config.telemetry_database_path, root / "local/telemetry.sqlite3")
    assert_eq(config.kb_root, root / "local/kb")
    assert_eq(config.default_model, "local")
    assert_eq(config.model_allowlist[0].provider, "faux")
    assert_in("local-faux.ts", str(config.extra_extension_paths[0]))
    assert_true(config.provider_auth_backend is not None)
    assert_true(config.stt_client is not None)
    assert_eq(config.search_provider, None)
    assert_eq(config.youtube_api, None)
    assert_false(config.youtube_sync_enabled)
    assert_eq(config.transcript_provider, None)
    assert_false(config.transcript_sync_enabled)
    assert_eq(config.gmail_transport, None)
    assert_false(config.gmail_sync_enabled)
    assert_false(config.gmail_purge_enabled)
    assert_eq(config.readwise_api_key, "")
    assert_false(config.readwise_sync_enabled)
    assert_false(config.readwise_reader_sync_enabled)
    assert_eq(config.vapid_public_key, "")
    assert_eq(config.ebook_statistics_db_path, "")
    assert_false(config.kosync_enabled)


async def _wait_for_local_service(url: str) -> None:
    """Wait briefly for one process managed by the local development command."""
    async with AsyncClient() as client:
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            try:
                response = await client.get(url)
            except Exception:
                await asyncio.sleep(0.1)
                continue
            if response.status_code < 500:
                return
            await asyncio.sleep(0.1)
    raise AssertionError(f"local service did not become ready: {url}")


@test()
async def dev_local_command_exposes_the_full_local_stack() -> None:
    """`just dev-local` exposes both development servers over disposable state."""
    with TemporaryDirectory() as directory:
        ports: list[int] = []
        for _ in range(2):
            with socket(AF_INET, SOCK_STREAM) as listener:
                listener.bind(("127.0.0.1", 0))
                ports.append(listener.getsockname()[1])
        process_environment = os.environ.copy()
        process_environment["OPENAI_API_KEY"] = "must-not-reach-pi"
        process_environment["TETHER_SEARCH_API_KEY"] = "must-not-reach-host"
        process = await asyncio.create_subprocess_exec(
            "just",
            "dev-local",
            str(ports[0]),
            str(ports[1]),
            directory,
            cwd=__file__.rsplit("/", 4)[0],
            env=process_environment,
            stderr=asyncio.subprocess.STDOUT,
            stdout=asyncio.subprocess.PIPE,
        )
        try:
            await _wait_for_local_service(f"http://127.0.0.1:{ports[0]}/openapi.json")
            await _wait_for_local_service(f"http://127.0.0.1:{ports[1]}")
            async with AsyncClient(base_url=f"http://127.0.0.1:{ports[0]}") as client:
                login_response = await client.post(
                    "/api/auth/login", json={"password": "dev"}
                )
                transcription_response = await client.post(
                    "/api/stt/transcriptions",
                    files={"file": ("clip.webm", b"local-audio", "audio/webm")},
                )
        finally:
            if process.returncode is None:
                process.terminate()
                try:
                    _ = await asyncio.wait_for(process.wait(), timeout=4)
                except TimeoutError:
                    process.kill()
                    _ = await process.wait()

        _ = await process.stdout.read() if process.stdout is not None else b""
        assert_true(process.returncode is not None)
        assert_eq(login_response.status_code, 204)
        assert_eq(transcription_response.json(), {"transcript": "Local transcription."})
        assert_true((Path(directory) / "tether.sqlite3").exists())


@test()
def telemetry_database_defaults_beside_main_database() -> None:
    """An unconfigured telemetry store sits beside the configured main store."""
    settings = HostSettings(
        app_password="test-app-password",
        database_path=Path("/data/custom-main.sqlite3"),
        session_secret="test-session-secret",
        stt_api_key="test-stt-key",
    )

    assert_eq(
        settings.resolved_telemetry_database_path, Path("/data/telemetry.sqlite3")
    )


@test()
def web_search_defaults_enabled() -> None:
    """The Tavily feature flag is on unless explicitly disabled."""
    settings = HostSettings(
        app_password="test-app-password",
        session_secret="test-session-secret",
        stt_api_key="test-stt-key",
    )

    assert_true(settings.search_enabled)


@test()
def sync_enabled_defaults_to_true() -> None:
    """Ingestion syncs are on unless a `TETHER_*_SYNC_ENABLED` flag disables them."""
    settings = HostSettings(
        app_password="test-app-password",
        session_secret="test-session-secret",
        stt_api_key="test-stt-key",
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
            TETHER_STT_API_KEY="test-stt-key",
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
def app_factory_returns_fastapi() -> None:
    """The host's public ASGI application is FastAPI."""
    app = server.create_app(
        config=AppConfig(
            app_password="test-app-password",
            database_path=":memory:",
            kb_root="unused",
            session_secret="test-session-secret",
        )
    )

    assert_eq(app.__class__, FastAPI)


@test()
def app_lifespan_installs_a_complete_typed_runtime() -> None:
    """The request-serving phase exposes one complete dependency graph."""
    with TemporaryDirectory() as directory:
        app = server.create_app(
            config=AppConfig(
                app_password="test-app-password",
                database_path=":memory:",
                kb_root=directory,
                session_secret="test-session-secret",
            )
        )

        with TestClient(app):
            runtime = app_runtime(app)

            assert_eq(runtime.app_password, "test-app-password")
            assert_false(hasattr(app.state, "youtube_service"))


@test()
def app_lifespan_closes_transcript_provider_resources() -> None:
    """Application shutdown closes reusable transcript HTTP resources."""

    class ClosableSource:
        source: str = "supadata"

        def __init__(self) -> None:
            self.close_calls: int = 0

        async def fetch(self, video_id: str) -> TranscriptFetchResult:
            return Err(TranscriptUnavailableFailure(video_id=video_id))

        async def aclose(self) -> None:
            self.close_calls += 1

    source = ClosableSource()
    with TemporaryDirectory() as directory:
        app = server.create_app(
            config=AppConfig(
                app_password="test-app-password",
                database_path=":memory:",
                kb_root=directory,
                session_secret="test-session-secret",
                transcript_provider=TranscriptProviderChain([source]),
            )
        )

        with TestClient(app):
            pass

        assert_eq(source.close_calls, 1)


@test()
def app_lifespan_closes_gmail_transport_after_its_worker_stops() -> None:
    """Shutdown cancels Gmail ingestion before closing its HTTP transport."""

    class ClosableGmailTransport:
        def __init__(self) -> None:
            self.close_calls: int = 0
            self.list_calls: int = 0

        async def aclose(self) -> None:
            self.close_calls += 1

        async def list_messages(
            self, *, query: str, page_token: str | None
        ) -> Result[GmailResponse, GmailNetworkFailure]:
            _ = query, page_token
            self.list_calls += 1
            return Ok(GmailResponse(payload={"messages": []}, status_code=200))

        async def get_message(
            self, message_id: str
        ) -> Result[GmailResponse, GmailNetworkFailure]:
            _ = message_id
            return Ok(GmailResponse(payload={}, status_code=404))

        async def list_labels(
            self,
        ) -> Result[GmailResponse, GmailNetworkFailure]:
            return Ok(GmailResponse(payload={"labels": []}, status_code=200))

        async def modify_labels(
            self,
            message_id: str,
            *,
            add_label_ids: Sequence[str],
            remove_label_ids: Sequence[str],
        ) -> Result[GmailResponse, GmailNetworkFailure]:
            _ = message_id, add_label_ids, remove_label_ids
            return Ok(GmailResponse(payload={}, status_code=200))

        async def trash_message(
            self, message_id: str
        ) -> Result[GmailResponse, GmailNetworkFailure]:
            _ = message_id
            return Ok(GmailResponse(payload={}, status_code=200))

    transport = ClosableGmailTransport()
    with TemporaryDirectory() as directory:
        app = server.create_app(
            config=AppConfig(
                app_password="test-app-password",
                database_path=":memory:",
                gmail_sync_enabled=True,
                gmail_transport=transport,
                kb_root=directory,
                session_secret="test-session-secret",
            )
        )

        with TestClient(app) as client:
            portal = client.portal
            if portal is not None:
                portal.call(
                    app_runtime(app).ingestion_lifecycle.readiness("gmail").wait
                )

        assert_eq(transport.list_calls, 1)
        assert_eq(transport.close_calls, 1)


@test()
def failed_app_startup_closes_transcript_provider_resources() -> None:
    """Owned resources close when a later startup step fails."""

    class ClosableSource:
        source: str = "supadata"

        def __init__(self) -> None:
            self.close_calls: int = 0

        async def fetch(self, video_id: str) -> TranscriptFetchResult:
            return Err(TranscriptUnavailableFailure(video_id=video_id))

        async def aclose(self) -> None:
            self.close_calls += 1

    class FailingEmbedder:
        @property
        def model_name(self) -> str:
            message = "embedding startup failed"
            raise RuntimeError(message)

        @property
        def vector_dim(self) -> int:
            return 8

        async def embed_documents(self, texts: object) -> list[list[float]]:
            return []

        async def embed_query(self, text: str) -> list[float]:
            return []

    source = ClosableSource()
    with TemporaryDirectory() as directory:
        app = server.create_app(
            config=AppConfig(
                app_password="test-app-password",
                database_path=":memory:",
                kb_root=directory,
                session_secret="test-session-secret",
                transcript_provider=TranscriptProviderChain([source]),
            ),
            embedder=FailingEmbedder(),
        )

        with assert_raises(RuntimeError), TestClient(app):
            pass

        assert_eq(source.close_calls, 1)


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
                "TETHER_STT_API_KEY": "test-stt-key",
            },
            text=True,
        )

    assert_eq(completed.returncode, 0)
    assert_eq(completed.stderr, "")


@test()
def serve_quiets_host_dependency_loggers() -> None:
    """The host adds its noisy dependencies to generic server logger defaults."""
    dependency_names = ("aiosqlite", "snekql", "httpcore2")
    original_levels = {name: logging.getLogger(name).level for name in dependency_names}

    def fake_run(_app: object, **_kwargs: object) -> None:
        pass

    original_run = server.uvicorn.run
    server.uvicorn.run = fake_run
    try:
        with captured_logging():
            serve(
                HostSettings(
                    app_password="test-app-password",
                    session_secret="test-session-secret",
                    stt_api_key="test-stt-key",
                )
            )
            configured_levels = {
                name: logging.getLogger(name).level for name in dependency_names
            }
    finally:
        server.uvicorn.run = original_run
        for name, level in original_levels.items():
            logging.getLogger(name).setLevel(level)

    assert_eq(configured_levels, dict.fromkeys(dependency_names, logging.WARNING))


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
                    stt_api_key="test-stt-key",
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
            TETHER_STT_API_KEY="test-stt-key",
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


@test()
def transcript_rate_limit_defaults_are_strict() -> None:
    """Defaults bound each pass, pace library calls, and back off real blocks."""
    settings = HostSettings(
        app_password="test-app-password",
        session_secret="test-session-secret",
        stt_api_key="test-stt-key",
    )
    assert_eq(settings.transcript_library_max_requests_per_pass, 5)
    assert_eq(settings.transcript_library_min_request_interval_seconds, 5.0)
    assert_eq(settings.transcript_block_pause_base_seconds, 2 * 60 * 60)
    assert_eq(settings.transcript_block_pause_cap_seconds, 24 * 60 * 60)


@test()
def app_config_from_settings_threads_the_block_pause_bounds() -> None:
    """Environment pause bounds become one acquisition configuration value."""
    settings = HostSettings(
        app_password="test-app-password",
        session_secret="test-session-secret",
        stt_api_key="test-stt-key",
        transcript_block_pause_base_seconds=111,
        transcript_block_pause_cap_seconds=222,
    )

    config = server._app_config_from_settings(settings)

    assert_eq(
        config.transcript_acquisition_config.block_pause_base,
        timedelta(seconds=111),
    )
    assert_eq(
        config.transcript_acquisition_config.block_pause_cap,
        timedelta(seconds=222),
    )


@test()
async def shutdown_awaits_tasks_that_honor_cancellation() -> None:
    """A task that responds to cancellation promptly is awaited and finished."""
    started = asyncio.Event()

    async def cooperative() -> None:
        started.set()
        await asyncio.sleep(10)

    task = asyncio.create_task(cooperative(), name="cooperative")
    await started.wait()

    await shutdown_background_tasks(
        [task],
        logger=structlog.stdlib.get_logger("test.server.shutdown"),
        grace_seconds=1.0,
    )

    assert_true(task.done())
    assert_true(task.cancelled())


@test()
async def shutdown_does_not_block_on_a_task_that_ignores_cancellation() -> None:
    """A task that doesn't unwind promptly on cancel is abandoned, not awaited.

    Regression test for the `just dev` shutdown hang: the boot lifespan awaited
    every background task to fully finish before returning, with no bound. A
    task whose current await doesn't propagate `CancelledError` right away
    (in production, the YouTube/transcript sync loops mid a synchronous
    `asyncio.to_thread` upstream call) could keep shutdown — and the whole
    `uvicorn --reload` process tree under `just dev` — waiting for however
    long that took (observed: up to ~2 minutes). Shutdown must bound the wait
    instead.
    """
    swallowed_once = False

    async def stubborn() -> None:
        nonlocal swallowed_once
        while True:
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                if swallowed_once:
                    raise
                swallowed_once = True

    task = asyncio.create_task(stubborn(), name="stubborn")
    await asyncio.sleep(0)  # let it reach the first sleep

    before = time.monotonic()
    await shutdown_background_tasks(
        [task],
        logger=structlog.stdlib.get_logger("test.server.shutdown"),
        grace_seconds=0.2,
    )
    elapsed = time.monotonic() - before

    # Bounded by the grace period, not the task's full unresponsive stretch.
    assert_true(elapsed < 1.0)
    assert_false(task.done())

    # Second cancel actually lands (the fake only swallows the first one);
    # drain it so it doesn't outlive the test.
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=1.0)
