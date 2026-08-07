# Health Connect record inventory

Pinned client: `androidx.health.connect:connect-client:1.1.0-alpha12`.

Status vocabulary:

- `captured_v2`: mapped to the current wire contract and persisted by host.
- `capture_planned`: readable in the pinned SDK; inventory prevents silent
  omission while the expanded contract/storage work lands.

The pinned SDK does not expose medical-record typed `Record` classes. Medical
records remain outside this inventory until the AndroidX client exposes stable
readable record types and Tether has a policy/permission treatment for them.

| Permission | Record type | Status |
| --- | --- | --- |
| `READ_ACTIVE_CALORIES_BURNED` | `ActiveCaloriesBurnedRecord` | `capture_planned` |
| `READ_BASAL_BODY_TEMPERATURE` | `BasalBodyTemperatureRecord` | `capture_planned` |
| `READ_BASAL_METABOLIC_RATE` | `BasalMetabolicRateRecord` | `capture_planned` |
| `READ_BLOOD_GLUCOSE` | `BloodGlucoseRecord` | `capture_planned` |
| `READ_BLOOD_PRESSURE` | `BloodPressureRecord` | `capture_planned` |
| `READ_BODY_FAT` | `BodyFatRecord` | `capture_planned` |
| `READ_BODY_TEMPERATURE` | `BodyTemperatureRecord` | `capture_planned` |
| `READ_BODY_WATER_MASS` | `BodyWaterMassRecord` | `capture_planned` |
| `READ_BONE_MASS` | `BoneMassRecord` | `capture_planned` |
| `READ_CERVICAL_MUCUS` | `CervicalMucusRecord` | `capture_planned` |
| `READ_DISTANCE` | `DistanceRecord` | `capture_planned` |
| `READ_ELEVATION_GAINED` | `ElevationGainedRecord` | `capture_planned` |
| `READ_EXERCISE` | `CyclingPedalingCadenceRecord` | `capture_planned` |
| `READ_EXERCISE` | `ExerciseSessionRecord` | `captured_v2` |
| `READ_FLOORS_CLIMBED` | `FloorsClimbedRecord` | `capture_planned` |
| `READ_HEART_RATE` | `HeartRateRecord` | `captured_v2` |
| `READ_HEART_RATE_VARIABILITY` | `HeartRateVariabilityRmssdRecord` | `capture_planned` |
| `READ_HEIGHT` | `HeightRecord` | `capture_planned` |
| `READ_HYDRATION` | `HydrationRecord` | `capture_planned` |
| `READ_INTERMENSTRUAL_BLEEDING` | `IntermenstrualBleedingRecord` | `capture_planned` |
| `READ_LEAN_BODY_MASS` | `LeanBodyMassRecord` | `capture_planned` |
| `READ_MENSTRUATION` | `MenstruationFlowRecord` | `capture_planned` |
| `READ_MENSTRUATION` | `MenstruationPeriodRecord` | `capture_planned` |
| `READ_MINDFULNESS_SESSION` | `MindfulnessSessionRecord` | `capture_planned` |
| `READ_NUTRITION` | `NutritionRecord` | `capture_planned` |
| `READ_OVULATION_TEST` | `OvulationTestRecord` | `capture_planned` |
| `READ_OXYGEN_SATURATION` | `OxygenSaturationRecord` | `capture_planned` |
| `READ_PLANNED_EXERCISE` | `PlannedExerciseSessionRecord` | `capture_planned` |
| `READ_POWER` | `PowerRecord` | `capture_planned` |
| `READ_RESPIRATORY_RATE` | `RespiratoryRateRecord` | `capture_planned` |
| `READ_RESTING_HEART_RATE` | `RestingHeartRateRecord` | `capture_planned` |
| `READ_SEXUAL_ACTIVITY` | `SexualActivityRecord` | `capture_planned` |
| `READ_SKIN_TEMPERATURE` | `SkinTemperatureRecord` | `capture_planned` |
| `READ_SLEEP` | `SleepSessionRecord` | `captured_v2` |
| `READ_SPEED` | `SpeedRecord` | `capture_planned` |
| `READ_STEPS` | `StepsCadenceRecord` | `capture_planned` |
| `READ_STEPS` | `StepsRecord` | `captured_v2` |
| `READ_TOTAL_CALORIES_BURNED` | `TotalCaloriesBurnedRecord` | `capture_planned` |
| `READ_VO2_MAX` | `Vo2MaxRecord` | `capture_planned` |
| `READ_WEIGHT` | `WeightRecord` | `capture_planned` |
| `READ_WHEELCHAIR_PUSHES` | `WheelchairPushesRecord` | `capture_planned` |
