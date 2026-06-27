"""App login, session cookie, and public REST guard behavior tests."""

from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from snektest import assert_eq, assert_in, assert_is_not_none, assert_true, test
from starlette.testclient import TestClient

from tether.auth import (
    SESSION_COOKIE,
    Principal,
    mint_session_cookie,
    verify_session_cookie,
)
from tether.server import AppConfig, create_app
from tether.telemetry import TelemetrySettings

APP_PASSWORD = "correct horse battery staple"
SESSION_SECRET = "stable-test-session-secret"


def make_client(root: Path, *, secure_cookies: bool = False) -> TestClient:
    """Create a test app with auth configured like a real host."""
    return TestClient(
        create_app(
            config=AppConfig(
                app_password=APP_PASSWORD,
                database_path=root / "tether.sqlite3",
                kb_root=root / ".tether",
                secure_cookies=secure_cookies,
                session_secret=SESSION_SECRET,
            ),
            telemetry_settings=TelemetrySettings(install_global_provider=False),
        )
    )


def login(client: TestClient) -> None:
    """Authenticate the test browser."""
    response = client.post("/api/auth/login", json={"password": APP_PASSWORD})
    assert_eq(response.status_code, 204)


def capture(client: TestClient, content: str) -> dict[str, Any]:
    """Capture one Memory through authenticated REST."""
    response = client.post("/api/memories", json={"content": content})
    assert_eq(response.status_code, 201)
    return response.json()


@test()
def correct_password_sets_http_only_signed_session_cookie() -> None:
    """Login mints an httpOnly cookie that verifies to the app principal."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        response = client.post("/api/auth/login", json={"password": APP_PASSWORD})

    assert_eq(response.status_code, 204)
    set_cookie = response.headers["set-cookie"]
    assert_in(f"{SESSION_COOKIE}=", set_cookie)
    assert_in("HttpOnly", set_cookie)
    assert_in("SameSite=lax", set_cookie)
    assert_is_not_none(
        verify_session_cookie(response.cookies[SESSION_COOKIE], SESSION_SECRET)
    )


@test()
def wrong_password_is_rejected_without_cookie() -> None:
    """Login uses the configured password as the credential gate."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        response = client.post("/api/auth/login", json={"password": "wrong"})

    assert_eq(response.status_code, 401)
    assert_eq(response.headers.get("set-cookie"), None)


@test()
def session_endpoint_reports_unauthenticated_and_authenticated_state() -> None:
    """The SPA can ask whether it should show login or the app."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        anonymous = client.get("/api/auth/session")
        login(client)
        authenticated = client.get("/api/auth/session")

    assert_eq(anonymous.json(), {"authenticated": False})
    assert_eq(authenticated.json(), {"authenticated": True})
    assert_in(f"{SESSION_COOKIE}=", authenticated.headers["set-cookie"])


@test()
def logout_clears_the_session_cookie() -> None:
    """Logout expires the browser's app session."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        login(client)
        response = client.post("/api/auth/logout")
        session = client.get("/api/auth/session")

    assert_eq(response.status_code, 204)
    assert_in(f"{SESSION_COOKIE}=", response.headers["set-cookie"])
    assert_eq(session.json(), {"authenticated": False})


@test()
def signed_cookie_survives_host_restart_with_stable_secret() -> None:
    """Stateless sessions verify in a later app process with the same secret."""
    with TemporaryDirectory() as directory:
        root = Path(directory)
        with make_client(root) as client:
            login(client)
            cookie_value = client.cookies[SESSION_COOKIE]
        with make_client(root) as client:
            client.cookies.set(SESSION_COOKIE, cookie_value)
            response = client.get("/api/auth/session")

    assert_eq(response.json(), {"authenticated": True})


@test()
def session_cookie_expires_after_thirty_days_and_slides_on_activity() -> None:
    """Session claims carry a 30-day expiry, and activity can mint a later one."""
    first = mint_session_cookie(
        Principal(sub="app"),
        SESSION_SECRET,
        issued_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    second = mint_session_cookie(
        Principal(sub="app"),
        SESSION_SECRET,
        issued_at=datetime(2026, 1, 2, tzinfo=UTC),
    )

    assert_is_not_none(
        verify_session_cookie(
            first,
            SESSION_SECRET,
            now=datetime(2026, 1, 30, 23, 59, tzinfo=UTC),
        )
    )
    assert_eq(
        verify_session_cookie(
            first,
            SESSION_SECRET,
            now=datetime(2026, 1, 31, 0, 1, tzinfo=UTC),
        ),
        None,
    )
    assert_is_not_none(
        verify_session_cookie(
            second,
            SESSION_SECRET,
            now=datetime(2026, 1, 31, 0, 1, tzinfo=UTC),
        )
    )


@test()
def public_rest_requires_a_valid_session_cookie() -> None:
    """Public REST rejects anonymous clients and accepts logged-in ones."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        anonymous = client.get("/api/memories", params={"state": "loose"})
        login(client)
        authenticated = client.get("/api/memories", params={"state": "loose"})

    assert_eq(anonymous.status_code, 401)
    assert_eq(authenticated.status_code, 200)


@test()
def auth_guard_exempts_internal_tools_and_docs() -> None:
    """Loopback tools and API docs stay reachable without an app cookie."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        internal = client.post("/internal/tools/capture", json={})
        openapi = client.get("/openapi.json")
        docs = client.get("/docs")

    assert_eq(internal.status_code, 401)
    assert_eq(openapi.status_code, 200)
    assert_eq(docs.status_code, 200)


@test()
def production_cookies_are_secure() -> None:
    """Production session cookies carry the Secure attribute."""
    with (
        TemporaryDirectory() as directory,
        make_client(Path(directory), secure_cookies=True) as client,
    ):
        response = client.post("/api/auth/login", json={"password": APP_PASSWORD})

    assert_in("Secure", response.headers["set-cookie"])


@test()
def public_rest_is_served_under_the_api_prefix() -> None:
    """The browser REST surface lives under `/api`, not the root."""
    with TemporaryDirectory() as directory, make_client(Path(directory)) as client:
        login(client)
        memory = capture(client, "I prefer aisle seats")
        old_path = client.get("/memories", params={"state": "loose"})
        document = client.get("/openapi.json").json()

    assert_eq(memory["state"], "loose")
    assert_eq(old_path.status_code, 404)
    assert_in("/api/memories", document["paths"])
    assert_true(all(path.startswith("/api") for path in document["paths"]))
