"""Public-behavior tests for the typed Gmail API client."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

import structlog
from snekok import Err, Ok, Result
from snektest import assert_eq, test

from tether.gmail_client import (
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

    async def list_messages(
        self, *, query: str, page_token: str | None
    ) -> Result[GmailResponse, GmailNetworkFailure]:
        return self.list_responses.pop(0)

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
        return Ok(GmailResponse(status_code=200, payload={}))

    async def trash_message(
        self, message_id: str
    ) -> Result[GmailResponse, GmailNetworkFailure]:
        return Ok(GmailResponse(status_code=200, payload={}))


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
