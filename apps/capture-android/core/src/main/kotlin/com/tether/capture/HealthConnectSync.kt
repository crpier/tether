package com.tether.capture

enum class HealthConnectRecordType(val wireName: String) {
    ACTIVE_CALORIES_BURNED("active_calories_burned"),
    BASAL_BODY_TEMPERATURE("basal_body_temperature"),
    BASAL_METABOLIC_RATE("basal_metabolic_rate"),
    BLOOD_GLUCOSE("blood_glucose"),
    BLOOD_PRESSURE("blood_pressure"),
    BODY_FAT("body_fat"),
    BODY_TEMPERATURE("body_temperature"),
    BODY_WATER_MASS("body_water_mass"),
    BONE_MASS("bone_mass"),
    CERVICAL_MUCUS("cervical_mucus"),
    CYCLING_PEDALING_CADENCE("cycling_pedaling_cadence"),
    DISTANCE("distance"),
    ELEVATION_GAINED("elevation_gained"),
    EXERCISE("exercise"),
    FLOORS_CLIMBED("floors_climbed"),
    HEART_RATE("heart_rate"),
    HEART_RATE_VARIABILITY_RMSSD("heart_rate_variability_rmssd"),
    HEIGHT("height"),
    HYDRATION("hydration"),
    INTERMENSTRUAL_BLEEDING("intermenstrual_bleeding"),
    LEAN_BODY_MASS("lean_body_mass"),
    MENSTRUATION_FLOW("menstruation_flow"),
    MENSTRUATION_PERIOD("menstruation_period"),
    MINDFULNESS_SESSION("mindfulness_session"),
    NUTRITION("nutrition"),
    OVULATION_TEST("ovulation_test"),
    OXYGEN_SATURATION("oxygen_saturation"),
    PLANNED_EXERCISE_SESSION("planned_exercise_session"),
    POWER("power"),
    RESPIRATORY_RATE("respiratory_rate"),
    RESTING_HEART_RATE("resting_heart_rate"),
    SEXUAL_ACTIVITY("sexual_activity"),
    SKIN_TEMPERATURE("skin_temperature"),
    SLEEP("sleep"),
    SPEED("speed"),
    STEPS("steps"),
    STEPS_CADENCE("steps_cadence"),
    TOTAL_CALORIES_BURNED("total_calories_burned"),
    VO2_MAX("vo2_max"),
    WEIGHT("weight"),
    WHEELCHAIR_PUSHES("wheelchair_pushes"),
}

data class HealthConnectMetadata(
    val id: String,
    val dataOriginPackage: String,
    val lastModifiedTimeEpochMillis: Long? = null,
    val clientRecordId: String? = null,
    val clientRecordVersion: Long? = null,
    val device: HealthConnectDevice? = null,
    val recordingMethod: Int? = null,
)

data class HealthConnectDevice(
    val manufacturer: String?,
    val model: String?,
    val type: Int?,
)

sealed class HealthConnectRecord {
    abstract val metadata: HealthConnectMetadata
    abstract val startTimeEpochMillis: Long?
    abstract val endTimeEpochMillis: Long?
    abstract val startZoneOffsetSeconds: Int?
    abstract val endZoneOffsetSeconds: Int?
    abstract val recordType: HealthConnectRecordType

    data class HeartRate(
        override val metadata: HealthConnectMetadata,
        override val startTimeEpochMillis: Long,
        override val endTimeEpochMillis: Long,
        override val startZoneOffsetSeconds: Int? = null,
        override val endZoneOffsetSeconds: Int? = null,
        val samples: List<HeartRateSample>,
    ) : HealthConnectRecord() {
        override val recordType = HealthConnectRecordType.HEART_RATE
    }

