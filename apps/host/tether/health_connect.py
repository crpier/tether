"""Atomic Health Connect cursor and baseline ingestion workflow."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Annotated, Any, Protocol, cast

from fastapi import APIRouter, Query
from snekql.sqlite import (
    Database,
    Fetched,
    Transaction,
    delete,
    insert,
    not_exists,
    select,
    update,
)
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from tether.health_connect_contracts import (
    GENERIC_RECORD_TYPES,
    CompleteHealthConnectBaselineRequest,
    GenericRecord,
    HealthConnectBaselineCompletionRead,
    HealthConnectBatchRead,
    HealthConnectBatchRequest,
    HealthConnectContractError,
    HealthConnectCursorConflictError,
    HealthConnectDeletion,
    HealthConnectRecords,
    HealthConnectSyncStateQuery,
    HealthConnectSyncStateRead,
    HealthRecordType,
    RecordMetadata,
    RecordStatus,
    RequestIdentityReuseError,
    StartHealthConnectBaselineRequest,
    canonical_record_types,
    parse_record_types,
    validate_versioned_record_types,
)
from tether.health_connect_persistence import (
    HcBaselineSeen,
    HcExerciseLap,
    HcExerciseRoutePoint,
    HcExerciseSegment,
    HcExerciseSession,
    HcExerciseSessionCurrent,
    HcGenericRecord,
    HcHeartRateRecord,
    HcHeartRateRecordCurrent,
    HcHeartRateSample,
    HcOrigin,
    HcPageRequest,
    HcSleepSession,
    HcSleepSessionCurrent,
    HcSleepStage,
    HcStepInterval,
    HcStepIntervalCurrent,
    HealthConnectSyncState,
)
from tether.structured_logging import Logger


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


async def _origin_id(transaction: Transaction, metadata: RecordMetadata) -> int:
    device = metadata.device
    origin_fields = {
        "data_origin_package": metadata.data_origin_package,
        "device_manufacturer": None if device is None else device.manufacturer,
        "device_model": None if device is None else device.model,
        "device_type": None if device is None else device.type,
    }
    origin_key = _hash_json(origin_fields)
    existing = await transaction.fetch_one_or_none(
        select(HcOrigin).where(HcOrigin.origin_key.eq(origin_key))
    )
    if existing is not None:
        return existing.origin_id
    created = await transaction.execute(
        insert(
            HcOrigin(
                data_origin_package=metadata.data_origin_package,
                device_manufacturer=None if device is None else device.manufacturer,
                device_model=None if device is None else device.model,
                device_type=None if device is None else device.type,
                origin_key=origin_key,
            )
        ).returning()
    )
    return created.origin_id


@dataclass(frozen=True, slots=True)
class HealthConnectIngestion:
    """Atomic Health Connect cursor, baseline, replay, and append gate.

    Example:
        ingestion = HealthConnectIngestion(database)
        state = await ingestion.fetch_sync_state("phone", ("steps",))
    """

    database: Database

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
            if stored is None:
                _ = await transaction.execute(
                    insert(
                        HealthConnectSyncState(
                            baseline_generation=1,
                            baseline_request_id=request_id,
                            completion_deleted_json=None,
                            completion_request_id=None,
                            current_token=starting_token,
                            installation_id=installation_id,
                            record_type_set=",".join(record_types),
                            state_key=key,
                            status="baseline",
                        )
                    )
                )
            else:
                _ = await transaction.execute(
                    update(HealthConnectSyncState)
                    .set(
                        HealthConnectSyncState.baseline_generation.to(
                            stored.baseline_generation + 1
                        ),
                        HealthConnectSyncState.baseline_request_id.to(request_id),
                        HealthConnectSyncState.completion_deleted_json.to(None),
                        HealthConnectSyncState.completion_request_id.to(None),
                        HealthConnectSyncState.current_token.to(starting_token),
                        HealthConnectSyncState.status.to("baseline"),
                    )
                    .where(HealthConnectSyncState.state_key.eq(key))
                )
            persisted = await transaction.fetch_one(
                select(HealthConnectSyncState).where(
                    HealthConnectSyncState.state_key.eq(key)
                )
            )
        return _state_read(persisted)

    async def complete_baseline(
        self, body: CompleteHealthConnectBaselineRequest
    ) -> HealthConnectBaselineCompletionRead:
        """Reconcile only bounded authoritative ranges and enter changes mode."""
        record_types = canonical_record_types(list(body.record_types))
        validate_versioned_record_types(body.contract_version, record_types)
        key = _state_key(body.installation_id, record_types)
        async with self.database.transaction(mode="immediate") as transaction:
            state = await transaction.fetch_one_or_none(
                select(HealthConnectSyncState).where(
                    HealthConnectSyncState.state_key.eq(key)
                )
            )
            if state is not None and state.completion_request_id == body.request_id:
                return HealthConnectBaselineCompletionRead(
                    deleted=json.loads(state.completion_deleted_json or "{}"),
                    status="completed",
                )
            if (
                state is None
                or state.current_token != body.expected_token
                or state.baseline_generation != body.baseline_generation
                or state.status != "baseline"
            ):
                raise HealthConnectCursorConflictError
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
        return HealthConnectBaselineCompletionRead(deleted=deleted, status="completed")

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
            await self._append_deletions(
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

    async def ingest_batch(
        self, batch: HealthConnectBatchRequest
    ) -> HealthConnectBatchRead:
        record_types = canonical_record_types(list(batch.record_types))
        validate_versioned_record_types(batch.contract_version, record_types)
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
                    raise RequestIdentityReuseError
                return HealthConnectBatchRead(
                    accepted=json.loads(replay.accepted_json),
                    deleted=json.loads(replay.deleted_json),
                    replayed=True,
                    skipped=json.loads(replay.skipped_json),
                    status="accepted",
                )
            state = await transaction.fetch_one_or_none(
                select(HealthConnectSyncState).where(
                    HealthConnectSyncState.state_key.eq(key)
                )
            )
            if state is None or state.current_token != batch.expected_token:
                raise HealthConnectCursorConflictError
            if batch.mode == "baseline" and (
                state.status != "baseline" or batch.next_token != batch.expected_token
            ):
                raise HealthConnectCursorConflictError
            if batch.mode == "changes" and state.status != "changes":
                raise HealthConnectCursorConflictError
            accepted, skipped, deleted = (
                _empty_counts(record_types),
                _empty_counts(record_types),
                _empty_counts(record_types),
            )
            received_at = time.time_ns() // 1_000_000
            await self._append_records(
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
            await self._append_deletions(
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
        return HealthConnectBatchRead(
            accepted=accepted,
            deleted=deleted,
            replayed=False,
            skipped=skipped,
            status="accepted",
        )

    async def _append_deletions(
        self,
        transaction: Transaction,
        batch: HealthConnectBatchRequest,
        received_at: int,
        deleted: dict[HealthRecordType, int],
        skipped: dict[HealthRecordType, int],
    ) -> None:
        """Append tombstones, resolving origin only from accepted history."""
        appenders = {
            "exercise": self._append_exercise_deletion,
            "heart_rate": self._append_heart_rate_deletion,
            "sleep": self._append_sleep_deletion,
            "steps": self._append_steps_deletion,
        }
        for deletion in batch.deletions:
            appender = appenders.get(
                deletion.record_type, self._append_generic_deletion
            )
            if await appender(transaction, batch, deletion, received_at, skipped):
                deleted[deletion.record_type] += 1

    async def _append_heart_rate_deletion(
        self,
        transaction: Transaction,
        batch: HealthConnectBatchRequest,
        deletion: HealthConnectDeletion,
        received_at: int,
        skipped: dict[HealthRecordType, int],
    ) -> bool:
        latest = await transaction.fetch_one_or_none(
            select(HcHeartRateRecord)
            .where(HcHeartRateRecord.record_uid.eq(deletion.record_id))
            .order_by(HcHeartRateRecord.version_id.desc())
            .limit(1)
        )
        if latest is not None and latest.is_deleted:
            skipped["heart_rate"] += 1
            return False
        _ = await transaction.execute(
            insert(
                HcHeartRateRecord(
                    client_record_id=None,
                    client_record_version=None,
                    end_time=None,
                    end_zone_offset_seconds=None,
                    is_deleted=True,
                    modified_at=None,
                    origin_id=None if latest is None else latest.origin_id,
                    payload_hash=_hash_json(deletion.model_dump(mode="json")),
                    received_at=received_at,
                    recording_method=None,
                    record_uid=deletion.record_id,
                    request_id=batch.request_id,
                    start_time=None,
                    start_zone_offset_seconds=None,
                )
            )
        )
        return True

    async def _append_sleep_deletion(
        self,
        transaction: Transaction,
        batch: HealthConnectBatchRequest,
        deletion: HealthConnectDeletion,
        received_at: int,
        skipped: dict[HealthRecordType, int],
    ) -> bool:
        latest = await transaction.fetch_one_or_none(
            select(HcSleepSession)
            .where(HcSleepSession.record_uid.eq(deletion.record_id))
            .order_by(HcSleepSession.version_id.desc())
            .limit(1)
        )
        if latest is not None and latest.is_deleted:
            skipped["sleep"] += 1
            return False
        _ = await transaction.execute(
            insert(
                HcSleepSession(
                    client_record_id=None,
                    client_record_version=None,
                    end_time=None,
                    end_zone_offset_seconds=None,
                    is_deleted=True,
                    modified_at=None,
                    notes=None,
                    origin_id=None if latest is None else latest.origin_id,
                    payload_hash=_hash_json(deletion.model_dump(mode="json")),
                    received_at=received_at,
                    recording_method=None,
                    record_uid=deletion.record_id,
                    request_id=batch.request_id,
                    start_time=None,
                    start_zone_offset_seconds=None,
                    title=None,
                )
            )
        )
        return True

    async def _append_steps_deletion(
        self,
        transaction: Transaction,
        batch: HealthConnectBatchRequest,
        deletion: HealthConnectDeletion,
        received_at: int,
        skipped: dict[HealthRecordType, int],
    ) -> bool:
        latest = await transaction.fetch_one_or_none(
            select(HcStepInterval)
            .where(HcStepInterval.record_uid.eq(deletion.record_id))
            .order_by(HcStepInterval.version_id.desc())
            .limit(1)
        )
        if latest is not None and latest.is_deleted:
            skipped["steps"] += 1
            return False
        _ = await transaction.execute(
            insert(
                HcStepInterval(
                    client_record_id=None,
                    client_record_version=None,
                    count=None,
                    end_time=None,
                    end_zone_offset_seconds=None,
                    is_deleted=True,
                    modified_at=None,
                    origin_id=None if latest is None else latest.origin_id,
                    payload_hash=_hash_json(deletion.model_dump(mode="json")),
                    received_at=received_at,
                    recording_method=None,
                    record_uid=deletion.record_id,
                    request_id=batch.request_id,
                    start_time=None,
                    start_zone_offset_seconds=None,
                )
            )
        )
        return True

    async def _append_exercise_deletion(
        self,
        transaction: Transaction,
        batch: HealthConnectBatchRequest,
        deletion: HealthConnectDeletion,
        received_at: int,
        skipped: dict[HealthRecordType, int],
    ) -> bool:
        latest = await transaction.fetch_one_or_none(
            select(HcExerciseSession)
            .where(HcExerciseSession.record_uid.eq(deletion.record_id))
            .order_by(HcExerciseSession.version_id.desc())
            .limit(1)
        )
        if latest is not None and latest.is_deleted:
            skipped["exercise"] += 1
            return False
        _ = await transaction.execute(
            insert(
                HcExerciseSession(
                    client_record_id=None,
                    client_record_version=None,
                    end_time=None,
                    end_zone_offset_seconds=None,
                    exercise_type=None,
                    is_deleted=True,
                    modified_at=None,
                    notes=None,
                    origin_id=None if latest is None else latest.origin_id,
                    payload_hash=_hash_json(deletion.model_dump(mode="json")),
                    planned_exercise_session_id=None,
                    received_at=received_at,
                    recording_method=None,
                    record_uid=deletion.record_id,
                    request_id=batch.request_id,
                    start_time=None,
                    start_zone_offset_seconds=None,
                    title=None,
                )
            )
        )
        return True

    async def _append_generic_deletion(
        self,
        transaction: Transaction,
        batch: HealthConnectBatchRequest,
        deletion: HealthConnectDeletion,
        received_at: int,
        skipped: dict[HealthRecordType, int],
    ) -> bool:
        latest = await transaction.fetch_one_or_none(
            select(HcGenericRecord)
            .where(HcGenericRecord.record_type.eq(deletion.record_type))
            .where(HcGenericRecord.record_uid.eq(deletion.record_id))
            .order_by(HcGenericRecord.version_id.desc())
            .limit(1)
        )
        if latest is not None and latest.is_deleted:
            skipped[deletion.record_type] += 1
            return False
        _ = await transaction.execute(
            insert(
                HcGenericRecord(
                    client_record_id=None,
                    client_record_version=None,
                    end_time=None,
                    end_zone_offset_seconds=None,
                    is_deleted=True,
                    modified_at=None,
                    origin_id=None if latest is None else latest.origin_id,
                    payload_hash=_hash_json(deletion.model_dump(mode="json")),
                    payload_json=None,
                    received_at=received_at,
                    recording_method=None,
                    record_type=deletion.record_type,
                    record_uid=deletion.record_id,
                    request_id=batch.request_id,
                    start_time=None,
                    start_zone_offset_seconds=None,
                )
            )
        )
        return True

    async def _append_records(
        self,
        transaction: Transaction,
        batch: HealthConnectBatchRequest,
        received_at: int,
        accepted: dict[HealthRecordType, int],
        skipped: dict[HealthRecordType, int],
    ) -> None:
        await self._append_heart_rates(
            transaction, batch, received_at, accepted, skipped
        )
        await self._append_sleep(transaction, batch, received_at, accepted, skipped)
        await self._append_steps(transaction, batch, received_at, accepted, skipped)
        await self._append_exercise(transaction, batch, received_at, accepted, skipped)
        await self._append_generic(transaction, batch, received_at, accepted, skipped)

    async def _append_heart_rates(
        self,
        transaction: Transaction,
        batch: HealthConnectBatchRequest,
        received_at: int,
        accepted: dict[HealthRecordType, int],
        skipped: dict[HealthRecordType, int],
    ) -> None:
        for record in batch.records.heart_rate:
            digest = _hash_json(record.model_dump(mode="json"))
            latest = await transaction.fetch_one_or_none(
                select(HcHeartRateRecord)
                .where(HcHeartRateRecord.record_uid.eq(record.metadata.id))
                .order_by(HcHeartRateRecord.version_id.desc())
                .limit(1)
            )
            if latest is not None and latest.payload_hash == digest:
                skipped["heart_rate"] += 1
                continue
            metadata = record.metadata
            parent = await transaction.execute(
                insert(
                    HcHeartRateRecord(
                        record_uid=metadata.id,
                        origin_id=await _origin_id(transaction, metadata),
                        modified_at=metadata.last_modified_time,
                        received_at=received_at,
                        request_id=batch.request_id,
                        is_deleted=False,
                        payload_hash=digest,
                        client_record_id=metadata.client_record_id,
                        client_record_version=metadata.client_record_version,
                        recording_method=metadata.recording_method,
                        start_time=record.start_time,
                        end_time=record.end_time,
                        start_zone_offset_seconds=record.start_zone_offset_seconds,
                        end_zone_offset_seconds=record.end_zone_offset_seconds,
                    )
                ).returning()
            )
            for index, sample in enumerate(record.samples):
                _ = await transaction.execute(
                    insert(
                        HcHeartRateSample(
                            version_id=parent.version_id,
                            sample_index=index,
                            time=sample.time,
                            beats_per_minute=sample.beats_per_minute,
                        )
                    )
                )
            accepted["heart_rate"] += 1

    async def _append_sleep(
        self,
        transaction: Transaction,
        batch: HealthConnectBatchRequest,
        received_at: int,
        accepted: dict[HealthRecordType, int],
        skipped: dict[HealthRecordType, int],
    ) -> None:
        for record in batch.records.sleep:
            digest = _hash_json(record.model_dump(mode="json"))
            latest = await transaction.fetch_one_or_none(
                select(HcSleepSession)
                .where(HcSleepSession.record_uid.eq(record.metadata.id))
                .order_by(HcSleepSession.version_id.desc())
                .limit(1)
            )
            if latest is not None and latest.payload_hash == digest:
                skipped["sleep"] += 1
                continue
            metadata = record.metadata
            parent = await transaction.execute(
                insert(
                    HcSleepSession(
                        record_uid=metadata.id,
                        origin_id=await _origin_id(transaction, metadata),
                        modified_at=metadata.last_modified_time,
                        received_at=received_at,
                        request_id=batch.request_id,
                        is_deleted=False,
                        payload_hash=digest,
                        client_record_id=metadata.client_record_id,
                        client_record_version=metadata.client_record_version,
                        recording_method=metadata.recording_method,
                        start_time=record.start_time,
                        end_time=record.end_time,
                        start_zone_offset_seconds=record.start_zone_offset_seconds,
                        end_zone_offset_seconds=record.end_zone_offset_seconds,
                        title=record.title,
                        notes=record.notes,
                    )
                ).returning()
            )
            for index, stage in enumerate(record.stages):
                _ = await transaction.execute(
                    insert(
                        HcSleepStage(
                            version_id=parent.version_id,
                            stage_index=index,
                            start_time=stage.start_time,
                            end_time=stage.end_time,
                            stage=stage.stage,
                        )
                    )
                )
            accepted["sleep"] += 1

    async def _append_steps(
        self,
        transaction: Transaction,
        batch: HealthConnectBatchRequest,
        received_at: int,
        accepted: dict[HealthRecordType, int],
        skipped: dict[HealthRecordType, int],
    ) -> None:
        for record in batch.records.steps:
            digest = _hash_json(record.model_dump(mode="json"))
            latest = await transaction.fetch_one_or_none(
                select(HcStepInterval)
                .where(HcStepInterval.record_uid.eq(record.metadata.id))
                .order_by(HcStepInterval.version_id.desc())
                .limit(1)
            )
            if latest is not None and latest.payload_hash == digest:
                skipped["steps"] += 1
                continue
            metadata = record.metadata
            _ = await transaction.execute(
                insert(
                    HcStepInterval(
                        record_uid=metadata.id,
                        origin_id=await _origin_id(transaction, metadata),
                        modified_at=metadata.last_modified_time,
                        received_at=received_at,
                        request_id=batch.request_id,
                        is_deleted=False,
                        payload_hash=digest,
                        client_record_id=metadata.client_record_id,
                        client_record_version=metadata.client_record_version,
                        recording_method=metadata.recording_method,
                        start_time=record.start_time,
                        end_time=record.end_time,
                        start_zone_offset_seconds=record.start_zone_offset_seconds,
                        end_zone_offset_seconds=record.end_zone_offset_seconds,
                        count=record.count,
                    )
                )
            )
            accepted["steps"] += 1

    async def _append_exercise(
        self,
        transaction: Transaction,
        batch: HealthConnectBatchRequest,
        received_at: int,
        accepted: dict[HealthRecordType, int],
        skipped: dict[HealthRecordType, int],
    ) -> None:
        for record in batch.records.exercise:
            digest = _hash_json(record.model_dump(mode="json"))
            latest = await transaction.fetch_one_or_none(
                select(HcExerciseSession)
                .where(HcExerciseSession.record_uid.eq(record.metadata.id))
                .order_by(HcExerciseSession.version_id.desc())
                .limit(1)
            )
            if latest is not None and latest.payload_hash == digest:
                skipped["exercise"] += 1
                continue
            metadata = record.metadata
            parent = await transaction.execute(
                insert(
                    HcExerciseSession(
                        record_uid=metadata.id,
                        origin_id=await _origin_id(transaction, metadata),
                        modified_at=metadata.last_modified_time,
                        received_at=received_at,
                        request_id=batch.request_id,
                        is_deleted=False,
                        payload_hash=digest,
                        client_record_id=metadata.client_record_id,
                        client_record_version=metadata.client_record_version,
                        recording_method=metadata.recording_method,
                        start_time=record.start_time,
                        end_time=record.end_time,
                        start_zone_offset_seconds=record.start_zone_offset_seconds,
                        end_zone_offset_seconds=record.end_zone_offset_seconds,
                        exercise_type=record.exercise_type,
                        title=record.title,
                        notes=record.notes,
                        planned_exercise_session_id=record.planned_exercise_session_id,
                    )
                ).returning()
            )
            for index, segment in enumerate(record.segments):
                _ = await transaction.execute(
                    insert(
                        HcExerciseSegment(
                            version_id=parent.version_id,
                            segment_index=index,
                            start_time=segment.start_time,
                            end_time=segment.end_time,
                            segment_type=segment.segment_type,
                            repetitions_count=segment.repetitions_count,
                        )
                    )
                )
            for index, lap in enumerate(record.laps):
                _ = await transaction.execute(
                    insert(
                        HcExerciseLap(
                            version_id=parent.version_id,
                            lap_index=index,
                            start_time=lap.start_time,
                            end_time=lap.end_time,
                            length_meters=lap.length_meters,
                        )
                    )
                )
            for index, point in enumerate(record.route):
                _ = await transaction.execute(
                    insert(
                        HcExerciseRoutePoint(
                            version_id=parent.version_id,
                            point_index=index,
                            time=point.time,
                            latitude=point.latitude,
                            longitude=point.longitude,
                            horizontal_accuracy_meters=point.horizontal_accuracy_meters,
                            vertical_accuracy_meters=point.vertical_accuracy_meters,
                            altitude_meters=point.altitude_meters,
                        )
                    )
                )
            accepted["exercise"] += 1

    async def _append_generic(
        self,
        transaction: Transaction,
        batch: HealthConnectBatchRequest,
        received_at: int,
        accepted: dict[HealthRecordType, int],
        skipped: dict[HealthRecordType, int],
    ) -> None:
        for record_type in GENERIC_RECORD_TYPES:
            records = cast("list[GenericRecord]", getattr(batch.records, record_type))
            for record in records:
                digest = _hash_json(record.model_dump(mode="json"))
                latest = await transaction.fetch_one_or_none(
                    select(HcGenericRecord)
                    .where(HcGenericRecord.record_type.eq(record_type))
                    .where(HcGenericRecord.record_uid.eq(record.metadata.id))
                    .order_by(HcGenericRecord.version_id.desc())
                    .limit(1)
                )
                if latest is not None and latest.payload_hash == digest:
                    skipped[record_type] += 1
                    continue
                metadata = record.metadata
                _ = await transaction.execute(
                    insert(
                        HcGenericRecord(
                            record_type=record_type,
                            record_uid=metadata.id,
                            origin_id=await _origin_id(transaction, metadata),
                            modified_at=metadata.last_modified_time,
                            received_at=received_at,
                            request_id=batch.request_id,
                            is_deleted=False,
                            payload_hash=digest,
                            payload_json=json.dumps(
                                record.payload,
                                sort_keys=True,
                                separators=(",", ":"),
                            ),
                            client_record_id=metadata.client_record_id,
                            client_record_version=metadata.client_record_version,
                            recording_method=metadata.recording_method,
                            start_time=record.start_time,
                            end_time=record.end_time,
                            start_zone_offset_seconds=record.start_zone_offset_seconds,
                            end_zone_offset_seconds=record.end_zone_offset_seconds,
                        )
                    )
                )
                accepted[record_type] += 1


router = APIRouter()


class _HealthConnectRuntime(Protocol):
    """Health Connect dependencies available while the host serves requests."""

    health_connect_ingestion: HealthConnectIngestion
    logger: Logger


def _runtime(request: Request) -> _HealthConnectRuntime:
    """Read Health Connect dependencies from the canonical host runtime."""
    return cast("_HealthConnectRuntime", request.app.state.runtime)


@router.get(
    "/api/telemetry/health-connect/sync-state",
    response_model=HealthConnectSyncStateRead,
)
async def read_health_connect_sync_state(
    request: Request, query: Annotated[HealthConnectSyncStateQuery, Query()]
) -> Response:
    try:
        record_types = parse_record_types(query.record_types)
    except HealthConnectContractError as error:
        return JSONResponse({"detail": str(error)}, status_code=422)
    service = _runtime(request).health_connect_ingestion
    return JSONResponse(
        (
            await service.fetch_sync_state(query.installation_id, record_types)
        ).model_dump(mode="json")
    )


@router.post(
    "/api/telemetry/health-connect/sync-state/baselines",
    response_model=HealthConnectSyncStateRead,
    status_code=201,
)
async def start_health_connect_baseline(
    request: Request, body: StartHealthConnectBaselineRequest
) -> Response:
    try:
        record_types = canonical_record_types(list(body.record_types))
        validate_versioned_record_types(body.contract_version, record_types)
    except HealthConnectContractError as error:
        return JSONResponse({"detail": str(error)}, status_code=422)
    service = _runtime(request).health_connect_ingestion
    state = await service.start_baseline(
        installation_id=body.installation_id,
        record_types=record_types,
        starting_token=body.starting_token,
        request_id=body.request_id,
    )
    _runtime(request).logger.info(
        "Health Connect baseline started",
        baseline_generation=state.baseline_generation,
        installation_id=body.installation_id,
        request_id=body.request_id,
    )
    return JSONResponse(state.model_dump(mode="json"), status_code=201)


@router.post(
    "/api/telemetry/health-connect/sync-state/baselines/complete",
    response_model=HealthConnectBaselineCompletionRead,
)
async def complete_health_connect_baseline(
    request: Request, body: CompleteHealthConnectBaselineRequest
) -> Response:
    """Reconcile bounded baseline absence and unlock live change pages."""
    service = _runtime(request).health_connect_ingestion
    try:
        report = await service.complete_baseline(body)
    except HealthConnectCursorConflictError:
        _runtime(request).logger.warning(
            "Health Connect baseline completion conflicted",
            error_category="cursor_conflict",
            installation_id=body.installation_id,
            request_id=body.request_id,
        )
        return JSONResponse({"detail": "baseline state is stale"}, status_code=409)
    except HealthConnectContractError as error:
        return JSONResponse({"detail": str(error)}, status_code=422)
    _runtime(request).logger.info(
        "Health Connect baseline completed",
        deleted=report.deleted,
        installation_id=body.installation_id,
        request_id=body.request_id,
    )
    return JSONResponse(report.model_dump(mode="json"))


@router.post(
    "/api/telemetry/health-connect/batches", response_model=HealthConnectBatchRead
)
async def ingest_health_connect_batch(
    request: Request, body: HealthConnectBatchRequest
) -> Response:
    service = _runtime(request).health_connect_ingestion
    try:
        report = await service.ingest_batch(body)
    except HealthConnectCursorConflictError:
        _runtime(request).logger.warning(
            "Health Connect page conflicted",
            error_category="cursor_conflict",
            installation_id=body.installation_id,
            request_id=body.request_id,
        )
        return JSONResponse({"detail": "expected token is stale"}, status_code=409)
    except HealthConnectContractError as error:
        return JSONResponse({"detail": str(error)}, status_code=409)
    _runtime(request).logger.info(
        "Health Connect page accepted",
        accepted=report.accepted,
        deleted=report.deleted,
        installation_id=body.installation_id,
        replayed=report.replayed,
        request_id=body.request_id,
        skipped=report.skipped,
    )
    return JSONResponse(report.model_dump(mode="json"))
