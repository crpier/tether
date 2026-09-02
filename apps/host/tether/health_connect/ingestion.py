"""Atomic Health Connect cursor, baseline, replay, and append workflow."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any, Protocol, cast

from snekok.result import Err, Ok, Result
from snekql.sqlite import (
    Database,
    DoUpdate,
    Fetched,
    Transaction,
    delete,
    insert,
    not_exists,
    select,
    update,
)

from tether.health_connect.contracts import (
    GENERIC_RECORD_TYPES,
    CompleteHealthConnectBaselineRequest,
    DuplicateRecordTypesError,
    HealthConnectBaselineCompletionRead,
    HealthConnectBatchRead,
    HealthConnectBatchRequest,
    HealthConnectDeletion,
    HealthConnectRecords,
    HealthConnectStepAggregateSnapshotRead,
    HealthConnectStepAggregateSnapshotRequest,
    HealthConnectSyncStateRead,
    HealthRecordType,
    RecordStatus,
    UnsupportedRecordTypesError,
    canonical_record_types,
    parse_record_types,
    validate_versioned_record_types,
)
from tether.health_connect.persistence import (
    HcBaselineSeen,
    HcExerciseSessionCurrent,
    HcHeartRateRecordCurrent,
    HcPageRequest,
    HcSleepSessionCurrent,
    HcStepAggregateBucket,
    HcStepAggregateBucketCurrent,
    HcStepAggregateSnapshot,
    HcStepIntervalCurrent,
    HealthConnectSyncState,
)
from tether.health_connect.record_writer import HealthConnectRecordWriter


def _empty_counts(
    record_types: tuple[HealthRecordType, ...],
) -> dict[HealthRecordType, int]:
    return dict.fromkeys(record_types, 0)


def _state_key(installation_id: str, record_types: tuple[HealthRecordType, ...]) -> str:
    return f"{installation_id}\x1f{','.join(record_types)}"


def _state_read(stored: HealthConnectSyncState[Fetched]) -> HealthConnectSyncStateRead:
    return HealthConnectSyncStateRead(
        baseline_generation=stored.baseline_generation,
        current_token=stored.current_token,
        installation_id=stored.installation_id,
        record_types=list(parse_record_types(stored.record_type_set)),
        status=cast("RecordStatus", stored.status),
    )


def _hash_json(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class HealthConnectCursorConflict:
    """A page or completion no longer matches durable cursor state."""


class HealthConnectContractFailure:
    """A validated request conflicts with the stream contract."""


@dataclass(frozen=True, slots=True)
class HealthConnectDuplicateRecordTypes(HealthConnectContractFailure):
    """A stream identity repeats a record type."""


@dataclass(frozen=True, slots=True)
class HealthConnectUnsupportedRecordTypes(HealthConnectContractFailure):
    """A stream contains record types unavailable to its contract version."""


@dataclass(frozen=True, slots=True)
class HealthConnectRequestIdentityConflict:
    """A committed request ID was presented with different page content."""


type HealthConnectBaselineFailure = (
    HealthConnectContractFailure | HealthConnectCursorConflict
)
type HealthConnectBatchFailure = (
    HealthConnectBaselineFailure | HealthConnectRequestIdentityConflict
)


class HealthConnectRecordSink(Protocol):
    """Record persistence needed by the atomic cursor workflow."""

    async def append_records(
        self,
        transaction: Transaction,
        batch: HealthConnectBatchRequest,
        received_at: int,
        accepted: dict[HealthRecordType, int],
        skipped: dict[HealthRecordType, int],
    ) -> None:
        """Append changed records while counting accepted and skipped inputs."""
        ...

    async def append_deletions(
        self,
        transaction: Transaction,
        batch: HealthConnectBatchRequest,
        received_at: int,
        deleted: dict[HealthRecordType, int],
        skipped: dict[HealthRecordType, int],
    ) -> None:
        """Append tombstones while counting deleted and skipped inputs."""
        ...


class HealthConnectIngestion:
    """Atomic Health Connect cursor, baseline, replay, and append gate.

    Example:
        ingestion = HealthConnectIngestion(database)
        state = await ingestion.fetch_sync_state("phone", ("steps",))
    """

    def __init__(
        self,
        database: Database,
        *,
        record_sink: HealthConnectRecordSink | None = None,
    ) -> None:
        self.database: Database = database
        self.record_sink: HealthConnectRecordSink = (
            record_sink if record_sink is not None else HealthConnectRecordWriter()
        )

    async def fetch_sync_state(
        self, installation_id: str, record_types: tuple[HealthRecordType, ...]
    ) -> HealthConnectSyncStateRead:
        async with self.database.transaction() as transaction:
            stored = await transaction.fetch_one_or_none(
                select(HealthConnectSyncState).where(
                    HealthConnectSyncState.state_key.eq(
                        _state_key(installation_id, record_types)
                    )
                )
            )
        if stored is None:
            return HealthConnectSyncStateRead(
                baseline_generation=0,
                current_token=None,
                installation_id=installation_id,
                record_types=list(record_types),
                status="initial",
            )
        return _state_read(stored)

    async def start_baseline(
        self,
        *,
        installation_id: str,
        record_types: tuple[HealthRecordType, ...],
        starting_token: str,
        request_id: str,
    ) -> HealthConnectSyncStateRead:
        key = _state_key(installation_id, record_types)
        async with self.database.transaction(mode="immediate") as transaction:
            stored = await transaction.fetch_one_or_none(
                select(HealthConnectSyncState).where(
                    HealthConnectSyncState.state_key.eq(key)
                )
            )
            if stored is not None and stored.baseline_request_id == request_id:
                return _state_read(stored)
            _ = await transaction.execute(
                delete(HcBaselineSeen).where(HcBaselineSeen.state_key.eq(key))
            )
            _ = await transaction.execute(
                insert(
                    HealthConnectSyncState(
                        baseline_generation=(
                            1 if stored is None else stored.baseline_generation + 1
                        ),
                        baseline_request_id=request_id,
                        completion_deleted_json=None,
                        completion_request_id=None,
                        current_token=starting_token,
                        installation_id=installation_id,
                        record_type_set=",".join(record_types),
                        state_key=key,
                        status="baseline",
                    )
                ).on_conflict(
                    HealthConnectSyncState.state_key,
                    action=DoUpdate(
                        HealthConnectSyncState.baseline_generation.to_inserted(),
                        HealthConnectSyncState.baseline_request_id.to_inserted(),
                        HealthConnectSyncState.completion_deleted_json.to_inserted(),
                        HealthConnectSyncState.completion_request_id.to_inserted(),
                        HealthConnectSyncState.current_token.to_inserted(),
                        HealthConnectSyncState.status.to_inserted(),
                    ),
                )
            )
            persisted = await transaction.fetch_one(
                select(HealthConnectSyncState).where(
                    HealthConnectSyncState.state_key.eq(key)
                )
            )
        return _state_read(persisted)

    async def complete_baseline(
        self, body: CompleteHealthConnectBaselineRequest
    ) -> Result[HealthConnectBaselineCompletionRead, HealthConnectBaselineFailure]:
        """Reconcile only bounded authoritative ranges and enter changes mode."""
        try:
            record_types = canonical_record_types(list(body.record_types))
            validate_versioned_record_types(body.contract_version, record_types)
        except (DuplicateRecordTypesError, UnsupportedRecordTypesError) as error:
            failure = (
                HealthConnectDuplicateRecordTypes()
                if isinstance(error, DuplicateRecordTypesError)
                else HealthConnectUnsupportedRecordTypes()
            )
            return Err(failure)
        key = _state_key(body.installation_id, record_types)
        async with self.database.transaction(mode="immediate") as transaction:
            state = await transaction.fetch_one_or_none(
                select(HealthConnectSyncState).where(
                    HealthConnectSyncState.state_key.eq(key)
                )
            )
            if state is not None and state.completion_request_id == body.request_id:
                return Ok(
                    HealthConnectBaselineCompletionRead(
                        deleted=json.loads(state.completion_deleted_json or "{}"),
                        status="completed",
                    )
                )
            if (
                state is None
                or state.current_token != body.expected_token
                or state.baseline_generation != body.baseline_generation
                or state.status != "baseline"
            ):
                return Err(HealthConnectCursorConflict())
            deleted = await self._reconcile_baseline(
                transaction, body, key, record_types
            )
            _ = await transaction.execute(
                update(HealthConnectSyncState)
                .set(
                    HealthConnectSyncState.completion_deleted_json.to(
                        json.dumps(deleted, sort_keys=True)
                    ),
                    HealthConnectSyncState.completion_request_id.to(body.request_id),
                    HealthConnectSyncState.status.to("changes"),
                )
                .where(HealthConnectSyncState.state_key.eq(key))
            )
        return Ok(
            HealthConnectBaselineCompletionRead(deleted=deleted, status="completed")
        )

    async def ingest_step_aggregate_snapshot(
        self, snapshot: HealthConnectStepAggregateSnapshotRequest
    ) -> Result[
        HealthConnectStepAggregateSnapshotRead,
        HealthConnectRequestIdentityConflict,
    ]:
        """Replace one authoritative canonical-step range append-only."""
        payload_hash = _hash_json(snapshot.model_dump(mode="json"))
        async with self.database.transaction(mode="immediate") as transaction:
            replay = await transaction.fetch_one_or_none(
                select(HcStepAggregateSnapshot).where(
                    HcStepAggregateSnapshot.request_id.eq(snapshot.request_id)
                )
            )
            if replay is not None:
                if replay.payload_hash != payload_hash:
                    return Err(HealthConnectRequestIdentityConflict())
                return Ok(
                    HealthConnectStepAggregateSnapshotRead(
                        accepted=replay.accepted_count,
                        deleted=replay.deleted_count,
                        replayed=True,
                        skipped=replay.skipped_count,
                        status="accepted",
                    )
                )

            latest_snapshot = await transaction.fetch_one_or_none(
                select(HcStepAggregateSnapshot)
                .all()
                .order_by(
                    HcStepAggregateSnapshot.end_time.desc(),
                    HcStepAggregateSnapshot.snapshot_id.desc(),
                )
                .limit(1)
            )
            if (
                latest_snapshot is not None
                and snapshot.end_time < latest_snapshot.end_time
            ):
                skipped = len(snapshot.buckets)
                _ = await transaction.execute(
                    insert(
                        HcStepAggregateSnapshot(
                            accepted_count=0,
                            deleted_count=0,
                            end_time=snapshot.end_time,
                            installation_id=snapshot.installation_id,
                            payload_hash=payload_hash,
                            received_at=time.time_ns() // 1_000_000,
                            request_id=snapshot.request_id,
                            skipped_count=skipped,
                            start_time=snapshot.start_time,
                        )
                    )
                )
                return Ok(
                    HealthConnectStepAggregateSnapshotRead(
                        accepted=0,
                        deleted=0,
                        replayed=False,
                        skipped=skipped,
                        status="accepted",
                    )
                )

            current_rows = await transaction.fetch_all(
                select(HcStepAggregateBucketCurrent)
                .where(
                    HcStepAggregateBucketCurrent.bucket_start.gte(snapshot.start_time)
                )
                .where(HcStepAggregateBucketCurrent.bucket_start.lt(snapshot.end_time))
            )
            current_by_start = {row.bucket_start: row for row in current_rows}
            incoming_by_start = {
                bucket.start_time: bucket for bucket in snapshot.buckets
            }
            accepted = 0
            skipped = 0
            for bucket in sorted(snapshot.buckets, key=lambda item: item.start_time):
                bucket_hash = _hash_json(bucket.model_dump(mode="json"))
                current = current_by_start.get(bucket.start_time)
                if current is not None and current.payload_hash == bucket_hash:
                    skipped += 1
                    continue
                _ = await transaction.execute(
                    insert(
                        HcStepAggregateBucket(
                            bucket_end=bucket.end_time,
                            bucket_start=bucket.start_time,
                            count=bucket.count,
                            is_deleted=False,
                            payload_hash=bucket_hash,
                            received_at=time.time_ns() // 1_000_000,
                            request_id=snapshot.request_id,
                            zone_offset_seconds=bucket.zone_offset_seconds,
                        )
                    )
                )
                accepted += 1

            deleted = 0
            for bucket_start, current in current_by_start.items():
                if bucket_start in incoming_by_start:
                    continue
                _ = await transaction.execute(
                    insert(
                        HcStepAggregateBucket(
                            bucket_end=current.bucket_end,
                            bucket_start=bucket_start,
                            count=None,
                            is_deleted=True,
                            payload_hash=_hash_json(
                                {"deleted_bucket_start": bucket_start}
                            ),
                            received_at=time.time_ns() // 1_000_000,
                            request_id=snapshot.request_id,
                            zone_offset_seconds=current.zone_offset_seconds,
                        )
                    )
                )
                deleted += 1

            _ = await transaction.execute(
                insert(
                    HcStepAggregateSnapshot(
                        accepted_count=accepted,
                        deleted_count=deleted,
                        end_time=snapshot.end_time,
                        installation_id=snapshot.installation_id,
                        payload_hash=payload_hash,
                        received_at=time.time_ns() // 1_000_000,
                        request_id=snapshot.request_id,
                        skipped_count=skipped,
                        start_time=snapshot.start_time,
                    )
                )
            )
        return Ok(
            HealthConnectStepAggregateSnapshotRead(
                accepted=accepted,
                deleted=deleted,
                replayed=False,
                skipped=skipped,
                status="accepted",
            )
        )

    async def ingest_batch(
        self, batch: HealthConnectBatchRequest
    ) -> Result[HealthConnectBatchRead, HealthConnectBatchFailure]:
        """Append one complete page and advance its cursor atomically."""
        try:
            record_types = canonical_record_types(list(batch.record_types))
            validate_versioned_record_types(batch.contract_version, record_types)
        except (DuplicateRecordTypesError, UnsupportedRecordTypesError) as error:
            failure = (
                HealthConnectDuplicateRecordTypes()
                if isinstance(error, DuplicateRecordTypesError)
                else HealthConnectUnsupportedRecordTypes()
            )
            return Err(failure)
        key = _state_key(batch.installation_id, record_types)
        payload_hash = _hash_json(batch.model_dump(mode="json"))
        async with self.database.transaction(mode="immediate") as transaction:
            replay = await transaction.fetch_one_or_none(
                select(HcPageRequest).where(
                    HcPageRequest.request_id.eq(batch.request_id)
                )
            )
            if replay is not None:
                if replay.state_key != key or replay.payload_hash != payload_hash:
                    return Err(HealthConnectRequestIdentityConflict())
                return Ok(
                    HealthConnectBatchRead(
                        accepted=json.loads(replay.accepted_json),
                        deleted=json.loads(replay.deleted_json),
                        replayed=True,
                        skipped=json.loads(replay.skipped_json),
                        status="accepted",
                    )
                )
            state = await transaction.fetch_one_or_none(
                select(HealthConnectSyncState).where(
                    HealthConnectSyncState.state_key.eq(key)
                )
            )
            if state is None or state.current_token != batch.expected_token:
                return Err(HealthConnectCursorConflict())
            mode_conflicts = (
                batch.mode == "baseline"
                and (
                    state.status != "baseline"
                    or batch.next_token != batch.expected_token
                )
            ) or (batch.mode == "changes" and state.status != "changes")
            if mode_conflicts:
                return Err(HealthConnectCursorConflict())
            accepted, skipped, deleted = (
                _empty_counts(record_types),
                _empty_counts(record_types),
                _empty_counts(record_types),
            )
            received_at = time.time_ns() // 1_000_000
            await self.record_sink.append_records(
                transaction, batch, received_at, accepted, skipped
            )
            if batch.mode == "baseline":
                baseline_records = [
                    ("exercise", batch.records.exercise),
                    ("heart_rate", batch.records.heart_rate),
                    ("sleep", batch.records.sleep),
                    ("steps", batch.records.steps),
                    *(
                        (record_type, getattr(batch.records, record_type))
                        for record_type in GENERIC_RECORD_TYPES
                    ),
                ]
                for record_type, records in baseline_records:
                    for record in records:
                        _ = await transaction.execute(
                            insert(
                                HcBaselineSeen(
                                    seen_key=_hash_json(
                                        [
                                            batch.request_id,
                                            record_type,
                                            record.metadata.id,
                                        ]
                                    ),
                                    state_key=key,
                                    baseline_generation=state.baseline_generation,
                                    record_type=record_type,
                                    record_uid=record.metadata.id,
                                )
                            )
                        )
            await self.record_sink.append_deletions(
                transaction, batch, received_at, deleted, skipped
            )
            _ = await transaction.execute(
                update(HealthConnectSyncState)
                .set(
                    HealthConnectSyncState.current_token.to(batch.next_token),
                    HealthConnectSyncState.status.to(
                        "baseline" if batch.mode == "baseline" else "changes"
                    ),
                )
                .where(HealthConnectSyncState.state_key.eq(key))
            )
            _ = await transaction.execute(
                insert(
                    HcPageRequest(
                        request_id=batch.request_id,
                        state_key=key,
                        payload_hash=payload_hash,
                        accepted_json=json.dumps(accepted, sort_keys=True),
                        deleted_json=json.dumps(deleted, sort_keys=True),
                        skipped_json=json.dumps(skipped, sort_keys=True),
                    )
                )
            )
        return Ok(
            HealthConnectBatchRead(
                accepted=accepted,
                deleted=deleted,
                replayed=False,
                skipped=skipped,
                status="accepted",
            )
        )

    async def _reconcile_baseline(
        self,
        transaction: Transaction,
        body: CompleteHealthConnectBaselineRequest,
        key: str,
        record_types: tuple[HealthRecordType, ...],
    ) -> dict[HealthRecordType, int]:
        """Tombstone missing current records in bounded batches."""
        if body.contract_version == 1:
            for record_type, scan in body.ranges.items():
                for record_uid in scan.seen_record_ids or []:
                    _ = await transaction.execute(
                        insert(
                            HcBaselineSeen(
                                seen_key=_hash_json(
                                    [body.request_id, record_type, record_uid]
                                ),
                                state_key=key,
                                baseline_generation=body.baseline_generation,
                                record_type=record_type,
                                record_uid=record_uid,
                            )
                        )
                    )
        deleted, skipped = _empty_counts(record_types), _empty_counts(record_types)
        cursors: dict[HealthRecordType, int] = dict.fromkeys(record_types, 0)
        while True:
            rows_by_type = await self._fetch_missing_current_rows(
                transaction, body, key, record_types, cursors
            )
            if not any(rows_by_type.values()):
                break
            for record_type, rows in rows_by_type.items():
                if rows:
                    cursors[record_type] = rows[-1].version_id
            reconciliation_batch = HealthConnectBatchRequest(
                contract_version=1,
                deletions=[
                    HealthConnectDeletion(
                        record_type=record_type, record_id=row.record_uid
                    )
                    for record_type, rows in rows_by_type.items()
                    for row in rows
                ],
                expected_token=body.expected_token,
                installation_id=body.installation_id,
                mode="baseline",
                next_token=body.expected_token,
                records=HealthConnectRecords(
                    exercise=[], heart_rate=[], sleep=[], steps=[]
                ),
                record_types=list(record_types),
                request_id=body.request_id,
            )
            await self.record_sink.append_deletions(
                transaction,
                reconciliation_batch,
                time.time_ns() // 1_000_000,
                deleted,
                skipped,
            )
        _ = await transaction.execute(
            delete(HcBaselineSeen).where(HcBaselineSeen.state_key.eq(key))
        )
        return deleted

    async def _fetch_missing_current_rows(
        self,
        transaction: Transaction,
        body: CompleteHealthConnectBaselineRequest,
        key: str,
        record_types: tuple[HealthRecordType, ...],
        cursors: dict[HealthRecordType, int],
    ) -> dict[HealthRecordType, list[Any]]:
        rows: dict[HealthRecordType, list[Any]] = {}
        if "heart_rate" in record_types:
            scan = body.ranges["heart_rate"]
            rows["heart_rate"] = await transaction.fetch_all(
                select(HcHeartRateRecordCurrent)
                .where(HcHeartRateRecordCurrent.version_id.gt(cursors["heart_rate"]))
                .where(HcHeartRateRecordCurrent.start_time.gte(scan.start_time))
                .where(HcHeartRateRecordCurrent.end_time.lte(scan.end_time))
                .where(
                    not_exists(
                        select(HcBaselineSeen.seen_key)
                        .where(HcBaselineSeen.state_key.eq(key))
                        .where(
                            HcBaselineSeen.baseline_generation.eq(
                                body.baseline_generation
                            )
                        )
                        .where(HcBaselineSeen.record_type.eq("heart_rate"))
                        .where(
                            HcBaselineSeen.record_uid.eq_col(
                                HcHeartRateRecordCurrent.record_uid
                            )
                        )
                    )
                )
                .order_by(HcHeartRateRecordCurrent.version_id.asc())
                .limit(500)
            )
        if "sleep" in record_types:
            scan = body.ranges["sleep"]
            rows["sleep"] = await transaction.fetch_all(
                select(HcSleepSessionCurrent)
                .where(HcSleepSessionCurrent.version_id.gt(cursors["sleep"]))
                .where(HcSleepSessionCurrent.start_time.gte(scan.start_time))
                .where(HcSleepSessionCurrent.end_time.lte(scan.end_time))
                .where(
                    not_exists(
                        select(HcBaselineSeen.seen_key)
                        .where(HcBaselineSeen.state_key.eq(key))
                        .where(
                            HcBaselineSeen.baseline_generation.eq(
                                body.baseline_generation
                            )
                        )
                        .where(HcBaselineSeen.record_type.eq("sleep"))
                        .where(
                            HcBaselineSeen.record_uid.eq_col(
                                HcSleepSessionCurrent.record_uid
                            )
                        )
                    )
                )
                .order_by(HcSleepSessionCurrent.version_id.asc())
                .limit(500)
            )
        if "steps" in record_types:
            scan = body.ranges["steps"]
            rows["steps"] = await transaction.fetch_all(
                select(HcStepIntervalCurrent)
                .where(HcStepIntervalCurrent.version_id.gt(cursors["steps"]))
                .where(HcStepIntervalCurrent.start_time.gte(scan.start_time))
                .where(HcStepIntervalCurrent.end_time.lte(scan.end_time))
                .where(
                    not_exists(
                        select(HcBaselineSeen.seen_key)
                        .where(HcBaselineSeen.state_key.eq(key))
                        .where(
                            HcBaselineSeen.baseline_generation.eq(
                                body.baseline_generation
                            )
                        )
                        .where(HcBaselineSeen.record_type.eq("steps"))
                        .where(
                            HcBaselineSeen.record_uid.eq_col(
                                HcStepIntervalCurrent.record_uid
                            )
                        )
                    )
                )
                .order_by(HcStepIntervalCurrent.version_id.asc())
                .limit(500)
            )
        if "exercise" in record_types:
            scan = body.ranges["exercise"]
            rows["exercise"] = await transaction.fetch_all(
                select(HcExerciseSessionCurrent)
                .where(HcExerciseSessionCurrent.version_id.gt(cursors["exercise"]))
                .where(HcExerciseSessionCurrent.start_time.gte(scan.start_time))
                .where(HcExerciseSessionCurrent.end_time.lte(scan.end_time))
                .where(
                    not_exists(
                        select(HcBaselineSeen.seen_key)
                        .where(HcBaselineSeen.state_key.eq(key))
                        .where(
                            HcBaselineSeen.baseline_generation.eq(
                                body.baseline_generation
                            )
                        )
                        .where(HcBaselineSeen.record_type.eq("exercise"))
                        .where(
                            HcBaselineSeen.record_uid.eq_col(
                                HcExerciseSessionCurrent.record_uid
                            )
                        )
                    )
                )
                .order_by(HcExerciseSessionCurrent.version_id.asc())
                .limit(500)
            )
        return rows
