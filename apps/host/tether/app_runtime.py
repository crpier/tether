"""Typed retained services available during one host lifetime."""

import asyncio
from dataclasses import dataclass
from typing import cast

from starlette.applications import Starlette

from tether.bucket_item_search import BucketItemSearchService
from tether.bucket_items import BucketItemService
from tether.health_connect import HealthConnectIngestion, HealthConnectTelemetry
from tether.structured_logging import Logger
from tether.telemetry_model import Telemetry
from tether.todos import TodoService
from tether.triage import TriageService


@dataclass(frozen=True, slots=True)
class AppRuntime:
    """Complete dependency graph of the headless deterministic host."""

    bucket_item_search_service: BucketItemSearchService
    bucket_item_service: BucketItemService
    health_connect_ingestion: HealthConnectIngestion
    health_connect_telemetry: HealthConnectTelemetry
    logger: Logger
    tasks: tuple[asyncio.Task[None], ...]
    telemetry: Telemetry
    todo_service: TodoService
    triage_service: TriageService


def install_app_runtime(app: Starlette, runtime: AppRuntime) -> None:
    """Install the runtime before request-serving begins."""
    app.state.runtime = runtime


def app_runtime(app: Starlette) -> AppRuntime:
    """Return the initialized runtime for an application."""
    return cast("AppRuntime", app.state.runtime)


__all__ = ["AppRuntime", "app_runtime", "install_app_runtime"]
