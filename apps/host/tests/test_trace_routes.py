"""REST behavior tests for the agent-trace inspection surface.

These drive the mounted Starlette app through `TestClient`, so the auth gate,
route wiring, and trace rendering are checked together. The recorder is
pre-populated directly (it is the same in-process object the tool seam writes to)
so the routes can be asserted without spawning a pi subprocess.
"""

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast

from snektest import assert_eq, assert_true, test
from starlette.testclient import TestClient

from tether.agent_trace import AgentTraceRecorder
from tether.embeddings import FakeEmbedder
from tether.server import AppConfig, create_app
from tether.telemetry import TelemetrySettings

APP_PASSWORD = "test-app-password"
SESSION_SECRET = "test-session-secret"


def _make_app(root: Path):  # noqa: ANN202 - returns a Starlette app
    """Create a test app with an isolated persistent DB and `.tether` root."""
    return create_app(
        config=AppConfig(
            app_password=APP_PASSWORD,
            database_path=root / "tether.sqlite3",
            kb_root=root / ".tether",
            session_secret=SESSION_SECRET,
        ),
        telemetry_settings=TelemetrySettings(install_global_provider=False),
        embedder=FakeEmbedder(),
    )


def _login(client: TestClient) -> None:
    """Authenticate the test browser."""
    response = client.post("/api/auth/login", json={"password": APP_PASSWORD})
    assert_eq(response.status_code, 204)


def _seed_completed_run(recorder: AgentTraceRecorder) -> str:
    """Record one completed run with a single tool call carrying a secret arg."""
    run_id = recorder.begin_run(session_id="s1", kind="conversation", prompt="hi")
    recorder.record_tool_call(
        session_id="s1",
        tool="capture",
        args={"content": "a fact", "tool_secret": "shh"},
        envelope={"success": True, "result": {"id": "m1", "state": "loose"}},
        duration_ms=3.0,
    )
    _ = recorder.end_run(session_id="s1", termination="completed")
    return run_id


@test()
def trace_routes_require_an_app_session() -> None:
    """The inspection surface is gated like every other browser-facing route."""
    with TemporaryDirectory() as directory:
        app = _make_app(Path(directory))
        run_id = _seed_completed_run(
            cast("AgentTraceRecorder", app.state.trace_recorder)
        )
        with TestClient(app) as client:
            assert_eq(client.get("/api/traces").status_code, 401)
            assert_eq(client.get(f"/api/traces/{run_id}").status_code, 401)


@test()
def a_completed_run_is_inspectable_with_secrets_redacted() -> None:
    """An authenticated browser can read a past run; secret args are masked."""
    with TemporaryDirectory() as directory:
        app = _make_app(Path(directory))
        run_id = _seed_completed_run(
            cast("AgentTraceRecorder", app.state.trace_recorder)
        )
        with TestClient(app) as client:
            _login(client)

            listed = client.get("/api/traces")
            assert_eq(listed.status_code, 200)
            runs = listed.json()["runs"]
            assert_eq(len(runs), 1)
            assert_eq(runs[0]["run_id"], run_id)

            detail = client.get(f"/api/traces/{run_id}")
            assert_eq(detail.status_code, 200)
            body = detail.json()
            assert_eq(body["kind"], "conversation")
            assert_eq(body["termination"], "completed")
            assert_eq(len(body["tool_calls"]), 1)
            tool_call = body["tool_calls"][0]
            assert_eq(tool_call["tool"], "capture")
            assert_eq(tool_call["args"]["content"], "a fact")
            assert_eq(tool_call["args"]["tool_secret"], "[redacted]")
            assert_true(tool_call["success"])


@test()
def an_unknown_run_id_is_a_404() -> None:
    """Inspecting a run that never existed (or was evicted) is a clean 404."""
    with TemporaryDirectory() as directory:
        app = _make_app(Path(directory))
        with TestClient(app) as client:
            _login(client)
            assert_eq(client.get("/api/traces/does-not-exist").status_code, 404)
