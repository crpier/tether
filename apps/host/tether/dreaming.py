"""Dreaming orchestration primitives and cursor math (host-owned conversations)."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import re
from collections.abc import AsyncGenerator, Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Protocol, cast
from uuid import NAMESPACE_URL, UUID, uuid5

import httpx2
from anyio import NamedTemporaryFile
from anyio import Path as AsyncPath
from opentelemetry.trace import Tracer
from pydantic import UUID7, PositiveInt
from snekql.sqlite import (
    CurrentTimestamp,
    Database,
    Fetched,
    Transaction,
    delete,
    insert,
    select,
    update,
)
from yaml import YAMLError, safe_load
from yaml import dump as yaml_dump

from tether.conversation_evidence import fetch_claim_supporting_message_ids
from tether.conversation_store import Conversation, ConversationTurn, Message
from tether.conversations import ConversationService
from tether.dreaming_store import (
    DreamConversationCursor,
    DreamingMutation,
    DreamingMutationActor,
    DreamingMutationOperation,
    DreamingWorkspaceFile,
    DreamMaintenanceProgress,
    DreamRun,
    DreamRunKind,
    DreamRunTerminalStatus,
)
from tether.memory_workspace import MemoryWorkspace, MemoryWorkspaceTopic
from tether.search_projection.loop import run_reconcile_loop
from tether.structured_logging import Logger
from tether.tool_runtime import TOOL_AUTH_HEADER

_DREAM_SETTLE_WINDOW = timedelta(minutes=20)
_MAINTENANCE_INTERVAL = timedelta(hours=24)
"""Delay before an auto run can consume new user-level evidence."""
_DREAM_MAX_MESSAGES = 200
"""Default max transcript rows per Dream run."""
_FRONTMATTER_SEPARATOR = "\n---\n"
_FRONTMATTER_PART_COUNT = 2
_SECOND_PERSON_CLAIM_PATTERN = re.compile(r"^- (?:you|your)\b", re.IGNORECASE)

_ACK_PATH = "/internal/dream-runs/{run_id}/mutations/{tool_call_id}/ack"
"""Dream mutation callback route on the same host."""


def _memory_claim_voice_error(documents: Iterable[str]) -> str | None:
    """Require every user-facing Claim to begin in second person."""
    for document in documents:
        body = document
        if document.startswith("---\n"):
            parts = document.split(_FRONTMATTER_SEPARATOR, 1)
            if len(parts) == _FRONTMATTER_PART_COUNT:
                body = parts[1]
        if any(
            not _SECOND_PERSON_CLAIM_PATTERN.search(line)
            for line in body.splitlines()
            if line.startswith("- ")
        ):
            return "Memory Claims must address the user as you or your"
    return None


@dataclass(frozen=True, slots=True)
class DreamingWorkspaceReconcileResult:
    """Counts produced by one reconciliation pass."""

    updated_files: int
    tombstones: int


class DreamRunNotFoundError(Exception):
    """Raised when a run cannot be resolved for completion."""


class ConversationMemoryRebuildBusyError(Exception):
    """Raised when active Dreaming work prevents a Memory rebuild."""


class ConversationMemoryRebuildError(Exception):
    """Raised when Memory rebuild preparation cannot complete safely."""


@dataclass(frozen=True, slots=True)
class ConversationMemoryRebuildResult:
    """Recorded preparation outcome for one Conversation Memory rebuild."""

    preserved_topics: int
    queued_runs: int
    rebuild_run_id: UUID7
    reset_cursors: int
    tombstoned_topics: int


@dataclass(frozen=True, slots=True)
class _AssimilationWindow:
    start_seq: int
    end_seq: int


def _as_utc(value: datetime) -> datetime:
    """Read legacy-aware timestamps as UTC-aware datetimes."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _utc_now() -> datetime:
    """Return the current UTC time for production maintenance decisions."""
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class MutationSettlement:
    """Terminal outcome of one mutation settlement attempt."""

    outcome: Literal["settled", "already_settled", "not_found", "failed"]
    error: str | None = None

    @property
    def acknowledged(self) -> bool:
        """Whether the mutation is (now) acknowledged."""
        return self.outcome in ("settled", "already_settled")