    data class Sleep(
        override val metadata: HealthConnectMetadata,
        override val startTimeEpochMillis: Long,
        override val endTimeEpochMillis: Long,
        override val startZoneOffsetSeconds: Int? = null,
        override val endZoneOffsetSeconds: Int? = null,
        val title: String? = null,
        val notes: String? = null,
        val stages: List<SleepStage> = emptyList(),
    ) : HealthConnectRecord() {
        override val recordType = HealthConnectRecordType.SLEEP
    }

    data class Steps(
        override val metadata: HealthConnectMetadata,
        override val startTimeEpochMillis: Long,
        override val endTimeEpochMillis: Long,
        override val startZoneOffsetSeconds: Int? = null,
        override val endZoneOffsetSeconds: Int? = null,
        val count: Long,
    ) : HealthConnectRecord() {
        override val recordType = HealthConnectRecordType.STEPS
    }

    data class Exercise(
        override val metadata: HealthConnectMetadata,
        override val startTimeEpochMillis: Long,
        override val endTimeEpochMillis: Long,
        override val startZoneOffsetSeconds: Int? = null,
        override val endZoneOffsetSeconds: Int? = null,
        val exerciseType: Int,
        val title: String? = null,
        val notes: String? = null,
        val plannedExerciseSessionId: String? = null,
        val segments: List<ExerciseSegment> = emptyList(),
        val laps: List<ExerciseLap> = emptyList(),
        val route: List<ExerciseRoutePoint> = emptyList(),
    ) : HealthConnectRecord() {
        override val recordType = HealthConnectRecordType.EXERCISE
    }

    data class Generic(
        override val recordType: HealthConnectRecordType,
        override val metadata: HealthConnectMetadata,
        override val startTimeEpochMillis: Long? = null,
        override val endTimeEpochMillis: Long? = null,
        override val startZoneOffsetSeconds: Int? = null,
        override val endZoneOffsetSeconds: Int? = null,
        val payload: Map<String, Any?> = emptyMap(),
    ) : HealthConnectRecord()
}

data class HeartRateSample(val timeEpochMillis: Long, val beatsPerMinute: Long)

data class SleepStage(
    val startTimeEpochMillis: Long,
    val endTimeEpochMillis: Long,
    val stage: Int,
)

data class ExerciseSegment(
    val startTimeEpochMillis: Long,
    val endTimeEpochMillis: Long,
    val segmentType: Int,
    val repetitionsCount: Long? = null,
)

data class ExerciseLap(
    val startTimeEpochMillis: Long,
    val endTimeEpochMillis: Long,
    val lengthMeters: Double? = null,
)

data class ExerciseRoutePoint(
    val timeEpochMillis: Long,
    val latitude: Double,
    val longitude: Double,
    val horizontalAccuracyMeters: Double? = null,
    val verticalAccuracyMeters: Double? = null,
    val altitudeMeters: Double? = null,
)

data class HealthConnectDeletion(
    val recordType: HealthConnectRecordType,
    val recordId: String,
)

data class HealthConnectScanBounds(
    val startTimeEpochMillis: Long,
    val endTimeEpochMillis: Long,
)


data class HealthConnectChanges(
    val records: List<HealthConnectRecord>,
    val deletions: List<HealthConnectDeletion>,
    val nextToken: String,
    val hasMore: Boolean = false,
)

class HealthConnectChangesTokenExpiredException : RuntimeException("Health Connect changes token expired")

interface HealthConnectSource {
    suspend fun getChangesToken(recordTypes: Set<HealthConnectRecordType>): String

    suspend fun scanBaseline(
        recordTypes: Set<HealthConnectRecordType>,
        consumePage: suspend (List<HealthConnectRecord>) -> Unit,
    ): Map<HealthConnectRecordType, HealthConnectScanBounds>

    suspend fun readChanges(token: String): HealthConnectChanges
}

enum class HostSyncState { Initial, Baseline, Changes }

data class HostSyncCursor(
    val state: HostSyncState,
    val generation: Int,
    val token: String?,
)

data class StartBaselineRequest(
    val installationId: String,
    val recordTypes: Set<HealthConnectRecordType>,
    val requestId: String,
    val startingToken: String,
)

