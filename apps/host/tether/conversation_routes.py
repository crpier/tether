"""HTTP presentation for host-owned conversations and transcripts."""

from __future__ import annotations

import hmac
import json
import re
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, Protocol, cast
from uuid import UUID

from fastapi import APIRouter, Query
from pydantic import UUID7, BaseModel, NonNegativeInt, PositiveInt
from snekql.sqlite import Fetched, select
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from tether.chat_prompt import ReplyMode
from tether.conversation_model import (
    ConversationArchiveBlockedError,
    ConversationKind,
    ConversationNotFoundError,
    ConversationStatus,
    ConversationTurnOrigin,
    ConversationTurnStatus,
    ConversationValidationError,
    MessageRole,
)
from tether.conversation_store import Conversation, ConversationTurn, Message
from tether.conversations import (
    SESSION_GAP,
    ConversationActivity,
    ConversationService,
)
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
from tether.model_selection import ModelNotAllowedError
from tether.structured_logging import Logger
from tether.tool_runtime import TOOL_AUTH_HEADER
from tether.trigger_store import ScheduledOccurrence


class ConversationRead(BaseModel):
    """HTTP representation of a host-owned conversation.

    `session_gap_seconds` and `latest_activity` let the frontend compute
    whether the *next* message will land on a fresh pi session (see
    `ConversationService.resolve_session`) without hardcoding the gap.
    """

    archived_at: datetime | None
    created_at: datetime
    display_name: str | None
    id: UUID7
    has_unread: bool
    kind: ConversationKind
    last_read_seq: NonNegativeInt
    latest_activity: datetime | None
    latest_message_seq: NonNegativeInt
    pending_turn_count: NonNegativeInt
    pi_session_id: UUID7
    running_turn_id: UUID7 | None
    scope_brief: str | None
    scope_revision: PositiveInt
    selected_model: str | None
    session_gap_seconds: int
    status: ConversationStatus
    title: str | None

    @classmethod
    def from_conversation(
        cls,
        conversation: Conversation[Fetched],
        *,
        activity: ConversationActivity,
    ) -> ConversationRead:
        """Render canonical state with its current activity signal."""
        return cls(
            archived_at=conversation.archived_at,
            created_at=conversation.created_at,
            display_name=conversation.display_name,
            has_unread=activity.latest_message_seq > conversation.last_read_seq,
            id=conversation.id,
            kind=conversation.kind,
            last_read_seq=conversation.last_read_seq,
            latest_activity=activity.latest_activity,
            latest_message_seq=activity.latest_message_seq,
            pending_turn_count=activity.pending_turn_count,
            pi_session_id=conversation.pi_session_id,
            running_turn_id=activity.running_turn_id,
            scope_brief=conversation.scope_brief,
            scope_revision=conversation.scope_revision,
            selected_model=conversation.selected_model,
            session_gap_seconds=int(SESSION_GAP.total_seconds()),
            status=conversation.status,
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

    evidence: list[str]
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


_SOURCE_CITATION = re.compile(r"\s*\[source\]\((?P<uri>tether://[^)\s]+)\)")


type _MemoryClaim = tuple[str | None, str, tuple[str, ...]]


def _memory_claims(content: str | None) -> list[_MemoryClaim]:
    """Extract ordered, cited Claims from one canonical Memory document."""
    if content is None:
        return []
    topic: str | None = None
    claims: list[_MemoryClaim] = []
    for line in content.splitlines():
        if line.startswith("## "):
            topic = line.removeprefix("## ").strip() or None
            continue
        if not line.startswith("- ") or _SOURCE_CITATION.search(line) is None:
            continue
        raw_claim = line.removeprefix("- ")
        evidence = tuple(
            match.group("uri") for match in _SOURCE_CITATION.finditer(raw_claim)
        )
        text = _SOURCE_CITATION.sub("", raw_claim).strip()
        if text:
            claims.append((topic, text, evidence))
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
            DreamingFactChangeRead(
                evidence=list(evidence), kind="removed", topic=topic, text=text
            )
            for topic, text, evidence in before_claims
            if (topic, text, evidence) not in after_set
        ),
        *(
            DreamingFactChangeRead(
                evidence=list(evidence), kind="added", topic=topic, text=text
            )
            for topic, text, evidence in after_claims
            if (topic, text, evidence) not in before_set
        ),
    ]


class DreamRunDetailRead(BaseModel):
    """One Dream run and its ordered canonical Memory effects."""

    run: DreamRunRead
    mutations: list[DreamingMutationRead]


