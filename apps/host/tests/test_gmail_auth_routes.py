"""HTTP behavior tests for Gmail authorization recovery."""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory

from snekok.result import Ok, Result
from snektest import assert_eq, test

from tests.surfaces import login, surface_client
from tether.gmail.auth_service import (
    GmailAuthBackend,
    GmailAuthFailure,
    GmailAuthorization,
    GoogleGmailAuthBackend,
)
from tether.gmail.oauth import GMAIL_READONLY_SCOPE, OAuthConfig


class FakeGoogleCredentials:
    """Serialize one recognizable installed credential."""

    def to_json(self) -> str:
        return json.dumps({"refresh_token": "durable-refresh-token"})


class FakeGoogleAuthorizationFlow:
    """Model Google's browser flow without network requests."""

    def __init__(self) -> None:
        self.credentials = FakeGoogleCredentials()
        self.redirect_uri: str = ""

    def authorization_url(self) -> tuple[str, str]:
        return "https://accounts.google.test/consent", "google-state"

    def fetch_token(self, *, authorization_response: str) -> None:
        _ = authorization_response


class RedirectGmailAuthBackend(GmailAuthBackend):
    """Capture the callback URI and return one Google consent request."""

    def __init__(self) -> None:
        self.authorization_response: str | None = None
        self.complete_calls: int = 0
        self.expected_state: str | None = None
        self.redirect_uri: str | None = None

    async def check(self) -> Result[bool, GmailAuthFailure]:
        return Ok(False)

    async def start(
        self, *, redirect_uri: str
    ) -> Result[GmailAuthorization, GmailAuthFailure]:
        self.redirect_uri = redirect_uri
        return Ok(
            GmailAuthorization(
                authorization_url="https://accounts.google.test/consent",
                state="google-state",
            )
        )

    async def complete(
        self, *, authorization_response: str, expected_state: str
    ) -> Result[None, GmailAuthFailure]:
        self.authorization_response = authorization_response
        self.complete_calls += 1
        self.expected_state = expected_state
        return Ok(None)


class DisconnectedGmailAuthBackend(GmailAuthBackend):
    """Report a Gmail integration with no usable credential."""

    async def check(self) -> Result[bool, GmailAuthFailure]:
        return Ok(False)

    async def start(
        self, *, redirect_uri: str
    ) -> Result[GmailAuthorization, GmailAuthFailure]:
        raise AssertionError("authorization was not expected")

    async def complete(
        self, *, authorization_response: str, expected_state: str
    ) -> Result[None, GmailAuthFailure]:
        raise AssertionError("authorization was not expected")


@test()
def invalid_gmail_credentials_do_not_crash_host_startup() -> None:
    """A broken durable token leaves the app available for UI reconnection."""
    with TemporaryDirectory() as directory:
        root = Path(directory)
        token_path = root / "gmail" / "token.json"
        token_path.parent.mkdir()
        _ = token_path.write_text("not-json", encoding="utf-8")
        backend = GoogleGmailAuthBackend(
            OAuthConfig(
                scopes=(GMAIL_READONLY_SCOPE,),
                client_secret_path=root / "client-secret.json",
                token_path=token_path,
            )
        )

        with surface_client(
            root,
            transcript_sync_enabled=False,
            gmail_auth_backend=backend,
            gmail_sync_enabled=True,
        ) as client:
            login(client)
            response = client.get("/api/gmail-auth")

    assert_eq(response.status_code, 200)
    assert_eq(response.json()["state"], "error")
    assert_eq(
        response.json()["error"],
        "Gmail authorization expired. Reconnect Gmail.",
    )


@test()
def successful_gmail_callback_persists_google_credentials() -> None:
    """Completing browser consent installs the refresh token durably."""
    with TemporaryDirectory() as directory:
        root = Path(directory)
        token_path = root / "gmail" / "token.json"
        backend = GoogleGmailAuthBackend(
            OAuthConfig(
                scopes=(GMAIL_READONLY_SCOPE,),
                client_secret_path=root / "client-secret.json",
                token_path=token_path,
            ),
            flow_factory=FakeGoogleAuthorizationFlow,
        )
        with surface_client(
            root,
            transcript_sync_enabled=False,
            gmail_auth_backend=backend,
            gmail_sync_enabled=False,
        ) as client:
            login(client)
            _ = client.post("/api/gmail-auth")

            response = client.get(
                "/api/gmail-auth/callback?state=google-state&code=google-code",
                follow_redirects=False,
            )

        stored_credential = json.loads(token_path.read_text("utf-8"))

    assert_eq(response.status_code, 303)
    assert_eq(stored_credential, {"refresh_token": "durable-refresh-token"})


