"""Wiring tests for the Gmail ingestion gate's disabled/credential-less boot.

`_wire_gmail` must be a genuine no-op — no task, no app.state, no ephemeral pi
config built — whenever the gate is off or no OAuth transport is configured, so
a fresh checkout never touches mail. These drive `_wire_gmail` directly against
a bare `Starlette` app with no `model_catalog`/`session_registry` on state at
all: reaching past the early return would raise `AttributeError`, so a passing
test is itself proof the wiring short-circuited before touching mail.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from anyio import TemporaryDirectory
from opentelemetry import trace
from snekql.sqlite import Config, Database
from snektest import assert_eq, test
from starlette.applications import Starlette

from tether.gmail import GmailResponse, create_gmail_schema
from tether.memories import KnowledgeBaseService, MemoryService, create_memory_schema
from tether.server import AppConfig, _wire_gmail
from tether.todos import TodoService, create_todo_schema
from tether.triggers import TriggerService, create_trigger_schema


class FakeGmailTransport:
    """A transport that would fail the test if ever called."""

    async def list_messages(
        self, *, query: str, page_token: str | None
    ) -> GmailResponse:
        message = "the disabled gate must never call the Gmail transport"
        raise AssertionError(message)

    async def get_message(self, message_id: str) -> GmailResponse:
        message = "the disabled gate must never call the Gmail transport"
        raise AssertionError(message)

    async def list_labels(self) -> GmailResponse:
        message = "the disabled gate must never call the Gmail transport"
        raise AssertionError(message)


async def _wire(config: AppConfig) -> list[asyncio.Task[None]]:
    """Run `_wire_gmail` against a bare app/db, for the disabled-wiring assertions."""
    db = await Database.initialize(backend=Config(database=":memory:"))
    await create_memory_schema(db)
    await create_trigger_schema(db)
    await create_todo_schema(db)
    await create_gmail_schema(db)
    app = Starlette()
    tracer = trace.NoOpTracerProvider().get_tracer("test.gmail_boot")
    try:
        async with TemporaryDirectory() as kb_root:
            kb_service = KnowledgeBaseService(kb_root=Path(kb_root))
            memory_service = MemoryService(
                database=db, kb_service=kb_service, tracer=tracer
            )
            trigger_service = TriggerService(database=db, tracer=tracer)
            todo_service = TodoService(database=db, tracer=tracer)
            return await _wire_gmail(
                app,
                config=config,
                database=db,
                memory_service=memory_service,
                trigger_service=trigger_service,
                todo_service=todo_service,
                kb_root=Path(kb_root),
            )
    finally:
        await db.close()


@test()
async def a_default_config_wires_no_background_task() -> None:
    """The gate's own defaults (disabled, no transport) wire nothing."""
    tasks = await _wire(AppConfig(app_password="pw", session_secret="s"))

    assert_eq(tasks, [])


@test()
async def a_configured_transport_without_the_enabled_flag_wires_nothing() -> None:
    """A transport alone, without the explicit enable flag, still wires nothing."""
    tasks = await _wire(
        AppConfig(
            app_password="pw",
            session_secret="s",
            gmail_transport=FakeGmailTransport(),
            gmail_sync_enabled=False,
        )
    )

    assert_eq(tasks, [])


@test()
async def the_enabled_flag_without_a_transport_wires_nothing() -> None:
    """The enable flag alone, without a configured transport, still wires nothing."""
    tasks = await _wire(
        AppConfig(
            app_password="pw",
            session_secret="s",
            gmail_sync_enabled=True,
            gmail_transport=None,
        )
    )

    assert_eq(tasks, [])
