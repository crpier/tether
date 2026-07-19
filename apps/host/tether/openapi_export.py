"""Emit the OpenAPI document used by generated web clients."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from starlette.routing import Route

from tether.artifact_routes import artifact_routes
from tether.auth import auth_routes
from tether.bucket_routes import bucket_item_routes
from tether.conversations import conversation_routes
from tether.kosync_routes import ebook_routes
from tether.model_selection import model_routes
from tether.notifications import notification_routes
from tether.openapi import build_openapi
from tether.panel_routes import panel_routes
from tether.push import push_routes
from tether.recall_routes import recall_routes
from tether.routes import routes
from tether.search_routes import search_routes
from tether.trigger_routes import trigger_routes
from tether.youtube_routes import youtube_routes

_EXPECTED_ARGUMENT_COUNT = 2


def public_api_routes() -> list[Route]:
    """Return the browser-facing REST routes described by OpenAPI.

    ```python
    paths = build_openapi_document()["paths"]
    assert "/api/auth/session" in paths
    ```
    """
    return [
        *auth_routes,
        *routes,
        *bucket_item_routes,
        *search_routes,
        *youtube_routes,
        *conversation_routes,
        *model_routes,
        *trigger_routes,
        *push_routes,
        *recall_routes,
        *notification_routes,
        *artifact_routes,
        *panel_routes,
        *ebook_routes,
    ]


def build_openapi_document() -> dict[str, Any]:
    """Build Tether's browser REST OpenAPI document.

    ```python
    build_openapi_document()["openapi"]
    # '3.1.0'
    ```
    """
    return build_openapi(public_api_routes(), title="Tether", version="0.1.0")


def write_openapi_document(output_path: str | Path) -> None:
    """Write Tether's OpenAPI document as stable formatted JSON.

    ```python
    write_openapi_document("openapi.json")
    ```
    """
    _ = Path(output_path).write_text(
        f"{json.dumps(build_openapi_document(), indent=2, sort_keys=True)}\n"
    )


def main() -> None:
    """Console entrypoint for `python -m tether.openapi_export`."""
    if len(sys.argv) != _EXPECTED_ARGUMENT_COUNT:
        _ = sys.stderr.write("usage: python -m tether.openapi_export <output-path>\n")
        raise SystemExit(2)
    write_openapi_document(sys.argv[1])


if __name__ == "__main__":
    main()
