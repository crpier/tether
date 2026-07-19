"""Dual-surface behaviour tests for Synthetic panels.

One app, both shells: the REST routes assert request parsing, status codes,
and response serialisation (including the `/results` execution subresource);
the `/internal/tools/*` endpoints assert the uniform envelope. Both derive
from `tether.panel_capabilities`.
"""

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from snektest import assert_eq, test

from tests.surfaces import call_tool, login, surface_client


def finance_panel_body(**overrides: Any) -> dict[str, Any]:
    """A minimal valid facets-only panel create body."""
    body: dict[str, Any] = {"name": "finance", "facets": {"domain": "finance"}}
    body.update(overrides)
    return body


@test()
def rest_create_list_update_delete_roundtrip() -> None:
    """The REST surface persists a panel through its full CRUD lifecycle."""
    with TemporaryDirectory() as root, surface_client(Path(root)) as client:
        login(client)

        created = client.post("/api/panels", json=finance_panel_body())
        assert_eq(created.status_code, 201)
        panel = created.json()
        assert_eq(panel["name"], "finance")
        assert_eq(panel["render_kind"], "table")

        listed = client.get("/api/panels")
        assert_eq(listed.status_code, 200)
        assert_eq([entry["id"] for entry in listed.json()], [panel["id"]])

        updated = client.put(
            f"/api/panels/{panel['id']}",
            json=finance_panel_body(name="money", version=panel["version"]),
        )
        assert_eq(updated.status_code, 200)
        assert_eq(updated.json()["name"], "money")

        deleted = client.delete(
            f"/api/panels/{panel['id']}",
            params={"version": updated.json()["version"]},
        )
        assert_eq(deleted.status_code, 200)
        assert_eq(client.get("/api/panels").json(), [])


@test()
def rest_rejects_a_malformed_spec_as_422() -> None:
    """An unscoped panel translates through PANEL_ERRORS to invalid_input."""
    with TemporaryDirectory() as root, surface_client(Path(root)) as client:
        login(client)

        response = client.post("/api/panels", json={"name": "everything", "facets": {}})

        assert_eq(response.status_code, 422)


@test()
def rest_conflicts_on_a_stale_version() -> None:
    """A stale observed version surfaces as 409 on the REST shell."""
    with TemporaryDirectory() as root, surface_client(Path(root)) as client:
        login(client)
        panel = client.post("/api/panels", json=finance_panel_body()).json()
        _ = client.put(
            f"/api/panels/{panel['id']}",
            json=finance_panel_body(name="renamed", version=panel["version"]),
        )

        stale = client.put(
            f"/api/panels/{panel['id']}",
            json=finance_panel_body(name="again", version=panel["version"]),
        )

        assert_eq(stale.status_code, 409)


@test()
def rest_results_recompute_over_the_live_corpus() -> None:
    """`GET /results` reflects a Memory tethered after the panel was saved."""
    with TemporaryDirectory() as root, surface_client(Path(root)) as client:
        login(client)
        panel = client.post("/api/panels", json=finance_panel_body()).json()

        empty = client.get(f"/api/panels/{panel['id']}/results")
        assert_eq(empty.status_code, 200)
        assert_eq(empty.json(), {"memories": [], "total": 0})

        memory = client.post(
            "/api/memories",
            json={"content": "rent is 900"},
        ).json()
        edited = client.patch(
            f"/api/memories/{memory['id']}",
            json={"content": "rent is 900", "version": memory["version"]},
        )
        assert_eq(edited.status_code, 200)

        # Facet + tether through the tool surface (facets ride capture/edit).
        _ = call_tool(
            client,
            "edit",
            memory_id=memory["id"],
            content="rent is 900",
            facets={"domain": "finance"},
            version=edited.json()["version"],
        )
        refreshed = client.get("/api/memories", params={"state": "loose"}).json()
        tethered = client.post(
            f"/api/memories/{memory['id']}/tether",
            json={"version": refreshed[0]["version"]},
        )
        assert_eq(tethered.status_code, 200)

        results = client.get(f"/api/panels/{panel['id']}/results")
        assert_eq(results.json()["total"], 1)
        assert_eq(results.json()["memories"][0]["id"], memory["id"])


@test()
def tool_surface_creates_lists_and_deletes_panels() -> None:
    """The internal tool shell drives the same capabilities via envelopes."""
    with TemporaryDirectory() as root, surface_client(Path(root)) as client:
        login(client)

        created = call_tool(
            client,
            "create_panel",
            name="gifts",
            facets={},
            query="gift ideas",
        )
        assert_eq(created["success"], True)
        panel = created["result"]
        assert_eq(panel["query"], "gift ideas")

        listed = call_tool(client, "list_panels")
        assert_eq(listed["success"], True)
        assert_eq([entry["id"] for entry in listed["result"]], [panel["id"]])

        updated = call_tool(
            client,
            "update_panel",
            panel_id=panel["id"],
            name="gift ideas",
            facets={},
            query="gift",
            version=panel["version"],
        )
        assert_eq(updated["success"], True)

        deleted = call_tool(
            client,
            "delete_panel",
            panel_id=panel["id"],
            version=updated["result"]["version"],
        )
        assert_eq(deleted["success"], True)
        assert_eq(call_tool(client, "list_panels")["result"], [])


@test()
def tool_surface_rejects_a_malformed_spec_as_invalid_input() -> None:
    """A malformed spec comes back as a well-formed error envelope."""
    with TemporaryDirectory() as root, surface_client(Path(root)) as client:
        login(client)

        envelope = call_tool(client, "create_panel", name="bad", facets={})

        assert_eq(envelope["success"], False)
        assert_eq(envelope["error"]["code"], "invalid_input")
