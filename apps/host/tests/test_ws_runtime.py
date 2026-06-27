"""Guards that the running server can actually upgrade WebSocket connections.

uvicorn only performs the `/ws` handshake when a WebSocket protocol
implementation (`websockets` or `wsproto`) is installed. Without one, the
running app returns 404 on every `/ws` upgrade while the Starlette `TestClient`
suite stays green — TestClient speaks ASGI WebSockets natively and never
exercises uvicorn's upgrade path, so it cannot see the missing dependency. This
test pins the runtime dependency so that gap cannot reopen unnoticed.
"""

import importlib.util
import subprocess
import sys

from snektest import assert_eq, assert_true, test
from uvicorn.config import WS_PROTOCOLS

from tether.server import WS_PROTOCOL


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


@test(mark="slow")
def configured_websocket_protocol_is_free_of_legacy_deprecation() -> None:
    """The uvicorn WebSocket protocol tether configures must not load the
    deprecated `websockets.legacy` API.

    uvicorn's default `"auto"` resolves to the legacy `websockets` protocol, which
    imports `websockets.legacy` and emits a `DeprecationWarning` at import time.
    Import the configured implementation in a fresh interpreter under
    `-W error::DeprecationWarning` so any such warning becomes a non-zero exit —
    a clean import cache makes this deterministic regardless of test order.
    """
    target = WS_PROTOCOLS[WS_PROTOCOL]
    assert target is not None, f"WS protocol {WS_PROTOCOL!r} has no implementation"
    module_name = target.split(":", 1)[0]

    result = subprocess.run(
        [
            sys.executable,
            "-W",
            "error::DeprecationWarning",
            "-c",
            f"import {module_name}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert_eq(result.returncode, 0, msg=result.stderr)
