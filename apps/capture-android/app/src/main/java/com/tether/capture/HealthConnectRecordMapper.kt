package com.tether.capture

import androidx.health.connect.client.records.ExerciseRouteResult
import androidx.health.connect.client.records.ExerciseSessionRecord as AndroidExerciseSessionRecord
import androidx.health.connect.client.records.HeartRateRecord as AndroidHeartRateRecord
import androidx.health.connect.client.records.SleepSessionRecord as AndroidSleepSessionRecord
import androidx.health.connect.client.records.StepsRecord as AndroidStepsRecord
import androidx.health.connect.client.records.metadata.Metadata
import androidx.health.connect.client.units.Length
import java.time.Instant
import java.time.ZoneOffset

object HealthConnectRecordMapper {
    fun map(record: AndroidHeartRateRecord): HealthConnectRecord.HeartRate = HealthConnectRecord.HeartRate(
        metadata = mapMetadata(record.metadata),
        startTimeEpochMillis = record.startTime.toEpochMilli(),
        endTimeEpochMillis = record.endTime.toEpochMilli(),
        startZoneOffsetSeconds = record.startZoneOffset.secondsOrNull(),
        endZoneOffsetSeconds = record.endZoneOffset.secondsOrNull(),
        samples = record.samples.map { sample ->
            HeartRateSample(
                timeEpochMillis = sample.time.toEpochMilli(),
                beatsPerMinute = sample.beatsPerMinute,
            )
        },
    )

    fun map(record: AndroidSleepSessionRecord): HealthConnectRecord.Sleep = HealthConnectRecord.Sleep(
        metadata = mapMetadata(record.metadata),
        startTimeEpochMillis = record.startTime.toEpochMilli(),
        endTimeEpochMillis = record.endTime.toEpochMilli(),
        startZoneOffsetSeconds = record.startZoneOffset.secondsOrNull(),
        endZoneOffsetSeconds = record.endZoneOffset.secondsOrNull(),
        title = record.title,
        notes = record.notes,
        stages = record.stages.map { stage ->
            SleepStage(
                startTimeEpochMillis = stage.startTime.toEpochMilli(),
                endTimeEpochMillis = stage.endTime.toEpochMilli(),
                stage = stage.stage,
            )
        },
    )

    fun map(record: AndroidStepsRecord): HealthConnectRecord.Steps = HealthConnectRecord.Steps(
        metadata = mapMetadata(record.metadata),
        startTimeEpochMillis = record.startTime.toEpochMilli(),
        endTimeEpochMillis = record.endTime.toEpochMilli(),
        startZoneOffsetSeconds = record.startZoneOffset.secondsOrNull(),
        endZoneOffsetSeconds = record.endZoneOffset.secondsOrNull(),
        count = record.count,
    )

    fun map(record: AndroidExerciseSessionRecord): HealthConnectRecord.Exercise = HealthConnectRecord.Exercise(
        metadata = mapMetadata(record.metadata),
        startTimeEpochMillis = record.startTime.toEpochMilli(),
        endTimeEpochMillis = record.endTime.toEpochMilli(),
        startZoneOffsetSeconds = record.startZoneOffset.secondsOrNull(),
        endZoneOffsetSeconds = record.endZoneOffset.secondsOrNull(),
        exerciseType = record.exerciseType,
        title = record.title,
        notes = record.notes,
        plannedExerciseSessionId = record.plannedExerciseSessionId,
        segments = record.segments.map { segment ->
            ExerciseSegment(
                startTimeEpochMillis = segment.startTime.toEpochMilli(),
                endTimeEpochMillis = segment.endTime.toEpochMilli(),
                segmentType = segment.segmentType,
                repetitionsCount = segment.repetitions.toLong(),
            )
        },
        laps = record.laps.map { lap ->
            ExerciseLap(
                startTimeEpochMillis = lap.startTime.toEpochMilli(),
                endTimeEpochMillis = lap.endTime.toEpochMilli(),
                lengthMeters = lap.length.metersOrNull(),
            )
        },
        route = record.exerciseRouteResult.routeLocations().map { location ->
            ExerciseRoutePoint(
                timeEpochMillis = location.time.toEpochMilli(),
                latitude = location.latitude,
                longitude = location.longitude,
                horizontalAccuracyMeters = location.horizontalAccuracy.metersOrNull(),
                verticalAccuracyMeters = location.verticalAccuracy.metersOrNull(),
                altitudeMeters = location.altitude.metersOrNull(),
            )
        },
    )

    private fun mapMetadata(metadata: Metadata): HealthConnectMetadata = HealthConnectMetadata(
        id = metadata.id,
        dataOriginPackage = metadata.dataOrigin.packageName,
        lastModifiedTimeEpochMillis = metadata.lastModifiedTime.nullIfEpochZero()?.toEpochMilli(),
        clientRecordId = metadata.clientRecordId,
        clientRecordVersion = metadata.clientRecordVersion.takeIf { it != 0L },
        device = metadata.device?.let { device ->
            HealthConnectDevice(
                manufacturer = device.manufacturer,
                model = device.model,
                type = device.type,
            )
        },
        recordingMethod = metadata.recordingMethod.takeIf { it != Metadata.RECORDING_METHOD_UNKNOWN },
    )

    private fun ExerciseRouteResult.routeLocations() = when (this) {
        is ExerciseRouteResult.Data -> exerciseRoute.route
        else -> emptyList()
    }

    private fun Length?.metersOrNull(): Double? = this?.let { value ->
        value.javaClass.getMethod("getMeters").invoke(value) as Double
    }

    private fun ZoneOffset?.secondsOrNull(): Int? = this?.totalSeconds

    private fun Instant.nullIfEpochZero(): Instant? = takeUnless { it == Instant.EPOCH }
}
