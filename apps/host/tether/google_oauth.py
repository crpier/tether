"""Shared Google OAuth mechanics for the YouTube and Gmail Integrations.

Infrastructure plumbing, deliberately outside both Integrations (ADR-0025):
installed-app OAuth flow, cached-token load/refresh with scope validation, and
the lazily-imported Google client shims. It owns no domain types — each
Integration passes its own scopes and builds its own API adapters on top.

The Google client libraries are imported lazily so the rest of Tether runs
without them installed; the import path raises a clear
`GoogleClientUnavailableError` when they are missing.
"""

from __future__ import annotations

import importlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Protocol, cast, runtime_checkable

_GOOGLE_INSTALL_HINT = (
    "Google client libraries are not installed. Install them with "
    "`uv pip install google-api-python-client google-auth-oauthlib` "
    "(or add them to the host dependencies) and re-run."
)


class GoogleClientUnavailableError(Exception):
    """Raised when the lazily-imported Google client libraries are missing."""


class GoogleAuthError(Exception):
    """Raised when a stored token is absent, missing a scope, or unrecoverable.

    The message instructs the user to delete the token and re-run the auth
    recipe; a half-authorized or revoked token fails loudly here rather than
    mid-sync.
    """


@runtime_checkable
class GoogleCredentials(Protocol):
    """The subset of `google.oauth2.credentials.Credentials` the adapters use."""

    @property
    def valid(self) -> bool:
        """Whether the token is currently usable (present and not expired)."""
        ...

    @property
    def expired(self) -> bool:
        """Whether the token has passed its expiry."""
        ...

    @property
    def refresh_token(self) -> str | None:
        """The refresh token, if the grant issued one."""
        ...

    @property
    def scopes(self) -> Sequence[str] | None:
        """The scopes the token was granted."""
        ...

    def refresh(self, request: object, /) -> None:
        """Refresh the access token in place, or raise on an unrecoverable grant."""
        ...

    def to_json(self) -> str:
        """Serialize the credentials to the cached-token JSON form."""
        ...


type CredentialsFromInfo = Callable[
    [Mapping[str, object], Sequence[str]], GoogleCredentials
]
"""Builds credentials from cached-token info + the required scopes."""

type RequestFactory = Callable[[], object]
"""Builds the transport request object a credentials refresh needs."""


@dataclass(frozen=True, slots=True)
class OAuthConfig:
    """Paths + toggles for the OAuth flow and token cache.

    `token_path` and `client_secret_path` default under the data dir at the call
    site; `scopes` are validated against every loaded token; `no_browser` prints
    the auth URL instead of opening a browser, for authorizing on a headless box.
    """

    token_path: Path
    client_secret_path: Path
    scopes: tuple[str, ...]
    no_browser: bool = False


def import_google_module(name: str) -> ModuleType:
    """Import a Google client module lazily, mapping absence to a clear error."""
    try:
        return importlib.import_module(name)
    except ImportError as error:
        raise GoogleClientUnavailableError(_GOOGLE_INSTALL_HINT) from error


# The Google client libraries ship no type stubs, so their attributes type as
# `Any`; each cast below pins one to the call signature the adapter relies on.
def _default_credentials_from_info() -> CredentialsFromInfo:
    module = import_google_module("google.oauth2.credentials")
    return cast("CredentialsFromInfo", module.Credentials.from_authorized_user_info)


def _default_request_factory() -> RequestFactory:
    module = import_google_module("google.auth.transport.requests")
    return cast("RequestFactory", module.Request)


def _require_scopes(credentials: GoogleCredentials, required: Sequence[str]) -> None:
    """Reject a token missing any required scope, before it is used for sync."""
    granted = set(credentials.scopes or ())
    missing = [scope for scope in required if scope not in granted]
    if missing:
        message = (
            f"stored Google token is missing required scope(s): "
            f"{', '.join(missing)}. Re-run the auth recipe to re-authorize."
        )
        raise GoogleAuthError(message)


