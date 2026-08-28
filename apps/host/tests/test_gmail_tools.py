"""Behavior tests for the Gmail tools available to agents."""

from __future__ import annotations

import base64
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast

from snekok import Ok, Result
from snekql.sqlite import Database, select
from snektest import assert_eq, test
from starlette.applications import Starlette

from tests.surfaces import call_tool, login, surface_client
from tether.app_runtime import app_runtime
from tether.email_evidence_store import EmailEvidenceSnapshot
from tether.gmail.client import GmailNetworkFailure, GmailResponse


@dataclass
class ScriptedGmailTransport:
    """A tiny transport that records scripted Gmail reads and writes."""

    responses: list[Result[GmailResponse, GmailNetworkFailure]] = field(
        default_factory=list[Result[GmailResponse, GmailNetworkFailure]]
    )
    list_calls: list[tuple[str, str | None, int | None]] = field(
        default_factory=list[tuple[str, str | None, int | None]]
    )
    labels_response: Result[GmailResponse, GmailNetworkFailure] = field(
        default_factory=lambda: Ok(
            GmailResponse(status_code=200, payload={"labels": []})
        )
    )
    labels_calls: int = 0
    message_response: Result[GmailResponse, GmailNetworkFailure] = field(
        default_factory=lambda: Ok(GmailResponse(status_code=404, payload={}))
    )
    preview_responses: dict[str, Result[GmailResponse, GmailNetworkFailure]] = field(
        default_factory=dict[str, Result[GmailResponse, GmailNetworkFailure]]
    )
    preview_calls: list[str] = field(default_factory=list[str])
    modify_calls: list[tuple[str, tuple[str, ...], tuple[str, ...]]] = field(
        default_factory=list[tuple[str, tuple[str, ...], tuple[str, ...]]]
    )
    raw_calls: list[str] = field(default_factory=list[str])
    trash_calls: list[str] = field(default_factory=list[str])

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
        return self.message_response

    async def get_message_preview(
        self, message_id: str
    ) -> Result[GmailResponse, GmailNetworkFailure]:
        self.preview_calls.append(message_id)
        return self.preview_responses[message_id]

    async def list_labels(self) -> Result[GmailResponse, GmailNetworkFailure]:
        self.labels_calls += 1
        return self.labels_response

    async def modify_labels(
        self,
        message_id: str,
        *,
        add_label_ids: Sequence[str],
        remove_label_ids: Sequence[str],
    ) -> Result[GmailResponse, GmailNetworkFailure]:
        self.modify_calls.append(
            (message_id, tuple(add_label_ids), tuple(remove_label_ids))
        )
        return Ok(GmailResponse(status_code=200, payload={}))

    async def trash_message(
        self, message_id: str
    ) -> Result[GmailResponse, GmailNetworkFailure]:
        self.trash_calls.append(message_id)
        return Ok(GmailResponse(status_code=200, payload={}))


def gmail_preview_response(
    message_id: str,
    thread_id: str,
    *,
    sender: str,
    subject: str,
    snippet: str,
) -> GmailResponse:
    """Build one valid Gmail metadata response."""
    return GmailResponse(
        status_code=200,
        payload={
            "id": message_id,
            "threadId": thread_id,
            "internalDate": "1767225600000",
            "snippet": snippet,
            "payload": {
                "headers": [
                    {"name": "From", "value": sender},
                    {"name": "Subject", "value": subject},
                ]
            },
        },
    )


async def _email_snapshot_count(database: Database) -> int:
    """Count retained email Evidence for default-promotion policy assertions."""
    async with database.transaction() as transaction:
        return len(await transaction.fetch_all(select(EmailEvidenceSnapshot).all()))


def gmail_raw_response(raw: str, *, message_id: str = "m1") -> GmailResponse:
    """Build one valid Gmail raw-source response."""
    return GmailResponse(
        status_code=200,
        payload={
            "id": message_id,
            "threadId": "t1",
            "raw": base64.urlsafe_b64encode(raw.encode("utf-8"))
            .decode("ascii")
            .rstrip("="),
        },
    )


def gmail_message_response(*, labels: list[str]) -> GmailResponse:
    """Build one valid Gmail full-message response."""
    return GmailResponse(
        status_code=200,
        payload={
            "id": "m1",
            "threadId": "t1",
            "internalDate": "1767225600000",
            "labelIds": labels,
            "payload": {"headers": []},
        },
    )


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
def archive_gmail_message_removes_the_inbox_label() -> None:
    """Archiving through pi removes `INBOX` without deleting the message."""
    transport = ScriptedGmailTransport(
        message_response=Ok(gmail_message_response(labels=["INBOX", "STARRED"]))
    )

    with (
        TemporaryDirectory() as directory,
        surface_client(Path(directory), gmail_transport=transport) as client,
    ):
        envelope = call_tool(client, "archive_gmail_message", message_id="m1")

    assert_eq(envelope["success"], True)
    assert_eq(
        envelope["result"],
        {"detail": None, "message_id": "m1", "outcome": "done"},
    )
    assert_eq(transport.modify_calls, [("m1", (), ("INBOX",))])
    assert_eq(transport.trash_calls, [])


