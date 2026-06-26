"""Starlette server for the Tether host: wires the Memory service over HTTP.

>>> app = create_app()
>>> # uvicorn tether.server:app
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

from anyio import Path as AsyncPath
from snekql.sqlite import Config, Database
from starlette.applications import Starlette

from tether.memories import (
    KnowledgeBaseService,
    MemoryService,
    create_memory_schema,
)
from tether.openapi import openapi_routes
from tether.routes import routes


@asynccontextmanager
async def lifespan(app: Starlette) -> AsyncGenerator[None]:
    """Build the Memory service for the app's lifetime and tear it down after.

    Initialise the snekql `Database` and `KnowledgeBaseService`, construct
    the `MemoryService`, and stash it on `app.state.memory_service` so route
    handlers can reach it. Close the database on shutdown.

    The KB root is created up front because `set_projection` writes a temp file
    into it; tethering would otherwise fail on a fresh checkout where the
    directory does not yet exist.
    """
    kb_root = Path(".tether")
    await AsyncPath(kb_root).mkdir(parents=True, exist_ok=True)
    async with await Database.initialize(
        # Yep, it's hardcoded. What are you going to do? Sue me?
        # jk, actually I should change it before merging in main
        backend=Config(database=":memory:"),
    ) as db:
        await create_memory_schema(db)
        kb_service = KnowledgeBaseService(kb_root=kb_root)
        app.state.memory_service = MemoryService(database=db, kb_service=kb_service)
        yield


def create_app() -> Starlette:
    """Construct the Starlette application with Memory routes and lifespan wiring.

    The Memory routes are also handed to `openapi_routes` so `/openapi.json`
    and `/docs` describe exactly the API that is mounted.
    """
    docs = openapi_routes(routes, title="Tether", version="0.1.0")
    return Starlette(routes=[*routes, *docs], lifespan=lifespan)


app = create_app()
"""Module-level ASGI app for `uvicorn tether.server:app`."""
