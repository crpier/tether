"""HTTP presentation for host-owned conversations and transcripts."""

from __future__ import annotations

import hmac
import json
import re
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, Protocol, cast
from uuid import UUID

from fastapi import APIRouter, Query
from pydantic import UUID7, BaseModel, PositiveInt
from snekql.sqlite import Fetched, select
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from tether.conversation_model import ConversationNotFoundError, MessageRole
from tether.conversation_store import Conversation, Message
from tether.conversations import SESSION_GAP, ConversationService
from tether.dreaming import (
    DreamingMutationCoordinator,
    DreamingService,
    DreamRunNotFoundError,
)
from tether.dreaming_store import (
    DreamingMutation,
    DreamingMutationActor,
    DreamingMutationOperation,
    DreamingMutationStatus,
    DreamingWorkspaceFile,
    DreamRun,
    DreamRunKind,
    DreamRunStatus,
    DreamRunTerminalStatus,
)
from tether.memory_workspace_service import MemoryWorkspaceService
from tether.model_selection import AgentModelConfig, ModelNotAllowedError
from tether.pi_errors import PiRuntimeError
from tether.structured_logging import Logger
from tether.tool_runtime import TOOL_AUTH_HEADER


class ConversationRead(BaseModel):
    """HTTP representation of a host-owned conversation.

    `session_gap_seconds` and `latest_activity` let the frontend compute
    whether the *next* message will land on a fresh pi session (see
    `ConversationService.resolve_session`) without hardcoding the gap.
    """

    created_at: datetime
    id: UUID7
    latest_activity: datetime | None
    pi_session_id: UUID7
    selected_model: str | None
    session_gap_seconds: int
    title: str | None

    @classmethod
    def from_conversation(
        cls,
        conversation: Conversation[Fetched],
        *,
        latest_activity: datetime | None,
    ) -> ConversationRead:
        """Render canonical state with its current activity signal."""
        return cls(
            created_at=conversation.created_at,
            id=conversation.id,
            latest_activity=latest_activity,
            pi_session_id=conversation.pi_session_id,
            selected_model=conversation.selected_model,
            session_gap_seconds=int(SESSION_GAP.total_seconds()),
            title=conversation.title,
        )


class DreamRunRead(BaseModel):
    """HTTP representation of one Dream run row."""

    id: UUID7
    conversation_id: UUID
    kind: DreamRunKind
    status: DreamRunStatus
    evidence_start_seq: PositiveInt
    evidence_end_seq: PositiveInt
    attempts: PositiveInt
    error: str | None
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime
    conversation_title: str | None = None
    mutation_count: int = 0

    @classmethod
    def from_run(
        cls,
        run: DreamRun[Fetched],
        *,
        conversation_title: str | None = None,
        mutation_count: int = 0,
    ) -> DreamRunRead:
        """Render one persisted dream run for browser JSON payloads."""
        return cls(
            id=run.id,
            conversation_id=run.conversation_id,
            kind=run.kind,
            status=run.status,
            evidence_start_seq=run.evidence_start_seq,
            evidence_end_seq=run.evidence_end_seq,
            attempts=run.attempts,
            error=run.error,
            completed_at=run.completed_at,
            created_at=run.created_at,
            updated_at=run.updated_at,
            conversation_title=conversation_title,
            mutation_count=mutation_count,
        )


class DreamingFactChangeRead(BaseModel):
    """One human-readable Claim addition or removal from a Dream mutation."""

    kind: Literal["added", "removed"]
    text: str
    topic: str | None


class DreamingMutationRead(BaseModel):
    """Inspectable effect of one Dreaming filesystem mutation."""

    id: UUID7
    tool_call_id: str
    actor: DreamingMutationActor
    operation: DreamingMutationOperation
    workspace_path: str
    status: DreamingMutationStatus
    attempts: int
    error: str | None
    created_at: datetime
    updated_at: datetime
    fact_changes: list[DreamingFactChangeRead]

    @classmethod
    def from_mutation(
        cls,
        mutation: DreamingMutation[Fetched],
        *,
        fact_changes: list[DreamingFactChangeRead],
    ) -> DreamingMutationRead:
        """Render mutation metadata with its human-readable Claim changes."""
        return cls(
            id=mutation.id,
            tool_call_id=mutation.tool_call_id,
            actor=mutation.actor,
            operation=mutation.operation,
            workspace_path=mutation.workspace_path,
            status=mutation.status,
            attempts=mutation.attempts,
            error=mutation.error,
            created_at=mutation.created_at,
            updated_at=mutation.updated_at,
            fact_changes=fact_changes,
        )


