"""Unit tests for the shared per-capability descriptor machinery.

One `ErrorRule` table per domain drives both HTTP surfaces: the REST decorator
translates the listed failures onto status codes and detail bodies, and the
tool endpoint translates the same table onto envelope error codes. These tests
pin that shared translation with a tiny stand-in domain, so the per-domain
surface tests only need to cover their tables' contents.
"""

from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel
from snektest import assert_eq, assert_is_none, test
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route
from starlette.testclient import TestClient

from tether.capabilities import (
    CapabilityOutcome,
    ErrorRule,
    bind_params,
    rest_response,
    translate_domain_errors,
)
from tether.memories import MemoryProvenance
from tether.tools import SessionRegistry, ToolEndpoint, ToolRoute
from tether.youtube import CacheMeta, QuotaMeta

SECRET = "test-process-secret"
SECRET_HEADER = "X-Tether-Tool-Secret"
SESSION = "session-abc"


class MissingThingError(Exception):
    """Stand-in absence failure."""


class ThingConflictError(Exception):
    """Stand-in conflict failure."""


class UnlistedError(Exception):
    """A failure no rule names; both surfaces must let it propagate."""


RULES: tuple[ErrorRule, ...] = (
    ErrorRule((MissingThingError,), "not_found", 404, detail="thing not found"),
    ErrorRule((ThingConflictError,), "conflict", 409),
)


class PokeParams(BaseModel):
    """Params for the stand-in tool capability."""

    thing_id: str
    version: int = 1


def rest_client(handler: Callable[[Request], Awaitable[Response]]) -> TestClient:
    """Mount one handler on a bare Starlette app."""
    app = Starlette(routes=[Route("/poke", handler, methods=["POST"])])
    return TestClient(app, raise_server_exceptions=False)


def tool_client(
    handler: Callable[[Request, Any], Awaitable[CapabilityOutcome]],
    *,
    errors: tuple[ErrorRule, ...],
) -> TestClient:
    """Mount one tool endpoint with the auth state its gate expects."""
    app = Starlette(
        routes=[
            ToolRoute(
                "/internal/tools/poke",
                ToolEndpoint(PokeParams, handler, errors=errors),
                methods=["POST"],
            )
        ]
    )
    app.state.tool_secret = SECRET
    registry = SessionRegistry()
    registry.register(SESSION)
    app.state.session_registry = registry
    return TestClient(app, raise_server_exceptions=False)


def call_tool(client: TestClient, **params: Any) -> dict[str, Any]:
    """Invoke the stand-in tool past the gate, returning the envelope."""
    response = client.post(
        "/internal/tools/poke",
        json={"session_id": SESSION, **params},
        headers={SECRET_HEADER: SECRET},
    )
    assert_eq(response.status_code, 200)
    return response.json()


@test()
def rest_translation_maps_a_listed_failure_to_its_status_and_fixed_detail() -> None:
    """A rule with a fixed `detail` hides the exception message from REST."""

    @translate_domain_errors(RULES)
    async def handler(_request: Request) -> Response:
        raise MissingThingError("thing t1 (row 42)")

    response = rest_client(handler).post("/poke")

    assert_eq(response.status_code, 404)
    assert_eq(response.json(), {"detail": "thing not found"})


@test()
def rest_translation_defaults_the_detail_to_the_exception_message() -> None:
    """A rule without a fixed `detail` serves the exception's own message."""

    @translate_domain_errors(RULES)
    async def handler(_request: Request) -> Response:
        raise ThingConflictError("stale version")

    response = rest_client(handler).post("/poke")

    assert_eq(response.status_code, 409)
    assert_eq(response.json(), {"detail": "stale version"})


@test()
def rest_translation_lets_an_unlisted_failure_propagate() -> None:
    """Failures outside the domain table are not swallowed into a status."""

    @translate_domain_errors(RULES)
    async def handler(_request: Request) -> Response:
        raise UnlistedError("boom")

    response = rest_client(handler).post("/poke")

    assert_eq(response.status_code, 500)


@test()
def rest_translation_passes_the_happy_path_through() -> None:
    """An outcome serialises verbatim at the capability's REST status."""

    @translate_domain_errors(RULES)
    async def handler(_request: Request) -> Response:
        return rest_response(CapabilityOutcome(result={"ok": True}), status_code=201)

    response = rest_client(handler).post("/poke")

    assert_eq(response.status_code, 201)
    assert_eq(response.json(), {"ok": True})


@test()
def tool_translation_maps_a_listed_failure_to_its_envelope_code() -> None:
    """The same rule that is a REST 404 is a `not_found` envelope, message fixed."""

    async def handler(_request: Request, _params: PokeParams) -> CapabilityOutcome:
        raise MissingThingError("thing t1 (row 42)")

    envelope = call_tool(tool_client(handler, errors=RULES), thing_id="t1")

    assert_eq(envelope["success"], False)
    assert_eq(envelope["error"], {"code": "not_found", "message": "not found"})
    assert_is_none(envelope["result"])


@test()
def tool_translation_keeps_the_message_for_non_absence_codes() -> None:
    """Conflict envelopes carry the exception's own message."""

    async def handler(_request: Request, _params: PokeParams) -> CapabilityOutcome:
        raise ThingConflictError("stale version")

    envelope = call_tool(tool_client(handler, errors=RULES), thing_id="t1")

    assert_eq(envelope["success"], False)
    assert_eq(envelope["error"], {"code": "conflict", "message": "stale version"})


@test()
def tool_translation_lets_an_unlisted_failure_propagate() -> None:
    """Failures outside the domain table are not swallowed into an envelope."""

    async def handler(_request: Request, _params: PokeParams) -> CapabilityOutcome:
        raise UnlistedError("boom")

    client = tool_client(handler, errors=RULES)
    response = client.post(
        "/internal/tools/poke",
        json={"session_id": SESSION, "thing_id": "t1"},
        headers={SECRET_HEADER: SECRET},
    )

    assert_eq(response.status_code, 500)


@test()
def tool_success_envelope_carries_the_outcome_metadata() -> None:
    """Result, provenance, quota, and cache ride from outcome to envelope."""

    async def handler(_request: Request, _params: PokeParams) -> CapabilityOutcome:
        return CapabilityOutcome(
            result={"id": "t1"},
            provenance=MemoryProvenance(kind="manual"),
            quota=QuotaMeta(limit=10, used=3, remaining=7),
            cache=CacheMeta(hit=True, source="cache"),
        )

    envelope = call_tool(tool_client(handler, errors=RULES), thing_id="t1")

    assert_eq(envelope["success"], True)
    assert_eq(envelope["result"], {"id": "t1"})
    assert_eq(envelope["provenance"], {"kind": "manual"})
    assert_eq(envelope["quota"], {"limit": 10, "used": 3, "remaining": 7})
    assert_eq(envelope["cache"], {"hit": True, "source": "cache"})
    assert_is_none(envelope["error"])


@test()
def bind_params_splats_the_params_fields_onto_the_execute() -> None:
    """`bind_params` adapts an execute taking keyword fields to a tool handler."""

    async def execute(
        _request: Request, thing_id: str, version: int = 1
    ) -> CapabilityOutcome:
        return CapabilityOutcome(result={"thing_id": thing_id, "version": version})

    envelope = call_tool(
        tool_client(bind_params(execute), errors=()), thing_id="t7", version=3
    )

    assert_eq(envelope["success"], True)
    assert_eq(envelope["result"], {"thing_id": "t7", "version": 3})
