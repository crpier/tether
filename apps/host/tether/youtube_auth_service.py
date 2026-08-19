"""Authorization state and coordination for the YouTube integration."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Literal, Protocol

from pydantic import BaseModel
from snekok import Err, Ok, Result

from tether.google_web_oauth import GoogleAuthorizationFlow, GoogleWebOAuthFlow
from tether.youtube_oauth import OAuthConfig, OAuthYouTubeApi
from tether.youtube_quota import LikedPage, RawYouTubeVideo, YouTubeApi

type YouTubeAuthState = Literal["authorizing", "connected", "disconnected", "error"]

authorization_failed_message = "Could not complete YouTube authorization."


class YouTubeAuthFailure(BaseModel):
    """Browser-safe failure from the YouTube authorization boundary."""

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


class YouTubeAuthBackend(Protocol):
    """Boundary for YouTube OAuth boundaries."""

    async def check(self) -> Result[bool, YouTubeAuthFailure]: ...

    async def start(
        self, *, redirect_uri: str
    ) -> Result[YouTubeAuthorization, YouTubeAuthFailure]: ...

    async def complete(
        self, *, authorization_response: str, expected_state: str
    ) -> Result[None, YouTubeAuthFailure]: ...


class YouTubeAuthorizationRequiredError(Exception):
    """Raised when a live YouTube read is attempted before authorization."""


class ReauthorizableYouTubeApi:
    """Stable API handle whose delegate can be replaced after consent."""

    def __init__(self) -> None:
        self._delegate: YouTubeApi | None = None

    @property
    def connected(self) -> bool:
        """Whether ingestion can currently call the live delegate."""
        return self._delegate is not None

    def connect(self, delegate: YouTubeApi) -> None:
        """Install the newly authorized Google API without restarting the host."""
        self._delegate = delegate

    def disconnect(self) -> None:
        """Remove the delegate while leaving the existing local corpus intact."""
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
        """Read metadata through the currently authorized delegate."""
        if self._delegate is None:
            raise YouTubeAuthorizationRequiredError
        return await self._delegate.fetch_video_metadata(video_ids)


class GoogleYouTubeAuthBackend:
    """Run Google web OAuth and install one reusable delegate."""

    def __init__(
        self,
        config: OAuthConfig,
        *,
        api_connection: ReauthorizableYouTubeApi | None = None,
        flow_factory: Callable[[], GoogleAuthorizationFlow] | None = None,
    ) -> None:
        self._api_connection = api_connection
        self._config = config
        self._flow = GoogleWebOAuthFlow(config, flow_factory=flow_factory)

    async def check(self) -> Result[bool, YouTubeAuthFailure]:
        """Validate the durable credential and reconnect delegate when usable."""
        if not self._flow.config.token_path.exists():
            if self._api_connection is not None:
                self._api_connection.disconnect()
            return Ok(value=False)
        try:
            api = OAuthYouTubeApi.from_config(self._config)
        except Exception:
            if self._api_connection is not None:
                self._api_connection.disconnect()
            return Err(
                YouTubeAuthFailure(
                    message="YouTube authorization expired. Reconnect YouTube."
                )
            )
        if self._api_connection is not None:
            self._api_connection.connect(api)
        return Ok(value=True)

    async def start(
        self, *, redirect_uri: str
    ) -> Result[YouTubeAuthorization, YouTubeAuthFailure]:
        """Create one browser authorization request."""
        outcome = await self._flow.start(redirect_uri=redirect_uri)
        if isinstance(outcome, Err):
            return Err(YouTubeAuthFailure(message=outcome.error.message))
        return Ok(YouTubeAuthorization(**outcome.value.model_dump()))

    async def complete(
        self, *, authorization_response: str, expected_state: str
    ) -> Result[None, YouTubeAuthFailure]:
        """Exchange callback and make delegate immediately usable."""
        _ = expected_state
        outcome = await self._flow.complete(
            authorization_response=authorization_response
        )
        if isinstance(outcome, Err):
            return Err(YouTubeAuthFailure(message=outcome.error.message))
        try:
            if self._api_connection is not None:
                self._api_connection.connect(OAuthYouTubeApi.from_config(self._config))
        except Exception:
            return Err(YouTubeAuthFailure(message=authorization_failed_message))
        return Ok(None)


class YouTubeAuthService:
    """Expose browser-safe authorization state over one backend."""

    def __init__(
        self,
        backend: YouTubeAuthBackend | None,
        *,
        on_authorized: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._backend = backend
        self._expected_state: str | None = None
        self._status = YouTubeAuthStatus()
        self._on_authorized = on_authorized

    @property
    def configured(self) -> bool:
        """Whether the installation is wired for Google OAuth."""
        return self._backend is not None

    async def available(self) -> bool:
        """Whether ingestion may call the external API now."""
        if not self.configured:
            return True
        return (await self.status()).state == "connected"

    async def status(self) -> YouTubeAuthStatus:
        """Return current status and memoize it for UI polling."""
        if self._status.state == "authorizing" or self._backend is None:
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
        """Create a browser consent request and hold expected state."""
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
        """Validate state and complete callback exchange."""
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
