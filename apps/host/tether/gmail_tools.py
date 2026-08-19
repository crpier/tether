"""Read-only Gmail chat tools for search and raw message fetch."""

from __future__ import annotations

from typing import NoReturn

from pydantic import BaseModel, Field
from snekok import Ok
from starlette.requests import Request
from starlette.routing import Route

from tether.app_runtime import app_runtime
from tether.capabilities import bind_params
from tether.capability_contracts import CapabilityOutcome, ErrorRule
from tether.gmail_client import (
    GmailAuthenticationFailure,
    GmailClient,
    GmailHttpFailure,
    GmailNetworkFailure,
    GmailProtocolFailure,
)
from tether.structured_logging import get_request_logger
from tether.tool_runtime import ToolSpec


class GmailToolsNotConfiguredError(Exception):
    """Surface that Gmail chat tools require an OAuth-backed client."""


class GmailToolsAuthError(Exception):
    """Surface expired or missing Gmail credentials."""


class GmailToolsNotFoundError(Exception):
    """Surface a missing message identifier to the caller."""


class GmailToolsUpstreamError(Exception):
    """Surface an upstream contract break or transport failure."""


_GMAIL_AUTH_ERROR = "Gmail authentication expired or was revoked; please re-authorize"
_GMAIL_MESSAGE_NOT_FOUND_ERROR = "requested message is not available"
_GMAIL_READ_NOT_FOUND_ERROR = "Gmail read returned not found"


class GmailSearchParams(BaseModel):
    """Search Gmail message metadata by query term, paged by token."""

    query: str = Field(min_length=1)
    max_results: int = Field(
        default=20,
        ge=1,
        le=50,
        description="Maximum rows to return in this page.",
    )
    page_token: str | None = None


class ReadGmailMessageParams(BaseModel):
    """Read one full raw RFC 2822 source text by message id."""

    message_id: str = Field(min_length=1)
    max_chars: int = Field(
        default=50_000,
        ge=1_000,
        le=200_000,
        description="Return only the first N characters of raw RFC 2822 text.",
    )


GMAIL_TOOL_ERRORS: tuple[ErrorRule, ...] = (
    ErrorRule((GmailToolsNotConfiguredError,), "upstream_error", 503),
    ErrorRule((GmailToolsNotFoundError,), "not_found", 404),
    ErrorRule((GmailToolsAuthError, GmailToolsUpstreamError), "upstream_error", 502),
)
"""Error mapping shared by both Gmail read tools."""


def _require_client(request: Request) -> GmailClient:
    """Load a configured Gmail client from host runtime or fail fast."""
    client = app_runtime(request.app).gmail_client
    if client is None:
        raise GmailToolsNotConfiguredError
    return client


def _translate_failure(error: object, *, not_found_if_404: bool = False) -> NoReturn:
    """Translate typed Gmail failures onto user-facing tool errors."""
    match error:
        case GmailAuthenticationFailure():
            message = _GMAIL_AUTH_ERROR
            raise GmailToolsAuthError(message)
        case GmailHttpFailure(status_code=404) if not_found_if_404:
            message = _GMAIL_MESSAGE_NOT_FOUND_ERROR
            raise GmailToolsNotFoundError(message)
        case GmailHttpFailure(status_code=404):
            message = _GMAIL_READ_NOT_FOUND_ERROR
            raise GmailToolsUpstreamError(message)
        case GmailHttpFailure() | GmailNetworkFailure() | GmailProtocolFailure():
            raise GmailToolsUpstreamError(str(error))
        case _:
            message = "Unhandled Gmail failure contract"
            raise RuntimeError(message)


async def _search_gmail(
    request: Request, query: str, max_results: int, page_token: str | None
) -> CapabilityOutcome:
    """Search Gmail and return message ids plus pagination metadata."""
    client = _require_client(request)
    search = await client.search_messages(
        query=query,
        logger=get_request_logger(request),
        max_results=max_results,
        page_token=page_token,
    )
    if isinstance(search, Ok):
        value = search.value
    else:
        _translate_failure(search.error)
    return CapabilityOutcome(
        result={
            "messages": [
                {
                    "message_id": identity.message_id,
                    "thread_id": identity.thread_id,
                }
                for identity in value.messages
            ],
            "next_page_token": value.next_page_token,
            "result_size_estimate": value.result_size_estimate,
        }
    )


async def _read_gmail_message(
    request: Request, message_id: str, max_chars: int
) -> CapabilityOutcome:
    """Read one raw message and trim to the requested character budget."""
    client = _require_client(request)
    message = await client.get_raw_message(message_id)
    if isinstance(message, Ok):
        raw = message.value
    else:
        _translate_failure(message.error, not_found_if_404=True)
    truncated = raw.raw_rfc2822[:max_chars]
    return CapabilityOutcome(
        result={
            "message_id": raw.message_id,
            "thread_id": raw.thread_id,
            "raw_rfc2822": truncated,
            "returned_chars": len(truncated),
            "total_chars": len(raw.raw_rfc2822),
            "truncated": len(raw.raw_rfc2822) > max_chars,
        }
    )


GMAIL_TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        "search_gmail",
        GmailSearchParams,
        bind_params(_search_gmail),
        GMAIL_TOOL_ERRORS,
    ),
    ToolSpec(
        "read_gmail_message",
        ReadGmailMessageParams,
        bind_params(_read_gmail_message),
        GMAIL_TOOL_ERRORS,
    ),
)
"""The read-only Gmail tools exposed to the internal tool surface."""


def internal_gmail_tool_routes() -> list[Route]:
    """Mount Gmail read tools under `/internal/tools/*`."""
    return [spec.route() for spec in GMAIL_TOOL_SPECS]
