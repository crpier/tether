"""Dreaming orchestration primitives and cursor math (host-owned conversations)."""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol, cast
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
    insert,
    select,
    update,
)
from yaml import dump as yaml_dump
from yaml import safe_load

from tether.conversation_store import Message
from tether.conversations import ConversationService
from tether.dreaming_store import (
    DreamConversationCursor,
    DreamingMutation,
    DreamingMutationActor,
    DreamingMutationOperation,
    DreamingWorkspaceFile,
    DreamRun,
    DreamRunKind,
    DreamRunTerminalStatus,
)
from tether.memory_workspace import MemoryWorkspace
from tether.structured_logging import Logger
from tether.tool_runtime import TOOL_AUTH_HEADER

_DREAM_SETTLE_WINDOW = timedelta(minutes=20)
"""Delay before an auto run can consume new user-level evidence."""
_DREAM_MAX_MESSAGES = 200
"""Default max transcript rows per Dream run."""
_FRONTMATTER_SEPARATOR = "\n---\n"
_FRONTMATTER_PART_COUNT = 2

_ACK_PATH = "/internal/dream-runs/{run_id}/mutations/{tool_call_id}/ack"
"""Dream mutation callback route on the same host."""


@dataclass(frozen=True, slots=True)
class DreamingWorkspaceReconcileResult:
    """Counts produced by one reconciliation pass."""

    updated_files: int
    tombstones: int


class DreamRunNotFoundError(Exception):
    """Raised when a run cannot be resolved for completion."""


@dataclass(frozen=True, slots=True)
class _AssimilationWindow:
    start_seq: int
    end_seq: int


