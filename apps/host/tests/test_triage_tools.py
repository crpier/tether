"""Behaviour tests for the `/internal/tools/triage_report` loopback tool.

These drive the mounted Starlette app through `TestClient`, exercising the
read-only Triage report over the same auth gate and uniform envelope as the
other internal tools. Assertions are on the report behaviour surfaced in the
envelope, never on model prose (there is none here). Items are seeded through
the real Bucket item tools exactly as the agent would Add them.
"""

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast

from snektest import assert_eq, assert_in, assert_is_none, test
from starlette.testclient import TestClient

from tether.server import AppConfig, create_app
from tether.telemetry import TelemetrySettings
from tether.tools import SessionRegistry

SECRET = "test-process-secret"
SECRET_HEADER = "X-Tether-Tool-Secret"
SESSION = "session-abc"


def make_client(root: Path) -> TestClient:
    """A test app with an isolated DB/KB, a known tool secret, one session."""
    app = create_app(
        config=AppConfig(
            app_password="test-app-password",
            database_path=root / "tether.sqlite3",
            kb_root=root / ".tether",
            session_secret="test-session-secret",
        ),
        telemetry_settings=TelemetrySettings(install_global_provider=False),
        tool_secret=SECRET,
    )
    cast("SessionRegistry", app.state.session_registry).register(SESSION)
    return TestClient(app)


def call(client: TestClient, tool: str, **params: Any) -> dict[str, Any]:
    """Invoke a tool with the known secret and session, returning the envelope."""
    response = client.post(
        f"/internal/tools/{tool}",
        json={"session_id": SESSION, **params},
        headers={SECRET_HEADER: SECRET},
    )
    assert_eq(response.status_code, 200)
    return response.json()


def add_movie(client: TestClient, **params: Any) -> dict[str, Any]:
    """Add one active movie Bucket item and return its result payload."""
    envelope = call(client, "add_movie", **params)
    assert_eq(envelope["success"], True)
    return envelope["result"]["item"]


@test()
def triage_report_without_secret_is_rejected() -> None:
    """A call lacking the per-process secret never reaches the tool."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        response = client.post(
            "/internal/tools/triage_report",
            json={"session_id": SESSION},
        )

    assert_eq(response.status_code, 401)


@test()
def triage_report_with_unknown_session_is_rejected() -> None:
    """Identity must resolve to a registered session id."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        response = client.post(
            "/internal/tools/triage_report",
            json={"session_id": "ghost"},
            headers={SECRET_HEADER: SECRET},
        )

    assert_eq(response.status_code, 401)


@test()
def triage_report_returns_a_uniform_collection_envelope() -> None:
    """The report is a collection result: success, no provenance, null quota."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        envelope = call(client, "triage_report")

    assert_eq(envelope["success"], True)
    assert_is_none(envelope["error"])
    assert_is_none(envelope["provenance"])
    assert_is_none(envelope["quota"])
    result = envelope["result"]
    assert_in("active", result)
    assert_in("under_specified", result)
    assert_in("duplicates", result)
    assert_in("stale", result)


@test()
def triage_report_flags_under_specified_and_clusters_duplicates() -> None:
    """The report flags a year-less movie and clusters two live twins."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        vague = add_movie(client, title="Mystery Film", intent_context="a hunch")
        first = add_movie(client, title="Dune", year=2021, intent_context="hype")
        second = add_movie(client, title="dune", year=2021, intent_context="again")

        result = call(client, "triage_report")["result"]

    flagged = {item["bucket_item_id"] for item in result["under_specified"]}
    assert_in(vague["id"], flagged)

    clusters = [set(cluster["bucket_item_ids"]) for cluster in result["duplicates"]]
    assert_in({first["id"], second["id"]}, clusters)
