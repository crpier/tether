"""The internal ebook-labeling tool surface, over the shared envelope.

These mount alongside the Memory and panel tools under `/internal/tools/*` — the
loopback seam a pi process calls back into — reusing the same auth gate,
params-to-envelope validation, and domain-error translation. The capability
executes live in `tether.kosync_capabilities`, shared with the REST routes; this
module only names each tool's params and mounts it.

The agent labels an ebook hash the user names (`label_ebook`), maps a hash from a
filename it knows (`match_ebook_filename`), and lists the hashes still unlabeled
(`list_unlabeled_ebooks`) so it can ask the user which book an unknown hash is.
"""

from __future__ import annotations

from pydantic import BaseModel, Field
from starlette.requests import Request
from starlette.routing import Route

from tether import kosync_capabilities
from tether.capabilities import CapabilityOutcome, bind_params
from tether.kosync_capabilities import KOSYNC_ERRORS
from tether.tools import ToolSpec


class LabelEbookParams(BaseModel):
    """Params for attaching a human title to a document hash."""

    document_hash: str = Field(min_length=1)
    title: str = Field(min_length=1)


class MatchEbookFilenameParams(BaseModel):
    """Params for labeling the document a filename hashes to."""

    filename: str = Field(min_length=1)


class ListUnlabeledEbooksParams(BaseModel):
    """Params for listing documents still without a title (takes none)."""


async def _label_ebook(request: Request, params: LabelEbookParams) -> CapabilityOutcome:
    """Project the flat tool params onto the shared label capability."""
    return await kosync_capabilities.label_ebook(
        request, params.document_hash, params.title
    )


KOSYNC_TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec("label_ebook", LabelEbookParams, _label_ebook, KOSYNC_ERRORS),
    ToolSpec(
        "match_ebook_filename",
        MatchEbookFilenameParams,
        bind_params(kosync_capabilities.match_ebook_filename),
        KOSYNC_ERRORS,
    ),
    ToolSpec(
        "list_unlabeled_ebooks",
        ListUnlabeledEbooksParams,
        bind_params(kosync_capabilities.list_unlabeled_ebooks),
        KOSYNC_ERRORS,
    ),
)
"""The ebook-labeling capabilities exposed as internal tools, in order."""


def internal_kosync_tool_routes() -> list[Route]:
    """Mount the ebook-labeling capabilities as `/internal/tools/*` endpoints.

    Returned separately from the public REST routes so they stay absent from the
    public OpenAPI document and generated client.
    """
    return [spec.route() for spec in KOSYNC_TOOL_SPECS]
