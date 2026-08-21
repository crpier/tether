"""Versioned Health Connect wire contract and validation rules."""

from __future__ import annotations

from typing import Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

HealthRecordType = Literal[
    "active_calories_burned",
    "basal_body_temperature",
    "basal_metabolic_rate",
    "blood_glucose",
    "blood_pressure",
    "body_fat",
    "body_temperature",
    "body_water_mass",
    "bone_mass",
    "cervical_mucus",
    "cycling_pedaling_cadence",
    "distance",
    "elevation_gained",
    "exercise",
    "floors_climbed",
    "heart_rate",
    "heart_rate_variability_rmssd",
    "height",
    "hydration",
    "intermenstrual_bleeding",
    "lean_body_mass",
    "menstruation_flow",
    "menstruation_period",
    "mindfulness_session",
    "nutrition",
    "ovulation_test",
    "oxygen_saturation",
    "planned_exercise_session",
    "power",
    "respiratory_rate",
    "resting_heart_rate",
    "sexual_activity",
    "skin_temperature",
    "sleep",
    "speed",
    "steps",
    "steps_cadence",
    "total_calories_burned",
    "vo2_max",
    "weight",
    "wheelchair_pushes",
]
_ALL_RECORD_TYPES: tuple[HealthRecordType, ...] = (
    "active_calories_burned",
    "basal_body_temperature",
    "basal_metabolic_rate",
    "blood_glucose",
    "blood_pressure",
    "body_fat",
    "body_temperature",
    "body_water_mass",
    "bone_mass",
    "cervical_mucus",
    "cycling_pedaling_cadence",
    "distance",
    "elevation_gained",
    "exercise",
    "floors_climbed",
    "heart_rate",
    "heart_rate_variability_rmssd",
    "height",
    "hydration",
    "intermenstrual_bleeding",
    "lean_body_mass",
    "menstruation_flow",
    "menstruation_period",
    "mindfulness_session",
    "nutrition",
    "ovulation_test",
    "oxygen_saturation",
    "planned_exercise_session",
    "power",
    "respiratory_rate",
    "resting_heart_rate",
    "sexual_activity",
    "skin_temperature",
    "sleep",
    "speed",
    "steps",
    "steps_cadence",
    "total_calories_burned",
    "vo2_max",
    "weight",
    "wheelchair_pushes",
)
_CAPTURED_RECORD_TYPES = frozenset({"exercise", "heart_rate", "sleep", "steps"})
_ALLOWED_RECORD_TYPES = frozenset(_ALL_RECORD_TYPES)
GENERIC_RECORD_TYPES: tuple[HealthRecordType, ...] = tuple(
    record_type
    for record_type in _ALL_RECORD_TYPES
    if record_type not in _CAPTURED_RECORD_TYPES
)
RecordStatus = Literal["baseline", "changes", "initial"]
GENERIC_RECORD_CONTRACT_VERSION = 3


class HealthConnectWireModel(BaseModel):
    """Strict base for the versioned Android/host JSON boundary."""

    model_config = ConfigDict(extra="forbid")


class Device(HealthConnectWireModel):
    """Nullable Health Connect writing-device metadata."""

    manufacturer: str | None = None
    model: str | None = None
    type: int | None = None


class RecordMetadata(HealthConnectWireModel):
    """Common metadata exposed by the pinned Health Connect wire contract."""

    id: str = Field(min_length=1)
    data_origin_package: str = Field(min_length=1)
    last_modified_time: int | None
    client_record_id: str | None
    client_record_version: int | None
    device: Device | None
    recording_method: int | None


class HeartRateSample(HealthConnectWireModel):
    time: int
    beats_per_minute: int = Field(gt=0)


class HeartRateRecord(HealthConnectWireModel):
    metadata: RecordMetadata
    start_time: int
    end_time: int
    start_zone_offset_seconds: int | None
    end_zone_offset_seconds: int | None
    samples: list[HeartRateSample] = Field(max_length=10_000)


class SleepStage(HealthConnectWireModel):
    start_time: int
    end_time: int
    stage: int


class SleepRecord(HealthConnectWireModel):
    metadata: RecordMetadata
    start_time: int
    end_time: int
    start_zone_offset_seconds: int | None
    end_zone_offset_seconds: int | None
    title: str | None
    notes: str | None
    stages: list[SleepStage] = Field(max_length=1_000)


class StepsRecord(HealthConnectWireModel):
    metadata: RecordMetadata
    start_time: int
    end_time: int
    start_zone_offset_seconds: int | None
    end_zone_offset_seconds: int | None
    count: int = Field(ge=0)


class ExerciseSegment(HealthConnectWireModel):
    start_time: int
    end_time: int
    segment_type: int
    repetitions_count: int = Field(ge=0)


class ExerciseLap(HealthConnectWireModel):
    start_time: int
    end_time: int
    length_meters: float | None


