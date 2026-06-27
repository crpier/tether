"""Behaviour tests for the loopback internal Bucket item tool surface.

These drive the mounted Starlette app through `TestClient`, calling the
`/internal/tools/*` Bucket item endpoints directly — no LLM, no pi. They share
the auth gate and uniform envelope with the Memory tools; here we assert the
Bucket-item-specific behaviour: per-type Add, dedup advisory, complete/delete,
and active-only Search.
"""

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast

from snektest import assert_eq, assert_in, assert_is_none, assert_not_in, test
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


@test()
def add_movie_returns_a_well_formed_success_envelope() -> None:
    """A successful Add conforms to the envelope; quota is null internally."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        envelope = call(
            client,
            "add_movie",
            title="Dune",
            year=2021,
            intent_context="a friend raved",
        )

    assert_eq(envelope["success"], True)
    item = envelope["result"]["item"]
    assert_eq(item["item_type"], "movie")
    assert_eq(item["state"], "active")
    assert_eq(item["data"], {"title": "Dune", "year": 2021})
    assert_eq(item["intent_context"], "a friend raved")
    assert_eq(envelope["result"]["dedup"]["severity"], "none")
    assert_eq(envelope["provenance"], {"kind": "manual"})
    assert_is_none(envelope["quota"])


@test()
def add_movie_without_year_stores_a_null_year() -> None:
    """Year is optional at the tool seam; the stored payload is the full type."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        envelope = call(client, "add_movie", title="Arrival", intent_context="sci-fi")

    assert_eq(envelope["result"]["item"]["data"], {"title": "Arrival", "year": None})


@test()
def add_place_carries_its_own_fields() -> None:
    """A place Add carries differently-shaped payload fields than a movie."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        envelope = call(
            client,
            "add_place",
            name="Lisbon",
            location="Portugal",
            intent_context="want to visit",
        )

    item = envelope["result"]["item"]
    assert_eq(item["item_type"], "place")
    assert_eq(item["data"], {"name": "Lisbon", "location": "Portugal"})


@test()
def add_blank_intent_yields_a_success_false_envelope() -> None:
    """Blank intent context is rejected as a well-formed envelope, adding nothing."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        envelope = call(client, "add_movie", title="Dune", intent_context="   ")

        assert_eq(envelope["success"], False)
        assert_eq(envelope["error"]["code"], "invalid_input")
        assert_is_none(envelope["result"])

        found = call(client, "search_bucket_items", q="Dune")

    assert_eq(found["result"], [])


@test()
def re_adding_an_active_item_warns() -> None:
    """The dedup advisory warns on an active duplicate at the tool seam."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        _ = call(client, "add_movie", title="Dune", intent_context="first")
        envelope = call(client, "add_movie", title="Dune", intent_context="again")

    assert_eq(envelope["result"]["dedup"]["severity"], "warn")


@test()
def completing_a_bucket_item_moves_it_to_terminal_history() -> None:
    """Complete moves an active item to terminal history via the tool seam."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        added = call(client, "add_movie", title="Dune", intent_context="watch")
        item = added["result"]["item"]

        envelope = call(
            client,
            "complete_bucket_item",
            bucket_item_id=item["id"],
            version=item["version"],
        )

    assert_eq(envelope["success"], True)
    assert_eq(envelope["result"]["state"], "completed")


@test()
def completing_a_terminal_item_yields_a_conflict_envelope() -> None:
    """A second complete is a well-formed conflict envelope."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        added = call(client, "add_movie", title="Dune", intent_context="watch")
        item = added["result"]["item"]
        _ = call(
            client,
            "complete_bucket_item",
            bucket_item_id=item["id"],
            version=item["version"],
        )

        envelope = call(
            client,
            "complete_bucket_item",
            bucket_item_id=item["id"],
            version=item["version"],
        )

    assert_eq(envelope["success"], False)
    assert_eq(envelope["error"]["code"], "conflict")


@test()
def search_returns_only_active_items() -> None:
    """Search over the tool seam returns active items, excluding terminal ones."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        active = call(client, "add_movie", title="Blade Runner", intent_context="a")
        done = call(client, "add_movie", title="Blade of Glory", intent_context="b")
        done_item = done["result"]["item"]
        _ = call(
            client,
            "delete_bucket_item",
            bucket_item_id=done_item["id"],
            version=done_item["version"],
        )

        found = call(client, "search_bucket_items", q="Blade")

    found_ids = [item["id"] for item in found["result"]]
    assert_in(active["result"]["item"]["id"], found_ids)
    assert_not_in(done_item["id"], found_ids)
