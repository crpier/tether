"""Host lifecycle tests for Readwise and Reader ingestion gates."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from pathlib import Path

import structlog
from anyio import TemporaryDirectory
from opentelemetry import trace
from snekok import Ok, Result
from snekql.sqlite import Config, Database
from snektest import assert_eq, assert_true, fixture, load_fixture, test

from tether.host_composition import _wire_reader, _wire_readwise
from tether.host_config import AppConfig
from tether.ingestion_lifecycle import IngestionLifecycle
from tether.memories import KnowledgeBaseService, MemoryService, create_memory_schema
from tether.readwise_http import ReadwiseNetworkFailure, ReadwiseResponse
from tether.readwise_store import create_readwise_schema
from tether.structured_logging import Logger


@dataclass
class FakeReadwiseTransport:
    """Record token and export calls while returning one configured status."""

    auth_status: int = 204
    close_calls: int = 0
    export_calls: int = 0
    token_calls: int = 0

    async def fetch_export(
        self,
        *,
        updated_after: object,
        page_cursor: str | None,
        include_deleted: bool,
    ) -> Result[ReadwiseResponse, ReadwiseNetworkFailure]:
        self.export_calls += 1
        return Ok(ReadwiseResponse(payload={"results": [], "nextPageCursor": None}))

    async def verify_token(
        self,
    ) -> Result[ReadwiseResponse, ReadwiseNetworkFailure]:
        self.token_calls += 1
        return Ok(ReadwiseResponse(payload={}, status_code=self.auth_status))

    async def aclose(self) -> None:
        """Record lifecycle-owned closure."""
        self.close_calls += 1


@dataclass
class FakeReaderTransport:
    """Record Reader list calls while returning one configured status."""

    status_code: int = 200
    close_calls: int = 0
    list_calls: int = 0

    async def fetch_list(
        self,
        *,
        updated_after: object,
        category: str,
        page_cursor: str | None,
    ) -> Result[ReadwiseResponse, ReadwiseNetworkFailure]:
        self.list_calls += 1
        return Ok(
            ReadwiseResponse(
                payload={"results": [], "nextPageCursor": None},
                status_code=self.status_code,
            )
        )

    async def aclose(self) -> None:
        """Record lifecycle-owned closure."""
        self.close_calls += 1


@dataclass
class ReadwiseBootEnvironment:
    """Dependencies shared by focused gate wiring tests."""

    database: Database
    lifecycle: IngestionLifecycle
    logger: Logger
    memory_service: MemoryService
    resources: contextlib.AsyncExitStack


@fixture
async def readwise_boot_environment() -> AsyncGenerator[ReadwiseBootEnvironment]:
    """A database, Memory service, lifecycle, and resource owner."""
    database = await Database.initialize(backend=Config(database=":memory:"))
    await create_memory_schema(database)
    await create_readwise_schema(database)
    logger = structlog.stdlib.get_logger("test.readwise_boot")
    lifecycle = IngestionLifecycle(logger)
    resources = contextlib.AsyncExitStack()
    await resources.__aenter__()
    async with TemporaryDirectory() as kb_root:
        yield ReadwiseBootEnvironment(
            database=database,
            lifecycle=lifecycle,
            logger=logger,
            memory_service=MemoryService(
                database=database,
                kb_service=KnowledgeBaseService(kb_root=Path(kb_root)),
                tracer=trace.NoOpTracerProvider().get_tracer("test.readwise_boot"),
            ),
            resources=resources,
        )
    await lifecycle.stop(grace_seconds=0.1)
    await resources.aclose()
    await database.close()


@test()
async def a_disabled_readwise_gate_is_immediately_ready() -> None:
    """Default configuration starts no Readwise background work."""
    environment = await load_fixture(readwise_boot_environment())

    await _wire_readwise(
        config=AppConfig(app_password="pw", session_secret="secret"),
        database=environment.database,
        ingestion_lifecycle=environment.lifecycle,
        logger=environment.logger,
        memory_service=environment.memory_service,
        resources=environment.resources,
    )

    assert_true(environment.lifecycle.readiness("readwise").is_set())


@test()
async def an_authenticated_readwise_gate_completes_its_boot_sync() -> None:
    """A valid token performs one Export pass before becoming ready."""
    environment = await load_fixture(readwise_boot_environment())
    transport = FakeReadwiseTransport()
    await _wire_readwise(
        config=AppConfig(
            app_password="pw",
            session_secret="secret",
            readwise_sync_enabled=True,
            readwise_transport=transport,
        ),
        database=environment.database,
        ingestion_lifecycle=environment.lifecycle,
        logger=environment.logger,
        memory_service=environment.memory_service,
        resources=environment.resources,
    )

    await asyncio.wait_for(
        environment.lifecycle.readiness("readwise").wait(), timeout=1
    )

    assert_eq(transport.token_calls, 1)
    assert_eq(transport.export_calls, 1)


@test()
async def an_owned_readwise_transport_closes_once() -> None:
    """Host resources close the provider transport after stopping its gate."""
    environment = await load_fixture(readwise_boot_environment())
    transport = FakeReadwiseTransport()
    await _wire_readwise(
        config=AppConfig(
            app_password="pw",
            session_secret="secret",
            readwise_sync_enabled=True,
            readwise_transport=transport,
        ),
        database=environment.database,
        ingestion_lifecycle=environment.lifecycle,
        logger=environment.logger,
        memory_service=environment.memory_service,
        resources=environment.resources,
    )
    await asyncio.wait_for(
        environment.lifecycle.readiness("readwise").wait(), timeout=1
    )
    await environment.lifecycle.stop(grace_seconds=0.1)

    await environment.resources.aclose()

    assert_eq(transport.close_calls, 1)


@test()
async def a_rejected_readwise_token_stops_before_export() -> None:
    """Authentication rejection disables the gate without advancing state."""
    environment = await load_fixture(readwise_boot_environment())
    transport = FakeReadwiseTransport(auth_status=401)
    await _wire_readwise(
        config=AppConfig(
            app_password="pw",
            session_secret="secret",
            readwise_sync_enabled=True,
            readwise_transport=transport,
        ),
        database=environment.database,
        ingestion_lifecycle=environment.lifecycle,
        logger=environment.logger,
        memory_service=environment.memory_service,
        resources=environment.resources,
    )

    await asyncio.wait_for(
        environment.lifecycle.readiness("readwise").wait(), timeout=1
    )

    assert_eq(transport.token_calls, 1)
    assert_eq(transport.export_calls, 0)


@test()
async def a_rejected_reader_token_stops_after_the_boot_request() -> None:
    """Reader authentication rejection does not enter periodic execution."""
    environment = await load_fixture(readwise_boot_environment())
    transport = FakeReaderTransport(status_code=401)
    await _wire_reader(
        config=AppConfig(
            app_password="pw",
            session_secret="secret",
            readwise_reader_sync_enabled=True,
            reader_transport=transport,
        ),
        database=environment.database,
        ingestion_lifecycle=environment.lifecycle,
        logger=environment.logger,
        memory_service=environment.memory_service,
        resources=environment.resources,
    )

    await asyncio.wait_for(
        environment.lifecycle.readiness("readwise-reader").wait(), timeout=1
    )

    assert_eq(transport.list_calls, 1)
