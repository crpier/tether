"""Shared Google web-app OAuth mechanics for Settings-style callbacks.

This module contains the flow protocol, adapter, and atomic token persistence used
by multiple Google OAuth integrations (YouTube and Gmail). The mechanics are
extracted so each integration only owns what is app-specific: API availability
checks and status/error wording.
"""

from __future__ import annotations

import asyncio
import importlib
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Protocol, cast

from pydantic import BaseModel
from snekok import Err, Ok, Result

from tether.google_oauth import OAuthConfig


class GoogleAuthFailure(BaseModel):
    """A browser-safe failure from the shared Google consent boundary."""

    kind: str = "authorization_failed"
    message: str


class GoogleAuthorization(BaseModel):
    """A pending consent URL and its state."""

    authorization_url: str
    state: str


class SerializableGoogleCredentials(Protocol):
    """Credential types we persist after successful OAuth consent."""

    def to_json(self) -> str:
        """Serialize refreshable credentials without exposing secrets."""
        ...


class GoogleAuthorizationFlow(Protocol):
    """Normalized browser OAuth flow used by integrations."""

    @property
    def credentials(self) -> SerializableGoogleCredentials:
        """Return credentials once token exchange succeeds."""
        ...

    @property
    def redirect_uri(self) -> str:
        """Current callback URI bound into this flow."""
        ...

    @redirect_uri.setter
    def redirect_uri(self, redirect_uri: str) -> None: ...

    def authorization_url(self) -> tuple[str, str]:
        """Return consent URL and generated state."""
        ...

    def fetch_token(self, *, authorization_response: str) -> None:
        """Exchange the authorization response for refreshable credentials."""
        ...


class _GoogleLibraryFlow(Protocol):
    """Untyped `google_auth_oauthlib.flow` shape narrowed to this boundary."""

    credentials: SerializableGoogleCredentials
    redirect_uri: str

    def authorization_url(self, **kwargs: str) -> tuple[str, str]: ...

    def fetch_token(self, *, authorization_response: str) -> None: ...


class _GoogleLibraryFlowFactory(Protocol):
    """Class-level constructor exported by `google_auth_oauthlib.flow.Flow`."""

    def from_client_secrets_file(
        self,
        client_secrets_file: str,
        *,
        autogenerate_code_verifier: bool,
        scopes: Sequence[str],
    ) -> _GoogleLibraryFlow: ...


class _GoogleLibraryFlowAdapter:
    """Normalize flow options required by this project."""

    def __init__(self, flow: _GoogleLibraryFlow) -> None:
        self._flow: _GoogleLibraryFlow = flow

    @property
    def credentials(self) -> SerializableGoogleCredentials:
        return self._flow.credentials

    @property
    def redirect_uri(self) -> str:
        return self._flow.redirect_uri

    @redirect_uri.setter
    def redirect_uri(self, redirect_uri: str) -> None:
        self._flow.redirect_uri = redirect_uri

    def authorization_url(self) -> tuple[str, str]:
        return self._flow.authorization_url(
            access_type="offline",
            prompt="consent",
        )

    def fetch_token(self, *, authorization_response: str) -> None:
        self._flow.fetch_token(authorization_response=authorization_response)


class GoogleWebOAuthFlow:
    """Create, persist, and complete one web OAuth flow instance."""

    def __init__(
        self,
        config: OAuthConfig,
        *,
        flow_factory: Callable[[], GoogleAuthorizationFlow] | None = None,
    ) -> None:
        self.config = config
        self._flow_factory: Callable[[], GoogleAuthorizationFlow] = (
            flow_factory if flow_factory is not None else self._create_google_flow
        )
        self._flow: GoogleAuthorizationFlow | None = None

    async def start(
        self, *, redirect_uri: str
    ) -> Result[GoogleAuthorization, GoogleAuthFailure]:
        """Create one in-progress consent URL and return state."""
        try:
            self._flow = await asyncio.to_thread(self._flow_factory)
            self._flow.redirect_uri = redirect_uri
            authorization_url, state = await asyncio.to_thread(
                self._flow.authorization_url
            )
        except Exception:
            self._flow = None
            return Err(
                GoogleAuthFailure(message="Could not start Google authorization.")
            )
        return Ok(GoogleAuthorization(authorization_url=authorization_url, state=state))

    async def complete(
        self, *, authorization_response: str
    ) -> Result[None, GoogleAuthFailure]:
        """Exchange one callback and persist newly issued credentials."""
        flow = self._flow
        self._flow = None
        if flow is None:
            return Err(GoogleAuthFailure(message="No Google authorization is active."))
        try:
            await asyncio.to_thread(
                flow.fetch_token, authorization_response=authorization_response
            )
            await asyncio.to_thread(
                self._persist_credentials, flow.credentials.to_json()
            )
        except Exception:
            return Err(
                GoogleAuthFailure(message="Google did not complete authorization.")
            )
        return Ok(None)

    def _create_google_flow(self) -> GoogleAuthorizationFlow:
        """Load Google web-flow support only when auth actually starts."""
        module = importlib.import_module("google_auth_oauthlib.flow")
        factory = cast("_GoogleLibraryFlowFactory", module.Flow)
        return _GoogleLibraryFlowAdapter(
            factory.from_client_secrets_file(
                str(self.config.client_secret_path),
                autogenerate_code_verifier=True,
                scopes=self.config.scopes,
            )
        )

    def _persist_credentials(self, credentials_json: str) -> None:
        """Persist credentials via atomic replace with restrictive permissions."""
        token_path = self.config.token_path
        token_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=token_path.parent,
                mode="w",
                encoding="utf-8",
                delete=False,
            ) as temporary_file:
                temp_path = Path(temporary_file.name)
                _ = temporary_file.write(credentials_json)
            temp_path.chmod(0o600)
            _ = temp_path.replace(token_path)
        finally:
            if temp_path is not None and temp_path.exists():
                _ = temp_path.unlink()


__all__ = [
    "GoogleAuthFailure",
    "GoogleAuthorization",
    "GoogleAuthorizationFlow",
    "GoogleWebOAuthFlow",
]
