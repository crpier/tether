"""Typed async HTTP boundaries shared by Readwise Export and Reader."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal, Protocol, cast

import httpx2
from snekok.result import Err, Ok, Result

_DEFAULT_BASE_URL = "https://readwise.io"
_EXPORT_PATH = "/api/v2/export/"
_AUTH_PATH = "/api/v2/auth/"
_LIST_PATH = "/api/v3/list/"
_READER_LIMIT = 100

type ReadwiseOperation = Literal["export", "list", "verify-token"]
"""Provider operation used as stable failure context."""


class ReadwiseConfigurationError(Exception):
    """Raised when an HTTP transport is built without an API key."""


@dataclass(frozen=True, slots=True)
class ReadwiseAuthenticationFailure:
    """A provider operation rejected by the configured token."""

    operation: ReadwiseOperation
    status_code: int


@dataclass(frozen=True, slots=True)
class ReadwiseHttpFailure:
    """A provider operation failed with a non-success HTTP response."""

    operation: ReadwiseOperation
    retry_after: timedelta | None
    status_code: int


@dataclass(frozen=True, slots=True)
class ReadwiseNetworkFailure:
    """A provider operation failed before receiving an HTTP response."""

    message: str
    operation: ReadwiseOperation


@dataclass(frozen=True, slots=True)
class ReadwiseProtocolFailure:
    """A successful provider response violated the expected payload contract."""

    operation: ReadwiseOperation


@dataclass(frozen=True, slots=True)
class ReadwiseRateLimitFailure:
    """A provider operation remained throttled after bounded retries."""

    operation: ReadwiseOperation
    retry_after: timedelta | None


type ReadwiseFailure = (
    ReadwiseAuthenticationFailure
    | ReadwiseHttpFailure
    | ReadwiseNetworkFailure
    | ReadwiseProtocolFailure
    | ReadwiseRateLimitFailure
)
"""Expected provider failures shared by both Readwise APIs."""


@dataclass(frozen=True, slots=True)
class ReadwiseResponse:
    """One normalized Readwise HTTP response."""

    payload: Mapping[str, object]
    retry_after: timedelta | None = None
    status_code: int = 200


class ReadwiseTransport(Protocol):
    """HTTP port consumed by the Readwise Export client."""

    async def aclose(self) -> None:
        """Close transport-owned network resources."""
        ...

    async def fetch_export(
        self,
        *,
        updated_after: datetime | None,
        page_cursor: str | None,
        include_deleted: bool,
    ) -> Result[ReadwiseResponse, ReadwiseNetworkFailure]:
        """Fetch one export page."""
        ...

    async def verify_token(
        self,
    ) -> Result[ReadwiseResponse, ReadwiseNetworkFailure]:
        """Fetch token validity from the authentication endpoint."""
        ...


class ReaderTransport(Protocol):
    """HTTP port consumed by the Reader document client."""

    async def aclose(self) -> None:
        """Close transport-owned network resources."""
        ...

    async def fetch_list(
        self,
        *,
        updated_after: datetime | None,
        category: str,
        page_cursor: str | None,
    ) -> Result[ReadwiseResponse, ReadwiseNetworkFailure]:
        """Fetch one Reader document page."""
        ...


def _retry_after_seconds(headers: Mapping[str, str]) -> timedelta | None:
    """Parse a delta-seconds `Retry-After` header when present."""
    value = headers.get("Retry-After") or headers.get("retry-after")
    if value is None:
        return None
    text = str(value).strip()
    return timedelta(seconds=int(text)) if text.isdigit() else None


def _from_httpx(response: httpx2.Response) -> ReadwiseResponse:
    """Decode one response while leaving payload validation to the client."""
    try:
        body = response.json()
    except ValueError:
        body = {}
    return ReadwiseResponse(
        payload=(
            cast("Mapping[str, object]", body) if isinstance(body, Mapping) else {}
        ),
        retry_after=_retry_after_seconds(response.headers),
        status_code=response.status_code,
    )


class HttpReadwiseTransport:
    """Reusable async transport for Export and token verification."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = _DEFAULT_BASE_URL,
        http_transport: httpx2.AsyncBaseTransport | None = None,
        timeout: timedelta | None = None,
    ) -> None:
        if not api_key:
            message = "Readwise API key is required to build the HTTP transport"
            raise ReadwiseConfigurationError(message)
        self._client: httpx2.AsyncClient = httpx2.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Token {api_key}"},
            timeout=(timeout or timedelta(seconds=30)).total_seconds(),
            transport=http_transport,
        )

    async def fetch_export(
        self,
        *,
        updated_after: datetime | None,
        page_cursor: str | None,
        include_deleted: bool,
    ) -> Result[ReadwiseResponse, ReadwiseNetworkFailure]:
        """Fetch one Export page without raising request failures."""
        params: dict[str, str] = {}
        if updated_after is not None:
            params["updatedAfter"] = updated_after.isoformat()
        if include_deleted:
            params["includeDeleted"] = "true"
        if page_cursor is not None:
            params["pageCursor"] = page_cursor
        return await self._get(_EXPORT_PATH, operation="export", params=params)

    async def verify_token(
        self,
    ) -> Result[ReadwiseResponse, ReadwiseNetworkFailure]:
        """Verify the configured token without raising request failures."""
        return await self._get(_AUTH_PATH, operation="verify-token")

    async def aclose(self) -> None:
        """Close the reusable HTTP connection pool."""
        await self._client.aclose()

    async def _get(
        self,
        path: str,
        *,
        operation: ReadwiseOperation,
        params: Mapping[str, str] | None = None,
    ) -> Result[ReadwiseResponse, ReadwiseNetworkFailure]:
        try:
            response = await self._client.get(path, params=dict(params or {}))
        except httpx2.RequestError as error:
            return Err(ReadwiseNetworkFailure(message=str(error), operation=operation))
        return Ok(_from_httpx(response))