class DreamingMutationCoordinator:
    """Coordinate persisted mutation attempts and workspace reconciliation."""

    def __init__(self, database: Database, workspace_root: Path) -> None:
        self.database: Database = database
        self.workspace_root: Path = workspace_root
        self._workspace_lock: asyncio.Lock = asyncio.Lock()

    @contextlib.asynccontextmanager
    async def mutation_scope(self) -> AsyncGenerator[None]:
        """Serialize one Dream mutation against workspace reconciliation."""
        async with self._workspace_lock:
            yield

    @staticmethod
    def _content_hash(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    @staticmethod
    def _to_relative_path(workspace_root: Path, raw_path: Path) -> str:
        root = workspace_root.resolve()
        try:
            return str(raw_path.resolve().relative_to(root))
        except ValueError:
            return str(raw_path)

    @staticmethod
    def mutation_tool_call_id(run: DreamRun[Fetched]) -> str:
        seed = f"{run.id}:{run.kind}:{run.evidence_start_seq}:{run.evidence_end_seq}"
        return str(uuid5(NAMESPACE_URL, seed))

    async def record_mutation(  # noqa: PLR0913
        self,
        *,
        run_id: UUID,
        tool_call_id: str,
        actor: DreamingMutationActor,
        operation: DreamingMutationOperation,
        workspace_path: Path,
        payload: str,
    ) -> DreamingMutation[Fetched] | None:
        """Persist (or refresh) a concrete mutation attempt."""
        relative_path = self._to_relative_path(self.workspace_root, workspace_path)
        async_path = AsyncPath(workspace_path)
        try:
            after_content = (
                await async_path.read_text(encoding="utf-8")
                if await async_path.exists()
                else None
            )
        except UnicodeDecodeError:
            after_content = None
        async with self.database.transaction() as tx:
            current_file = await tx.fetch_one_or_none(
                select(DreamingWorkspaceFile).where(
                    DreamingWorkspaceFile.path.eq(relative_path)
                )
            )
            before_content = (
                current_file.content
                if current_file is not None and current_file.is_tombstone == 0
                else None
            )
            existing = await tx.fetch_one_or_none(
                select(DreamingMutation)
                .where(DreamingMutation.run_id.eq(run_id))
                .where(DreamingMutation.tool_call_id.eq(tool_call_id))
            )
            if existing is not None:
                _ = await tx.execute(
                    update(DreamingMutation)
                    .set(DreamingMutation.actor.to(actor))
                    .set(DreamingMutation.operation.to(operation))
                    .set(DreamingMutation.workspace_path.to(relative_path))
                    .set(DreamingMutation.payload.to(payload))
                    .set(DreamingMutation.after_content.to(after_content))
                    .set(DreamingMutation.status.to("executed"))
                    .set(DreamingMutation.attempts.to(existing.attempts + 1))
                    .set(DreamingMutation.error.to(None))
                    .where(DreamingMutation.id.eq(existing.id))
                )
                return existing
            _ = await tx.execute(
                insert(
                    DreamingMutation(
                        run_id=run_id,
                        tool_call_id=tool_call_id,
                        actor=actor,
                        operation=operation,
                        workspace_path=relative_path,
                        payload=payload,
                        before_content=before_content,
                        after_content=after_content,
                        status="executed",
                    )
                )
            )
            return await tx.fetch_one_or_none(
                select(DreamingMutation)
                .where(DreamingMutation.run_id.eq(run_id))
                .where(DreamingMutation.tool_call_id.eq(tool_call_id))
            )

    async def acknowledge_mutation(
        self,
        run_id: UUID,
        tool_call_id: str,
    ) -> tuple[bool, str | None]:
        """Mark this mutation as acknowledged after reconciling its file path."""
        async with self.database.transaction(mode="immediate") as tx:
            mutation = await tx.fetch_one_or_none(
                select(DreamingMutation)
                .where(DreamingMutation.run_id.eq(run_id))
                .where(DreamingMutation.tool_call_id.eq(tool_call_id))
            )
            if mutation is None:
                return False, "mutation not found"
            if mutation.status == "acknowledged":
                return True, None
            workspace_path = self.workspace_root / mutation.workspace_path
            try:
                await self._reconcile_file(
                    tx,
                    workspace_path=workspace_path,
                    source_run_id=run_id,
                    source_tool_call_id=tool_call_id,
                    actor=mutation.actor,
                )
                _ = await tx.execute(
                    update(DreamingMutation)
                    .set(DreamingMutation.status.to("acknowledged"))
                    .set(DreamingMutation.attempts.to(mutation.attempts + 1))
                    .set(DreamingMutation.error.to(None))
                    .where(DreamingMutation.id.eq(mutation.id))
                )
            except Exception as error:
                _ = await tx.execute(
                    update(DreamingMutation)
                    .set(DreamingMutation.status.to("failed"))
                    .set(DreamingMutation.attempts.to(mutation.attempts + 1))
                    .set(DreamingMutation.error.to(f"{type(error).__name__}: {error}"))
                    .where(DreamingMutation.id.eq(mutation.id))
                )
                return False, f"{type(error).__name__}: {error}"
            return True, None

    async def settle(
        self,
        run_id: UUID,
        tool_call_id: str,
        *,
        acknowledger: DreamingMutationAcknowledger | None = None,
    ) -> MutationSettlement:
        """Drive one recorded mutation to acknowledged: the one ack policy.

        Single home of the ADR-0022 lifecycle: pending lookup, the
        retry-only-notification rule, idempotent recording, and
        reconciliation repair all live here. Callers classify the returned
        outcome; none of them re-implements the policy.

        The notifier defaults to in-process acknowledgement (reconcile plus
        record). Remote callers — the executor resuming after a crash —
        inject a notification-only acknowledger (HTTP in production) so a
        retry never repeats the filesystem operation.
        """
        notify = acknowledger if acknowledger is not None else self.acknowledge_mutation
        async with self.database.transaction() as tx:
            mutation = await tx.fetch_one_or_none(
                select(DreamingMutation)
                .where(DreamingMutation.run_id.eq(run_id))
                .where(DreamingMutation.tool_call_id.eq(tool_call_id))
            )
        if mutation is None:
            return MutationSettlement("not_found")
        if mutation.status == "acknowledged":
            return MutationSettlement("already_settled")
        acknowledged, error = await notify(run_id, tool_call_id)
        if not acknowledged:
            # Leave the record untouched: it stays retryable.
            return MutationSettlement("failed", error)
        return MutationSettlement("settled")

    async def reconcile_workspace(
        self,
        *,
        logger: Logger,
    ) -> DreamingWorkspaceReconcileResult:
        """Repair workspace drift while preserving completed Dreaming mutations."""
        async with self._workspace_lock:
            return await self._reconcile_workspace(logger=logger)

    async def _reconcile_workspace(
        self,
        *,
        logger: Logger,
    ) -> DreamingWorkspaceReconcileResult:
        """Perform one serialized repair pass."""
        scan = await MemoryWorkspace(self.workspace_root).scan()
        workspace_topics: dict[str, str] = {}
        for topic in scan.topics:
            workspace_topics[
                self._to_relative_path(self.workspace_root, topic.path)
            ] = await AsyncPath(topic.path).read_text(encoding="utf-8")

        async with self.database.transaction(mode="immediate") as transaction:
            recorded_files = await transaction.fetch_all(
                select(DreamingWorkspaceFile).where(
                    DreamingWorkspaceFile.path.is_not_null()
                )
            )
            pending_mutations = [
                *await transaction.fetch_all(
                    select(DreamingMutation).where(
                        DreamingMutation.status.eq("executed")
                    )
                ),
                *await transaction.fetch_all(
                    select(DreamingMutation).where(DreamingMutation.status.eq("failed"))
                ),
            ]
            pending_by_path: dict[str, list[DreamingMutation[Fetched]]] = {}
            for mutation in pending_mutations:
                if mutation.actor == "dream":
                    pending_by_path.setdefault(mutation.workspace_path, []).append(
                        mutation
                    )

            recorded_by_path = {file.path: file for file in recorded_files}
            updated_files = 0
            for relative_path, content in workspace_topics.items():
                if await self._reconcile_present_file(
                    transaction,
                    relative_path=relative_path,
                    content=content,
                    recorded_file=recorded_by_path.get(relative_path),
                    pending_mutations=pending_by_path.get(relative_path, []),
                ):
                    updated_files += 1

            tombstones = 0
            for relative_path, recorded_file in recorded_by_path.items():
                if relative_path in workspace_topics or recorded_file.is_tombstone == 1:
                    continue
                outcome = await self._reconcile_missing_file(
                    transaction,
                    relative_path=relative_path,
                    recorded_file=recorded_file,
                    pending_mutations=pending_by_path.get(relative_path, []),
                )
                updated_files += int(outcome == "restored")
                tombstones += int(outcome == "tombstoned")

        logger.debug(
            "Reconciled dreaming workspace",
            updated=updated_files,
            tombstones=tombstones,
        )
        return DreamingWorkspaceReconcileResult(
            updated_files=updated_files,
            tombstones=tombstones,
        )

    async def _reconcile_present_file(
        self,
        transaction: Transaction,
        *,
        relative_path: str,
        content: str,
        recorded_file: DreamingWorkspaceFile[Fetched] | None,
        pending_mutations: list[DreamingMutation[Fetched]],
    ) -> bool:
        """Accept a pending Dream write or repair an unauthorized file."""
        recorded_content = (
            recorded_file.content
            if recorded_file is not None and recorded_file.is_tombstone == 0
            else None
        )
        pending_write = next(
            (
                mutation
                for mutation in pending_mutations
                if mutation.after_content == content
                and mutation.before_content == recorded_content
            ),
            None,
        )
        content_hash = self._content_hash(content)
        if pending_write is not None:
            await self._upsert_file_state(
                transaction,
                workspace_path=relative_path,
                content_hash=content_hash,
                content=content,
                is_tombstone=False,
                source_run_id=pending_write.run_id,
                source_tool_call_id=pending_write.tool_call_id,
                mutation_actor="dream",
            )
            return (
                recorded_file is None
                or recorded_file.is_tombstone == 1
                or recorded_file.content_hash != content_hash
            )
        if recorded_file is None or recorded_file.is_tombstone == 1:
            await AsyncPath(self.workspace_root / relative_path).unlink()
            return True
        if (
            recorded_file.content is not None
            and recorded_file.content_hash != content_hash
        ):
            await self._restore_file(relative_path, recorded_file.content)
            return True
        return False

    async def _reconcile_missing_file(
        self,
        transaction: Transaction,
        *,
        relative_path: str,
        recorded_file: DreamingWorkspaceFile[Fetched],
        pending_mutations: list[DreamingMutation[Fetched]],
    ) -> Literal["restored", "tombstoned", "unchanged"]:
        """Accept a pending Dream deletion or restore unauthorized removal."""
        pending_deletion = next(
            (
                mutation
                for mutation in pending_mutations
                if mutation.operation == "delete"
                and mutation.after_content is None
                and mutation.before_content == recorded_file.content
            ),
            None,
        )
        if pending_deletion is not None:
            await self._upsert_file_state(
                transaction,
                workspace_path=relative_path,
                content_hash="",
                content=None,
                is_tombstone=True,
                source_run_id=pending_deletion.run_id,
                source_tool_call_id=pending_deletion.tool_call_id,
                mutation_actor="dream",
            )
            return "tombstoned"
        if recorded_file.content is not None:
            await self._restore_file(relative_path, recorded_file.content)
            return "restored"
        return "unchanged"

    async def _restore_file(self, relative_path: str, content: str) -> None:
        """Atomically replace unauthorized workspace drift with recorded content."""
        workspace_path = AsyncPath(self.workspace_root / relative_path)
        await workspace_path.parent.mkdir(parents=True, exist_ok=True)
        async with NamedTemporaryFile(
            mode="w",
            dir=str(workspace_path.parent),
            delete=False,
            encoding="utf-8",
        ) as file:
            temporary_path = AsyncPath(file.wrapped.name)
            _ = await file.write(content)
        _ = await temporary_path.replace(workspace_path)

    async def _reconcile_file(
        self,
        tx: Transaction,
        workspace_path: Path,
        source_run_id: UUID,
        source_tool_call_id: str,
        actor: DreamingMutationActor,
    ) -> None:
        """Persist one workspace file's current state."""
        relative_path = self._to_relative_path(self.workspace_root, workspace_path)
        async_path = AsyncPath(workspace_path)
        if await async_path.exists():
            text = await async_path.read_text(encoding="utf-8")
            await self._upsert_file_state(
                tx,
                workspace_path=relative_path,
                content_hash=self._content_hash(text),
                content=text,
                is_tombstone=False,
                source_run_id=source_run_id,
                source_tool_call_id=source_tool_call_id,
                mutation_actor=actor,
            )
            return

        await self._upsert_file_state(
            tx,
            workspace_path=relative_path,
            content_hash="",
            content=None,
            is_tombstone=True,
            source_run_id=source_run_id,
            source_tool_call_id=source_tool_call_id,
            mutation_actor=actor,
        )

    async def _upsert_file_state(  # noqa: PLR0913
        self,
        tx: Transaction,
        workspace_path: str,
        *,
        content_hash: str,
        content: str | None,
        is_tombstone: bool,
        source_run_id: UUID | None = None,
        source_tool_call_id: str | None = None,
        mutation_actor: DreamingMutationActor | None = None,
    ) -> None:
        """Persist one current file state in the version table."""
        current = await tx.fetch_one_or_none(
            select(DreamingWorkspaceFile).where(
                DreamingWorkspaceFile.path.eq(workspace_path)
            )
        )
        next_version = 1 if current is None else current.version + 1
        if current is None:
            _ = await tx.execute(
                insert(
                    DreamingWorkspaceFile(
                        path=workspace_path,
                        content_hash=content_hash,
                        content=content,
                        is_tombstone=1 if is_tombstone else 0,
                        version=next_version,
                        source_run_id=source_run_id,
                        source_tool_call_id=source_tool_call_id,
                        actor=mutation_actor,
                    )
                )
            )
            return

        if (
            current.content_hash == content_hash
            and current.is_tombstone == (1 if is_tombstone else 0)
            and current.content == content
        ):
            _ = await tx.execute(
                update(DreamingWorkspaceFile)
                .set(DreamingWorkspaceFile.updated_at.to(CurrentTimestamp))
                .where(DreamingWorkspaceFile.path.eq(workspace_path))
            )
            return

        _ = await tx.execute(
            update(DreamingWorkspaceFile)
            .set(DreamingWorkspaceFile.content_hash.to(content_hash))
            .set(DreamingWorkspaceFile.content.to(content))
            .set(DreamingWorkspaceFile.is_tombstone.to(1 if is_tombstone else 0))
            .set(DreamingWorkspaceFile.version.to(next_version))
            .set(DreamingWorkspaceFile.source_run_id.to(source_run_id))
            .set(DreamingWorkspaceFile.source_tool_call_id.to(source_tool_call_id))
            .set(DreamingWorkspaceFile.actor.to(mutation_actor))
            .set(DreamingWorkspaceFile.updated_at.to(CurrentTimestamp))
            .where(DreamingWorkspaceFile.path.eq(workspace_path))
        )


DreamingMutationAcknowledger = Callable[[UUID, str], Awaitable[tuple[bool, str | None]]]
"""Callable contract for mutation ACK callbacks."""


class HttpDreamingMutationAcknowledger:
    """Call the host ACK callback for one production mutation."""

    def __init__(
        self,
        *,
        base_url: str,
        tool_secret: str,
        timeout_seconds: float = 2.0,
        http_transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._tool_secret = tool_secret
        self._timeout_seconds = timeout_seconds
        self._http_transport = http_transport

    async def __call__(
        self,
        run_id: UUID,
        tool_call_id: str,
    ) -> tuple[bool, str | None]:
        try:
            async with httpx2.AsyncClient(
                base_url=self._base_url,
                timeout=self._timeout_seconds,
                headers={TOOL_AUTH_HEADER: self._tool_secret},
                transport=self._http_transport,
            ) as client:
                response = await client.post(
                    _ACK_PATH.format(
                        run_id=run_id,
                        tool_call_id=tool_call_id,
                    )
                )
        except Exception as error:
            return False, f"{type(error).__name__}: {error}"
        if not response.is_success:
            return False, _extract_json_detail(response)
        return True, None


def _extract_json_detail(response: httpx2.Response) -> str:
    """Read a compact callback error message from one failed host response."""
    try:
        payload: Any = response.json()
    except ValueError:
        return response.text
    if isinstance(payload, dict):
        detail = cast("dict[str, object]", payload).get("detail")
        if isinstance(detail, str):
            return detail
    return response.text


@dataclass(frozen=True, slots=True)
class DreamRunExecutionResult:
    """Terminal outcome returned by one Dream run execution callback."""

    status: DreamRunTerminalStatus
    error: str | None = None


class DreamRunExecutor(Protocol):
    """Callable contract for executing one dream run to terminal state."""

    def __call__(
        self,
        run: DreamRun[Fetched],
        *,
        logger: Logger,
    ) -> Awaitable[DreamRunExecutionResult]: ...


class DreamingCurationRunner(Protocol):
    """Model-backed text runner that curates bounded Evidence into Claims."""

    async def run(self, prompt: str) -> str: ...


@dataclass(frozen=True, slots=True)
class _ConversationDreamEvidence:
    """Normalized source supplied to one Conversation Dream run."""

    content: str
    occurred_at: datetime
    role: str
    seq: PositiveInt
    supports_claim: bool
    uri: str


class ConversationWindowDreamingExecutor:
    """Apply one Dreaming window to the canonical Memory workspace."""

    def __init__(
        self,
        conversation_service: ConversationService,
        workspace_root: Path,
        mutation_coordinator: DreamingMutationCoordinator | None = None,
        mutation_acknowledger: DreamingMutationAcknowledger | None = None,
        curation_runner: DreamingCurationRunner | None = None,
    ) -> None:
        self.conversation_service: ConversationService = conversation_service
        self.workspace_root: Path = workspace_root
        self.curation_runner: DreamingCurationRunner | None = curation_runner
        self.mutation_coordinator: DreamingMutationCoordinator = (
            mutation_coordinator
            if mutation_coordinator is not None
            else DreamingMutationCoordinator(
                conversation_service.database,
                workspace_root,
            )
        )
        self.mutation_acknowledger: DreamingMutationAcknowledger = (
            mutation_acknowledger
            if mutation_acknowledger is not None
            else self.mutation_coordinator.acknowledge_mutation
        )

    async def __call__(
        self,
        run: DreamRun[Fetched],
        *,
        logger: Logger,
    ) -> DreamRunExecutionResult:
        evidence = await self._fetch_evidence(run)
        if not evidence:
            logger.info(
                "Dream run had no evidence rows; marking no-op",
                run_id=str(run.id),
                conversation_id=str(run.conversation_id),
                start_seq=run.evidence_start_seq,
                end_seq=run.evidence_end_seq,
            )
            return DreamRunExecutionResult(status="no_op")

        workspace_path = await self._ensure_workspace_path(run)
        tool_call_id = self.mutation_coordinator.mutation_tool_call_id(run)
        # Resume path: a prior execution may have recorded the mutation
        # without a successful ack. Retry is notification-only (ADR-0022);
        # the lifecycle module owns that policy.
        settlement = await self.mutation_coordinator.settle(
            run.id,
            tool_call_id,
            acknowledger=self.mutation_acknowledger,
        )
        if settlement.outcome in ("settled", "failed"):
            logger.info(
                "Dream run mutation acknowledged after prior execution",
                run_id=str(run.id),
                workspace_path=str(workspace_path),
                acknowledged=settlement.acknowledged,
                error=settlement.error,
            )
            return DreamRunExecutionResult(
                status="success" if settlement.acknowledged else "failed",
                error=settlement.error,
            )

        curated_body = (
            await self.curation_runner.run(self._render_curation_prompt(run, evidence))
            if self.curation_runner is not None
            else None
        )
        if curated_body is not None:
            normalized_curated_body = curated_body.strip()
            if normalized_curated_body == "NO_CHANGES":
                return DreamRunExecutionResult(status="no_op")
            if validation_error := self._validate_curated_body(
                normalized_curated_body, evidence
            ):
                return DreamRunExecutionResult(status="failed", error=validation_error)
            curated_body = normalized_curated_body
        async with self.mutation_coordinator.mutation_scope():
            written = await self._write_dream_document(
                run=run,
                evidence=evidence,
                workspace_path=workspace_path,
                logger=logger,
                curated_body=curated_body,
            )
            logger.info(
                "Dream run wrote evidence file",
                run_id=str(run.id),
                workspace_path=str(workspace_path),
                message_count=len(evidence),
                result="success" if written is not None else "no_op",
            )
            if written is None:
                return DreamRunExecutionResult(status="no_op")

            payload = self._render_run_section(run, evidence)
            _ = await self.mutation_coordinator.record_mutation(
                run_id=run.id,
                tool_call_id=tool_call_id,
                actor="dream",
                operation="write",
                workspace_path=Path(workspace_path),
                payload=payload,
            )
            settlement = await self.mutation_coordinator.settle(
                run.id,
                tool_call_id,
                acknowledger=self.mutation_acknowledger,
            )
        return DreamRunExecutionResult(
            status="success" if settlement.acknowledged else "failed",
            error=settlement.error,
        )

    async def _write_dream_document(
        self,
        *,
        run: DreamRun[Fetched],
        evidence: list[_ConversationDreamEvidence],
        workspace_path: AsyncPath,
        logger: Logger,
        curated_body: str | None = None,
    ) -> AsyncPath | None:
        """Write deterministic dream evidence and report no-op when unchanged."""
        if not await workspace_path.parent.exists():
            await workspace_path.parent.mkdir(parents=True, exist_ok=True)

        evidence_uris = self._evidence_uris(evidence)
        existing_document: str | None = None
        existing_frontmatter: dict[str, Any] = {}
        if await workspace_path.exists():
            existing_document = await workspace_path.read_text(encoding="utf-8")
            existing_frontmatter, _ = self._split_frontmatter(existing_document)
            raw_evidence = existing_frontmatter.get("evidence")
            existing_evidence: list[object] = []
            if isinstance(raw_evidence, list):
                existing_evidence = cast("list[object]", raw_evidence)
            existing_uris = set(self._evidence_uris_from_frontmatter(existing_evidence))
            if set(evidence_uris).issubset(existing_uris):
                logger.debug(
                    "Dream run document is unchanged",
                    run_id=str(run.id),
                    workspace_path=str(workspace_path),
                )
                return None
            if self._contains_run_payload(existing_document, run.id):
                logger.debug(
                    "Dream run document already contains this run payload",
                    run_id=str(run.id),
                    workspace_path=str(workspace_path),
                )
                return None

        content = (
            self._render_curated_document(run, evidence, curated_body)
            if curated_body is not None
            else self._render_dream_document(
                run=run,
                evidence=evidence,
                previous_frontmatter=existing_frontmatter,
                existing_document=existing_document,
            )
        )
        async with NamedTemporaryFile(
            mode="w",
            dir=str(workspace_path.parent),
            delete=False,
        ) as file:
            temp_path = AsyncPath(file.wrapped.name)
            _ = await file.write(content)
        _ = await temp_path.replace(workspace_path)
        return workspace_path

    async def _ensure_workspace_path(self, run: DreamRun[Fetched]) -> AsyncPath:
        """Return a stable file path for the run under the workspace root."""
        root = AsyncPath(self.workspace_root)
        await root.mkdir(parents=True, exist_ok=True)
        target = root / str(run.conversation_id)
        await target.mkdir(parents=True, exist_ok=True)
        return target / f"{run.id}.md"

    @staticmethod
    def _render_curation_prompt(
        run: DreamRun[Fetched], evidence: list[_ConversationDreamEvidence]
    ) -> str:
        """Render exact bounded Evidence for unattended Claim curation."""
        evidence_blocks = "\n\n".join(
            "\n".join(
                (
                    f"seq: {source.seq}",
                    "kind: message",
                    f"role: {source.role}",
                    f"supports_claim: {str(source.supports_claim).lower()}",
                    f"created_at: {source.occurred_at.isoformat()}",
                    f"uri: {source.uri}",
                    "content:",
                    source.content,
                )
            )
            for source in evidence
        )
        return f"""Curate durable, user-centric Claims from this bounded Conversation Evidence.

Rules:
- Only sources marked `supports_claim: true` may support Claims.
- User Messages outrank assistant conclusions; explicit user corrections supersede them.
- Assistant repetition does not corroborate or strengthen an agent-derived conclusion.
- Preserve uncertainty in assistant conclusions instead of converting inference to fact.
- Scheduled, Health, reasoning, tool, partial, and failed Messages are context only.
- Omit transient requests, implementation chatter, and unsupported assertions.
- Address the user as `you` and `your`. Begin every Claim with `You` or `Your`. Never call them "the user" or use third-person pronouns for them.
- Return Markdown only, grouped under `##` Topic headings.
- Every Claim is one `- ` bullet with an inline exact `tether://` source citation.
- Use only exact Evidence URIs below. Preserve uncertainty and corrections.
- Return `NO_CHANGES` when no durable Claim is supported.

run_id: {run.id}
evidence_start_seq: {run.evidence_start_seq}
evidence_end_seq: {run.evidence_end_seq}

{evidence_blocks}
"""

    @staticmethod
    def _validate_curated_body(
        curated_body: str, evidence: list[_ConversationDreamEvidence]
    ) -> str | None:
        """Refuse citations that cannot support Claims in this bounded run."""
        supported_evidence_uris = {
            source.uri for source in evidence if source.supports_claim
        }
        claim_lines = [
            line for line in curated_body.splitlines() if line.startswith("- ")
        ]
        if voice_error := _memory_claim_voice_error((curated_body,)):
            return voice_error
        if not claim_lines or any(
            not re.search(r"tether://message/[0-9A-Za-z-]+", claim)
            for claim in claim_lines
        ):
            return "every curated Claim must cite bounded supporting Evidence"
        cited_uris = set(re.findall(r"tether://message/[0-9A-Za-z-]+", curated_body))
        unsupported = sorted(cited_uris - supported_evidence_uris)
        if unsupported:
            return (
                "curated Claim cites outside bounded supporting Evidence: "
                + ", ".join(unsupported)
            )
        return None

    @staticmethod
    def _render_curated_document(
        run: DreamRun[Fetched],
        evidence: list[_ConversationDreamEvidence],
        curated_body: str,
    ) -> str:
        """Wrap curated Claims in canonical Topic frontmatter."""
        normalized_body = curated_body.strip()
        heading = next(
            (
                line.removeprefix("##").strip()
                for line in normalized_body.splitlines()
                if line.startswith("##") and line.removeprefix("##").strip()
            ),
            "Conversation insights",
        )
        frontmatter = {
            "title": heading,
            "kind": run.kind,
            "conversation": str(run.conversation_id),
            "evidence_start_seq": run.evidence_start_seq,
            "evidence_end_seq": run.evidence_end_seq,
            "evidence": ConversationWindowDreamingExecutor._evidence_uris(evidence),
        }
        return (
            "---\n"
            + yaml_dump(frontmatter, default_flow_style=False, sort_keys=False)
            + "---\n\n"
            + normalized_body
            + "\n"
        )

    def _render_dream_document(
        self,
        *,
        run: DreamRun[Fetched],
        evidence: list[_ConversationDreamEvidence],
        existing_document: str | None,
        previous_frontmatter: dict[str, Any],
    ) -> str:
        """Render a cumulative, deterministic draft from previous corpus + new window."""
        new_uris = self._evidence_uris(evidence)
        merged_evidence = self._merge_evidence(
            previous_frontmatter,
            new_uris,
        )
        frontmatter = {
            "title": previous_frontmatter.get("title", self._default_title(run)),
            "kind": run.kind,
            "conversation": str(run.conversation_id),
            "evidence_start_seq": run.evidence_start_seq,
            "evidence_end_seq": run.evidence_end_seq,
            "evidence": merged_evidence,
        }
        existing_body = (
            ""
            if existing_document is None
            else self._strip_frontmatter(existing_document)
        ).rstrip()
        section = self._render_run_section(run, evidence)
        body = "\n\n".join(part for part in (existing_body, section) if part)
        return (
            "---\n"
            + yaml_dump(frontmatter, default_flow_style=False, sort_keys=False)
            + "---\n\n"
            + body
            + "\n"
        )

    @staticmethod
    def _default_title(run: DreamRun[Fetched]) -> str:
        return f"Dreamed memory {run.id}"

    @staticmethod
    def _strip_frontmatter(document: str) -> str:
        if not document.startswith("---\n"):
            return document
        parts = document.split(_FRONTMATTER_SEPARATOR, 1)
        if len(parts) != _FRONTMATTER_PART_COUNT:
            return document
        return parts[1]

    @staticmethod
    def _split_frontmatter(document: str) -> tuple[dict[str, Any], str]:
        if not document.startswith("---\n"):
            return {}, document
        if _FRONTMATTER_SEPARATOR not in document[3:]:
            return {}, document
        raw, body = document[3:].split(_FRONTMATTER_SEPARATOR, 1)
        loaded_frontmatter = safe_load(raw)
        if not isinstance(loaded_frontmatter, dict):
            return {}, document
        frontmatter_data = cast("dict[str, Any]", loaded_frontmatter)
        return frontmatter_data, body

    @staticmethod
    def _evidence_uris(evidence: list[_ConversationDreamEvidence]) -> list[str]:
        return list(dict.fromkeys(source.uri for source in evidence))

    @staticmethod
    def _evidence_uris_from_frontmatter(raw_values: list[object]) -> list[str]:
        return [
            raw_value
            for raw_value in raw_values
            if isinstance(raw_value, str) and raw_value.startswith("tether://")
        ]

    @staticmethod
    def _merge_evidence(
        frontmatter: dict[str, Any],
        evidence: list[str],
    ) -> list[str]:
        existing: list[str] = []
        existing_raw = frontmatter.get("evidence", [])
        if isinstance(existing_raw, list):
            existing.extend(
                item
                for item in cast("list[object]", existing_raw)
                if isinstance(item, str)
            )
        merged: list[str] = []
        seen: set[str] = set()
        for value in existing + evidence:
            if value in seen:
                continue
            seen.add(value)
            merged.append(value)
        return merged

    @staticmethod
    def _render_run_section(
        run: DreamRun[Fetched],
        evidence: list[_ConversationDreamEvidence],
    ) -> str:
        lines = [
            "## Dream slice",
            f"- run_id: {run.id}",
            f"- evidence_start_seq: {run.evidence_start_seq}",
            f"- evidence_end_seq: {run.evidence_end_seq}",
            f"- kind: {run.kind}",
            "- messages:",
            *(
                " ".join(
                    (
                        "  -",
                        str(source.seq),
                        source.role,
                        source.occurred_at.isoformat(),
                        source.uri,
                    )
                )
                for source in evidence
            ),
            "",
            *(source.content for source in evidence),
            "",
        ]
        return "\n".join(lines)

    @staticmethod
    def _contains_run_payload(document: str, run_id: UUID) -> bool:
        return re.search(rf"run_id:\s*{re.escape(str(run_id))}", document) is not None

    async def _fetch_evidence(
        self, run: DreamRun[Fetched]
    ) -> list[_ConversationDreamEvidence]:
        """Return Conversation Messages bounded by the run window."""
        if run.evidence_end_seq < run.evidence_start_seq:
            return []
        messages = await self.conversation_service.fetch_messages(
            run.conversation_id,
            before_seq=run.evidence_end_seq + 1,
            limit=run.evidence_end_seq - run.evidence_start_seq + 1,
        )
        bounded_messages = [
            message for message in messages if message.seq >= run.evidence_start_seq
        ]
        supporting_message_ids = await fetch_claim_supporting_message_ids(
            self.conversation_service.database,
            bounded_messages,
        )
        return [
            _ConversationDreamEvidence(
                content=message.content,
                occurred_at=message.created_at,
                role=message.role,
                seq=message.seq,
                supports_claim=message.id in supporting_message_ids,
                uri=f"tether://message/{message.id}",
            )
            for message in bounded_messages
        ]


_MAINTENANCE_MIN_FILES = 1
"""Every current Topic is eligible for periodic semantic maintenance."""

_MAINTENANCE_MAX_FILES = 8
"""Upper bound on topic files consolidated in one maintenance run."""

_MAINTENANCE_MAX_CHARS = 24_000
"""Upper bound on total input characters handed to one consolidation call."""

_MAINTENANCE_MAX_FOLLOWUP_MESSAGES = 100
"""Bound newer user Evidence considered for correction and supersession."""

_EVIDENCE_URI_PATTERN = re.compile(r"tether://[^\s)\]>,\"']+")
_MESSAGE_URI_PATTERN = re.compile(
    "".join(
        (
            r"tether://message/([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-",
            r"[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})",
        )
    )
)
_FILE_SEPARATOR_PATTERN = re.compile(r"^===\s+(\S+)\s+===\s*$", re.MULTILINE)
_RETIREMENT_SEPARATOR = "=== RETIREMENTS ==="
_SOURCE_CITATION_PATTERN = re.compile(r"\s*\[source\]\(tether://[^)]+\)", re.IGNORECASE)

MaintenanceRetirementReason = Literal[
    "expired",
    "explicitly_no_longer_current",
    "superseded",
    "unsupported",
]


@dataclass(frozen=True, slots=True)
class MaintenanceRetirement:
    """One justified removal from current Memory."""

    basis: tuple[str, ...]
    claim: str
    reason: MaintenanceRetirementReason


@dataclass(frozen=True, slots=True)
class MaintenanceTransition:
    """One fully parsed candidate state and its destructive decisions."""

    documents: list[tuple[str, str]]
    retirements: list[MaintenanceRetirement]


@dataclass(frozen=True, slots=True, kw_only=True)
class MaintenanceDreamingAgent:
    """Model roles and clock used to propose and verify Memory transitions."""

    clock: Callable[[], datetime] = _utc_now
    curator: DreamingCurationRunner
    verifier: DreamingCurationRunner


class _MaintenanceError(Exception):
    """A consolidated response violated a maintenance invariant."""


def maintenance_group_run_id(folder: str) -> UUID7:
    """Stable synthetic UUIDv7 for a non-conversation workspace folder."""
    raw = bytearray(uuid5(NAMESPACE_URL, f"tether:maintenance-group:{folder}").bytes)
    raw[6] = (raw[6] & 0x0F) | 0x70
    raw[8] = (raw[8] & 0x3F) | 0x80
    return UUID(bytes=bytes(raw))


class MaintenanceDreamingExecutor:
    """Revise current Topics through bounded, verified Memory transitions.

    The curator may merge structure and retire Claims that canonical Evidence
    proves are no longer current. Deterministic checks and a separate verifier
    run before recorded mutations, so failed transitions leave Memory unchanged.
    """

    def __init__(
        self,
        database: Database,
        workspace_root: Path,
        *,
        mutation_coordinator: DreamingMutationCoordinator | None = None,
        mutation_acknowledger: DreamingMutationAcknowledger | None = None,
        agent: MaintenanceDreamingAgent | None = None,
    ) -> None:
        self.database: Database = database
        self.workspace_root: Path = Path(workspace_root)
        self.agent: MaintenanceDreamingAgent | None = agent
        self.mutation_coordinator: DreamingMutationCoordinator = (
            mutation_coordinator
            if mutation_coordinator is not None
            else DreamingMutationCoordinator(database, self.workspace_root)
        )
        self.mutation_acknowledger: DreamingMutationAcknowledger = (
            mutation_acknowledger
            if mutation_acknowledger is not None
            else self.mutation_coordinator.acknowledge_mutation
        )

    async def __call__(
        self,
        run: DreamRun[Fetched],
        *,
        logger: Logger,
    ) -> DreamRunExecutionResult:
        agent = self.agent
        current_time = agent.clock() if agent is not None else _utc_now()
        batch = await self._select_batch(run.conversation_id, now=current_time)
        if len(batch) < _MAINTENANCE_MIN_FILES:
            return DreamRunExecutionResult(status="no_op")
        if agent is None:
            return DreamRunExecutionResult(
                status="failed",
                error="maintenance requires a consolidation runner",
            )
        evidence = await self._fetch_message_evidence(batch)
        response = (
            await agent.curator.run(
                self._render_prompt(batch, evidence=evidence, now=current_time)
            )
        ).strip()
        if response == "NO_CHANGES":
            voice_error = _memory_claim_voice_error(content for _, content in batch)
            if voice_error is None:
                await self._mark_maintained(batch)
                logger.info(
                    "Maintenance found nothing to consolidate",
                    run_id=str(run.id),
                    files=len(batch),
                )
            return DreamRunExecutionResult(
                status="no_op" if voice_error is None else "failed",
                error=voice_error,
            )
        try:
            transition = self._parse_response(response)
            needs_semantic_verification = self._validate_transition(
                batch,
                transition,
                evidence=evidence,
            )
        except _MaintenanceError as error:
            return DreamRunExecutionResult(status="failed", error=str(error))
        verification_error = (
            await self._verify_retirements(
                agent,
                batch,
                evidence=evidence,
                now=current_time,
                response=response,
            )
            if transition.retirements or needs_semantic_verification
            else None
        )
        if verification_error:
            return DreamRunExecutionResult(
                status="failed",
                error=verification_error,
            )

        applied = await self._apply(
            run,
            batch,
            transition.documents,
            retirements=transition.retirements,
        )
        logger.info(
            "Maintenance consolidated topics",
            run_id=str(run.id),
            inputs=len(batch),
            outputs=len(applied),
            retirements=len(transition.retirements),
        )
        return DreamRunExecutionResult(status="success")

    async def _verify_retirements(
        self,
        agent: MaintenanceDreamingAgent,
        batch: list[tuple[str, str]],
        *,
        evidence: list[Message[Fetched]],
        now: datetime,
        response: str,
    ) -> str | None:
        """Require semantic verification before any Claim leaves current Memory."""
        verdict = (
            await agent.verifier.run(
                self._render_verification_prompt(
                    batch,
                    evidence=evidence,
                    now=now,
                    response=response,
                )
            )
        ).strip()
        if verdict == "APPROVED":
            return None
        return f"transition verifier rejected retirement: {verdict}"

    def _folder_name(self, conversation_id: UUID) -> str:
        """Resolve the workspace folder a maintenance run targets."""
        direct = self.workspace_root / str(conversation_id)
        if direct.is_dir():
            return str(conversation_id)
        for child in sorted(self.workspace_root.iterdir()):
            if child.is_dir() and maintenance_group_run_id(child.name) == (
                conversation_id
            ):
                return child.name
        return ""

    async def _conversation_topics(
        self, conversation_id: UUID
    ) -> list[tuple[str, str]]:
        """Return current valid topics under the targeted folder group."""
        scan = await MemoryWorkspace(self.workspace_root).scan()
        folder = self._folder_name(conversation_id)
        pairs: list[tuple[str, str]] = []
        for topic in scan.topics:
            relative = topic.path.relative_to(self.workspace_root).as_posix()
            if folder:
                if not relative.startswith(f"{folder}/"):
                    continue
            elif "/" in relative:
                continue
            pairs.append((relative, await AsyncPath(topic.path).read_text("utf-8")))
        return pairs

    async def _select_batch(
        self,
        conversation_id: UUID,
        *,
        now: datetime,
    ) -> list[tuple[str, str]]:
        """Prioritize due review, then least-recently maintained Topics."""
        topics = await self._conversation_topics(conversation_id)
        if len(topics) < _MAINTENANCE_MIN_FILES:
            return []
        progress = {row.path: row for row in await self._fetch_progress()}

        def _maintenance_key(pair: tuple[str, str]) -> tuple[int, str, str]:
            entry = progress.get(pair[0])
            maintained = "" if entry is None else str(entry.maintained_at)
            due_order = 0 if self._review_after_is_due(pair[1], now=now) else 1
            return (due_order, maintained, pair[0])

        ordered = sorted(topics, key=_maintenance_key)
        batch: list[tuple[str, str]] = []
        total = 0
        for pair in ordered:
            if len(batch) >= _MAINTENANCE_MAX_FILES:
                break
            if batch and total + len(pair[1]) > _MAINTENANCE_MAX_CHARS:
                break
            batch.append(pair)
            total += len(pair[1])
        return batch

    @staticmethod
    def _review_after_is_due(document: str, *, now: datetime) -> bool:
        """Prioritize an explicit temporal review hint once its date arrives."""
        frontmatter: dict[str, object] = {}
        if document.startswith("---\n"):
            parts = document.split(_FRONTMATTER_SEPARATOR, 1)
            if len(parts) == _FRONTMATTER_PART_COUNT:
                try:
                    loaded = safe_load(parts[0][3:])
                except YAMLError:
                    loaded = None
                if isinstance(loaded, dict):
                    frontmatter = cast("dict[str, object]", loaded)
        review_after = frontmatter.get("review_after")
        if isinstance(review_after, datetime):
            return _as_utc(review_after) <= _as_utc(now)
        if isinstance(review_after, date):
            return review_after <= _as_utc(now).date()
        return False

    async def _fetch_progress(self) -> list[DreamMaintenanceProgress[Fetched]]:
        async with self.database.transaction() as tx:
            return list(await tx.fetch_all(select(DreamMaintenanceProgress).all()))

    async def _fetch_message_evidence(
        self, batch: list[tuple[str, str]]
    ) -> list[Message[Fetched]]:
        """Resolve cited Conversation Evidence so maintenance can reason over time."""
        message_ids = {
            UUID(match.group(1))
            for _, content in batch
            for match in _MESSAGE_URI_PATTERN.finditer(content)
        }
        if not message_ids:
            return []
        async with self.database.transaction() as transaction:
            cited_messages = await transaction.fetch_all(
                select(Message).where(Message.id.in_(*sorted(message_ids)))
            )
            oldest_seq_by_conversation: dict[UUID, int] = {}
            for message in cited_messages:
                previous = oldest_seq_by_conversation.get(message.conversation_id)
                oldest_seq_by_conversation[message.conversation_id] = (
                    message.seq if previous is None else min(previous, message.seq)
                )
            related_messages: dict[UUID, Message[Fetched]] = {
                message.id: message for message in cited_messages
            }
            for conversation_id, oldest_seq in oldest_seq_by_conversation.items():
                followups = await transaction.fetch_all(
                    select(Message)
                    .where(Message.conversation_id.eq(conversation_id))
                    .where(Message.role.eq("user"))
                    .where(Message.seq.gte(oldest_seq))
                    .order_by(Message.seq.desc())
                    .limit(_MAINTENANCE_MAX_FOLLOWUP_MESSAGES)
                )
                related_messages.update({message.id: message for message in followups})
        supporting_ids = await fetch_claim_supporting_message_ids(
            self.database,
            related_messages.values(),
        )
        return sorted(
            (
                message
                for message in related_messages.values()
                if message.id in supporting_ids
            ),
            key=lambda message: (message.created_at, message.seq),
        )

    def _render_prompt(
        self,
        batch: list[tuple[str, str]],
        *,
        evidence: list[Message[Fetched]],
        now: datetime,
    ) -> str:
        """Render current state, its source Evidence, and temporal policy."""
        blocks = "\n\n".join(
            f"<<< {relative}\n{content}\n>>>" for relative, content in batch
        )
        evidence_blocks = "\n\n".join(
            "\n".join(
                (
                    f"uri: tether://message/{message.id}",
                    f"role: {message.role}",
                    f"created_at: {_as_utc(message.created_at).isoformat()}",
                    "content:",
                    message.content,
                )
            )
            for message in evidence
        )
        if not evidence_blocks:
            evidence_blocks = "(No Conversation Evidence resolved.)"
        return f"""Maintain current Memory Topic documents using canonical Evidence.

current_time: {_as_utc(now).isoformat()}

Inputs (workspace-relative paths with complete contents):

{blocks}

Canonical Conversation Evidence cited by the inputs:

{evidence_blocks}

Rules:
- Merge duplicate or tightly overlapping Claims into concise Claims that preserve their complete meaning and citations; keep distinct facts distinct.
- User Messages outrank assistant conclusions; explicit user corrections supersede them.
- Assistant repetition does not corroborate or strengthen an agent-derived conclusion.
- Preserve uncertainty in assistant conclusions instead of converting inference to fact.
- Address the user as `you` and `your`. Begin every Claim with `You` or `Your`; never call them "the user" or use third-person pronouns for them.
- Rewrite existing third-person user references into second person without changing their meaning or citations.
- Unify related fragments into fewer larger Topic files with meaningful titles and stable kebab-case `.md` paths.
- Preserve every supported idea unless it qualifies for retirement. Age or disuse alone never qualifies.
- Retire a Claim only when its explicit time bound passed, newer Evidence supersedes it, Evidence explicitly says it is no longer current, or it lacks permitted support.
- When uncertain, preserve or qualify the Claim and set `review_after`; never guess.
- Every evidence citation and retirement basis must be copied verbatim from the supplied Evidence; never invent or alter a `tether://` URI.
- Return either exactly `NO_CHANGES` or zero or more resulting documents followed by a retirement ledger when any Claim is removed:

=== <workspace-relative/path.md> ===
---
title: <Topic title>
---

<document body>

=== RETIREMENTS ===
- claim: <exact old Claim text without the leading dash or source citation>
  reason: <expired|superseded|explicitly_no_longer_current|unsupported>
  basis:
    - <exact supplied Evidence URI>
"""

    def _render_verification_prompt(
        self,
        batch: list[tuple[str, str]],
        *,
        evidence: list[Message[Fetched]],
        now: datetime,
        response: str,
    ) -> str:
        """Ask a separate pass to reject semantically unsafe retirement."""
        current_documents = "\n\n".join(
            f"<<< {relative}\n{content}\n>>>" for relative, content in batch
        )
        evidence_blocks = "\n\n".join(
            "\n".join(
                (
                    f"uri: tether://message/{message.id}",
                    f"role: {message.role}",
                    f"created_at: {_as_utc(message.created_at).isoformat()}",
                    "content:",
                    message.content,
                )
            )
            for message in evidence
        )
        return f"""Verify one proposed transition of the user's current Memory.

current_time: {_as_utc(now).isoformat()}

Current Topic documents:
{current_documents}

Canonical Conversation Evidence:
{evidence_blocks}

Proposed transition:
{response}

Return exactly `APPROVED` only when all three checks pass:
- coverage: every prior Claim's supported meaning remains in a resulting Claim or has an explicit retirement;
- preservation: no still-supported Claim is dropped or distorted;
- faithfulness: every retirement reason follows from supplied Evidence and time.

Age or disuse alone never justifies retirement. Otherwise return one concise rejection reason.
"""

    def _parse_response(self, response: str) -> MaintenanceTransition:
        """Parse resulting documents and an optional retirement ledger."""
        if response.count(_RETIREMENT_SEPARATOR) > 1:
            message = "consolidation response repeated the retirement ledger"
            raise _MaintenanceError(message)
        documents_text, marker, retirements_text = response.partition(
            _RETIREMENT_SEPARATOR
        )
        documents = self._parse_documents(documents_text.strip())
        retirements = (
            self._parse_retirements(retirements_text.strip()) if marker else []
        )
        if not documents and not retirements:
            message = "consolidation response contained no documents or retirements"
            raise _MaintenanceError(message)
        return MaintenanceTransition(
            documents=documents,
            retirements=retirements,
        )

    def _parse_documents(self, response: str) -> list[tuple[str, str]]:
        """Parse the candidate current Topic documents."""
        if not response:
            return []
        separators = list(_FILE_SEPARATOR_PATTERN.finditer(response))
        if not separators:
            message = "consolidation response contained malformed documents"
            raise _MaintenanceError(message)
        proposals: list[tuple[str, str]] = []
        seen_paths: set[str] = set()
        for index, separator in enumerate(separators):
            raw_path = separator.group(1)
            safe_path = self._safe_relative_path(raw_path)
            if safe_path is None:
                message = f"consolidated path is unsafe: {raw_path}"
                raise _MaintenanceError(message)
            if safe_path in seen_paths:
                message = f"consolidated path repeated: {safe_path}"
                raise _MaintenanceError(message)
            seen_paths.add(safe_path)
            end = (
                separators[index + 1].start()
                if index + 1 < len(separators)
                else len(response)
            )
            document = response[separator.end() : end].strip("\n")
            if not self._document_title(document):
                message = f"consolidated document lacks frontmatter title: {safe_path}"
                raise _MaintenanceError(message)
            proposals.append((safe_path, document + "\n"))
        return proposals

    def _parse_retirements(self, response: str) -> list[MaintenanceRetirement]:
        """Decode the closed retirement vocabulary from YAML."""
        try:
            loaded = safe_load(response)
        except YAMLError as error:
            message = f"retirement ledger is invalid YAML: {error}"
            raise _MaintenanceError(message) from error
        if not isinstance(loaded, list):
            message = "retirement ledger must be a YAML list"
            raise _MaintenanceError(message)
        retirements: list[MaintenanceRetirement] = []
        allowed_reasons: set[str] = {
            "expired",
            "explicitly_no_longer_current",
            "superseded",
            "unsupported",
        }
        for raw_retirement in cast("list[object]", loaded):
            if not isinstance(raw_retirement, dict):
                message = "each retirement must be a mapping"
                raise _MaintenanceError(message)
            retirement = cast("dict[str, object]", raw_retirement)
            if set(retirement) != {"basis", "claim", "reason"}:
                message = "retirement must contain only basis, claim, and reason"
                raise _MaintenanceError(message)
            claim = retirement["claim"]
            reason = retirement["reason"]
            basis = retirement["basis"]
            if not isinstance(claim, str) or not claim.strip():
                message = "retirement claim must be non-empty text"
                raise _MaintenanceError(message)
            if not isinstance(reason, str) or reason not in allowed_reasons:
                message = f"retirement reason is unsupported: {reason}"
                raise _MaintenanceError(message)
            if (
                not isinstance(basis, list)
                or not basis
                or not all(isinstance(uri, str) for uri in cast("list[object]", basis))
            ):
                message = "retirement basis must contain Evidence URIs"
                raise _MaintenanceError(message)
            retirements.append(
                MaintenanceRetirement(
                    basis=tuple(cast("list[str]", basis)),
                    claim=claim.strip(),
                    reason=cast("MaintenanceRetirementReason", reason),
                )
            )
        return retirements

    @staticmethod
    def _document_title(document: str) -> str | None:
        """Return the frontmatter title of one proposed document, or `None`."""
        if not document.startswith("---\n"):
            return None
        parts = document.split(_FRONTMATTER_SEPARATOR, 1)
        if len(parts) != _FRONTMATTER_PART_COUNT:
            return None
        loaded = cast("dict[str, object] | None", safe_load(parts[0][3:]))
        if loaded is None:
            return None
        raw_title = loaded.get("title")
        if not isinstance(raw_title, str) or not raw_title.strip():
            return None
        return raw_title

    @staticmethod
    def _safe_relative_path(raw_path: str) -> str | None:
        """Normalize a proposed path, refusing anything outside the root."""
        if "\\" in raw_path or not raw_path.endswith(".md"):
            return None
        candidate = PurePosixPath(raw_path)
        if candidate.is_absolute() or any(
            part in ("..", "") for part in candidate.parts
        ):
            return None
        return candidate.as_posix()

    @staticmethod
    def _evidence_uris(document: str) -> set[str]:
        """Extract canonical citations without sentence-ending punctuation."""
        return {
            match.rstrip(".,;:!?") for match in _EVIDENCE_URI_PATTERN.findall(document)
        }

    @staticmethod
    def _claim_texts(document: str) -> set[str]:
        """Read exact current Claim text while excluding source-link markup."""
        body = document
        if document.startswith("---\n"):
            parts = document.split(_FRONTMATTER_SEPARATOR, 1)
            if len(parts) == _FRONTMATTER_PART_COUNT:
                body = parts[1]
        return {
            _SOURCE_CITATION_PATTERN.sub("", line.removeprefix("- ")).strip()
            for line in body.splitlines()
            if line.startswith("- ")
            and _SOURCE_CITATION_PATTERN.sub("", line.removeprefix("- ")).strip()
        }

    @classmethod
    def _validate_transition(
        cls,
        batch: list[tuple[str, str]],
        transition: MaintenanceTransition,
        *,
        evidence: list[Message[Fetched]],
    ) -> bool:
        """Reject invalid transitions and flag semantic rewrites for verification."""
        if voice_error := _memory_claim_voice_error(
            document for _, document in transition.documents
        ):
            raise _MaintenanceError(voice_error)
        supported = {
            uri for _, content in batch for uri in cls._evidence_uris(content)
        } | {f"tether://message/{message.id}" for message in evidence}
        permitted_message_uris = {
            f"tether://message/{message.id}" for message in evidence
        }
        for relative, document in transition.documents:
            document_citations = cls._evidence_uris(document)
            if unsupported := document_citations - supported:
                message = (
                    "consolidated document contains an unsupported citation: "
                    f"{relative}: {', '.join(sorted(unsupported))}"
                )
                raise _MaintenanceError(message)
            ineligible_messages = {
                uri
                for uri in document_citations
                if uri.startswith("tether://message/")
                and uri not in permitted_message_uris
            }
            if ineligible_messages:
                message = (
                    "current Claims require permitted Message Evidence: "
                    f"{relative}: {', '.join(sorted(ineligible_messages))}"
                )
                raise _MaintenanceError(message)
        retired_claims: set[str] = set()
        for retirement in transition.retirements:
            if unsupported := set(retirement.basis) - supported:
                message = (
                    "retirement contains an unsupported Evidence basis: "
                    f"{', '.join(sorted(unsupported))}"
                )
                raise _MaintenanceError(message)
            if retirement.claim in retired_claims:
                message = f"retirement repeated Claim: {retirement.claim}"
                raise _MaintenanceError(message)
            retired_claims.add(retirement.claim)

        prior_claims = {
            claim for _, document in batch for claim in cls._claim_texts(document)
        }
        resulting_claims = {
            claim
            for _, document in transition.documents
            for claim in cls._claim_texts(document)
        }
        if unknown_retirements := retired_claims - prior_claims:
            message = "retirement does not name an exact prior Claim: " + ", ".join(
                sorted(unknown_retirements)
            )
            raise _MaintenanceError(message)
        unexplained = prior_claims - resulting_claims - retired_claims
        return bool(unexplained)

    async def _apply(
        self,
        run: DreamRun[Fetched],
        batch: list[tuple[str, str]],
        proposed: list[tuple[str, str]],
        *,
        retirements: list[MaintenanceRetirement],
    ) -> list[str]:
        """Write consolidated files, delete replaced ones, record mutations."""
        written: list[tuple[str, str]] = []
        replaced_inputs: dict[str, str] = dict(batch)
        retirement_payload = yaml_dump(
            {
                "retirements": [
                    {
                        "claim": retirement.claim,
                        "reason": retirement.reason,
                        "basis": list(retirement.basis),
                    }
                    for retirement in retirements
                ]
            },
            default_flow_style=False,
            sort_keys=False,
        )
        async with self.mutation_coordinator.mutation_scope():
            for index, (relative, document) in enumerate(proposed):
                target = self.workspace_root / relative
                existing = (
                    await AsyncPath(target).read_text("utf-8")
                    if await AsyncPath(target).exists()
                    else None
                )
                _ = replaced_inputs.pop(relative, None)
                if existing == document:
                    written.append((relative, document))
                    continue
                await AsyncPath(target.parent).mkdir(parents=True, exist_ok=True)
                async with NamedTemporaryFile(
                    mode="w",
                    dir=str(target.parent),
                    delete=False,
                    encoding="utf-8",
                ) as file:
                    temporary_path = AsyncPath(file.wrapped.name)
                    _ = await file.write(document)
                _ = await temporary_path.replace(AsyncPath(target))
                written.append((relative, document))
                tool_call_id = self._mutation_tool_call_id(run, index, relative)
                _ = await self.mutation_coordinator.record_mutation(
                    run_id=run.id,
                    tool_call_id=tool_call_id,
                    actor="dream",
                    operation="write",
                    workspace_path=target,
                    payload=document,
                )
                settlement = await self.mutation_coordinator.settle(
                    run.id,
                    tool_call_id,
                    acknowledger=self.mutation_acknowledger,
                )
                _ = settlement
            for relative in replaced_inputs:
                target = self.workspace_root / relative
                if await AsyncPath(target).exists():
                    await AsyncPath(target).unlink()
                tool_call_id = self._mutation_tool_call_id(run, len(proposed), relative)
                _ = await self.mutation_coordinator.record_mutation(
                    run_id=run.id,
                    tool_call_id=tool_call_id,
                    actor="dream",
                    operation="delete",
                    workspace_path=target,
                    payload=retirement_payload,
                )
                settlement = await self.mutation_coordinator.settle(
                    run.id,
                    tool_call_id,
                    acknowledger=self.mutation_acknowledger,
                )
                _ = settlement
        await self._record_progress(written, deleted=replaced_inputs.keys())
        return [relative for relative, _ in written]

    @staticmethod
    def _mutation_tool_call_id(
        run: DreamRun[Fetched], index: int, relative: str
    ) -> str:
        seed = f"{run.id}:maintenance:{index}:{relative}"
        return str(uuid5(NAMESPACE_URL, seed))

    async def _record_progress(
        self,
        written: list[tuple[str, str]],
        *,
        deleted: Iterable[str],
    ) -> None:
        """Persist maintained paths and drop tombstoned ones."""
        async with self.database.transaction(mode="immediate") as tx:
            for relative, document in written:
                existing = await tx.fetch_one_or_none(
                    select(DreamMaintenanceProgress).where(
                        DreamMaintenanceProgress.path.eq(relative)
                    )
                )
                content_hash = hashlib.sha256(document.encode()).hexdigest()
                if existing is None:
                    _ = await tx.execute(
                        insert(
                            DreamMaintenanceProgress(
                                path=relative,
                                content_hash=content_hash,
                            )
                        )
                    )
                else:
                    _ = await tx.execute(
                        update(DreamMaintenanceProgress)
                        .set(DreamMaintenanceProgress.content_hash.to(content_hash))
                        .set(
                            DreamMaintenanceProgress.maintained_at.to(CurrentTimestamp)
                        )
                        .where(DreamMaintenanceProgress.path.eq(relative))
                    )
            for relative in deleted:
                _ = await tx.execute(
                    delete(DreamMaintenanceProgress).where(
                        DreamMaintenanceProgress.path.eq(relative)
                    )
                )

    async def _mark_maintained(self, batch: list[tuple[str, str]]) -> None:
        """Record unchanged files as maintained at their current content."""
        await self._record_progress(batch, deleted=())


class KindDispatchingDreamExecutor:
    """Route claimed runs to the executor matching their kind."""

    def __init__(self, routes: Mapping[str, DreamRunExecutor]) -> None:
        self.routes: Mapping[str, DreamRunExecutor] = routes

    async def __call__(
        self,
        run: DreamRun[Fetched],
        *,
        logger: Logger,
    ) -> DreamRunExecutionResult:
        route = self.routes.get(run.kind)
        if route is None:
            return DreamRunExecutionResult(
                status="failed",
                error=f"no executor registered for run kind {run.kind}",
            )
        return await route(run, logger=logger)


class DreamingService:
    """Stateful orchestration surface for Dream run request scheduling."""

    def __init__(  # noqa: PLR0913 - keyword-only tuning knobs, all defaulted
        self,
        database: Database,
        *,
        settle_window: timedelta = _DREAM_SETTLE_WINDOW,
        max_messages_per_run: PositiveInt = _DREAM_MAX_MESSAGES,
        tracer: Tracer | None = None,
        workspace_root: Path | None = None,
        maintenance_interval: timedelta = _MAINTENANCE_INTERVAL,
        mutation_coordinator: DreamingMutationCoordinator | None = None,
    ) -> None:
        self.database: Database = database
        self.settle_window: timedelta = settle_window
        self.max_messages_per_run: PositiveInt = max_messages_per_run
        self.tracer: Tracer | None = tracer
        self.workspace_root: Path | None = workspace_root
        self.maintenance_interval: timedelta = maintenance_interval
        self.mutation_coordinator: DreamingMutationCoordinator | None = (
            mutation_coordinator
            if mutation_coordinator is not None
            else DreamingMutationCoordinator(database, workspace_root)
            if workspace_root is not None
            else None
        )
        self._immediate_assimilation_requests: set[UUID] = set()
        self._orchestration_lock: asyncio.Lock = asyncio.Lock()

    def request_immediate_assimilation(self, conversation_id: UUID) -> None:
        """Mark one active Conversation for post-turn settle-window bypass."""
        self._immediate_assimilation_requests.add(conversation_id)

    def consume_immediate_assimilation_request(self, conversation_id: UUID) -> bool:
        """Consume one collapsed post-turn immediate-assimilation request."""
        if conversation_id not in self._immediate_assimilation_requests:
            return False
        self._immediate_assimilation_requests.remove(conversation_id)
        return True

    async def rebuild_conversation_memory(
        self,
        *,
        logger: Logger,
        now: datetime,
    ) -> ConversationMemoryRebuildResult:
        """Tombstone current Message-only Topics before replaying Conversations."""
        coordinator = self.mutation_coordinator
        if self.workspace_root is None or coordinator is None:
            message = "Memory rebuild requires a configured workspace"
            raise ConversationMemoryRebuildError(message)

        async with self._orchestration_lock:
            async with self.database.transaction() as transaction:
                active_run = await transaction.fetch_one_or_none(
                    select(DreamRun)
                    .where(DreamRun.status.in_("queued", "running"))
                    .order_by(DreamRun.created_at.asc())
                    .limit(1)
                )
            if active_run is not None:
                raise ConversationMemoryRebuildBusyError(active_run.id)

            scan = await MemoryWorkspace(self.workspace_root).scan()
            conversation_topics = [
                topic
                for topic in scan.topics
                if (
                    evidence_uris := set(topic.evidence)
                    | set(_EVIDENCE_URI_PATTERN.findall(topic.body))
                )
                and all(uri.startswith("tether://message/") for uri in evidence_uris)
            ]
            async with self.database.transaction(mode="immediate") as transaction:
                rebuild_run = await transaction.execute(
                    insert(
                        DreamRun(
                            conversation_id=maintenance_group_run_id(
                                "conversation-memory-rebuild"
                            ),
                            kind="rebuild",
                            status="running",
                            evidence_start_seq=1,
                            evidence_end_seq=1,
                        )
                    ).returning()
                )
            try:
                await self._tombstone_conversation_topics(
                    coordinator,
                    conversation_topics,
                    rebuild_run,
                )
            except Exception as error:
                _ = await self.complete_run(
                    rebuild_run.id,
                    status="failed",
                    logger=logger,
                    now=now,
                    error=f"{type(error).__name__}: {error}",
                )
                if isinstance(error, ConversationMemoryRebuildError):
                    raise
                message = f"Memory rebuild failed: {type(error).__name__}: {error}"
                raise ConversationMemoryRebuildError(message) from error

            async with self.database.transaction(mode="immediate") as transaction:
                cursors = await transaction.fetch_all(
                    select(DreamConversationCursor).all()
                )
                _ = await transaction.execute(
                    delete(DreamConversationCursor).where(
                        DreamConversationCursor.last_assimilated_seq.gte(0)
                    )
                )
                for topic in conversation_topics:
                    relative_path = topic.path.relative_to(
                        self.workspace_root
                    ).as_posix()
                    _ = await transaction.execute(
                        delete(DreamMaintenanceProgress).where(
                            DreamMaintenanceProgress.path.eq(relative_path)
                        )
                    )
            _ = await self.complete_run(
                rebuild_run.id,
                status="success",
                logger=logger,
                now=now,
            )
        queued_runs = await self.queue_pending_manual_runs(logger=logger, now=now)
        logger.info(
            "Prepared Conversation Memory rebuild",
            rebuild_run_id=str(rebuild_run.id),
            preserved_topics=len(scan.topics) - len(conversation_topics),
            queued_runs=len(queued_runs),
            reset_cursors=len(cursors),
            tombstoned_topics=len(conversation_topics),
        )
        return ConversationMemoryRebuildResult(
            preserved_topics=len(scan.topics) - len(conversation_topics),
            queued_runs=len(queued_runs),
            rebuild_run_id=rebuild_run.id,
            reset_cursors=len(cursors),
            tombstoned_topics=len(conversation_topics),
        )

    async def queue_manual_run(
        self,
        conversation_id: UUID,
        *,
        logger: Logger,
        now: datetime,
    ) -> DreamRun[Fetched] | None:
        """Queue a Dream run that bypasses the settling delay."""
        return await self._queue_run(
            conversation_id,
            kind="manual",
            logger=logger,
            explicit=True,
            now=now,
        )

    async def queue_assimilation_run(
        self,
        conversation_id: UUID,
        *,
        logger: Logger,
        now: datetime,
    ) -> DreamRun[Fetched] | None:
        """Queue an automatic assimilation run if evidence has settled."""
        return await self._queue_run(
            conversation_id,
            kind="assimilation",
            logger=logger,
            explicit=False,
            now=now,
        )

    async def queue_pending_manual_runs(
        self,
        *,
        logger: Logger,
        now: datetime,
    ) -> list[DreamRun[Fetched]]:
        """Queue an immediate manual run for every conversation with new evidence.

        Backs the browser's "Dream now" action: instant, settle-window-free,
        and conversation-agnostic. Conversations whose cursor already sits at
        their latest message resolve no window and are skipped.
        """
        return await self._queue_for_all_conversations(
            kind="manual",
            explicit=True,
            logger=logger,
            now=now,
        )

    async def queue_settled_assimilation_runs(
        self,
        *,
        logger: Logger,
        now: datetime,
    ) -> list[DreamRun[Fetched]]:
        """Scan every conversation and queue runs for settled evidence.

        The periodic backstop for post-turn queueing: without it, evidence
        only assimilates when the next chat turn happens, so a quiet stretch
        leaves Memory stale indefinitely.
        """
        return await self._queue_for_all_conversations(
            kind="assimilation",
            explicit=False,
            logger=logger,
            now=now,
        )

    async def queue_maintenance_runs(
        self,
        *,
        logger: Logger,
        now: datetime,
        force: bool = False,
    ) -> list[DreamRun[Fetched]]:
        """Queue semantic maintenance for every due Topic group.

        Maintenance revises current Memory without consuming new Evidence, so
        it yields to any conversation with an unassimilated window. Every group,
        including one-file groups, becomes due after `maintenance_interval`.
        """
        if self.workspace_root is None:
            return []
        queued: list[DreamRun[Fetched]] = []
        for folder, conversation_id in await self._maintenance_groups():
            run = await self._queue_maintenance_run(
                folder,
                conversation_id,
                logger=logger,
                now=now,
                force=force,
            )
            if run is not None:
                queued.append(run)
        return queued

    async def _tombstone_conversation_topics(
        self,
        coordinator: DreamingMutationCoordinator,
        topics: list[MemoryWorkspaceTopic],
        rebuild_run: DreamRun[Fetched],
    ) -> None:
        """Record every destructive rebuild change before cursor reset."""
        assert self.workspace_root is not None
        async with coordinator.mutation_scope():
            for topic in topics:
                await AsyncPath(topic.path).unlink()
                relative_path = topic.path.relative_to(self.workspace_root)
                tool_call_id = str(
                    uuid5(
                        NAMESPACE_URL,
                        f"{rebuild_run.id}:{relative_path.as_posix()}",
                    )
                )
                _ = await coordinator.record_mutation(
                    run_id=rebuild_run.id,
                    tool_call_id=tool_call_id,
                    actor="dream",
                    operation="delete",
                    workspace_path=topic.path,
                    payload="Conversation Memory rebuild",
                )
                settlement = await coordinator.settle(
                    rebuild_run.id,
                    tool_call_id,
                )
                if not settlement.acknowledged:
                    message = settlement.error or "mutation was not acknowledged"
                    raise ConversationMemoryRebuildError(message)

    async def _maintenance_groups(self) -> list[tuple[str, UUID]]:
        """List current workspace groups and their maintenance run identities.

        Conversation folders use their own id. Vertical folders and the
        workspace root receive stable synthetic ids.
        """
        assert self.workspace_root is not None
        scan = await MemoryWorkspace(self.workspace_root).scan()
        counts: dict[str, int] = {}
        for topic in scan.topics:
            relative = topic.path.relative_to(self.workspace_root)
            folder = relative.parts[0] if len(relative.parts) > 1 else ""
            counts[folder] = counts.get(folder, 0) + 1
        groups: list[tuple[str, UUID]] = []
        for folder, count in counts.items():
            if count < _MAINTENANCE_MIN_FILES:
                continue
            try:
                conversation_id: UUID = UUID(folder)
            except ValueError:
                conversation_id = maintenance_group_run_id(folder)
            groups.append((folder, conversation_id))
        return sorted(groups)

    async def queue_maintenance_run(
        self,
        conversation_id: UUID,
        *,
        logger: Logger,
        now: datetime,
    ) -> DreamRun[Fetched] | None:
        """Queue one forced maintenance run for a conversation folder."""
        return await self._queue_maintenance_run(
            str(conversation_id),
            conversation_id,
            logger=logger,
            now=now,
            force=True,
        )

    async def _queue_maintenance_run(
        self,
        folder: str,
        conversation_id: UUID,
        *,
        logger: Logger,
        now: datetime,
        force: bool,
    ) -> DreamRun[Fetched] | None:
        """Queue a maintenance run when the folder group is eligible."""
        async with self._orchestration_lock:
            return await self._queue_maintenance_run_locked(
                folder,
                conversation_id,
                logger=logger,
                now=now,
                force=force,
            )

    async def _queue_maintenance_run_locked(
        self,
        folder: str,
        conversation_id: UUID,
        *,
        logger: Logger,
        now: datetime,
        force: bool,
    ) -> DreamRun[Fetched] | None:
        """Resolve and insert maintenance while rebuild preparation is excluded."""
        if self.workspace_root is None:
            return None
        folder_path = self.workspace_root / folder if folder else self.workspace_root
        topics = [
            path
            for path in folder_path.glob("*.md")
            if not path.name.startswith((".", "~"))
        ]
        if len(topics) < _MAINTENANCE_MIN_FILES:
            return None
        pending_window = await self._resolve_window(
            conversation_id,
            explicit=True,
            now=now,
        )
        if pending_window is not None:
            return None
        if not force and not await self._maintenance_due(conversation_id, now=now):
            return None
        return await self._insert_maintenance_run(
            conversation_id,
            logger=logger,
        )

    async def _maintenance_due(
        self,
        conversation_id: UUID,
        *,
        now: datetime,
    ) -> bool:
        """Return whether the interval has elapsed since last completion."""
        async with self.database.transaction() as tx:
            last = await tx.fetch_one_or_none(
                select(DreamRun)
                .where(DreamRun.conversation_id.eq(conversation_id))
                .where(DreamRun.kind.eq("maintenance"))
                .where(DreamRun.status.in_("success", "no_op"))
                .order_by(DreamRun.created_at.desc())
                .limit(1)
            )
        if last is None:
            return True
        finished = last.completed_at or last.created_at
        return _as_utc(now) - _as_utc(finished) >= self.maintenance_interval

    async def _insert_maintenance_run(
        self,
        conversation_id: UUID,
        *,
        logger: Logger,
    ) -> DreamRun[Fetched] | None:
        """Insert a maintenance run unless one is already active."""
        async with self.database.transaction() as tx:
            cursor = await tx.fetch_one_or_none(
                select(DreamConversationCursor).where(
                    DreamConversationCursor.conversation_id.eq(conversation_id)
                )
            )
        seq = max(cursor.last_assimilated_seq if cursor is not None else 0, 1)

        async with self.database.transaction(mode="immediate") as tx:
            active = await tx.fetch_one_or_none(
                select(DreamRun)
                .where(DreamRun.conversation_id.eq(conversation_id))
                .where(DreamRun.status.in_("queued", "running"))
                .order_by(DreamRun.created_at.desc())
                .limit(1)
            )
            if active is not None:
                logger.info(
                    "Maintenance skipped: active Dream run",
                    conversation_id=str(conversation_id),
                    run_id=str(active.id),
                )
                return None
            run = await tx.execute(
                insert(
                    DreamRun(
                        conversation_id=conversation_id,
                        kind="maintenance",
                        status="queued",
                        evidence_start_seq=seq,
                        evidence_end_seq=seq,
                    )
                ).returning()
            )
        logger.info(
            "Queued Dream run",
            conversation_id=str(conversation_id),
            run_id=str(run.id),
            kind="maintenance",
        )
        return run

    async def scan_forever(
        self, *, interval_seconds: float = 60.0, logger: Logger
    ) -> None:
        """Queue assimilation runs for settled evidence on a fixed interval.

        The correctness backstop for post-turn queueing: assimilation used to
        depend on the *next* chat turn noticing settled evidence, so any quiet
        stretch left Memory stale indefinitely. A failed pass is logged and
        swallowed; the next tick retries.
        """
        await run_reconcile_loop(
            lambda: self.queue_settled_assimilation_runs(
                logger=logger,
                now=datetime.now(UTC),
            ),
            interval_seconds=interval_seconds,
            initial_delay_seconds=interval_seconds,
            logger=logger,
            failure_message="Dream assimilation scan failed",
        )

    async def maintenance_forever(
        self, *, interval_seconds: float = 86_400.0, logger: Logger
    ) -> None:
        """Queue periodic semantic maintenance for current Topic groups."""
        await run_reconcile_loop(
            lambda: self.queue_maintenance_runs(
                logger=logger,
                now=datetime.now(UTC),
            ),
            interval_seconds=interval_seconds,
            initial_delay_seconds=interval_seconds,
            logger=logger,
            failure_message="Dream maintenance scan failed",
        )

    async def _queue_for_all_conversations(
        self,
        *,
        kind: DreamRunKind,
        explicit: bool,
        logger: Logger,
        now: datetime,
    ) -> list[DreamRun[Fetched]]:
        async with self.database.transaction() as tx:
            rows = await tx.fetch_all(select(Conversation).all())
        queued: list[DreamRun[Fetched]] = []
        for conversation in rows:
            run = await self._queue_run(
                conversation.id,
                kind=kind,
                logger=logger,
                explicit=explicit,
                now=now,
            )
            if run is not None:
                queued.append(run)
        return queued

    async def claim_next_run(
        self,
        *,
        logger: Logger,
        now: datetime | None = None,
    ) -> DreamRun[Fetched] | None:
        """Claim the oldest queued run and transition it to `running`."""
        resolved_now = _as_utc(now or datetime.now(UTC))

        async def _claim(tx: Transaction) -> DreamRun[Fetched] | None:
            candidate = await tx.fetch_one_or_none(
                select(DreamRun)
                .where(DreamRun.status.eq("queued"))
                .order_by(DreamRun.created_at.asc())
                .limit(1)
            )
            if candidate is None:
                return None
            matched = await tx.execute(
                update(DreamRun)
                .set(DreamRun.status.to("running"))
                .set(DreamRun.error.to(None))
                .set(DreamRun.updated_at.to(resolved_now))
                .where(DreamRun.id.eq(candidate.id))
                .where(DreamRun.status.eq("queued"))
            )
            if matched == 0:
                return None
            claimed = await tx.fetch_one_or_none(
                select(DreamRun).where(DreamRun.id.eq(candidate.id))
            )
            if claimed is None:
                return None
            logger.info(
                "Dream run claimed",
                run_id=str(claimed.id),
                conversation_id=str(claimed.conversation_id),
            )
            return claimed

        async with (
            self._orchestration_lock,
            self.database.transaction(mode="immediate") as tx,
        ):
            return await _claim(tx)

    async def complete_run(
        self,
        run_id: UUID7,
        *,
        status: DreamRunTerminalStatus,
        logger: Logger,
        now: datetime | None = None,
        error: str | None = None,
    ) -> DreamRun[Fetched]:
        """Mark a run terminal and persist cursor progress on success or no-op."""
        resolved_now = _as_utc(now or datetime.now(UTC))

        async def _complete(tx: Transaction) -> DreamRun[Fetched]:
            run = await tx.fetch_one_or_none(
                select(DreamRun).where(DreamRun.id.eq(run_id))
            )
            if run is None:
                raise DreamRunNotFoundError(run_id)
            if run.status in {"success", "no_op", "failed"}:
                logger.info(
                    "Dream run completion replayed",
                    run_id=str(run.id),
                    status=run.status,
                )
                return run
            _ = await tx.execute(
                update(DreamRun)
                .set(DreamRun.status.to(status))
                .set(DreamRun.error.to(error))
                .set(DreamRun.completed_at.to(resolved_now))
                .set(DreamRun.updated_at.to(resolved_now))
                .where(DreamRun.id.eq(run_id))
            )
            if status in {"success", "no_op"} and run.kind not in {
                "maintenance",
                "rebuild",
            }:
                await self._advance_cursor(
                    tx, run.conversation_id, run.evidence_end_seq
                )
            refreshed = await tx.fetch_one_or_none(
                select(DreamRun).where(DreamRun.id.eq(run_id))
            )
            if refreshed is None:
                raise DreamRunNotFoundError(run_id)
            return refreshed

        async with self.database.transaction(mode="immediate") as tx:
            run = await _complete(tx)
        assert run is not None
        logger.info(
            "Dream run completed",
            run_id=str(run.id),
            status=run.status,
            conversation_id=str(run.conversation_id),
        )
        return run

    async def _queue_run(
        self,
        conversation_id: UUID,
        *,
        logger: Logger,
        explicit: bool,
        kind: DreamRunKind,
        now: datetime,
    ) -> DreamRun[Fetched] | None:
        """Insert or reuse a run for one conversation, or noop if nothing ready."""
        async with self._orchestration_lock:
            return await self._queue_run_locked(
                conversation_id,
                logger=logger,
                explicit=explicit,
                kind=kind,
                now=now,
            )

    async def _queue_run_locked(
        self,
        conversation_id: UUID,
        *,
        logger: Logger,
        explicit: bool,
        kind: DreamRunKind,
        now: datetime,
    ) -> DreamRun[Fetched] | None:
        """Resolve and insert a run while rebuild preparation is excluded."""
        window = await self._resolve_window(
            conversation_id,
            explicit=explicit,
            now=now,
        )
        if window is None:
            return None

        async def _queue(tx: Transaction) -> DreamRun[Fetched]:
            active = await tx.fetch_one_or_none(
                select(DreamRun)
                .where(DreamRun.conversation_id.eq(conversation_id))
                .where(DreamRun.status.in_("queued", "running"))
                .order_by(DreamRun.created_at.desc())
                .limit(1)
            )
            if active is not None:
                if (
                    active.kind == kind
                    and active.evidence_start_seq == window.start_seq
                    and active.evidence_end_seq == window.end_seq
                ):
                    logger.info(
                        "Reusing existing active Dream run",
                        conversation_id=str(conversation_id),
                        run_id=str(active.id),
                    )
                    return active
                return active
            run = await tx.execute(
                insert(
                    DreamRun(
                        conversation_id=conversation_id,
                        kind=kind,
                        status="queued",
                        evidence_start_seq=window.start_seq,
                        evidence_end_seq=window.end_seq,
                    )
                ).returning()
            )
            logger.info(
                "Queued Dream run",
                conversation_id=str(conversation_id),
                run_id=str(run.id),
                kind=kind,
                start_seq=run.evidence_start_seq,
                end_seq=run.evidence_end_seq,
            )
            return run

        async with self.database.transaction(mode="immediate") as tx:
            return await _queue(tx)

    async def _resolve_window(
        self,
        conversation_id: UUID,
        *,
        explicit: bool,
        now: datetime,
    ) -> _AssimilationWindow | None:
        """Return the next unprocessed evidence window, or `None` if not ready."""
        now_utc = _as_utc(now)

        async with self.database.transaction() as tx:
            cursor = await tx.fetch_one_or_none(
                select(DreamConversationCursor).where(
                    DreamConversationCursor.conversation_id.eq(conversation_id)
                )
            )
            start_seq = cursor.last_assimilated_seq + 1 if cursor is not None else 1
            latest = await tx.fetch_one_or_none(
                select(Message)
                .where(Message.conversation_id.eq(conversation_id))
                .order_by(Message.seq.desc())
                .limit(1)
            )
            settled_end_seq = 0 if latest is None else latest.seq
            if latest is not None and latest.turn_id is not None:
                latest_turn = await tx.fetch_one_or_none(
                    select(ConversationTurn).where(
                        ConversationTurn.id.eq(latest.turn_id)
                    )
                )
                if latest_turn is not None and latest_turn.status not in {
                    "succeeded",
                    "failed",
                    "cancelled",
                }:
                    first_unsettled = await tx.fetch_one(
                        select(Message)
                        .where(Message.turn_id.eq(latest_turn.id))
                        .order_by(Message.seq.asc())
                        .limit(1)
                    )
                    settled_end_seq = first_unsettled.seq - 1
            candidate_messages = await tx.fetch_all(
                select(Message)
                .where(Message.conversation_id.eq(conversation_id))
                .where(Message.seq.gte(start_seq))
                .where(Message.seq.lte(settled_end_seq))
                .where(Message.role.in_("user", "assistant"))
                .order_by(Message.seq.asc())
            )
        if latest is None or not candidate_messages:
            return None
        supporting_ids = await fetch_claim_supporting_message_ids(
            self.database,
            candidate_messages,
        )
        supporting_messages = [
            message for message in candidate_messages if message.id in supporting_ids
        ]
        if not supporting_messages:
            return None
        latest_supporting = supporting_messages[-1]
        if (
            not explicit
            and latest_supporting.role == "user"
            and now_utc - _as_utc(latest_supporting.created_at) < self.settle_window
        ):
            return None
        proposed_end = settled_end_seq
        max_end = (
            window_size(self.max_messages_per_run, start_seq)
            if self.max_messages_per_run > 0
            else proposed_end
        )
        end_seq = min(proposed_end, max_end)
        if end_seq < start_seq:
            return None
        return _AssimilationWindow(start_seq=start_seq, end_seq=end_seq)

    async def _advance_cursor(
        self,
        tx: Transaction,
        conversation_id: UUID,
        through_seq: int,
    ) -> None:
        """Advance cursor to `through_seq` if this run includes new evidence."""
        cursor = await tx.fetch_one_or_none(
            select(DreamConversationCursor).where(
                DreamConversationCursor.conversation_id.eq(conversation_id)
            )
        )
        if cursor is None:
            _ = await tx.execute(
                insert(
                    DreamConversationCursor(
                        conversation_id=conversation_id,
                        last_assimilated_seq=through_seq,
                    )
                )
            )
            return
        if cursor.last_assimilated_seq >= through_seq:
            return
        _ = await tx.execute(
            update(DreamConversationCursor)
            .set(DreamConversationCursor.last_assimilated_seq.to(through_seq))
            .where(DreamConversationCursor.conversation_id.eq(conversation_id))
        )


@dataclass(frozen=True, slots=True)
class DreamingWorkerConfig:
    """Tuning knobs for the Dreaming worker loop."""

    poll_interval_seconds: float = 5.0


class DreamingWorker:
    """Execute queued Dream runs via an injected callback."""

    def __init__(
        self,
        dreaming_service: DreamingService,
        executor: DreamRunExecutor,
        logger: Logger,
        *,
        config: DreamingWorkerConfig | None = None,
    ) -> None:
        self.dreaming_service: DreamingService = dreaming_service
        self.executor: DreamRunExecutor = executor
        self.logger: Logger = logger
        self.config: DreamingWorkerConfig = config or DreamingWorkerConfig()

    async def run_once(self) -> DreamRun[Fetched] | None:
        """Process one queued Dream run and settle it terminally if one exists."""
        run = await self.dreaming_service.claim_next_run(logger=self.logger)
        if run is None:
            return None
        try:
            result = await self.executor(run, logger=self.logger)
        except Exception as error:
            self.logger.exception(
                "Dream run callback raised",
                run_id=str(run.id),
                error=str(error),
            )
            result = DreamRunExecutionResult(
                status="failed",
                error=f"{type(error).__name__}: {error}",
            )
        if result.status not in {"success", "no_op", "failed"}:
            self.logger.warning(
                "Dream run callback returned non-terminal status",
                run_id=str(run.id),
                status=result.status,
            )
            result = DreamRunExecutionResult(
                status="failed", error="non-terminal callback result"
            )
        return await self.dreaming_service.complete_run(
            run.id,
            status=result.status,
            logger=self.logger,
            error=result.error,
        )

    async def run_forever(self) -> None:
        """Continuously claim and complete Dream runs until cancellation."""
        await asyncio.sleep(self.config.poll_interval_seconds)
        while True:
            made_progress = False
            while True:
                completed = await self.run_once()
                if completed is None:
                    break
                made_progress = True
            if not made_progress:
                await asyncio.sleep(self.config.poll_interval_seconds)


def window_size(max_messages_per_run: int, start_seq: PositiveInt) -> int:
    """Compute the upper bound from a bounded run window."""
    if max_messages_per_run <= 0:
        return start_seq
    return start_seq + max_messages_per_run - 1


__all__ = [
    "ConversationMemoryRebuildBusyError",
    "ConversationMemoryRebuildError",
    "ConversationMemoryRebuildResult",
    "ConversationWindowDreamingExecutor",
    "DreamRunExecutionResult",
    "DreamRunExecutor",
    "DreamRunNotFoundError",
    "DreamingMutationCoordinator",
    "DreamingService",
    "DreamingWorker",
    "DreamingWorkerConfig",
    "DreamingWorkspaceReconcileResult",
    "MaintenanceDreamingAgent",
    "MaintenanceDreamingExecutor",
]
