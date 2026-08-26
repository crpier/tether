"""Shared TestClient drivers for retained headless host boundaries."""

from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from snektest import assert_eq
from starlette.testclient import TestClient

from tether.server import AppConfig, create_app
from tether.telemetry import TelemetrySettings

API_TOKEN = "test-capture-token"
OPEN_WEBUI_TOKEN = "test-open-webui-token"


@contextmanager
def surface_client(root: Path) -> Generator[TestClient]:
    """Run the retained host with isolated canonical and telemetry databases."""
    with TestClient(
        create_app(
            config=AppConfig(
                api_token=API_TOKEN,
                database_path=root / "tether.sqlite3",
                open_webui_token=OPEN_WEBUI_TOKEN,
                telemetry_database_path=root / "telemetry.sqlite3",
            ),
            telemetry_settings=TelemetrySettings(install_global_provider=False),
        ),
        headers={"Authorization": f"Bearer {API_TOKEN}"},
    ) as client:
        yield client


def login(client: TestClient) -> None:
    """Retain the old setup spelling while browser authentication is absent."""
    _ = client


def call_tool(client: TestClient, tool: str, **params: Any) -> dict[str, Any]:
    """Invoke one Open WebUI tool and return its envelope."""
    response = client.post(
        f"/tools/{tool}",
        json=params,
        headers={"Authorization": f"Bearer {OPEN_WEBUI_TOKEN}"},
    )
    assert_eq(response.status_code, 200)
    document: dict[str, Any] = response.json()
    return document
