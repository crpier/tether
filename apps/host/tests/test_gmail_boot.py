"""Wiring tests for the Gmail ingestion gate's disabled/credential-less boot.

Gmail composition must be a genuine no-op whenever the gate is off or no OAuth
transport is configured, so a fresh checkout never touches mail.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import structlog
from anyio import TemporaryDirectory
from opentelemetry import trace
from snekok import Ok, Result
from snekql.sqlite import Config, Database
from snektest import assert_true, test

from tether.agent_trace import AgentTraceRecorder
from tether.gmail_client import GmailNetworkFailure, GmailResponse
from tether.gmail_store import create_gmail_schema
from tether.host_config import AppConfig
from tether.host_resources import HostBootstrap
from tether.ingestion_composition import compose_gmail
from tether.ingestion_lifecycle import IngestionLifecycle
from tether.local_dependencies import LocalSttTransport
from tether.memories import MemoryService
from tether.memory_projection import KnowledgeBaseService
from tether.memory_store import create_memory_schema
from tether.model_selection import AgentModelCatalog
from tether.stt import SttClient
from tether.todos import TodoService, create_todo_schema
from tether.tools import SessionRegistry
from tether.triggers import TriggerService, create_trigger_schema


class FakeGmailTransport:
    """A transport that would fail the test if ever called."""

    async def list_messages(
        self, *, query: str, page_token: str | None
    ) -> Result[GmailResponse, GmailNetworkFailure]:
        message = "the disabled gate must never call the Gmail transport"
        raise AssertionError(message)

    async def get_message(
        self, message_id: str
    ) -> Result[GmailResponse, GmailNetworkFailure]:
        message = "the disabled gate must never call the Gmail transport"
        raise AssertionError(message)

    async def list_labels(self) -> Result[GmailResponse, GmailNetworkFailure]:
        message = "the disabled gate must never call the Gmail transport"
        raise AssertionError(message)

    async def modify_labels(
        self,
        message_id: str,
        *,
        add_label_ids: Sequence[str],
        remove_label_ids: Sequence[str],
    ) -> Result[GmailResponse, GmailNetworkFailure]:
        message = "the disabled gate must never call the Gmail transport"
        raise AssertionError(message)

    async def trash_message(
        self, message_id: str
    ) -> Result[GmailResponse, GmailNetworkFailure]:
        message = "the disabled gate must never call the Gmail transport"
        raise AssertionError(message)


@dataclass
class BootGmailTransport:
    """Record boot requests while returning one configured provider status."""

    status_code: int = 200
    label_calls: int = 0
    list_calls: int = 0

    async def list_messages(
        self, *, query: str, page_token: str | None
    ) -> Result[GmailResponse, GmailNetworkFailure]:
        _ = query, page_token
        self.list_calls += 1
        return Ok(GmailResponse(payload={"messages": []}, status_code=self.status_code))

    async def get_message(
        self, message_id: str
    ) -> Result[GmailResponse, GmailNetworkFailure]:
        _ = message_id
        return Ok(GmailResponse(payload={}, status_code=self.status_code))

    async def list_labels(self) -> Result[GmailResponse, GmailNetworkFailure]:
        self.label_calls += 1
        return Ok(GmailResponse(payload={"labels": []}, status_code=self.status_code))

    async def modify_labels(
        self,
        message_id: str,
        *,
        add_label_ids: Sequence[str],
        remove_label_ids: Sequence[str],
    ) -> Result[GmailResponse, GmailNetworkFailure]:
        _ = message_id, add_label_ids, remove_label_ids
        return Ok(GmailResponse(payload={}, status_code=self.status_code))

    async def trash_message(
        self, message_id: str
    ) -> Result[GmailResponse, GmailNetworkFailure]:
        _ = message_id
        return Ok(GmailResponse(payload={}, status_code=self.status_code))


async def _wire(config: AppConfig) -> asyncio.Event:
    """Compose Gmail over bare dependencies for disabled-gate assertions."""
    db = await Database.initialize(backend=Config(database=":memory:"))
    await create_memory_schema(db)
    await create_trigger_schema(db)
    await create_todo_schema(db)
    await create_gmail_schema(db)
    logger = structlog.stdlib.get_logger("test.gmail_boot")
    ingestion_lifecycle = IngestionLifecycle(logger)
    tracer = trace.NoOpTracerProvider().get_tracer("test.gmail_boot")
    try:
        async with TemporaryDirectory() as kb_root:
            kb_service = KnowledgeBaseService(kb_root=Path(kb_root))
            memory_service = MemoryService(
                database=db, kb_service=kb_service, tracer=tracer
            )
            trigger_service = TriggerService(database=db, tracer=tracer)
            todo_service = TodoService(database=db, tracer=tracer)
            await compose_gmail(
                bootstrap=HostBootstrap(
                    session_registry=SessionRegistry(),
                    stt_client=SttClient(
                        transport=LocalSttTransport(), model="test-stt"
                    ),
                    tool_secret="test-tool-secret",
                    trace_recorder=AgentTraceRecorder(),
                ),
                config=config,
                database=db,
                ingestion_lifecycle=ingestion_lifecycle,
                kb_root=Path(kb_root),
                logger=logger,
                memory_service=memory_service,
                model_catalog=AgentModelCatalog(default_model=None, models=()),
                trigger_service=trigger_service,
                todo_service=todo_service,
            )
            readiness = ingestion_lifecycle.readiness("gmail")
            await asyncio.wait_for(readiness.wait(), timeout=1)
            return readiness
    finally:
        await ingestion_lifecycle.stop(grace_seconds=0.1)
        await db.close()


@test()
async def a_default_config_wires_no_background_task() -> None:
    """The gate's own defaults (disabled, no transport) wire nothing."""
    readiness = await _wire(AppConfig(app_password="pw", session_secret="s"))

    assert_true(readiness.is_set())


@test()
async def a_configured_transport_without_the_enabled_flag_wires_nothing() -> None:
    """A transport alone, without the explicit enable flag, still wires nothing."""
    readiness = await _wire(
        AppConfig(
            app_password="pw",
            session_secret="s",
            gmail_transport=FakeGmailTransport(),
            gmail_sync_enabled=False,
        )
    )

    assert_true(readiness.is_set())


@test()
async def the_enabled_flag_without_a_transport_wires_nothing() -> None:
    """The enable flag alone, without a configured transport, still wires nothing."""
    readiness = await _wire(
        AppConfig(
            app_password="pw",
            session_secret="s",
            gmail_sync_enabled=True,
            gmail_transport=None,
        )
    )

    assert_true(readiness.is_set())


@test()
async def an_authenticated_provider_completes_the_boot_sync() -> None:
    """A successful provider response lists mail before readiness is released."""
    transport = BootGmailTransport()

    readiness = await _wire(
        AppConfig(
            app_password="pw",
            session_secret="s",
            gmail_sync_enabled=True,
            gmail_transport=transport,
        )
    )

    assert_true(readiness.is_set())
    assert transport.label_calls == 1
    assert transport.list_calls == 1


@test()
async def an_authentication_rejection_stops_after_the_boot_request() -> None:
    """A rejected credential does not enter periodic synchronization."""
    transport = BootGmailTransport(status_code=401)

    readiness = await _wire(
        AppConfig(
            app_password="pw",
            session_secret="s",
            gmail_sync_enabled=True,
            gmail_transport=transport,
        )
    )

    assert_true(readiness.is_set())
    assert transport.label_calls == 1
    assert transport.list_calls == 0
