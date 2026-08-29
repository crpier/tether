"""Service behavior for Dreaming orchestration cursoring and run queueing."""

import asyncio
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast
from uuid import UUID, uuid7

import httpx2
import structlog
from pydantic import UUID7
from snekql.sqlite import (
    Config,
    Database,
    Fetched,
    Pending,
    PendingGeneration,
    insert,
    select,
    update,
)
from snektest import (
    Param,
    assert_eq,
    assert_is_none,
    fixture,
    load_fixture,
    test,
)

from tether.conversation_model import MessageDraft, MessageRole
from tether.conversation_store import (
    Conversation,
    ConversationTurn,
    Message,
    create_conversation_schema,
)
from tether.conversations import ConversationService
from tether.dreaming import (
    ConversationWindowDreamingExecutor,
    DreamingMutationCoordinator,
    DreamingService,
    DreamingWorker,
    DreamRunExecutionResult,
    HttpDreamingMutationAcknowledger,
    KindDispatchingDreamExecutor,
    MaintenanceDreamingAgent,
    MaintenanceDreamingExecutor,
)
from tether.dreaming_store import (
    DreamConversationCursor,
    DreamingMutation,
    DreamingWorkspaceFile,
    DreamMaintenanceProgress,
    DreamRun,
    create_dreaming_schema,
)
from tether.email_evidence_store import create_email_evidence_schema
from tether.structured_logging import Logger
from tether.tool_runtime import TOOL_AUTH_HEADER


class _Callback:
    """Deterministic callback that yields a fixed outcome and records calls."""

    def __init__(self, result: DreamRunExecutionResult) -> None:
        self.result: DreamRunExecutionResult = result
        self.calls: int = 0

    async def __call__(self, run: object, logger: object) -> DreamRunExecutionResult:
        self.calls += 1
        return self.result


def test_logger() -> Logger:
    """Provide a deterministic logger for service calls."""
    logger: Logger = structlog.get_logger("test.dreaming")
    return logger


@fixture
async def conversation_fixture() -> AsyncGenerator[
    tuple[ConversationService, DreamingService, UUID]
]:
    """Isolated conversation + Dreaming state stack with schema prepared."""
    db = await Database.initialize(backend=Config(database=":memory:"))
    await create_conversation_schema(db)
    await create_dreaming_schema(db)
    await create_email_evidence_schema(db)
    conversation_service = ConversationService(db)
    conversation = (await conversation_service.list_conversations())[0]
    yield conversation_service, DreamingService(db), conversation.id
    await db.close()


async def _append(
    conversation_service: ConversationService,
    *,
    conversation_id: UUID,
    role: MessageRole,
    content: str,
) -> Message[Fetched] | Message[Pending]:
    """Append one message as in normal turns."""
    return await conversation_service.append_message(
        MessageDraft(
            content=content,
            conversation_id=conversation_id,
            role=role,
        )
    )


async def _retime(
    message_id: UUID | PendingGeneration,
    *,
    database: Database,
    when: datetime,
) -> None:
    """Force one message timestamp for deterministic settling checks."""
    async with database.transaction() as tx:
        normalized_message_id = cast("UUID7", message_id)
        _ = await tx.execute(
            update(Message)
            .set(Message.created_at.to(when))
            .where(Message.id.eq(normalized_message_id))
        )


async def _fixture() -> tuple[ConversationService, DreamingService, UUID]:
    """Fetch a fresh fixture instance for one test run."""
    return await load_fixture(conversation_fixture())


@test()
async def auto_run_waits_for_settled_user_message() -> None:
    """Auto queueing waits until the last user message ages past settle window."""
    conversation_service, dreaming_service, conversation_id = await _fixture()

    recent = await _append(
        conversation_service,
        conversation_id=conversation_id,
        role="user",
        content="still typing",
    )
    await _retime(
        recent.id,
        database=conversation_service.database,
        when=datetime.now(UTC) - timedelta(minutes=10),
    )

    assert_is_none(
        await dreaming_service.queue_assimilation_run(
            conversation_id,
            logger=test_logger(),
            now=datetime.now(UTC),
        )
    )


@test()
async def auto_run_enqueues_after_settling_window() -> None:
    """Auto queueing emits one run after user evidence has settled."""
    conversation_service, dreaming_service, conversation_id = await _fixture()

    settled = await _append(
        conversation_service,
        conversation_id=conversation_id,
        role="user",
        content="hello",
    )
    await _retime(
        settled.id,
        database=conversation_service.database,
        when=datetime.now(UTC) - timedelta(minutes=30),
    )

    run = await dreaming_service.queue_assimilation_run(
        conversation_id,
        logger=test_logger(),
        now=datetime.now(UTC),
    )

    assert run is not None
    assert_eq(run.status, "queued")
    assert_eq(run.evidence_start_seq, 1)
    assert_eq(run.evidence_end_seq, 1)


@test()
async def scheduled_assistant_conclusion_queues_assimilation() -> None:
    """A succeeded unattended answer can open a Dreaming window without a user Message."""
    conversation_service, dreaming_service, conversation_id = await _fixture()

    async with conversation_service.database.transaction() as transaction:
        turn = await transaction.execute(
            insert(
                ConversationTurn(
                    conversation_id=conversation_id,
                    origin="scheduled",
                    status="succeeded",
                    turn_seq=1,
                )
            ).returning()
        )
    _ = await conversation_service.append_message(
        MessageDraft(
            content="Review recent viewing patterns.",
            conversation_id=conversation_id,
            role="scheduled",
            turn_id=turn.id,
        )
    )
    conclusion = await conversation_service.append_message(
        MessageDraft(
            content="Recent viewing favors long-form technical interviews.",
            conversation_id=conversation_id,
            role="assistant",
            turn_id=turn.id,
        )
    )

    run = await dreaming_service.queue_assimilation_run(
        conversation_id,
        logger=test_logger(),
        now=datetime.now(UTC),
    )

    assert run is not None
    assert_eq(run.evidence_start_seq, 1)
    assert_eq(run.evidence_end_seq, conclusion.seq)


@test()
async def assimilation_stops_before_a_nonterminal_turn() -> None:
    """Dreaming cannot consume assistant output before its turn settles."""
    conversation_service, dreaming_service, conversation_id = await _fixture()

    prior = await _append(
        conversation_service,
        conversation_id=conversation_id,
        role="user",
        content="I prefer long-form interviews.",
    )
    await _retime(
        prior.id,
        database=conversation_service.database,
        when=datetime.now(UTC) - timedelta(minutes=30),
    )
    async with conversation_service.database.transaction() as transaction:
        running = await transaction.execute(
            insert(
                ConversationTurn(
                    conversation_id=conversation_id,
                    origin="scheduled",
                    status="running",
                    turn_seq=1,
                )
            ).returning()
        )
    _ = await conversation_service.append_message(
        MessageDraft(
            content="Analyze recent viewing.",
            conversation_id=conversation_id,
            role="scheduled",
            turn_id=running.id,
        )
    )
    _ = await conversation_service.append_message(
        MessageDraft(
            content="A still-running provisional conclusion.",
            conversation_id=conversation_id,
            role="assistant",
            turn_id=running.id,
        )
    )

    run = await dreaming_service.queue_assimilation_run(
        conversation_id,
        logger=test_logger(),
        now=datetime.now(UTC),
    )

    assert run is not None
    assert_eq(run.evidence_end_seq, prior.seq)


@test()
async def explicit_memory_request_collapses_until_post_turn_consumption() -> None:
    """Remember/correction intent bypasses idle only after the turn settles."""
    _, dreaming_service, conversation_id = await _fixture()

    dreaming_service.request_immediate_assimilation(conversation_id)
    dreaming_service.request_immediate_assimilation(conversation_id)

    assert_eq(
        dreaming_service.consume_immediate_assimilation_request(conversation_id),
        True,
    )
    assert_eq(
        dreaming_service.consume_immediate_assimilation_request(conversation_id),
        False,
    )


@test()
async def manual_run_bypasses_settling_window() -> None:
    """Manual queueing ignores settle constraints and uses current window."""
    conversation_service, dreaming_service, conversation_id = await _fixture()

    recent = await _append(
        conversation_service,
        conversation_id=conversation_id,
        role="user",
        content="quick question",
    )
    await _retime(
        recent.id,
        database=conversation_service.database,
        when=datetime.now(UTC) - timedelta(minutes=1),
    )

    run = await dreaming_service.queue_manual_run(
        conversation_id,
        logger=test_logger(),
        now=datetime.now(UTC),
    )

    assert run is not None
    assert_eq(run.kind, "manual")
    assert_eq(run.evidence_start_seq, 1)


@test()
async def manual_run_is_idempotent_while_active() -> None:
    """An active run is reused instead of creating duplicates."""
    conversation_service, dreaming_service, conversation_id = await _fixture()

    settled = await _append(
        conversation_service,
        conversation_id=conversation_id,
        role="user",
        content="note",
    )
    await _retime(
        settled.id,
        database=conversation_service.database,
        when=datetime.now(UTC) - timedelta(minutes=40),
    )

    first = await dreaming_service.queue_manual_run(
        conversation_id,
        logger=test_logger(),
        now=datetime.now(UTC),
    )
    second = await dreaming_service.queue_manual_run(
        conversation_id,
        logger=test_logger(),
        now=datetime.now(UTC),
    )

    assert first is not None
    assert second is not None
    assert_eq(first.id, second.id)

    async with conversation_service.database.transaction() as tx:
        total = await tx.fetch_all(
            select(DreamRun).where(DreamRun.conversation_id.eq(conversation_id))
        )
    assert_eq(len(total), 1)


@test()
async def run_completion_advances_conversation_cursor() -> None:
    """Successful completion advances the per-conversation high-water cursor."""
    conversation_service, dreaming_service, conversation_id = await _fixture()

    settled = await _append(
        conversation_service,
        conversation_id=conversation_id,
        role="user",
        content="hello",
    )
    assistant = await _append(
        conversation_service,
        conversation_id=conversation_id,
        role="assistant",
        content="ack",
    )
    await _retime(
        settled.id,
        database=conversation_service.database,
        when=datetime.now(UTC) - timedelta(minutes=50),
    )
    await _retime(
        assistant.id,
        database=conversation_service.database,
        when=datetime.now(UTC) - timedelta(minutes=49),
    )

    run = await dreaming_service.queue_assimilation_run(
        conversation_id,
        logger=test_logger(),
        now=datetime.now(UTC),
    )
    assert run is not None

    completed = await dreaming_service.complete_run(
        run.id,
        logger=test_logger(),
        now=datetime.now(UTC),
        status="success",
    )
    assert_eq(completed.status, "success")

    async with conversation_service.database.transaction() as tx:
        cursor = await tx.fetch_one_or_none(
            select(DreamConversationCursor).where(
                DreamConversationCursor.conversation_id.eq(conversation_id)
            )
        )
    assert cursor is not None
    assert_eq(cursor.last_assimilated_seq, completed.evidence_end_seq)