class ConversationTurnSummaryRead(BaseModel):
    """Compact lifecycle repeated beside each flat transcript Message."""

    failure_code: str | None
    failure_summary: str | None
    intended_fire_at: datetime | None
    occurrence_id: UUID | None
    origin: ConversationTurnOrigin
    trigger_id: UUID | None
    status: ConversationTurnStatus

    @classmethod
    def from_turn(
        cls,
        turn: ConversationTurn[Fetched],
        *,
        occurrence: ScheduledOccurrence[Fetched] | None = None,
    ) -> ConversationTurnSummaryRead:
        """Render stable lifecycle state without raw diagnostics."""
        return cls(
            failure_code=turn.failure_code,
            failure_summary=turn.failure_summary,
            intended_fire_at=(
                None if occurrence is None else occurrence.intended_fire_at
            ),
            occurrence_id=None if occurrence is None else occurrence.id,
            origin=turn.origin,
            status=turn.status,
            trigger_id=None if occurrence is None else occurrence.trigger_id,
        )


class ConversationTurnRead(BaseModel):
    """Flat durable turn state used for queue restoration and deep links."""

    completed_at: datetime | None
    conversation_id: UUID7
    created_at: datetime
    failure_code: str | None
    failure_summary: str | None
    id: UUID7
    origin: ConversationTurnOrigin
    prompt: str
    reply_mode: ReplyMode
    request_id: UUID | None
    started_at: datetime | None
    status: ConversationTurnStatus

    @classmethod
    def from_turn(cls, turn: ConversationTurn[Fetched]) -> ConversationTurnRead:
        """Render stable lifecycle state without execution diagnostics."""
        return cls(
            completed_at=turn.completed_at,
            conversation_id=turn.conversation_id,
            created_at=turn.created_at,
            failure_code=turn.failure_code,
            failure_summary=turn.failure_summary,
            id=turn.id,
            origin=turn.origin,
            prompt=turn.prompt_snapshot or "",
            reply_mode=cast("ReplyMode", turn.reply_mode),
            request_id=turn.request_id,
            started_at=turn.started_at,
            status=turn.status,
        )


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
    turn: ConversationTurnSummaryRead | None
    turn_id: UUID7 | None
    turn_message_seq: PositiveInt | None

    @classmethod
    def from_message(
        cls,
        message: Message[Fetched],
        *,
        turn: ConversationTurnSummaryRead | None = None,
    ) -> MessageRead:
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
            turn=turn,
            turn_id=message.turn_id,
            turn_message_seq=message.turn_message_seq,
        )


class CreateConversationRequest(BaseModel):
    """Body for creating an active Scoped Conversation."""

    display_name: str
    scope_brief: str


class UpdateConversationRequest(BaseModel):
    """Editable fields of a Scoped Conversation."""

    display_name: str | None = None
    scope_brief: str | None = None


class SetConversationModelRequest(BaseModel):
    """Body for selecting a conversation's model."""

    selected_model: str


class MarkConversationReadRequest(BaseModel):
    """Last Message sequence the client has actually rendered."""

    last_read_seq: NonNegativeInt


class ConversationListQuery(BaseModel):
    """Filters for ordinary or lifecycle-management Conversation lists."""

    include_archived: bool = False


class MessagesQuery(BaseModel):
    """Query string for windowed or turn-filtered transcript pagination."""

    limit: PositiveInt | None = None
    before_seq: PositiveInt | None = None
    turn_id: UUID7 | None = None


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


def _path_turn_id(raw_turn_id: str) -> UUID:
    """Parse `{turn_id}` and map malformed ids as `not found`."""
    try:
        return UUID(raw_turn_id)
    except ValueError as error:
        raise ConversationNotFoundError(raw_turn_id) from error


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
        activity=await service.conversation_activity(conversation.id),
    )


