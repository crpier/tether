"""Gmail chat tools for search, reads, labels, archive, and Trash."""

from __future__ import annotations

from datetime import date
from typing import NoReturn, Protocol, Self, cast

from pydantic import BaseModel, Field, model_validator
from snekok.result import Ok
from starlette.requests import Request
from starlette.routing import Route

from tether.capabilities import bind_params
from tether.capability_contracts import CapabilityOutcome, ErrorRule
from tether.gmail.client import (
    GmailAuthenticationFailure,
    GmailClient,
    GmailHttpFailure,
    GmailNetworkFailure,
    GmailProtocolFailure,
)
from tether.structured_logging import get_request_logger
from tether.tool_runtime import ToolSpec


class _GmailToolsRuntime(Protocol):
    """The slice of the host runtime this module uses.

    Declared consumer-side so this module never imports `tether.app_runtime`:
    the platform's runtime types this Integration, so a module-level import in
    either direction would close a static import cycle (ADR-0025).
    """

    gmail_client: GmailClient | None


def _runtime(request: Request) -> _GmailToolsRuntime:
    """Read the installed application runtime off the request."""
    return cast("_GmailToolsRuntime", request.app.state.runtime)


class GmailToolsNotConfiguredError(Exception):
    """Surface that Gmail chat tools require an OAuth-backed client."""


class GmailToolsAuthError(Exception):
    """Surface expired or missing Gmail credentials."""


class GmailToolsNotFoundError(Exception):
    """Surface a missing message identifier to the caller."""


class GmailToolsLabelError(Exception):
    """Surface an unknown or contradictory Gmail label request."""


class GmailToolsUpstreamError(Exception):
    """Surface an upstream contract break or transport failure."""


_GMAIL_AUTH_ERROR = "Gmail authentication expired or was revoked; please re-authorize"
_GMAIL_MESSAGE_NOT_FOUND_ERROR = "requested message is not available"
_GMAIL_READ_NOT_FOUND_ERROR = "Gmail read returned not found"


class ArchiveGmailMessageParams(BaseModel):
    """Archive one Gmail message by removing its inbox label."""

    message_id: str = Field(min_length=1)


class GmailSearchParams(BaseModel):
    """Search by query, labels, and dates; return sender, subject, time, and preview."""

    query: str = Field(
        default="",
        description="Optional Gmail search text or native Gmail query operators.",
    )
    after: date | None = Field(
        default=None,
        description="Include messages on or after this ISO date.",
    )
    before: date | None = Field(
        default=None,
        description="Include messages before this ISO date.",
    )
    labels: list[str] = Field(
        default_factory=list,
        description="Human-readable Gmail labels that every result must carry.",
    )
    max_results: int = Field(
        default=20,
        ge=1,
        le=50,
        description="Maximum rows to return in this page.",
    )
    page_token: str | None = None

    @model_validator(mode="after")
    def date_window_ends_after_it_starts(self) -> Self:
        """Reject empty or reversed periods before querying Gmail."""
        if (
            self.after is not None
            and self.before is not None
            and self.before <= self.after
        ):
            message = "before must be later than after"
            raise ValueError(message)
        return self


class ListGmailLabelsParams(BaseModel):
    """List every Gmail account label available to the agent."""


class UpdateGmailLabelsParams(BaseModel):
    """Add and remove human-readable labels on one Gmail message."""

    add_labels: list[str] = Field(default_factory=list)
    message_id: str = Field(min_length=1)
    remove_labels: list[str] = Field(default_factory=list)


class TrashGmailMessageParams(BaseModel):
    """Move one Gmail message to Trash without permanently deleting it."""

    message_id: str = Field(min_length=1)


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
    ErrorRule((GmailToolsLabelError,), "invalid_input", 400),
    ErrorRule((GmailToolsNotConfiguredError,), "upstream_error", 503),
    ErrorRule((GmailToolsNotFoundError,), "not_found", 404),
    ErrorRule((GmailToolsAuthError, GmailToolsUpstreamError), "upstream_error", 502),
)
"""Error mapping shared by Gmail read and write tools."""


def _require_client(request: Request) -> GmailClient:
    """Load a configured Gmail client from host runtime or fail fast."""
    client = _runtime(request).gmail_client
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


async def _archive_gmail_message(
    request: Request, message_id: str
) -> CapabilityOutcome:
    """Archive one message and report its idempotent outcome."""
    archived = await _require_client(request).archive(message_id)
    if isinstance(archived, Ok):
        outcome = archived.value
    else:
        _translate_failure(archived.unwrap_error())
    return CapabilityOutcome(
        result={
            "detail": outcome.detail,
            "message_id": message_id,
            "outcome": outcome.outcome,
        }
    )


async def undo_archive(request: Request, message_id: str) -> CapabilityOutcome:
    """Restore one archived message to the inbox."""
    restored = await _require_client(request).update_labels(
        message_id,
        add_label_ids=("INBOX",),
        remove_label_ids=(),
    )
    if isinstance(restored, Ok):
        outcome = restored.value
    else:
        _translate_failure(restored.unwrap_error(), not_found_if_404=True)
    return CapabilityOutcome(
        result={
            "detail": outcome.detail,
            "message_id": message_id,
            "outcome": outcome.outcome,
        }
    )


