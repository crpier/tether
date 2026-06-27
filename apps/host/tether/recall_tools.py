"""The internal Recall tool surface, over the shared envelope.

These mount alongside the Memory, Bucket item, YouTube, and trigger tools under
`/internal/tools/*` — the loopback seam a pi process calls back into — reusing
the same auth gate, params-to-envelope validation, and domain-error translation
(`tether.tools`). They let the agent drive the Recall path: turn an educational
video into a study item, list what is owed a review, and answer a prompt (which
tethers the distilled-learnings Memory on full completion).

Starting Recall resolves the source video through the YouTube service to hand the
recall service its already-fetched transcript, mirroring the public route.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import UUID7, BaseModel, NonNegativeInt
from starlette.requests import Request
from starlette.routing import Route

from tether.logging import Logger, get_request_logger
from tether.recall import TranscriptNotReadyError
from tether.recall_routes import (
    AnswerOutcomeRead,
    DuePromptRead,
    StudyItemRead,
)
from tether.tools import ToolEndpoint, ToolEnvelope, ToolRoute


class StartRecallParams(BaseModel):
    """Params for promoting an ingested educational video into a study item."""

    video_id: str


class AnswerRecallPromptParams(BaseModel):
    """Params for answering a recall prompt: the chosen option and elapsed time."""

    prompt_id: UUID7
    selected_index: NonNegativeInt
    response_ms: NonNegativeInt


class ListDueRecallPromptsParams(BaseModel):
    """Params for listing outstanding recall prompts.

    The due list is computed over the whole live schedule, so it takes no inputs
    beyond the session identity the gate already requires.
    """


def _tool_logger(request: Request) -> Logger:
    """Return the request logging context installed by middleware."""
    return get_request_logger(request)


async def _start_recall(request: Request, params: StartRecallParams) -> ToolEnvelope:
    """Promote an ingested educational video into a study item under Recall."""
    video = await request.app.state.youtube_service.get_video(params.video_id)
    if video.transcript is None:
        message = f"video {params.video_id} has no fetched transcript to distil"
        raise TranscriptNotReadyError(message)
    study_item = await request.app.state.recall_service.start_recall(
        source_video_id=video.video_id,
        source_title=video.title,
        transcript=video.transcript,
        now=datetime.now(UTC),
        logger=_tool_logger(request),
    )
    return ToolEnvelope(
        success=True,
        result=StudyItemRead.from_study_item(study_item).model_dump(mode="json"),
    )


async def _list_due_prompts(
    request: Request, _params: ListDueRecallPromptsParams
) -> ToolEnvelope:
    """List the recall prompts currently owed a review."""
    due = await request.app.state.recall_service.list_due_prompts(
        datetime.now(UTC), logger=_tool_logger(request)
    )
    return ToolEnvelope(
        success=True,
        result=[DuePromptRead.from_due(item).model_dump(mode="json") for item in due],
    )


async def _answer_prompt(
    request: Request, params: AnswerRecallPromptParams
) -> ToolEnvelope:
    """Answer a recall prompt, grading and rescheduling it."""
    service = request.app.state.recall_service
    prompt = await service.fetch_prompt(params.prompt_id)
    outcome = await service.answer_prompt(
        prompt,
        selected_index=params.selected_index,
        response_ms=params.response_ms,
        now=datetime.now(UTC),
        logger=_tool_logger(request),
    )
    return ToolEnvelope(
        success=True,
        result=AnswerOutcomeRead.from_outcome(outcome).model_dump(mode="json"),
    )


def internal_recall_tool_routes() -> list[Route]:
    """Mount the Recall capabilities as `/internal/tools/*` POST endpoints.

    Returned separately from the public Recall routes so they stay absent from
    the public OpenAPI document and generated client.
    """
    return [
        ToolRoute(
            "/internal/tools/start_recall",
            ToolEndpoint(StartRecallParams, _start_recall),
            methods=["POST"],
        ),
        ToolRoute(
            "/internal/tools/list_due_recall_prompts",
            ToolEndpoint(ListDueRecallPromptsParams, _list_due_prompts),
            methods=["POST"],
        ),
        ToolRoute(
            "/internal/tools/answer_recall_prompt",
            ToolEndpoint(AnswerRecallPromptParams, _answer_prompt),
            methods=["POST"],
        ),
    ]
