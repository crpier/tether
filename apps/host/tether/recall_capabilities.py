"""The Recall domain's capability descriptor.

The pieces the REST routes (`tether.recall_routes`) and the internal tools
(`tether.recall_tools`) both need live here once: the Read models, the
domain→code map (`RECALL_ERRORS`), and one execute function per capability —
the service call plus its Read-model rendering. Starting Recall also reads the
source video through the YouTube service to hand the recall service its
already-fetched transcript, so `YouTubeVideoNotFoundError` sits in this
domain's table too.

A recall prompt's read model **omits `correct_index`** — the surface must be
able to render the question and choices without being told which one is right.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from pydantic import UUID7, BaseModel
from starlette.requests import Request

from tether.capabilities import CapabilityOutcome, ErrorRule
from tether.logging import get_request_logger
from tether.recall import (
    AnswerOutcome,
    DuePrompt,
    Fetched,
    InvalidAnswerError,
    InvalidPromptError,
    RecallPrompt,
    RecallPromptKind,
    RecallPromptNotFoundError,
    StudyItem,
    StudyItemExistsError,
    StudyItemNotFoundError,
    StudyItemState,
    TranscriptNotReadyError,
)
from tether.youtube import YouTubeVideoNotFoundError

RECALL_ERRORS: tuple[ErrorRule, ...] = (
    ErrorRule(
        (StudyItemNotFoundError, RecallPromptNotFoundError, YouTubeVideoNotFoundError),
        "not_found",
        404,
        detail="not found",
    ),
    ErrorRule((StudyItemExistsError,), "conflict", 409),
    ErrorRule(
        (TranscriptNotReadyError, InvalidPromptError, InvalidAnswerError),
        "invalid_input",
        422,
    ),
)
"""The Recall domain→code map both surfaces translate failures through."""


class StudyItemRead(BaseModel):
    """HTTP representation of a study item."""

    id: UUID7
    memory_id: UUID7
    source_video_id: str
    source_title: str
    state: StudyItemState
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None

    @classmethod
    def from_study_item(cls, item: StudyItem[Fetched]) -> StudyItemRead:
        """Render a stored study item as its HTTP representation."""
        return cls(
            id=item.id,
            memory_id=item.memory_id,
            source_video_id=item.source_video_id,
            source_title=item.source_title,
            state=item.state,
            created_at=item.created_at,
            updated_at=item.updated_at,
            completed_at=item.completed_at,
        )


class RecallPromptRead(BaseModel):
    """HTTP representation of a recall prompt, with the answer key withheld.

    `correct_index` is deliberately absent: the client renders and answers the
    prompt without being able to read the right choice off the wire.
    """

    id: UUID7
    study_item_id: UUID7
    kind: RecallPromptKind
    question: str
    choices: list[str]
    due_at: datetime

    @classmethod
    def from_prompt(cls, prompt: RecallPrompt[Fetched]) -> RecallPromptRead:
        """Render a stored prompt as its HTTP representation, without the key."""
        return cls(
            id=prompt.id,
            study_item_id=prompt.study_item_id,
            kind=prompt.kind,
            question=prompt.question,
            choices=prompt.choices,
            due_at=prompt.due_at,
        )


class DuePromptRead(BaseModel):
    """An outstanding prompt plus the study item it belongs to."""

    prompt: RecallPromptRead
    study_item: StudyItemRead

    @classmethod
    def from_due(cls, due: DuePrompt) -> DuePromptRead:
        """Render a due prompt as its HTTP representation."""
        return cls(
            prompt=RecallPromptRead.from_prompt(due.prompt),
            study_item=StudyItemRead.from_study_item(due.study_item),
        )


class AnswerOutcomeRead(BaseModel):
    """The result of answering: the grading, the rescheduled prompt, completion."""

    correct: bool
    quality: int
    completed: bool
    tethered: bool
    prompt: RecallPromptRead

    @classmethod
    def from_outcome(cls, outcome: AnswerOutcome) -> AnswerOutcomeRead:
        """Render an answer outcome as its HTTP representation."""
        return cls(
            correct=outcome.correct,
            quality=outcome.quality,
            completed=outcome.completed,
            tethered=outcome.tethered,
            prompt=RecallPromptRead.from_prompt(outcome.prompt),
        )


async def start_recall(request: Request, video_id: str) -> CapabilityOutcome:
    """Promote an ingested educational video into a study item under Recall."""
    video = await request.app.state.youtube_service.get_video(video_id)
    if video.transcript is None:
        message = f"video {video_id} has no fetched transcript to distil"
        raise TranscriptNotReadyError(message)
    study_item = await request.app.state.recall_service.start_recall(
        source_video_id=video.video_id,
        source_title=video.title,
        transcript=video.transcript,
        now=datetime.now(UTC),
        logger=get_request_logger(request),
    )
    return CapabilityOutcome(
        result=StudyItemRead.from_study_item(study_item).model_dump(mode="json")
    )


async def list_study_items(request: Request) -> CapabilityOutcome:
    """List every study item, newest-first."""
    items = await request.app.state.recall_service.list_study_items(
        logger=get_request_logger(request),
    )
    return CapabilityOutcome(
        result=[
            StudyItemRead.from_study_item(item).model_dump(mode="json")
            for item in items
        ]
    )


async def list_due_prompts(
    request: Request, limit: int | None = None
) -> CapabilityOutcome:
    """List the recall prompts currently owed a review (the outstanding surface)."""
    due = await request.app.state.recall_service.list_due_prompts(
        datetime.now(UTC),
        limit=limit,
        logger=get_request_logger(request),
    )
    return CapabilityOutcome(
        result=[DuePromptRead.from_due(item).model_dump(mode="json") for item in due]
    )


async def answer_prompt(
    request: Request, prompt_id: UUID, selected_index: int, response_ms: int
) -> CapabilityOutcome:
    """Answer a recall prompt, grading and rescheduling it (tethering on completion)."""
    service = request.app.state.recall_service
    prompt = await service.fetch_prompt(prompt_id)
    outcome = await service.answer_prompt(
        prompt,
        selected_index=selected_index,
        response_ms=response_ms,
        now=datetime.now(UTC),
        logger=get_request_logger(request),
    )
    return CapabilityOutcome(
        result=AnswerOutcomeRead.from_outcome(outcome).model_dump(mode="json")
    )