enum class HealthConnectBatchMode { Baseline, Changes }

data class HealthConnectBatchRequest(
    val installationId: String,
    val recordTypes: Set<HealthConnectRecordType>,
    val requestId: String,
    val mode: HealthConnectBatchMode,
    val expectedToken: String,
    val nextToken: String,
    val records: List<HealthConnectRecord>,
    val deletions: List<HealthConnectDeletion>,
)

data class CompleteBaselineRequest(
    val installationId: String,
    val recordTypes: Set<HealthConnectRecordType>,
    val requestId: String,
    val generation: Int,
    val expectedToken: String,
    val scannedBounds: Map<HealthConnectRecordType, HealthConnectScanBounds>,
)

sealed class BatchUploadResult {
    data object Accepted : BatchUploadResult()
    data object StaleToken : BatchUploadResult()
    data class Conflict(val reason: String) : BatchUploadResult()
}

interface HealthConnectHost {
    suspend fun getSyncState(
        installationId: String,
        recordTypes: Set<HealthConnectRecordType>,
    ): HostSyncCursor

    suspend fun startBaseline(request: StartBaselineRequest): HostSyncCursor

    suspend fun uploadBatch(request: HealthConnectBatchRequest): BatchUploadResult

    suspend fun completeBaseline(request: CompleteBaselineRequest): HostSyncCursor
}

interface RequestIds {
    fun next(): String

    fun stable(key: String): String = next()
}

class SequentialRequestIds(private val prefix: String) : RequestIds {
    private var index = 0

    override fun next(): String {
        index += 1
        return "$prefix-$index"
    }
}

sealed class HealthConnectSyncResult {
    data object Success : HealthConnectSyncResult()
    data class Failed(val reason: String) : HealthConnectSyncResult()
}

private class BaselineUploadRejectedException : RuntimeException()

