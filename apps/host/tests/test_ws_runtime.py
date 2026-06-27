"""Guards that the running server can actually upgrade WebSocket connections.

uvicorn only performs the `/ws` handshake when a WebSocket protocol
implementation (`websockets` or `wsproto`) is installed. Without one, the
running app returns 404 on every `/ws` upgrade while the Starlette `TestClient`
suite stays green — TestClient speaks ASGI WebSockets natively and never
exercises uvicorn's upgrade path, so it cannot see the missing dependency. This
test pins the runtime dependency so that gap cannot reopen unnoticed.
"""

import importlib.util

from snektest import assert_true, test


@test()
def uvicorn_has_a_websocket_implementation() -> None:
    """A uvicorn-compatible WebSocket implementation must be importable."""
    has_implementation = (
        importlib.util.find_spec("websockets") is not None
        or importlib.util.find_spec("wsproto") is not None
    )
    assert_true(
        has_implementation,
        msg=(
            "no uvicorn WebSocket implementation installed (websockets/wsproto); "
            "the /ws upgrade will 404 in the running app"
        ),
    )
