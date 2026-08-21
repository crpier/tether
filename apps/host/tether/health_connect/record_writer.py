"""Append-only Health Connect record and tombstone writer."""

from __future__ import annotations

import hashlib
import json
from typing import cast

from snekql.sqlite import Transaction, insert, select

from tether.health_connect.contracts import (
    GENERIC_RECORD_TYPES,
    GenericRecord,
    HealthConnectBatchRequest,
    HealthConnectDeletion,
    HealthRecordType,
    RecordMetadata,
)
from tether.health_connect.persistence import (
    HcExerciseLap,
    HcExerciseRoutePoint,
    HcExerciseSegment,
    HcExerciseSession,
    HcGenericRecord,
    HcHeartRateRecord,
    HcHeartRateSample,
    HcOrigin,
    HcSleepSession,
    HcSleepStage,
    HcStepInterval,
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


class HealthConnectRecordWriter:
    """Persist append-only Health Connect records without owning cursor state."""

    async def append_records(
        self,
        transaction: Transaction,
        batch: HealthConnectBatchRequest,
        received_at: int,
        accepted: dict[HealthRecordType, int],
        skipped: dict[HealthRecordType, int],
    ) -> None:
        """Append changed parent and child records while preserving source order."""
        await self._append_heart_rates(
            transaction, batch, received_at, accepted, skipped
        )
        await self._append_sleep(transaction, batch, received_at, accepted, skipped)
        await self._append_steps(transaction, batch, received_at, accepted, skipped)
        await self._append_exercise(transaction, batch, received_at, accepted, skipped)
        await self._append_generic(transaction, batch, received_at, accepted, skipped)

    async def append_deletions(
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