class HttpReaderTransport:
    """Reusable async transport for Reader document pages."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = _DEFAULT_BASE_URL,
        http_transport: httpx2.AsyncBaseTransport | None = None,
        timeout: timedelta | None = None,
    ) -> None:
        if not api_key:
            message = "Readwise API key is required to build the Reader transport"
            raise ReadwiseConfigurationError(message)
        self._client: httpx2.AsyncClient = httpx2.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Token {api_key}"},
            timeout=(timeout or timedelta(seconds=30)).total_seconds(),
            transport=http_transport,
        )

    async def fetch_list(
        self,
        *,
        updated_after: datetime | None,
        category: str,
        page_cursor: str | None,
    ) -> Result[ReadwiseResponse, ReadwiseNetworkFailure]:
        """Fetch one Reader page without raising request failures."""
        params: dict[str, str] = {
            "category": category,
            "limit": str(_READER_LIMIT),
        }
        if updated_after is not None:
            params["updatedAfter"] = updated_after.isoformat()
        if page_cursor is not None:
            params["pageCursor"] = page_cursor
        try:
            response = await self._client.get(_LIST_PATH, params=params)
        except httpx2.RequestError as error:
            return Err(ReadwiseNetworkFailure(message=str(error), operation="list"))
        return Ok(_from_httpx(response))

    async def aclose(self) -> None:
        """Close the reusable HTTP connection pool."""
        await self._client.aclose()


__all__ = [
    "HttpReaderTransport",
    "HttpReadwiseTransport",
    "ReaderTransport",
    "ReadwiseAuthenticationFailure",
    "ReadwiseConfigurationError",
    "ReadwiseFailure",
    "ReadwiseHttpFailure",
    "ReadwiseNetworkFailure",
    "ReadwiseOperation",
    "ReadwiseProtocolFailure",
    "ReadwiseRateLimitFailure",
    "ReadwiseResponse",
    "ReadwiseTransport",
]
