"""Dual-surface tests for Proposals: HTTP routes and the tool gate.

The REST routes assert request parsing, status codes, and optimistic-concurrency
409s; the tool registry is asserted to expose *only* `propose` and
`list_proposals` — approve/reject/grant/revoke are human-only and must never be
tools. The app's proposal service starts with an empty action registry (no
consumer in Phase A), so each test that needs to compose a real proposal
installs a fake `test.ok` kind onto the live service first.
"""

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast

from pydantic import BaseModel
from snektest import assert_eq, assert_in, assert_not_in, test
from starlette.applications import Starlette
from starlette.testclient import TestClient

from tests.surfaces import call_tool, login, surface_client
from tether.action_registry import (
    ActionContext,
    ActionResult,
    ActionSpec,
    build_action_registry,
)
from tether.proposal_tools import PROPOSAL_TOOL_SPECS
from tether.proposals import ProposalService
from tether.tool_registry import all_tool_specs

make_client = surface_client


class NoParams(BaseModel):
    """Empty params for the fake `test.ok` action kind."""


async def _ok(params: BaseModel, context: ActionContext) -> ActionResult:
    """A fake executor that always succeeds."""
    _ = params, context
    return ActionResult(outcome="succeeded")


def install_fake_kind(client: TestClient) -> None:
    """Register `test.ok`/`test.other` action kinds onto the live service."""
    app = cast("Starlette", client.app)
    service = cast("ProposalService", app.state.proposal_service)
    service.action_registry = build_action_registry(
        [
            ActionSpec("test.ok", NoParams, _ok, ui_hint="test.ok"),
            ActionSpec("test.other", NoParams, _ok, ui_hint="test.other"),
        ]
    )


def compose_pending(client: TestClient) -> dict[str, Any]:
    """Compose an uncovered (pending) proposal through the tool, return its view."""
    envelope = call_tool(
        client,
        "propose",
        consumer="test",
        title="Queue me",
        summary="a summary",
        actions=[{"kind": "test.ok", "scope": None, "params": {}}],
    )
    assert_eq(envelope["success"], True)
    return cast("dict[str, Any]", envelope["result"]["proposal"])


# --- the gate: only propose/list_proposals are tools -----------------------


@test()
def gate_verbs_are_absent_from_the_tool_registry() -> None:
    """The Proposal domain exposes only `propose` and `list_proposals` as tools.

    The gate verbs (approve/reject/grant/revoke) must never be Proposal tools, so
    the closed tool world holds the gate. `reject` exists globally as a *Memory*
    tool, so absence is asserted against the Proposal domain's own specs, while
    the two proposal tools are also asserted present in the global registry.
    """
    proposal_tool_names = {spec.name for spec in PROPOSAL_TOOL_SPECS}
    global_names = {spec.name for spec in all_tool_specs()}

    assert_eq(proposal_tool_names, {"propose", "list_proposals"})
    assert_in("propose", global_names)
    assert_in("list_proposals", global_names)
    for verb in ("approve", "reject", "grant", "revoke"):
        assert_not_in(verb, proposal_tool_names)


# --- tool surface ----------------------------------------------------------


@test()
def propose_unknown_kind_is_a_success_false_envelope() -> None:
    """With the empty Phase-A registry, an unknown kind is an invalid_input."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        envelope = call_tool(
            client,
            "propose",
            consumer="test",
            title="x",
            summary="y",
            actions=[{"kind": "gmail.archive", "scope": None, "params": {}}],
        )

    assert_eq(envelope["success"], False)
    assert_eq(envelope["error"]["code"], "invalid_input")


@test()
def propose_queues_and_list_proposals_sees_it() -> None:
    """A composed proposal queues pending and shows up in the tool list."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        install_fake_kind(client)
        proposal = compose_pending(client)
        listed = call_tool(client, "list_proposals")

    assert_eq(proposal["state"], "pending")
    assert_in(proposal["id"], [p["id"] for p in listed["result"]])


# --- REST surface: proposals ----------------------------------------------


