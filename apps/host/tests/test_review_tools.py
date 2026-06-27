"""Behaviour tests for the `/internal/tools/review_digest` loopback tool.

These drive the mounted Starlette app through `TestClient`, exercising the
read-only AI-assisted Review digest over the same auth gate and uniform envelope
as the other internal tools. Assertions are on grouping/flagging behaviour
surfaced in the envelope, never on model prose (there is none here).
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


def capture(client: TestClient, content: str) -> dict[str, Any]:
    """Capture one loose Memory and return its result payload."""
    envelope = call(client, "capture", content=content)
    assert_eq(envelope["success"], True)
    return envelope["result"]


def capture_tethered(client: TestClient, content: str) -> dict[str, Any]:
    """Capture then tether a Memory, landing it in the trusted corpus."""
    memory = capture(client, content)
    tethered = call(client, "tether", memory_id=memory["id"], version=memory["version"])
    assert_eq(tethered["success"], True)
    return tethered["result"]


@test()
def review_digest_without_secret_is_rejected() -> None:
    """A call lacking the per-process secret never reaches the tool."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        response = client.post(
            "/internal/tools/review_digest",
            json={"session_id": SESSION},
        )

    assert_eq(response.status_code, 401)


@test()
def review_digest_with_unknown_session_is_rejected() -> None:
    """Identity must resolve to a registered session id."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        response = client.post(
            "/internal/tools/review_digest",
            json={"session_id": "ghost"},
            headers={SECRET_HEADER: SECRET},
        )

    assert_eq(response.status_code, 401)


@test()
def review_digest_returns_a_uniform_collection_envelope() -> None:
    """The digest is a collection result: success, no provenance, null quota."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        envelope = call(client, "review_digest")

    assert_eq(envelope["success"], True)
    assert_is_none(envelope["error"])
    assert_is_none(envelope["provenance"])
    assert_is_none(envelope["quota"])
    result = envelope["result"]
    assert_in("queue", result)
    assert_in("dedup_groups", result)
    assert_in("bulk_groups", result)
    assert_in("contradictions", result)


@test()
def review_digest_groups_duplicates_and_flags_contradictions() -> None:
    """The digest clusters near-dup loose Memories and surfaces contradictions."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        tethered = capture_tethered(client, "I live in Berlin")
        contradicting = capture(client, "I live in Munich now")
        first_dup = capture(client, "I prefer aisle seats on flights")
        second_dup = capture(client, "I prefer aisle seats on flights please")

        result = call(client, "review_digest")["result"]

    dedup_clusters = [set(group["memory_ids"]) for group in result["dedup_groups"]]
    assert_in({first_dup["id"], second_dup["id"]}, dedup_clusters)

    contradiction_pairs = {
        (pair["loose_id"], pair["tethered_id"]) for pair in result["contradictions"]
    }
    assert_in((contradicting["id"], tethered["id"]), contradiction_pairs)
