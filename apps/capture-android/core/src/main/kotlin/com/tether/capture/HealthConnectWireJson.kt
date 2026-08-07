package com.tether.capture

import org.json.JSONArray
import org.json.JSONObject

object HealthConnectWireJson {
    private const val CONTRACT_VERSION = 3

    fun startBaselineRequest(request: StartBaselineRequest): JSONObject = JSONObject()
        .put("contract_version", CONTRACT_VERSION)
        .put("installation_id", request.installationId)
        .put("record_types", JSONArray(request.recordTypes.map { it.wireName }))
        .put("request_id", request.requestId)
        .put("starting_token", request.startingToken)

    fun completeBaselineRequest(request: CompleteBaselineRequest): JSONObject = JSONObject()
        .put("contract_version", CONTRACT_VERSION)
        .put("installation_id", request.installationId)
        .put("record_types", JSONArray(request.recordTypes.map { it.wireName }))
        .put("request_id", request.requestId)
        .put("expected_token", request.expectedToken)
        .put("baseline_generation", request.generation)
        .put("ranges", rangesJson(request.scannedBounds))

    fun batchRequest(request: HealthConnectBatchRequest): JSONObject = JSONObject()
        .put("contract_version", CONTRACT_VERSION)
        .put("installation_id", request.installationId)
        .put("record_types", JSONArray(request.recordTypes.map { it.wireName }))
        .put("request_id", request.requestId)
        .put("mode", request.mode.wireName())
        .put("expected_token", request.expectedToken)
        .put("next_token", request.nextToken)
        .put("records", recordsByType(request.records))
        .put("deletions", JSONArray(request.deletions.map { deletionJson(it) }))

    private fun rangesJson(bounds: Map<HealthConnectRecordType, HealthConnectScanBounds>): JSONObject {
        val json = JSONObject()
        bounds.toSortedMap(compareBy { it.wireName }).forEach { (type, range) ->
            json.put(
                type.wireName,
                JSONObject()
                    .put("start_time", range.startTimeEpochMillis)
                    .put("end_time", range.endTimeEpochMillis),
            )
        }
        return json
    }

    private fun recordsByType(records: List<HealthConnectRecord>): JSONObject {
        val json = JSONObject()
        records.groupBy { it.recordType }.toSortedMap(compareBy { it.wireName }).forEach { (type, typeRecords) ->
            json.put(
                type.wireName,
                JSONArray(
                    typeRecords.map { record ->
                        when (record) {
                            is HealthConnectRecord.HeartRate -> heartRateJson(record)
                            is HealthConnectRecord.Sleep -> sleepJson(record)
                            is HealthConnectRecord.Steps -> stepsJson(record)
                            is HealthConnectRecord.Exercise -> exerciseJson(record)
                            is HealthConnectRecord.Generic -> genericJson(record)
                        }
                    },
                ),
            )
        }
        return json
    }

    private fun commonRecordJson(record: HealthConnectRecord): JSONObject = JSONObject()
        .put("metadata", metadataJson(record.metadata))
        .put("start_time", nullable(record.startTimeEpochMillis))
        .put("end_time", nullable(record.endTimeEpochMillis))
        .put("start_zone_offset_seconds", nullable(record.startZoneOffsetSeconds))
        .put("end_zone_offset_seconds", nullable(record.endZoneOffsetSeconds))

    private fun heartRateJson(record: HealthConnectRecord.HeartRate): JSONObject = commonRecordJson(record)
        .put(
            "samples",
            JSONArray(
                record.samples.map { sample ->
                    JSONObject()
                        .put("time", sample.timeEpochMillis)
                        .put("beats_per_minute", sample.beatsPerMinute)
                },
            ),
        )

    private fun sleepJson(record: HealthConnectRecord.Sleep): JSONObject = commonRecordJson(record)
        .put("title", nullable(record.title))
        .put("notes", nullable(record.notes))
        .put(
            "stages",
            JSONArray(
                record.stages.map { stage ->
                    JSONObject()
                        .put("start_time", stage.startTimeEpochMillis)
                        .put("end_time", stage.endTimeEpochMillis)
                        .put("stage", stage.stage)
                },
            ),
        )

    private fun stepsJson(record: HealthConnectRecord.Steps): JSONObject = commonRecordJson(record)
        .put("count", record.count)

    private fun genericJson(record: HealthConnectRecord.Generic): JSONObject = commonRecordJson(record)
        .put("payload", JSONObject(record.payload.mapValues { (_, value) -> jsonValue(value) }))

    private fun exerciseJson(record: HealthConnectRecord.Exercise): JSONObject = commonRecordJson(record)
        .put("exercise_type", record.exerciseType)
        .put("title", nullable(record.title))
        .put("notes", nullable(record.notes))
        .put("planned_exercise_session_id", nullable(record.plannedExerciseSessionId))
        .put(
            "segments",
            JSONArray(
                record.segments.map { segment ->
                    JSONObject()
                        .put("start_time", segment.startTimeEpochMillis)
                        .put("end_time", segment.endTimeEpochMillis)
                        .put("segment_type", segment.segmentType)
                        .put("repetitions_count", nullable(segment.repetitionsCount))
                },
            ),
        )
        .put(
            "laps",
            JSONArray(
                record.laps.map { lap ->
                    JSONObject()
                        .put("start_time", lap.startTimeEpochMillis)
                        .put("end_time", lap.endTimeEpochMillis)
                        .put("length_meters", nullable(lap.lengthMeters))
                },
            ),
        )
        .put(
            "route",
            JSONArray(
                record.route.map { point ->
                    JSONObject()
                        .put("time", point.timeEpochMillis)
                        .put("latitude", point.latitude)
                        .put("longitude", point.longitude)
                        .put("horizontal_accuracy_meters", nullable(point.horizontalAccuracyMeters))
                        .put("vertical_accuracy_meters", nullable(point.verticalAccuracyMeters))
                        .put("altitude_meters", nullable(point.altitudeMeters))
                },
            ),
        )

    private fun metadataJson(metadata: HealthConnectMetadata): JSONObject = JSONObject()
        .put("id", metadata.id)
        .put("data_origin_package", metadata.dataOriginPackage)
        .put("last_modified_time", nullable(metadata.lastModifiedTimeEpochMillis))
        .put("client_record_id", nullable(metadata.clientRecordId))
        .put("client_record_version", nullable(metadata.clientRecordVersion))
        .put("device", metadata.device?.let { deviceJson(it) } ?: JSONObject.NULL)
        .put("recording_method", nullable(metadata.recordingMethod))

    private fun deviceJson(device: HealthConnectDevice): JSONObject = JSONObject()
        .put("manufacturer", nullable(device.manufacturer))
        .put("model", nullable(device.model))
        .put("type", nullable(device.type))

    private fun deletionJson(deletion: HealthConnectDeletion): JSONObject = JSONObject()
        .put("record_type", deletion.recordType.wireName)
        .put("record_id", deletion.recordId)

    private fun HealthConnectBatchMode.wireName(): String = when (this) {
        HealthConnectBatchMode.Baseline -> "baseline"
        HealthConnectBatchMode.Changes -> "changes"
    }

    private fun nullable(value: Any?): Any = value ?: JSONObject.NULL

    private fun jsonValue(value: Any?): Any = when (value) {
        null -> JSONObject.NULL
        is Map<*, *> -> JSONObject(
            value.entries.associate { (key, child) -> key.toString() to jsonValue(child) },
        )
        is Iterable<*> -> JSONArray(value.map { jsonValue(it) })
        is Array<*> -> JSONArray(value.map { jsonValue(it) })
        else -> value
    }
}