class HealthConnectSyncCoordinator(
    private val installationId: String,
    private val recordTypes: Set<HealthConnectRecordType>,
    private val health: HealthConnectSource,
    private val host: HealthConnectHost,
    private val requestIds: RequestIds,
) {
    suspend fun syncOnce(): HealthConnectSyncResult {
        val cursor = host.getSyncState(installationId, recordTypes)
        return try {
            when (cursor.state) {
                HostSyncState.Initial -> runBaseline()
                HostSyncState.Baseline -> resumeBaseline(cursor)
                HostSyncState.Changes -> runChanges(cursor)
            }
        } catch (_: HealthConnectChangesTokenExpiredException) {
            runBaseline()
        }
    }

    private suspend fun runBaseline(): HealthConnectSyncResult {
        val startingToken = health.getChangesToken(recordTypes)
        val baselineCursor = host.startBaseline(
            StartBaselineRequest(
                installationId = installationId,
                recordTypes = recordTypes,
                requestId = stableRequestId("baseline-start:$installationId:$startingToken"),
                startingToken = startingToken,
            ),
        )
        return runBaselineWithCursor(baselineCursor, startingToken)
    }

    private suspend fun runBaselineWithCursor(
        baselineCursor: HostSyncCursor,
        startingToken: String,
    ): HealthConnectSyncResult {
        val scannedBounds = try {
            health.scanBaseline(recordTypes) { page ->
                for (records in page.chunked(MAX_PARENT_RECORDS_PER_BATCH)) {
                    val baselineResult = host.uploadBatch(
                        HealthConnectBatchRequest(
                            installationId = installationId,
                            recordTypes = recordTypes,
                            requestId = stableRequestId(
                                "baseline-page:$installationId:$startingToken:${records.recordIdentityKey()}",
                            ),
                            mode = HealthConnectBatchMode.Baseline,
                            expectedToken = startingToken,
                            nextToken = startingToken,
                            records = records,
                            deletions = emptyList(),
                        ),
                    )
                    if (baselineResult != BatchUploadResult.Accepted) {
                        throw BaselineUploadRejectedException()
                    }
                }
            }
        } catch (_: BaselineUploadRejectedException) {
            return HealthConnectSyncResult.Failed("baseline cursor changed")
        }
        host.completeBaseline(
            CompleteBaselineRequest(
                installationId = installationId,
                recordTypes = recordTypes,
                requestId = stableRequestId(
                    "baseline-complete:$installationId:${baselineCursor.generation}:$startingToken:${scannedBounds.identityKey()}",
                ),
                generation = baselineCursor.generation,
                expectedToken = startingToken,
                scannedBounds = scannedBounds,
            ),
        )
        val changes = health.readChanges(startingToken)
        return uploadChanges(expectedToken = startingToken, changes = changes)
    }

    private suspend fun resumeBaseline(cursor: HostSyncCursor): HealthConnectSyncResult {
        val token = cursor.token ?: return HealthConnectSyncResult.Failed("missing token")
        return runBaselineWithCursor(cursor, token)
    }

    private suspend fun runChanges(cursor: HostSyncCursor): HealthConnectSyncResult {
        val token = cursor.token ?: return HealthConnectSyncResult.Failed("missing token")
        return uploadChanges(expectedToken = token, changes = health.readChanges(token))
    }

    private suspend fun uploadChanges(
        expectedToken: String,
        changes: HealthConnectChanges,
        allowStaleRefresh: Boolean = true,
    ): HealthConnectSyncResult {
        val result = host.uploadBatch(
            HealthConnectBatchRequest(
                installationId = installationId,
                recordTypes = recordTypes,
                requestId = stableRequestId(
                    "changes:$installationId:$expectedToken:${changes.nextToken}:${changes.records.recordIdentityKey()}:${changes.deletions.deletionIdentityKey()}",
                ),
                mode = HealthConnectBatchMode.Changes,
                expectedToken = expectedToken,
                nextToken = changes.nextToken,
                records = changes.records,
                deletions = changes.deletions,
            ),
        )
        return when (result) {
            BatchUploadResult.Accepted -> {
                if (changes.hasMore) {
                    uploadChanges(
                        expectedToken = changes.nextToken,
                        changes = health.readChanges(changes.nextToken),
                    )
                } else {
                    HealthConnectSyncResult.Success
                }
            }
            BatchUploadResult.StaleToken -> {
                if (!allowStaleRefresh) {
                    HealthConnectSyncResult.Failed("host cursor kept changing")
                } else {
                    val cursor = host.getSyncState(installationId, recordTypes)
                    val token = cursor.token ?: return HealthConnectSyncResult.Failed("missing token")
                    val refreshedChanges = health.readChanges(token)
                    uploadChanges(
                        expectedToken = token,
                        changes = refreshedChanges,
                        allowStaleRefresh = false,
                    )
                }
            }
            is BatchUploadResult.Conflict -> HealthConnectSyncResult.Failed(result.reason)
        }
    }

    private fun stableRequestId(key: String): String =
        requestIds.stable("health-connect-v3:$key")

    private fun List<HealthConnectRecord>.recordIdentityKey(): String = joinToString(",") { record ->
        "${record.recordType.wireName}:${record.metadata.id}:${record.metadata.lastModifiedTimeEpochMillis}"
    }

    private fun List<HealthConnectDeletion>.deletionIdentityKey(): String = joinToString(",") { deletion ->
        "${deletion.recordType.wireName}:${deletion.recordId}"
    }

    private fun Map<HealthConnectRecordType, HealthConnectScanBounds>.identityKey(): String =
        entries.sortedBy { it.key.wireName }.joinToString(";") { (type, bounds) ->
            "${type.wireName}:${bounds.startTimeEpochMillis}:${bounds.endTimeEpochMillis}"
        }

    companion object {
        const val MAX_PARENT_RECORDS_PER_BATCH = 1_000
    }
}
