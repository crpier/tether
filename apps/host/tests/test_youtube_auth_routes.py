"""HTTP behavior tests for YouTube authorization recovery."""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import parse_qs, urlparse

from snekok.result import Ok, Result
from snektest import assert_eq, assert_false, assert_true, test

from tests.surfaces import login, surface_client
from tether.youtube.auth_service import (
    GoogleYouTubeAuthBackend,
    ReauthorizableYouTubeApi,
    YouTubeAuthBackend,
    YouTubeAuthFailure,
    YouTubeAuthorization,
)
from tether.youtube.local import InMemoryYouTubeApi
from tether.youtube.oauth import REQUIRED_SCOPES, OAuthConfig
from tether.youtube.quota import LikedPage, RawYouTubeVideo
from tether.youtube.types import VideoId


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


class RedirectYouTubeAuthBackend(YouTubeAuthBackend):
    """Capture the callback URI and return one Google consent request."""

    def __init__(self) -> None:
        self.authorization_response: str | None = None
        self.complete_calls: int = 0
        self.expected_state: str | None = None
        self.redirect_uri: str | None = None

    async def check(self) -> Result[bool, YouTubeAuthFailure]:
        return Ok(False)

    async def start(
        self, *, redirect_uri: str
    ) -> Result[YouTubeAuthorization, YouTubeAuthFailure]:
        self.redirect_uri = redirect_uri
        return Ok(
            YouTubeAuthorization(
                authorization_url="https://accounts.google.test/consent",
                state="google-state",
            )
        )

    async def complete(
        self, *, authorization_response: str, expected_state: str
    ) -> Result[None, YouTubeAuthFailure]:
        self.authorization_response = authorization_response
        self.complete_calls += 1
        self.expected_state = expected_state
        return Ok(None)


class AuthorizationGuardedYouTubeApi(InMemoryYouTubeApi):
    """Reject upstream reads until its matching OAuth callback completes."""

    def __init__(self, *, liked: list[RawYouTubeVideo]) -> None:
        super().__init__(liked=liked)
        self.authorized: bool = False

    async def list_liked_page(
        self, *, page_token: str | None, page_size: int
    ) -> LikedPage:
        if not self.authorized:
            raise AssertionError("YouTube was read before authorization")
        return await super().list_liked_page(
            page_token=page_token,
            page_size=page_size,
        )


class ConnectedAfterCallbackYouTubeAuthBackend(RedirectYouTubeAuthBackend):
    """Become connected only after the browser callback completes."""

    def __init__(self, youtube_api: AuthorizationGuardedYouTubeApi) -> None:
        super().__init__()
        self.connected: bool = False
        self.youtube_api: AuthorizationGuardedYouTubeApi = youtube_api

    async def check(self) -> Result[bool, YouTubeAuthFailure]:
        return Ok(self.connected)

    async def complete(
        self, *, authorization_response: str, expected_state: str
    ) -> Result[None, YouTubeAuthFailure]:
        outcome = await super().complete(
            authorization_response=authorization_response,
            expected_state=expected_state,
        )
        self.connected = True
        self.youtube_api.authorized = True
        return outcome


class DisconnectedYouTubeAuthBackend(YouTubeAuthBackend):
    """Report an integration that has no usable Google credential."""

    async def check(self) -> Result[bool, YouTubeAuthFailure]:
        return Ok(False)

    async def start(
        self, *, redirect_uri: str
    ) -> Result[YouTubeAuthorization, YouTubeAuthFailure]:
        raise AssertionError("authorization was not expected")

    async def complete(
        self, *, authorization_response: str, expected_state: str
    ) -> Result[None, YouTubeAuthFailure]:
        raise AssertionError("authorization was not expected")


