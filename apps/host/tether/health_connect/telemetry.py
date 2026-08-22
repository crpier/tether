"""Typed composition of read-only Health Connect telemetry queries."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

from snekql.sqlite import Database

from tether.health_connect.contracts import HealthRecordType
from tether.health_connect.insight_model import (
    HealthConnectMetricStatusRead,
    HealthConnectSleepEpisodeInsightRead,
    HealthConnectSleepingHeartRateInsightRead,
    HealthConnectSleepTrendInsightRead,
)
from tether.health_connect.insights import HealthConnectInsightQuery
from tether.health_connect.inventory import HealthConnectInventoryQuery
from tether.health_connect.records import HealthConnectRecordQuery
from tether.health_connect.summary import HealthConnectSummaryQuery
from tether.health_connect.telemetry_model import (
    HealthConnectInventoryEntry,
    HealthConnectQueryRead,
    HealthConnectSummaryRead,
)


class HealthConnectInsightPort(Protocol):
    """Episode-aware deterministic query required by Health chat tools."""

    async def fetch_metric_status(
        self, *, record_type: HealthRecordType
    ) -> HealthConnectMetricStatusRead:
        """Read synchronization and record availability for one metric."""
        ...

    async def fetch_sleep_episode(
        self,
        *,
        days: int,
        episode_kind: Literal["latest", "nap", "primary_sleep"],
    ) -> HealthConnectSleepEpisodeInsightRead:
        """Read one compact sleep episode with measured details."""
        ...

    async def fetch_sleep_trend(
        self, *, days: int
    ) -> HealthConnectSleepTrendInsightRead:
        """Read daily sleep observations and comparable recent windows."""
        ...

    async def fetch_sleeping_heart_rate(
        self, *, days: int
    ) -> HealthConnectSleepingHeartRateInsightRead:
        """Read sleep-aligned heart rate with a personal baseline."""
        ...


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

    insights: HealthConnectInsightPort
    inventory: HealthConnectInventoryPort
    records: HealthConnectRecordPort
    summary: HealthConnectSummaryPort

    @classmethod
    def from_database(cls, database: Database) -> HealthConnectTelemetry:
        """Compose every read concern over the canonical telemetry database."""
        return cls(
            insights=HealthConnectInsightQuery(database),
            inventory=HealthConnectInventoryQuery(database),
            records=HealthConnectRecordQuery(database),
            summary=HealthConnectSummaryQuery(database),
        )