@test()
def browser_can_undo_a_completed_archive() -> None:
    """Undo restores `INBOX` after an archive receipt reports a real change."""
    transport = ScriptedGmailTransport(
        message_response=Ok(gmail_message_response(labels=["STARRED"]))
    )

    with (
        TemporaryDirectory() as directory,
        surface_client(Path(directory), gmail_transport=transport) as client,
    ):
        login(client)
        response = client.post(
            "/api/gmail/actions/undo",
            json={"action": "archive", "message_id": "m1"},
        )

    assert_eq(response.status_code, 200)
    assert_eq(
        response.json(),
        {
            "detail": None,
            "message_id": "m1",
            "outcome": "done",
        },
    )
    assert_eq(transport.modify_calls, [("m1", ("INBOX",), ())])


@test()
def trash_gmail_message_moves_the_message_to_trash() -> None:
    """Deleting through pi stays reversible by using Gmail Trash."""
    transport = ScriptedGmailTransport(
        message_response=Ok(gmail_message_response(labels=["INBOX"]))
    )

    with (
        TemporaryDirectory() as directory,
        surface_client(Path(directory), gmail_transport=transport) as client,
    ):
        envelope = call_tool(client, "trash_gmail_message", message_id="m1")

    assert_eq(envelope["success"], True)
    assert_eq(
        envelope["result"],
        {"detail": None, "message_id": "m1", "outcome": "done"},
    )
    assert_eq(transport.trash_calls, ["m1"])


@test()
def update_gmail_labels_adds_and_removes_labels_by_name() -> None:
    """The label tool resolves human names before one atomic Gmail update."""
    transport = ScriptedGmailTransport(
        labels_response=Ok(
            GmailResponse(
                status_code=200,
                payload={
                    "labels": [
                        {"id": "Label_42", "name": "Project X", "type": "user"},
                        {"id": "Label_7", "name": "Old", "type": "user"},
                    ]
                },
            )
        ),
        message_response=Ok(gmail_message_response(labels=["INBOX", "Label_7"])),
    )

    with (
        TemporaryDirectory() as directory,
        surface_client(Path(directory), gmail_transport=transport) as client,
    ):
        envelope = call_tool(
            client,
            "update_gmail_labels",
            message_id="m1",
            add_labels=["Project X"],
            remove_labels=["Old"],
        )

    assert_eq(envelope["success"], True)
    assert_eq(
        envelope["result"],
        {"detail": None, "message_id": "m1", "outcome": "done"},
    )
    assert_eq(transport.labels_calls, 1)
    assert_eq(transport.modify_calls, [("m1", ("Label_42",), ("Label_7",))])


@test()
def list_gmail_labels_returns_account_labels() -> None:
    """The agent can read every Gmail system and user label."""
    transport = ScriptedGmailTransport(
        labels_response=Ok(
            GmailResponse(
                status_code=200,
                payload={
                    "labels": [
                        {"id": "INBOX", "name": "INBOX", "type": "system"},
                        {"id": "Label_42", "name": "Project X", "type": "user"},
                    ]
                },
            )
        )
    )

    with (
        TemporaryDirectory() as directory,
        surface_client(Path(directory), gmail_transport=transport) as client,
    ):
        envelope = call_tool(client, "list_gmail_labels")

    assert_eq(envelope["success"], True)
    assert_eq(
        envelope["result"],
        {
            "labels": [
                {"label_id": "INBOX", "name": "INBOX"},
                {"label_id": "Label_42", "name": "Project X"},
            ]
        },
    )
    assert_eq(transport.labels_calls, 1)