@test()
async def claim_next_run_marks_queued_run_running() -> None:
    """Claiming advances queued runs to `running` while preserving evidence window."""
    conversation_service, dreaming_service, conversation_id = await _fixture()

    message = await _append(
        conversation_service,
        conversation_id=conversation_id,
        role="user",
        content="queue me",
    )
    await _retime(
        message.id,
        database=conversation_service.database,
        when=datetime.now(UTC) - timedelta(minutes=50),
    )

    run = await dreaming_service.queue_manual_run(
        conversation_id,
        logger=test_logger(),
        now=datetime.now(UTC),
    )
    assert run is not None

    claimed = await dreaming_service.claim_next_run(
        logger=test_logger(),
        now=datetime.now(UTC),
    )
    assert claimed is not None
    assert_eq(claimed.id, run.id)
    assert_eq(claimed.status, "running")

    again = await dreaming_service.claim_next_run(
        logger=test_logger(),
        now=datetime.now(UTC),
    )
    assert_is_none(again)


@test()
async def complete_run_replay_is_idempotent() -> None:
    """Completing a run twice keeps the first terminal state."""
    conversation_service, dreaming_service, conversation_id = await _fixture()

    message = await _append(
        conversation_service,
        conversation_id=conversation_id,
        role="user",
        content="once",
    )
    await _retime(
        message.id,
        database=conversation_service.database,
        when=datetime.now(UTC) - timedelta(minutes=50),
    )

    run = await dreaming_service.queue_manual_run(
        conversation_id,
        logger=test_logger(),
        now=datetime.now(UTC),
    )
    assert run is not None

    success = await dreaming_service.complete_run(
        run.id,
        logger=test_logger(),
        now=datetime.now(UTC),
        status="success",
    )
    assert_eq(success.status, "success")

    replay = await dreaming_service.complete_run(
        run.id,
        logger=test_logger(),
        now=datetime.now(UTC),
        status="failed",
        error="late",
    )
    assert_eq(replay.status, "success")


@test()
async def run_worker_marks_callback_failure() -> None:
    """Exceptions from callbacks become failed terminal completions."""
    conversation_service, dreaming_service, conversation_id = await _fixture()

    message = await _append(
        conversation_service,
        conversation_id=conversation_id,
        role="user",
        content="badwork",
    )
    await _retime(
        message.id,
        database=conversation_service.database,
        when=datetime.now(UTC) - timedelta(minutes=50),
    )

    run = await dreaming_service.queue_manual_run(
        conversation_id,
        logger=test_logger(),
        now=datetime.now(UTC),
    )
    assert run is not None

    async def _explode(
        run: object, *, logger: object
    ) -> DreamRunExecutionResult:  # pragma: no cover
        raise RuntimeError("boom")

    worker = DreamingWorker(
        dreaming_service,
        _explode,
        logger=test_logger(),
    )
    completed = await worker.run_once()

    assert completed is not None
    assert_eq(completed.status, "failed")
    assert completed.error is not None


@test()
async def run_worker_sets_running_run_to_success() -> None:
    """Worker loop executes callback and persists terminal completion."""
    conversation_service, dreaming_service, conversation_id = await _fixture()

    message = await _append(
        conversation_service,
        conversation_id=conversation_id,
        role="user",
        content="work it",
    )
    await _retime(
        message.id,
        database=conversation_service.database,
        when=datetime.now(UTC) - timedelta(minutes=50),
    )

    run = await dreaming_service.queue_manual_run(
        conversation_id,
        logger=test_logger(),
        now=datetime.now(UTC),
    )
    assert run is not None

    worker = DreamingWorker(
        dreaming_service,
        _Callback(
            DreamRunExecutionResult(status="success", error=None),
        ),
        logger=test_logger(),
    )
    completed = await worker.run_once()

    assert completed is not None
    assert_eq(completed.status, "success")


@test()
async def mutation_tool_call_id_is_deterministic_for_a_run() -> None:
    """Mutation idempotency keys are stable for the same run envelope."""
    _, dreaming_service, conversation_id = await _fixture()

    async with dreaming_service.database.transaction() as tx:
        left = await tx.execute(
            insert(
                DreamRun(
                    id=UUID("019f0000-0000-7000-8000-000000000002"),
                    conversation_id=conversation_id,
                    kind="manual",
                    status="queued",
                    evidence_start_seq=1,
                    evidence_end_seq=2,
                )
            ).returning()
        )
        right = await tx.execute(
            insert(
                DreamRun(
                    id=UUID("019f0000-0000-7000-8000-000000000003"),
                    conversation_id=conversation_id,
                    kind="manual",
                    status="queued",
                    evidence_start_seq=1,
                    evidence_end_seq=2,
                )
            ).returning()
        )
    assert left is not None
    assert right is not None

    with TemporaryDirectory() as workspace_root:
        coordinator = DreamingMutationCoordinator(
            dreaming_service.database,
            Path(workspace_root),
        )
        first = coordinator.mutation_tool_call_id(left)
        second = coordinator.mutation_tool_call_id(left)
        third = coordinator.mutation_tool_call_id(right)

        assert_eq(first, second)
        assert_eq(first != third, True)


@test()
async def production_executor_curates_evidence_into_claims() -> None:
    """Production executor writes model-curated Claims instead of transcript text."""
    conversation_service, dreaming_service, conversation_id = await _fixture()

    message = await _append(
        conversation_service,
        conversation_id=conversation_id,
        role="user",
        content="I liked Roboquest",
    )
    await _retime(
        message.id,
        database=conversation_service.database,
        when=datetime.now(UTC) - timedelta(minutes=50),
    )

    run = await dreaming_service.queue_manual_run(
        conversation_id,
        logger=test_logger(),
        now=datetime.now(UTC),
    )
    assert run is not None

    class _Runner:
        def __init__(self) -> None:
            self.prompts: list[str] = []

        async def run(self, prompt: str) -> str:
            self.prompts.append(prompt)
            return (
                "## Gaming\n\n"
                f"- You like Roboquest. [source](tether://message/{message.id})"
            )

    with TemporaryDirectory() as workspace_root:
        root = Path(workspace_root)
        coordinator = DreamingMutationCoordinator(dreaming_service.database, root)
        expected_tool_call_id = coordinator.mutation_tool_call_id(run)
        runner = _Runner()
        executor = ConversationWindowDreamingExecutor(
            conversation_service,
            workspace_root=root,
            mutation_coordinator=coordinator,
            curation_runner=runner,
        )
        result = await executor(run, logger=test_logger())

        assert_eq(result.status, "success")
        assert_eq(len(runner.prompts), 1)
        assert "I liked Roboquest" in runner.prompts[0]

        written = root / str(run.conversation_id) / f"{run.id}.md"
        assert written.exists()
        document = written.read_text(encoding="utf-8")
        assert "title: Gaming" in document
        assert "You like Roboquest" in document
        assert "## Dream slice" not in document

        async with dreaming_service.database.transaction() as tx:
            mutations = await tx.fetch_all(
                select(DreamingMutation).where(DreamingMutation.run_id.eq(run.id))
            )
            assert_eq(len(mutations), 1)
            assert_eq(mutations[0].status, "acknowledged")
            assert_eq(mutations[0].tool_call_id, expected_tool_call_id)
            files = await tx.fetch_all(
                select(DreamingWorkspaceFile).where(
                    DreamingWorkspaceFile.path.eq(f"{run.conversation_id}/{run.id}.md")
                )
            )
            assert_eq(len(files), 1)
            assert_eq(files[0].is_tombstone, 0)
            assert_eq(files[0].actor, "dream")
            assert_eq(files[0].source_run_id, run.id)
            assert_eq(files[0].source_tool_call_id, expected_tool_call_id)

        repeat = await executor(run, logger=test_logger())
        assert_eq(repeat.status, "no_op")


@test()
async def production_executor_instructs_curator_to_use_second_person() -> None:
    """The curation task states the user-facing Memory voice."""
    conversation_service, dreaming_service, conversation_id = await _fixture()

    _ = await _append(
        conversation_service,
        conversation_id=conversation_id,
        role="user",
        content="I liked Roboquest",
    )
    run = await dreaming_service.queue_manual_run(
        conversation_id,
        logger=test_logger(),
        now=datetime.now(UTC),
    )
    assert run is not None

    class _Runner:
        def __init__(self) -> None:
            self.prompts: list[str] = []

        async def run(self, prompt: str) -> str:
            self.prompts.append(prompt)
            return "NO_CHANGES"

    with TemporaryDirectory() as workspace_root:
        runner = _Runner()
        result = await ConversationWindowDreamingExecutor(
            conversation_service,
            workspace_root=Path(workspace_root),
            curation_runner=runner,
        )(run, logger=test_logger())

    assert_eq(result.status, "no_op")
    assert "Address the user as `you` and `your`" in runner.prompts[0]
    assert "Begin every Claim with `You` or `Your`" in runner.prompts[0]
    assert 'Never call them "the user"' in runner.prompts[0]
    assert "[source](tether://...)" in runner.prompts[0]
    assert "Never use raw HTML for Evidence links" in runner.prompts[0]


@test()
async def production_executor_accepts_final_assistant_message_citation() -> None:
    """A succeeded turn's final answer may support an agent-derived Claim."""
    conversation_service, dreaming_service, conversation_id = await _fixture()

    async with conversation_service.database.transaction() as transaction:
        turn = await transaction.execute(
            insert(
                ConversationTurn(
                    conversation_id=conversation_id,
                    origin="interactive",
                    status="succeeded",
                    turn_seq=1,
                )
            ).returning()
        )
    _ = await conversation_service.append_message(
        MessageDraft(
            content="What patterns are in my liked videos?",
            conversation_id=conversation_id,
            role="user",
            turn_id=turn.id,
        )
    )
    conclusion = await conversation_service.append_message(
        MessageDraft(
            content="Your feed is mostly industry sense-making, not tutorials.",
            conversation_id=conversation_id,
            role="assistant",
            turn_id=turn.id,
        )
    )
    run = await dreaming_service.queue_manual_run(
        conversation_id,
        logger=test_logger(),
        now=datetime.now(UTC),
    )
    assert run is not None

    class _Runner:
        async def run(self, prompt: str) -> str:
            return (
                "## Learning\n\n- You use YouTube mainly for industry sense-making. "
                f"[source](tether://message/{conclusion.id})"
            )

    with TemporaryDirectory() as workspace_root:
        result = await ConversationWindowDreamingExecutor(
            conversation_service,
            workspace_root=Path(workspace_root),
            curation_runner=_Runner(),
        )(run, logger=test_logger())

    assert_eq(result.status, "success")


@test(
    [
        Param(value="Likes Roboquest.", name="omitted_subject"),
        Param(value="The user likes Roboquest.", name="the_user"),
        Param(value="He likes Roboquest.", name="he"),
        Param(value="His favorite game is Roboquest.", name="his"),
        Param(value="She likes Roboquest.", name="she"),
        Param(value="Her favorite game is Roboquest.", name="her"),
        Param(value="They like Roboquest.", name="they"),
        Param(value="Their favorite game is Roboquest.", name="their"),
    ]
)
async def production_executor_rejects_third_person_user_claims(value: str) -> None:
    """Conversation curation cannot refer to the user in third person."""
    conversation_service, dreaming_service, conversation_id = await _fixture()

    message = await _append(
        conversation_service,
        conversation_id=conversation_id,
        role="user",
        content="I liked Roboquest",
    )
    run = await dreaming_service.queue_manual_run(
        conversation_id,
        logger=test_logger(),
        now=datetime.now(UTC),
    )
    assert run is not None

    class _Runner:
        async def run(self, prompt: str) -> str:
            return f"## Gaming\n\n- {value} [source](tether://message/{message.id})"

    with TemporaryDirectory() as workspace_root:
        root = Path(workspace_root)
        result = await ConversationWindowDreamingExecutor(
            conversation_service,
            workspace_root=root,
            curation_runner=_Runner(),
        )(run, logger=test_logger())

        assert_eq(result.status, "failed")
        assert_eq(result.error, "Memory Claims must address the user as you or your")
        assert not (root / str(conversation_id) / f"{run.id}.md").exists()


