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

from pydantic import BaseModel, NonNegativeInt
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from tether import recall_capabilities
from tether.capabilities import rest_response, translate_domain_errors
from tether.openapi import EndpointRoute, endpoint
from tether.recall import RecallPromptNotFoundError
from tether.recall_capabilities import (
    RECALL_ERRORS,
    AnswerOutcomeRead,
    DuePromptRead,
    StudyItemRead,
)


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


def _path_prompt_id(request: Request) -> UUID:
    """Parse the `{prompt_id}` path segment, treating a bad id as absent."""
    raw_id = request.path_params["prompt_id"]
    try:
        return UUID(raw_id)
    except ValueError as error:
        raise RecallPromptNotFoundError(raw_id) from error


_translate_domain_errors = translate_domain_errors(RECALL_ERRORS)


@endpoint(request_body=StartRecallRequest, response=StudyItemRead, status=201)
@_translate_domain_errors
async def start_recall(request: Request, body: StartRecallRequest) -> Response:
    """Promote an ingested educational video into a study item under Recall."""
    outcome = await recall_capabilities.start_recall(request, body.video_id)
    return rest_response(outcome, status_code=201)


@endpoint(response=StudyItemRead, response_is_list=True)
async def list_study_items(request: Request) -> Response:
    """List every study item, newest-first."""
    return rest_response(await recall_capabilities.list_study_items(request))


@endpoint(response=DuePromptRead, response_is_list=True)
async def list_due_prompts(request: Request) -> Response:
    """List the recall prompts currently owed a review (the outstanding surface)."""
    return rest_response(await recall_capabilities.list_due_prompts(request))


@endpoint(request_body=AnswerPromptRequest, response=AnswerOutcomeRead)
@_translate_domain_errors
async def answer_prompt(request: Request, body: AnswerPromptRequest) -> Response:
    """Answer a recall prompt, grading and rescheduling it (tethering on completion)."""
    outcome = await recall_capabilities.answer_prompt(
        request, _path_prompt_id(request), body.selected_index, body.response_ms
    )
    return rest_response(outcome)


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