@test()
def invalid_youtube_credentials_do_not_crash_host_startup() -> None:
    """A broken durable token leaves the app available for UI reconnection."""
    with TemporaryDirectory() as directory:
        root = Path(directory)
        token_path = root / "youtube" / "token.json"
        token_path.parent.mkdir()
        _ = token_path.write_text("not-json", encoding="utf-8")
        backend = GoogleYouTubeAuthBackend(
            OAuthConfig(
                scopes=REQUIRED_SCOPES,
                client_secret_path=root / "client-secret.json",
                token_path=token_path,
            ),
            api_connection=ReauthorizableYouTubeApi(),
        )

        with surface_client(
            root,
            transcript_sync_enabled=False,
            youtube_api=InMemoryYouTubeApi(),
            youtube_auth_backend=backend,
            youtube_sync_enabled=True,
        ) as client:
            login(client)
            response = client.get("/api/youtube-auth")

    assert_eq(response.status_code, 200)
    assert_eq(response.json()["state"], "error")
    assert_eq(
        response.json()["error"],
        "YouTube authorization expired. Reconnect YouTube.",
    )


@test()
def successful_youtube_callback_immediately_syncs_likes() -> None:
    """A newly connected integration refreshes the local corpus without restart."""
    youtube_api = AuthorizationGuardedYouTubeApi(
        liked=[
            RawYouTubeVideo(
                video_id=VideoId("fresh-video"),
                title="Freshly liked",
                channel="Channel",
                topic="youtube",
            )
        ]
    )
    backend = ConnectedAfterCallbackYouTubeAuthBackend(youtube_api)
    with (
        TemporaryDirectory() as directory,
        surface_client(
            Path(directory),
            transcript_sync_enabled=False,
            youtube_api=youtube_api,
            youtube_auth_backend=backend,
            youtube_sync_enabled=True,
        ) as client,
    ):
        login(client)
        _ = client.post("/api/youtube-auth")

        _ = client.get("/api/youtube-auth/callback?state=google-state&code=google-code")
        videos = client.get("/api/youtube")

    assert_eq(videos.status_code, 200)
    assert_eq([video["video_id"] for video in videos.json()["videos"]], ["fresh-video"])


@test()
async def google_consent_does_not_request_previously_granted_scopes() -> None:
    """A YouTube-only token exchange cannot be widened by project grant history."""
    with TemporaryDirectory() as directory:
        root = Path(directory)
        client_secret_path = root / "client-secret.json"
        _ = client_secret_path.write_text(
            json.dumps(
                {
                    "web": {
                        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                        "client_id": "test.apps.googleusercontent.com",
                        "client_secret": "test-secret",
                        "redirect_uris": [
                            "https://tether.example.test/api/youtube-auth/callback"
                        ],
                        "token_uri": "https://oauth2.googleapis.com/token",
                    }
                }
            ),
            encoding="utf-8",
        )
        backend = GoogleYouTubeAuthBackend(
            OAuthConfig(
                scopes=REQUIRED_SCOPES,
                client_secret_path=client_secret_path,
                token_path=root / "token.json",
            )
        )

        outcome = await backend.start(
            redirect_uri="https://tether.example.test/api/youtube-auth/callback"
        )

    assert_true(isinstance(outcome, Ok))
    if not isinstance(outcome, Ok):
        return
    query = parse_qs(urlparse(outcome.value.authorization_url).query)
    assert_eq(query["access_type"], ["offline"])
    assert_eq(query["prompt"], ["consent"])
    assert_false("include_granted_scopes" in query)


@test()
def successful_youtube_callback_persists_google_credentials() -> None:
    """Completing browser consent installs the refresh token durably."""
    with TemporaryDirectory() as directory:
        root = Path(directory)
        token_path = root / "youtube" / "token.json"
        backend = GoogleYouTubeAuthBackend(
            OAuthConfig(
                scopes=REQUIRED_SCOPES,
                client_secret_path=root / "client-secret.json",
                token_path=token_path,
            ),
            flow_factory=FakeGoogleAuthorizationFlow,
        )
        with surface_client(
            root,
            transcript_sync_enabled=False,
            youtube_auth_backend=backend,
            youtube_sync_enabled=False,
        ) as client:
            login(client)
            _ = client.post("/api/youtube-auth")

            response = client.get(
                "/api/youtube-auth/callback?state=google-state&code=google-code",
                follow_redirects=False,
            )

        stored_credential = json.loads(token_path.read_text("utf-8"))

    assert_eq(response.status_code, 303)
    assert_eq(stored_credential, {"refresh_token": "durable-refresh-token"})


