package com.tether.capture

import androidx.health.connect.client.records.ExerciseLap as AndroidExerciseLap
import androidx.health.connect.client.records.ExerciseRoute
import androidx.health.connect.client.records.ExerciseSegment as AndroidExerciseSegment
import androidx.health.connect.client.records.ExerciseSessionRecord
import androidx.health.connect.client.records.HeartRateRecord
import androidx.health.connect.client.records.SleepSessionRecord
import androidx.health.connect.client.records.StepsRecord
import androidx.health.connect.client.records.metadata.DataOrigin
import androidx.health.connect.client.records.metadata.Device
import androidx.health.connect.client.records.metadata.Metadata
import androidx.health.connect.client.units.Length
import org.junit.Assert.assertEquals
import org.junit.Test
import java.time.Instant
import java.time.ZoneOffset

class HealthConnectRecordMapperTest {
    @Test
    fun mapsAllSupportedRecordShapesWithoutDroppingNestedFields() {
        val metadata = metadata(
            recordingMethod = Metadata.RECORDING_METHOD_ACTIVELY_RECORDED,
            id = "upstream-id",
            dataOrigin = DataOrigin("com.example.watch"),
            lastModifiedTime = Instant.ofEpochMilli(1700000000100),
            clientRecordId = "client-id",
            clientRecordVersion = 7,
            device = Device(Device.TYPE_WATCH, "Google", "Pixel Watch 3"),
        )

        val records = listOf(
            HealthConnectRecordMapper.map(
                HeartRateRecord(
                    Instant.ofEpochMilli(1700000000000),
                    ZoneOffset.ofHours(-5),
                    Instant.ofEpochMilli(1700000060000),
                    ZoneOffset.ofHours(-5),
                    listOf(HeartRateRecord.Sample(Instant.ofEpochMilli(1700000001000), 61)),
                    metadata,
                ),
            ),
            HealthConnectRecordMapper.map(
                SleepSessionRecord(
                    Instant.ofEpochMilli(1699980000000),
                    null,
                    Instant.ofEpochMilli(1700008800000),
                    null,
                    metadata,
                    "Night sleep",
                    "note",
                    listOf(SleepSessionRecord.Stage(Instant.ofEpochMilli(1699980000000), Instant.ofEpochMilli(1699983600000), 4)),
                ),
            ),
            HealthConnectRecordMapper.map(
                StepsRecord(
                    Instant.ofEpochMilli(1700000000000),
                    ZoneOffset.ofHours(1),
                    Instant.ofEpochMilli(1700003600000),
                    ZoneOffset.ofHours(1),
                    1234,
                    metadata,
                ),
            ),
            HealthConnectRecordMapper.map(
                ExerciseSessionRecord(
                    Instant.ofEpochMilli(1700000000000),
                    ZoneOffset.ofHours(-5),
                    Instant.ofEpochMilli(1700003600000),
                    ZoneOffset.ofHours(-5),
                    metadata,
                    ExerciseSessionRecord.EXERCISE_TYPE_RUNNING,
                    "Morning run",
                    null,
                    listOf(AndroidExerciseSegment(Instant.ofEpochMilli(1700000000000), Instant.ofEpochMilli(1700000600000), AndroidExerciseSegment.EXERCISE_SEGMENT_TYPE_RUNNING, 2)),
                    listOf(AndroidExerciseLap(Instant.ofEpochMilli(1700000000000), Instant.ofEpochMilli(1700000600000), Length.meters(1000.5))),
                    ExerciseRoute(
                        listOf(
                            ExerciseRoute.Location(
                                Instant.ofEpochMilli(1700000001000),
                                40.1,
                                -73.2,
                                Length.meters(3.2),
                                null,
                                Length.meters(12.5),
                            ),
                        ),
                    ),
                    "planned-1",
                ),
            ),
        )

        assertEquals(
            HealthConnectRecord.HeartRate(
                metadata = expectedMetadata,
                startTimeEpochMillis = 1700000000000,
                endTimeEpochMillis = 1700000060000,
                startZoneOffsetSeconds = -18000,
                endZoneOffsetSeconds = -18000,
                samples = listOf(HeartRateSample(1700000001000, 61)),
            ),
            records[0],
        )
        assertEquals(listOf(SleepStage(1699980000000, 1699983600000, 4)), (records[1] as HealthConnectRecord.Sleep).stages)
        assertEquals(1234, (records[2] as HealthConnectRecord.Steps).count)
        val exercise = records[3] as HealthConnectRecord.Exercise
        assertEquals(listOf(ExerciseSegment(1700000000000, 1700000600000, AndroidExerciseSegment.EXERCISE_SEGMENT_TYPE_RUNNING, 2)), exercise.segments)
        assertEquals(listOf(ExerciseLap(1700000000000, 1700000600000, 1000.5)), exercise.laps)
        assertEquals(listOf(ExerciseRoutePoint(1700000001000, 40.1, -73.2, 3.2, null, 12.5)), exercise.route)
        assertEquals("planned-1", exercise.plannedExerciseSessionId)
    }

    private fun metadata(
        recordingMethod: Int,
        id: String,
        dataOrigin: DataOrigin,
        lastModifiedTime: Instant,
        clientRecordId: String?,
        clientRecordVersion: Long,
        device: Device?,
    ): Metadata {
        val constructor = Metadata::class.java.constructors.first { it.parameterTypes.size == 7 }
        return constructor.newInstance(
            recordingMethod,
            id,
            dataOrigin,
            lastModifiedTime,
            clientRecordId,
            clientRecordVersion,
            device,
        ) as Metadata
    }

    private val expectedMetadata = HealthConnectMetadata(
        id = "upstream-id",
        dataOriginPackage = "com.example.watch",
        lastModifiedTimeEpochMillis = 1700000000100,
        clientRecordId = "client-id",
        clientRecordVersion = 7,
        device = HealthConnectDevice("Google", "Pixel Watch 3", Device.TYPE_WATCH),
        recordingMethod = Metadata.RECORDING_METHOD_ACTIVELY_RECORDED,
    )
}
