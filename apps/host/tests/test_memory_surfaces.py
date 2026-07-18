"""Dual-surface behaviour tests for the Memory Review spine.

One app, both shells: the REST routes assert request parsing, status codes,
and response serialisation; the `/internal/tools/*` endpoints assert the auth
gate and the uniform envelope. Both derive from `tether.memory_capabilities`,
so the service behaviour itself (capture → tether → search invariants) is
exercised once through whichever shell states it most directly.
"""

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from snektest import assert_eq, assert_in, assert_is_none, assert_not_in, test
from starlette.testclient import TestClient

from tests.surfaces import SESSION, call_tool, login, surface_client
from tether.embeddings import FakeEmbedder


def make_client(root: Path) -> Any:
    """A dual-surface app with a `FakeEmbedder` so hybrid search runs offline."""
    return surface_client(root, embedder=FakeEmbedder())


def rest_capture(client: TestClient, content: str) -> dict[str, Any]:
    """Capture one Memory through REST and return its JSON representation."""
    login(client)
    response = client.post("/api/memories", json={"content": content})
    assert_eq(response.status_code, 201)
    return response.json()


def tool_capture(client: TestClient, content: str) -> dict[str, Any]:
    """Capture one Memory through the tool seam and return its result payload."""
    envelope = call_tool(client, "capture", content=content)
    assert_eq(envelope["success"], True)
    return envelope["result"]


@test()
def post_memories_captures_trimmed_content() -> None:
    """`POST /api/memories` accepts `content` and returns a loose Memory."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        memory = rest_capture(client, "  I prefer aisle seats  ")

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
def patch_memories_edits_content_and_keeps_version_checks() -> None:
    """`PATCH /api/memories/{id}` edits `content` at the observed version."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        memory = rest_capture(client, "I live in Berlin")
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
        memory = rest_capture(client, "needle deleted memory")
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
def rest_tethering_an_absent_memory_is_404() -> None:
    """REST absence surfaces as a 404 with the domain's fixed detail."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        login(client)
        response = client.post(
            "/api/memories/018f0000-0000-7000-8000-000000000000/tether",
            json={"version": 1},
        )

    assert_eq(response.status_code, 404)
    assert_eq(response.json(), {"detail": "memory not found"})


@test()
def rest_re_tethering_is_a_409_conflict() -> None:
    """The same domain conflict that envelopes as `conflict` is a REST 409."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        memory = rest_capture(client, "I prefer window seats")
        first = client.post(
            f"/api/memories/{memory['id']}/tether",
            json={"version": memory["version"]},
        )
        again = client.post(
            f"/api/memories/{memory['id']}/tether",
            json={"version": memory["version"]},
        )

    assert_eq(first.status_code, 200)
    assert_eq(again.status_code, 409)


@test()
def call_without_secret_is_rejected() -> None:
    """A call lacking the per-process secret never reaches a tool."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        response = client.post(
            "/internal/tools/capture",
            json={"session_id": SESSION, "content": "I prefer aisle seats"},
        )

    assert_eq(response.status_code, 401)


@test()
def call_with_wrong_secret_is_rejected() -> None:
    """A mismatched secret is rejected the same as a missing one."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        response = client.post(
            "/internal/tools/capture",
            json={"session_id": SESSION, "content": "x"},
            headers={"X-Tether-Tool-Secret": "wrong"},
        )

    assert_eq(response.status_code, 401)


@test()
def call_with_unknown_session_is_rejected() -> None:
    """Identity must resolve to a registered session id."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        response = client.post(
            "/internal/tools/capture",
            json={"session_id": "ghost", "content": "x"},
            headers={"X-Tether-Tool-Secret": "test-process-secret"},
        )

    assert_eq(response.status_code, 401)


@test()
def capture_returns_a_well_formed_success_envelope() -> None:
    """A successful capture conforms to the envelope; quota is null internally."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        envelope = call_tool(client, "capture", content="  I prefer aisle seats  ")

    assert_eq(envelope["success"], True)
    assert_eq(envelope["result"]["content"], "I prefer aisle seats")
    assert_eq(envelope["result"]["state"], "loose")
    assert_is_none(envelope["error"])
    assert_eq(envelope["provenance"], {"kind": "manual"})
    assert_is_none(envelope["quota"])


