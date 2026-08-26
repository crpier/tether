"""Host health route behavior tests."""

from pathlib import Path
from tempfile import TemporaryDirectory

from snektest import assert_eq, test
from starlette.testclient import TestClient

from tether.server import AppConfig, create_app
from tether.telemetry import TelemetrySettings


@test()
def health_is_public_and_reports_readiness() -> None:
    """Compose can gate Open WebUI startup without an application credential."""
    with (
        TemporaryDirectory() as directory,
        TestClient(
            create_app(
                config=AppConfig(
                    api_token="capture-token",
                    database_path=Path(directory) / "tether.sqlite3",
                    open_webui_token="open-webui-token",
                    telemetry_database_path=Path(directory) / "telemetry.sqlite3",
                ),
                telemetry_settings=TelemetrySettings(install_global_provider=False),
            )
        ) as client,
    ):
        response = client.get("/health")

    assert_eq(response.status_code, 200)
    assert_eq(response.json(), {"status": "ok"})
