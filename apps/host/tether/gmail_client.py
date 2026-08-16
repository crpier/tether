"""Typed Gmail API client and transport contracts."""

from __future__ import annotations

import base64
import binascii
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol, cast

from snekok import Err, Ok, Result

from tether.structured_logging import Logger

_BODY_TRUNCATE_CHARS = 4_000
_HTTP_FORBIDDEN = 403
_HTTP_NOT_FOUND = 404
_HTTP_OK = 200
_HTTP_UNAUTHORIZED = 401
_INBOX_LABEL = "INBOX"
_TRASH_LABEL = "TRASH"

_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")

type GmailOperation = Literal[
    "get-message",
    "list-labels",
    "list-messages",
    "modify-labels",
    "trash-message",
]
"""Provider operation used as stable failure context."""

type GmailWriteOutcome = Literal["done", "already", "gone"]
"""Terminal state of one idempotent mailbox write."""


@dataclass(frozen=True, slots=True)
class GmailAuthenticationFailure:
    """A Gmail operation rejected the configured credentials."""

    message: str
    operation: GmailOperation
    status_code: int | None = None


@dataclass(frozen=True, slots=True)
class GmailHttpFailure:
    """A Gmail operation returned an unexpected HTTP status."""

    operation: GmailOperation
    status_code: int


@dataclass(frozen=True, slots=True)
class GmailNetworkFailure:
    """A Gmail operation failed before receiving an HTTP response."""

    message: str
    operation: GmailOperation


@dataclass(frozen=True, slots=True)
class GmailProtocolFailure:
    """A successful Gmail response violated the expected payload contract."""

    message: str
    operation: GmailOperation


type GmailFailure = (
    GmailAuthenticationFailure
    | GmailHttpFailure
    | GmailNetworkFailure
    | GmailProtocolFailure
)
"""Expected Gmail provider failures."""

type GmailTransportFailure = GmailAuthenticationFailure | GmailNetworkFailure
"""Failures that can occur before client-level response classification."""


@dataclass(frozen=True, slots=True)
class GmailResponse:
    """One normalized Gmail HTTP response."""

    payload: Mapping[str, object]
    status_code: int


@dataclass(frozen=True, slots=True)
class GmailWriteResult:
    """The terminal result of one idempotent mailbox write."""

    outcome: GmailWriteOutcome
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class GmailMessage:
    """One validated and parsed Gmail message."""

    body_text: str
    date_header: str
    from_header: str
    internal_date: datetime
    label_ids: tuple[str, ...]
    message_id: str
    subject: str
    thread_id: str


class GmailTransport(Protocol):
    """Async HTTP port consumed by `GmailClient`."""

    async def get_message(
        self, message_id: str
    ) -> Result[GmailResponse, GmailTransportFailure]:
        """Fetch one message's full payload."""
        ...

    async def list_labels(
        self,
    ) -> Result[GmailResponse, GmailTransportFailure]:
        """Fetch account labels."""
        ...

    async def list_messages(
        self, *, query: str, page_token: str | None
    ) -> Result[GmailResponse, GmailTransportFailure]:
        """Fetch one page of message identities."""
        ...

    async def modify_labels(
        self,
        message_id: str,
        *,
        add_label_ids: Sequence[str],
        remove_label_ids: Sequence[str],
    ) -> Result[GmailResponse, GmailTransportFailure]:
        """Add and remove labels on one message."""
        ...

    async def trash_message(
        self, message_id: str
    ) -> Result[GmailResponse, GmailTransportFailure]:
        """Move one message to Trash."""
        ...


def _debug(logger: Logger, event: str, **context: object) -> None:
    logger.debug(event, **context)


def _decode_base64url(data: str) -> str:
    """Decode an unpadded Gmail body part, tolerating malformed content."""
    padded = data + "=" * (-len(data) % 4)
    try:
        return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")
    except binascii.Error:
        return ""


def _strip_html(html_body: str) -> str:
    """Strip tags and collapse whitespace from a fallback HTML body."""
    return _WHITESPACE_RE.sub(" ", _TAG_RE.sub(" ", html_body)).strip()


def _walk_body_parts(part: Mapping[str, object]) -> tuple[str | None, str | None]:
    """Find the first plain and HTML leaves in a MIME part tree."""
    mime_type = part.get("mimeType")
    body = part.get("body")
    encoded = (
        cast("Mapping[str, object]", body).get("data")
        if isinstance(body, Mapping)
        else None
    )
    if isinstance(encoded, str) and encoded:
        if mime_type == "text/plain":
            return _decode_base64url(encoded), None
        if mime_type == "text/html":
            return None, _decode_base64url(encoded)
    plain: str | None = None
    html_body: str | None = None
    children = part.get("parts")
    if isinstance(children, list):
        for child in cast("list[object]", children):
            if not isinstance(child, Mapping):
                continue
            child_plain, child_html = _walk_body_parts(
                cast("Mapping[str, object]", child)
            )
            plain = plain or child_plain
            html_body = html_body or child_html
    return plain, html_body