@test()
async def production_executor_rejects_unsupported_claim_citations() -> None:
    """Dreaming cannot write a Claim citing anything outside its Evidence bounds."""
    conversation_service, dreaming_service, conversation_id = await _fixture()

    await _append(
        conversation_service,
        conversation_id=conversation_id,
        role="user",
        content="I liked Roboquest",
    )
    run = await dreaming_service.queue_manual_run(
        conversation_id,
        logger=test_logger(),
        now=datetime.now(UTC),
    )
    assert run is not None

    class _Runner:
        async def run(self, prompt: str) -> str:
            return (
                "## Gaming\n\n- You like Roboquest. "
                "[source](tether://message/019f0000-0000-7000-8000-000000000099)"
            )

    with TemporaryDirectory() as workspace_root:
        root = Path(workspace_root)
        result = await ConversationWindowDreamingExecutor(
            conversation_service,
            workspace_root=root,
            curation_runner=_Runner(),
        )(run, logger=test_logger())

        assert_eq(result.status, "failed")
        assert result.error is not None
        assert "outside bounded supporting Evidence" in result.error
        assert not (root / str(conversation_id) / f"{run.id}.md").exists()


@test()
async def production_executor_requires_a_citation_for_every_claim() -> None:
    """Uncited model prose cannot become a current Memory Claim."""
    conversation_service, dreaming_service, conversation_id = await _fixture()

    await _append(
        conversation_service,
        conversation_id=conversation_id,
        role="user",
        content="I liked Roboquest",
    )
    run = await dreaming_service.queue_manual_run(
        conversation_id,
        logger=test_logger(),
        now=datetime.now(UTC),
    )
    assert run is not None

    class _Runner:
        async def run(self, prompt: str) -> str:
            return "## Gaming\n\n- You like Roboquest."

    with TemporaryDirectory() as workspace_root:
        result = await ConversationWindowDreamingExecutor(
            conversation_service,
            workspace_root=Path(workspace_root),
            curation_runner=_Runner(),
        )(run, logger=test_logger())

    assert_eq(result.status, "failed")
    assert result.error is not None
    assert "must cite bounded supporting Evidence" in result.error


@test()
async def http_acknowledger_calls_internal_callback() -> None:
    """Production ACK callback calls through to the host mutation endpoint."""
    calls: list[tuple[str, str]] = []

    def _ack(request: httpx2.Request) -> httpx2.Response:
        calls.append((request.url.path, request.headers.get(TOOL_AUTH_HEADER, "")))
        return httpx2.Response(
            status_code=200,
            json={
                "run_id": str(request.url.path.split("/")[-3]),
                "acknowledged": True,
            },
        )

    run_id = UUID("019f08f0-0000-7000-8000-000000000002")
    tool_call_id = "tool-123"

    ack = HttpDreamingMutationAcknowledger(
        base_url="http://example.org",
        tool_secret="test-secret",
        http_transport=httpx2.MockTransport(_ack),
    )
    acknowledged, error = await ack(run_id, tool_call_id)

    assert_eq(acknowledged, True)
    assert_is_none(error)
    assert_eq(
        calls[0][0], f"/internal/dream-runs/{run_id}/mutations/{tool_call_id}/ack"
    )
    assert_eq(calls[0][1], "test-secret")


@test()
async def http_acknowledger_returns_detail_for_failed_callback() -> None:
    """HTTP failures surface their callback detail to caller."""

    def _ack(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            status_code=404,
            json={"detail": "mutation not found"},
        )

    ack = HttpDreamingMutationAcknowledger(
        base_url="http://example.org",
        tool_secret="test-secret",
        http_transport=httpx2.MockTransport(_ack),
    )
    acknowledged, error = await ack(
        UUID("019f08f0-0000-7000-8000-000000000003"),
        "tool-456",
    )

    assert_eq(acknowledged, False)
    assert_eq(error, "mutation not found")


@test()
async def settle_reports_not_found_for_unknown_mutation() -> None:
    """Settling an unrecorded mutation reports not_found without notifying."""
    _conversation_service, dreaming_service, _ = await _fixture()

    async def _acknowledger(run_id: UUID, tool_call_id: str) -> tuple[bool, str | None]:
        raise AssertionError("acknowledger must not be called for unknown mutations")

    with TemporaryDirectory() as workspace_root:
        coordinator = DreamingMutationCoordinator(
            dreaming_service.database,
            Path(workspace_root),
        )
        settlement = await coordinator.settle(
            uuid7(),
            "nope",
            acknowledger=_acknowledger,
        )

    assert_eq(settlement.outcome, "not_found")
    assert_eq(settlement.error, None)


@test()
async def settle_drives_recorded_mutation_through_acknowledger() -> None:
    """A recorded mutation is settled by notification and marked acknowledged."""
    _conversation_service, dreaming_service, _ = await _fixture()

    with TemporaryDirectory() as workspace_root:
        root = Path(workspace_root)
        coordinator = DreamingMutationCoordinator(dreaming_service.database, root)
        run_id = uuid7()
        _ = await coordinator.record_mutation(
            run_id=run_id,
            tool_call_id="tc-1",
            actor="dream",
            operation="write",
            workspace_path=root / "note.md",
            payload="payload",
        )
        calls: list[tuple[UUID, str]] = []

        async def _acknowledger(
            ack_run_id: UUID,
            ack_tool_call_id: str,
        ) -> tuple[bool, str | None]:
            # Mirror the production shape: the notifier (HTTP ack in prod)
            # reaches the host, which performs the real acknowledgement.
            calls.append((ack_run_id, ack_tool_call_id))
            return await coordinator.acknowledge_mutation(ack_run_id, ack_tool_call_id)

        settlement = await coordinator.settle(
            run_id,
            "tc-1",
            acknowledger=_acknowledger,
        )

        assert_eq(settlement.outcome, "settled")
        assert_eq(settlement.error, None)
        assert_eq(calls, [(run_id, "tc-1")])
        async with dreaming_service.database.transaction() as tx:
            mutation = await tx.fetch_one_or_none(
                select(DreamingMutation).where(DreamingMutation.run_id.eq(run_id))
            )
        assert mutation is not None
        assert_eq(mutation.status, "acknowledged")


@test()
async def settle_is_idempotent_for_already_acknowledged_mutation() -> None:
    """Re-settling an acknowledged mutation skips the notifier entirely."""
    _conversation_service, dreaming_service, _ = await _fixture()

    with TemporaryDirectory() as workspace_root:
        root = Path(workspace_root)
        coordinator = DreamingMutationCoordinator(dreaming_service.database, root)
        run_id = uuid7()
        _ = await coordinator.record_mutation(
            run_id=run_id,
            tool_call_id="tc-1",
            actor="dream",
            operation="write",
            workspace_path=root / "note.md",
            payload="payload",
        )
        first = await coordinator.settle(run_id, "tc-1")

        async def _acknowledger(
            run_id: UUID, tool_call_id: str
        ) -> tuple[bool, str | None]:
            raise AssertionError("acknowledger must not rerun for settled mutations")

        second = await coordinator.settle(
            run_id,
            "tc-1",
            acknowledger=_acknowledger,
        )

    assert_eq(first.outcome, "settled")
    assert_eq(second.outcome, "already_settled")
    assert_eq(second.error, None)


@test()
async def reconciliation_waits_for_inflight_dream_mutation() -> None:
    """Reconciliation cannot inspect a Dream write before its mutation is recorded."""
    _, dreaming_service, _ = await _fixture()

    with TemporaryDirectory() as workspace_root:
        root = Path(workspace_root)
        topic_path = root / "new-topic.md"
        coordinator = DreamingMutationCoordinator(dreaming_service.database, root)

        async with coordinator.mutation_scope():
            topic_path.write_text(
                "---\ntitle: New topic\n---\nDreamed content.\n",
                encoding="utf-8",
            )
            reconciliation = asyncio.create_task(
                coordinator.reconcile_workspace(logger=test_logger())
            )
            await asyncio.sleep(0)
            assert_eq(reconciliation.done(), False)
            _ = await coordinator.record_mutation(
                run_id=UUID("019f0000-0000-7000-8000-000000000001"),
                tool_call_id="write-new-topic",
                actor="dream",
                operation="write",
                workspace_path=topic_path,
                payload="dream payload",
            )

        _ = await reconciliation
        assert_eq(topic_path.exists(), True)


@test()
async def settle_defaults_to_direct_acknowledgement() -> None:
    """Without an injected notifier, settling reconciles in-process."""
    _conversation_service, dreaming_service, _ = await _fixture()

    with TemporaryDirectory() as workspace_root:
        root = Path(workspace_root)
        coordinator = DreamingMutationCoordinator(dreaming_service.database, root)
        run_id = uuid7()
        note = root / "note.md"
        note.write_text("dream body", encoding="utf-8")
        _ = await coordinator.record_mutation(
            run_id=run_id,
            tool_call_id="tc-1",
            actor="dream",
            operation="write",
            workspace_path=note,
            payload="payload",
        )

        settlement = await coordinator.settle(run_id, "tc-1")

        assert_eq(settlement.outcome, "settled")
        async with dreaming_service.database.transaction() as tx:
            file_row = await tx.fetch_one_or_none(
                select(DreamingWorkspaceFile).where(
                    DreamingWorkspaceFile.path.eq("note.md")
                )
            )
        assert file_row is not None
        assert_eq(file_row.source_run_id, run_id)


@test()
async def settle_reports_acknowledger_failure_without_touching_the_record() -> None:
    """A failed notification leaves the mutation retryable (ADR-0022)."""
    _conversation_service, dreaming_service, _ = await _fixture()

    with TemporaryDirectory() as workspace_root:
        root = Path(workspace_root)
        coordinator = DreamingMutationCoordinator(dreaming_service.database, root)
        run_id = uuid7()
        _ = await coordinator.record_mutation(
            run_id=run_id,
            tool_call_id="tc-1",
            actor="dream",
            operation="write",
            workspace_path=root / "note.md",
            payload="payload",
        )

        async def _acknowledger(
            run_id: UUID, tool_call_id: str
        ) -> tuple[bool, str | None]:
            return False, "simulated notifier failure"

        settlement = await coordinator.settle(
            run_id,
            "tc-1",
            acknowledger=_acknowledger,
        )

        assert_eq(settlement.outcome, "failed")
        assert_eq(settlement.error, "simulated notifier failure")
        async with dreaming_service.database.transaction() as tx:
            mutation = await tx.fetch_one_or_none(
                select(DreamingMutation).where(DreamingMutation.run_id.eq(run_id))
            )
        assert mutation is not None
        assert_eq(mutation.status, "executed")


