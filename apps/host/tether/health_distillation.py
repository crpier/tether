"""Health consolidation: bounded agent Distillations over episode summaries.

Bespoke sibling of the conversation Dreaming path (ADR 0016): one run
consumes a bounded window of typed episode summaries (Health Connect source
version ids per session type) and may produce exactly one reviewed Memory
document whose Claims cite only summaries inside that window. Mutations flow
through the shared DreamingMutationCoordinator so Review surfaces, retries,
and workspace reconciliation behave identically (ADR 0022).

Evidence URIs are canonical and stable:
`tether://health-connect/<session-type>/<record_uid>@v<version_id>`.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from anyio import NamedTemporaryFile
from anyio import Path as AsyncPath
from snekql.sqlite import Database, Fetched, insert, select, update
from yaml import dump as yaml_dump

from tether.dreaming import (
    DreamingMutationAcknowledger,
    DreamingMutationCoordinator,
    DreamRunExecutionResult,
)
from tether.dreaming_store import HealthDreamRun
from tether.health_connect import (
    HcExerciseEpisodeSummary,
    HcSleepEpisodeSummary,
)
from tether.search_projection.loop import run_reconcile_loop
from tether.structured_logging import Logger

_EXERCISE_URI_PATTERN = r"tether://health-connect/exercise/[^@\s\)]+@v[0-9]+"
_SLEEP_URI_PATTERN = r"tether://health-connect/sleep/[^@\s\)]+@v[0-9]+"
_CLAIM_URI_PATTERN = rf"({_EXERCISE_URI_PATTERN}|{_SLEEP_URI_PATTERN})"


class HealthDreamRunExecutor(Protocol):
    """Callable contract for executing one health dream run to terminal state."""

    async def __call__(
        self, run: HealthDreamRun[Fetched], *, logger: Logger
    ) -> DreamRunExecutionResult: ...


class HealthCurationRunner(Protocol):
    """One unattended model call that curates Claims from summaries."""

    async def run(self, prompt: str) -> str: ...


def _summary_uri(
    kind: Literal["exercise", "sleep"], record_uid: str, version_id: int
) -> str:
    return f"tether://health-connect/{kind}/{record_uid}@v{version_id}"


DEFAULT_MAX_SUMMARIES_PER_RUN = 60
"""Episode summaries one distillation run may consume in its prompt."""

_BACKLOG_CHUNKS_PER_SCAN_TICK = 25
"""Upper bound on queue_run advances per scan tick when draining a backlog."""


@dataclass(frozen=True, slots=True)
class HealthDistillationService:
    """Queue and settle Health consolidation runs over summary windows."""

    database: Database
    telemetry_database: Database
    max_summaries_per_run: int = DEFAULT_MAX_SUMMARIES_PER_RUN

    async def drain_backlog(self) -> list[HealthDreamRun[Fetched]]:
        """Queue successive capped runs until the backlog is windowed.

        Each queued run renders at most `max_summaries_per_run` episode
        summaries per session type into its prompt, so a large uncaptured
        backlog becomes many bounded runs instead of one unbounded pass.
        Returns the runs queued this call.
        """
        drained: list[HealthDreamRun[Fetched]] = []
        while len(drained) < _BACKLOG_CHUNKS_PER_SCAN_TICK:
            run = await self.queue_run()
            if run is None:
                break
            drained.append(run)
        return drained

    async def queue_run(self) -> HealthDreamRun[Fetched] | None:
        """Queue one run when new summaries exist; coalesce otherwise.

        Bounds are captured at queue time so later materialization cannot
        mutate an in-flight or queued window. When no summaries exist, or the
        newest bounds were already captured by a prior run, nothing queues.
        """
        async with self.telemetry_database.transaction() as transaction:
            exercise_row = await transaction.fetch_one(
                select(HcExerciseEpisodeSummary.version_id.max()).all()
            )
            sleep_row = await transaction.fetch_one(
                select(HcSleepEpisodeSummary.version_id.max()).all()
            )
        exercise_through = int(exercise_row) if isinstance(exercise_row, int) else None
        sleep_through = int(sleep_row) if isinstance(sleep_row, int) else None
        if exercise_through is None and sleep_through is None:
            return None
        async with self.database.transaction(mode="immediate") as transaction:
            # Order by captured bounds, not created_at: chunks queued
            # back-to-back share a timestamp at millisecond precision.
            previous = await transaction.fetch_one_or_none(
                select(HealthDreamRun)
                .all()
                .order_by(HealthDreamRun.exercise_through_version_id.desc())
                .order_by(HealthDreamRun.sleep_through_version_id.desc())
                .limit(1)
            )
            if previous is not None and self._covers(
                previous, exercise_through, sleep_through
            ):
                return None
            exercise_since = previous.exercise_through_version_id if previous else 0
            sleep_since = previous.sleep_through_version_id if previous else 0
            cap = self.max_summaries_per_run
            run = HealthDreamRun(
                status="queued",
                exercise_since_version_id=exercise_since,
                exercise_through_version_id=min(
                    exercise_through or 0, exercise_since + cap
                ),
                sleep_since_version_id=sleep_since,
                sleep_through_version_id=min(sleep_through or 0, sleep_since + cap),
            )
            return await transaction.execute(insert(run).returning())

    async def scan_forever(
        self, *, interval_seconds: float = 60.0, logger: Logger
    ) -> None:
        """Queue consolidation runs when new summaries exist, on an interval.

        Correctness backstop for post-sync triggers: a failed pass is logged
        and swallowed; the next tick retries.
        """

        async def _scan_once() -> list[HealthDreamRun[Fetched]]:
            return await self.drain_backlog()

        await run_reconcile_loop(
            _scan_once,
            interval_seconds=interval_seconds,
            initial_delay_seconds=interval_seconds,
            logger=logger,
            failure_message="Health distillation scan failed",
        )

    async def queue_explicit_run(
        self, *, start: datetime, end: datetime
    ) -> HealthDreamRun[Fetched] | None:
        """Queue one manual run bounded to episodes ending inside a period.

        Bounds derive from summary rows whose episode `end_time` falls in
        `[start, end]`; the lower bound excludes everything settled before
        the period. Empty periods and exact repeats of prior bounds queue
        nothing.
        """
        start_ms = int(start.timestamp() * 1_000)
        end_ms = int(end.timestamp() * 1_000)
        async with self.telemetry_database.transaction() as transaction:
            bounds: dict[str, tuple[int | None, int | None]] = {}
            for model, key in (
                (HcExerciseEpisodeSummary, "exercise"),
                (HcSleepEpisodeSummary, "sleep"),
            ):
                through_row = await transaction.fetch_one(
                    select(model.version_id.max()).where(model.end_time.lte(end_ms))
                )
                since_row = await transaction.fetch_one(
                    select(model.version_id.max()).where(model.end_time.lt(start_ms))
                )
                bounds[key] = (
                    int(through_row) if isinstance(through_row, int) else None,
                    int(since_row) if isinstance(since_row, int) else None,
                )
        (ex_through, ex_since), (sl_through, sl_since) = (
            bounds["exercise"],
            bounds["sleep"],
        )
        if ex_through is None and sl_through is None:
            return None
        resolved_ex = (ex_since or 0, ex_through or 0)
        resolved_sl = (sl_since or 0, sl_through or 0)
        if resolved_ex[0] == resolved_ex[1] and resolved_sl[0] == resolved_sl[1]:
            return None
        async with self.database.transaction(mode="immediate") as transaction:
            duplicate = await transaction.fetch_one_or_none(
                select(HealthDreamRun)
                .where(HealthDreamRun.exercise_since_version_id.eq(resolved_ex[0]))
                .where(HealthDreamRun.exercise_through_version_id.eq(resolved_ex[1]))
                .where(HealthDreamRun.sleep_since_version_id.eq(resolved_sl[0]))
                .where(HealthDreamRun.sleep_through_version_id.eq(resolved_sl[1]))
            )
            if duplicate is not None:
                return None
            run = HealthDreamRun(
                status="queued",
                exercise_since_version_id=resolved_ex[0],
                exercise_through_version_id=resolved_ex[1],
                sleep_since_version_id=resolved_sl[0],
                sleep_through_version_id=resolved_sl[1],
            )
            return await transaction.execute(insert(run).returning())

    @staticmethod
    def _covers(
        run: HealthDreamRun[Fetched],
        exercise_through: int | None,
        sleep_through: int | None,
    ) -> bool:
        return run.exercise_through_version_id >= (
            exercise_through or 0
        ) and run.sleep_through_version_id >= (sleep_through or 0)

    async def claim_next_run(self, *, logger: Logger) -> HealthDreamRun[Fetched] | None:
        """Atomically claim one queued run for execution."""
        del logger
        async with self.database.transaction(mode="immediate") as transaction:
            run = await transaction.fetch_one_or_none(
                select(HealthDreamRun)
                .where(HealthDreamRun.status.eq("queued"))
                .order_by(HealthDreamRun.created_at.asc())
                .limit(1)
            )
            if run is None:
                return None
            updated = await transaction.execute(
                update(HealthDreamRun)
                .set(HealthDreamRun.status.to("running"))
                .set(HealthDreamRun.attempts.to(run.attempts + 1))
                .where(HealthDreamRun.id.eq(run.id))
                .returning()
            )
            del updated
            return await transaction.fetch_one_or_none(
                select(HealthDreamRun).where(HealthDreamRun.id.eq(run.id))
            )

    async def complete_run(
        self,
        run_id: UUID,
        *,
        status: Literal["success", "no_op", "failed"],
        logger: Logger,
        error: str | None = None,
    ) -> HealthDreamRun[Fetched]:
        """Settle one run terminally."""
        resolved_now = datetime.now(UTC)
        logger.info(
            "Health dream run completed",
            run_id=str(run_id),
            status=status,
            error=error,
        )
        async with self.database.transaction(mode="immediate") as transaction:
            _ = await transaction.execute(
                update(HealthDreamRun)
                .set(HealthDreamRun.status.to(status))
                .set(HealthDreamRun.error.to(error))
                .set(HealthDreamRun.completed_at.to(resolved_now))
                .set(HealthDreamRun.updated_at.to(resolved_now))
                .where(HealthDreamRun.id.eq(run_id))
            )
            run = await transaction.fetch_one_or_none(
                select(HealthDreamRun).where(HealthDreamRun.id.eq(run_id))
            )
        assert run is not None
        return run


@dataclass(frozen=True, slots=True)
class HealthDistillationExecutor:
    """Apply one bounded summary window to the Memory workspace."""

    telemetry_database: Database
    workspace_root: Path
    mutation_coordinator: DreamingMutationCoordinator
    curation_runner: HealthCurationRunner | None = None
    mutation_acknowledger: DreamingMutationAcknowledger | None = None

    @staticmethod
    def mutation_tool_call_id(run: HealthDreamRun[Fetched]) -> str:
        seed = (
            f"{run.id}:health:"
            f"{run.exercise_since_version_id}:{run.exercise_through_version_id}:"
            f"{run.sleep_since_version_id}:{run.sleep_through_version_id}"
        )
        return str(uuid5(NAMESPACE_URL, seed))

    async def __call__(
        self, run: HealthDreamRun[Fetched], *, logger: Logger
    ) -> DreamRunExecutionResult:
        evidence = await self._fetch_summaries(run)
        if not evidence:
            logger.info(
                "Health dream run had no summary rows; marking no-op",
                run_id=str(run.id),
            )
            return DreamRunExecutionResult(status="no_op")

        tool_call_id = self.mutation_tool_call_id(run)
        # Resume path: a prior execution may have recorded the mutation
        # without a successful ack. Retry is notification-only (ADR-0022).
        settlement = await self.mutation_coordinator.settle(
            run.id,
            tool_call_id,
            acknowledger=self.mutation_acknowledger
            or self.mutation_coordinator.acknowledge_mutation,
        )
        if settlement.outcome in ("settled", "failed"):
            logger.info(
                "Health dream run mutation acknowledged after prior execution",
                run_id=str(run.id),
                acknowledged=settlement.acknowledged,
                error=settlement.error,
            )
            return DreamRunExecutionResult(
                status="success" if settlement.acknowledged else "failed",
                error=settlement.error,
            )

        uris = [uri for _, uri, _ in evidence]
        curated_body = (
            await self.curation_runner.run(self._render_prompt(evidence, run))
            if self.curation_runner is not None
            else None
        )
        if curated_body is None:
            return DreamRunExecutionResult(status="no_op")
        normalized_body = curated_body.strip()
        if normalized_body == "NO_CHANGES":
            return DreamRunExecutionResult(status="no_op")
        if validation_error := self._validate_claims(normalized_body, set(uris)):
            return DreamRunExecutionResult(status="failed", error=validation_error)

        async with self.mutation_coordinator.mutation_scope():
            written = await self._write_document(
                run=run,
                body=normalized_body,
                uris=uris,
            )
            if written is None:
                logger.info(
                    "Health dream document already contains this run payload",
                    run_id=str(run.id),
                )
            _ = await self.mutation_coordinator.record_mutation(
                run_id=run.id,
                tool_call_id=tool_call_id,
                actor="dream",
                operation="write",
                workspace_path=self._document_path(run),
                payload=normalized_body,
            )
            settlement = await self.mutation_coordinator.settle(
                run.id,
                tool_call_id,
                acknowledger=self.mutation_acknowledger
                or self.mutation_coordinator.acknowledge_mutation,
            )
        return DreamRunExecutionResult(
            status="success" if settlement.acknowledged else "failed",
            error=settlement.error,
        )

    async def _fetch_summaries(
        self, run: HealthDreamRun[Fetched]
    ) -> list[tuple[str, str, str]]:
        """Return bounded (label, uri, rendered-block) triples for the window."""
        blocks: list[tuple[str, str, str]] = []
        async with self.telemetry_database.transaction() as transaction:
            if run.exercise_through_version_id > run.exercise_since_version_id:
                rows = list(
                    await transaction.fetch_all(
                        select(HcExerciseEpisodeSummary)
                        .where(
                            HcExerciseEpisodeSummary.version_id.gt(
                                run.exercise_since_version_id
                            )
                        )
                        .where(
                            HcExerciseEpisodeSummary.version_id.lte(
                                run.exercise_through_version_id
                            )
                        )
                    )
                )
                for row in rows:
                    uri = _summary_uri("exercise", row.record_uid, row.version_id)
                    blocks.append(("exercise", uri, self._render_exercise(row)))
            if run.sleep_through_version_id > run.sleep_since_version_id:
                rows = list(
                    await transaction.fetch_all(
                        select(HcSleepEpisodeSummary)
                        .where(
                            HcSleepEpisodeSummary.version_id.gt(
                                run.sleep_since_version_id
                            )
                        )
                        .where(
                            HcSleepEpisodeSummary.version_id.lte(
                                run.sleep_through_version_id
                            )
                        )
                    )
                )
                for row in rows:
                    uri = _summary_uri("sleep", row.record_uid, row.version_id)
                    blocks.append(("sleep", uri, self._render_sleep(row)))
        return blocks

    @staticmethod
    def _render_exercise(row: HcExerciseEpisodeSummary[Fetched]) -> str:
        started = _datetime_from_millis(row.start_time)
        ended = _datetime_from_millis(row.end_time)
        fields = [
            "type: exercise",
            f"record_uid: {row.record_uid}",
            f"version_id: {row.version_id}",
            f"start: {started.isoformat()}",
            f"end: {ended.isoformat()}",
            f"duration_minutes: {row.duration_minutes:g}",
        ]
        if row.title:
            fields.append(f"title: {row.title}")
        if row.segment_count:
            fields.append(f"segment_count: {row.segment_count}")
        if row.lap_count:
            fields.append(f"lap_count: {row.lap_count}")
        if row.total_lap_meters is not None:
            fields.append(f"total_lap_meters: {row.total_lap_meters:g}")
        return "\n".join(fields)

    @staticmethod
    def _render_sleep(row: HcSleepEpisodeSummary[Fetched]) -> str:
        started = _datetime_from_millis(row.start_time)
        ended = _datetime_from_millis(row.end_time)
        stage_values = (
            ("awake", row.minutes_awake),
            ("sleeping", row.minutes_sleeping),
            ("light", row.minutes_light),
            ("deep", row.minutes_deep),
            ("rem", row.minutes_rem),
            ("out_of_bed", row.minutes_out_of_bed),
            ("awake_in_bed", row.minutes_awake_in_bed),
            ("other", row.minutes_other),
        )
        time_asleep = sum(
            value
            for name, value in stage_values
            if name in {"sleeping", "light", "deep", "rem"}
        )
        stage_total = sum(value for _, value in stage_values)
        stage_percentages = ", ".join(
            f"{name}={value / time_asleep * 100:.2f}%"
            for name, value in stage_values
            if name in {"sleeping", "light", "deep", "rem"} and value > 0
        )
        return "\n".join(
            (
                "type: sleep",
                f"record_uid: {row.record_uid}",
                f"version_id: {row.version_id}",
                f"start: {started.isoformat()}",
                f"end: {ended.isoformat()}",
                f"duration_minutes: {row.duration_minutes:g}",
                f"time_asleep_minutes: {time_asleep:g}",
                f"sleep_efficiency_percent: {time_asleep / row.duration_minutes * 100:g}",
                f"stage_coverage_percent: {stage_total / row.duration_minutes * 100:g}",
                f"stage_percent_of_time_asleep: {stage_percentages}",
                *(
                    f"{name}: {value:g} min"
                    for name, value in stage_values
                    if value > 0
                ),
            )
        )

    @staticmethod
    def _render_prompt(
        evidence: list[tuple[str, str, str]], run: HealthDreamRun[Fetched]
    ) -> str:
        blocks = "\n\n".join(
            "\n".join((f"uri: {uri}", block)) for _, uri, block in evidence
        )
        return f"""Distill durable, user-centric Claims from this bounded set of Health Connect episode summaries.