@test()
def authenticated_user_can_start_youtube_authorization() -> None:
    """Settings receives Google consent and the backend gets the exact callback."""
    backend = RedirectYouTubeAuthBackend()
    with (
        TemporaryDirectory() as directory,
        surface_client(
            Path(directory),
            transcript_sync_enabled=False,
            youtube_auth_backend=backend,
            youtube_sync_enabled=False,
        ) as client,
    ):
        login(client)

        response = client.post("/api/youtube-auth")

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
        "http://testserver/api/youtube-auth/callback",
    )


@test()
def configured_public_origin_is_used_for_the_entire_google_oauth_flow() -> None:
    """Funnel's HTTP proxy cannot downgrade Google's registered HTTPS callback."""
    backend = RedirectYouTubeAuthBackend()
    with (
        TemporaryDirectory() as directory,
        surface_client(
            Path(directory),
            public_origin="https://tether.tail2da0b1.ts.net/",
            transcript_sync_enabled=False,
            youtube_auth_backend=backend,
            youtube_sync_enabled=False,
        ) as client,
    ):
        login(client)
        _ = client.post("/api/youtube-auth")

        _ = client.get(
            "/api/youtube-auth/callback?state=google-state&code=google-code",
            follow_redirects=False,
        )

    expected_callback = "https://tether.tail2da0b1.ts.net/api/youtube-auth/callback"
    assert_eq(backend.redirect_uri, expected_callback)
    assert_eq(
        backend.authorization_response,
        f"{expected_callback}?state=google-state&code=google-code",
    )


@test()
def successful_youtube_callback_returns_to_settings() -> None:
    """Google's callback exchanges once and redirects back to the integration UI."""
    backend = RedirectYouTubeAuthBackend()
    with (
        TemporaryDirectory() as directory,
        surface_client(
            Path(directory),
            transcript_sync_enabled=False,
            youtube_auth_backend=backend,
            youtube_sync_enabled=False,
        ) as client,
    ):
        login(client)
        _ = client.post("/api/youtube-auth")

        response = client.get(
            "/api/youtube-auth/callback?state=google-state&code=google-code",
            follow_redirects=False,
        )

    assert_eq(response.status_code, 303)
    assert_eq(response.headers["location"], "/settings?youtube_auth=connected")
    assert_eq(backend.complete_calls, 1)
    assert_eq(backend.expected_state, "google-state")
    assert_eq(
        backend.authorization_response,
        "http://testserver/api/youtube-auth/callback?state=google-state&code=google-code",
    )


@test()
def youtube_callback_rejects_mismatched_state() -> None:
    """A forged callback cannot reach Google's token exchange boundary."""
    backend = RedirectYouTubeAuthBackend()
    with (
        TemporaryDirectory() as directory,
        surface_client(
            Path(directory),
            transcript_sync_enabled=False,
            youtube_auth_backend=backend,
            youtube_sync_enabled=False,
        ) as client,
    ):
        login(client)
        _ = client.post("/api/youtube-auth")

        response = client.get(
            "/api/youtube-auth/callback?state=forged-state&code=google-code"
        )

    assert_eq(response.status_code, 400)
    assert_eq(response.json(), {"detail": "invalid YouTube authorization state"})
    assert_eq(backend.complete_calls, 0)


@test()
def authenticated_user_can_read_disconnected_youtube_authorization() -> None:
    """Settings reports when no usable Google credential is installed."""
    with (
        TemporaryDirectory() as directory,
        surface_client(
            Path(directory),
            transcript_sync_enabled=False,
            youtube_auth_backend=DisconnectedYouTubeAuthBackend(),
            youtube_sync_enabled=False,
        ) as client,
    ):
        login(client)

        response = client.get("/api/youtube-auth")

    assert_eq(response.status_code, 200)
    assert_eq(
        response.json(),
        {
            "authorization_url": None,
            "error": None,
            "state": "disconnected",
        },
    )