class ExerciseRoutePoint(HealthConnectWireModel):
    time: int
    latitude: float
    longitude: float
    horizontal_accuracy_meters: float | None
    vertical_accuracy_meters: float | None
    altitude_meters: float | None


class ExerciseRecord(HealthConnectWireModel):
    metadata: RecordMetadata
    start_time: int
    end_time: int
    start_zone_offset_seconds: int | None
    end_zone_offset_seconds: int | None
    exercise_type: int
    title: str | None
    notes: str | None
    planned_exercise_session_id: str | None
    segments: list[ExerciseSegment] = Field(max_length=10_000)
    laps: list[ExerciseLap] = Field(max_length=10_000)
    route: list[ExerciseRoutePoint] = Field(max_length=100_000)


class GenericRecord(HealthConnectWireModel):
    metadata: RecordMetadata
    start_time: int | None = None
    end_time: int | None = None
    start_zone_offset_seconds: int | None = None
    end_zone_offset_seconds: int | None = None
    payload: dict[str, object] = Field(default_factory=dict)


def _typed_record_list_field() -> int:
    # A long sync gap replays one Health Connect changes page as a single
    # batch; the cap must absorb a multi-week backlog (observed: >1000
    # heart-rate records after 12 days of continuous watch data) or the page
    # is rejected wholesale and the changes token never advances.
    return 25_000


def _generic_record_list_field() -> list[GenericRecord]:
    return cast(
        "list[GenericRecord]",
        Field(default_factory=list, max_length=_typed_record_list_field()),
    )


def _exercise_record_list_field() -> list[ExerciseRecord]:
    return cast(
        "list[ExerciseRecord]",
        Field(default_factory=list, max_length=_typed_record_list_field()),
    )


def _heart_rate_record_list_field() -> list[HeartRateRecord]:
    return cast(
        "list[HeartRateRecord]",
        Field(default_factory=list, max_length=_typed_record_list_field()),
    )


def _sleep_record_list_field() -> list[SleepRecord]:
    return cast(
        "list[SleepRecord]",
        Field(default_factory=list, max_length=_typed_record_list_field()),
    )


def _steps_record_list_field() -> list[StepsRecord]:
    return cast(
        "list[StepsRecord]",
        Field(default_factory=list, max_length=_typed_record_list_field()),
    )


class HealthConnectRecords(HealthConnectWireModel):
    active_calories_burned: list[GenericRecord] = _generic_record_list_field()
    basal_body_temperature: list[GenericRecord] = _generic_record_list_field()
    basal_metabolic_rate: list[GenericRecord] = _generic_record_list_field()
    blood_glucose: list[GenericRecord] = _generic_record_list_field()
    blood_pressure: list[GenericRecord] = _generic_record_list_field()
    body_fat: list[GenericRecord] = _generic_record_list_field()
    body_temperature: list[GenericRecord] = _generic_record_list_field()
    body_water_mass: list[GenericRecord] = _generic_record_list_field()
    bone_mass: list[GenericRecord] = _generic_record_list_field()
    cervical_mucus: list[GenericRecord] = _generic_record_list_field()
    cycling_pedaling_cadence: list[GenericRecord] = _generic_record_list_field()
    distance: list[GenericRecord] = _generic_record_list_field()
    elevation_gained: list[GenericRecord] = _generic_record_list_field()
    exercise: list[ExerciseRecord] = _exercise_record_list_field()
    floors_climbed: list[GenericRecord] = _generic_record_list_field()
    heart_rate: list[HeartRateRecord] = _heart_rate_record_list_field()
    heart_rate_variability_rmssd: list[GenericRecord] = _generic_record_list_field()
    height: list[GenericRecord] = _generic_record_list_field()
    hydration: list[GenericRecord] = _generic_record_list_field()
    intermenstrual_bleeding: list[GenericRecord] = _generic_record_list_field()
    lean_body_mass: list[GenericRecord] = _generic_record_list_field()
    menstruation_flow: list[GenericRecord] = _generic_record_list_field()
    menstruation_period: list[GenericRecord] = _generic_record_list_field()
    mindfulness_session: list[GenericRecord] = _generic_record_list_field()
    nutrition: list[GenericRecord] = _generic_record_list_field()
    ovulation_test: list[GenericRecord] = _generic_record_list_field()
    oxygen_saturation: list[GenericRecord] = _generic_record_list_field()
    planned_exercise_session: list[GenericRecord] = _generic_record_list_field()
    power: list[GenericRecord] = _generic_record_list_field()
    respiratory_rate: list[GenericRecord] = _generic_record_list_field()
    resting_heart_rate: list[GenericRecord] = _generic_record_list_field()
    sexual_activity: list[GenericRecord] = _generic_record_list_field()
    skin_temperature: list[GenericRecord] = _generic_record_list_field()
    sleep: list[SleepRecord] = _sleep_record_list_field()
    speed: list[GenericRecord] = _generic_record_list_field()
    steps: list[StepsRecord] = _steps_record_list_field()
    steps_cadence: list[GenericRecord] = _generic_record_list_field()
    total_calories_burned: list[GenericRecord] = _generic_record_list_field()
    vo2_max: list[GenericRecord] = _generic_record_list_field()
    weight: list[GenericRecord] = _generic_record_list_field()
    wheelchair_pushes: list[GenericRecord] = _generic_record_list_field()