def _as_utc(value: datetime) -> datetime:
    """Read legacy-aware timestamps as UTC-aware datetimes."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


class DreamingMutationCoordinator:
    """Coordinate persisted mutation attempts and workspace reconciliation."""

    def __init__(self, database: Database, workspace_root: Path) -> None:
        self.database: Database = database
        self.workspace_root: Path = workspace_root

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

    async def fetch_mutation(
        self,
        run_id: UUID,
        tool_call_id: str,
    ) -> DreamingMutation[Fetched] | None:
        """Read one persisted attempt for the given run and tool-call."""
        async with self.database.transaction() as tx:
            return await tx.fetch_one_or_none(
                select(DreamingMutation)
                .where(DreamingMutation.run_id.eq(run_id))
                .where(DreamingMutation.tool_call_id.eq(tool_call_id))
            )

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
        async with self.database.transaction() as tx:
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
                    .set(
                        DreamingMutation.workspace_path.to(
                            self._to_relative_path(self.workspace_root, workspace_path)
                        )
                    )
                    .set(DreamingMutation.payload.to(payload))
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
                        workspace_path=self._to_relative_path(
                            self.workspace_root,
                            workspace_path,
                        ),
                        payload=payload,
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

    async def reconcile_workspace(
        self,
        *,
        logger: Logger,
    ) -> DreamingWorkspaceReconcileResult:
        """Reconcile every discoverable file and mark missing ones as tombstones."""
        result = await MemoryWorkspace(self.workspace_root).scan()
        current_topics: dict[str, str] = {}
        for topic in result.topics:
            path = AsyncPath(topic.path)
            text = await path.read_text(encoding="utf-8")
            current_topics[self._to_relative_path(self.workspace_root, topic.path)] = (
                text
            )

        async with self.database.transaction(mode="immediate") as tx:
            current = await tx.fetch_all(
                select(DreamingWorkspaceFile).where(
                    DreamingWorkspaceFile.path.is_not_null()
                )
            )
            current_by_path = {row.path: row for row in current}
            updated = 0
            for relative_path, text in current_topics.items():
                await self._upsert_file_state(
                    tx,
                    workspace_path=relative_path,
                    content_hash=self._content_hash(text),
                    content=text,
                    is_tombstone=False,
                    mutation_actor="human_external",
                )
                if (
                    relative_path not in current_by_path
                    or current_by_path[relative_path].content_hash
                    != self._content_hash(text)
                    or current_by_path[relative_path].is_tombstone == 1
                ):
                    updated += 1
            tombstones = 0
            for relative_path, existing in current_by_path.items():
                if relative_path not in current_topics and existing.is_tombstone == 0:
                    await self._upsert_file_state(
                        tx,
                        workspace_path=relative_path,
                        content_hash=existing.content_hash,
                        content=None,
                        is_tombstone=True,
                        source_run_id=existing.source_run_id,
                        source_tool_call_id=existing.source_tool_call_id,
                        mutation_actor=existing.actor,
                    )
                    tombstones += 1
            logger.debug(
                "Reconciled dreaming workspace",
                updated=updated,
                tombstones=tombstones,
            )
            return DreamingWorkspaceReconcileResult(
                updated_files=updated,
                tombstones=tombstones,
            )

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
        pending = await self.mutation_coordinator.fetch_mutation(
            run.id,
            tool_call_id,
        )
        if pending is not None and pending.status != "acknowledged":
            acknowledged, error = await self.mutation_acknowledger(run.id, tool_call_id)
            logger.info(
                "Dream run mutation acknowledged after prior execution",
                run_id=str(run.id),
                workspace_path=str(workspace_path),
                acknowledged=acknowledged,
                error=error,
            )
            return DreamRunExecutionResult(
                status="success" if acknowledged else "failed",
                error=None if acknowledged else error,
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
        acknowledged, error = await self.mutation_acknowledger(run.id, tool_call_id)
        return DreamRunExecutionResult(
            status="success" if acknowledged else "failed",
            error=None if acknowledged else error,
        )

    async def _write_dream_document(
        self,
        *,
        run: DreamRun[Fetched],
        evidence: list[Message[Fetched]],
        workspace_path: AsyncPath,
        logger: Logger,
        curated_body: str | None = None,
    ) -> AsyncPath | None:
        """Write deterministic dream evidence and report no-op when unchanged."""
        if not await workspace_path.parent.exists():
            await workspace_path.parent.mkdir(parents=True, exist_ok=True)

        evidence_uris = self._message_uris(evidence)
        existing_document: str | None = None
        existing_frontmatter: dict[str, Any] = {}
        if await workspace_path.exists():
            existing_document = await workspace_path.read_text(encoding="utf-8")
            existing_frontmatter, _ = self._split_frontmatter(existing_document)
            raw_evidence = existing_frontmatter.get("evidence")
            existing_evidence: list[object] = []
            if isinstance(raw_evidence, list):
                existing_evidence = cast("list[object]", raw_evidence)
            existing_uris = set(self._message_uris_from_frontmatter(existing_evidence))
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
        run: DreamRun[Fetched], evidence: list[Message[Fetched]]
    ) -> str:
        """Render exact bounded Evidence for unattended Claim curation."""
        evidence_blocks = "\n\n".join(
            "\n".join(
                (
                    f"seq: {message.seq}",
                    f"role: {message.role}",
                    f"created_at: {message.created_at.isoformat()}",
                    f"uri: tether://message/{message.id}",
                    "content:",
                    message.content,
                )
            )
            for message in evidence
        )
        return f"""Curate durable, user-centric Claims from this bounded Conversation Evidence.

Rules:
- Only user Messages support Claims about the user.
- Assistant, reasoning, and tool Messages are context only.
- Omit transient requests, implementation chatter, and assistant-authored facts.
- Return Markdown only, grouped under `##` Topic headings.
- Every Claim is one `- ` bullet with an inline `[source](tether://message/<id>)` citation.
- Use only exact Message URIs below. Preserve uncertainty and corrections.
- Return `NO_CHANGES` when no durable Claim is supported.

run_id: {run.id}
evidence_start_seq: {run.evidence_start_seq}
evidence_end_seq: {run.evidence_end_seq}

