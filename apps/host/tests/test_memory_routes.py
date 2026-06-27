"""REST behavior tests for the Memory Review spine.

These drive the mounted Starlette app through `TestClient`, so request parsing,
route wiring, service behavior, and response serialization are checked together.
"""

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from snektest import assert_eq, assert_in, assert_not_in, assert_true, test
from starlette.testclient import TestClient

from tether.server import AppConfig, create_app
from tether.telemetry import TelemetrySettings

APP_PASSWORD = "test-app-password"
SESSION_SECRET = "test-session-secret"


def make_client(root: Path) -> TestClient:
    """Create a test app with isolated persistent DB and `.tether` root."""
    return TestClient(
        create_app(
            config=AppConfig(
                app_password=APP_PASSWORD,
                database_path=root / "tether.sqlite3",
                kb_root=root / ".tether",
                session_secret=SESSION_SECRET,
            ),
            telemetry_settings=TelemetrySettings(install_global_provider=False),
        )
    )


def login(client: TestClient) -> None:
    """Authenticate the test browser."""
    response = client.post("/api/auth/login", json={"password": APP_PASSWORD})
    assert_eq(response.status_code, 204)


def capture(client: TestClient, content: str) -> dict[str, Any]:
    """Capture one Memory through REST and return its JSON representation."""
    login(client)
    response = client.post("/api/memories", json={"content": content})
    assert_eq(response.status_code, 201)
    return response.json()


@test()
def post_memories_captures_trimmed_content() -> None:
    """`POST /api/memories` accepts `content` and returns a loose Memory."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        memory = capture(client, "  I prefer aisle seats  ")

    assert_eq(memory["content"], "I prefer aisle seats")
    assert_eq(memory["state"], "loose")
    assert_eq(memory["version"], 1)


@test()
def post_memories_rejects_blank_content() -> None:
    """`content` must be non-empty after trimming."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        login(client)
        response = client.post("/api/memories", json={"content": "   "})

    assert_eq(response.status_code, 422)
    assert_eq(response.json()["detail"][0]["loc"], ["content"])


@test()
def keyword_search_returns_tethered_memories_only() -> None:
    """REST keyword Search excludes loose Memories sharing the query term."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        loose = capture(client, "needle loose memory")
        tethered = capture(client, "needle tethered memory")
        response = client.post(
            f"/api/memories/{tethered['id']}/tether",
            json={"version": tethered["version"]},
        )
        assert_eq(response.status_code, 200)

        search_response = client.get("/api/memories/search", params={"q": "needle"})

    found = [memory["id"] for memory in search_response.json()]
    assert_in(tethered["id"], found)
    assert_not_in(loose["id"], found)


@test()
def patch_memories_edits_content_and_keeps_version_checks() -> None:
    """`PATCH /api/memories/{id}` edits `content` at the observed version."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        memory = capture(client, "I live in Berlin")
        response = client.patch(
            f"/api/memories/{memory['id']}",
            json={"content": "  I live in Munich  ", "version": memory["version"]},
        )

    assert_eq(response.status_code, 200)
    assert_eq(response.json()["content"], "I live in Munich")
    assert_eq(response.json()["version"], 2)


@test()
def delete_memories_soft_deletes_and_removes_from_search() -> None:
    """`DELETE /api/memories/{id}` uses the version query param and hides Memory."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        memory = capture(client, "needle deleted memory")
        tether_response = client.post(
            f"/api/memories/{memory['id']}/tether",
            json={"version": memory["version"]},
        )
        tethered = tether_response.json()

        delete_response = client.delete(
            f"/api/memories/{memory['id']}",
            params={"version": tethered["version"]},
        )
        search_response = client.get("/api/memories/search", params={"q": "needle"})

    assert_eq(delete_response.status_code, 200)
    assert_eq(search_response.json(), [])


@test()
def configured_database_path_persists_between_app_instances() -> None:
    """A configured SQLite path survives app shutdown and restart."""
    with TemporaryDirectory() as directory:
        root = Path(directory)
        database_path = root / "host.sqlite3"
        kb_root = root / ".tether"
        with TestClient(
            create_app(
                config=AppConfig(
                    app_password=APP_PASSWORD,
                    database_path=database_path,
                    kb_root=kb_root,
                    session_secret=SESSION_SECRET,
                ),
                telemetry_settings=TelemetrySettings(install_global_provider=False),
            )
        ) as client:
            memory = capture(client, "I prefer aisle seats")
        with TestClient(
            create_app(
                config=AppConfig(
                    app_password=APP_PASSWORD,
                    database_path=database_path,
                    kb_root=kb_root,
                    session_secret=SESSION_SECRET,
                ),
                telemetry_settings=TelemetrySettings(install_global_provider=False),
            )
        ) as client:
            login(client)
            response = client.get("/api/memories", params={"state": "loose"})

        found = [memory["id"] for memory in response.json()]
        assert_in(memory["id"], found)
        assert_true(database_path.exists())