async def _search_gmail(request: Request, params: BaseModel) -> CapabilityOutcome:
    """Search Gmail and return useful message previews plus pagination."""
    search_params = cast("GmailSearchParams", params)
    client = _require_client(request)
    terms = [*(term for term in (search_params.query.strip(),) if term)]
    terms.extend(f'label:"{label}"' for label in search_params.labels)
    if search_params.after is not None:
        terms.append(f"after:{search_params.after:%Y/%m/%d}")
    if search_params.before is not None:
        terms.append(f"before:{search_params.before:%Y/%m/%d}")
    search = await client.search_messages(
        query=" ".join(terms),
        logger=get_request_logger(request),
        max_results=search_params.max_results,
        page_token=search_params.page_token,
    )
    if isinstance(search, Ok):
        value = search.value
    else:
        _translate_failure(search.unwrap_error())
    return CapabilityOutcome(
        result={
            "messages": [
                {
                    "body_preview": message.body_preview,
                    "message_id": message.message_id,
                    "received_at": message.received_at.isoformat(),
                    "sender": message.sender,
                    "subject": message.subject,
                    "thread_id": message.thread_id,
                }
                for message in value.messages
            ],
            "next_page_token": value.next_page_token,
            "result_size_estimate": value.result_size_estimate,
        }
    )


async def _update_gmail_labels(
    request: Request,
    add_labels: list[str],
    message_id: str,
    remove_labels: list[str],
) -> CapabilityOutcome:
    """Resolve human label names and apply one atomic message update."""
    client = _require_client(request)
    listed = await client.list_labels()
    if isinstance(listed, Ok):
        label_ids_by_name = {label.name: label.label_id for label in listed.value}
    else:
        _translate_failure(listed.unwrap_error())
    unknown_labels = [
        label
        for label in (*add_labels, *remove_labels)
        if label not in label_ids_by_name
    ]
    if unknown_labels:
        message = f"unknown Gmail labels: {', '.join(dict.fromkeys(unknown_labels))}"
        raise GmailToolsLabelError(message)
    overlap = set(add_labels) & set(remove_labels)
    if overlap:
        message = (
            f"labels cannot be both added and removed: {', '.join(sorted(overlap))}"
        )
        raise GmailToolsLabelError(message)
    updated = await client.update_labels(
        message_id,
        add_label_ids=tuple(label_ids_by_name[name] for name in add_labels),
        remove_label_ids=tuple(label_ids_by_name[name] for name in remove_labels),
    )
    if isinstance(updated, Ok):
        outcome = updated.value
    else:
        _translate_failure(updated.unwrap_error())
    return CapabilityOutcome(
        result={
            "detail": outcome.detail,
            "message_id": message_id,
            "outcome": outcome.outcome,
        }
    )


async def _trash_gmail_message(request: Request, message_id: str) -> CapabilityOutcome:
    """Move one message to Trash and report its idempotent outcome."""
    trashed = await _require_client(request).trash(message_id)
    if isinstance(trashed, Ok):
        outcome = trashed.value
    else:
        _translate_failure(trashed.unwrap_error())
    return CapabilityOutcome(
        result={
            "detail": outcome.detail,
            "message_id": message_id,
            "outcome": outcome.outcome,
        }
    )


async def _list_gmail_labels(request: Request) -> CapabilityOutcome:
    """List validated Gmail account labels."""
    labels = await _require_client(request).list_labels()
    if isinstance(labels, Ok):
        available_labels = labels.value
    else:
        _translate_failure(labels.unwrap_error())
    return CapabilityOutcome(
        result={
            "labels": [
                {"label_id": label.label_id, "name": label.name}
                for label in available_labels
            ]
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
        _translate_failure(message.unwrap_error(), not_found_if_404=True)
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
        "archive_gmail_message",
        ArchiveGmailMessageParams,
        bind_params(_archive_gmail_message),
        GMAIL_TOOL_ERRORS,
    ),
    ToolSpec(
        "search_gmail",
        GmailSearchParams,
        _search_gmail,
        GMAIL_TOOL_ERRORS,
    ),
    ToolSpec(
        "read_gmail_message",
        ReadGmailMessageParams,
        bind_params(_read_gmail_message),
        GMAIL_TOOL_ERRORS,
    ),
    ToolSpec(
        "list_gmail_labels",
        ListGmailLabelsParams,
        bind_params(_list_gmail_labels),
        GMAIL_TOOL_ERRORS,
    ),
    ToolSpec(
        "trash_gmail_message",
        TrashGmailMessageParams,
        bind_params(_trash_gmail_message),
        GMAIL_TOOL_ERRORS,
    ),
    ToolSpec(
        "update_gmail_labels",
        UpdateGmailLabelsParams,
        bind_params(_update_gmail_labels),
        GMAIL_TOOL_ERRORS,
    ),
)
"""The Gmail tools exposed to the internal tool surface."""


def internal_gmail_tool_routes() -> list[Route]:
    """Mount Gmail tools under `/internal/tools/*`."""
    return [spec.route() for spec in GMAIL_TOOL_SPECS]