{evidence_blocks}
"""

    @staticmethod
    def _validate_curated_body(
        curated_body: str, evidence: list[Message[Fetched]]
    ) -> str | None:
        """Refuse citations that cannot support Claims in this bounded run."""
        user_evidence_uris = {
            f"tether://message/{message.id}"
            for message in evidence
            if message.role == "user"
        }
        claim_lines = [
            line for line in curated_body.splitlines() if line.startswith("- ")
        ]
        if not claim_lines or any(
            not re.search(r"tether://message/[0-9A-Za-z-]+", claim)
            for claim in claim_lines
        ):
            return "every curated Claim must cite bounded user Evidence"
        cited_uris = set(re.findall(r"tether://message/[0-9A-Za-z-]+", curated_body))
        unsupported = sorted(cited_uris - user_evidence_uris)
        if unsupported:
            return "curated Claim cites outside bounded user Evidence: " + ", ".join(
                unsupported
            )
        return None

    @staticmethod
    def _render_curated_document(
        run: DreamRun[Fetched],
        evidence: list[Message[Fetched]],
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
            "evidence": ConversationWindowDreamingExecutor._message_uris(evidence),
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
        evidence: list[Message[Fetched]],
        existing_document: str | None,
        previous_frontmatter: dict[str, Any],
    ) -> str:
        """Render a cumulative, deterministic draft from previous corpus + new window."""
        new_uris = self._message_uris(evidence)
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
    def _message_uris(messages: list[Message[Fetched]]) -> list[str]:
        return [f"tether://message/{message.id}" for message in messages]

    @staticmethod
    def _message_uris_from_frontmatter(raw_values: list[object]) -> list[str]:
        return [
            raw_value
            for raw_value in raw_values
            if isinstance(raw_value, str) and raw_value.startswith("tether://message/")
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
        evidence: list[Message[Fetched]],
    ) -> str:
        lines = [
            "## Dream slice",
            f"- run_id: {run.id}",
            f"- evidence_start_seq: {run.evidence_start_seq}",
            f"- evidence_end_seq: {run.evidence_end_seq}",
            f"- kind: {run.kind}",
            "- messages:",
            *(
                f"  - {message.seq} {message.role} {message.created_at.isoformat()} tether://message/{message.id}"
                for message in evidence
            ),
            "",
            *(message.content for message in evidence),
            "",
        ]
        return "\n".join(lines)

    @staticmethod
    def _contains_run_payload(document: str, run_id: UUID) -> bool:
        return re.search(rf"run_id:\s*{re.escape(str(run_id))}", document) is not None

    async def _fetch_evidence(self, run: DreamRun[Fetched]) -> list[Message[Fetched]]:
        """Return persisted messages bounded by run window bounds."""
        if run.evidence_end_seq < run.evidence_start_seq:
            return []
        messages = await self.conversation_service.fetch_messages(
            run.conversation_id,
            before_seq=run.evidence_end_seq + 1,
            limit=run.evidence_end_seq - run.evidence_start_seq + 1,
        )
        return [
            message for message in messages if message.seq >= run.evidence_start_seq
        ]


class DreamingService:
    """Stateful orchestration surface for Dream run request scheduling."""

    def __init__(
        self,
        database: Database,
        *,
        settle_window: timedelta = _DREAM_SETTLE_WINDOW,
        max_messages_per_run: PositiveInt = _DREAM_MAX_MESSAGES,
        tracer: Tracer | None = None,
    ) -> None:
        self.database: Database = database
        self.settle_window: timedelta = settle_window
        self.max_messages_per_run: PositiveInt = max_messages_per_run
        self.tracer: Tracer | None = tracer

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

        async with self.database.transaction(mode="immediate") as tx:
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
            if status in {"success", "no_op"}:
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
            latest_user = await tx.fetch_one_or_none(
                select(Message)
                .where(Message.conversation_id.eq(conversation_id))
                .where(Message.role.eq("user"))
                .order_by(Message.seq.desc())
                .limit(1)
            )
            if latest_user is None:
                return None
            if not explicit and (
                now_utc - _as_utc(latest_user.created_at) < self.settle_window
            ):
                return None
            latest = await tx.fetch_one_or_none(
                select(Message)
                .where(Message.conversation_id.eq(conversation_id))
                .order_by(Message.seq.desc())
                .limit(1)
            )
            if latest is None:
                return None
            start_seq = cursor.last_assimilated_seq + 1 if cursor is not None else 1
            if latest_user.seq < start_seq:
                return None
            proposed_end = latest.seq
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
    "ConversationWindowDreamingExecutor",
    "DreamRunExecutionResult",
    "DreamRunExecutor",
    "DreamRunNotFoundError",
    "DreamingMutationCoordinator",
    "DreamingService",
    "DreamingWorker",
    "DreamingWorkerConfig",
    "DreamingWorkspaceReconcileResult",
]