@test()
def malformed_input_yields_a_success_false_envelope_without_state() -> None:
    """Blank content is rejected as a well-formed envelope, capturing nothing."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        envelope = call_tool(client, "capture", content="   ")

        assert_eq(envelope["success"], False)
        assert_eq(envelope["error"]["code"], "invalid_input")
        assert_is_none(envelope["result"])

        queue = call_tool(client, "browse", state="loose")

    assert_eq(queue["result"], [])


@test()
def malformed_memory_id_yields_a_success_false_envelope() -> None:
    """A non-UUID memory id is dumb input, not a destructive action."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        envelope = call_tool(client, "tether", memory_id="not-a-uuid", version=1)

    assert_eq(envelope["success"], False)
    assert_eq(envelope["error"]["code"], "invalid_input")


@test()
def capture_tether_search_invariant_holds_at_the_tool_seam() -> None:
    """Loose Memories are unsearchable; tethering makes them searchable."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        memory = tool_capture(client, "needle in the corpus")

        before = call_tool(client, "search", q="needle")
        assert_eq(before["result"], [])

        tethered = call_tool(
            client, "tether", memory_id=memory["id"], version=memory["version"]
        )
        assert_eq(tethered["success"], True)
        assert_eq(tethered["result"]["state"], "tethered")

        after = call_tool(client, "search", q="needle")

    found = [hit["id"] for hit in after["result"]]
    assert_in(memory["id"], found)
    assert_is_none(after["provenance"])


@test()
def all_six_tools_are_invokable() -> None:
    """capture, browse, search, tether, edit, reject all reach MemoryService."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        memory = tool_capture(client, "I live in Berlin")

        edited = call_tool(
            client,
            "edit",
            memory_id=memory["id"],
            content="I live in Munich",
            version=memory["version"],
        )
        assert_eq(edited["result"]["content"], "I live in Munich")

        tethered = call_tool(
            client,
            "tether",
            memory_id=memory["id"],
            version=edited["result"]["version"],
        )
        assert_eq(tethered["result"]["state"], "tethered")

        browsed = call_tool(client, "browse", state="tethered")
        assert_in(memory["id"], [m["id"] for m in browsed["result"]])

        rejected = call_tool(
            client,
            "reject",
            memory_id=memory["id"],
            version=tethered["result"]["version"],
        )
        assert_eq(rejected["success"], True)

        gone = call_tool(client, "search", q="Munich")
        assert_eq(gone["result"], [])


@test()
def tethering_an_absent_memory_is_a_not_found_envelope() -> None:
    """A live-but-absent target surfaces as `not_found`, never a crash."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        envelope = call_tool(
            client,
            "tether",
            memory_id="018f0000-0000-7000-8000-000000000000",
            version=1,
        )

    assert_eq(envelope["success"], False)
    assert_eq(envelope["error"]["code"], "not_found")


@test()
def re_tethering_is_a_conflict_envelope() -> None:
    """Tethering an already-tethered Memory is a domain conflict, enveloped."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        memory = tool_capture(client, "I prefer window seats")
        first = call_tool(
            client, "tether", memory_id=memory["id"], version=memory["version"]
        )

        again = call_tool(
            client, "tether", memory_id=memory["id"], version=memory["version"]
        )

    assert_eq(first["success"], True)
    assert_eq(again["success"], False)
    assert_eq(again["error"]["code"], "conflict")


@test()
def internal_surface_is_absent_from_the_public_openapi() -> None:
    """The tool surface is not described by the public OpenAPI document."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        document = client.get("/openapi.json").json()

    tool_paths = [path for path in document["paths"] if path.startswith("/internal")]
    assert_not_in("/internal/tools/capture", document["paths"])
    assert_eq(tool_paths, [])


@test()
def capture_tool_persists_supplied_facets() -> None:
    """`capture(facets=...)` at the tool seam persists the facet set verbatim."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        envelope = call_tool(
            client,
            "capture",
            content="I prefer aisle seats",
            facets={"topic": "travel"},
        )

    assert_eq(envelope["success"], True)
    assert_eq(envelope["result"]["facets"], {"topic": "travel"})


