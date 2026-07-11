"""Shared drivers for the dual-surface behaviour tests.

Each domain exposes the same capability twice — a public REST route and a
loopback `/internal/tools/*` tool endpoint — and issue #139 derives both from
one per-domain capability descriptor. The `test_*_surfaces` modules therefore
drive one app through both shells; this module owns the shared wiring: an app
client serving both surfaces (browser session auth plus the tool gate), the
browser login, and the enveloped tool call.
"""

from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

from snektest import assert_eq
from starlette.testclient import TestClient

from tether.embeddings import Embedder
from tether.server import AppConfig, create_app
from tether.telemetry import TelemetrySettings
from tether.tools import SessionRegistry

APP_PASSWORD = "test-app-password"
SESSION_SECRET = "test-session-secret"
TOOL_SECRET = "test-process-secret"
SECRET_HEADER = "X-Tether-Tool-Secret"
SESSION = "session-abc"


@contextmanager
def surface_client(
    root: Path, *, embedder: Embedder | None = None, **config: Any
) -> Generator[TestClient]:
    """A test app serving both surfaces from one isolated DB/KB root.

    The browser surface authenticates through `login`; the tool surface carries
    the known secret and the registered session. Extra keyword arguments are
    forwarded to `AppConfig` (seeded YouTube APIs, quota limits, generators).
    Yields only after any deferred YouTube boot sync has completed (see #122),
    so seeded source videos exist deterministically.
    """
    app = create_app(
        config=AppConfig(
            app_password=APP_PASSWORD,
            database_path=root / "tether.sqlite3",
            kb_root=root / ".tether",
            session_secret=SESSION_SECRET,
            **config,
        ),
        telemetry_settings=TelemetrySettings(install_global_provider=False),
        tool_secret=TOOL_SECRET,
        embedder=embedder,
    )
    cast("SessionRegistry", app.state.session_registry).register(SESSION)
    with TestClient(app) as client:
        boot_done = getattr(app.state, "youtube_boot_done", None)
        portal = client.portal
        if boot_done is not None and portal is not None:
            portal.call(boot_done.wait)
        yield client


def login(client: TestClient) -> None:
    """Authenticate the test browser against the REST surface."""
    response = client.post("/api/auth/login", json={"password": APP_PASSWORD})
    assert_eq(response.status_code, 204)


def call_tool(client: TestClient, tool: str, **params: Any) -> dict[str, Any]:
    """Invoke a tool with the known secret and session, returning the envelope."""
    response = client.post(
        f"/internal/tools/{tool}",
        json={"session_id": SESSION, **params},
        headers={SECRET_HEADER: TOOL_SECRET},
    )
    assert_eq(response.status_code, 200)
    return response.json()