@test()
async def manual_scan_queues_runs_only_for_conversations_with_new_evidence() -> None:
    """Dream-now covers every conversation; assimilated ones are skipped."""
    conversation_service, dreaming_service, first_id = await _fixture()

    await _append(
        conversation_service,
        conversation_id=first_id,
        role="user",
        content="pending evidence",
    )

    conversations = await conversation_service.list_conversations()
    template = conversations[0]
    async with conversation_service.database.transaction() as tx:
        second = await tx.execute(
            insert(Conversation(selected_model=template.selected_model)).returning()
        )
    await _append(
        conversation_service,
        conversation_id=second.id,
        role="user",
        content="already dreamed",
    )
    assimilated = await dreaming_service.queue_manual_run(
        second.id,
        logger=test_logger(),
        now=datetime.now(UTC),
    )
    assert assimilated is not None
    _ = await dreaming_service.complete_run(
        assimilated.id,
        status="success",
        logger=test_logger(),
    )

    runs = await dreaming_service.queue_pending_manual_runs(
        logger=test_logger(),
        now=datetime.now(UTC),
    )

    assert_eq(len(runs), 1)
    assert_eq(runs[0].conversation_id, first_id)
    assert_eq(runs[0].kind, "manual")
    assert_eq(runs[0].status, "queued")


@test()
async def assimilation_scan_queues_settled_evidence_and_skips_recent() -> None:
    """The periodic scan queues settled evidence but honors the settle window."""
    conversation_service, dreaming_service, conversation_id = await _fixture()

    settled = await _append(
        conversation_service,
        conversation_id=conversation_id,
        role="user",
        content="old enough to dream",
    )
    await _retime(
        settled.id,
        database=conversation_service.database,
        when=datetime.now(UTC) - timedelta(minutes=25),
    )

    runs = await dreaming_service.queue_settled_assimilation_runs(
        logger=test_logger(),
        now=datetime.now(UTC),
    )
    assert_eq(len(runs), 1)
    assert_eq(runs[0].kind, "assimilation")

    recent = await _append(
        conversation_service,
        conversation_id=conversation_id,
        role="user",
        content="too fresh",
    )
    await _retime(
        recent.id,
        database=conversation_service.database,
        when=datetime.now(UTC),
    )

    again = await dreaming_service.queue_settled_assimilation_runs(
        logger=test_logger(),
        now=datetime.now(UTC),
    )
    assert_eq(again, [])


@test()
async def production_executor_replays_pending_mutation_ack() -> None:
    """Executor replays a prior successful file mutation whose ack previously failed."""

    class _FlakyCoordinator(DreamingMutationCoordinator):
        def __init__(self, database: Database, workspace_root: Path) -> None:
            super().__init__(database, workspace_root)
            self.fail_once = True

        async def acknowledge_mutation(
            self,
            run_id: UUID,
            tool_call_id: str,
        ) -> tuple[bool, str | None]:
            if self.fail_once:
                self.fail_once = False
                return False, "simulated notifier failure"
            return await super().acknowledge_mutation(run_id, tool_call_id)

    conversation_service, dreaming_service, conversation_id = await _fixture()

    message = await _append(
        conversation_service,
        conversation_id=conversation_id,
        role="user",
        content="retry later",
    )
    await _retime(
        message.id,
        database=conversation_service.database,
        when=datetime.now(UTC) - timedelta(minutes=50),
    )

    run = await dreaming_service.queue_manual_run(
        conversation_id,
        logger=test_logger(),
        now=datetime.now(UTC),
    )
    assert run is not None

    with TemporaryDirectory() as workspace_root:
        root = Path(workspace_root)
        coordinator = _FlakyCoordinator(dreaming_service.database, root)
        executor = ConversationWindowDreamingExecutor(
            conversation_service,
            workspace_root=root,
            mutation_coordinator=coordinator,
        )

        failed = await executor(run, logger=test_logger())
        assert_eq(failed.status, "failed")

        # Same run should recover by retrying the same deterministic tool_call_id.
        recovered = await executor(run, logger=test_logger())
        assert_eq(recovered.status, "success")

        async with dreaming_service.database.transaction() as tx:
            mutations = await tx.fetch_all(
                select(DreamingMutation)
                .where(DreamingMutation.run_id.eq(run.id))
                .where(
                    DreamingMutation.tool_call_id.eq(
                        coordinator.mutation_tool_call_id(run)
                    )
                )
            )
            assert_eq(len(mutations), 1)
            assert_eq(mutations[0].status, "acknowledged")


@test()
async def production_executor_marks_run_noop_for_empty_window() -> None:
    """Default production executor no-ops when a run has no bounded rows."""
    conversation_service, dreaming_service, conversation_id = await _fixture()

    message = await _append(
        conversation_service,
        conversation_id=conversation_id,
        role="user",
        content="one message",
    )
    await _retime(
        message.id,
        database=conversation_service.database,
        when=datetime.now(UTC) - timedelta(minutes=50),
    )

    async with dreaming_service.database.transaction() as tx:
        stale = await tx.execute(
            insert(
                DreamRun(
                    conversation_id=conversation_id,
                    kind="manual",
                    status="queued",
                    evidence_start_seq=99,
                    evidence_end_seq=100,
                )
            ).returning()
        )
        assert stale is not None
    with TemporaryDirectory() as workspace_root:
        root = Path(workspace_root)
        executor = ConversationWindowDreamingExecutor(
            conversation_service,
            workspace_root=root,
        )
        result = await executor(stale, logger=test_logger())

        assert_eq(result.status, "no_op")
        target = root / str(stale.conversation_id)
        assert not target.exists()


@test()
async def production_executor_bounds_window_exactly() -> None:
    """Executor consumes only the run's inclusive message bounds."""
    conversation_service, _, conversation_id = await _fixture()

    first = await _append(
        conversation_service,
        conversation_id=conversation_id,
        role="user",
        content="alpha",
    )
    second = await _append(
        conversation_service,
        conversation_id=conversation_id,
        role="assistant",
        content="beta",
    )
    third = await _append(
        conversation_service,
        conversation_id=conversation_id,
        role="user",
        content="gamma",
    )
    await _retime(
        first.id,
        database=conversation_service.database,
        when=datetime.now(UTC) - timedelta(minutes=80),
    )
    await _retime(
        second.id,
        database=conversation_service.database,
        when=datetime.now(UTC) - timedelta(minutes=70),
    )
    await _retime(
        third.id,
        database=conversation_service.database,
        when=datetime.now(UTC) - timedelta(minutes=60),
    )

    async with conversation_service.database.transaction() as tx:
        run = await tx.execute(
            insert(
                DreamRun(
                    conversation_id=conversation_id,
                    kind="manual",
                    status="queued",
                    evidence_start_seq=2,
                    evidence_end_seq=3,
                )
            ).returning()
        )
        assert run is not None

    with TemporaryDirectory() as workspace_root:
        root = Path(workspace_root)
        executor = ConversationWindowDreamingExecutor(
            conversation_service,
            workspace_root=root,
        )
        result = await executor(run, logger=test_logger())
        assert_eq(result.status, "success")

        written = root / str(conversation_id) / f"{run.id}.md"
        contents = written.read_text(encoding="utf-8")
        assert "alpha" not in contents
        assert "- 1 user" not in contents
        assert " 2 assistant" in contents
        assert " 3 user" in contents


@test()
async def explicit_window_is_bounded_by_message_count() -> None:
    """Windowing keeps the queued run below the configured per-run message cap."""
    conversation_service, _, conversation_id = await _fixture()

    capped = DreamingService(
        conversation_service.database,
        max_messages_per_run=2,
    )
    for index in range(1, 6):
        message = await _append(
            conversation_service,
            conversation_id=conversation_id,
            role="user",
            content=f"m{index}",
        )
        await _retime(
            message.id,
            database=conversation_service.database,
            when=datetime.now(UTC) - timedelta(minutes=30),
        )

    run = await capped.queue_manual_run(
        conversation_id,
        logger=test_logger(),
        now=datetime.now(UTC),
    )

    assert run is not None
    assert_eq(run.evidence_start_seq, 1)
    assert_eq(run.evidence_end_seq, 2)


# ---------------------------------------------------------------------------
# Maintenance runs: consolidation of fragmented topic files (issue #601)
# ---------------------------------------------------------------------------


def _maintenance_service(
    conversation_service: ConversationService,
    workspace_root: Path,
) -> DreamingService:
    """Dreaming service wired to a scratch workspace with a 12h cadence."""
    return DreamingService(
        conversation_service.database,
        workspace_root=workspace_root,
        maintenance_interval=timedelta(hours=12),
    )


def _write_topic(  # noqa: PLR0913 - fixture helper mirrors document shape
    workspace_root: Path,
    folder: UUID | str,
    name: str,
    *,
    title: str,
    body: str,
    uris: tuple[str, ...] = (),
    review_after: str | None = None,
) -> Path:
    """Write one canonical topic document into the conversation folder."""
    key = str(folder)
    directory = workspace_root / key if key else workspace_root
    directory.mkdir(parents=True, exist_ok=True)
    evidence_lines = "".join(f"- {uri}\n" for uri in uris)
    review_line = "" if review_after is None else f"review_after: {review_after}\n"
    document = (
        f"---\ntitle: {title}\nevidence:\n{evidence_lines}{review_line}---\n\n{body}\n"
    )
    path = directory / name
    _ = path.write_text(document, encoding="utf-8")
    return path


@fixture
async def maintenance_fixture() -> AsyncGenerator[
    tuple[ConversationService, DreamingService, UUID, Path]
]:
    """Conversation + Dreaming stack with a scratch memory workspace."""
    db = await Database.initialize(backend=Config(database=":memory:"))
    await create_conversation_schema(db)
    await create_dreaming_schema(db)
    conversation_service = ConversationService(db)
    conversation = (await conversation_service.list_conversations())[0]
    scratch = TemporaryDirectory()
    root = Path(scratch.name)
    yield (
        conversation_service,
        _maintenance_service(conversation_service, root),
        conversation.id,
        root,
    )
    await db.close()
    scratch.cleanup()


@test()
async def maintenance_run_queues_for_a_single_topic() -> None:
    """A single Topic is eligible for temporal review without fragmentation."""
    _, service, conversation_id, root = await load_fixture(maintenance_fixture())
    _ = _write_topic(
        root, conversation_id, "a.md", title="Gaming", body="- Likes Roboquest."
    )

    queued = await service.queue_maintenance_run(
        conversation_id,
        logger=test_logger(),
        now=datetime.now(UTC),
    )

    assert queued is not None
    assert_eq(queued.kind, "maintenance")


@test()
async def maintenance_run_queues_for_a_fragmented_conversation() -> None:
    """Two or more topic files make the conversation eligible."""
    _, service, conversation_id, root = await load_fixture(maintenance_fixture())
    _ = _write_topic(
        root, conversation_id, "a.md", title="Gaming", body="- Likes Roboquest."
    )
    _ = _write_topic(
        root, conversation_id, "b.md", title="Gaming notes", body="- Owns a Switch."
    )

    run = await service.queue_maintenance_run(
        conversation_id,
        logger=test_logger(),
        now=datetime.now(UTC),
    )

    assert run is not None
    assert_eq(run.kind, "maintenance")
    assert_eq(run.status, "queued")


