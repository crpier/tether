"""REST behavior tests for Bucket items.

These drive the mounted Starlette app through `TestClient`, so request parsing,
route wiring, service behavior, and response serialization are checked together.
"""

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from snektest import assert_eq, assert_in, assert_not_in, test
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


def add_item(
    client: TestClient,
    item_type: str = "movie",
    data: dict[str, Any] | None = None,
    intent_context: str = "saved on a whim",
) -> dict[str, Any]:
    """Add one Bucket item through REST and return the response JSON."""
    login(client)
    payload = data if data is not None else {"title": "Dune"}
    response = client.post(
        "/api/bucket-items",
        json={
            "item_type": item_type,
            "data": payload,
            "intent_context": intent_context,
        },
    )
    assert_eq(response.status_code, 201)
    return response.json()


@test()
def post_bucket_items_adds_active_item_with_intent_and_provenance() -> None:
    """`POST /api/bucket-items` Adds an active item recording intent + provenance."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        body = add_item(
            client, "movie", {"title": "Dune", "year": 2021}, "a friend raved"
        )

    item = body["item"]
    assert_eq(item["state"], "active")
    assert_eq(item["item_type"], "movie")
    assert_eq(item["data"], {"title": "Dune", "year": 2021})
    assert_eq(item["intent_context"], "a friend raved")
    assert_eq(body["dedup"]["severity"], "none")


@test()
def post_bucket_items_trims_intent_context() -> None:
    """Intent context is stored trimmed."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        body = add_item(client, intent_context="  saw a trailer  ")

    assert_eq(body["item"]["intent_context"], "saw a trailer")


@test()
def post_bucket_items_rejects_blank_intent_context() -> None:
    """Intent context must be non-empty after trimming."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        login(client)
        response = client.post(
            "/api/bucket-items",
            json={
                "item_type": "movie",
                "data": {"title": "Dune"},
                "intent_context": "   ",
            },
        )

    assert_eq(response.status_code, 422)


@test()
def post_bucket_items_rejects_invalid_payload() -> None:
    """A payload missing its item type's required field is a 422."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        login(client)
        response = client.post(
            "/api/bucket-items",
            json={
                "item_type": "movie",
                "data": {"year": 2021},
                "intent_context": "recommended",
            },
        )

    assert_eq(response.status_code, 422)


@test()
def post_bucket_items_warns_on_active_duplicate() -> None:
    """Re-adding an active duplicate warns but still creates the item."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        first = add_item(client, "movie", {"title": "Dune"})
        second = add_item(client, "movie", {"title": "Dune"})

    assert_eq(second["dedup"]["severity"], "warn")
    assert_eq(second["item"]["state"], "active")
    duplicate_ids = [dup["id"] for dup in second["dedup"]["duplicates"]]
    assert_in(first["item"]["id"], duplicate_ids)


@test()
def post_bucket_items_informs_on_completed_duplicate() -> None:
    """Re-adding a completed duplicate informs but allows."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        first = add_item(client, "movie", {"title": "Dune"})
        complete_response = client.post(
            f"/api/bucket-items/{first['item']['id']}/complete",
            json={"version": first["item"]["version"]},
        )
        assert_eq(complete_response.status_code, 200)

        second = add_item(client, "movie", {"title": "Dune"})

    assert_eq(second["dedup"]["severity"], "inform")


@test()
def complete_bucket_item_moves_it_to_completed() -> None:
    """`POST /api/bucket-items/{id}/complete` moves the item to terminal history."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        added = add_item(client)
        response = client.post(
            f"/api/bucket-items/{added['item']['id']}/complete",
            json={"version": added["item"]["version"]},
        )

    assert_eq(response.status_code, 200)
    assert_eq(response.json()["state"], "completed")


@test()
def complete_bucket_item_conflicts_when_already_terminal() -> None:
    """Completing an already-terminal item is a 409."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        added = add_item(client)
        first = client.post(
            f"/api/bucket-items/{added['item']['id']}/complete",
            json={"version": added["item"]["version"]},
        )
        assert_eq(first.status_code, 200)

        second = client.post(
            f"/api/bucket-items/{added['item']['id']}/complete",
            json={"version": added["item"]["version"]},
        )

    assert_eq(second.status_code, 409)


@test()
def delete_bucket_item_moves_it_to_deleted() -> None:
    """`DELETE /api/bucket-items/{id}` uses the version query param."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        added = add_item(client)
        response = client.delete(
            f"/api/bucket-items/{added['item']['id']}",
            params={"version": added["item"]["version"]},
        )

    assert_eq(response.status_code, 200)
    assert_eq(response.json()["state"], "deleted")


@test()
def search_returns_only_active_matches() -> None:
    """`GET /api/bucket-items/search` returns active items matching the query."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        active = add_item(client, "movie", {"title": "Blade Runner"})
        done = add_item(client, "movie", {"title": "Blade of Glory"})
        client.post(
            f"/api/bucket-items/{done['item']['id']}/complete",
            json={"version": done["item"]["version"]},
        )

        response = client.get("/api/bucket-items/search", params={"q": "Blade"})

    found = [item["id"] for item in response.json()]
    assert_in(active["item"]["id"], found)
    assert_not_in(done["item"]["id"], found)


@test()
def browse_active_excludes_terminal_items() -> None:
    """`GET /api/bucket-items?state=active` lists only active items."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        active = add_item(client, "movie", {"title": "Active One"})
        gone = add_item(client, "movie", {"title": "Gone One"})
        client.delete(
            f"/api/bucket-items/{gone['item']['id']}",
            params={"version": gone["item"]["version"]},
        )

        response = client.get("/api/bucket-items", params={"state": "active"})

    found = [item["id"] for item in response.json()]
    assert_in(active["item"]["id"], found)
    assert_not_in(gone["item"]["id"], found)


@test()
def browse_deleted_returns_retained_history() -> None:
    """`GET /api/bucket-items?state=deleted` surfaces retained deleted history."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        gone = add_item(client, "movie", {"title": "Dismissed"})
        client.delete(
            f"/api/bucket-items/{gone['item']['id']}",
            params={"version": gone["item"]["version"]},
        )

        response = client.get("/api/bucket-items", params={"state": "deleted"})

    found = [item["id"] for item in response.json()]
    assert_in(gone["item"]["id"], found)