@test()
def get_proposal_returns_the_detail() -> None:
    """`GET /api/proposals/{id}` returns the proposal bundled with its actions."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        install_fake_kind(client)
        login(client)
        proposal = compose_pending(client)

        response = client.get(f"/api/proposals/{proposal['id']}")

    assert_eq(response.status_code, 200)
    body = response.json()
    assert_eq(body["state"], "pending")
    assert_eq(len(body["actions"]), 1)


@test()
def approve_executes_and_bumps_version() -> None:
    """`POST /approve` runs the batch and returns an executed proposal."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        install_fake_kind(client)
        login(client)
        proposal = compose_pending(client)

        response = client.post(
            f"/api/proposals/{proposal['id']}/approve",
            json={"version": proposal["version"], "deselected_action_ids": []},
        )

    assert_eq(response.status_code, 200)
    body = response.json()
    assert_eq(body["state"], "executed")
    assert_eq(body["actions"][0]["outcome"], "succeeded")


@test()
def approve_with_a_stale_version_conflicts() -> None:
    """A second `approve` at the stale version is a 409."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        install_fake_kind(client)
        login(client)
        proposal = compose_pending(client)
        body: dict[str, object] = {
            "version": proposal["version"],
            "deselected_action_ids": [],
        }
        _ = client.post(f"/api/proposals/{proposal['id']}/approve", json=body)

        conflict = client.post(f"/api/proposals/{proposal['id']}/approve", json=body)

    assert_eq(conflict.status_code, 409)


@test()
def reject_with_a_stale_version_conflicts() -> None:
    """A second `reject` at the stale version is a 409."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        install_fake_kind(client)
        login(client)
        proposal = compose_pending(client)
        body: dict[str, object] = {"version": proposal["version"]}
        first = client.post(f"/api/proposals/{proposal['id']}/reject", json=body)
        assert_eq(first.status_code, 200)

        conflict = client.post(f"/api/proposals/{proposal['id']}/reject", json=body)

    assert_eq(conflict.status_code, 409)


@test()
def reject_returns_the_revocation_signal() -> None:
    """Rejecting an action in a granted category surfaces the covering grant id."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        install_fake_kind(client)
        login(client)
        grant = client.post("/api/grants", json={"kind": "test.ok"}).json()
        # test.ok is granted, but the second action (test.other) is not, so the
        # whole proposal still queues. Rejecting it should then surface the
        # test.ok grant as revocable, since it covers one of the actions.
        envelope = call_tool(
            client,
            "propose",
            consumer="test",
            title="Queue me",
            summary="a summary",
            actions=[
                {"kind": "test.ok", "scope": None, "params": {}},
                {"kind": "test.other", "scope": None, "params": {}},
            ],
        )
        proposal = envelope["result"]["proposal"]

        response = client.post(
            f"/api/proposals/{proposal['id']}/reject",
            json={"version": proposal["version"], "reason": "no thanks"},
        )

    assert_eq(response.status_code, 200)
    body = response.json()
    assert_eq(body["proposal"]["state"], "rejected")
    assert_in(grant["id"], body["revocable_grant_ids"])


@test()
def list_proposals_filters_by_state() -> None:
    """`GET /api/proposals?state=pending` filters the list by lifecycle state."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        install_fake_kind(client)
        login(client)
        proposal = compose_pending(client)

        pending = client.get("/api/proposals", params={"state": "pending"}).json()
        executed = client.get("/api/proposals", params={"state": "executed"}).json()

    assert_in(proposal["id"], [p["id"] for p in pending])
    assert_eq(executed, [])


# --- REST surface: grants --------------------------------------------------


@test()
def grants_create_list_and_revoke() -> None:
    """Grants can be created, listed live, and convergently revoked."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        login(client)
        created = client.post(
            "/api/grants", json={"kind": "gmail.archive", "scope": "newsletter"}
        )
        assert_eq(created.status_code, 201)
        grant = created.json()

        listed = client.get("/api/grants").json()
        assert_in(grant["id"], [g["id"] for g in listed])

        revoked = client.delete(f"/api/grants/{grant['id']}")
        assert_eq(revoked.status_code, 204)
        # Convergent: revoking again is still a no-op 204.
        again = client.delete(f"/api/grants/{grant['id']}")
        assert_eq(again.status_code, 204)

        after = client.get("/api/grants").json()

    assert_eq([g["id"] for g in after], [])


@test()
def suggestions_endpoint_returns_a_list() -> None:
    """`GET /api/grants/suggestions` serves the read-time calibration list."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        login(client)
        response = client.get("/api/grants/suggestions")

    assert_eq(response.status_code, 200)
    assert_eq(response.json(), [])
