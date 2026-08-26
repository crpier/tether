"""Headless host lifespan, authentication, and route-surface tests."""

import os
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

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

from tether.app_runtime import app_runtime
from tether.server import (
    AppConfig,
    HostConfigurationError,
    create_app,
    create_app_from_environment,
)

CAPTURE_TOKEN = "capture-token"
OPEN_WEBUI_TOKEN = "open-webui-token"
CAPTURE_AUTHORIZATION = {"Authorization": f"Bearer {CAPTURE_TOKEN}"}
OPEN_WEBUI_AUTHORIZATION = {"Authorization": f"Bearer {OPEN_WEBUI_TOKEN}"}


@contextmanager
def headless_host(root: Path) -> Generator[TestClient]:
    """Boot the environment factory with only retained host configuration."""
    retained_environment = {
        "TETHER_API_TOKEN": CAPTURE_TOKEN,
        "TETHER_DATABASE_PATH": str(root / "tether.sqlite3"),
        "TETHER_OPEN_WEBUI_TOKEN": OPEN_WEBUI_TOKEN,
        "TETHER_TELEMETRY_DATABASE_PATH": str(root / "telemetry.sqlite3"),
    }
    forbidden_names = {
        "TETHER_APP_PASSWORD",
        "TETHER_DEFAULT_MODEL",
        "TETHER_MODEL_ALLOWLIST",
        "TETHER_PI_BINARY",
        "TETHER_SESSION_SECRET",
        "TETHER_STT_API_KEY",
        "TETHER_TTS_API_KEY",
        "TETHER_VAPID_PRIVATE_KEY",
        "TETHER_VAPID_PUBLIC_KEY",
        "TETHER_VAPID_SUBJECT",
    }
    previous = {
        name: os.environ.get(name)
        for name in set(retained_environment) | forbidden_names
    }
    for name in forbidden_names:
        os.environ.pop(name, None)
    os.environ.update(retained_environment)
    try:
        with TestClient(create_app_from_environment()) as client:
            yield client
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


@test()
def headless_host_boots_without_assistant_runtime_secrets() -> None:
    """Database paths and independent bearer tokens are sufficient to boot."""
    with TemporaryDirectory() as directory:
        root = Path(directory)

        with headless_host(root):
            main_exists = (root / "tether.sqlite3").exists()
            telemetry_exists = (root / "telemetry.sqlite3").exists()

    assert_true(main_exists)
    assert_true(telemetry_exists)


@test()
def health_is_public() -> None:
    """Container health checks need no application credential."""
    with TemporaryDirectory() as directory, headless_host(Path(directory)) as client:
        response = client.get("/health")

    assert_eq(response.status_code, 200)
    assert_eq(response.json(), {"status": "ok"})


@test()
def capture_token_authorizes_health_connect_only() -> None:
    """The Android credential reaches Health Connect but not assistant tools."""
    with TemporaryDirectory() as directory, headless_host(Path(directory)) as client:
        health = client.get(
            "/api/telemetry/health-connect/sync-state",
            params={"installation_id": "pixel", "record_types": "steps"},
            headers=CAPTURE_AUTHORIZATION,
        )
        tools = client.get("/tools/openapi.json", headers=CAPTURE_AUTHORIZATION)

    assert_eq(health.status_code, 200)
    assert_eq(tools.status_code, 401)


@test()
def open_webui_token_authorizes_tools_only() -> None:
    """The Open WebUI credential cannot impersonate the Android client."""
    with TemporaryDirectory() as directory, headless_host(Path(directory)) as client:
        tools = client.get("/tools/openapi.json", headers=OPEN_WEBUI_AUTHORIZATION)
        health = client.get(
            "/api/telemetry/health-connect/sync-state",
            params={"installation_id": "pixel", "record_types": "steps"},
            headers=OPEN_WEBUI_AUTHORIZATION,
        )

    assert_eq(tools.status_code, 200)
    assert_eq(health.status_code, 401)


@test()
def shared_boundary_token_is_rejected_at_startup() -> None:
    """One bearer value cannot grant both Android and Open WebUI authority."""
    with assert_raises(HostConfigurationError):
        _ = create_app(
            config=AppConfig(api_token="shared-token", open_webui_token="shared-token")
        )


@test()
def deleted_http_surfaces_are_absent() -> None:
    """Legacy browser, chat, capture, speech, and scheduling routes stay deleted."""
    paths = (
        "/api/auth/session",
        "/api/capture",
        "/api/conversations",
        "/api/memory-topics",
        "/api/notifications",
        "/api/provider-auth/status",
        "/api/stt/transcriptions",
        "/api/triggers",
        "/api/tts/speech",
        "/docs",
        "/openapi.json",
        "/redoc",
        "/trace",
        "/ws",
    )
    with TemporaryDirectory() as directory, headless_host(Path(directory)) as client:
        statuses = [client.get(path).status_code for path in paths]

    assert_eq(statuses, [404] * len(paths))


@test()
def only_deterministic_background_work_is_composed() -> None:
    """Startup owns only the deterministic Health episode projection sweep."""
    with TemporaryDirectory() as directory, headless_host(Path(directory)) as client:
        assert isinstance(client.app, Starlette)
        task_names = [task.get_name() for task in app_runtime(client.app).tasks]

    assert_eq(task_names, ["health-episode-sweep"])
    assert_false(any("model" in name.casefold() for name in task_names))
    assert_false(any("pi-runtime" in name.casefold() for name in task_names))


@test()
def selected_list_and_search_tools_publish_hard_result_bounds() -> None:
    """Tool schemas prevent an assistant from requesting unbounded collections."""
    with TemporaryDirectory() as directory, headless_host(Path(directory)) as client:
        document = client.get(
            "/tools/openapi.json", headers=OPEN_WEBUI_AUTHORIZATION
        ).json()

    schemas = document["components"]["schemas"]
    search_schema = schemas["SearchBucketItemsParams"]
    assert_eq(search_schema["properties"]["limit"]["maximum"], 50)
    assert_eq(search_schema["properties"]["limit"]["default"], 50)
    assert_in("ListTodosParams", schemas)


@test()
def public_tool_schema_omits_deleted_session_and_trigger_language() -> None:
    """Descriptions speak only in the retained headless capability vocabulary."""
    with TemporaryDirectory() as directory, headless_host(Path(directory)) as client:
        document = client.get(
            "/tools/openapi.json", headers=OPEN_WEBUI_AUTHORIZATION
        ).json()

    rendered = str(document).casefold()
    assert_false("session identity" in rendered)
    assert_false("scheduled trigger" in rendered)
    assert_false("link_todo_trigger" in rendered)
