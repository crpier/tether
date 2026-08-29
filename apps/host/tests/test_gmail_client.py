"""Public-behavior tests for the typed Gmail API client."""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

import structlog
from snekok.result import Err, Ok, Result
from snektest import assert_eq, test

from tether.gmail.client import (
    GmailAuthenticationFailure,
    GmailClient,
    GmailNetworkFailure,
    GmailProtocolFailure,
    GmailResponse,
)
from tether.structured_logging import Logger


def test_logger() -> Logger:
    """Return a throwaway logger for client calls."""
    return structlog.stdlib.get_logger("test.gmail-client")


@dataclass
class ScriptedGmailTransport:
    """Return scripted responses at the Gmail transport boundary."""

    list_responses: list[Result[GmailResponse, GmailNetworkFailure]] = field(
        default_factory=list[Result[GmailResponse, GmailNetworkFailure]]
    )
    preview_responses: list[Result[GmailResponse, GmailNetworkFailure]] = field(
        default_factory=list[Result[GmailResponse, GmailNetworkFailure]]
    )

    async def get_raw_message(
        self, message_id: str
    ) -> Result[GmailResponse, GmailNetworkFailure]:
        return self.list_responses.pop(0)

    async def list_messages(
        self, *, query: str, page_token: str | None, max_results: int | None = None
    ) -> Result[GmailResponse, GmailNetworkFailure]:
        return self.list_responses.pop(0)

    async def get_message(
        self, message_id: str
    ) -> Result[GmailResponse, GmailNetworkFailure]:
        return Ok(GmailResponse(status_code=404, payload={}))

    async def get_message_preview(
        self, message_id: str
    ) -> Result[GmailResponse, GmailNetworkFailure]:
        return self.preview_responses.pop(0)

    async def list_labels(self) -> Result[GmailResponse, GmailNetworkFailure]:
        return Ok(GmailResponse(status_code=200, payload={"labels": []}))

    async def modify_labels(
        self,
        message_id: str,
        *,
        add_label_ids: Sequence[str],
        remove_label_ids: Sequence[str],
    ) -> Result[GmailResponse, GmailNetworkFailure]:
        return Ok(GmailResponse(status_code=200, payload={}))

    async def trash_message(
        self, message_id: str
    ) -> Result[GmailResponse, GmailNetworkFailure]:
        return Ok(GmailResponse(status_code=200, payload={}))


@dataclass
class ConcurrentPreviewTransport(ScriptedGmailTransport):
    """Track concurrent metadata reads while returning previews by id."""

    active_previews: int = 0
    max_active_previews: int = 0

    async def get_message_preview(
        self, message_id: str
    ) -> Result[GmailResponse, GmailNetworkFailure]:
        self.active_previews += 1
        self.max_active_previews = max(
            self.max_active_previews,
            self.active_previews,
        )
        try:
            await asyncio.sleep(0.01)
            return Ok(
                GmailResponse(
                    status_code=200,
                    payload={
                        "id": message_id,
                        "threadId": f"thread-{message_id}",
                        "internalDate": "1767268800000",
                        "snippet": f"Preview {message_id}",
                        "payload": {"headers": []},
                    },
                )
            )
        finally:
            self.active_previews -= 1


@test()
async def authentication_rejection_is_a_typed_client_failure() -> None:
    """A provider 401 remains inspectable without exception handling."""
    transport = ScriptedGmailTransport(
        list_responses=[Ok(GmailResponse(status_code=401, payload={}))]
    )

    messages = await GmailClient(transport).list_message_ids(
        query="-in:spam", logger=test_logger()
    )

    assert isinstance(messages, Err)
    assert isinstance(messages.error, GmailAuthenticationFailure)
    assert_eq(messages.error.operation, "list-messages")
    assert_eq(messages.error.status_code, 401)


@test()
async def malformed_success_is_a_typed_protocol_failure() -> None:
    """A successful status cannot smuggle an invalid listing into the domain."""
    transport = ScriptedGmailTransport(
        list_responses=[
            Ok(GmailResponse(status_code=200, payload={"messages": "invalid"}))
        ]
    )

    messages = await GmailClient(transport).list_message_ids(
        query="-in:spam", logger=test_logger()
    )

    assert isinstance(messages, Err)
    assert isinstance(messages.error, GmailProtocolFailure)
    assert_eq(messages.error.operation, "list-messages")