@test()
def authenticated_user_can_start_gmail_authorization() -> None:
    """Settings receives Google consent and the backend gets the exact callback."""
    backend = RedirectGmailAuthBackend()
    with (
        TemporaryDirectory() as directory,
        surface_client(
            Path(directory),
            transcript_sync_enabled=False,
            gmail_auth_backend=backend,
            gmail_sync_enabled=False,
        ) as client,
    ):
        login(client)

        response = client.post("/api/gmail-auth")

    assert_eq(response.status_code, 202)
    assert_eq(
        response.json(),
        {
            "authorization_url": "https://accounts.google.test/consent",
            "error": None,
            "state": "authorizing",
        },
    )
    assert_eq(
        backend.redirect_uri,
        "http://testserver/api/gmail-auth/callback",
    )


@test()
def configured_public_origin_is_used_for_the_entire_google_oauth_flow() -> None:
    """Reverse-proxy traffic cannot downgrade Gmail's HTTPS callback."""
    backend = RedirectGmailAuthBackend()
    with (
        TemporaryDirectory() as directory,
        surface_client(
            Path(directory),
            public_origin="https://tether.tail2da0b1.ts.net/",
            transcript_sync_enabled=False,
            gmail_auth_backend=backend,
            gmail_sync_enabled=False,
        ) as client,
    ):
        login(client)
        _ = client.post("/api/gmail-auth")

        _ = client.get(
            "/api/gmail-auth/callback?state=google-state&code=google-code",
            follow_redirects=False,
        )

    expected_callback = "https://tether.tail2da0b1.ts.net/api/gmail-auth/callback"
    assert_eq(backend.redirect_uri, expected_callback)
    assert_eq(
        backend.authorization_response,
        f"{expected_callback}?state=google-state&code=google-code",
    )


@test()
def successful_gmail_callback_returns_to_settings() -> None:
    """Google's callback redirects back to the settings auth panel."""
    backend = RedirectGmailAuthBackend()
    with (
        TemporaryDirectory() as directory,
        surface_client(
            Path(directory),
            transcript_sync_enabled=False,
            gmail_auth_backend=backend,
            gmail_sync_enabled=False,
        ) as client,
    ):
        login(client)
        _ = client.post("/api/gmail-auth")

        response = client.get(
            "/api/gmail-auth/callback?state=google-state&code=google-code",
            follow_redirects=False,
        )

    assert_eq(response.status_code, 303)
    assert_eq(response.headers["location"], "/settings?gmail_auth=connected")
    assert_eq(backend.complete_calls, 1)
    assert_eq(backend.expected_state, "google-state")
    assert_eq(
        backend.authorization_response,
        "http://testserver/api/gmail-auth/callback?state=google-state&code=google-code",
    )


@test()
def gmail_callback_rejects_mismatched_state() -> None:
    """A forged callback cannot reach Google's token exchange boundary."""
    backend = RedirectGmailAuthBackend()
    with (
        TemporaryDirectory() as directory,
        surface_client(
            Path(directory),
            transcript_sync_enabled=False,
            gmail_auth_backend=backend,
            gmail_sync_enabled=False,
        ) as client,
    ):
        login(client)
        _ = client.post("/api/gmail-auth")

        response = client.get(
            "/api/gmail-auth/callback?state=forged-state&code=google-code"
        )

    assert_eq(response.status_code, 400)
    assert_eq(response.json(), {"detail": "invalid Gmail authorization state"})
    assert_eq(backend.complete_calls, 0)


@test()
def authenticated_user_can_read_disconnected_gmail_authorization() -> None:
    """Settings reports when no usable Gmail credential is installed."""
    with (
        TemporaryDirectory() as directory,
        surface_client(
            Path(directory),
            transcript_sync_enabled=False,
            gmail_auth_backend=DisconnectedGmailAuthBackend(),
            gmail_sync_enabled=False,
        ) as client,
    ):
        login(client)

        response = client.get("/api/gmail-auth")

    assert_eq(response.status_code, 200)
    assert_eq(
        response.json(),
        {
            "authorization_url": None,
            "error": None,
            "state": "disconnected",
        },
    )
