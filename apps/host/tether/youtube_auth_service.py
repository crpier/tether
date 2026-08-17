"""Authorization state and coordination for the YouTube integration."""

from __future__ import annotations

import asyncio
import importlib
import logging
import tempfile
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from types import ModuleType
from typing import Literal, Protocol, cast

from pydantic import BaseModel
from snekok import Err, Ok, Result

from tether.youtube_oauth import OAuthConfig, OAuthYouTubeApi
from tether.youtube_quota import LikedPage, RawYouTubeVideo, YouTubeApi

type YouTubeAuthState = Literal["authorizing", "connected", "disconnected", "error"]

logger = logging.getLogger(__name__)


class YouTubeAuthFailure(BaseModel):
    """A browser-safe failure from the Google authorization boundary."""

    kind: Literal["authorization_failed", "state_mismatch"] = "authorization_failed"
    message: str


class YouTubeAuthorization(BaseModel):
    """A pending Google consent request safe to return to the browser."""

    authorization_url: str
    state: str


class YouTubeAuthStatus(BaseModel):
    """Current authorization state without credential material."""

    authorization_url: str | None = None
    error: str | None = None
    state: YouTubeAuthState = "disconnected"


class SerializableGoogleCredentials(Protocol):
    """Credential serialization needed after Google's code exchange."""

    def to_json(self) -> str:
        """Serialize refreshable credentials without exposing them to the browser."""
        ...


class GoogleAuthorizationFlow(Protocol):
    """Normalized browser OAuth flow used by the Google boundary."""

    @property
    def credentials(self) -> SerializableGoogleCredentials:
        """Return credentials after a successful token exchange."""
        ...

    @property
    def redirect_uri(self) -> str:
        """Return this attempt's callback URI."""
        ...

    @redirect_uri.setter
    def redirect_uri(self, redirect_uri: str) -> None:
        """Set the exact callback registered with Google."""
        ...

    def authorization_url(self) -> tuple[str, str]:
        """Return Google's consent URL and its generated CSRF state."""
        ...

    def fetch_token(self, *, authorization_response: str) -> None:
        """Exchange Google's callback URL for credentials."""
        ...


class _GoogleLibraryFlow(Protocol):
    """Untyped `google-auth-oauthlib` flow narrowed to the used surface."""

    credentials: SerializableGoogleCredentials
    redirect_uri: str

    def authorization_url(self, **kwargs: str) -> tuple[str, str]:
        """Return the raw library authorization request."""
        ...

    def fetch_token(self, *, authorization_response: str) -> None:
        """Exchange an authorization response through the raw library."""
        ...


class _GoogleLibraryFlowFactory(Protocol):
    """Class-level constructor exposed by `google_auth_oauthlib.flow.Flow`."""

    def from_client_secrets_file(
        self,
        client_secrets_file: str,
        *,
        autogenerate_code_verifier: bool,
        scopes: Sequence[str],
    ) -> _GoogleLibraryFlow:
        """Build a web flow from a Google client-secret document."""
        ...


class _GoogleAuthorizationFlowAdapter:
    """Apply Tether's refresh-token policy to the Google library flow."""

    def __init__(self, flow: _GoogleLibraryFlow) -> None:
        self._flow: _GoogleLibraryFlow = flow

    @property
    def credentials(self) -> SerializableGoogleCredentials:
        """Expose credentials only to the durable server-side writer."""
        return self._flow.credentials

    @property
    def redirect_uri(self) -> str:
        """Return the exact callback registered for this attempt."""
        return self._flow.redirect_uri

    @redirect_uri.setter
    def redirect_uri(self, redirect_uri: str) -> None:
        self._flow.redirect_uri = redirect_uri

    def authorization_url(self) -> tuple[str, str]:
        """Request offline access so Google returns a refresh token."""
        return self._flow.authorization_url(
            access_type="offline",
            prompt="consent",
        )

    def fetch_token(self, *, authorization_response: str) -> None:
        """Delegate Google's blocking authorization-code exchange."""
        self._flow.fetch_token(authorization_response=authorization_response)


