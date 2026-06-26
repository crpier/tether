"""Behaviour tests for the loopback internal tool surface.

These drive the mounted Starlette app through `TestClient`, calling the
`/internal/tools/*` endpoints directly — no LLM, no pi. The surface is
authorized by a per-process secret (carried in a header) and an identity (the
pi session id in the body, validated against the host session registry). Every
tool response that gets past the auth gate conforms to the uniform envelope.
"""

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast

from snektest import assert_eq, assert_in, assert_is_none, assert_not_in, test
from starlette.testclient import TestClient

from tether.server import create_app
from tether.telemetry import TelemetrySettings
from tether.tools import SessionRegistry

SECRET = "test-process-secret"
SECRET_HEADER = "X-Tether-Tool-Secret"
SESSION = "session-abc"


def make_client(root: Path) -> TestClient:
    """A test app with an isolated DB/KB, a known tool secret, one session."""
    app = create_app(
        database_path=root / "tether.sqlite3",
        kb_root=root / ".tether",
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


def capture(client: TestClient, content: str) -> dict[str, Any]:
    """Capture one Memory through the tool seam and return its result payload."""
    envelope = call(client, "capture", content=content)
    assert_eq(envelope["success"], True)
    return envelope["result"]


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
            headers={SECRET_HEADER: "wrong"},
        )

    assert_eq(response.status_code, 401)


@test()
def call_with_unknown_session_is_rejected() -> None:
    """Identity must resolve to a registered session id."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        response = client.post(
            "/internal/tools/capture",
            json={"session_id": "ghost", "content": "x"},
            headers={SECRET_HEADER: SECRET},
        )

    assert_eq(response.status_code, 401)


@test()
def capture_returns_a_well_formed_success_envelope() -> None:
    """A successful capture conforms to the envelope; quota is null internally."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        envelope = call(client, "capture", content="  I prefer aisle seats  ")

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
        envelope = call(client, "capture", content="   ")

        assert_eq(envelope["success"], False)
        assert_eq(envelope["error"]["code"], "invalid_input")
        assert_is_none(envelope["result"])

        queue = call(client, "browse", state="loose")

    assert_eq(queue["result"], [])


@test()
def malformed_memory_id_yields_a_success_false_envelope() -> None:
    """A non-UUID memory id is dumb input, not a destructive action."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        envelope = call(client, "tether", memory_id="not-a-uuid", version=1)

    assert_eq(envelope["success"], False)
    assert_eq(envelope["error"]["code"], "invalid_input")


@test()
def capture_tether_search_invariant_holds_at_the_tool_seam() -> None:
    """Loose Memories are unsearchable; tethering makes them searchable."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        memory = capture(client, "needle in the corpus")

        before = call(client, "search", q="needle")
        assert_eq(before["result"], [])

        tethered = call(
            client, "tether", memory_id=memory["id"], version=memory["version"]
        )
        assert_eq(tethered["success"], True)
        assert_eq(tethered["result"]["state"], "tethered")

        after = call(client, "search", q="needle")

    found = [hit["id"] for hit in after["result"]]
    assert_in(memory["id"], found)
    assert_is_none(after["provenance"])


@test()
def all_six_tools_are_invokable() -> None:
    """capture, browse, search, tether, edit, reject all reach MemoryService."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        memory = capture(client, "I live in Berlin")

        edited = call(
            client,
            "edit",
            memory_id=memory["id"],
            content="I live in Munich",
            version=memory["version"],
        )
        assert_eq(edited["result"]["content"], "I live in Munich")

        tethered = call(
            client,
            "tether",
            memory_id=memory["id"],
            version=edited["result"]["version"],
        )
        assert_eq(tethered["result"]["state"], "tethered")

        browsed = call(client, "browse", state="tethered")
        assert_in(memory["id"], [m["id"] for m in browsed["result"]])

        rejected = call(
            client,
            "reject",
            memory_id=memory["id"],
            version=tethered["result"]["version"],
        )
        assert_eq(rejected["success"], True)

        gone = call(client, "search", q="Munich")
        assert_eq(gone["result"], [])


@test()
def tethering_an_absent_memory_is_a_not_found_envelope() -> None:
    """A live-but-absent target surfaces as `not_found`, never a crash."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        envelope = call(
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
        memory = capture(client, "I prefer window seats")
        first = call(
            client, "tether", memory_id=memory["id"], version=memory["version"]
        )

        again = call(
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
