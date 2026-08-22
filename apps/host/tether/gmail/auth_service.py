"""Authorization state and reusable Gmail transport for Settings-style consent."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import Literal, Protocol

from pydantic import BaseModel
from snekok import Err, Ok, Result

from tether.gmail.client import (
    GmailAuthenticationFailure,
    GmailClient,
    GmailFailure,
    GmailLabel,
    GmailMessage,
    GmailMessagePreview,
    GmailOperation,
    GmailRawMessage,
    GmailSearchPage,
    GmailTransport,
    GmailWriteResult,
)
from tether.gmail.oauth import OAuthConfig, load_credentials
from tether.google_web_oauth import GoogleAuthorizationFlow, GoogleWebOAuthFlow
from tether.structured_logging import Logger

type GmailAuthState = Literal["authorizing", "connected", "disconnected", "error"]


authorization_failed_message = "Could not complete Gmail authorization."


class GmailAuthFailure(BaseModel):
    """Browser-safe failure from the Gmail authorization boundary."""

    kind: Literal["authorization_failed", "state_mismatch"] = "authorization_failed"
    message: str


class GmailAuthorization(BaseModel):
    """A pending Google consent request safe to return to the browser."""

    authorization_url: str
    state: str


class GmailAuthStatus(BaseModel):
    """Current authorization state without credential material."""

    authorization_url: str | None = None
    error: str | None = None
    state: GmailAuthState = "disconnected"


class GmailAuthBackend(Protocol):
    """Boundary for Gmail OAuth backends."""

    async def check(self) -> Result[bool, GmailAuthFailure]: ...

    async def start(
        self, *, redirect_uri: str
    ) -> Result[GmailAuthorization, GmailAuthFailure]: ...

    async def complete(
        self, *, authorization_response: str, expected_state: str
    ) -> Result[None, GmailAuthFailure]: ...


class ReauthorizableGmailClient(GmailClient):
    """Gmail client whose delegate can be swapped after consent callback."""

    def __init__(self) -> None:
        self._delegate: GmailClient | None = None

    @property
    def connected(self) -> bool:
        """Whether an OAuth-backed delegate can currently serve calls."""

        return self._delegate is not None

    def connect(self, transport: GmailTransport) -> None:
        """Install a new delegate without restarting the host."""

        self._delegate = GmailClient(transport)

    def disconnect(self) -> None:
        """Drop the delegate while keeping wrapper identity stable."""

        self._delegate = None

    def _require_client(self, operation: GmailOperation) -> GmailAuthenticationFailure:
        return GmailAuthenticationFailure(
            message="Gmail authorization not configured",
            operation=operation,
        )

    async def list_message_ids(
        self,
        *,
        query: str,
        logger: Logger,
    ) -> Result[list[str], GmailFailure]:
        """Read one full query page set through the current delegate."""
        if self._delegate is None:
            return Err(self._require_client("list-messages"))
        return await self._delegate.list_message_ids(query=query, logger=logger)

    async def search_messages(
        self,
        *,
        query: str,
        logger: Logger,
        max_results: int | None = None,
        page_token: str | None = None,
    ) -> Result[GmailSearchPage, GmailFailure]:
        """Read one page through the current delegate."""
        if self._delegate is None:
            return Err(self._require_client("list-messages"))
        return await self._delegate.search_messages(
            query=query,
            logger=logger,
            max_results=max_results,
            page_token=page_token,
        )

    async def get_message(self, message_id: str) -> Result[GmailMessage, GmailFailure]:
        """Fetch one full message through the current delegate."""
        if self._delegate is None:
            return Err(self._require_client("get-message"))
        return await self._delegate.get_message(message_id)

    async def get_message_preview(
        self, message_id: str
    ) -> Result[GmailMessagePreview, GmailFailure]:
        """Fetch one lightweight message preview through the current delegate."""
        if self._delegate is None:
            return Err(self._require_client("get-message-preview"))
        return await self._delegate.get_message_preview(message_id)

    async def get_raw_message(
        self, message_id: str
    ) -> Result[GmailRawMessage, GmailFailure]:
        """Fetch one raw message through the current delegate."""
        if self._delegate is None:
            return Err(self._require_client("get-raw-message"))
        return await self._delegate.get_raw_message(message_id)

    async def list_labels(self) -> Result[tuple[GmailLabel, ...], GmailFailure]:
        """List labels through the current delegate."""
        if self._delegate is None:
            return Err(self._require_client("list-labels"))
        return await self._delegate.list_labels()

    async def resolve_label_id(self, name: str) -> Result[str | None, GmailFailure]:
        """Resolve one label id through the current delegate."""
        if self._delegate is None:
            return Err(self._require_client("list-labels"))
        return await self._delegate.resolve_label_id(name)

    async def archive(self, message_id: str) -> Result[GmailWriteResult, GmailFailure]:
        """Archive through the current delegate."""
        if self._delegate is None:
            return Err(self._require_client("modify-labels"))
        return await self._delegate.archive(message_id)

    async def label(
        self, message_id: str, label_id: str
    ) -> Result[GmailWriteResult, GmailFailure]:
        """Apply one label through the current delegate."""
        if self._delegate is None:
            return Err(self._require_client("modify-labels"))
        return await self._delegate.label(message_id, label_id)

    async def update_labels(
        self,
        message_id: str,
        *,
        add_label_ids: Sequence[str],
        remove_label_ids: Sequence[str],
    ) -> Result[GmailWriteResult, GmailFailure]:
        """Update labels through the current delegate."""
        if self._delegate is None:
            return Err(self._require_client("modify-labels"))
        return await self._delegate.update_labels(
            message_id,
            add_label_ids=add_label_ids,
            remove_label_ids=remove_label_ids,
        )

    async def trash(self, message_id: str) -> Result[GmailWriteResult, GmailFailure]:
        """Trash through the current delegate."""
        if self._delegate is None:
            return Err(self._require_client("trash-message"))
        return await self._delegate.trash(message_id)


class GoogleGmailAuthBackend:
    """Run shared Google web OAuth and validate one reusable token path."""

    def __init__(
        self,
        config: OAuthConfig,
        *,
        flow_factory: Callable[[], GoogleAuthorizationFlow] | None = None,
        client: ReauthorizableGmailClient | None = None,
    ) -> None:
        self._config = config
        self._client = client
        self._flow = GoogleWebOAuthFlow(config, flow_factory=flow_factory)

    async def check(self) -> Result[bool, GmailAuthFailure]:
        """Validate stored credentials and reconnect the delegate when usable."""
        if not self._config.token_path.exists():
            if self._client is not None:
                self._client.disconnect()
            return Ok(value=False)
        try:
            _ = load_credentials(self._config)
        except Exception:
            if self._client is not None:
                self._client.disconnect()
            return Err(
                GmailAuthFailure(
                    message="Gmail authorization expired. Reconnect Gmail."
                )
            )
        return Ok(value=True)

    async def start(
        self, *, redirect_uri: str
    ) -> Result[GmailAuthorization, GmailAuthFailure]:
        """Create one browser consent request."""
        outcome = await self._flow.start(redirect_uri=redirect_uri)
        if isinstance(outcome, Err):
            return Err(GmailAuthFailure(message=outcome.error.message))
        return Ok(GmailAuthorization(**outcome.value.model_dump()))

    async def complete(
        self, *, authorization_response: str, expected_state: str
    ) -> Result[None, GmailAuthFailure]:
        """Exchange callback and persist refreshed credentials."""
        _ = expected_state
        outcome = await self._flow.complete(
            authorization_response=authorization_response
        )
        if isinstance(outcome, Err):
            return Err(GmailAuthFailure(message=outcome.error.message))
        return Ok(None)


class GoogleGmailAuthService:
    """Expose browser-safe Gmail authorization state over one backend."""

    def __init__(
        self,
        backend: GmailAuthBackend | None,
        *,
        on_authorized: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._backend = backend
        self._expected_state: str | None = None
        self._status = GmailAuthStatus()
        self._on_authorized = on_authorized

    @property
    def configured(self) -> bool:
        """Whether Gmail OAuth is wired for this install."""

        return self._backend is not None

    async def available(self) -> bool:
        """Whether Gmail tooling can be invoked right now."""

        if not self.configured:
            return True
        return (await self.status()).state == "connected"

    async def status(self) -> GmailAuthStatus:
        """Return current status and memoize it for UI polling."""

        if self._status.state == "authorizing" or self._backend is None:
            return self._status
        outcome = await self._backend.check()
        if isinstance(outcome, Err):
            self._status = GmailAuthStatus(
                error=outcome.error.message,
                state="error",
            )
            return self._status
        self._status = GmailAuthStatus(
            state="connected" if outcome.value else "disconnected"
        )
        return self._status

    async def start(self, *, redirect_uri: str) -> GmailAuthStatus:
        """Create a browser consent request and hold expected state."""
        if self._backend is None:
            self._status = GmailAuthStatus(
                error="Gmail authorization is not configured.",
                state="error",
            )
            return self._status
        outcome = await self._backend.start(redirect_uri=redirect_uri)
        if isinstance(outcome, Err):
            self._status = GmailAuthStatus(
                error=outcome.error.message,
                state="error",
            )
            return self._status
        self._expected_state = outcome.value.state
        self._status = GmailAuthStatus(
            authorization_url=outcome.value.authorization_url,
            state="authorizing",
        )
        return self._status

    async def complete(
        self, *, authorization_response: str, state: str
    ) -> Result[GmailAuthStatus, GmailAuthFailure]:
        """Validate state and complete callback exchange."""
        expected_state = self._expected_state
        if self._backend is None or expected_state is None or state != expected_state:
            return Err(
                GmailAuthFailure(
                    kind="state_mismatch",
                    message="invalid Gmail authorization state",
                )
            )
        self._expected_state = None
        outcome = await self._backend.complete(
            authorization_response=authorization_response,
            expected_state=expected_state,
        )
        if isinstance(outcome, Err):
            self._status = GmailAuthStatus(
                error=outcome.error.message,
                state="error",
            )
            return Err(outcome.error)
        if self._on_authorized is not None:
            await self._on_authorized()
        self._status = GmailAuthStatus(state="connected")
        return Ok(self._status)


__all__ = [
    "GmailAuthBackend",
    "GmailAuthFailure",
    "GmailAuthState",
    "GmailAuthStatus",
    "GmailAuthorization",
    "GoogleGmailAuthBackend",
    "GoogleGmailAuthService",
    "ReauthorizableGmailClient",
    "authorization_failed_message",
]