class HealthConnectDeletion(HealthConnectWireModel):
    record_type: HealthRecordType
    record_id: str = Field(min_length=1)


class HealthConnectBatchRequest(HealthConnectWireModel):
    contract_version: Literal[1, 2, 3]
    mode: Literal["baseline", "changes"]
    installation_id: str = Field(min_length=1)
    record_types: list[HealthRecordType]
    request_id: str = Field(min_length=1)
    expected_token: str
    next_token: str
    records: HealthConnectRecords
    deletions: list[HealthConnectDeletion] = Field(max_length=10_000)


class AuthoritativeScanRange(HealthConnectWireModel):
    """Exact time range scanned authoritatively by Health Connect."""

    start_time: int
    end_time: int
    seen_record_ids: list[str] | None = Field(default=None, max_length=100_000)


class V1SeenIdsRequiredError(ValueError):
    """A v1 completion omitted its authoritative client ID set."""

    def __init__(self) -> None:
        super().__init__("contract v1 completion requires seen_record_ids")


class BaselineRangesMismatchError(ValueError):
    """Baseline completion ranges do not match the stream's record types."""

    def __init__(self) -> None:
        super().__init__("ranges must match record_types")


class CompleteHealthConnectBaselineRequest(HealthConnectWireModel):
    """Bounded authoritative scan used to reconcile expired-token gaps."""

    contract_version: Literal[1, 2, 3]
    installation_id: str
    record_types: list[HealthRecordType]
    request_id: str
    expected_token: str
    baseline_generation: int = Field(gt=0)
    ranges: dict[HealthRecordType, AuthoritativeScanRange]

    @model_validator(mode="after")
    def ranges_match_record_types(self) -> Self:
        """Reconcile only streams explicitly granted and scanned by Android."""
        record_types = set(canonical_record_types(list(self.record_types)))
        if set(self.ranges) != record_types:
            raise BaselineRangesMismatchError
        if self.contract_version == 1 and any(
            scan.seen_record_ids is None for scan in self.ranges.values()
        ):
            raise V1SeenIdsRequiredError
        return self


class HealthConnectBaselineCompletionRead(HealthConnectWireModel):
    """Safe operational counts from baseline reconciliation."""

    deleted: dict[HealthRecordType, int]
    status: Literal["completed"]


class HealthConnectSyncStateQuery(HealthConnectWireModel):
    installation_id: str
    record_types: str


class StartHealthConnectBaselineRequest(HealthConnectWireModel):
    contract_version: Literal[1, 2, 3]
    installation_id: str
    record_types: list[HealthRecordType]
    request_id: str
    starting_token: str


class HealthConnectSyncStateRead(HealthConnectWireModel):
    baseline_generation: int
    current_token: str | None
    installation_id: str
    record_types: list[HealthRecordType]
    status: RecordStatus


class HealthConnectBatchRead(HealthConnectWireModel):
    accepted: dict[HealthRecordType, int]
    deleted: dict[HealthRecordType, int]
    replayed: bool
    skipped: dict[HealthRecordType, int]
    status: Literal["accepted"]


class HealthConnectContractError(Exception):
    """Malformed stream identity or request reuse."""


class UnsupportedRecordTypesError(HealthConnectContractError):
    """The stream contains an unknown record type."""

    def __init__(self) -> None:
        super().__init__("record_types contains unsupported values")


class DuplicateRecordTypesError(HealthConnectContractError):
    """The stream repeats a record type."""

    def __init__(self) -> None:
        super().__init__("record_types must not contain duplicates")


def parse_record_types(raw: str) -> tuple[HealthRecordType, ...]:
    values = set(raw.split(","))
    if "" in values or not values or not values <= _ALLOWED_RECORD_TYPES:
        raise UnsupportedRecordTypesError
    return cast("tuple[HealthRecordType, ...]", tuple(sorted(values)))


def canonical_record_types(raw: list[str]) -> tuple[HealthRecordType, ...]:
    if len(set(raw)) != len(raw):
        raise DuplicateRecordTypesError
    return parse_record_types(",".join(raw))


def validate_versioned_record_types(
    contract_version: int,
    record_types: tuple[HealthRecordType, ...],
) -> None:
    if (
        contract_version < GENERIC_RECORD_CONTRACT_VERSION
        and not set(record_types) <= _CAPTURED_RECORD_TYPES
    ):
        raise UnsupportedRecordTypesError
