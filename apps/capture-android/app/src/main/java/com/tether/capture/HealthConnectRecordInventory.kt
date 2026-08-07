package com.tether.capture

import androidx.health.connect.client.permission.HealthPermission
import androidx.health.connect.client.records.ActiveCaloriesBurnedRecord
import androidx.health.connect.client.records.BasalBodyTemperatureRecord
import androidx.health.connect.client.records.BasalMetabolicRateRecord
import androidx.health.connect.client.records.BloodGlucoseRecord
import androidx.health.connect.client.records.BloodPressureRecord
import androidx.health.connect.client.records.BodyFatRecord
import androidx.health.connect.client.records.BodyTemperatureRecord
import androidx.health.connect.client.records.BodyWaterMassRecord
import androidx.health.connect.client.records.BoneMassRecord
import androidx.health.connect.client.records.CervicalMucusRecord
import androidx.health.connect.client.records.CyclingPedalingCadenceRecord
import androidx.health.connect.client.records.DistanceRecord
import androidx.health.connect.client.records.ElevationGainedRecord
import androidx.health.connect.client.records.ExerciseSessionRecord
import androidx.health.connect.client.records.FloorsClimbedRecord
import androidx.health.connect.client.records.HeartRateRecord
import androidx.health.connect.client.records.HeartRateVariabilityRmssdRecord
import androidx.health.connect.client.records.HeightRecord
import androidx.health.connect.client.records.HydrationRecord
import androidx.health.connect.client.records.IntermenstrualBleedingRecord
import androidx.health.connect.client.records.LeanBodyMassRecord
import androidx.health.connect.client.records.MenstruationFlowRecord
import androidx.health.connect.client.records.MenstruationPeriodRecord
import androidx.health.connect.client.records.MindfulnessSessionRecord
import androidx.health.connect.client.records.NutritionRecord
import androidx.health.connect.client.records.OvulationTestRecord
import androidx.health.connect.client.records.OxygenSaturationRecord
import androidx.health.connect.client.records.PlannedExerciseSessionRecord
import androidx.health.connect.client.records.PowerRecord
import androidx.health.connect.client.records.Record
import androidx.health.connect.client.records.RespiratoryRateRecord
import androidx.health.connect.client.records.RestingHeartRateRecord
import androidx.health.connect.client.records.SexualActivityRecord
import androidx.health.connect.client.records.SkinTemperatureRecord
import androidx.health.connect.client.records.SleepSessionRecord
import androidx.health.connect.client.records.SpeedRecord
import androidx.health.connect.client.records.StepsCadenceRecord
import androidx.health.connect.client.records.StepsRecord
import androidx.health.connect.client.records.TotalCaloriesBurnedRecord
import androidx.health.connect.client.records.Vo2MaxRecord
import androidx.health.connect.client.records.WeightRecord
import androidx.health.connect.client.records.WheelchairPushesRecord
import kotlin.reflect.KClass

enum class HealthConnectRecordCaptureStatus {
    CAPTURED_V2,
    CAPTURED_GENERIC_V3,
}

data class HealthConnectRecordInventoryEntry(
    val recordType: HealthConnectRecordType,
    val recordClass: KClass<out Record>,
    val readPermission: String,
    val status: HealthConnectRecordCaptureStatus,
)

