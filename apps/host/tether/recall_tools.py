"""The internal Recall tool surface, over the shared envelope.

These mount alongside the Memory, Bucket item, YouTube, and trigger tools under
`/internal/tools/*` — the loopback seam a pi process calls back into — reusing
the same auth gate, params-to-envelope validation, and rule-driven domain-error
translation (`tether.tools`). The capability executes live in
`tether.recall_capabilities`, shared with the REST routes; this module only
names each tool's params model and mounts it. They let the agent drive the
Recall path: turn an educational video into a study item, list what is owed a
review, and answer a prompt (which tethers the distilled-learnings Memory on
full completion).
"""

from __future__ import annotations

from pydantic import UUID7, BaseModel, NonNegativeInt, PositiveInt
from starlette.routing import Route

from tether import recall_capabilities
from tether.capabilities import bind_params
from tether.recall_capabilities import RECALL_ERRORS
from tether.tools import ToolSpec


class StartRecallParams(BaseModel):
    """Params for promoting an ingested educational video into a study item."""

    video_id: str


class AnswerRecallPromptParams(BaseModel):
    """Params for answering a recall prompt: the chosen option and elapsed time."""

    prompt_id: UUID7
    selected_index: NonNegativeInt
    response_ms: NonNegativeInt


class ListDueRecallPromptsParams(BaseModel):
    """Params for listing outstanding recall prompts, capped at `limit`.

    The due list is computed over the whole live schedule; `limit` bounds how
    many soonest-due prompts come back so a large backlog can't flood the model.
    """

    limit: PositiveInt = 50


RECALL_TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        "start_recall",
        StartRecallParams,
        bind_params(recall_capabilities.start_recall),
        RECALL_ERRORS,
    ),
    ToolSpec(
        "list_due_recall_prompts",
        ListDueRecallPromptsParams,
        bind_params(recall_capabilities.list_due_prompts),
        RECALL_ERRORS,
    ),
    ToolSpec(
        "answer_recall_prompt",
        AnswerRecallPromptParams,
        bind_params(recall_capabilities.answer_prompt),
        RECALL_ERRORS,
    ),
)
"""The Recall capabilities exposed as internal tools, in generated order."""


def internal_recall_tool_routes() -> list[Route]:
    """Mount the Recall capabilities as `/internal/tools/*` POST endpoints.

    Returned separately from the public Recall routes so they stay absent from
    the public OpenAPI document and generated client.
    """
    return [spec.route() for spec in RECALL_TOOL_SPECS]