@test()
async def due_maintenance_scan_respects_the_interval() -> None:
    """A recently maintained conversation is skipped until the interval elapses."""
    _, service, conversation_id, root = await load_fixture(maintenance_fixture())
    _ = _write_topic(
        root, conversation_id, "a.md", title="Gaming", body="- Likes Roboquest."
    )
    _ = _write_topic(
        root, conversation_id, "b.md", title="Gaming notes", body="- Owns a Switch."
    )
    now = datetime.now(UTC)
    first = await service.queue_maintenance_runs(logger=test_logger(), now=now)
    assert_eq(len(first), 1)
    claimed = await service.claim_next_run(logger=test_logger())
    assert claimed is not None
    _ = await service.complete_run(claimed.id, status="success", logger=test_logger())

    soon = await service.queue_maintenance_runs(
        logger=test_logger(), now=now + timedelta(hours=1)
    )
    assert_eq(soon, [])

    later = await service.queue_maintenance_runs(
        logger=test_logger(), now=now + timedelta(hours=13)
    )
    assert_eq(len(later), 1)


@test()
async def due_maintenance_scan_waits_for_pending_evidence() -> None:
    """Assimilation always wins: unassimilated evidence blocks maintenance."""
    conversation_service, service, conversation_id, root = await load_fixture(
        maintenance_fixture()
    )
    _ = _write_topic(
        root, conversation_id, "a.md", title="Gaming", body="- Likes Roboquest."
    )
    _ = _write_topic(
        root, conversation_id, "b.md", title="Gaming notes", body="- Owns a Switch."
    )
    message = await _append(
        conversation_service,
        conversation_id=conversation_id,
        role="user",
        content="I finished the campaign",
    )
    await _retime(
        message.id,
        database=conversation_service.database,
        when=datetime.now(UTC) - timedelta(hours=2),
    )

    queued = await service.queue_maintenance_runs(
        logger=test_logger(), now=datetime.now(UTC)
    )
    assert_eq(queued, [])


@test()
async def maintenance_completion_never_advances_the_assimilation_cursor() -> None:
    """Consolidation is not evidence: the cursor must stay put."""
    conversation_service, service, conversation_id, root = await load_fixture(
        maintenance_fixture()
    )
    _ = _write_topic(
        root, conversation_id, "a.md", title="Gaming", body="- Likes Roboquest."
    )
    _ = _write_topic(
        root, conversation_id, "b.md", title="Gaming notes", body="- Owns a Switch."
    )
    async with conversation_service.database.transaction() as tx:
        _ = await tx.execute(
            insert(
                DreamConversationCursor(
                    conversation_id=conversation_id,
                    last_assimilated_seq=5,
                )
            )
        )

    run = await service.queue_maintenance_run(
        conversation_id,
        logger=test_logger(),
        now=datetime.now(UTC),
    )
    assert run is not None
    _ = await service.complete_run(run.id, status="success", logger=test_logger())

    async with conversation_service.database.transaction() as tx:
        cursor = await tx.fetch_one_or_none(
            select(DreamConversationCursor).where(
                DreamConversationCursor.conversation_id.eq(conversation_id)
            )
        )
        assert cursor is not None
        assert_eq(cursor.last_assimilated_seq, 5)