async def _messages_response(
    request: Request,
    conversation_id: UUID,
    *,
    limit: int | None = None,
    before_seq: int | None = None,
    turn_id: UUID | None = None,
) -> Response:
    """Serialize settled transcript rows or translate absence to 404."""
    try:
        messages = await _runtime(request).conversation_service.fetch_messages(
            conversation_id,
            limit=limit,
            before_seq=before_seq,
            turn_id=turn_id,
        )
    except ConversationNotFoundError:
        return JSONResponse({"detail": "conversation not found"}, status_code=404)
    turn_ids = {message.turn_id for message in messages if message.turn_id is not None}
    async with _runtime(request).conversation_service.database.transaction() as tx:
        turns = (
            []
            if not turn_ids
            else await tx.fetch_all(
                select(ConversationTurn).where(ConversationTurn.id.in_(*turn_ids))
            )
        )
    occurrence_ids = {
        turn.scheduled_occurrence_id
        for turn in turns
        if turn.scheduled_occurrence_id is not None
    }
    async with _runtime(request).conversation_service.database.transaction() as tx:
        occurrences = (
            []
            if not occurrence_ids
            else await tx.fetch_all(
                select(ScheduledOccurrence).where(
                    ScheduledOccurrence.id.in_(*occurrence_ids)
                )
            )
        )
    occurrences_by_id = {occurrence.id: occurrence for occurrence in occurrences}
    summaries = {
        turn.id: ConversationTurnSummaryRead.from_turn(
            turn,
            occurrence=(
                None
                if turn.scheduled_occurrence_id is None
                else occurrences_by_id.get(turn.scheduled_occurrence_id)
            ),
        )
        for turn in turns
    }
    return JSONResponse(
        [
            MessageRead.from_message(
                message,
                turn=(
                    None if message.turn_id is None else summaries.get(message.turn_id)
                ),
            ).model_dump(mode="json")
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
async def list_conversations(
    request: Request,
    query: Annotated[ConversationListQuery, Query()],
) -> Response:
    """List active Conversations unless archived state is explicitly requested."""
    service = _runtime(request).conversation_service
    conversations = await service.list_conversations(
        include_archived=query.include_archived
    )
    return JSONResponse(
        [
            (await _to_read(service, conversation)).model_dump(mode="json")
            for conversation in conversations
        ]
    )


@router.post(
    "/api/conversations",
    response_model=ConversationRead,
    status_code=201,
)
async def create_conversation(
    request: Request,
    body: CreateConversationRequest,
) -> Response:
    """Create an active Scoped Conversation."""
    service = _runtime(request).conversation_service
    try:
        conversation = await service.create_scoped_conversation(
            display_name=body.display_name,
            scope_brief=body.scope_brief,
        )
    except ConversationValidationError as error:
        return JSONResponse({"detail": str(error)}, status_code=422)
    return JSONResponse(
        (await _to_read(service, conversation)).model_dump(mode="json"),
        status_code=201,
    )


@router.get(
    "/api/conversations/{conversation_id}",
    response_model=ConversationRead,
)
async def fetch_conversation(request: Request, conversation_id: str) -> Response:
    """Fetch one Conversation including archived lifecycle state."""
    try:
        parsed_conversation_id = _path_conversation_id(conversation_id)
        service = _runtime(request).conversation_service
        conversation = await service.fetch_conversation(parsed_conversation_id)
    except ConversationNotFoundError:
        return JSONResponse({"detail": "conversation not found"}, status_code=404)
    return JSONResponse((await _to_read(service, conversation)).model_dump(mode="json"))


@router.patch(
    "/api/conversations/{conversation_id}",
    response_model=ConversationRead,
)
async def update_conversation(
    request: Request,
    body: UpdateConversationRequest,
    conversation_id: str,
) -> Response:
    """Edit one Scoped Conversation's name or scope brief."""
    try:
        parsed_conversation_id = _path_conversation_id(conversation_id)
        service = _runtime(request).conversation_service
        conversation = await service.update_scoped_conversation(
            parsed_conversation_id,
            display_name=body.display_name,
            scope_brief=body.scope_brief,
        )
    except ConversationNotFoundError:
        return JSONResponse({"detail": "conversation not found"}, status_code=404)
    except ConversationValidationError as error:
        return JSONResponse({"detail": str(error)}, status_code=422)
    return JSONResponse((await _to_read(service, conversation)).model_dump(mode="json"))


@router.post(
    "/api/conversations/{conversation_id}/archive",
    response_model=ConversationRead,
)
async def archive_conversation(request: Request, conversation_id: str) -> Response:
    """Archive a Scoped Conversation whose dependent work has settled."""
    try:
        parsed_conversation_id = _path_conversation_id(conversation_id)
        service = _runtime(request).conversation_service
        conversation = await service.archive_conversation(parsed_conversation_id)
    except ConversationNotFoundError:
        return JSONResponse({"detail": "conversation not found"}, status_code=404)
    except ConversationValidationError as error:
        return JSONResponse({"detail": str(error)}, status_code=422)
    except ConversationArchiveBlockedError as error:
        return JSONResponse(
            {"blocker": error.blocker, "detail": "conversation archive blocked"},
            status_code=409,
        )
    await _runtime(request).conversation_runtime_registry.discard(conversation.id)
    return JSONResponse((await _to_read(service, conversation)).model_dump(mode="json"))


@router.post(
    "/api/conversations/{conversation_id}/restore",
    response_model=ConversationRead,
)
async def restore_conversation(request: Request, conversation_id: str) -> Response:
    """Restore an archived Scoped Conversation."""
    try:
        parsed_conversation_id = _path_conversation_id(conversation_id)
        service = _runtime(request).conversation_service
        conversation = await service.restore_conversation(parsed_conversation_id)
    except ConversationNotFoundError:
        return JSONResponse({"detail": "conversation not found"}, status_code=404)
    except ConversationValidationError as error:
        return JSONResponse({"detail": str(error)}, status_code=422)
    return JSONResponse((await _to_read(service, conversation)).model_dump(mode="json"))


@router.post(
    "/api/conversations/{conversation_id}/read",
    response_model=ConversationRead,
)
async def mark_conversation_read(
    request: Request,
    conversation_id: str,
    body: MarkConversationReadRequest | None = None,
) -> Response:
    """Advance durable read position to an observed or current Message tail."""
    try:
        parsed_conversation_id = _path_conversation_id(conversation_id)
        service = _runtime(request).conversation_service
        conversation = await service.mark_conversation_read(
            parsed_conversation_id,
            last_read_seq=body.last_read_seq if body is not None else None,
        )
    except ConversationNotFoundError:
        return JSONResponse({"detail": "conversation not found"}, status_code=404)
    except ConversationValidationError as error:
        return JSONResponse({"detail": str(error)}, status_code=422)
    return JSONResponse((await _to_read(service, conversation)).model_dump(mode="json"))


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


@router.post("/api/dream-now", response_model=list[DreamRunRead])
async def dream_now(request: Request) -> Response:
    """Queue an instant manual Dream run for every conversation with new evidence."""
    runtime = _runtime(request)
    if not runtime.dreaming_enabled:
        return JSONResponse({"detail": "dreaming not enabled"}, status_code=404)
    runs = await runtime.dreaming_service.queue_pending_manual_runs(
        logger=runtime.logger,
        now=datetime.now(UTC),
    )
    return JSONResponse(
        [run.model_dump(mode="json") for run in map(DreamRunRead.from_run, runs)]
    )


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
    # The lifecycle module owns the ack policy; this route only maps the
    # outcome onto HTTP. Default acknowledger = in-process settlement.
    settlement = await coordinator.settle(parsed_run_id, tool_call_id)
    if settlement.outcome == "not_found":
        return JSONResponse({"detail": "mutation not found"}, status_code=404)
    if not settlement.acknowledged:
        return JSONResponse(
            {"detail": settlement.error or "mutation acknowledgment failed"},
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
        conversation, _ = await _runtime(
            request
        ).conversation_service.set_selected_model(
            parsed_conversation_id,
            body.selected_model,
        )
    except ConversationNotFoundError:
        return JSONResponse({"detail": "conversation not found"}, status_code=404)
    except ModelNotAllowedError:
        return JSONResponse({"detail": "model not allowed"}, status_code=422)
    return JSONResponse(
        (
            await _to_read(_runtime(request).conversation_service, conversation)
        ).model_dump(mode="json")
    )


@router.get(
    "/api/conversations/{conversation_id}/turns",
    response_model=list[ConversationTurnRead],
)
async def list_nonterminal_turns(request: Request, conversation_id: str) -> Response:
    """List pending and running turns in durable FIFO order."""
    try:
        parsed_conversation_id = _path_conversation_id(conversation_id)
        runtime = _runtime(request)
        _ = await runtime.conversation_service.fetch_conversation(
            parsed_conversation_id
        )
    except ConversationNotFoundError:
        return JSONResponse({"detail": "conversation not found"}, status_code=404)
    async with runtime.conversation_service.database.transaction() as transaction:
        turns = await transaction.fetch_all(
            select(ConversationTurn)
            .where(ConversationTurn.conversation_id.eq(parsed_conversation_id))
            .where(ConversationTurn.status.in_("pending", "running"))
            .order_by(ConversationTurn.turn_seq.asc())
        )
    return JSONResponse(
        [ConversationTurnRead.from_turn(turn).model_dump(mode="json") for turn in turns]
    )


@router.get(
    "/api/conversations/{conversation_id}/turns/{turn_id}",
    response_model=ConversationTurnRead,
)
async def fetch_turn_detail(
    request: Request,
    conversation_id: str,
    turn_id: str,
) -> Response:
    """Fetch one turn only through its owning Conversation."""
    try:
        parsed_conversation_id = _path_conversation_id(conversation_id)
        parsed_turn_id = _path_turn_id(turn_id)
    except ConversationNotFoundError:
        return JSONResponse({"detail": "turn not found"}, status_code=404)
    runtime = _runtime(request)
    async with runtime.conversation_service.database.transaction() as transaction:
        turn = await transaction.fetch_one_or_none(
            select(ConversationTurn)
            .where(ConversationTurn.id.eq(parsed_turn_id))
            .where(ConversationTurn.conversation_id.eq(parsed_conversation_id))
        )
    if turn is None:
        return JSONResponse({"detail": "turn not found"}, status_code=404)
    return JSONResponse(ConversationTurnRead.from_turn(turn).model_dump(mode="json"))


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
        turn_id=query.turn_id,
    )


__all__ = [
    "ConversationRead",
    "ConversationTurnRead",
    "DreamRunDetailRead",
    "DreamRunRead",
    "DreamingMutationRead",
    "MessageRead",
    "MessagesQuery",
    "router",
]
