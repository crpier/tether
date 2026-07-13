"""Dual-surface behaviour tests for Bucket items.

One app, both shells: the REST routes assert request parsing, status codes,
and response serialisation; the `/internal/tools/*` endpoints assert the
per-type Add spellings and the uniform envelope. Both derive from
`tether.bucket_capabilities`, so shared service behaviour (dedup advisory,
active-only search, terminal history) is exercised once through whichever
shell states it most directly.
"""

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from snektest import assert_eq, assert_in, assert_is_none, assert_not_in, test
from starlette.testclient import TestClient

from tests.surfaces import call_tool, login, surface_client

make_client = surface_client


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


@test()
def add_movie_returns_a_well_formed_success_envelope() -> None:
    """A successful Add conforms to the envelope; quota is null internally."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        envelope = call_tool(
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
        envelope = call_tool(
            client, "add_movie", title="Arrival", intent_context="sci-fi"
        )

    assert_eq(envelope["result"]["item"]["data"], {"title": "Arrival", "year": None})


@test()
def add_place_carries_its_own_fields() -> None:
    """A place Add carries differently-shaped payload fields than a movie."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        envelope = call_tool(
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
def add_book_carries_its_own_fields() -> None:
    """A book Add carries its own payload fields and lands active."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        envelope = call_tool(
            client,
            "add_book",
            title="Dune",
            author="Frank Herbert",
            intent_context="read before the movie",
        )

    item = envelope["result"]["item"]
    assert_eq(item["item_type"], "book")
    assert_eq(item["state"], "active")
    assert_eq(item["data"], {"title": "Dune", "author": "Frank Herbert"})
    assert_eq(envelope["result"]["dedup"]["severity"], "none")


@test()
def add_travel_carries_its_own_fields() -> None:
    """A travel Add carries its own payload fields and lands active."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        envelope = call_tool(
            client,
            "add_travel",
            destination="Japan",
            season="spring",
            intent_context="cherry blossoms",
        )

    item = envelope["result"]["item"]
    assert_eq(item["item_type"], "travel")
    assert_eq(item["state"], "active")
    assert_eq(item["data"], {"destination": "Japan", "season": "spring"})
    assert_eq(envelope["result"]["dedup"]["severity"], "none")


@test()
def add_book_without_author_stores_a_null_author() -> None:
    """Author is optional at the tool seam; the stored payload is the full type."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        envelope = call_tool(
            client, "add_book", title="Piranesi", intent_context="book club"
        )

    assert_eq(envelope["result"]["item"]["data"], {"title": "Piranesi", "author": None})


@test()
def add_blank_intent_yields_a_success_false_envelope() -> None:
    """Blank intent context is rejected as a well-formed envelope, adding nothing."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        envelope = call_tool(client, "add_movie", title="Dune", intent_context="   ")

        assert_eq(envelope["success"], False)
        assert_eq(envelope["error"]["code"], "invalid_input")
        assert_is_none(envelope["result"])

        found = call_tool(client, "search_bucket_items", q="Dune")

    assert_eq(found["result"], [])


@test()
def completing_a_terminal_item_yields_a_conflict_envelope() -> None:
    """A complete succeeds through the tool seam; a second one is a conflict."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        added = call_tool(client, "add_movie", title="Dune", intent_context="watch")
        item = added["result"]["item"]
        first = call_tool(
            client,
            "complete_bucket_item",
            bucket_item_id=item["id"],
            version=item["version"],
        )
        assert_eq(first["success"], True)
        assert_eq(first["result"]["state"], "completed")

        envelope = call_tool(
            client,
            "complete_bucket_item",
            bucket_item_id=item["id"],
            version=item["version"],
        )

    assert_eq(envelope["success"], False)
    assert_eq(envelope["error"]["code"], "conflict")


@test()
def tool_search_excludes_items_deleted_through_the_tool_seam() -> None:
    """Delete and Search work through the tool seam: terminal items drop out."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        active = call_tool(
            client, "add_movie", title="Blade Runner", intent_context="a"
        )
        done = call_tool(
            client, "add_movie", title="Blade of Glory", intent_context="b"
        )
        done_item = done["result"]["item"]
        deleted = call_tool(
            client,
            "delete_bucket_item",
            bucket_item_id=done_item["id"],
            version=done_item["version"],
        )
        assert_eq(deleted["result"]["state"], "deleted")

        found = call_tool(client, "search_bucket_items", q="Blade")

    found_ids = [item["id"] for item in found["result"]]
    assert_in(active["result"]["item"]["id"], found_ids)
    assert_not_in(done_item["id"], found_ids)
