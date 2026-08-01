package com.tether.capture

import org.json.JSONArray
import org.json.JSONObject

object HealthConnectWireJson {
    private const val CONTRACT_VERSION = 1

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
                    .put("end_time", range.endTimeEpochMillis)
                    .put("seen_record_ids", JSONArray(range.seenRecordIds.sorted())),
            )
        }
        return json
    }

    private fun recordsByType(records: List<HealthConnectRecord>): JSONObject = JSONObject()
        .put(
            HealthConnectRecordType.HEART_RATE.wireName,
            JSONArray(records.filterIsInstance<HealthConnectRecord.HeartRate>().map { heartRateJson(it) }),
        )
        .put(
            HealthConnectRecordType.SLEEP.wireName,
            JSONArray(records.filterIsInstance<HealthConnectRecord.Sleep>().map { sleepJson(it) }),
        )
        .put(
            HealthConnectRecordType.STEPS.wireName,
            JSONArray(records.filterIsInstance<HealthConnectRecord.Steps>().map { stepsJson(it) }),
        )
        .put(
            HealthConnectRecordType.EXERCISE.wireName,
            JSONArray(records.filterIsInstance<HealthConnectRecord.Exercise>().map { exerciseJson(it) }),
        )

    private fun commonRecordJson(record: HealthConnectRecord): JSONObject = JSONObject()
        .put("metadata", metadataJson(record.metadata))
        .put("start_time", record.startTimeEpochMillis)
        .put("end_time", record.endTimeEpochMillis)
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
}