def load_credentials(
    config: OAuthConfig,
    *,
    credentials_from_info: CredentialsFromInfo | None = None,
    request_factory: RequestFactory | None = None,
) -> GoogleCredentials:
    """Load cached credentials, validate scopes, and refresh on expiry.

    Raises `GoogleAuthError` when the token is absent, missing a required scope,
    cannot be refreshed (revoked/expired with no usable refresh token), or the
    refresh itself fails — every case actionable by re-running the auth recipe. A
    successful refresh is written back to `token_path` so the next run reuses it.

    The Google-backed builders are injectable so the mechanics test against fakes
    without importing the real libraries or hitting the network.
    """
    if not config.token_path.exists():
        message = (
            f"no cached Google token at {config.token_path}; "
            f"run the auth recipe to authorize."
        )
        raise GoogleAuthError(message)
    # `json.loads` returns `Any`; the cached token is always a JSON object here.
    info = cast(
        "Mapping[str, object]", json.loads(config.token_path.read_text("utf-8"))
    )
    build = credentials_from_info or _default_credentials_from_info()
    credentials = build(info, list(config.scopes))
    _require_scopes(credentials, config.scopes)
    if credentials.valid:
        return credentials
    if credentials.refresh_token is None:
        message = (
            f"cached Google token at {config.token_path} is expired and cannot "
            f"refresh. Delete it and re-run the auth recipe to re-authorize."
        )
        raise GoogleAuthError(message)
    request = (request_factory or _default_request_factory())()
    try:
        credentials.refresh(request)
    except Exception as error:
        message = (
            f"cached Google token at {config.token_path} could not be refreshed "
            f"(revoked or unrecoverable). Delete it and re-run the auth recipe "
            f"to re-authorize."
        )
        raise GoogleAuthError(message) from error
    _ = config.token_path.write_text(credentials.to_json(), encoding="utf-8")
    return credentials


class _InstalledAppFlow(Protocol):
    """The subset of `InstalledAppFlow` the bootstrap drives."""

    def run_local_server(self, *, port: int, open_browser: bool) -> GoogleCredentials:
        """Run the local-server consent flow and return the granted credentials."""
        ...


class _InstalledAppFlowFactory(Protocol):
    """The `InstalledAppFlow` class, entered via its client-secrets constructor."""

    def from_client_secrets_file(
        self, client_secrets_file: str, scopes: Sequence[str], /
    ) -> _InstalledAppFlow:
        """Build a flow from a downloaded Desktop-app client-secret JSON."""
        ...


def _default_installed_app_flow() -> _InstalledAppFlowFactory:
    module = import_google_module("google_auth_oauthlib.flow")
    # The Google libraries ship no type stubs, so the class types as `Any`.
    return cast("_InstalledAppFlowFactory", module.InstalledAppFlow)


def run_auth_flow(config: OAuthConfig) -> GoogleCredentials:
    """Run the installed-app OAuth flow once and cache the token to disk.

    Opens the browser on an ephemeral local port, or — in `no_browser` mode —
    prints the authorization URL for a headless box. Requires the OAuth client
    secret JSON to already be in place.
    """
    if not config.client_secret_path.exists():
        message = (
            f"no OAuth client secret at {config.client_secret_path}; download a "
            f"Desktop-app OAuth client JSON from the Google Cloud Console and "
            f"place it there."
        )
        raise GoogleAuthError(message)
    flow_cls = _default_installed_app_flow()
    flow = flow_cls.from_client_secrets_file(
        str(config.client_secret_path), list(config.scopes)
    )
    credentials = flow.run_local_server(port=0, open_browser=not config.no_browser)
    config.token_path.parent.mkdir(parents=True, exist_ok=True)
    _ = config.token_path.write_text(credentials.to_json(), encoding="utf-8")
    return credentials
