"""HTTP-level behaviour tests for `GET /api/search`, the fused cross-source
Search route.

`test_search_fusion_service.py` and `test_search_fusion.py` cover the fusion
engine and the `SearchFusionService`/capability-execute seam directly; this
module is the one place that drives an actual HTTP request through
`tether.search_routes.SearchQuery` — proving the query string itself parses
into the right capability call, in particular `sources` (the one query
parameter with no scalar shape: it rides the query string as a
comma-separated list, per `SearchQuery._split_comma_separated`) and
`after`/`before` (`AwareDatetime` from an ISO 8601 query value).
"""

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from snektest import assert_eq, test
from starlette.testclient import TestClient

from tests.surfaces import login, surface_client
from tether.embeddings import FakeEmbedder


def make_client(root: Path) -> Any:
    """A dual-surface app with a `FakeEmbedder` so fused Search runs offline."""
    return surface_client(root, embedder=FakeEmbedder())


def rest_capture(client: TestClient, content: str) -> dict[str, Any]:
    """Capture and tether one Memory through REST; return its JSON."""
    login(client)
    captured = client.post("/api/memories", json={"content": content}).json()
    tethered = client.post(
        f"/api/memories/{captured['id']}/tether",
        json={"version": captured["version"]},
    )
    return tethered.json()


def rest_add_item(client: TestClient, title: str) -> dict[str, Any]:
    """Add one active Bucket item through REST; return its JSON."""
    login(client)
    response = client.post(
        "/api/bucket-items",
        json={
            "item_type": "movie",
            "data": {"title": title},
            "intent_context": "saved on a whim",
        },
    )
    return response.json()["item"]


@test()
def get_search_fuses_memory_and_bucket_item_hits() -> None:
    """`GET /api/search?q=...` returns both a matching tethered Memory and a
    matching active Bucket item, each tagged with its own `source`."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        memory = rest_capture(client, "aisle seat preference for flights")
        item = rest_add_item(client, "Aisle Seat Confidential")

        response = client.get("/api/search", params={"q": "aisle seat"})

    assert_eq(response.status_code, 200)
    hits = response.json()
    sources_by_id = {
        (hit["memory"] or hit["bucket_item"])["id"]: hit["source"] for hit in hits
    }
    assert_eq(sources_by_id.get(memory["id"]), "memory")
    assert_eq(sources_by_id.get(item["id"]), "bucket_item")


@test()
def get_search_sources_param_restricts_fusion_to_the_named_arms() -> None:
    """`sources=memory` (a comma-separated query value) skips the Bucket-item
    arm entirely, even though it has a matching item."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        memory = rest_capture(client, "aisle seat preference for flights")
        item = rest_add_item(client, "Aisle Seat Confidential")

        response = client.get(
            "/api/search", params={"q": "aisle seat", "sources": "memory"}
        )

    hits = response.json()
    found_ids = {(hit["memory"] or hit["bucket_item"])["id"] for hit in hits}
    assert_eq(response.status_code, 200)
    assert_eq(memory["id"] in found_ids, True)
    assert_eq(item["id"] in found_ids, False)


@test()
def get_search_rejects_a_backwards_time_window() -> None:
    """`after` later than `before` is a 400, not a silently-empty result."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        login(client)
        response = client.get(
            "/api/search",
            params={
                "q": "anything",
                "after": "2026-02-01T00:00:00Z",
                "before": "2026-01-01T00:00:00Z",
            },
        )

    assert_eq(response.status_code, 400)


@test()
def get_search_rejects_a_blank_query() -> None:
    """An empty `q` is a 400, mirroring the per-source Search routes."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        login(client)
        response = client.get("/api/search", params={"q": "   "})

    assert_eq(response.status_code, 400)