def _extract_body(payload: Mapping[str, object]) -> str:
    """Prefer plain text, then stripped HTML, and bound prompt input size."""
    plain, html_body = _walk_body_parts(payload)
    return (plain or _strip_html(html_body or ""))[:_BODY_TRUNCATE_CHARS]


def _header(headers: list[object], name: str) -> str:
    """Return one case-insensitive message header value."""
    for header in headers:
        if not isinstance(header, Mapping):
            continue
        header_mapping = cast("Mapping[str, object]", header)
        header_name = header_mapping.get("name")
        header_value = header_mapping.get("value")
        if (
            isinstance(header_name, str)
            and header_name.lower() == name.lower()
            and isinstance(header_value, str)
        ):
            return header_value
    return ""


def _parse_message(
    payload: Mapping[str, object], *, operation: GmailOperation
) -> Result[GmailMessage, GmailProtocolFailure]:
    """Validate known message fields before constructing the domain value."""
    message_id = payload.get("id")
    thread_id = payload.get("threadId")
    internal_date = payload.get("internalDate")
    label_ids = payload.get("labelIds")
    mime_payload = payload.get("payload")
    if (
        not isinstance(message_id, str)
        or not message_id
        or not isinstance(thread_id, str)
        or not thread_id
        or not isinstance(internal_date, str)
        or not internal_date.isdigit()
        or not isinstance(label_ids, list)
        or any(not isinstance(label, str) for label in cast("list[object]", label_ids))
        or not isinstance(mime_payload, Mapping)
    ):
        return Err(
            GmailProtocolFailure(
                message="message payload has invalid required fields",
                operation=operation,
            )
        )
    mime_mapping = cast("Mapping[str, object]", mime_payload)
    headers = mime_mapping.get("headers")
    if not isinstance(headers, list):
        return Err(
            GmailProtocolFailure(
                message="message payload has invalid headers", operation=operation
            )
        )
    return Ok(
        GmailMessage(
            body_text=_extract_body(mime_mapping),
            date_header=_header(cast("list[object]", headers), "Date"),
            from_header=_header(cast("list[object]", headers), "From"),
            internal_date=datetime.fromtimestamp(int(internal_date) / 1000, tz=UTC),
            label_ids=tuple(cast("list[str]", label_ids)),
            message_id=message_id,
            subject=_header(cast("list[object]", headers), "Subject"),
            thread_id=thread_id,
        )
    )


def _response_failure(
    response: GmailResponse, *, operation: GmailOperation
) -> GmailAuthenticationFailure | GmailHttpFailure | None:
    """Classify non-success statuses without changing successful payloads."""
    if response.status_code == _HTTP_OK:
        return None
    if response.status_code in {_HTTP_UNAUTHORIZED, _HTTP_FORBIDDEN}:
        return GmailAuthenticationFailure(
            message=f"Gmail {operation} returned {response.status_code}",
            operation=operation,
            status_code=response.status_code,
        )
    return GmailHttpFailure(operation=operation, status_code=response.status_code)


