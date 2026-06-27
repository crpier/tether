"""HTTP routes for the Recall tethering path (the dedicated recall surface).

Each handler adapts one `RecallService` capability to HTTP: `endpoint` validates
the body with Pydantic, the handler calls `request.app.state.recall_service`, and
the result is serialised as a study-item or recall-prompt read model. Starting
Recall also reads the source video through `request.app.state.youtube_service` to
hand the service the already-fetched transcript.

Domain exceptions translate to status codes at this boundary —
`StudyItemNotFoundError` / `RecallPromptNotFoundError` /
`YouTubeVideoNotFoundError` -> 404, `StudyItemExistsError` -> 409, and a
not-yet-distillable source or a malformed answer (`TranscriptNotReadyError`,
`InvalidPromptError`, `InvalidAnswerError`) -> 422.

A recall prompt's read model **omits `correct_index`** — the surface must be able
to render the question and choices without being told which one is right.
"""

from __future__ import annotations

import functools
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from uuid import UUID

from pydantic import UUID7, BaseModel, NonNegativeInt
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from tether.logging import Logger, get_request_logger
from tether.openapi import EndpointRoute, endpoint
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


class StartRecallRequest(BaseModel):
    """Body for promoting an ingested educational video into a study item.

    >>> StartRecallRequest(video_id="v1").video_id
    'v1'
    """

    video_id: str


class AnswerPromptRequest(BaseModel):
    """Body for answering a recall prompt: the chosen option and how long it took.

    >>> AnswerPromptRequest(selected_index=0, response_ms=1200).selected_index
    0
    """

    selected_index: NonNegativeInt
    response_ms: NonNegativeInt


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


def _request_logger(request: Request) -> Logger:
    """Return the request logging context installed by middleware."""
    return get_request_logger(request)


def _path_prompt_id(request: Request) -> UUID:
    """Parse the `{prompt_id}` path segment, treating a bad id as absent."""
    raw_id = request.path_params["prompt_id"]
    try:
        return UUID(raw_id)
    except ValueError as error:
        raise RecallPromptNotFoundError(raw_id) from error


def _translate_domain_errors(
    handler: Callable[..., Awaitable[Response]],
) -> Callable[..., Awaitable[Response]]:
    """Map Recall domain failures onto HTTP status codes at the boundary."""

    @functools.wraps(handler)
    async def translated(*arguments: object) -> Response:
        try:
            return await handler(*arguments)
        except (
            StudyItemNotFoundError,
            RecallPromptNotFoundError,
            YouTubeVideoNotFoundError,
        ):
            return JSONResponse({"detail": "not found"}, status_code=404)
        except StudyItemExistsError as error:
            return JSONResponse({"detail": str(error)}, status_code=409)
        except (
            TranscriptNotReadyError,
            InvalidPromptError,
            InvalidAnswerError,
        ) as error:
            return JSONResponse({"detail": str(error)}, status_code=422)

    return translated


@endpoint(request_body=StartRecallRequest, response=StudyItemRead, status=201)
@_translate_domain_errors
async def start_recall(request: Request, body: StartRecallRequest) -> Response:
    """Promote an ingested educational video into a study item under Recall."""
    video = await request.app.state.youtube_service.get_video(body.video_id)
    if video.transcript is None:
        message = f"video {body.video_id} has no fetched transcript to distil"
        raise TranscriptNotReadyError(message)
    study_item = await request.app.state.recall_service.start_recall(
        source_video_id=video.video_id,
        source_title=video.title,
        transcript=video.transcript,
        now=datetime.now(UTC),
        logger=_request_logger(request),
    )
    return JSONResponse(
        StudyItemRead.from_study_item(study_item).model_dump(mode="json"),
        status_code=201,
    )


@endpoint(response=StudyItemRead, response_is_list=True)
async def list_study_items(request: Request) -> Response:
    """List every study item, newest-first."""
    items = await request.app.state.recall_service.list_study_items(
        logger=_request_logger(request),
    )
    return JSONResponse(
        [StudyItemRead.from_study_item(item).model_dump(mode="json") for item in items]
    )


@endpoint(response=DuePromptRead, response_is_list=True)
async def list_due_prompts(request: Request) -> Response:
    """List the recall prompts currently owed a review (the outstanding surface)."""
    due = await request.app.state.recall_service.list_due_prompts(
        datetime.now(UTC),
        logger=_request_logger(request),
    )
    return JSONResponse(
        [DuePromptRead.from_due(item).model_dump(mode="json") for item in due]
    )


@endpoint(request_body=AnswerPromptRequest, response=AnswerOutcomeRead)
@_translate_domain_errors
async def answer_prompt(request: Request, body: AnswerPromptRequest) -> Response:
    """Answer a recall prompt, grading and rescheduling it (tethering on completion)."""
    service = request.app.state.recall_service
    prompt = await service.fetch_prompt(_path_prompt_id(request))
    outcome = await service.answer_prompt(
        prompt,
        selected_index=body.selected_index,
        response_ms=body.response_ms,
        now=datetime.now(UTC),
        logger=_request_logger(request),
    )
    return JSONResponse(AnswerOutcomeRead.from_outcome(outcome).model_dump(mode="json"))


# `/api/recall/prompts` precedes `/api/recall/prompts/{prompt_id}/answer` so the
# literal collection path wins over the parameterised one.
recall_routes: list[Route] = [
    EndpointRoute("/api/recall/study-items", start_recall, methods=["POST"]),
    EndpointRoute("/api/recall/study-items", list_study_items, methods=["GET"]),
    EndpointRoute("/api/recall/prompts", list_due_prompts, methods=["GET"]),
    EndpointRoute(
        "/api/recall/prompts/{prompt_id}/answer", answer_prompt, methods=["POST"]
    ),
]