class YouTubeAuthBackend(Protocol):
    """Boundary to Google OAuth and the durable YouTube credential."""

    async def check(self) -> Result[bool, YouTubeAuthFailure]:
        """Report whether the installed credential is usable."""
        ...

    async def start(
        self, *, redirect_uri: str
    ) -> Result[YouTubeAuthorization, YouTubeAuthFailure]:
        """Create one browser authorization request."""
        ...

    async def complete(
        self, *, authorization_response: str, expected_state: str
    ) -> Result[None, YouTubeAuthFailure]:
        """Exchange one validated browser callback for a durable credential."""
        ...


class YouTubeAuthorizationRequiredError(Exception):
    """Raised when a live YouTube read is attempted before authorization."""


class ReauthorizableYouTubeApi:
    """Stable API handle whose Google delegate can be replaced after consent."""

    def __init__(self) -> None:
        self._delegate: YouTubeApi | None = None

    @property
    def connected(self) -> bool:
        """Whether live YouTube requests currently have a usable delegate."""
        return self._delegate is not None

    def connect(self, delegate: YouTubeApi) -> None:
        """Install the newly authorized Google API without restarting the host."""
        self._delegate = delegate

    def disconnect(self) -> None:
        """Remove an unusable Google API while preserving the local corpus."""
        self._delegate = None

    async def list_liked_page(
        self, *, page_token: str | None, page_size: int
    ) -> LikedPage:
        """Read one liked page through the currently authorized delegate."""
        if self._delegate is None:
            raise YouTubeAuthorizationRequiredError
        return await self._delegate.list_liked_page(
            page_token=page_token,
            page_size=page_size,
        )

    async def fetch_video_metadata(
        self, video_ids: Sequence[str]
    ) -> Mapping[str, RawYouTubeVideo]:
        """Read video metadata through the currently authorized delegate."""
        if self._delegate is None:
            raise YouTubeAuthorizationRequiredError
        return await self._delegate.fetch_video_metadata(video_ids)


class GoogleYouTubeAuthBackend:
    """Run Google's web flow and install its credential atomically."""

    def __init__(
        self,
        config: OAuthConfig,
        *,
        api_connection: ReauthorizableYouTubeApi | None = None,
        flow_factory: Callable[[], GoogleAuthorizationFlow] | None = None,
    ) -> None:
        self._api_connection: ReauthorizableYouTubeApi | None = api_connection
        self._config: OAuthConfig = config
        self._flow: GoogleAuthorizationFlow | None = None
        self._flow_factory: Callable[[], GoogleAuthorizationFlow] = (
            flow_factory if flow_factory is not None else self._create_google_flow
        )

    async def check(self) -> Result[bool, YouTubeAuthFailure]:
        """Validate and refresh the durable credential when one exists."""
        if not self._config.token_path.exists():
            if self._api_connection is not None:
                self._api_connection.disconnect()
            return Ok(value=False)
        try:
            youtube_api = await asyncio.to_thread(
                OAuthYouTubeApi.from_config,
                self._config,
            )
        except Exception:
            if self._api_connection is not None:
                self._api_connection.disconnect()
            return Err(
                YouTubeAuthFailure(
                    message="YouTube authorization expired. Reconnect YouTube."
                )
            )
        if self._api_connection is not None:
            self._api_connection.connect(youtube_api)
        return Ok(value=True)

    async def start(
        self, *, redirect_uri: str
    ) -> Result[YouTubeAuthorization, YouTubeAuthFailure]:
        """Create one stateful Google browser-consent attempt."""
        try:
            self._flow = await asyncio.to_thread(self._flow_factory)
            self._flow.redirect_uri = redirect_uri
            authorization_url, state = await asyncio.to_thread(
                self._flow.authorization_url
            )
        except Exception:
            self._flow = None
            return Err(
                YouTubeAuthFailure(message="Could not start Google authorization.")
            )
        return Ok(
            YouTubeAuthorization(
                authorization_url=authorization_url,
                state=state,
            )
        )

    async def complete(
        self, *, authorization_response: str, expected_state: str
    ) -> Result[None, YouTubeAuthFailure]:
        """Exchange Google's callback and atomically install its credential."""
        _ = expected_state
        flow = self._flow
        self._flow = None
        if flow is None:
            return Err(
                YouTubeAuthFailure(message="No YouTube authorization is active.")
            )
        try:
            await asyncio.to_thread(
                flow.fetch_token,
                authorization_response=authorization_response,
            )
            await asyncio.to_thread(
                self._persist_credentials,
                flow.credentials.to_json(),
            )
            if self._api_connection is not None:
                self._api_connection.connect(
                    await asyncio.to_thread(
                        OAuthYouTubeApi.from_config,
                        self._config,
                    )
                )
        except Exception:
            logger.exception("Google YouTube token exchange failed")
            return Err(
                YouTubeAuthFailure(
                    message="Google did not complete YouTube authorization."
                )
            )
        return Ok(None)

    def _create_google_flow(self) -> GoogleAuthorizationFlow:
        """Load Google's optional library only when authorization starts."""
        module: ModuleType = importlib.import_module("google_auth_oauthlib.flow")
        flow_factory = cast("_GoogleLibraryFlowFactory", module.Flow)
        return _GoogleAuthorizationFlowAdapter(
            flow_factory.from_client_secrets_file(
                str(self._config.client_secret_path),
                autogenerate_code_verifier=True,
                scopes=self._config.scopes,
            )
        )

    def _persist_credentials(self, credentials_json: str) -> None:
        """Replace the token in one rename so readers never see partial JSON."""
        token_path: Path = self._config.token_path
        token_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=token_path.parent,
                mode="w",
                encoding="utf-8",
                delete=False,
            ) as temporary_file:
                temporary_path = Path(temporary_file.name)
                _ = temporary_file.write(credentials_json)
            temporary_path.chmod(0o600)
            _ = temporary_path.replace(token_path)
        finally:
            if temporary_path is not None and temporary_path.exists():
                temporary_path.unlink()