_SOURCE_CITATION = re.compile(r"\s*\[source\]\(tether://message/[0-9A-Za-z-]+\)")


def _memory_claims(content: str | None) -> list[tuple[str | None, str]]:
    """Extract ordered, cited Claims from one canonical Memory document."""
    if content is None:
        return []
    topic: str | None = None
    claims: list[tuple[str | None, str]] = []
    for line in content.splitlines():
        if line.startswith("## "):
            topic = line.removeprefix("## ").strip() or None
            continue
        if not line.startswith("- ") or _SOURCE_CITATION.search(line) is None:
            continue
        text = _SOURCE_CITATION.sub("", line.removeprefix("- ")).strip()
        if text:
            claims.append((topic, text))
    return claims


def _fact_changes(
    before: str | None,
    after: str | None,
) -> list[DreamingFactChangeRead]:
    """Describe exact Claim-level additions and removals between snapshots."""
    before_claims = _memory_claims(before)
    after_claims = _memory_claims(after)
    before_set = set(before_claims)
    after_set = set(after_claims)
    return [
        *(
            DreamingFactChangeRead(kind="removed", topic=topic, text=text)
            for topic, text in before_claims
            if (topic, text) not in after_set
        ),
        *(
            DreamingFactChangeRead(kind="added", topic=topic, text=text)
            for topic, text in after_claims
            if (topic, text) not in before_set
        ),
    ]


class DreamRunDetailRead(BaseModel):
    """One Dream run and its ordered canonical Memory effects."""

    run: DreamRunRead
    mutations: list[DreamingMutationRead]


class MessageRead(BaseModel):
    """HTTP representation of a settled transcript row."""

    content: str
    conversation_id: UUID7
    created_at: datetime
    id: UUID7
    pi_message_id: str | None
    role: MessageRole
    seq: PositiveInt
    tool_args: dict[str, Any] | None
    tool_name: str | None
    tool_result: dict[str, Any] | None

    @classmethod
    def from_message(cls, message: Message[Fetched]) -> MessageRead:
        """Decode stored JSON fields at the HTTP presentation boundary."""
        return cls(
            content=message.content,
            conversation_id=message.conversation_id,
            created_at=message.created_at,
            id=message.id,
            pi_message_id=message.pi_message_id,
            role=message.role,
            seq=message.seq,
            tool_args=(
                json.loads(message.tool_args) if message.tool_args is not None else None
            ),
            tool_name=message.tool_name,
            tool_result=(
                json.loads(message.tool_result)
                if message.tool_result is not None
                else None
            ),
        )


class SetConversationModelRequest(BaseModel):
    """Body for selecting a conversation's model."""

    selected_model: str


class MessagesQuery(BaseModel):
    """Query string for windowed transcript pagination."""

    limit: PositiveInt | None = None
    before_seq: PositiveInt | None = None


class CompleteDreamRunRequest(BaseModel):
    """Body for marking a Dream run terminal."""

    status: DreamRunTerminalStatus
    error: str | None = None


class DreamMutationAckResponse(BaseModel):
    """Result payload after a mutation acknowledgement attempt."""

    run_id: UUID
    tool_call_id: str
    acknowledged: bool


class _ConversationRuntimeRegistry(Protocol):
    """Live process operations required by conversation routes."""

    async def set_model(
        self,
        conversation_id: object,
        model: AgentModelConfig,
    ) -> None: ...

    async def discard(self, conversation_id: object) -> None: ...


class _ConversationRuntime(Protocol):
    """Conversation dependencies available while serving requests."""

    conversation_runtime_registry: _ConversationRuntimeRegistry
    conversation_service: ConversationService
    dreaming_enabled: bool
    dreaming_service: DreamingService
    memory_workspace_service: MemoryWorkspaceService
    tool_secret: str
    logger: Logger


def _path_conversation_id(raw_conversation_id: str) -> UUID:
    """Parse `{conversation_id}` and map malformed ids as `not found`."""
    try:
        return UUID(raw_conversation_id)
    except ValueError as error:
        raise ConversationNotFoundError(raw_conversation_id) from error


