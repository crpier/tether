"""OAuth-backed `GmailTransport`, over the shared Google installed-app flow.

The Gmail gate reuses the exact OAuth mechanics `tether.youtube_oauth` already
built for YouTube: `OAuthConfig`, the installed-app consent flow, and
`load_credentials`'s cached-token load + scope check + auto-refresh. Rather
than going through the `googleapiclient` discovery client (as the YouTube
adapter does), this transport calls the Gmail REST API directly over HTTP —
`GmailTransport` only needs three plain calls (list, get, list-labels), so a
thin `httpx2` client with a Bearer token is simpler than standing up a second
discovery resource. `load_credentials` is re-run before every request (a cheap
local JSON read that only touches the network when the cached token has
expired), so a refreshed token is always used and persisted back to disk.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from types import TracebackType
from typing import Any, Self, cast

import httpx2

from tether.gmail import GmailResponse, GmailTransport
from tether.youtube_oauth import OAuthConfig, load_credentials

GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
"""Read-only access to a user's Gmail messages and labels."""

_DEFAULT_BASE_URL = "https://gmail.googleapis.com"
_USER_ID = "me"
"""The Gmail API's special user id for the authenticated account."""


class HttpGmailTransport(GmailTransport):
    """The production `GmailTransport`: a thin httpx client over the Gmail v1 API.

    Holds the OAuth config (not a live credentials object) so every call
    re-validates and, if needed, refreshes the cached token through
    `load_credentials` before it is used — mirroring how the YouTube adapter
    refreshes on every discovery-client call, just without that client.

    Holds a single `httpx2.AsyncClient` for the transport's own lifetime
    (created eagerly at construction) rather than opening one per call — the
    transport is a long-lived, boot-to-shutdown object, so a fresh client per
    request only added connection-setup overhead with no isolation benefit.
    Callers own its lifecycle: use it as an `async with` context manager, or
    call `aclose` explicitly (mirrors `PiRuntime`'s `__aenter__`/`__aexit__`).

    >>> async with HttpGmailTransport(config) as transport:
    ...     response = await transport.list_messages(query="-in:spam", page_token=None)
    ...     response.status_code
    200
    """

    def __init__(
        self,
        config: OAuthConfig,
        *,
        base_url: str = _DEFAULT_BASE_URL,
        timeout: timedelta | None = None,
    ) -> None:
        self._config: OAuthConfig = config
        self._base_url: str = base_url
        self._timeout: timedelta = timeout or timedelta(seconds=30)
        self._client: httpx2.AsyncClient = httpx2.AsyncClient(
            base_url=base_url, timeout=self._timeout.total_seconds()
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the held httpx client; safe to call once at shutdown."""
        await self._client.aclose()

    async def list_messages(
        self, *, query: str, page_token: str | None
    ) -> GmailResponse:
        params: dict[str, str] = {"q": query}
        if page_token is not None:
            params["pageToken"] = page_token
        return await self._get(f"/gmail/v1/users/{_USER_ID}/messages", params=params)

    async def get_message(self, message_id: str) -> GmailResponse:
        return await self._get(
            f"/gmail/v1/users/{_USER_ID}/messages/{message_id}",
            params={"format": "full"},
        )

    async def list_labels(self) -> GmailResponse:
        return await self._get(f"/gmail/v1/users/{_USER_ID}/labels")

    async def _get(
        self, path: str, *, params: dict[str, str] | None = None
    ) -> GmailResponse:
        credentials = await asyncio.to_thread(load_credentials, self._config)
        # The Google client libraries ship no type stubs; `.token` is present on
        # the real `google.oauth2.credentials.Credentials` but not declared on
        # the reduced `GoogleCredentials` protocol `load_credentials` returns.
        token = cast("Any", credentials).token
        response = await self._client.get(
            path,
            params=params or {},
            headers={"Authorization": f"Bearer {token}"},
        )
        return _from_httpx(response)


def _from_httpx(response: Any) -> GmailResponse:
    """Normalize an httpx response into a `GmailResponse` (decode JSON best-effort)."""
    try:
        body = response.json()
    except Exception:
        body = {}
    payload = cast("dict[str, object]", body) if isinstance(body, dict) else {}
    return GmailResponse(status_code=int(response.status_code), payload=payload)


__all__ = ["GMAIL_READONLY_SCOPE", "HttpGmailTransport"]
