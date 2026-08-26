"""Dual-surface behaviour tests for Artifacts.

One app, both shells: `create_artifact`/`update_artifact`/`list_artifact_events`
are tool-only (no REST create/update — see `tether.artifact_routes`), so
mutations drive through the `/internal/tools/*` seam while reads and the
events relay drive through REST, per the ticket's testing decisions
(versioning increments, latest/by-version reads, event append-only + scoping,
size-cap rejection, tool results carrying no `html`).
"""

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from snektest import assert_eq, assert_not_in, test
from starlette.testclient import TestClient

from tests.surfaces import call_tool, login, surface_client

make_client = surface_client


def create_artifact(
    client: TestClient, title: str = "Quiz", html: str = "<html>hi</html>"
) -> dict[str, Any]:
    """Create one artifact through the tool seam, returning the pointer envelope."""
    envelope = call_tool(client, "create_artifact", title=title, html=html)
    assert_eq(envelope["success"], True)
    return envelope


@test()
async def create_artifact_returns_a_pointer_with_no_html() -> None:
    """`create_artifact`'s result is a small pointer — no `html`, no `title`."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        envelope = create_artifact(client)

    assert_eq(set(envelope["result"].keys()), {"id", "version"})
    assert_eq(envelope["result"]["version"], 1)


@test()
async def created_artifact_is_readable_via_rest_latest() -> None:
    """A tool-created artifact is fetchable by id through `GET /api/artifacts/{id}`."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        login(client)
        pointer = create_artifact(client, "Quiz", "<html>one</html>")["result"]

        response = client.get(f"/api/artifacts/{pointer['id']}")

    assert_eq(response.status_code, 200)
    body = response.json()
    assert_eq(body["id"], pointer["id"])
    assert_eq(body["title"], "Quiz")
    assert_eq(body["html"], "<html>one</html>")
    assert_eq(body["version"], 1)


@test()
async def update_artifact_appends_a_new_version_carrying_the_title_forward() -> None:
    """`update_artifact` increments `version` by 1 and keeps the old version fetchable."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        login(client)
        pointer = create_artifact(client, "Quiz", "<html>one</html>")["result"]

        update_envelope = call_tool(
            client, "update_artifact", id=pointer["id"], html="<html>two</html>"
        )
        assert_eq(update_envelope["success"], True)
        assert_eq(set(update_envelope["result"].keys()), {"id", "version"})
        assert_eq(update_envelope["result"]["version"], 2)

        latest = client.get(f"/api/artifacts/{pointer['id']}")
        first_version = client.get(f"/api/artifacts/{pointer['id']}/versions/1")
        second_version = client.get(f"/api/artifacts/{pointer['id']}/versions/2")

    assert_eq(latest.json()["html"], "<html>two</html>")
    assert_eq(latest.json()["version"], 2)
    assert_eq(latest.json()["title"], "Quiz")
    assert_eq(first_version.json()["html"], "<html>one</html>")
    assert_eq(second_version.json()["html"], "<html>two</html>")


@test()
async def list_artifacts_surfaces_the_latest_version_without_html() -> None:
    """`GET /api/artifacts` lists the newest version per artifact, no `html`."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        login(client)
        pointer = create_artifact(client, "Quiz", "<html>one</html>")["result"]
        call_tool(client, "update_artifact", id=pointer["id"], html="<html>two</html>")

        response = client.get("/api/artifacts")

    assert_eq(response.status_code, 200)
    entries = response.json()
    assert_eq(len(entries), 1)
    assert_eq(entries[0]["id"], pointer["id"])
    assert_eq(entries[0]["version"], 2)
    assert_not_in("html", entries[0])