def _runtime(request: Request) -> _ConversationRuntime:
    """Read conversation dependencies from the canonical host runtime."""
    return cast("_ConversationRuntime", request.app.state.runtime)


def _path_dream_run_id(raw_run_id: str) -> UUID:
    """Parse `{run_id}` and map malformed ids as `not found`."""
    try:
        return UUID(raw_run_id)
    except ValueError as error:
        raise DreamRunNotFoundError(raw_run_id) from error


async def _to_read(
    service: ConversationService,
    conversation: Conversation[Fetched],
) -> ConversationRead:
    """Render canonical conversation state with its latest activity."""
    return ConversationRead.from_conversation(
        conversation,
        latest_activity=await service.latest_activity(conversation.id),
    )


async def _messages_response(
    request: Request,
    conversation_id: UUID,
    *,
    limit: int | None = None,
    before_seq: int | None = None,
) -> Response:
    """Serialize settled transcript rows or translate absence to 404."""
    try:
        messages = await _runtime(request).conversation_service.fetch_messages(
            conversation_id,
            limit=limit,
            before_seq=before_seq,
        )
    except ConversationNotFoundError:
        return JSONResponse({"detail": "conversation not found"}, status_code=404)
    return JSONResponse(
        [
            MessageRead.from_message(message).model_dump(mode="json")
            for message in messages
        ]
    )


async def _dream_runs_query(
    runtime: _ConversationRuntime, *, conversation_id: UUID
) -> list[DreamRun[Fetched]]:
    """Read dream runs for one conversation ordered newest first."""
    async with runtime.dreaming_service.database.transaction() as tx:
        return await tx.fetch_all(
            select(DreamRun)
            .where(DreamRun.conversation_id.eq(conversation_id))
            .order_by(DreamRun.created_at.desc())
        )


router = APIRouter()


@router.get("/api/conversations", response_model=list[ConversationRead])
async def list_conversations(request: Request) -> Response:
    """List host-owned conversations."""
    service = _runtime(request).conversation_service
    conversations = await service.list_conversations()
    return JSONResponse(
        [
            (await _to_read(service, conversation)).model_dump(mode="json")
            for conversation in conversations
        ]
    )


@router.get("/api/dream-runs", response_model=list[DreamRunRead])
async def list_all_dream_runs(request: Request) -> Response:
    """List Dream runs across conversations for the inspectable history UI."""
    runtime = _runtime(request)
    async with runtime.dreaming_service.database.transaction() as tx:
        runs = await tx.fetch_all(
            select(DreamRun).all().order_by(DreamRun.created_at.desc())
        )
        mutations = await tx.fetch_all(select(DreamingMutation).all())
        conversations = await tx.fetch_all(select(Conversation).all())
    conversation_by_id = {
        conversation.id: conversation for conversation in conversations
    }
    mutation_counts: dict[UUID, int] = {}
    for mutation in mutations:
        mutation_counts[mutation.run_id] = mutation_counts.get(mutation.run_id, 0) + 1
    return JSONResponse(
        [
            DreamRunRead.from_run(
                run,
                conversation_title=(
                    conversation_by_id[run.conversation_id].title
                    if run.conversation_id in conversation_by_id
                    else None
                ),
                mutation_count=mutation_counts.get(run.id, 0),
            ).model_dump(mode="json")
            for run in runs
        ]
    )


@router.get(
    "/api/conversations/{conversation_id}/dream-runs",
    response_model=list[DreamRunRead],
)
async def list_dream_runs(request: Request, conversation_id: str) -> Response:
    """List dream runs for one conversation."""
    try:
        parsed_conversation_id = _path_conversation_id(conversation_id)
    except ConversationNotFoundError:
        return JSONResponse({"detail": "conversation not found"}, status_code=404)
    runtime = _runtime(request)
    try:
        _ = await runtime.conversation_service.fetch_conversation(
            parsed_conversation_id
        )
    except ConversationNotFoundError:
        return JSONResponse({"detail": "conversation not found"}, status_code=404)
    runs = await _dream_runs_query(runtime, conversation_id=parsed_conversation_id)
    return JSONResponse(
        [DreamRunRead.from_run(run).model_dump(mode="json") for run in runs]
    )


