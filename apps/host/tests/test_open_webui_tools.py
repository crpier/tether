"""Open WebUI OpenAPI tool-server behavior tests."""

from pathlib import Path
from tempfile import TemporaryDirectory

from snektest import assert_eq, assert_in, assert_not_in, test
from starlette.testclient import TestClient
from structlog.testing import capture_logs
from structlog.typing import EventDict

from tether.open_webui import SELECTED_TOOL_NAMES
from tether.server import AppConfig, create_app
from tether.telemetry import TelemetrySettings

API_TOKEN = "capture-api-token"
OPEN_WEBUI_TOKEN = "open-webui-tool-token"
AUTHORIZATION = {"Authorization": f"Bearer {OPEN_WEBUI_TOKEN}"}


def make_client(root: Path) -> TestClient:
    """Create an isolated host with independent capture and tool credentials."""
    return TestClient(
        create_app(
            config=AppConfig(
                api_token=API_TOKEN,
                database_path=root / "tether.sqlite3",
                open_webui_token=OPEN_WEBUI_TOKEN,
                telemetry_database_path=root / "telemetry.sqlite3",
            ),
            telemetry_settings=TelemetrySettings(install_global_provider=False),
        )
    )


@test()
def openapi_document_requires_open_webui_bearer_token() -> None:
    """Anonymous clients cannot discover the tool schema."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        response = client.get("/tools/openapi.json")

    assert_eq(response.status_code, 401)


@test()
def capture_bearer_token_cannot_authorize_tools() -> None:
    """The Android credential is not valid at the Open WebUI boundary."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        response = client.get(
            "/tools/openapi.json",
            headers={"Authorization": f"Bearer {API_TOKEN}"},
        )

    assert_eq(response.status_code, 401)


@test()
def openapi_document_exposes_only_selected_operations() -> None:
    """Discovery publishes the fixed deterministic capability selection."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        document = client.get("/tools/openapi.json", headers=AUTHORIZATION).json()

    assert_eq(
        set(document["paths"]),
        {f"/tools/{name}" for name in SELECTED_TOOL_NAMES},
    )
    assert_eq(
        {path_item["post"]["operationId"] for path_item in document["paths"].values()},
        set(SELECTED_TOOL_NAMES),
    )
    assert_eq(
        document["components"]["securitySchemes"]["OpenWebUIBearer"],
        {"scheme": "bearer", "type": "http"},
    )


@test()
def selected_collection_parameters_have_hard_result_bounds() -> None:
    """Record and Bucket searches cannot request more than fifty results."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        document = client.get("/tools/openapi.json", headers=AUTHORIZATION).json()

    schemas = document["components"]["schemas"]
    assert_eq(
        schemas["SearchBucketItemsParams"]["properties"]["limit"]["maximum"],
        50,
    )
    assert_eq(
        schemas["QueryHealthConnectParams"]["properties"]["limit"]["maximum"],
        50,
    )
    assert_in("bounded", schemas["ListTodosParams"]["description"].casefold())


@test()
def public_schema_omits_deleted_runtime_vocabulary() -> None:
    """Tool descriptions and models contain no session or trigger continuation API."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        document = client.get("/tools/openapi.json", headers=AUTHORIZATION).json()

    rendered = str(document).casefold()
    assert_not_in("session identity", rendered)
    assert_not_in("scheduled trigger", rendered)
    assert_not_in("link_todo_trigger", rendered)
    assert_not_in("trigger_id", rendered)


@test()
def mutation_tool_runs_without_runtime_transport_fields() -> None:
    """A Todo mutation accepts domain parameters and returns the shared envelope."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        response = client.post(
            "/tools/create_todo",
            json={"action": "call the dentist"},
            headers=AUTHORIZATION,
        )

    assert_eq(response.status_code, 200)
    assert_eq(response.json()["result"]["action"], "call the dentist")
    assert_eq(response.json()["success"], True)


@test()
def invalid_parameters_return_a_tool_error_envelope() -> None:
    """Body validation remains a tool value rather than a framework 422."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        response = client.post("/tools/create_todo", json={}, headers=AUTHORIZATION)

    assert_eq(response.status_code, 200)
    assert_eq(response.json()["success"], False)
    assert_eq(response.json()["error"]["code"], "invalid_input")
    assert_in("action", response.json()["error"]["message"])


@test()
def tool_logs_contain_only_bounded_call_metadata() -> None:
    """Diagnostics omit credentials, arguments, and returned domain values."""
    with (
        TemporaryDirectory() as directory,
        make_client(Path(directory)) as client,
        capture_logs() as logs,
    ):
        response = client.post(
            "/tools/create_todo",
            json={"action": "sensitive dentist details"},
            headers=AUTHORIZATION,
        )

    assert_eq(response.status_code, 200)
    tool_logs: list[EventDict] = [
        entry for entry in logs if entry.get("event") == "Open WebUI tool call"
    ]
    assert_eq(len(tool_logs), 1)
    assert_eq(
        set(tool_logs[0]),
        {"duration_ms", "event", "log_level", "operation", "success"},
    )
    assert_not_in("sensitive dentist details", str(tool_logs[0]))
    assert_not_in(OPEN_WEBUI_TOKEN, str(tool_logs[0]))
