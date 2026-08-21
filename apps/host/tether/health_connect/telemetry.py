"""Typed composition of read-only Health Connect telemetry queries."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

from snekql.sqlite import Database

from tether.health_connect.contracts import HealthRecordType
from tether.health_connect.inventory import HealthConnectInventoryQuery
from tether.health_connect.records import HealthConnectRecordQuery
from tether.health_connect.summary import HealthConnectSummaryQuery
from tether.health_connect.telemetry_model import (
    HealthConnectInventoryEntry,
    HealthConnectQueryRead,
    HealthConnectSummaryRead,
)


class HealthConnectInventoryPort(Protocol):
    """Inventory query required by Health Connect tool presentation."""

    async def fetch_inventory(self) -> list[HealthConnectInventoryEntry]:
        """List populated current projections and their observed bounds."""
        ...


class HealthConnectRecordPort(Protocol):
    """Bounded current-record query required by tool presentation."""

    async def fetch_records(
        self,
        *,
        record_type: HealthRecordType,
        after: datetime | None,
        before: datetime | None,
        limit: int,
    ) -> HealthConnectQueryRead:
        """Read one current projection with matching-set metadata."""
        ...


class HealthConnectSummaryPort(Protocol):
    """Aggregate query required by Health Connect tool presentation."""

    async def fetch_summary(
        self,
        *,
        after: datetime,
        before: datetime,
        bucket: Literal["none", "day"],
    ) -> HealthConnectSummaryRead:
        """Aggregate current records overlapping a bounded time window."""
        ...


@dataclass(frozen=True, slots=True)
class HealthConnectTelemetry:
    """Canonical bundle of independent Health Connect read concerns."""

    inventory: HealthConnectInventoryPort
    records: HealthConnectRecordPort
    summary: HealthConnectSummaryPort

    @classmethod
    def from_database(cls, database: Database) -> HealthConnectTelemetry:
        """Compose every read concern over the canonical telemetry database."""
        return cls(
            inventory=HealthConnectInventoryQuery(database),
            records=HealthConnectRecordQuery(database),
            summary=HealthConnectSummaryQuery(database),
        )
