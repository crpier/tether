"""Behavior tests for the read-only Gmail tools available to agents."""

from __future__ import annotations

import base64
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory

from snekok import Ok, Result
from snektest import assert_eq, test

from tests.surfaces import call_tool, surface_client
from tether.gmail_client import GmailNetworkFailure, GmailResponse


@dataclass
class ScriptedGmailTransport:
    """A tiny transport that returns scripted list and raw responses."""

    responses: list[Result[GmailResponse, GmailNetworkFailure]] = field(
        default_factory=list[Result[GmailResponse, GmailNetworkFailure]]
    )
    list_calls: list[tuple[str, str | None, int | None]] = field(
        default_factory=list[tuple[str, str | None, int | None]]
    )
    raw_calls: list[str] = field(default_factory=list[str])

    async def list_messages(
        self, *, query: str, page_token: str | None, max_results: int | None = None
    ) -> Result[GmailResponse, GmailNetworkFailure]:
        self.list_calls.append((query, page_token, max_results))
        return self.responses.pop(0)

    async def get_raw_message(
        self, message_id: str
    ) -> Result[GmailResponse, GmailNetworkFailure]:
        self.raw_calls.append(message_id)
        return self.responses.pop(0)

    async def get_message(
        self, message_id: str
    ) -> Result[GmailResponse, GmailNetworkFailure]:
        return Ok(GmailResponse(status_code=404, payload={}))

    async def list_labels(self) -> Result[GmailResponse, GmailNetworkFailure]:
        return Ok(GmailResponse(status_code=200, payload={"labels": []}))

    async def modify_labels(
        self,
        message_id: str,
        *,
        add_label_ids: Sequence[str],
        remove_label_ids: Sequence[str],
    ) -> Result[GmailResponse, GmailNetworkFailure]:
        raise AssertionError("read-only tool path must never mutate labels")

    async def trash_message(
        self, message_id: str
    ) -> Result[GmailResponse, GmailNetworkFailure]:
        raise AssertionError("read-only tool path must never trash messages")


@test()
def disabled_gmail_tools_return_tool_unavailable() -> None:
    """No configured Gmail client becomes an upstream_error boundary failure."""
    with (
        TemporaryDirectory() as directory,
        surface_client(Path(directory)) as client,
    ):
        envelope = call_tool(client, "search_gmail", query="from:alice")

    assert_eq(envelope["success"], False)
    assert_eq(envelope["error"]["code"], "upstream_error")


@test()
def search_gmail_returns_paginated_message_rows() -> None:
    """`search_gmail` returns rows and pagination metadata from one page."""
    transport = ScriptedGmailTransport(
        responses=[
            Ok(
                GmailResponse(
                    status_code=200,
                    payload={
                        "messages": [
                            {"id": "m1", "threadId": "t1"},
                            {"id": "m2", "threadId": "t2"},
                        ],
                        "nextPageToken": "next",
                        "resultSizeEstimate": 2,
                    },
                )
            )
        ]
    )

    with (
        TemporaryDirectory() as directory,
        surface_client(Path(directory), gmail_transport=transport) as client,
    ):
        envelope = call_tool(
            client,
            "search_gmail",
            query="in:inbox",
            max_results=2,
            page_token="page",
        )

    assert_eq(envelope["success"], True)
    assert_eq(
        envelope["result"],
        {
            "messages": [
                {"message_id": "m1", "thread_id": "t1"},
                {"message_id": "m2", "thread_id": "t2"},
            ],
            "next_page_token": "next",
            "result_size_estimate": 2,
        },
    )
    assert_eq(transport.list_calls, [("in:inbox", "page", 2)])


@test()
def read_gmail_message_truncates_payload_and_reports_metadata() -> None:
    """`read_gmail_message` truncates safely and marks truncation metadata."""
    raw = "From: alice@x\n\n" + "x" * 1500
    transport = ScriptedGmailTransport(
        responses=[
            Ok(
                GmailResponse(
                    status_code=200,
                    payload={
                        "id": "m1",
                        "threadId": "t1",
                        "raw": base64.urlsafe_b64encode(raw.encode("utf-8"))
                        .decode("ascii")
                        .rstrip("="),
                    },
                )
            )
        ]
    )

    with (
        TemporaryDirectory() as directory,
        surface_client(Path(directory), gmail_transport=transport) as client,
    ):
        envelope = call_tool(
            client,
            "read_gmail_message",
            message_id="m1",
            max_chars=1000,
        )

    assert_eq(envelope["success"], True)
    assert_eq(envelope["result"]["message_id"], "m1")
    assert_eq(envelope["result"]["thread_id"], "t1")
    assert_eq(envelope["result"]["returned_chars"], 1000)
    assert_eq(envelope["result"]["total_chars"], len(raw))
    assert_eq(envelope["result"]["truncated"], True)
    assert_eq(envelope["result"]["raw_rfc2822"], raw[:1000])


@test()
def read_gmail_message_maps_404_to_not_found_and_403_to_auth() -> None:
    """The read tool maps a deleted message and expired token distinctly."""
    with TemporaryDirectory() as directory:
        transport = ScriptedGmailTransport(
            responses=[
                Ok(
                    GmailResponse(
                        status_code=404,
                        payload={"id": "m1", "threadId": "t1", "raw": ""},
                    )
                )
            ]
        )
        with surface_client(Path(directory), gmail_transport=transport) as client:
            missing = call_tool(client, "read_gmail_message", message_id="gone")

        assert_eq(missing["success"], False)
        assert_eq(missing["error"]["code"], "not_found")
        assert_eq(missing["error"]["message"], "not found")

        auth_transport = ScriptedGmailTransport(
            responses=[Ok(GmailResponse(status_code=403, payload={}))]
        )
        with surface_client(Path(directory), gmail_transport=auth_transport) as client:
            auth = call_tool(client, "read_gmail_message", message_id="m1")

        assert_eq(auth["success"], False)
        assert_eq(auth["error"]["code"], "upstream_error")
        assert_eq(
            auth["error"]["message"],
            "Gmail authentication expired or was revoked; please re-authorize",
        )


@test()
def read_tool_does_not_use_write_methods() -> None:
    """Raw reads stay read-only and do not invoke Gmail write transports."""
    transport = ScriptedGmailTransport(
        responses=[
            Ok(
                GmailResponse(
                    status_code=200,
                    payload={
                        "id": "m1",
                        "threadId": "t1",
                        "raw": base64.urlsafe_b64encode(b"Hi")
                        .decode("ascii")
                        .rstrip("="),
                    },
                )
            )
        ]
    )

    with (
        TemporaryDirectory() as directory,
        surface_client(Path(directory), gmail_transport=transport) as client,
    ):
        _ = call_tool(client, "read_gmail_message", message_id="m1")

    assert_eq(transport.raw_calls, ["m1"])
    assert_eq(transport.list_calls, [])