@test()
async def posted_events_are_relayed_and_listed_oldest_first() -> None:
    """`POST .../events` records an event; `GET .../events` lists it, append-only."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        login(client)
        pointer = create_artifact(client)["result"]

        first = client.post(
            f"/api/artifacts/{pointer['id']}/events",
            json={"payload": {"type": "answer", "value": 1}},
        )
        second = client.post(
            f"/api/artifacts/{pointer['id']}/events",
            json={"payload": {"type": "answer", "value": 2}},
        )
        listed = client.get(f"/api/artifacts/{pointer['id']}/events")

    assert_eq(first.status_code, 201)
    assert_eq(second.status_code, 201)
    assert_eq(listed.status_code, 200)
    events = listed.json()
    assert_eq(len(events), 2)
    assert_eq(events[0]["payload"], {"type": "answer", "value": 1})
    assert_eq(events[1]["payload"], {"type": "answer", "value": 2})


@test()
async def events_are_scoped_to_their_own_artifact() -> None:
    """Events for one artifact never leak into another artifact's event list."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        login(client)
        first_artifact = create_artifact(client, "A")["result"]
        second_artifact = create_artifact(client, "B")["result"]

        client.post(
            f"/api/artifacts/{first_artifact['id']}/events",
            json={"payload": {"who": "first"}},
        )

        first_events = client.get(f"/api/artifacts/{first_artifact['id']}/events")
        second_events = client.get(f"/api/artifacts/{second_artifact['id']}/events")

    assert_eq(len(first_events.json()), 1)
    assert_eq(second_events.json(), [])


@test()
async def list_artifact_events_tool_matches_the_rest_view() -> None:
    """`list_artifact_events` (tool) surfaces the same events as the REST route."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        login(client)
        pointer = create_artifact(client)["result"]
        client.post(
            f"/api/artifacts/{pointer['id']}/events",
            json={"payload": {"type": "done"}},
        )

        envelope = call_tool(client, "list_artifact_events", artifact_id=pointer["id"])

    assert_eq(envelope["success"], True)
    assert_eq(len(envelope["result"]), 1)
    assert_eq(envelope["result"][0]["payload"], {"type": "done"})


@test()
async def create_artifact_rejects_oversized_html() -> None:
    """`create_artifact` fails with a typed error when `html` exceeds the size cap."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        oversized_html = "a" * (1_000_000 + 1)
        envelope = call_tool(
            client, "create_artifact", title="Big", html=oversized_html
        )

    assert_eq(envelope["success"], False)
    assert_eq(envelope["error"]["code"], "invalid_input")


@test()
async def update_artifact_rejects_oversized_html() -> None:
    """`update_artifact` fails with a typed error when `html` exceeds the size cap."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        pointer = create_artifact(client)["result"]
        oversized_html = "a" * (1_000_000 + 1)

        envelope = call_tool(
            client, "update_artifact", id=pointer["id"], html=oversized_html
        )

    assert_eq(envelope["success"], False)
    assert_eq(envelope["error"]["code"], "invalid_input")


@test()
async def unknown_artifact_id_404s_on_every_read_and_write_route() -> None:
    """Absence maps to 404 across latest, version, events list, and events post."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        login(client)
        unknown_id = "018f0000-0000-7000-8000-000000000000"

        latest = client.get(f"/api/artifacts/{unknown_id}")
        version = client.get(f"/api/artifacts/{unknown_id}/versions/1")
        events = client.get(f"/api/artifacts/{unknown_id}/events")
        post_event = client.post(
            f"/api/artifacts/{unknown_id}/events", json={"payload": {}}
        )

    assert_eq(latest.status_code, 404)
    assert_eq(version.status_code, 404)
    assert_eq(events.status_code, 404)
    assert_eq(post_event.status_code, 404)


@test()
async def unknown_artifact_id_404s_on_the_tool_seam_too() -> None:
    """`update_artifact` and `list_artifact_events` 404 (as `not_found`) on an
    unknown artifact id, matching the REST mapping."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        unknown_id = "018f0000-0000-7000-8000-000000000000"

        update_envelope = call_tool(
            client, "update_artifact", id=unknown_id, html="<html></html>"
        )
        events_envelope = call_tool(
            client, "list_artifact_events", artifact_id=unknown_id
        )

    assert_eq(update_envelope["success"], False)
    assert_eq(update_envelope["error"]["code"], "not_found")
    assert_eq(events_envelope["success"], False)
    assert_eq(events_envelope["error"]["code"], "not_found")


@test()
async def requesting_an_unknown_version_of_a_known_artifact_404s() -> None:
    """A known artifact id with a version that was never written still 404s."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        login(client)
        pointer = create_artifact(client)["result"]

        response = client.get(f"/api/artifacts/{pointer['id']}/versions/99")

    assert_eq(response.status_code, 404)
