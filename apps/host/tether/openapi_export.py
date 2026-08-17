"""Emit FastAPI's OpenAPI document for generated web clients."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI

from tether.artifact_routes import router as artifact_router
from tether.auth_routes import router as auth_router
from tether.bucket_routes import router as bucket_router
from tether.capture_routes import router as capture_router
from tether.conversation_routes import router as conversation_router
from tether.health_connect import router as health_connect_router
from tether.kosync_routes import router as ebook_router
from tether.model_selection import router as model_router
from tether.notification_routes import router as notification_router
from tether.panel_routes import router as panel_router
from tether.proposal_routes import router as proposal_router
from tether.provider_auth_routes import router as provider_auth_router
from tether.push_routes import router as push_router
from tether.recall_routes import router as recall_router
from tether.routes import router as memory_router
from tether.search_routes import router as search_router
from tether.stt_routes import router as stt_router
from tether.todo_routes import router as todo_router
from tether.trigger_routes import router as trigger_router
from tether.youtube_auth_routes import router as youtube_auth_router
from tether.youtube_routes import router as youtube_router

_EXPECTED_ARGUMENT_COUNT = 2
_PUBLIC_ROUTERS = (
    auth_router,
    memory_router,
    capture_router,
    health_connect_router,
    bucket_router,
    todo_router,
    search_router,
    youtube_router,
    youtube_auth_router,
    conversation_router,
    model_router,
    trigger_router,
    push_router,
    recall_router,
    notification_router,
    artifact_router,
    panel_router,
    ebook_router,
    stt_router,
    proposal_router,
    provider_auth_router,
)


def public_api_router() -> APIRouter:
    """Return all browser-facing REST routes described by OpenAPI.

    ```python
    paths = build_openapi_document()["paths"]
    assert "/api/auth/session" in paths
    ```
    """
    router = APIRouter()
    for public_router in _PUBLIC_ROUTERS:
        router.include_router(public_router)
    return router


def build_openapi_document() -> dict[str, Any]:
    """Build Tether's browser REST OpenAPI document with FastAPI."""
    app = FastAPI(title="Tether", version="0.1.0")
    app.include_router(public_api_router())
    return app.openapi()


def write_openapi_document(output_path: str | Path) -> None:
    """Write Tether's OpenAPI document as stable formatted JSON."""
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
