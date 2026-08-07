# Health Connect record inventory

Pinned client: `androidx.health.connect:connect-client:1.1.0-alpha12`.

Status vocabulary:

- `captured_v2`: mapped to a typed v2/v3 shape and persisted in typed host tables.
- `captured_generic_v3`: mapped to v3 generic JSON payloads and persisted in
  generic host storage until typed per-record projections are needed.

The pinned SDK does not expose medical-record typed `Record` classes. Medical
records remain outside this inventory until the AndroidX client exposes stable
readable record types and Tether has a policy/permission treatment for them.

| Permission | Record type | Status |
| --- | --- | --- |
| `READ_ACTIVE_CALORIES_BURNED` | `ActiveCaloriesBurnedRecord` | `captured_generic_v3` |
| `READ_BASAL_BODY_TEMPERATURE` | `BasalBodyTemperatureRecord` | `captured_generic_v3` |
| `READ_BASAL_METABOLIC_RATE` | `BasalMetabolicRateRecord` | `captured_generic_v3` |
| `READ_BLOOD_GLUCOSE` | `BloodGlucoseRecord` | `captured_generic_v3` |
| `READ_BLOOD_PRESSURE` | `BloodPressureRecord` | `captured_generic_v3` |
| `READ_BODY_FAT` | `BodyFatRecord` | `captured_generic_v3` |
| `READ_BODY_TEMPERATURE` | `BodyTemperatureRecord` | `captured_generic_v3` |
| `READ_BODY_WATER_MASS` | `BodyWaterMassRecord` | `captured_generic_v3` |
| `READ_BONE_MASS` | `BoneMassRecord` | `captured_generic_v3` |
| `READ_CERVICAL_MUCUS` | `CervicalMucusRecord` | `captured_generic_v3` |
| `READ_DISTANCE` | `DistanceRecord` | `captured_generic_v3` |
| `READ_ELEVATION_GAINED` | `ElevationGainedRecord` | `captured_generic_v3` |
| `READ_EXERCISE` | `CyclingPedalingCadenceRecord` | `captured_generic_v3` |
| `READ_EXERCISE` | `ExerciseSessionRecord` | `captured_v2` |
| `READ_FLOORS_CLIMBED` | `FloorsClimbedRecord` | `captured_generic_v3` |
| `READ_HEART_RATE` | `HeartRateRecord` | `captured_v2` |
| `READ_HEART_RATE_VARIABILITY` | `HeartRateVariabilityRmssdRecord` | `captured_generic_v3` |
| `READ_HEIGHT` | `HeightRecord` | `captured_generic_v3` |
| `READ_HYDRATION` | `HydrationRecord` | `captured_generic_v3` |
| `READ_INTERMENSTRUAL_BLEEDING` | `IntermenstrualBleedingRecord` | `captured_generic_v3` |
| `READ_LEAN_BODY_MASS` | `LeanBodyMassRecord` | `captured_generic_v3` |
| `READ_MENSTRUATION` | `MenstruationFlowRecord` | `captured_generic_v3` |
| `READ_MENSTRUATION` | `MenstruationPeriodRecord` | `captured_generic_v3` |
| `READ_MINDFULNESS_SESSION` | `MindfulnessSessionRecord` | `captured_generic_v3` |
| `READ_NUTRITION` | `NutritionRecord` | `captured_generic_v3` |
| `READ_OVULATION_TEST` | `OvulationTestRecord` | `captured_generic_v3` |
| `READ_OXYGEN_SATURATION` | `OxygenSaturationRecord` | `captured_generic_v3` |
| `READ_PLANNED_EXERCISE` | `PlannedExerciseSessionRecord` | `captured_generic_v3` |
| `READ_POWER` | `PowerRecord` | `captured_generic_v3` |
| `READ_RESPIRATORY_RATE` | `RespiratoryRateRecord` | `captured_generic_v3` |
| `READ_RESTING_HEART_RATE` | `RestingHeartRateRecord` | `captured_generic_v3` |
| `READ_SEXUAL_ACTIVITY` | `SexualActivityRecord` | `captured_generic_v3` |
| `READ_SKIN_TEMPERATURE` | `SkinTemperatureRecord` | `captured_generic_v3` |
| `READ_SLEEP` | `SleepSessionRecord` | `captured_v2` |
| `READ_SPEED` | `SpeedRecord` | `captured_generic_v3` |
| `READ_STEPS` | `StepsCadenceRecord` | `captured_generic_v3` |
| `READ_STEPS` | `StepsRecord` | `captured_v2` |
| `READ_TOTAL_CALORIES_BURNED` | `TotalCaloriesBurnedRecord` | `captured_generic_v3` |
| `READ_VO2_MAX` | `Vo2MaxRecord` | `captured_generic_v3` |
| `READ_WEIGHT` | `WeightRecord` | `captured_generic_v3` |
| `READ_WHEELCHAIR_PUSHES` | `WheelchairPushesRecord` | `captured_generic_v3` |