@router.get("/api/dream-runs/{run_id}", response_model=DreamRunDetailRead)
async def get_dream_run(request: Request, run_id: str) -> Response:
    """Return one Dream run with the Memory mutations it attempted."""
    try:
        parsed_run_id = _path_dream_run_id(run_id)
    except DreamRunNotFoundError:
        return JSONResponse({"detail": "dream run not found"}, status_code=404)
    runtime = _runtime(request)
    async with runtime.dreaming_service.database.transaction() as tx:
        run = await tx.fetch_one_or_none(
            select(DreamRun).where(DreamRun.id.eq(parsed_run_id))
        )
        mutations = await tx.fetch_all(
            select(DreamingMutation)
            .where(DreamingMutation.run_id.eq(parsed_run_id))
            .order_by(DreamingMutation.created_at.asc())
        )
        workspace_files = await tx.fetch_all(
            select(DreamingWorkspaceFile).where(
                DreamingWorkspaceFile.source_run_id.eq(parsed_run_id)
            )
        )
    if run is None:
        return JSONResponse({"detail": "dream run not found"}, status_code=404)
    try:
        conversation = await runtime.conversation_service.fetch_conversation(
            run.conversation_id
        )
    except ConversationNotFoundError:
        conversation = None
    workspace_file_by_mutation = {
        (workspace_file.path, workspace_file.source_tool_call_id): workspace_file
        for workspace_file in workspace_files
    }
    mutation_reads: list[DreamingMutationRead] = []
    for mutation in mutations:
        after_content = mutation.after_content
        if mutation.before_content is None and after_content is None:
            legacy_file = workspace_file_by_mutation.get(
                (mutation.workspace_path, mutation.tool_call_id)
            )
            after_content = legacy_file.content if legacy_file is not None else None
        mutation_reads.append(
            DreamingMutationRead.from_mutation(
                mutation,
                fact_changes=_fact_changes(
                    mutation.before_content,
                    after_content,
                ),
            )
        )
    detail = DreamRunDetailRead(
        run=DreamRunRead.from_run(
            run,
            conversation_title=conversation.title if conversation is not None else None,
            mutation_count=len(mutations),
        ),
        mutations=mutation_reads,
    )
    return JSONResponse(detail.model_dump(mode="json"))


@router.post("/api/conversations/{conversation_id}/dream-now", status_code=200)
async def queue_dream_run(request: Request, conversation_id: str) -> Response:
    """Queue a manual Dream run for the latest evidence window."""
    try:
        parsed_conversation_id = _path_conversation_id(conversation_id)
    except ConversationNotFoundError:
        return JSONResponse({"detail": "conversation not found"}, status_code=404)
    runtime = _runtime(request)
    if not runtime.dreaming_enabled:
        return JSONResponse({"detail": "dreaming not enabled"}, status_code=404)
    try:
        _ = await runtime.conversation_service.fetch_conversation(
            parsed_conversation_id
        )
    except ConversationNotFoundError:
        return JSONResponse({"detail": "conversation not found"}, status_code=404)
    run = await runtime.dreaming_service.queue_manual_run(
        parsed_conversation_id,
        logger=runtime.logger,
        now=datetime.now(UTC),
    )
    if run is None:
        return Response(status_code=204)
    return JSONResponse(DreamRunRead.from_run(run).model_dump(mode="json"))


@router.post(
    "/api/dream-runs/{run_id}/complete",
    response_model=DreamRunRead,
)
async def complete_dream_run(
    request: Request,
    body: CompleteDreamRunRequest,
    run_id: str,
) -> Response:
    """Mark one dream run terminal from an external worker callback."""
    try:
        parsed_run_id = _path_dream_run_id(run_id)
    except DreamRunNotFoundError:
        return JSONResponse({"detail": "dream run not found"}, status_code=404)
    runtime = _runtime(request)
    if not runtime.dreaming_enabled:
        return JSONResponse({"detail": "dreaming not enabled"}, status_code=404)
    try:
        run = await runtime.dreaming_service.complete_run(
            parsed_run_id,
            logger=runtime.logger,
            now=datetime.now(UTC),
            status=body.status,
            error=body.error,
        )
    except DreamRunNotFoundError:
        return JSONResponse({"detail": "dream run not found"}, status_code=404)
    return JSONResponse(DreamRunRead.from_run(run).model_dump(mode="json"))


