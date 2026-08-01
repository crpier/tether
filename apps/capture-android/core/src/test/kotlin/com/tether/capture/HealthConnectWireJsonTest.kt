package com.tether.capture

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Test
import java.io.File

class HealthConnectWireJsonTest {
    @Test
    fun representativeBatchMatchesHostFixture() {
        val request = HealthConnectBatchRequest(
            installationId = "pixel-installation",
            recordTypes = linkedSetOf(
                HealthConnectRecordType.HEART_RATE,
                HealthConnectRecordType.SLEEP,
                HealthConnectRecordType.STEPS,
                HealthConnectRecordType.EXERCISE,
            ),
            requestId = "page-request-1",
            mode = HealthConnectBatchMode.Baseline,
            expectedToken = "opaque-starting-token",
            nextToken = "opaque-starting-token",
            records = listOf(
                HealthConnectRecord.HeartRate(
                    metadata = HealthConnectMetadata(
                        id = "heart-1",
                        dataOriginPackage = "com.example.watch",
                        lastModifiedTimeEpochMillis = 1700000000100,
                        device = HealthConnectDevice("Google", "Pixel Watch 3", 2),
                        recordingMethod = 1,
                    ),
                    startTimeEpochMillis = 1700000000000,
                    endTimeEpochMillis = 1700000060000,
                    startZoneOffsetSeconds = -18000,
                    endZoneOffsetSeconds = -18000,
                    samples = listOf(
                        HeartRateSample(1700000001000, 61),
                        HeartRateSample(1700000002000, 63),
                    ),
                ),
                HealthConnectRecord.Sleep(
                    metadata = HealthConnectMetadata(
                        id = "sleep-1",
                        dataOriginPackage = "com.example.phone",
                        lastModifiedTimeEpochMillis = 1700000000200,
                        clientRecordId = "client-sleep",
                        clientRecordVersion = 3,
                        recordingMethod = 2,
                    ),
                    startTimeEpochMillis = 1699980000000,
                    endTimeEpochMillis = 1700008800000,
                    title = "Night sleep",
                    notes = "Representative fixture note",
                    stages = listOf(SleepStage(1699980000000, 1699983600000, 4)),
                ),
                HealthConnectRecord.Steps(
                    metadata = HealthConnectMetadata(
                        id = "steps-1",
                        dataOriginPackage = "com.example.phone",
                    ),
                    startTimeEpochMillis = 1700000000000,
                    endTimeEpochMillis = 1700003600000,
                    startZoneOffsetSeconds = 3600,
                    endZoneOffsetSeconds = 3600,
                    count = 1234,
                ),
                HealthConnectRecord.Exercise(
                    metadata = HealthConnectMetadata(
                        id = "exercise-1",
                        dataOriginPackage = "com.example.watch",
                        lastModifiedTimeEpochMillis = 1700000000300,
                        clientRecordId = "run-1",
                        clientRecordVersion = 1,
                        device = HealthConnectDevice("Google", "Pixel Watch 3", 2),
                        recordingMethod = 1,
                    ),
                    startTimeEpochMillis = 1700000000000,
                    endTimeEpochMillis = 1700003600000,
                    startZoneOffsetSeconds = -18000,
                    endZoneOffsetSeconds = -18000,
                    exerciseType = 56,
                    title = "Morning run",
                    plannedExerciseSessionId = null,
                    segments = listOf(ExerciseSegment(1700000000000, 1700000600000, 57, 2)),
                    laps = listOf(ExerciseLap(1700000000000, 1700000600000, 1000.5)),
                    route = listOf(
                        ExerciseRoutePoint(
                            timeEpochMillis = 1700000001000,
                            latitude = 40.1,
                            longitude = -73.2,
                            horizontalAccuracyMeters = 3.2,
                            altitudeMeters = 12.5,
                        ),
                    ),
                ),
            ),
            deletions = emptyList(),
        )

        assertEquals(
            JSONObject(fixtureText()).toString(),
            HealthConnectWireJson.batchRequest(request).toString(),
        )
    }

    private fun fixtureText(): String {
        val candidates = listOf(
            File("../../host/tests/fixtures/health_connect/v1/representative-batch.json"),
            File("../host/tests/fixtures/health_connect/v1/representative-batch.json"),
            File("apps/host/tests/fixtures/health_connect/v1/representative-batch.json"),
        )
        return candidates.first { it.exists() }.readText()
    }
}