@test()
def edit_tool_facets_round_trip_and_omission_leaves_unchanged() -> None:
    """`edit(facets=...)` replaces facets; omitting it leaves them unchanged."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        memory = tool_capture(client, "I live in Berlin")
        first_edit = call_tool(
            client,
            "edit",
            memory_id=memory["id"],
            content="I live in Berlin",
            version=memory["version"],
            facets={"topic": "housing"},
        )
        second_edit = call_tool(
            client,
            "edit",
            memory_id=memory["id"],
            content="I live in Munich",
            version=first_edit["result"]["version"],
        )

    assert_eq(first_edit["result"]["facets"], {"topic": "housing"})
    assert_eq(second_edit["result"]["facets"], {"topic": "housing"})


@test()
def search_tool_filters_by_facets() -> None:
    """`search(facets=...)` at the tool seam keeps only exact-matching Memories."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        travel = tool_capture(client, "needle travel note")
        _ = call_tool(
            client,
            "edit",
            memory_id=travel["id"],
            content="needle travel note",
            version=travel["version"],
            facets={"topic": "travel"},
        )
        edited_travel = call_tool(client, "browse", state="loose")["result"][0]
        tethered_travel = call_tool(
            client, "tether", memory_id=travel["id"], version=edited_travel["version"]
        )["result"]

        other = tool_capture(client, "needle shopping note")
        tethered_other = call_tool(
            client, "tether", memory_id=other["id"], version=other["version"]
        )["result"]

        filtered = call_tool(client, "search", q="needle", facets={"topic": "travel"})

    found = [hit["id"] for hit in filtered["result"]]
    assert_in(tethered_travel["id"], found)
    assert_not_in(tethered_other["id"], found)


@test()
def facet_overview_tool_reports_counts() -> None:
    """`facet_overview` reports distinct keys/values and counts through the tool seam."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        _ = call_tool(client, "capture", content="first", facets={"topic": "travel"})
        _ = call_tool(client, "capture", content="second", facets={"topic": "travel"})

        envelope = call_tool(client, "facet_overview")

    entries = {
        (entry["key"], entry["value"]): entry["count"] for entry in envelope["result"]
    }
    assert_eq(entries[("topic", "travel")], 2)


@test()
def rename_facet_key_tool_renames_across_the_corpus() -> None:
    """`rename_facet_key` at the tool seam renames the key on every carrier."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        memory = tool_capture(client, "a fact")
        _ = call_tool(
            client,
            "edit",
            memory_id=memory["id"],
            content="a fact",
            version=memory["version"],
            facets={"topik": "travel"},
        )

        envelope = call_tool(
            client, "rename_facet_key", old_key="topik", new_key="topic"
        )

        browsed = call_tool(client, "browse", state="loose")

    assert_eq(envelope["result"], {"changed_count": 1})
    assert_eq(browsed["result"][0]["facets"], {"topic": "travel"})


@test()
def merge_facet_value_tool_merges_across_the_corpus() -> None:
    """`merge_facet_value` at the tool seam rewrites the value on every carrier."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        memory = tool_capture(client, "a fact")
        _ = call_tool(
            client,
            "edit",
            memory_id=memory["id"],
            content="a fact",
            version=memory["version"],
            facets={"topic": "travle"},
        )

        envelope = call_tool(
            client,
            "merge_facet_value",
            key="topic",
            old_value="travle",
            new_value="travel",
        )

        browsed = call_tool(client, "browse", state="loose")

    assert_eq(envelope["result"], {"changed_count": 1})
    assert_eq(browsed["result"][0]["facets"], {"topic": "travel"})


@test()
def configured_database_path_persists_between_app_instances() -> None:
    """A configured SQLite path survives app shutdown and restart."""
    with TemporaryDirectory() as directory:
        root = Path(directory)
        with make_client(root) as client:
            memory = rest_capture(client, "I prefer aisle seats")
        with make_client(root) as client:
            login(client)
            response = client.get("/api/memories", params={"state": "loose"})

        found = [memory["id"] for memory in response.json()]
        assert_in(memory["id"], found)
        assert_eq((root / "tether.sqlite3").exists(), True)