@test()
def search_gmail_returns_an_empty_page_when_gmail_omits_messages() -> None:
    """Gmail omits the `messages` member when a search has no matches."""
    transport = ScriptedGmailTransport(
        responses=[
            Ok(
                GmailResponse(
                    status_code=200,
                    payload={"resultSizeEstimate": 0},
                )
            )
        ]
    )

    with (
        TemporaryDirectory() as directory,
        surface_client(Path(directory), gmail_transport=transport) as client,
    ):
        envelope = call_tool(client, "search_gmail", labels=["No matches"])

    assert_eq(envelope["success"], True)
    assert_eq(envelope["result"]["messages"], [])
    assert_eq(envelope["result"]["result_size_estimate"], 0)


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
        ],
        preview_responses={
            "m1": Ok(
                gmail_preview_response(
                    "m1",
                    "t1",
                    sender="Alice <alice@example.com>",
                    subject="Project kickoff",
                    snippet="Agenda and notes for tomorrow.",
                )
            ),
            "m2": Ok(
                gmail_preview_response(
                    "m2",
                    "t2",
                    sender="Bob <bob@example.com>",
                    subject="Re: Project kickoff",
                    snippet="I added the budget details.",
                )
            ),
        },
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
                {
                    "body_preview": "Agenda and notes for tomorrow.",
                    "message_id": "m1",
                    "received_at": "2026-01-01T00:00:00+00:00",
                    "sender": "Alice <alice@example.com>",
                    "subject": "Project kickoff",
                    "thread_id": "t1",
                },
                {
                    "body_preview": "I added the budget details.",
                    "message_id": "m2",
                    "received_at": "2026-01-01T00:00:00+00:00",
                    "sender": "Bob <bob@example.com>",
                    "subject": "Re: Project kickoff",
                    "thread_id": "t2",
                },
            ],
            "next_page_token": "next",
            "result_size_estimate": 2,
        },
    )
    assert_eq(transport.list_calls, [("in:inbox", "page", 2)])
    assert_eq(transport.preview_calls, ["m1", "m2"])


@test()
def search_gmail_matches_every_requested_label() -> None:
    """Label filters become AND-ed Gmail search terms."""
    transport = ScriptedGmailTransport(
        responses=[
            Ok(
                GmailResponse(
                    status_code=200,
                    payload={"messages": [], "resultSizeEstimate": 0},
                )
            )
        ]
    )

    with (
        TemporaryDirectory() as directory,
        surface_client(Path(directory), gmail_transport=transport) as client,
    ):
        _ = call_tool(
            client,
            "search_gmail",
            query="project kickoff",
            labels=["Receipts", "Project X"],
        )

    assert_eq(
        transport.list_calls,
        [('project kickoff label:"Receipts" label:"Project X"', None, 20)],
    )


@test()
def search_gmail_supports_a_label_only_search() -> None:
    """A text query is optional when labels identify the desired messages."""
    transport = ScriptedGmailTransport(
        responses=[
            Ok(
                GmailResponse(
                    status_code=200,
                    payload={"messages": [], "resultSizeEstimate": 0},
                )
            )
        ]
    )

    with (
        TemporaryDirectory() as directory,
        surface_client(Path(directory), gmail_transport=transport) as client,
    ):
        envelope = call_tool(client, "search_gmail", labels=["Receipts"])

    assert_eq(envelope["success"], True)
    assert_eq(transport.list_calls, [('label:"Receipts"', None, 20)])


@test()
def search_gmail_applies_an_inclusive_exclusive_date_window() -> None:
    """ISO date bounds become Gmail's inclusive `after` and exclusive `before`."""
    transport = ScriptedGmailTransport(
        responses=[
            Ok(
                GmailResponse(
                    status_code=200,
                    payload={"messages": [], "resultSizeEstimate": 0},
                )
            )
        ]
    )

    with (
        TemporaryDirectory() as directory,
        surface_client(Path(directory), gmail_transport=transport) as client,
    ):
        _ = call_tool(
            client,
            "search_gmail",
            query="invoice",
            after="2026-01-01",
            before="2026-02-01",
        )

    assert_eq(
        transport.list_calls,
        [("invoice after:2026/01/01 before:2026/02/01", None, 20)],
    )


@test()
def search_gmail_rejects_a_reversed_date_window() -> None:
    """A search period must end after it starts."""
    transport = ScriptedGmailTransport(
        responses=[
            Ok(
                GmailResponse(
                    status_code=200,
                    payload={"messages": [], "resultSizeEstimate": 0},
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
            query="invoice",
            after="2026-02-01",
            before="2026-01-01",
        )

    assert_eq(envelope["success"], False)
    assert_eq(envelope["error"]["code"], "invalid_input")
    assert_eq(transport.list_calls, [])


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
def ordinary_gmail_read_creates_no_email_evidence() -> None:
    """Reading email without explicit promotion retains no citeable snapshot."""
    transport = ScriptedGmailTransport(
        responses=[Ok(gmail_raw_response("From: Alice\n\nTemporary update."))]
    )

    with (
        TemporaryDirectory() as directory,
        surface_client(Path(directory), gmail_transport=transport) as client,
    ):
        _ = call_tool(client, "read_gmail_message", message_id="m1")
        runtime = app_runtime(cast("Starlette", client.app))
        portal = client.portal
        assert portal is not None
        snapshot_count = portal.call(
            _email_snapshot_count,
            runtime.conversation_service.database,
        )

    assert_eq(snapshot_count, 0)


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
    assert_eq(transport.modify_calls, [])
    assert_eq(transport.trash_calls, [])