@router.post(
    "/internal/dream-runs/{run_id}/mutations/{tool_call_id}/ack",
    response_model=DreamMutationAckResponse,
    include_in_schema=False,
)
async def acknowledge_dream_mutation(
    request: Request,
    run_id: str,
    tool_call_id: str,
) -> Response:
    """Ack one Dream mutation from a PI tool callback."""
    try:
        parsed_run_id = _path_dream_run_id(run_id)
    except DreamRunNotFoundError:
        return JSONResponse({"detail": "dream run not found"}, status_code=404)
    runtime = _runtime(request)
    offered_secret = request.headers.get(TOOL_AUTH_HEADER, "")
    if not hmac.compare_digest(offered_secret, runtime.tool_secret):
        return JSONResponse({"detail": "invalid tool secret"}, status_code=401)
    if not runtime.dreaming_enabled:
        return JSONResponse(
            {"detail": "dreaming not enabled"},
            status_code=404,
        )
    coordinator = DreamingMutationCoordinator(
        runtime.dreaming_service.database,
        runtime.memory_workspace_service.workspace_root,
    )
    acknowledged, error = await coordinator.acknowledge_mutation(
        run_id=parsed_run_id,
        tool_call_id=tool_call_id,
    )
    if not acknowledged:
        if error == "mutation not found":
            return JSONResponse({"detail": "mutation not found"}, status_code=404)
        return JSONResponse(
            {"detail": error or "mutation acknowledgment failed"},
            status_code=500,
        )
    return JSONResponse(
        DreamMutationAckResponse(
            run_id=parsed_run_id,
            tool_call_id=tool_call_id,
            acknowledged=True,
        ).model_dump(mode="json")
    )


@router.post(
    "/api/conversations/{conversation_id}/model",
    response_model=ConversationRead,
)
async def set_conversation_model(
    request: Request,
    body: SetConversationModelRequest,
    conversation_id: str,
) -> Response:
    """Select the model used for subsequent turns in one conversation."""
    try:
        parsed_conversation_id = _path_conversation_id(conversation_id)
    except ConversationNotFoundError:
        return JSONResponse({"detail": "conversation not found"}, status_code=404)
    try:
        conversation, selected_model = await _runtime(
            request
        ).conversation_service.set_selected_model(
            parsed_conversation_id,
            body.selected_model,
        )
    except ConversationNotFoundError:
        return JSONResponse({"detail": "conversation not found"}, status_code=404)
    except ModelNotAllowedError:
        return JSONResponse({"detail": "model not allowed"}, status_code=422)
    try:
        await _runtime(request).conversation_runtime_registry.set_model(
            conversation.id,
            selected_model,
        )
    except PiRuntimeError:
        return JSONResponse({"detail": "set_model failed"}, status_code=502)
    return JSONResponse(
        (
            await _to_read(_runtime(request).conversation_service, conversation)
        ).model_dump(mode="json")
    )


@router.get(
    "/api/conversations/{conversation_id}/messages",
    response_model=list[MessageRead],
)
async def list_messages(
    request: Request,
    query: Annotated[MessagesQuery, Query()],
    conversation_id: str,
) -> Response:
    """List settled transcript rows for one conversation."""
    try:
        parsed_conversation_id = _path_conversation_id(conversation_id)
    except ConversationNotFoundError:
        return JSONResponse({"detail": "conversation not found"}, status_code=404)
    return await _messages_response(
        request,
        parsed_conversation_id,
        limit=query.limit,
        before_seq=query.before_seq,
    )


@router.delete(
    "/api/conversations/{conversation_id}/messages",
    response_model=ConversationRead,
)
async def clear_messages(request: Request, conversation_id: str) -> Response:
    """Clear one conversation's transcript and rotate its pi session."""
    try:
        parsed_conversation_id = _path_conversation_id(conversation_id)
    except ConversationNotFoundError:
        return JSONResponse({"detail": "conversation not found"}, status_code=404)
    service = _runtime(request).conversation_service
    try:
        conversation = await service.clear_conversation(parsed_conversation_id)
    except ConversationNotFoundError:
        return JSONResponse({"detail": "conversation not found"}, status_code=404)
    await _runtime(request).conversation_runtime_registry.discard(conversation.id)
    return JSONResponse((await _to_read(service, conversation)).model_dump(mode="json"))


__all__ = [
    "ConversationRead",
    "DreamRunDetailRead",
    "DreamRunRead",
    "DreamingMutationRead",
    "MessageRead",
    "MessagesQuery",
    "router",
]