class GmailClient:
    """Paginate, validate, and mutate Gmail through typed results."""

    def __init__(self, transport: GmailTransport) -> None:
        self.transport: GmailTransport = transport

    async def list_message_ids(  # noqa: PLR0911 - provider failures exit explicitly
        self, *, query: str, logger: Logger
    ) -> Result[list[str], GmailFailure]:
        """Walk every page of a query and validate each message identity."""
        _debug(logger, "Listing Gmail messages", query=query)
        message_ids: list[str] = []
        page_token: str | None = None
        while True:
            transported = await self.transport.list_messages(
                query=query, page_token=page_token
            )
            if isinstance(transported, Err):
                return Err(transported.error)
            response = transported.value
            if failure := _response_failure(response, operation="list-messages"):
                return Err(failure)
            messages = response.payload.get("messages", [])
            if not isinstance(messages, list):
                return Err(
                    GmailProtocolFailure(
                        message="message listing has invalid messages",
                        operation="list-messages",
                    )
                )
            for entry in cast("list[object]", messages):
                if not isinstance(entry, Mapping):
                    return Err(
                        GmailProtocolFailure(
                            message="message listing has an invalid identity",
                            operation="list-messages",
                        )
                    )
                identity = cast("Mapping[str, object]", entry).get("id")
                if not isinstance(identity, str):
                    return Err(
                        GmailProtocolFailure(
                            message="message listing has an invalid identity",
                            operation="list-messages",
                        )
                    )
                message_ids.append(identity)
            next_token = response.payload.get("nextPageToken")
            if next_token is not None and not isinstance(next_token, str):
                return Err(
                    GmailProtocolFailure(
                        message="message listing has an invalid page token",
                        operation="list-messages",
                    )
                )
            page_token = next_token
            if not page_token:
                _debug(
                    logger,
                    "Gmail message listing completed",
                    result_count=len(message_ids),
                )
                return Ok(message_ids)

    async def get_message(self, message_id: str) -> Result[GmailMessage, GmailFailure]:
        """Fetch and validate one message."""
        transported = await self.transport.get_message(message_id)
        if isinstance(transported, Err):
            return Err(transported.error)
        response = transported.value
        if failure := _response_failure(response, operation="get-message"):
            return Err(failure)
        return _parse_message(response.payload, operation="get-message")

    async def resolve_label_id(  # noqa: PLR0911 - provider failures exit explicitly
        self, name: str
    ) -> Result[str | None, GmailFailure]:
        """Resolve a display name to a label id."""
        transported = await self.transport.list_labels()
        if isinstance(transported, Err):
            return Err(transported.error)
        response = transported.value
        if failure := _response_failure(response, operation="list-labels"):
            return Err(failure)
        labels = response.payload.get("labels")
        if not isinstance(labels, list):
            return Err(
                GmailProtocolFailure(
                    message="label listing has invalid labels",
                    operation="list-labels",
                )
            )
        for label in cast("list[object]", labels):
            if not isinstance(label, Mapping):
                return Err(
                    GmailProtocolFailure(
                        message="label listing has an invalid label",
                        operation="list-labels",
                    )
                )
            label_mapping = cast("Mapping[str, object]", label)
            label_id = label_mapping.get("id")
            label_name = label_mapping.get("name")
            if not isinstance(label_id, str) or not isinstance(label_name, str):
                return Err(
                    GmailProtocolFailure(
                        message="label listing has an invalid label",
                        operation="list-labels",
                    )
                )
            if label_name == name:
                return Ok(label_id)
        return Ok(None)

    async def archive(self, message_id: str) -> Result[GmailWriteResult, GmailFailure]:
        """Remove `INBOX` when the message still needs archiving."""
        fetched = await self._get_or_none(message_id)
        if isinstance(fetched, Err):
            return Err(fetched.error)
        if fetched.value is None:
            return Ok(GmailWriteResult("gone", "message no longer exists"))
        if _INBOX_LABEL not in fetched.value.label_ids:
            return Ok(GmailWriteResult("already", "message already archived"))
        return await self._modify_labels(
            message_id, add_label_ids=(), remove_label_ids=(_INBOX_LABEL,)
        )

    async def label(
        self, message_id: str, label_id: str
    ) -> Result[GmailWriteResult, GmailFailure]:
        """Add a label when it is not already present."""
        fetched = await self._get_or_none(message_id)
        if isinstance(fetched, Err):
            return Err(fetched.error)
        if fetched.value is None:
            return Ok(GmailWriteResult("gone", "message no longer exists"))
        if label_id in fetched.value.label_ids:
            return Ok(GmailWriteResult("already", "message already carries the label"))
        return await self._modify_labels(
            message_id, add_label_ids=(label_id,), remove_label_ids=()
        )

    async def trash(self, message_id: str) -> Result[GmailWriteResult, GmailFailure]:
        """Move a message to Trash when needed."""
        fetched = await self._get_or_none(message_id)
        if isinstance(fetched, Err):
            return Err(fetched.error)
        if fetched.value is None:
            return Ok(GmailWriteResult("gone", "message no longer exists"))
        if _TRASH_LABEL in fetched.value.label_ids:
            return Ok(GmailWriteResult("already", "message already trashed"))
        transported = await self.transport.trash_message(message_id)
        if isinstance(transported, Err):
            return Err(transported.error)
        if failure := _response_failure(transported.value, operation="trash-message"):
            return Err(failure)
        return Ok(GmailWriteResult("done"))

    async def _get_or_none(
        self, message_id: str
    ) -> Result[GmailMessage | None, GmailFailure]:
        transported = await self.transport.get_message(message_id)
        if isinstance(transported, Err):
            return Err(transported.error)
        response = transported.value
        if response.status_code == _HTTP_NOT_FOUND:
            return Ok(None)
        if failure := _response_failure(response, operation="get-message"):
            return Err(failure)
        return _parse_message(response.payload, operation="get-message")

    async def _modify_labels(
        self,
        message_id: str,
        *,
        add_label_ids: Sequence[str],
        remove_label_ids: Sequence[str],
    ) -> Result[GmailWriteResult, GmailFailure]:
        transported = await self.transport.modify_labels(
            message_id,
            add_label_ids=add_label_ids,
            remove_label_ids=remove_label_ids,
        )
        if isinstance(transported, Err):
            return Err(transported.error)
        if failure := _response_failure(transported.value, operation="modify-labels"):
            return Err(failure)
        return Ok(GmailWriteResult("done"))


__all__ = [
    "GmailAuthenticationFailure",
    "GmailClient",
    "GmailFailure",
    "GmailHttpFailure",
    "GmailMessage",
    "GmailNetworkFailure",
    "GmailOperation",
    "GmailProtocolFailure",
    "GmailResponse",
    "GmailTransport",
    "GmailTransportFailure",
    "GmailWriteOutcome",
    "GmailWriteResult",
]