class _ConsolidationRunner:
    """Scripted consolidation runner recording prompts."""

    def __init__(self, response: str) -> None:
        self.response = response
        self.prompts: list[str] = []

    async def run(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.response


@test()
async def maintenance_executor_merges_fragmented_topics() -> None:
    """The executor applies a consolidated rewrite as recorded mutations."""
    conversation_service, service, conversation_id, root = await load_fixture(
        maintenance_fixture()
    )
    first_evidence = await _append(
        conversation_service,
        conversation_id=conversation_id,
        role="user",
        content="I like co-op shooters.",
    )
    second_evidence = await _append(
        conversation_service,
        conversation_id=conversation_id,
        role="user",
        content="Roboquest is a favorite.",
    )
    first_uri = f"tether://message/{first_evidence.id}"
    second_uri = f"tether://message/{second_evidence.id}"
    first = _write_topic(
        root,
        conversation_id,
        "a.md",
        title="Gaming",
        body="## Gaming\n\n- Likes co-op shooters.",
        uris=(first_uri,),
    )
    second = _write_topic(
        root,
        conversation_id,
        "b.md",
        title="Gaming notes",
        body="## Gaming notes\n\n- Likes Roboquest.",
        uris=(second_uri,),
    )
    async with conversation_service.database.transaction() as transaction:
        _ = await transaction.execute(
            insert(
                DreamConversationCursor(
                    conversation_id=conversation_id,
                    last_assimilated_seq=2,
                )
            )
        )
    run = await service.queue_maintenance_run(
        conversation_id,
        logger=test_logger(),
        now=datetime.now(UTC),
    )
    assert run is not None
    merged = "".join(
        (
            f"=== {conversation_id}/gaming.md ===\n",
            "---\n",
            "title: Gaming\n",
            "---\n\n",
            "## Gaming\n\n",
            "- You like co-op shooters such as Roboquest. ",
            "[source](citation:E1) [source](citation:E2)\n",
        )
    )
    executor = MaintenanceDreamingExecutor(
        conversation_service.database,
        workspace_root=root,
        agent=MaintenanceDreamingAgent(
            curator=_ConsolidationRunner(merged),
            verifier=_ConsolidationRunner("APPROVED"),
        ),
    )

    result = await executor(run, logger=test_logger())

    assert_eq(result.status, "success")
    assert not first.exists()
    assert not second.exists()
    merged_path = root / str(conversation_id) / "gaming.md"
    assert merged_path.exists()
    assert "You like co-op shooters such as Roboquest" in merged_path.read_text(
        encoding="utf-8"
    )

    async with conversation_service.database.transaction() as tx:
        mutations = await tx.fetch_all(
            select(DreamingMutation).where(DreamingMutation.run_id.eq(run.id))
        )
        operations = sorted(mutation.operation for mutation in mutations)
        assert_eq(operations, ["delete", "delete", "write"])
        assert all(mutation.status == "acknowledged" for mutation in mutations)
        progress = await tx.fetch_all(select(DreamMaintenanceProgress).all())
        assert_eq([row.path for row in progress], [f"{conversation_id}/gaming.md"])


@test()
async def maintenance_executor_resolves_short_citation_aliases() -> None:
    """Curators handle short aliases while Memory retains canonical Evidence URIs."""
    conversation_service, service, conversation_id, root = await load_fixture(
        maintenance_fixture()
    )
    evidence = await _append(
        conversation_service,
        conversation_id=conversation_id,
        role="user",
        content="I like Roboquest.",
    )
    canonical_uri = f"tether://message/{evidence.id}"
    topic = _write_topic(
        root,
        conversation_id,
        "gaming.md",
        title="Gaming",
        body=f"- You like Roboquest. [source]({canonical_uri})",
        uris=(canonical_uri,),
    )
    async with conversation_service.database.transaction() as transaction:
        _ = await transaction.execute(
            insert(
                DreamConversationCursor(
                    conversation_id=conversation_id,
                    last_assimilated_seq=evidence.seq,
                )
            )
        )
    run = await service.queue_maintenance_run(
        conversation_id,
        logger=test_logger(),
        now=datetime.now(UTC),
    )
    assert run is not None
    curator = _ConsolidationRunner(
        "".join(
            (
                f"=== {conversation_id}/gaming.md ===\n",
                "---\n",
                "title: Gaming\n",
                "evidence:\n",
                "- citation:E1\n",
                "---\n\n",
                "- You like Roboquest. [source](citation:E1)\n",
            )
        )
    )
    executor = MaintenanceDreamingExecutor(
        conversation_service.database,
        workspace_root=root,
        agent=MaintenanceDreamingAgent(
            curator=curator,
            verifier=_ConsolidationRunner("APPROVED"),
        ),
    )

    result = await executor(run, logger=test_logger())

    assert_eq(result.status, "success")
    assert "citation:E1" in curator.prompts[0]
    assert "tether://" not in curator.prompts[0]
    contents = topic.read_text(encoding="utf-8")
    assert canonical_uri in contents
    assert "citation:E1" not in contents


@test()
async def maintenance_executor_keeps_multi_digit_aliases_unambiguous() -> None:
    """Alias resolution does not treat `citation:E1` as a prefix of `citation:E10`."""
    conversation_service, service, conversation_id, root = await load_fixture(
        maintenance_fixture()
    )
    canonical_uris = tuple(
        f"tether://health-connect/record/{record}"
        for record in (*"abcdefghi", "target")
    )
    topic = _write_topic(
        root,
        conversation_id,
        "health.md",
        title="Health",
        body=f"- Your latest measurement is recorded. [source]({canonical_uris[-1]})",
        uris=canonical_uris,
    )
    run = await service.queue_maintenance_run(
        conversation_id,
        logger=test_logger(),
        now=datetime.now(UTC),
    )
    assert run is not None
    proposed = "".join(
        (
            f"=== {conversation_id}/health.md ===\n",
            "---\n",
            "title: Health\n",
            "---\n\n",
            "- Your latest measurement is recorded. [source](citation:E10)\n",
        )
    )
    executor = MaintenanceDreamingExecutor(
        conversation_service.database,
        workspace_root=root,
        agent=MaintenanceDreamingAgent(
            curator=_ConsolidationRunner(proposed),
            verifier=_ConsolidationRunner("APPROVED"),
        ),
    )

    result = await executor(run, logger=test_logger())

    assert_eq(result.status, "success")
    contents = topic.read_text(encoding="utf-8")
    assert canonical_uris[-1] in contents
    assert "citation:E10" not in contents


@test(
    [
        Param(value="citation:E999", name="unknown"),
        Param(value="citation:", name="empty"),
        Param(value="citation:not-an-alias", name="malformed"),
    ]
)
async def maintenance_executor_rejects_unknown_citation_aliases(value: str) -> None:
    """Unknown and malformed aliases fail closed without changing current Memory."""
    conversation_service, service, conversation_id, root = await load_fixture(
        maintenance_fixture()
    )
    canonical_uri = "tether://health-connect/record/weight-1"
    topic = _write_topic(
        root,
        conversation_id,
        "health.md",
        title="Health",
        body=f"- Your weight is stable. [source]({canonical_uri})",
        uris=(canonical_uri,),
    )
    original = topic.read_text(encoding="utf-8")
    run = await service.queue_maintenance_run(
        conversation_id,
        logger=test_logger(),
        now=datetime.now(UTC),
    )
    assert run is not None
    proposed = "".join(
        (
            f"=== {conversation_id}/health.md ===\n",
            "---\n",
            "title: Health\n",
            "---\n\n",
            f"- Your weight is stable. [source]({value})\n",
        )
    )
    executor = MaintenanceDreamingExecutor(
        conversation_service.database,
        workspace_root=root,
        agent=MaintenanceDreamingAgent(
            curator=_ConsolidationRunner(proposed),
            verifier=_ConsolidationRunner("APPROVED"),
        ),
    )

    result = await executor(run, logger=test_logger())

    assert_eq(result.status, "failed")
    assert_eq(
        result.error,
        f"maintenance output contains an unknown citation alias: {value}",
    )
    assert_eq(topic.read_text(encoding="utf-8"), original)


@test()
async def maintenance_executor_rejects_canonical_citations_from_curator() -> None:
    """Curator output must reference bounded Evidence through its short alias."""
    conversation_service, service, conversation_id, root = await load_fixture(
        maintenance_fixture()
    )
    canonical_uri = "tether://health-connect/record/weight-1"
    topic = _write_topic(
        root,
        conversation_id,
        "health.md",
        title="Health",
        body=f"- Your weight is stable. [source]({canonical_uri})",
        uris=(canonical_uri,),
    )
    original = topic.read_text(encoding="utf-8")
    run = await service.queue_maintenance_run(
        conversation_id,
        logger=test_logger(),
        now=datetime.now(UTC),
    )
    assert run is not None
    proposed = "".join(
        (
            f"=== {conversation_id}/health.md ===\n",
            "---\n",
            "title: Health\n",
            "---\n\n",
            f"- Your weight is stable. [source]({canonical_uri})\n",
        )
    )
    executor = MaintenanceDreamingExecutor(
        conversation_service.database,
        workspace_root=root,
        agent=MaintenanceDreamingAgent(
            curator=_ConsolidationRunner(proposed),
            verifier=_ConsolidationRunner("APPROVED"),
        ),
    )

    result = await executor(run, logger=test_logger())

    assert_eq(result.status, "failed")
    assert_eq(
        result.error,
        "maintenance output contains a canonical Evidence URI; use a citation alias",
    )
    assert_eq(topic.read_text(encoding="utf-8"), original)


@test()
async def maintenance_executor_prioritizes_a_due_topic() -> None:
    """A due `review_after` Topic enters a bounded batch before undated peers."""
    conversation_service, service, conversation_id, root = await load_fixture(
        maintenance_fixture()
    )
    for name in "abcdefgh":
        _ = _write_topic(
            root,
            conversation_id,
            f"{name}.md",
            title=name.upper(),
            body=f"- You have stable Claim {name}.",
        )
    _ = _write_topic(
        root,
        conversation_id,
        "z.md",
        title="Due",
        body="- You need temporal review.",
        review_after="2026-08-01",
    )
    run = await service.queue_maintenance_run(
        conversation_id,
        logger=test_logger(),
        now=datetime(2026, 9, 15, tzinfo=UTC),
    )
    assert run is not None
    curator = _ConsolidationRunner("NO_CHANGES")
    executor = MaintenanceDreamingExecutor(
        conversation_service.database,
        workspace_root=root,
        agent=MaintenanceDreamingAgent(
            clock=lambda: datetime(2026, 9, 15, tzinfo=UTC),
            curator=curator,
            verifier=_ConsolidationRunner("APPROVED"),
        ),
    )

    result = await executor(run, logger=test_logger())

    assert_eq(result.status, "no_op")
    assert f"<<< {conversation_id}/z.md" in curator.prompts[0]


@test()
async def maintenance_executor_instructs_curator_to_use_second_person() -> None:
    """Maintenance keeps the same user-facing voice as initial curation."""
    conversation_service, service, conversation_id, root = await load_fixture(
        maintenance_fixture()
    )
    _ = _write_topic(
        root, conversation_id, "a.md", title="Gaming", body="- You like Roboquest."
    )
    _ = _write_topic(
        root,
        conversation_id,
        "b.md",
        title="Gaming notes",
        body="- You own a Switch.",
    )
    run = await service.queue_maintenance_run(
        conversation_id,
        logger=test_logger(),
        now=datetime.now(UTC),
    )
    assert run is not None
    curator = _ConsolidationRunner("NO_CHANGES")
    executor = MaintenanceDreamingExecutor(
        conversation_service.database,
        workspace_root=root,
        agent=MaintenanceDreamingAgent(
            curator=curator,
            verifier=_ConsolidationRunner("APPROVED"),
        ),
    )

    result = await executor(run, logger=test_logger())

    assert_eq(result.status, "no_op")
    assert "Address the user as `you` and `your`" in curator.prompts[0]
    assert "Begin every Claim with `You` or `Your`" in curator.prompts[0]
    assert "Rewrite existing third-person user references" in curator.prompts[0]
    assert "claim:C1 | You like Roboquest." in curator.prompts[0]
    assert "claim:C2 | You own a Switch." in curator.prompts[0]
    assert "[source](citation:E1)" in curator.prompts[0]
    assert "Never use raw HTML for Evidence links" in curator.prompts[0]


@test()
async def maintenance_executor_rejects_no_changes_for_mixed_claim_voice() -> None:
    """A style violation remains work even when the curator returns no changes."""
    conversation_service, service, conversation_id, root = await load_fixture(
        maintenance_fixture()
    )
    _ = _write_topic(
        root,
        conversation_id,
        "a.md",
        title="Gaming",
        body="- The user likes Roboquest.",
    )
    _ = _write_topic(
        root,
        conversation_id,
        "b.md",
        title="Gaming notes",
        body="- You own a Switch.",
    )
    run = await service.queue_maintenance_run(
        conversation_id,
        logger=test_logger(),
        now=datetime.now(UTC),
    )
    assert run is not None
    executor = MaintenanceDreamingExecutor(
        conversation_service.database,
        workspace_root=root,
        agent=MaintenanceDreamingAgent(
            curator=_ConsolidationRunner("NO_CHANGES"),
            verifier=_ConsolidationRunner("APPROVED"),
        ),
    )

    result = await executor(run, logger=test_logger())

    assert_eq(result.status, "failed")
    assert_eq(result.error, "Memory Claims must address the user as you or your")


@test()
async def maintenance_executor_marks_no_changes_and_records_progress() -> None:
    """NO_CHANGES leaves files alone but marks the batch maintained."""
    conversation_service, service, conversation_id, root = await load_fixture(
        maintenance_fixture()
    )
    first = _write_topic(
        root, conversation_id, "a.md", title="Gaming", body="- You like Roboquest."
    )
    second = _write_topic(
        root,
        conversation_id,
        "b.md",
        title="Gaming notes",
        body="- You own a Switch.",
    )
    run = await service.queue_maintenance_run(
        conversation_id,
        logger=test_logger(),
        now=datetime.now(UTC),
    )
    assert run is not None
    executor = MaintenanceDreamingExecutor(
        conversation_service.database,
        workspace_root=root,
        agent=MaintenanceDreamingAgent(
            curator=_ConsolidationRunner("NO_CHANGES"),
            verifier=_ConsolidationRunner("APPROVED"),
        ),
    )

    result = await executor(run, logger=test_logger())

    assert_eq(result.status, "no_op")
    assert first.exists()
    assert second.exists()
    async with conversation_service.database.transaction() as tx:
        progress = await tx.fetch_all(select(DreamMaintenanceProgress).all())
        assert_eq(len(progress), 2)


@test()
async def maintenance_executor_supplies_dated_aliased_evidence() -> None:
    """The curator receives dated Evidence without opaque canonical identifiers."""
    conversation_service, service, conversation_id, root = await load_fixture(
        maintenance_fixture()
    )
    fixed_now = datetime(2026, 9, 15, 12, tzinfo=UTC)
    evidence = await _append(
        conversation_service,
        conversation_id=conversation_id,
        role="user",
        content="I am avoiding coffee this week.",
    )
    evidence_id = evidence.id
    await _retime(
        evidence_id,
        database=conversation_service.database,
        when=datetime(2026, 8, 1, 9, tzinfo=UTC),
    )
    _ = _write_topic(
        root,
        conversation_id,
        "coffee.md",
        title="Coffee",
        body=(
            f"- You are avoiding coffee this week. "
            f"[source](tether://message/{evidence_id})"
        ),
        uris=(f"tether://message/{evidence_id}",),
    )
    async with conversation_service.database.transaction() as transaction:
        _ = await transaction.execute(
            insert(
                DreamConversationCursor(
                    conversation_id=conversation_id,
                    last_assimilated_seq=1,
                )
            )
        )
    run = await service.queue_maintenance_run(
        conversation_id,
        logger=test_logger(),
        now=fixed_now,
    )
    assert run is not None
    curator = _ConsolidationRunner("NO_CHANGES")
    executor = MaintenanceDreamingExecutor(
        conversation_service.database,
        workspace_root=root,
        agent=MaintenanceDreamingAgent(
            clock=lambda: fixed_now,
            curator=curator,
            verifier=_ConsolidationRunner("APPROVED"),
        ),
    )

    result = await executor(run, logger=test_logger())

    assert_eq(result.status, "no_op")
    assert_eq(len(curator.prompts), 1)
    assert "current_time: 2026-09-15T12:00:00+00:00" in curator.prompts[0]
    assert "citation: citation:E1" in curator.prompts[0]
    assert f"tether://message/{evidence_id}" not in curator.prompts[0]
    assert "role: user" in curator.prompts[0]
    assert "created_at: 2026-08-01T09:00:00+00:00" in curator.prompts[0]
    assert "I am avoiding coffee this week." in curator.prompts[0]


@test()
async def maintenance_executor_supplies_newer_user_evidence() -> None:
    """Later assertions are available for supersession decisions."""
    conversation_service, service, conversation_id, root = await load_fixture(
        maintenance_fixture()
    )
    old_evidence = await _append(
        conversation_service,
        conversation_id=conversation_id,
        role="user",
        content="I am vegetarian.",
    )
    newer_evidence = await _append(
        conversation_service,
        conversation_id=conversation_id,
        role="user",
        content="I eat fish now.",
    )
    await _retime(
        old_evidence.id,
        database=conversation_service.database,
        when=datetime(2025, 1, 1, tzinfo=UTC),
    )
    await _retime(
        newer_evidence.id,
        database=conversation_service.database,
        when=datetime(2026, 8, 1, tzinfo=UTC),
    )
    _ = _write_topic(
        root,
        conversation_id,
        "diet.md",
        title="Diet",
        body=(f"- You are vegetarian. [source](tether://message/{old_evidence.id})"),
        uris=(f"tether://message/{old_evidence.id}",),
    )
    async with conversation_service.database.transaction() as transaction:
        _ = await transaction.execute(
            insert(
                DreamConversationCursor(
                    conversation_id=conversation_id,
                    last_assimilated_seq=2,
                )
            )
        )
    run = await service.queue_maintenance_run(
        conversation_id,
        logger=test_logger(),
        now=datetime(2026, 9, 15, tzinfo=UTC),
    )
    assert run is not None
    curator = _ConsolidationRunner("NO_CHANGES")
    executor = MaintenanceDreamingExecutor(
        conversation_service.database,
        workspace_root=root,
        agent=MaintenanceDreamingAgent(
            curator=curator,
            verifier=_ConsolidationRunner("APPROVED"),
        ),
    )

    result = await executor(run, logger=test_logger())

    assert_eq(result.status, "no_op")
    assert "citation: citation:E2" in curator.prompts[0]
    assert f"tether://message/{newer_evidence.id}" not in curator.prompts[0]
    assert "I eat fish now." in curator.prompts[0]


@test()
async def maintenance_user_evidence_supersedes_assistant_conclusion() -> None:
    """A user correction replaces a lower-authority agent conclusion."""
    conversation_service, service, conversation_id, root = await load_fixture(
        maintenance_fixture()
    )
    async with conversation_service.database.transaction() as transaction:
        turn = await transaction.execute(
            insert(
                ConversationTurn(
                    conversation_id=conversation_id,
                    origin="interactive",
                    status="succeeded",
                    turn_seq=1,
                )
            ).returning()
        )
    old_evidence = await conversation_service.append_message(
        MessageDraft(
            conversation_id=conversation_id,
            role="assistant",
            content="The user is vegetarian.",
            turn_id=turn.id,
        )
    )
    _ = await _append(
        conversation_service,
        conversation_id=conversation_id,
        role="user",
        content="I eat fish now.",
    )
    topic = _write_topic(
        root,
        conversation_id,
        "diet.md",
        title="Diet",
        body=(f"- You are vegetarian. [source](tether://message/{old_evidence.id})"),
        uris=(f"tether://message/{old_evidence.id}",),
    )
    async with conversation_service.database.transaction() as transaction:
        _ = await transaction.execute(
            insert(
                DreamConversationCursor(
                    conversation_id=conversation_id,
                    last_assimilated_seq=2,
                )
            )
        )
    run = await service.queue_maintenance_run(
        conversation_id,
        logger=test_logger(),
        now=datetime(2026, 9, 15, tzinfo=UTC),
    )
    assert run is not None
    curator = _ConsolidationRunner(
        "".join(
            (
                f"=== {conversation_id}/diet.md ===\n",
                "---\n",
                "title: Diet\n",
                "---\n\n",
                "- You eat fish now. [source](citation:E2)\n\n",
                "=== RETIREMENTS ===\n",
                "claim:C1 | superseded | citation:E2\n",
            )
        )
    )
    executor = MaintenanceDreamingExecutor(
        conversation_service.database,
        workspace_root=root,
        agent=MaintenanceDreamingAgent(
            curator=curator,
            verifier=_ConsolidationRunner("APPROVED"),
        ),
    )

    result = await executor(run, logger=test_logger())

    assert_eq(result.status, "success")
    updated = topic.read_text(encoding="utf-8")
    assert "You eat fish now." in updated
    assert "Vegetarian." not in updated


@test()
async def maintenance_executor_retires_an_expired_claim() -> None:
    """Verified temporal maintenance removes an explicitly expired Claim."""
    conversation_service, service, conversation_id, root = await load_fixture(
        maintenance_fixture()
    )
    fixed_now = datetime(2026, 9, 15, 12, tzinfo=UTC)
    evidence = await _append(
        conversation_service,
        conversation_id=conversation_id,
        role="user",
        content="I am avoiding coffee this week.",
    )
    evidence_id = evidence.id
    await _retime(
        evidence_id,
        database=conversation_service.database,
        when=datetime(2026, 8, 1, 9, tzinfo=UTC),
    )
    topic = _write_topic(
        root,
        conversation_id,
        "coffee.md",
        title="Coffee",
        body=(
            f"- You are avoiding coffee this week. "
            f"[source](tether://message/{evidence_id})"
        ),
        uris=(f"tether://message/{evidence_id}",),
    )
    async with conversation_service.database.transaction() as transaction:
        _ = await transaction.execute(
            insert(
                DreamConversationCursor(
                    conversation_id=conversation_id,
                    last_assimilated_seq=1,
                )
            )
        )
    run = await service.queue_maintenance_run(
        conversation_id,
        logger=test_logger(),
        now=fixed_now,
    )
    assert run is not None
    curator = _ConsolidationRunner(
        "=== RETIREMENTS ===\nclaim:C1 | expired | citation:E1\n"
    )
    verifier = _ConsolidationRunner("APPROVED")
    executor = MaintenanceDreamingExecutor(
        conversation_service.database,
        workspace_root=root,
        agent=MaintenanceDreamingAgent(
            clock=lambda: fixed_now,
            curator=curator,
            verifier=verifier,
        ),
    )

    result = await executor(run, logger=test_logger())

    assert_eq(result.status, "success")
    assert not topic.exists()
    assert "claim:C1 | You are avoiding coffee this week." in curator.prompts[0]
    assert "Never copy old Claim text into a retirement" in curator.prompts[0]
    assert_eq(len(verifier.prompts), 1)
    assert "citation:E1" in verifier.prompts[0]
    assert "claim:C1 | You are avoiding coffee this week." in verifier.prompts[0]
    assert "claim:C1 | expired | citation:E1" in verifier.prompts[0]
    assert (
        "Claim aliases intentionally identify exact current Claims"
        in (verifier.prompts[0])
    )
    assert "tether://" not in verifier.prompts[0]
    assert (
        "Citation aliases intentionally replace canonical Evidence URIs"
        in (verifier.prompts[0])
    )
    assert (
        "Accept `[source](citation:E1)` as a valid Evidence link"
        in (verifier.prompts[0])
    )
    async with conversation_service.database.transaction() as transaction:
        deletion = await transaction.fetch_one_or_none(
            select(DreamingMutation)
            .where(DreamingMutation.run_id.eq(run.id))
            .where(DreamingMutation.operation.eq("delete"))
        )
    assert deletion is not None
    assert deletion.payload is not None
    assert "You are avoiding coffee this week." in deletion.payload
    assert f"tether://message/{evidence_id}" in deletion.payload
    assert "claim:C1" not in deletion.payload
    assert "citation:E1" not in deletion.payload
    assert "reason: expired" in deletion.payload


@test()
async def maintenance_executor_rejects_yaml_retirement_ledger() -> None:
    """The former model-authored YAML shape cannot bypass the line protocol."""
    conversation_service, service, conversation_id, root = await load_fixture(
        maintenance_fixture()
    )
    evidence_uri = "tether://health-connect/record/coffee-1"
    topic = _write_topic(
        root,
        conversation_id,
        "coffee.md",
        title="Coffee",
        body=f"- You avoid coffee. [source]({evidence_uri})",
        uris=(evidence_uri,),
    )
    original = topic.read_text(encoding="utf-8")
    run = await service.queue_maintenance_run(
        conversation_id,
        logger=test_logger(),
        now=datetime.now(UTC),
    )
    assert run is not None
    verifier = _ConsolidationRunner("APPROVED")
    executor = MaintenanceDreamingExecutor(
        conversation_service.database,
        workspace_root=root,
        agent=MaintenanceDreamingAgent(
            curator=_ConsolidationRunner(
                "".join(
                    (
                        "=== RETIREMENTS ===\n",
                        '- claim: "You avoid coffee."\n',
                        "  reason: unsupported\n",
                        "  basis:\n",
                        "    - citation:E1\n",
                    )
                )
            ),
            verifier=verifier,
        ),
    )

    result = await executor(run, logger=test_logger())

    assert_eq(result.status, "failed")
    assert_eq(result.error, "retirement must use one Claim alias per line")
    assert_eq(verifier.prompts, [])
    assert_eq(topic.read_text(encoding="utf-8"), original)


@test(
    [
        Param(
            value=(
                "claim:C999",
                "maintenance output contains an unknown Claim alias: claim:C999",
            ),
            name="unknown",
        ),
        Param(
            value=(
                "claim:C0",
                "retirement contains a malformed Claim alias: claim:C0",
            ),
            name="malformed",
        ),
    ]
)
async def maintenance_executor_rejects_invalid_claim_alias(
    case: tuple[str, str],
) -> None:
    """A retirement accepts only exact aliases from the bounded Topic batch."""
    conversation_service, service, conversation_id, root = await load_fixture(
        maintenance_fixture()
    )
    evidence_uri = "tether://health-connect/record/coffee-1"
    topic = _write_topic(
        root,
        conversation_id,
        "coffee.md",
        title="Coffee",
        body=f"- You avoid coffee. [source]({evidence_uri})",
        uris=(evidence_uri,),
    )
    original = topic.read_text(encoding="utf-8")
    run = await service.queue_maintenance_run(
        conversation_id,
        logger=test_logger(),
        now=datetime.now(UTC),
    )
    assert run is not None
    executor = MaintenanceDreamingExecutor(
        conversation_service.database,
        workspace_root=root,
        agent=MaintenanceDreamingAgent(
            curator=_ConsolidationRunner(
                f"=== RETIREMENTS ===\n{case[0]} | unsupported | citation:E1\n"
            ),
            verifier=_ConsolidationRunner("APPROVED"),
        ),
    )

    result = await executor(run, logger=test_logger())

    assert_eq(result.status, "failed")
    assert_eq(result.error, case[1])
    assert_eq(topic.read_text(encoding="utf-8"), original)


@test()
async def maintenance_executor_rejects_duplicate_claim_alias() -> None:
    """One bounded Claim cannot be retired twice in the same transition."""
    conversation_service, service, conversation_id, root = await load_fixture(
        maintenance_fixture()
    )
    evidence_uri = "tether://health-connect/record/coffee-1"
    topic = _write_topic(
        root,
        conversation_id,
        "coffee.md",
        title="Coffee",
        body=f"- You avoid coffee. [source]({evidence_uri})",
        uris=(evidence_uri,),
    )
    original = topic.read_text(encoding="utf-8")
    run = await service.queue_maintenance_run(
        conversation_id,
        logger=test_logger(),
        now=datetime.now(UTC),
    )
    assert run is not None
    executor = MaintenanceDreamingExecutor(
        conversation_service.database,
        workspace_root=root,
        agent=MaintenanceDreamingAgent(
            curator=_ConsolidationRunner(
                "".join(
                    (
                        "=== RETIREMENTS ===\n",
                        "claim:C1 | expired | citation:E1\n",
                        "claim:C1 | unsupported | citation:E1\n",
                    )
                )
            ),
            verifier=_ConsolidationRunner("APPROVED"),
        ),
    )

    result = await executor(run, logger=test_logger())

    assert_eq(result.status, "failed")
    assert_eq(result.error, "retirement repeated Claim alias: claim:C1")
    assert_eq(topic.read_text(encoding="utf-8"), original)


@test()
async def rejected_retirement_leaves_current_memory_unchanged() -> None:
    """A semantic-verifier rejection prevents every workspace mutation."""
    conversation_service, service, conversation_id, root = await load_fixture(
        maintenance_fixture()
    )
    evidence_uri = "tether://message/018f0000-0000-7000-8000-0000000000a1"
    topic = _write_topic(
        root,
        conversation_id,
        "family.md",
        title="Family",
        body=f"- Sister is Ana. [source]({evidence_uri})",
        uris=(evidence_uri,),
    )
    original = topic.read_text(encoding="utf-8")
    run = await service.queue_maintenance_run(
        conversation_id,
        logger=test_logger(),
        now=datetime(2026, 9, 15, tzinfo=UTC),
    )
    assert run is not None
    curator = _ConsolidationRunner(
        "=== RETIREMENTS ===\nclaim:C1 | expired | citation:E1\n"
    )
    executor = MaintenanceDreamingExecutor(
        conversation_service.database,
        workspace_root=root,
        agent=MaintenanceDreamingAgent(
            curator=curator,
            verifier=_ConsolidationRunner(
                "Age alone does not show this Claim is no longer current."
            ),
        ),
    )

    result = await executor(run, logger=test_logger())

    assert_eq(result.status, "failed")
    assert_eq(topic.read_text(encoding="utf-8"), original)


@test()
async def semantic_verifier_rejects_unexplained_claim_loss() -> None:
    """The verifier blocks a rewrite that omits a supported idea."""
    conversation_service, service, conversation_id, root = await load_fixture(
        maintenance_fixture()
    )
    topic = _write_topic(
        root,
        conversation_id,
        "gaming.md",
        title="Gaming",
        body="- Likes Roboquest.",
    )
    original = topic.read_text(encoding="utf-8")
    run = await service.queue_maintenance_run(
        conversation_id,
        logger=test_logger(),
        now=datetime(2026, 9, 15, tzinfo=UTC),
    )
    assert run is not None
    proposed = "".join(
        (
            f"=== {conversation_id}/gaming.md ===\n",
            "---\n",
            "title: Gaming\n",
            "---\n\n",
            "No current Claims.\n",
        )
    )
    executor = MaintenanceDreamingExecutor(
        conversation_service.database,
        workspace_root=root,
        agent=MaintenanceDreamingAgent(
            curator=_ConsolidationRunner(proposed),
            verifier=_ConsolidationRunner("Supported Claim was omitted."),
        ),
    )

    result = await executor(run, logger=test_logger())

    assert_eq(result.status, "failed")
    assert result.error is not None
    assert "Supported Claim was omitted." in result.error
    assert_eq(topic.read_text(encoding="utf-8"), original)


@test()
async def maintenance_executor_accepts_final_assistant_support() -> None:
    """Maintenance preserves a Claim supported by an eligible agent conclusion."""
    conversation_service, service, conversation_id, root = await load_fixture(
        maintenance_fixture()
    )
    async with conversation_service.database.transaction() as transaction:
        turn = await transaction.execute(
            insert(
                ConversationTurn(
                    conversation_id=conversation_id,
                    origin="health",
                    status="succeeded",
                    turn_seq=1,
                )
            ).returning()
        )
    assistant = await conversation_service.append_message(
        MessageDraft(
            conversation_id=conversation_id,
            role="assistant",
            content="The user loves black coffee.",
            turn_id=turn.id,
        )
    )
    assistant_uri = f"tether://message/{assistant.id}"
    async with conversation_service.database.transaction() as transaction:
        _ = await transaction.execute(
            insert(
                DreamConversationCursor(
                    conversation_id=conversation_id,
                    last_assimilated_seq=assistant.seq,
                )
            )
        )
    _ = _write_topic(
        root,
        conversation_id,
        "coffee.md",
        title="Coffee",
        body=f"- Loves black coffee. [source]({assistant_uri})",
        uris=(assistant_uri,),
    )
    run = await service.queue_maintenance_run(
        conversation_id,
        logger=test_logger(),
        now=datetime(2026, 9, 15, tzinfo=UTC),
    )
    assert run is not None
    proposed = "".join(
        (
            f"=== {conversation_id}/coffee.md ===\n",
            "---\n",
            "title: Coffee\n",
            "---\n\n",
            "- You love black coffee. [source](citation:E1)\n",
        )
    )
    executor = MaintenanceDreamingExecutor(
        conversation_service.database,
        workspace_root=root,
        agent=MaintenanceDreamingAgent(
            curator=_ConsolidationRunner(proposed),
            verifier=_ConsolidationRunner("APPROVED"),
        ),
    )

    result = await executor(run, logger=test_logger())

    assert_eq(result.status, "success")


@test()
async def maintenance_executor_rejects_third_person_user_claims() -> None:
    """Maintenance cannot reintroduce mixed user-facing prose."""
    conversation_service, service, conversation_id, root = await load_fixture(
        maintenance_fixture()
    )
    _ = _write_topic(
        root, conversation_id, "a.md", title="Gaming", body="- Likes Roboquest."
    )
    _ = _write_topic(
        root, conversation_id, "b.md", title="Gaming notes", body="- Owns a Switch."
    )
    run = await service.queue_maintenance_run(
        conversation_id,
        logger=test_logger(),
        now=datetime.now(UTC),
    )
    assert run is not None
    proposed = (
        f"=== {conversation_id}/gaming.md ===\n"
        "---\n"
        "title: Gaming\n"
        "---\n\n"
        "- The user likes Roboquest.\n"
        "- Owns a Switch.\n"
    )
    executor = MaintenanceDreamingExecutor(
        conversation_service.database,
        workspace_root=root,
        agent=MaintenanceDreamingAgent(
            curator=_ConsolidationRunner(proposed),
            verifier=_ConsolidationRunner("APPROVED"),
        ),
    )

    result = await executor(run, logger=test_logger())

    assert_eq(result.status, "failed")
    assert_eq(result.error, "Memory Claims must address the user as you or your")


@test()
async def maintenance_executor_rejects_invented_citations() -> None:
    """Output may only cite evidence that the batch supports."""
    conversation_service, service, conversation_id, root = await load_fixture(
        maintenance_fixture()
    )
    _ = _write_topic(
        root,
        conversation_id,
        "a.md",
        title="Gaming",
        body="- Likes Roboquest.",
        uris=("tether://message/018f0000-0000-7000-8000-0000000000a1",),
    )
    _ = _write_topic(
        root, conversation_id, "b.md", title="Gaming notes", body="- Owns a Switch."
    )
    run = await service.queue_maintenance_run(
        conversation_id,
        logger=test_logger(),
        now=datetime.now(UTC),
    )
    assert run is not None
    fabricated = (
        f"=== {conversation_id}/gaming.md ===\n"
        "---\n"
        "title: Gaming\n"
        "---\n\n"
        "- You like Roboquest. [source](tether://message/"
        "018f0000-0000-7000-8000-0000000000ffff)\n"
    )
    executor = MaintenanceDreamingExecutor(
        conversation_service.database,
        workspace_root=root,
        agent=MaintenanceDreamingAgent(
            curator=_ConsolidationRunner(fabricated),
            verifier=_ConsolidationRunner("APPROVED"),
        ),
    )

    result = await executor(run, logger=test_logger())

    assert_eq(result.status, "failed")
    assert result.error is not None
    assert "citation" in result.error


@test()
async def maintenance_executor_rejects_unsafe_paths() -> None:
    """Consolidated output must stay inside the workspace root."""
    conversation_service, service, conversation_id, root = await load_fixture(
        maintenance_fixture()
    )
    _ = _write_topic(
        root, conversation_id, "a.md", title="Gaming", body="- Likes Roboquest."
    )
    _ = _write_topic(
        root, conversation_id, "b.md", title="Gaming notes", body="- Owns a Switch."
    )
    run = await service.queue_maintenance_run(
        conversation_id,
        logger=test_logger(),
        now=datetime.now(UTC),
    )
    assert run is not None
    escape = "=== ../escape.md ===\n---\ntitle: Escape\n---\n\n- Likes Roboquest.\n"
    executor = MaintenanceDreamingExecutor(
        conversation_service.database,
        workspace_root=root,
        agent=MaintenanceDreamingAgent(
            curator=_ConsolidationRunner(escape),
            verifier=_ConsolidationRunner("APPROVED"),
        ),
    )

    result = await executor(run, logger=test_logger())

    assert_eq(result.status, "failed")
    assert not (root.parent / "escape.md").exists()


@test()
async def dispatching_executor_routes_runs_by_kind() -> None:
    """The worker callback dispatches maintenance runs to their executor."""
    _, service, conversation_id, root = await load_fixture(maintenance_fixture())
    _ = _write_topic(
        root, conversation_id, "a.md", title="Gaming", body="- Likes Roboquest."
    )
    _ = _write_topic(
        root, conversation_id, "b.md", title="Gaming notes", body="- Owns a Switch."
    )
    run = await service.queue_maintenance_run(
        conversation_id,
        logger=test_logger(),
        now=datetime.now(UTC),
    )
    assert run is not None
    maintenance = _Callback(DreamRunExecutionResult(status="no_op"))
    fallback = _Callback(DreamRunExecutionResult(status="failed"))
    dispatcher = KindDispatchingDreamExecutor(
        {"maintenance": maintenance, "assimilation": fallback}
    )

    result = await dispatcher(run, logger=test_logger())

    assert_eq(result.status, "no_op")
    assert_eq(maintenance.calls, 1)
    assert_eq(fallback.calls, 0)


@test()
async def maintenance_covers_non_conversation_folders_like_health() -> None:
    """Vertical folders (e.g. health/) consolidate via a synthetic run id."""
    _, service, _, root = await load_fixture(maintenance_fixture())
    _ = _write_topic(
        root,
        "health",
        "a.md",
        title="Exercise",
        body="- Runs weekly.",
        uris=("tether://health-connect/exercise/e51e4ead@v1",),
    )
    _ = _write_topic(
        root,
        "health",
        "b.md",
        title="Exercise",
        body="- Lifts twice weekly.",
        uris=("tether://health-connect/exercise/014eeb5e@v241",),
    )

    queued = await service.queue_maintenance_runs(
        logger=test_logger(), now=datetime.now(UTC), force=True
    )

    assert_eq(len(queued), 1)
    assert_eq(queued[0].kind, "maintenance")

    merged = (
        "=== health/exercise.md ===\n"
        "---\n"
        "title: Exercise\n"
        "---\n\n"
        "- You run weekly. [source](citation:E1)\n"
        "- You lift twice weekly. [source](citation:E2)\n"
    )
    executor = MaintenanceDreamingExecutor(
        service.database,
        workspace_root=root,
        agent=MaintenanceDreamingAgent(
            curator=_ConsolidationRunner(merged),
            verifier=_ConsolidationRunner("APPROVED"),
        ),
    )
    result = await executor(queued[0], logger=test_logger())

    assert_eq(result.status, "success")
    assert not (root / "health" / "a.md").exists()
    assert not (root / "health" / "b.md").exists()
    assert (root / "health" / "exercise.md").exists()


@test()
async def maintenance_includes_root_level_topics() -> None:
    """Workspace-root topic files form their own consolidation group."""
    _, service, _, root = await load_fixture(maintenance_fixture())
    _ = _write_topic(root, "", "a.md", title="Interests", body="- Likes robotics.")
    _ = _write_topic(root, "", "b.md", title="Learning", body="- Reads papers.")

    queued = await service.queue_maintenance_runs(
        logger=test_logger(), now=datetime.now(UTC), force=True
    )

    assert_eq(len(queued), 1)


@test()
async def maintenance_executor_rejects_unsupported_health_citations() -> None:
    """Non-message evidence URIs are validated just like message ones."""
    _, service, _, root = await load_fixture(maintenance_fixture())
    _ = _write_topic(
        root,
        "health",
        "a.md",
        title="Exercise",
        body="- Runs weekly.",
        uris=("tether://health-connect/exercise/e51e4ead@v1",),
    )
    _ = _write_topic(root, "health", "b.md", title="Sleep", body="- Sleeps 8h.")
    queued = await service.queue_maintenance_runs(
        logger=test_logger(), now=datetime.now(UTC), force=True
    )
    assert_eq(len(queued), 1)
    fabricated = (
        "=== health/exercise.md ===\n"
        "---\n"
        "title: Exercise\n"
        "---\n\n"
        "- You run weekly. [source](tether://health-connect/exercise/fabricated@v9)\n"
    )
    executor = MaintenanceDreamingExecutor(
        service.database,
        workspace_root=root,
        agent=MaintenanceDreamingAgent(
            curator=_ConsolidationRunner(fabricated),
            verifier=_ConsolidationRunner("APPROVED"),
        ),
    )

    result = await executor(queued[0], logger=test_logger())

    assert_eq(result.status, "failed")
    assert result.error is not None
    assert "citation" in result.error
