"""Public boundary tests for Readwise HTTP transports."""

from __future__ import annotations

import httpx2
from snekok import Err
from snektest import assert_eq, test

from tether.readwise_http import (
    HttpReaderTransport,
    HttpReadwiseTransport,
    ReadwiseNetworkFailure,
)


@test()
async def a_reader_network_error_is_a_typed_transport_failure() -> None:
    """Reader request failures do not escape the async transport boundary."""

    def disconnect(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("connection reset", request=request)

    transport = HttpReaderTransport(
        "token", http_transport=httpx2.MockTransport(disconnect)
    )
    response = await transport.fetch_list(
        updated_after=None, category="epub", page_cursor=None
    )
    await transport.aclose()

    assert isinstance(response, Err)
    assert_eq(
        response.error,
        ReadwiseNetworkFailure(operation="list", message="connection reset"),
    )


@test()
async def a_network_error_is_a_typed_transport_failure() -> None:
    """Request failures do not escape the async transport boundary."""

    def disconnect(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("connection reset", request=request)

    transport = HttpReadwiseTransport(
        "token", http_transport=httpx2.MockTransport(disconnect)
    )
    response = await transport.fetch_export(
        updated_after=None, page_cursor=None, include_deleted=False
    )
    await transport.aclose()

    assert isinstance(response, Err)
    assert_eq(
        response.error,
        ReadwiseNetworkFailure(operation="export", message="connection reset"),
    )
