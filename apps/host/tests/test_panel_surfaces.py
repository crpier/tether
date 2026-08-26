"""Dual-surface behaviour tests for Synthetic panels.

One app, both shells: the REST routes assert request parsing, status codes,
and response serialisation (including the `/results` execution subresource);
the `/internal/tools/*` endpoints assert the uniform envelope. Both derive
from `tether.panel_capabilities`.
"""

import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast

from snekql.sqlite import Database, insert
from snektest import assert_eq, test
from starlette.applications import Starlette

from tests.surfaces import call_tool, login, surface_client
from tether.app_runtime import app_runtime
from tether.dreaming_store import DreamingWorkspaceFile


async def seed_topic(database: Database, path: str, content: str) -> None:
    """Record one acknowledged Dreaming Topic for the surface fixture."""
    async with database.transaction() as transaction:
        _ = await transaction.execute(
            insert(
                DreamingWorkspaceFile(
                    path=path,
                    content_hash=hashlib.sha256(content.encode()).hexdigest(),
                    content=content,
                    is_tombstone=0,
                    version=1,
                    actor="dream",
                )
            )
        )


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
def rest_results_recompute_over_current_topics() -> None:
    """`GET /results` reflects a Topic Dreaming added after panel creation."""
    with TemporaryDirectory() as root, surface_client(Path(root)) as client:
        login(client)
        panel = client.post("/api/panels", json=finance_panel_body()).json()

        empty = client.get(f"/api/panels/{panel['id']}/results")
        assert_eq(empty.status_code, 200)
        assert_eq(empty.json(), {"topics": [], "total": 0})

        content = "---\ntitle: Finances\ndomain: finance\n---\nRent is 900.\n"
        topic_path = Path(root) / ".tether" / "memory" / "finance.md"
        topic_path.parent.mkdir(parents=True, exist_ok=True)
        topic_path.write_text(content, encoding="utf-8")
        portal = client.portal
        assert portal is not None
        portal.call(
            seed_topic,
            app_runtime(cast("Starlette", client.app)).dreaming_service.database,
            "finance.md",
            content,
        )

        results = client.get(f"/api/panels/{panel['id']}/results")
        assert_eq(results.json()["total"], 1)
        assert_eq(results.json()["topics"][0]["path"], "finance.md")


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