Rules:
- Summaries are computed structure derived from raw telemetry; cite them exactly as given.
- Every Claim is one `- ` bullet with an inline `[source](<summary uri>)` citation.
- Use only exact summary URIs below. Prefer cross-episode patterns over restating one episode.
- A pattern needs at least 3 comparable episodes. State the sample size and relevant data gaps.
- Do not treat separate sleep episodes as fragmentation. They may be naps or split sleep; distinguish them only when the supplied times and duration support it.
- Preserve uncertainty; never invent episodes, values, correlations, or causal explanations.
- Keep computed observations separate from interpretation. Never make clinical conclusions.
- Return Markdown grouped under `##` Topic headings.
- Return `NO_CHANGES` when no durable Claim is supported.

run_id: {run.id}
exercise_since_version_id: {run.exercise_since_version_id}
exercise_through_version_id: {run.exercise_through_version_id}
sleep_since_version_id: {run.sleep_since_version_id}
sleep_through_version_id: {run.sleep_through_version_id}

{blocks}
"""

    @staticmethod
    def _validate_claims(curated_body: str, allowed_uris: set[str]) -> str | None:
        claim_lines = [
            line for line in curated_body.splitlines() if line.startswith("- ")
        ]
        if not claim_lines:
            return "curated body carries no Claims"
        cited_uris = set(re.findall(_CLAIM_URI_PATTERN, curated_body))
        if len(cited_uris) < len(claim_lines):
            return "every curated Claim must cite a bounded summary URI"
        unsupported = sorted(cited_uris - allowed_uris)
        if unsupported:
            return "curated Claim cites outside the bounded window: " + ", ".join(
                unsupported
            )
        return None

    def _document_path(self, run: HealthDreamRun[Fetched]) -> Path:
        return self.workspace_root / "health" / f"{run.id}.md"

    async def _write_document(
        self, *, run: HealthDreamRun[Fetched], body: str, uris: list[str]
    ) -> Path | None:
        """Write frontmatter + body atomically; skip when this run wrote it."""
        document_path = self._document_path(run)
        await AsyncPath(document_path.parent).mkdir(parents=True, exist_ok=True)
        if document_path.exists():
            existing = document_path.read_text(encoding="utf-8")
            if f"run_id: {run.id}" in existing:
                return None
        heading = next(
            (
                line.removeprefix("##").strip()
                for line in body.splitlines()
                if line.startswith("##") and line.removeprefix("##").strip()
            ),
            "Health insights",
        )
        frontmatter = {
            "title": heading,
            "kind": "health_distillation",
            "run_id": str(run.id),
            "evidence": uris,
        }
        content = (
            "---\n"
            + yaml_dump(frontmatter, default_flow_style=False, sort_keys=False)
            + "---\n\n"
            + body
            + "\n"
        )
        async with NamedTemporaryFile(
            mode="w",
            dir=str(document_path.parent),
            delete=False,
        ) as file:
            temp_path = AsyncPath(file.wrapped.name)
            _ = await file.write(content)
        _ = await temp_path.replace(document_path)
        return document_path


@dataclass(frozen=True, slots=True)
class HealthDreamingWorker:
    """Execute queued Health consolidation runs via an injected executor."""

    service: HealthDistillationService
    executor: HealthDreamRunExecutor
    logger: Logger
    poll_interval_seconds: float = 5.0

    async def run_once(self) -> HealthDreamRun[Fetched] | None:
        """Process one queued run terminally; return it, or None when idle."""
        run = await self.service.claim_next_run(logger=self.logger)
        if run is None:
            return None
        try:
            result = await self.executor(run, logger=self.logger)
        except Exception as error:
            self.logger.exception(
                "Health dream executor raised",
                run_id=str(run.id),
                error=str(error),
            )
            result = DreamRunExecutionResult(
                status="failed",
                error=f"{type(error).__name__}: {error}",
            )
        if result.status not in {"success", "no_op", "failed"}:
            result = DreamRunExecutionResult(
                status="failed", error="non-terminal executor result"
            )
        return await self.service.complete_run(
            run.id,
            status=result.status,
            logger=self.logger,
            error=result.error,
        )

    async def run_forever(self) -> None:
        """Continuously claim and complete health runs until cancellation."""
        await asyncio.sleep(self.poll_interval_seconds)
        while True:
            made_progress = False
            while True:
                completed = await self.run_once()
                if completed is None:
                    break
                made_progress = True
            if not made_progress:
                await asyncio.sleep(self.poll_interval_seconds)


def _datetime_from_millis(value: int) -> datetime:
    return datetime.fromtimestamp(value / 1_000, UTC)