@test()
async def get_message_preview_normalizes_metadata_and_provider_snippet() -> None:
    """A lightweight metadata response becomes an agent-useful preview."""
    transport = ScriptedGmailTransport(
        preview_responses=[
            Ok(
                GmailResponse(
                    status_code=200,
                    payload={
                        "id": "msg-1",
                        "threadId": "thread-1",
                        "internalDate": "1767268800000",
                        "snippet": "  Your order &amp; delivery\nare confirmed.  ",
                        "payload": {
                            "headers": [
                                {"name": "From", "value": "Alice <alice@example.com>"},
                                {"name": "Subject", "value": "Order update"},
                            ]
                        },
                    },
                )
            )
        ]
    )

    preview = await GmailClient(transport).get_message_preview("msg-1")

    assert isinstance(preview, Ok)
    assert_eq(preview.value.message_id, "msg-1")
    assert_eq(preview.value.thread_id, "thread-1")
    assert_eq(preview.value.sender, "Alice <alice@example.com>")
    assert_eq(preview.value.subject, "Order update")
    assert_eq(preview.value.body_preview, "Your order & delivery are confirmed.")
    assert_eq(
        preview.value.received_at,
        datetime.fromtimestamp(1767268800, tz=UTC),
    )


@test()
async def search_messages_returns_hydrated_page() -> None:
    """Search preserves page metadata while hydrating message previews."""
    transport = ScriptedGmailTransport(
        list_responses=[
            Ok(
                GmailResponse(
                    status_code=200,
                    payload={
                        "messages": [
                            {"id": "msg-1", "threadId": "thread-1"},
                        ],
                        "nextPageToken": "next",
                        "resultSizeEstimate": 15,
                    },
                )
            )
        ],
        preview_responses=[
            Ok(
                GmailResponse(
                    status_code=200,
                    payload={
                        "id": "msg-1",
                        "threadId": "thread-1",
                        "internalDate": "1767268800000",
                        "snippet": "Project summary",
                        "payload": {
                            "headers": [
                                {"name": "From", "value": "foo@example.com"},
                                {"name": "Subject", "value": "Status"},
                            ]
                        },
                    },
                )
            )
        ],
    )

    search = await GmailClient(transport).search_messages(
        query="from:foo", logger=test_logger(), max_results=10, page_token="page"
    )

    assert isinstance(search, Ok)
    assert_eq(search.value.messages[0].message_id, "msg-1")
    assert_eq(search.value.messages[0].sender, "foo@example.com")
    assert_eq(search.value.messages[0].subject, "Status")
    assert_eq(search.value.messages[0].body_preview, "Project summary")
    assert_eq(search.value.next_page_token, "next")
    assert_eq(search.value.result_size_estimate, 15)


@test()
async def search_messages_bounds_concurrent_preview_hydration() -> None:
    """One large page cannot fan out unbounded Gmail metadata requests."""
    message_ids = [f"msg-{index}" for index in range(10)]
    transport = ConcurrentPreviewTransport(
        list_responses=[
            Ok(
                GmailResponse(
                    status_code=200,
                    payload={
                        "messages": [
                            {
                                "id": message_id,
                                "threadId": f"thread-{message_id}",
                            }
                            for message_id in message_ids
                        ],
                        "resultSizeEstimate": len(message_ids),
                    },
                )
            )
        ]
    )

    search = await GmailClient(transport).search_messages(
        query="in:inbox",
        logger=test_logger(),
    )

    assert isinstance(search, Ok)
    assert_eq(
        [message.message_id for message in search.value.messages],
        message_ids,
    )
    assert_eq(transport.max_active_previews, 8)


@test()
async def get_raw_message_decodes_strict_and_rejects_garbage() -> None:
    """`get_raw_message` keeps a strict base64 boundary for raw reads."""
    payload = "From: a\r\n\r\nhello"
    encoded = (
        base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")
    )
    transport = ScriptedGmailTransport(
        list_responses=[
            Ok(
                GmailResponse(
                    status_code=200,
                    payload={"id": "msg-1", "threadId": "thread-1", "raw": encoded},
                )
            )
        ]
    )
    raw = await GmailClient(transport).get_raw_message("msg-1")

    assert isinstance(raw, Ok)
    assert_eq(raw.value.message_id, "msg-1")
    assert_eq(raw.value.thread_id, "thread-1")
    assert_eq(raw.value.raw_rfc2822, payload)

    bad = ScriptedGmailTransport(
        list_responses=[
            Ok(
                GmailResponse(
                    status_code=200,
                    payload={"id": "msg-1", "threadId": "thread-1", "raw": "@@@"},
                )
            )
        ]
    )
    failure = await GmailClient(bad).get_raw_message("msg-1")

    assert isinstance(failure, Err)
    assert isinstance(failure.error, GmailProtocolFailure)
    assert_eq(failure.error.operation, "get-raw-message")
