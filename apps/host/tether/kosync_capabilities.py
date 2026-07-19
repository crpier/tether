"""The ebook-labeling capability descriptor, shared by REST and the tool surface.

Progress ingestion is device-facing (the `/kosync/*` protocol in
`tether.kosync_routes`); *labeling* is owner-facing — attaching a human title to
a document hash, and listing the hashes still unlabeled so the agent can ask.
Those three capabilities are exposed twice, a public REST route
(`tether.kosync_routes`) and a loopback `/internal/tools/*` endpoint
(`tether.kosync_tools`), both deriving the execute functions and the Read model
from here so the two surfaces never drift.
"""

from __future__ import annotations

from typing import cast

from pydantic import BaseModel
from starlette.requests import Request

from tether.capabilities import CapabilityOutcome, ErrorRule
from tether.kosync import EbookDocument, Fetched, KosyncService

KOSYNC_ERRORS: tuple[ErrorRule, ...] = ()
"""No domain failures translate here: labeling upserts, so a hash is never
absent and no conflict lifecycle exists. Malformed params are rejected as
`invalid_input` by the shared validation ahead of the execute."""


class EbookDocumentRead(BaseModel):
    """HTTP/tool representation of a document Tether has seen progress for.

    `finished` collapses the internal `finished_captured_at` stamp to the only
    fact a surface needs — whether the one finished Memory has been minted.

    >>> EbookDocumentRead(document_hash="abc", title=None, finished=False).finished
    False
    """

    document_hash: str
    title: str | None
    finished: bool

    @classmethod
    def from_document(cls, document: EbookDocument[Fetched]) -> EbookDocumentRead:
        """Render a stored document row as its surface representation."""
        return cls(
            document_hash=document.document_hash,
            title=document.title,
            finished=document.finished_captured_at is not None,
        )


def _service(request: Request) -> KosyncService:
    """The `KosyncService` the lifespan wires unconditionally onto app state.

    Labeling works even when the device-facing protocol is disabled — the
    service is always present; only the `/kosync/*` routes are gated.
    """
    return cast("KosyncService", request.app.state.kosync_service)


def _single(document: EbookDocument[Fetched]) -> CapabilityOutcome:
    """Render a single-document outcome."""
    return CapabilityOutcome(
        result=EbookDocumentRead.from_document(document).model_dump(mode="json")
    )


async def label_ebook(
    request: Request, document_hash: str, title: str
) -> CapabilityOutcome:
    """Attach a human title to a document hash (upserting the row)."""
    return _single(await _service(request).label_ebook(document_hash, title))


async def match_ebook_filename(request: Request, filename: str) -> CapabilityOutcome:
    """Label the document a filename hashes to, titled from the filename."""
    return _single(await _service(request).match_ebook_filename(filename))


async def list_unlabeled_ebooks(request: Request) -> CapabilityOutcome:
    """List every document still without a title, oldest first."""
    documents = await _service(request).list_unlabeled()
    return CapabilityOutcome(
        result=[
            EbookDocumentRead.from_document(document).model_dump(mode="json")
            for document in documents
        ]
    )
