"""Offline tests for the OAuth-backed Gmail HTTP transport."""

from __future__ import annotations

from pathlib import Path

import httpx2
from snekok import Err
from snektest import assert_eq, test

from tether.gmail_client import GmailNetworkFailure
from tether.gmail_oauth import HttpGmailTransport
from tether.youtube_oauth import OAuthConfig


@test()
async def request_errors_are_typed_network_failures() -> None:
    """A connection failure does not escape the transport boundary."""

    def reject(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("offline", request=request)

    transport = HttpGmailTransport(
        OAuthConfig(
            client_secret_path=Path("unused-client.json"),
            token_path=Path("unused-token.json"),
            scopes=(),
        ),
        http_transport=httpx2.MockTransport(reject),
        token_loader=lambda _config: "test-token",
    )

    response = await transport.list_messages(query="in:inbox", page_token=None)
    await transport.aclose()

    assert isinstance(response, Err)
    assert isinstance(response.error, GmailNetworkFailure)
    assert_eq(response.error.operation, "list-messages")
