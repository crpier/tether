"""HTTP routes for the Recall tethering path (the dedicated recall surface).

Each route adapts one Recall capability to HTTP: `endpoint` validates the body
with Pydantic, the handler binds the validated input (plus any path id) onto
the capability execute in `tether.recall_capabilities`, and the outcome is
served as a study-item or recall-prompt read model. Domain exceptions translate
to status codes through the domain's `ErrorRule` table (`RECALL_ERRORS`) —
absence (study item, prompt, or source video) -> 404, an already-studied
video -> 409, a not-yet-distillable source or malformed answer -> 422 — the
same table the internal tool surface maps onto envelope codes.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter
from pydantic import BaseModel, NonNegativeInt
from starlette.requests import Request
from starlette.responses import Response

from tether import recall_capabilities
from tether.capabilities import rest_response, translate_domain_errors
from tether.recall import PromptAnswer, RecallPromptNotFoundError
from tether.recall_capabilities import (
    RECALL_ERRORS,
    AnswerOutcomeRead,
    DuePromptRead,
    EssayGradeProposalRead,
    StudyItemRead,
)


class StartRecallRequest(BaseModel):
    """Body for promoting an ingested educational video into a study item.

    >>> StartRecallRequest(video_id="v1").video_id
    'v1'
    """

    video_id: str


class AnswerPromptRequest(BaseModel):
    """Body for answering a recall prompt, shaped by the prompt's kind.

    Multiple choice sends `selected_index`; short answer sends `answer_text`;
    essay sends `answer_text` plus the human-confirmed `confirmed_correct`
    (ADR 0004 — the model only ever proposes an essay grade). `response_ms`
    always rides along to refine the SM-2 quality.

    >>> AnswerPromptRequest(selected_index=0, response_ms=1200).selected_index
    0
    """

    selected_index: NonNegativeInt | None = None
    answer_text: str | None = None
    confirmed_correct: bool | None = None
    response_ms: NonNegativeInt

    def to_answer(self) -> PromptAnswer:
        """Project the request body onto the domain's answer input."""
        return PromptAnswer(
            response_ms=self.response_ms,
            selected_index=self.selected_index,
            answer_text=self.answer_text,
            confirmed_correct=self.confirmed_correct,
        )


class ProposeEssayGradeRequest(BaseModel):
    """Body for requesting a model-proposed essay grade to confirm.

    >>> ProposeEssayGradeRequest(answer_text="An essay.").answer_text
    'An essay.'
    """

    answer_text: str


def _path_prompt_id(raw_id: str) -> UUID:
    """Parse the `{prompt_id}` path segment, treating a bad id as absent."""
    try:
        return UUID(raw_id)
    except ValueError as error:
        raise RecallPromptNotFoundError(raw_id) from error


_translate_domain_errors = translate_domain_errors(RECALL_ERRORS)


router = APIRouter()


@router.post("/api/recall/study-items", response_model=StudyItemRead, status_code=201)
@_translate_domain_errors
async def start_recall(request: Request, body: StartRecallRequest) -> Response:
    """Promote an ingested educational video into a study item under Recall."""
    outcome = await recall_capabilities.start_recall(request, body.video_id)
    return rest_response(outcome, status_code=201)


@router.get("/api/recall/study-items", response_model=list[StudyItemRead])
async def list_study_items(request: Request) -> Response:
    """List every study item, newest-first."""
    return rest_response(await recall_capabilities.list_study_items(request))


@router.get("/api/recall/prompts", response_model=list[DuePromptRead])
async def list_due_prompts(request: Request) -> Response:
    """List the recall prompts currently owed a review (the outstanding surface)."""
    return rest_response(await recall_capabilities.list_due_prompts(request))


@router.post("/api/recall/prompts/{prompt_id}/answer", response_model=AnswerOutcomeRead)
@_translate_domain_errors
async def answer_prompt(
    request: Request, body: AnswerPromptRequest, prompt_id: str
) -> Response:
    """Answer a recall prompt, grading and rescheduling it (tethering on completion)."""
    outcome = await recall_capabilities.answer_prompt(
        request, _path_prompt_id(prompt_id), body.to_answer()
    )
    return rest_response(outcome)


@router.post(
    "/api/recall/prompts/{prompt_id}/grade-proposal",
    response_model=EssayGradeProposalRead,
)
@_translate_domain_errors
async def propose_essay_grade(
    request: Request, body: ProposeEssayGradeRequest, prompt_id: str
) -> Response:
    """Propose a model grade for an essay answer, for the human to confirm."""
    outcome = await recall_capabilities.propose_essay_grade(
        request, _path_prompt_id(prompt_id), body.answer_text
    )
    return rest_response(outcome)


# `/api/recall/prompts` precedes `/api/recall/prompts/{prompt_id}/answer` so the
# literal collection path wins over the parameterised one.
