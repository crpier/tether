"""Episode-aware deterministic insights over current Health Connect Telemetry."""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from statistics import median
from typing import Literal

from snekql.sqlite import Database, Fetched, select

from tether.health_connect.contracts import HealthRecordType, parse_record_types
from tether.health_connect.insight_model import (
    HealthConnectDailySleepTrendRead,
    HealthConnectMetricStatusRead,
    HealthConnectSleepBaselineDeltaRead,
    HealthConnectSleepBaselineRead,
    HealthConnectSleepDayRead,
    HealthConnectSleepEpisodeInsightRead,
    HealthConnectSleepEpisodeRead,
    HealthConnectSleepingHeartRateBaselineRead,
    HealthConnectSleepingHeartRateCoverageRead,
    HealthConnectSleepingHeartRateInsightRead,
    HealthConnectSleepingHeartRateObservationRead,
    HealthConnectSleepingHeartRateRead,
    HealthConnectSleepingHeartRateWindowComparisonRead,
    HealthConnectSleepTrendComparisonRead,
    HealthConnectSleepTrendCoverageRead,
    HealthConnectSleepTrendInsightRead,
    HealthConnectSleepTrendWindowRead,
    HealthConnectStageHeartRateRead,
)
from tether.health_connect.inventory import HealthConnectInventoryQuery
from tether.health_connect.persistence import (
    HcHeartRateSampleCurrent,
    HcSleepSessionCurrent,
    HcSleepStageCurrent,
    HealthConnectSyncState,
)
from tether.health_connect.telemetry_values import (
    duration_minutes,
    render_sleep_stage,
    stage_coverage_is_complete,
)

_ASLEEP_STAGE_CODES = frozenset({2, 4, 5, 6})
"""Health Connect stage values counted as time asleep."""

_DAILY_TREND_LIMIT = 31
_NAP_END_HOUR = 20
_NAP_MAX_MINUTES = 240
_NAP_MIN_MINUTES = 30
_NAP_START_HOUR = 8
_PRIMARY_SLEEP_MIN_MINUTES = 180
_PRIMARY_SLEEP_NIGHT_END_HOUR = 8
_PRIMARY_SLEEP_NIGHT_START_HOUR = 18


class HealthConnectIncompleteSleepEpisodeError(Exception):
    """A current sleep projection lacks a required endpoint."""

    def __init__(self) -> None:
        super().__init__("sleep episode is missing a required timestamp")


