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
    CAPTURE_PLANNED,
}

data class HealthConnectRecordInventoryEntry(
    val recordClass: KClass<out Record>,
    val readPermission: String,
    val status: HealthConnectRecordCaptureStatus,
)

object HealthConnectRecordInventory {
    val entries: List<HealthConnectRecordInventoryEntry> = listOf(
        captured(ExerciseSessionRecord::class),
        captured(HeartRateRecord::class),
        captured(SleepSessionRecord::class),
        captured(StepsRecord::class),
        planned(ActiveCaloriesBurnedRecord::class),
        planned(BasalBodyTemperatureRecord::class),
        planned(BasalMetabolicRateRecord::class),
        planned(BloodGlucoseRecord::class),
        planned(BloodPressureRecord::class),
        planned(BodyFatRecord::class),
        planned(BodyTemperatureRecord::class),
        planned(BodyWaterMassRecord::class),
        planned(BoneMassRecord::class),
        planned(CervicalMucusRecord::class),
        planned(CyclingPedalingCadenceRecord::class),
        planned(DistanceRecord::class),
        planned(ElevationGainedRecord::class),
        planned(FloorsClimbedRecord::class),
        planned(HeartRateVariabilityRmssdRecord::class),
        planned(HeightRecord::class),
        planned(HydrationRecord::class),
        planned(IntermenstrualBleedingRecord::class),
        planned(LeanBodyMassRecord::class),
        planned(MenstruationFlowRecord::class),
        planned(MenstruationPeriodRecord::class),
        planned(MindfulnessSessionRecord::class),
        planned(NutritionRecord::class),
        planned(OvulationTestRecord::class),
        planned(OxygenSaturationRecord::class),
        planned(PlannedExerciseSessionRecord::class),
        planned(PowerRecord::class),
        planned(RespiratoryRateRecord::class),
        planned(RestingHeartRateRecord::class),
        planned(SexualActivityRecord::class),
        planned(SkinTemperatureRecord::class),
        planned(SpeedRecord::class),
        planned(StepsCadenceRecord::class),
        planned(TotalCaloriesBurnedRecord::class),
        planned(Vo2MaxRecord::class),
        planned(WeightRecord::class),
        planned(WheelchairPushesRecord::class),
    )

    private fun captured(
        recordClass: KClass<out Record>,
    ): HealthConnectRecordInventoryEntry = HealthConnectRecordInventoryEntry(
        recordClass = recordClass,
        readPermission = HealthPermission.getReadPermission(recordClass),
        status = HealthConnectRecordCaptureStatus.CAPTURED_V2,
    )

    private fun planned(
        recordClass: KClass<out Record>,
    ): HealthConnectRecordInventoryEntry = HealthConnectRecordInventoryEntry(
        recordClass = recordClass,
        readPermission = HealthPermission.getReadPermission(recordClass),
        status = HealthConnectRecordCaptureStatus.CAPTURE_PLANNED,
    )
}
