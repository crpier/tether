"""OAuth-backed `GmailTransport`, over the shared Google installed-app flow.

The Gmail gate reuses the exact OAuth mechanics `tether.youtube_oauth` already
built for YouTube: `OAuthConfig`, the installed-app consent flow, and
`load_credentials`'s cached-token load + scope check + auto-refresh. Rather
than going through the `googleapiclient` discovery client (as the YouTube
adapter does), this transport calls the Gmail REST API directly over HTTP.
The narrow transport covers message listing, reads, labels, archive, and trash,
so a thin `httpx2` client with a Bearer token is simpler than standing up a
second discovery resource. `load_credentials` is re-run before every request (a cheap
local JSON read that only touches the network when the cached token has
expired), so a refreshed token is always used and persisted back to disk.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from datetime import timedelta
from types import TracebackType
from typing import Any, Self, cast

import httpx2
from snekok import Err, Ok, Result

from tether.gmail_client import (
    GmailAuthenticationFailure,
    GmailNetworkFailure,
    GmailOperation,
    GmailResponse,
    GmailTransportFailure,
)
from tether.youtube_oauth import OAuthConfig, YouTubeAuthError, load_credentials

GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
"""Read-only access to a user's Gmail messages and labels."""

GMAIL_MODIFY_SCOPE = "https://www.googleapis.com/auth/gmail.modify"
"""Read/write access to labels and message state (archive, label, trash) — the
scope the backlog-purge write path needs. It does not subsume message reads for
listing purposes cleanly, so the auth config requests it alongside
`GMAIL_READONLY_SCOPE`; a token minted before this scope was added fails a write
with a `403` until the user re-runs `just gmail-auth` (see `docs/development.md`)."""

_DEFAULT_BASE_URL = "https://gmail.googleapis.com"
_USER_ID = "me"
"""The Gmail API's special user id for the authenticated account."""


def _load_token(config: OAuthConfig) -> str:
    """Load one refreshed OAuth token from the shared credential store."""
    credentials = load_credentials(config)
    return cast("str", cast("Any", credentials).token)


class HttpGmailTransport:
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
        http_transport: httpx2.AsyncBaseTransport | None = None,
        timeout: timedelta | None = None,
        token_loader: Callable[[OAuthConfig], str] = _load_token,
    ) -> None:
        self._config: OAuthConfig = config
        self._base_url: str = base_url
        self._timeout: timedelta = timeout or timedelta(seconds=30)
        self._token_loader: Callable[[OAuthConfig], str] = token_loader
        self._client: httpx2.AsyncClient = httpx2.AsyncClient(
            base_url=base_url,
            timeout=self._timeout.total_seconds(),
            transport=http_transport,
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
    ) -> Result[GmailResponse, GmailTransportFailure]:
        params: dict[str, str] = {"q": query}
        if page_token is not None:
            params["pageToken"] = page_token
        return await self._get(
            f"/gmail/v1/users/{_USER_ID}/messages",
            operation="list-messages",
            params=params,
        )

    async def get_message(
        self, message_id: str
    ) -> Result[GmailResponse, GmailTransportFailure]:
        return await self._get(
            f"/gmail/v1/users/{_USER_ID}/messages/{message_id}",
            operation="get-message",
            params={"format": "full"},
        )

    async def list_labels(
        self,
    ) -> Result[GmailResponse, GmailTransportFailure]:
        return await self._get(
            f"/gmail/v1/users/{_USER_ID}/labels", operation="list-labels"
        )

    async def modify_labels(
        self,
        message_id: str,
        *,
        add_label_ids: Sequence[str],
        remove_label_ids: Sequence[str],
    ) -> Result[GmailResponse, GmailTransportFailure]:
        return await self._post(
            f"/gmail/v1/users/{_USER_ID}/messages/{message_id}/modify",
            operation="modify-labels",
            json_body={
                "addLabelIds": list(add_label_ids),
                "removeLabelIds": list(remove_label_ids),
            },
        )

    async def trash_message(
        self, message_id: str
    ) -> Result[GmailResponse, GmailTransportFailure]:
        return await self._post(
            f"/gmail/v1/users/{_USER_ID}/messages/{message_id}/trash",
            operation="trash-message",
            json_body={},
        )

    async def _get(
        self,
        path: str,
        *,
        operation: GmailOperation,
        params: dict[str, str] | None = None,
    ) -> Result[GmailResponse, GmailTransportFailure]:
        token_result = await self._token(operation)
        if isinstance(token_result, Err):
            return Err(token_result.error)
        try:
            response = await self._client.get(
                path,
                params=params or {},
                headers={"Authorization": f"Bearer {token_result.value}"},
            )
        except httpx2.RequestError as error:
            return Err(GmailNetworkFailure(message=str(error), operation=operation))
        return Ok(_from_httpx(response))

    async def _post(
        self,
        path: str,
        *,
        operation: GmailOperation,
        json_body: dict[str, object],
    ) -> Result[GmailResponse, GmailTransportFailure]:
        token_result = await self._token(operation)
        if isinstance(token_result, Err):
            return Err(token_result.error)
        try:
            response = await self._client.post(
                path,
                json=json_body,
                headers={"Authorization": f"Bearer {token_result.value}"},
            )
        except httpx2.RequestError as error:
            return Err(GmailNetworkFailure(message=str(error), operation=operation))
        return Ok(_from_httpx(response))

    async def _token(
        self, operation: GmailOperation
    ) -> Result[str, GmailAuthenticationFailure]:
        """Load and refresh the cached credential as a typed auth outcome."""
        try:
            token = await asyncio.to_thread(self._token_loader, self._config)
        except YouTubeAuthError as error:
            return Err(
                GmailAuthenticationFailure(message=str(error), operation=operation)
            )
        return Ok(token)


def _from_httpx(response: httpx2.Response) -> GmailResponse:
    """Normalize an HTTP response while leaving validation to the client."""
    try:
        body = response.json()
    except ValueError:
        body = {}
    payload = cast("dict[str, object]", body) if isinstance(body, dict) else {}
    return GmailResponse(status_code=int(response.status_code), payload=payload)


__all__ = ["GMAIL_MODIFY_SCOPE", "GMAIL_READONLY_SCOPE", "HttpGmailTransport"]