object HealthConnectRecordInventory {
    val entries: List<HealthConnectRecordInventoryEntry> = listOf(
        captured(HealthConnectRecordType.EXERCISE, ExerciseSessionRecord::class),
        captured(HealthConnectRecordType.HEART_RATE, HeartRateRecord::class),
        captured(HealthConnectRecordType.SLEEP, SleepSessionRecord::class),
        captured(HealthConnectRecordType.STEPS, StepsRecord::class),
        generic(HealthConnectRecordType.ACTIVE_CALORIES_BURNED, ActiveCaloriesBurnedRecord::class),
        generic(HealthConnectRecordType.BASAL_BODY_TEMPERATURE, BasalBodyTemperatureRecord::class),
        generic(HealthConnectRecordType.BASAL_METABOLIC_RATE, BasalMetabolicRateRecord::class),
        generic(HealthConnectRecordType.BLOOD_GLUCOSE, BloodGlucoseRecord::class),
        generic(HealthConnectRecordType.BLOOD_PRESSURE, BloodPressureRecord::class),
        generic(HealthConnectRecordType.BODY_FAT, BodyFatRecord::class),
        generic(HealthConnectRecordType.BODY_TEMPERATURE, BodyTemperatureRecord::class),
        generic(HealthConnectRecordType.BODY_WATER_MASS, BodyWaterMassRecord::class),
        generic(HealthConnectRecordType.BONE_MASS, BoneMassRecord::class),
        generic(HealthConnectRecordType.CERVICAL_MUCUS, CervicalMucusRecord::class),
        generic(HealthConnectRecordType.CYCLING_PEDALING_CADENCE, CyclingPedalingCadenceRecord::class),
        generic(HealthConnectRecordType.DISTANCE, DistanceRecord::class),
        generic(HealthConnectRecordType.ELEVATION_GAINED, ElevationGainedRecord::class),
        generic(HealthConnectRecordType.FLOORS_CLIMBED, FloorsClimbedRecord::class),
        generic(HealthConnectRecordType.HEART_RATE_VARIABILITY_RMSSD, HeartRateVariabilityRmssdRecord::class),
        generic(HealthConnectRecordType.HEIGHT, HeightRecord::class),
        generic(HealthConnectRecordType.HYDRATION, HydrationRecord::class),
        generic(HealthConnectRecordType.INTERMENSTRUAL_BLEEDING, IntermenstrualBleedingRecord::class),
        generic(HealthConnectRecordType.LEAN_BODY_MASS, LeanBodyMassRecord::class),
        generic(HealthConnectRecordType.MENSTRUATION_FLOW, MenstruationFlowRecord::class),
        generic(HealthConnectRecordType.MENSTRUATION_PERIOD, MenstruationPeriodRecord::class),
        generic(HealthConnectRecordType.MINDFULNESS_SESSION, MindfulnessSessionRecord::class),
        generic(HealthConnectRecordType.NUTRITION, NutritionRecord::class),
        generic(HealthConnectRecordType.OVULATION_TEST, OvulationTestRecord::class),
        generic(HealthConnectRecordType.OXYGEN_SATURATION, OxygenSaturationRecord::class),
        generic(HealthConnectRecordType.PLANNED_EXERCISE_SESSION, PlannedExerciseSessionRecord::class),
        generic(HealthConnectRecordType.POWER, PowerRecord::class),
        generic(HealthConnectRecordType.RESPIRATORY_RATE, RespiratoryRateRecord::class),
        generic(HealthConnectRecordType.RESTING_HEART_RATE, RestingHeartRateRecord::class),
        generic(HealthConnectRecordType.SEXUAL_ACTIVITY, SexualActivityRecord::class),
        generic(HealthConnectRecordType.SKIN_TEMPERATURE, SkinTemperatureRecord::class),
        generic(HealthConnectRecordType.SPEED, SpeedRecord::class),
        generic(HealthConnectRecordType.STEPS_CADENCE, StepsCadenceRecord::class),
        generic(HealthConnectRecordType.TOTAL_CALORIES_BURNED, TotalCaloriesBurnedRecord::class),
        generic(HealthConnectRecordType.VO2_MAX, Vo2MaxRecord::class),
        generic(HealthConnectRecordType.WEIGHT, WeightRecord::class),
        generic(HealthConnectRecordType.WHEELCHAIR_PUSHES, WheelchairPushesRecord::class),
    )

    val entryByRecordType: Map<HealthConnectRecordType, HealthConnectRecordInventoryEntry> = entries
        .associateBy { it.recordType }

    val entryByRecordClass: Map<KClass<out Record>, HealthConnectRecordInventoryEntry> = entries
        .associateBy { it.recordClass }

    private fun captured(
        recordType: HealthConnectRecordType,
        recordClass: KClass<out Record>,
    ): HealthConnectRecordInventoryEntry = HealthConnectRecordInventoryEntry(
        recordType = recordType,
        recordClass = recordClass,
        readPermission = HealthPermission.getReadPermission(recordClass),
        status = HealthConnectRecordCaptureStatus.CAPTURED_V2,
    )

    private fun generic(
        recordType: HealthConnectRecordType,
        recordClass: KClass<out Record>,
    ): HealthConnectRecordInventoryEntry = HealthConnectRecordInventoryEntry(
        recordType = recordType,
        recordClass = recordClass,
        readPermission = HealthPermission.getReadPermission(recordClass),
        status = HealthConnectRecordCaptureStatus.CAPTURED_GENERIC_V3,
    )
}
