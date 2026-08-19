"""Offline tests for the OAuth-backed Gmail HTTP transport."""

from __future__ import annotations

from pathlib import Path

import httpx2
from snekok import Err, Ok
from snektest import assert_eq, test

from tether.gmail_client import GmailNetworkFailure
from tether.gmail_oauth import HttpGmailTransport
from tether.youtube_oauth import OAuthConfig


@test()
async def list_messages_forwards_query_page_token_and_max_results() -> None:
    """A bounded search request uses the documented query, cursor, and maxResults."""

    captured: dict[str, object] = {}

    def capture(request: httpx2.Request) -> httpx2.Response:
        captured["method"] = request.method
        captured["path"] = str(request.url.path)
        captured["params"] = dict(request.url.params)
        captured["auth"] = request.headers.get("authorization")
        return httpx2.Response(
            200,
            json={"messages": [], "nextPageToken": None, "resultSizeEstimate": 0},
        )

    transport = HttpGmailTransport(
        OAuthConfig(
            client_secret_path=Path("unused-client.json"),
            token_path=Path("unused-token.json"),
            scopes=(),
        ),
        http_transport=httpx2.MockTransport(capture),
        token_loader=lambda _config: "test-token",
    )

    response = await transport.list_messages(
        query="from:alice",
        page_token="page-1",
        max_results=20,
    )
    await transport.aclose()

    assert isinstance(response, Ok)
    assert_eq(response.value.status_code, 200)
    assert_eq(captured["method"], "GET")
    assert_eq(captured["path"], "/gmail/v1/users/me/messages")
    assert_eq(captured["auth"], "Bearer test-token")
    params = captured["params"]
    assert_eq(params.get("q"), "from:alice")
    assert_eq(params.get("pageToken"), "page-1")
    assert_eq(params.get("maxResults"), "20")
    assert "maxResults" in params


@test()
async def list_messages_omits_max_results_when_unset() -> None:
    """A legacy call keeps the request payload exactly equivalent."""

    captured: dict[str, object] = {}

    def capture(request: httpx2.Request) -> httpx2.Response:
        captured["params"] = dict(request.url.params)
        return httpx2.Response(200, json={"messages": []})

    transport = HttpGmailTransport(
        OAuthConfig(
            client_secret_path=Path("unused-client.json"),
            token_path=Path("unused-token.json"),
            scopes=(),
        ),
        http_transport=httpx2.MockTransport(capture),
        token_loader=lambda _config: "test-token",
    )

    response = await transport.list_messages(query="in:inbox", page_token="page-2")
    await transport.aclose()

    assert isinstance(response, Ok)
    assert_eq(response.value.status_code, 200)
    assert_eq(captured["params"], {"q": "in:inbox", "pageToken": "page-2"})


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