@dataclass(frozen=True, slots=True)
class HealthConnectInsightQuery:
    """Answer common Health questions without exposing raw telemetry joins."""

    database: Database

    async def fetch_sleep_episode(
        self,
        *,
        days: int,
        episode_kind: Literal["latest", "nap", "primary_sleep"],
    ) -> HealthConnectSleepEpisodeInsightRead:
        """Return the newest matching sleep episode within a bounded period."""
        async with self.database.transaction() as transaction:
            latest_end = await transaction.fetch_one(
                select(HcSleepSessionCurrent.end_time.max()).all()
            )
            if latest_end is None:
                return HealthConnectSleepEpisodeInsightRead(
                    requested_days=days,
                    selected_episode=None,
                    status="no_matching_episode",
                )
            rows = list(
                await transaction.fetch_all(
                    select(HcSleepSessionCurrent)
                    .where(
                        HcSleepSessionCurrent.end_time.gte(
                            latest_end
                            - int(timedelta(days=days).total_seconds() * 1_000)
                        )
                    )
                    .order_by(
                        HcSleepSessionCurrent.end_time.desc(),
                        HcSleepSessionCurrent.start_time.desc(),
                    )
                )
            )
            selected = next(
                (
                    row
                    for row in rows
                    if episode_kind == "latest"
                    or self._classify_sleep(row) == episode_kind
                ),
                None,
            )
            if (
                selected is None
                or selected.start_time is None
                or selected.end_time is None
            ):
                return HealthConnectSleepEpisodeInsightRead(
                    requested_days=days,
                    selected_episode=None,
                    status="no_matching_episode",
                )
            stages = list(
                await transaction.fetch_all(
                    select(HcSleepStageCurrent)
                    .where(
                        HcSleepStageCurrent.version_id.in_(
                            *(row.version_id for row in rows)
                        )
                    )
                    .order_by(
                        HcSleepStageCurrent.version_id.asc(),
                        HcSleepStageCurrent.stage_index.asc(),
                    )
                )
            )
            heart_rate_samples = list(
                await transaction.fetch_all(
                    select(HcHeartRateSampleCurrent)
                    .where(HcHeartRateSampleCurrent.time.gte(selected.start_time))
                    .where(HcHeartRateSampleCurrent.time.lt(selected.end_time))
                    .order_by(HcHeartRateSampleCurrent.time.asc())
                )
            )
        stages_by_version: dict[int, list[HcSleepStageCurrent[Fetched]]] = {}
        for stage in stages:
            stages_by_version.setdefault(stage.version_id, []).append(stage)
        selected_date = self._local_datetime(
            selected.end_time,
            selected.end_zone_offset_seconds or selected.start_zone_offset_seconds,
        ).date()
        episode_reads = [
            self._episode_read(
                row,
                stages=stages_by_version.get(row.version_id, []),
                heart_rate_samples=[],
            )
            for row in rows
        ]
        selected_episode = self._episode_read(
            selected,
            stages=stages_by_version.get(selected.version_id, []),
            heart_rate_samples=heart_rate_samples,
        )
        return HealthConnectSleepEpisodeInsightRead(
            baseline=self._baseline(
                selected_episode,
                episode_reads=episode_reads,
                period_days=days,
            ),
            requested_days=days,
            selected_episode=selected_episode,
            sleep_day=self._sleep_day(
                selected_date.isoformat(),
                [
                    episode
                    for episode in episode_reads
                    if episode.local_end.date() == selected_date
                ],
            ),
            status="available",
        )

    async def fetch_sleep_trend(
        self, *, days: int
    ) -> HealthConnectSleepTrendInsightRead:
        """Return local-day sleep measurements and comparable seven-day windows."""
        async with self.database.transaction() as transaction:
            latest_end = await transaction.fetch_one(
                select(HcSleepSessionCurrent.end_time.max()).all()
            )
            if latest_end is None:
                return HealthConnectSleepTrendInsightRead(
                    comparison=None,
                    coverage=HealthConnectSleepTrendCoverageRead(
                        nap_count=0,
                        primary_sleep_count=0,
                        sleep_day_count=0,
                        stage_complete_episode_count=0,
                        total_episode_count=0,
                    ),
                    daily=[],
                    daily_truncated=False,
                    requested_days=days,
                    status="no_sleep_data",
                )
            rows = list(
                await transaction.fetch_all(
                    select(HcSleepSessionCurrent)
                    .where(
                        HcSleepSessionCurrent.end_time.gte(
                            latest_end
                            - int(timedelta(days=days).total_seconds() * 1_000)
                        )
                    )
                    .order_by(HcSleepSessionCurrent.end_time.asc())
                )
            )
            stages = list(
                await transaction.fetch_all(
                    select(HcSleepStageCurrent)
                    .where(
                        HcSleepStageCurrent.version_id.in_(
                            *(row.version_id for row in rows)
                        )
                    )
                    .order_by(
                        HcSleepStageCurrent.version_id.asc(),
                        HcSleepStageCurrent.stage_index.asc(),
                    )
                )
            )
            stages_by_version: dict[int, list[HcSleepStageCurrent[Fetched]]] = {}
            for stage in stages:
                stages_by_version.setdefault(stage.version_id, []).append(stage)
            episodes: list[HealthConnectSleepEpisodeRead] = []
            for row in rows:
                if row.start_time is None or row.end_time is None:
                    continue
                heart_rate_samples = list(
                    await transaction.fetch_all(
                        select(HcHeartRateSampleCurrent)
                        .where(HcHeartRateSampleCurrent.time.gte(row.start_time))
                        .where(HcHeartRateSampleCurrent.time.lt(row.end_time))
                        .order_by(HcHeartRateSampleCurrent.time.asc())
                    )
                )
                episodes.append(
                    self._episode_read(
                        row,
                        stages=stages_by_version.get(row.version_id, []),
                        heart_rate_samples=heart_rate_samples,
                    )
                )
        episodes_by_date: dict[date, list[HealthConnectSleepEpisodeRead]] = {}
        for episode in episodes:
            episodes_by_date.setdefault(episode.local_end.date(), []).append(episode)
        daily: list[HealthConnectDailySleepTrendRead] = []
        for local_date, date_episodes in sorted(episodes_by_date.items()):
            sleep_day = self._sleep_day(local_date.isoformat(), date_episodes)
            primary_sleeps = [
                episode
                for episode in date_episodes
                if episode.classification == "primary_sleep"
            ]
            primary_sleep = (
                max(primary_sleeps, key=lambda episode: episode.time_in_bed_minutes)
                if primary_sleeps
                else None
            )
            daily.append(
                HealthConnectDailySleepTrendRead(
                    date=local_date.isoformat(),
                    evidence_uris=sleep_day.evidence_uris,
                    nap_count=sleep_day.nap_count,
                    primary_sleep_count=sleep_day.primary_sleep_count,
                    primary_sleep_efficiency_percent=(
                        primary_sleep.sleep_efficiency_percent
                        if primary_sleep is not None
                        else None
                    ),
                    primary_sleep_evidence_uri=(
                        primary_sleep.evidence_uri
                        if primary_sleep is not None
                        else None
                    ),
                    primary_sleep_local_end=(
                        primary_sleep.local_end if primary_sleep is not None else None
                    ),
                    primary_sleep_local_start=(
                        primary_sleep.local_start if primary_sleep is not None else None
                    ),
                    primary_sleeping_heart_rate_bpm=(
                        primary_sleep.sleeping_heart_rate.average_bpm
                        if primary_sleep is not None
                        and primary_sleep.sleeping_heart_rate is not None
                        else None
                    ),
                    primary_sleeping_heart_rate_sample_count=(
                        primary_sleep.sleeping_heart_rate.sample_count
                        if primary_sleep is not None
                        and primary_sleep.sleeping_heart_rate is not None
                        else None
                    ),
                    stage_minutes=sleep_day.stage_minutes,
                    time_asleep_minutes=sleep_day.time_asleep_minutes,
                    time_in_bed_minutes=sleep_day.time_in_bed_minutes,
                )
            )
        latest_date = max(episodes_by_date)
        current_start = latest_date - timedelta(days=6)
        previous_start = latest_date - timedelta(days=13)
        previous_end = latest_date - timedelta(days=7)
        primary_episodes = [
            episode for episode in episodes if episode.classification == "primary_sleep"
        ]
        return HealthConnectSleepTrendInsightRead(
            comparison=HealthConnectSleepTrendComparisonRead(
                current_7_days=self._trend_window(
                    primary_episodes,
                    start_date=current_start,
                    end_date=latest_date,
                ),
                previous_7_days=self._trend_window(
                    primary_episodes,
                    start_date=previous_start,
                    end_date=previous_end,
                ),
            ),
            coverage=HealthConnectSleepTrendCoverageRead(
                nap_count=sum(episode.classification == "nap" for episode in episodes),
                primary_sleep_count=len(primary_episodes),
                sleep_day_count=len(episodes_by_date),
                stage_complete_episode_count=sum(
                    episode.stages_complete for episode in episodes
                ),
                total_episode_count=len(episodes),
            ),
            daily=daily[-_DAILY_TREND_LIMIT:],
            daily_truncated=len(daily) > _DAILY_TREND_LIMIT,
            requested_days=days,
            status="available",
        )

    @staticmethod
    def _trend_window(
        episodes: list[HealthConnectSleepEpisodeRead],
        *,
        start_date: date,
        end_date: date,
    ) -> HealthConnectSleepTrendWindowRead:
        """Average comparable primary sleeps while keeping sample size visible."""
        selected = [
            episode
            for episode in episodes
            if start_date <= episode.local_end.date() <= end_date
        ]
        efficiencies = [
            episode.sleep_efficiency_percent
            for episode in selected
            if episode.sleep_efficiency_percent is not None
        ]
        sleeping_heart_rates = [
            episode.sleeping_heart_rate.average_bpm
            for episode in selected
            if episode.sleeping_heart_rate is not None
        ]
        stage_labels = sorted(
            {
                label
                for episode in selected
                for label in episode.stage_percent_of_time_asleep
            }
        )
        return HealthConnectSleepTrendWindowRead(
            average_sleep_efficiency_percent=(
                round(sum(efficiencies) / len(efficiencies), 2)
                if efficiencies
                else None
            ),
            average_sleeping_heart_rate_bpm=(
                round(sum(sleeping_heart_rates) / len(sleeping_heart_rates), 2)
                if sleeping_heart_rates
                else None
            ),
            average_stage_percent_of_time_asleep={
                label: round(
                    sum(
                        episode.stage_percent_of_time_asleep[label]
                        for episode in selected
                        if label in episode.stage_percent_of_time_asleep
                    )
                    / sum(
                        label in episode.stage_percent_of_time_asleep
                        for episode in selected
                    ),
                    2,
                )
                for label in stage_labels
            },
            average_time_asleep_minutes=(
                round(
                    sum(episode.time_asleep_minutes for episode in selected)
                    / len(selected),
                    2,
                )
                if selected
                else None
            ),
            end_date=end_date.isoformat(),
            primary_sleep_count=len(selected),
            start_date=start_date.isoformat(),
        )

    async def fetch_sleeping_heart_rate(
        self, *, days: int
    ) -> HealthConnectSleepingHeartRateInsightRead:
        """Return primary-sleep heart rate against prior personal observations."""
        trend = await self.fetch_sleep_trend(days=days)
        measured: list[tuple[str, float, str, int]] = []
        for day in trend.daily:
            average_bpm = day.primary_sleeping_heart_rate_bpm
            sample_count = day.primary_sleeping_heart_rate_sample_count
            evidence_uri = day.primary_sleep_evidence_uri
            if (
                average_bpm is not None
                and sample_count is not None
                and evidence_uri is not None
            ):
                measured.append((day.date, average_bpm, evidence_uri, sample_count))
        baseline_values = [average_bpm for _, average_bpm, _, _ in measured[:-1]]
        baseline_bpm = median(baseline_values) if baseline_values else None
        latest_bpm = measured[-1][1] if measured else None
        current_average = (
            trend.comparison.current_7_days.average_sleeping_heart_rate_bpm
            if trend.comparison is not None
            else None
        )
        previous_average = (
            trend.comparison.previous_7_days.average_sleeping_heart_rate_bpm
            if trend.comparison is not None
            else None
        )
        return HealthConnectSleepingHeartRateInsightRead(
            baseline=HealthConnectSleepingHeartRateBaselineRead(
                comparison_episode_count=len(baseline_values),
                latest_difference_bpm=(
                    round(latest_bpm - baseline_bpm, 2)
                    if latest_bpm is not None and baseline_bpm is not None
                    else None
                ),
                median_bpm=(
                    round(baseline_bpm, 2) if baseline_bpm is not None else None
                ),
            ),
            coverage=HealthConnectSleepingHeartRateCoverageRead(
                primary_sleep_count=sum(day.primary_sleep_count for day in trend.daily),
                with_heart_rate_count=len(measured),
            ),
            observations=[
                HealthConnectSleepingHeartRateObservationRead(
                    average_bpm=average_bpm,
                    date=local_date,
                    difference_from_baseline_bpm=(
                        round(average_bpm - baseline_bpm, 2)
                        if baseline_bpm is not None
                        else None
                    ),
                    evidence_uri=evidence_uri,
                    sample_count=sample_count,
                )
                for local_date, average_bpm, evidence_uri, sample_count in measured
            ],
            requested_days=days,
            status="available" if measured else "no_sleeping_heart_rate",
            window_comparison=HealthConnectSleepingHeartRateWindowComparisonRead(
                current_7_days_average_bpm=current_average,
                current_7_days_episode_count=(
                    trend.comparison.current_7_days.primary_sleep_count
                    if trend.comparison is not None
                    else 0
                ),
                difference_bpm=(
                    round(current_average - previous_average, 2)
                    if current_average is not None and previous_average is not None
                    else None
                ),
                previous_7_days_average_bpm=previous_average,
                previous_7_days_episode_count=(
                    trend.comparison.previous_7_days.primary_sleep_count
                    if trend.comparison is not None
                    else 0
                ),
            ),
        )

    async def fetch_metric_status(
        self, *, record_type: HealthRecordType
    ) -> HealthConnectMetricStatusRead:
        """Explain whether one supported metric has synchronized current records."""
        inventory = await HealthConnectInventoryQuery(self.database).fetch_inventory()
        record_count = next(
            (
                entry.record_count
                for entry in inventory
                if entry.record_type == record_type
            ),
            0,
        )
        async with self.database.transaction() as transaction:
            sync_states = await transaction.fetch_all(
                select(HealthConnectSyncState).all()
            )
        sync_configured = any(
            record_type in parse_record_types(sync_state.record_type_set)
            for sync_state in sync_states
        )
        metric_name = record_type.replace("_", " ").capitalize()
        status: Literal["available", "not_synchronized", "synchronized_no_records"]
        if record_count > 0:
            status = "available"
            explanation = (
                f"{metric_name} synchronization has {record_count} current "
                "Health Connect record"
                f"{'s' if record_count != 1 else ''}."
            )
        elif sync_configured:
            status = "synchronized_no_records"
            explanation = (
                f"{metric_name} synchronization is configured, but Health Connect "
                "has provided no current records. The source may have no measurements."
            )
        else:
            status = "not_synchronized"
            explanation = (
                f"{metric_name} is supported, but it is not in a configured Health "
                "Connect sync. Permission and source availability are unknown."
            )
        return HealthConnectMetricStatusRead(
            explanation=explanation,
            record_count=record_count,
            record_type=record_type,
            status=status,
            sync_configured=sync_configured,
        )

    def _classify_sleep(
        self, row: HcSleepSessionCurrent[Fetched]
    ) -> Literal["nap", "other", "primary_sleep"]:
        """Separate daytime naps from likely primary sleeps without inference."""
        time_in_bed = duration_minutes(row.start_time, row.end_time)
        local_start = self._local_datetime(
            row.start_time, row.start_zone_offset_seconds
        )
        if (
            _NAP_MIN_MINUTES <= time_in_bed <= _NAP_MAX_MINUTES
            and _NAP_START_HOUR <= local_start.hour < _NAP_END_HOUR
        ):
            return "nap"
        if time_in_bed >= _PRIMARY_SLEEP_MIN_MINUTES and (
            local_start.hour < _PRIMARY_SLEEP_NIGHT_END_HOUR
            or local_start.hour >= _PRIMARY_SLEEP_NIGHT_START_HOUR
        ):
            return "primary_sleep"
        return "other"

    @staticmethod
    def _baseline(
        selected: HealthConnectSleepEpisodeRead,
        *,
        episode_reads: list[HealthConnectSleepEpisodeRead],
        period_days: int,
    ) -> HealthConnectSleepBaselineRead:
        """Compare an episode only with prior episodes of the same kind."""
        comparison = [
            episode
            for episode in episode_reads
            if episode.evidence_uri != selected.evidence_uri
            and episode.classification == selected.classification
            and episode.local_end <= selected.local_start
        ]
        if not comparison:
            return HealthConnectSleepBaselineRead(
                classification=selected.classification,
                comparison_episode_count=0,
                median_sleep_efficiency_percent=None,
                median_stage_percent_of_time_asleep={},
                median_time_asleep_minutes=None,
                median_time_in_bed_minutes=None,
                period_days=period_days,
                selected_delta=None,
            )
        median_efficiency = median(
            episode.sleep_efficiency_percent
            for episode in comparison
            if episode.sleep_efficiency_percent is not None
        )
        median_stage_percent = {
            label: round(
                median(
                    episode.stage_percent_of_time_asleep[label]
                    for episode in comparison
                    if label in episode.stage_percent_of_time_asleep
                ),
                2,
            )
            for label in sorted(
                {
                    label
                    for episode in comparison
                    for label in episode.stage_percent_of_time_asleep
                }
            )
        }
        median_time_asleep = median(
            episode.time_asleep_minutes for episode in comparison
        )
        median_time_in_bed = median(
            episode.time_in_bed_minutes for episode in comparison
        )
        return HealthConnectSleepBaselineRead(
            classification=selected.classification,
            comparison_episode_count=len(comparison),
            median_sleep_efficiency_percent=round(median_efficiency, 2),
            median_stage_percent_of_time_asleep=median_stage_percent,
            median_time_asleep_minutes=round(median_time_asleep, 2),
            median_time_in_bed_minutes=round(median_time_in_bed, 2),
            period_days=period_days,
            selected_delta=HealthConnectSleepBaselineDeltaRead(
                sleep_efficiency_percentage_points=round(
                    (selected.sleep_efficiency_percent or 0) - median_efficiency, 2
                ),
                stage_percentage_points={
                    label: round(
                        selected.stage_percent_of_time_asleep.get(label, 0)
                        - baseline_percent,
                        2,
                    )
                    for label, baseline_percent in median_stage_percent.items()
                },
                time_asleep_minutes=round(
                    selected.time_asleep_minutes - median_time_asleep, 2
                ),
                time_in_bed_minutes=round(
                    selected.time_in_bed_minutes - median_time_in_bed, 2
                ),
            ),
        )

    @staticmethod
    def _sleep_day(
        local_date: str, episodes: list[HealthConnectSleepEpisodeRead]
    ) -> HealthConnectSleepDayRead:
        """Combine primary sleep and naps without treating either as fragmentation."""
        stage_minutes: dict[str, float] = {}
        for episode in episodes:
            for label, minutes in episode.stage_minutes.items():
                stage_minutes[label] = stage_minutes.get(label, 0.0) + minutes
        ordered_episodes = sorted(episodes, key=lambda episode: episode.local_start)
        return HealthConnectSleepDayRead(
            date=local_date,
            episode_count=len(episodes),
            evidence_uris=[episode.evidence_uri for episode in ordered_episodes],
            nap_count=sum(episode.classification == "nap" for episode in episodes),
            primary_sleep_count=sum(
                episode.classification == "primary_sleep" for episode in episodes
            ),
            stage_minutes={
                label: round(minutes, 2)
                for label, minutes in sorted(stage_minutes.items())
            },
            time_asleep_minutes=round(
                sum(episode.time_asleep_minutes for episode in episodes), 2
            ),
            time_in_bed_minutes=round(
                sum(episode.time_in_bed_minutes for episode in episodes), 2
            ),
        )

    def _episode_read(
        self,
        row: HcSleepSessionCurrent[Fetched],
        *,
        stages: list[HcSleepStageCurrent[Fetched]],
        heart_rate_samples: list[HcHeartRateSampleCurrent[Fetched]],
    ) -> HealthConnectSleepEpisodeRead:
        """Collapse one episode's stages and aligned samples into stable metrics."""
        stage_minutes: dict[str, float] = {}
        time_asleep_minutes = 0.0
        for stage in stages:
            minutes = duration_minutes(stage.start_time, stage.end_time)
            label = render_sleep_stage(stage.stage)
            stage_minutes[label] = stage_minutes.get(label, 0.0) + minutes
            if stage.stage in _ASLEEP_STAGE_CODES:
                time_asleep_minutes += minutes
        time_in_bed_minutes = duration_minutes(row.start_time, row.end_time)
        covered_minutes = sum(stage_minutes.values())
        sleeping_heart_rate = None
        if heart_rate_samples:
            beats = [sample.beats_per_minute for sample in heart_rate_samples]
            sample_times = [sample.time for sample in heart_rate_samples]
            beats_by_stage: dict[str, list[int]] = {}
            for stage in stages:
                stage_beats = [
                    sample.beats_per_minute
                    for sample in heart_rate_samples[
                        bisect_left(sample_times, stage.start_time) : bisect_left(
                            sample_times, stage.end_time
                        )
                    ]
                ]
                if stage_beats:
                    beats_by_stage.setdefault(
                        render_sleep_stage(stage.stage), []
                    ).extend(stage_beats)
            sleeping_heart_rate = HealthConnectSleepingHeartRateRead(
                average_bpm=round(sum(beats) / len(beats), 2),
                by_stage={
                    label: HealthConnectStageHeartRateRead(
                        average_bpm=round(sum(stage_beats) / len(stage_beats), 2),
                        maximum_bpm=max(stage_beats),
                        minimum_bpm=min(stage_beats),
                        sample_count=len(stage_beats),
                    )
                    for label, stage_beats in sorted(beats_by_stage.items())
                },
                maximum_bpm=max(beats),
                minimum_bpm=min(beats),
                sample_count=len(beats),
            )
        stage_percent_of_time_asleep = {
            label: round(minutes / time_asleep_minutes * 100, 2)
            for label, minutes in sorted(stage_minutes.items())
            if label in {"deep", "light", "rem", "sleeping"} and time_asleep_minutes > 0
        }
        stage_coverage_percent = (
            round(covered_minutes / time_in_bed_minutes * 100, 2)
            if time_in_bed_minutes > 0
            else 0.0
        )
        return HealthConnectSleepEpisodeRead(
            classification=self._classify_sleep(row),
            evidence_uri=(
                f"tether://health-connect/sleep/{row.record_uid}@v{row.version_id}"
            ),
            local_end=self._local_datetime(
                row.end_time,
                row.end_zone_offset_seconds or row.start_zone_offset_seconds,
            ),
            local_start=self._local_datetime(
                row.start_time, row.start_zone_offset_seconds
            ),
            record_id=row.record_uid,
            sleep_efficiency_percent=(
                round(time_asleep_minutes / time_in_bed_minutes * 100, 2)
                if time_in_bed_minutes > 0
                else None
            ),
            sleeping_heart_rate=sleeping_heart_rate,
            source_version=row.version_id,
            stage_coverage_percent=stage_coverage_percent,
            stage_interval_count=len(stages),
            stage_minutes={
                label: round(minutes, 2)
                for label, minutes in sorted(stage_minutes.items())
            },
            stage_percent_of_time_asleep=stage_percent_of_time_asleep,
            stages_complete=stage_coverage_is_complete(stage_coverage_percent),
            time_asleep_minutes=round(time_asleep_minutes, 2),
            time_in_bed_minutes=round(time_in_bed_minutes, 2),
        )

    @staticmethod
    def _local_datetime(
        epoch_millis: int | None, zone_offset_seconds: int | None
    ) -> datetime:
        """Render the record's fixed captured offset instead of silently using UTC."""
        if epoch_millis is None:
            raise HealthConnectIncompleteSleepEpisodeError
        return datetime.fromtimestamp(
            epoch_millis / 1_000,
            timezone(timedelta(seconds=zone_offset_seconds or 0)),
        )