class YouTubeAuthService:
    """Expose browser-safe authorization state over one backend."""

    def __init__(
        self,
        backend: YouTubeAuthBackend | None,
        *,
        on_authorized: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._backend: YouTubeAuthBackend | None = backend
        self._expected_state: str | None = None
        self._on_authorized: Callable[[], Awaitable[None]] | None = on_authorized
        self._status: YouTubeAuthStatus = YouTubeAuthStatus()

    @property
    def configured(self) -> bool:
        """Whether this installation requires Google authorization."""
        return self._backend is not None

    async def available(self) -> bool:
        """Whether ingestion may call its configured upstream API now."""
        if not self.configured:
            return True
        return (await self.status()).state == "connected"

    async def status(self) -> YouTubeAuthStatus:
        """Check whether the server currently has a usable YouTube credential."""
        if self._backend is None or self._status.state == "authorizing":
            return self._status
        outcome = await self._backend.check()
        if isinstance(outcome, Err):
            self._status = YouTubeAuthStatus(
                error=outcome.error.message,
                state="error",
            )
            return self._status
        self._status = YouTubeAuthStatus(
            state="connected" if outcome.value else "disconnected"
        )
        return self._status

    async def start(self, *, redirect_uri: str) -> YouTubeAuthStatus:
        """Create a Google consent request and expose its browser-safe URL."""
        if self._backend is None:
            self._status = YouTubeAuthStatus(
                error="YouTube authorization is not configured.",
                state="error",
            )
            return self._status
        outcome = await self._backend.start(redirect_uri=redirect_uri)
        if isinstance(outcome, Err):
            self._status = YouTubeAuthStatus(
                error=outcome.error.message,
                state="error",
            )
            return self._status
        self._expected_state = outcome.value.state
        self._status = YouTubeAuthStatus(
            authorization_url=outcome.value.authorization_url,
            state="authorizing",
        )
        return self._status

    async def complete(
        self, *, authorization_response: str, state: str
    ) -> Result[YouTubeAuthStatus, YouTubeAuthFailure]:
        """Validate OAuth state before exchanging the callback for credentials."""
        expected_state = self._expected_state
        if self._backend is None or expected_state is None or state != expected_state:
            return Err(
                YouTubeAuthFailure(
                    kind="state_mismatch",
                    message="invalid YouTube authorization state",
                )
            )
        self._expected_state = None
        outcome = await self._backend.complete(
            authorization_response=authorization_response,
            expected_state=expected_state,
        )
        if isinstance(outcome, Err):
            self._status = YouTubeAuthStatus(
                error=outcome.error.message,
                state="error",
            )
            return Err(outcome.error)
        if self._on_authorized is not None:
            await self._on_authorized()
        self._status = YouTubeAuthStatus(state="connected")
        return Ok(self._status)
