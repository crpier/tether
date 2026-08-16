"""Domain values and filename identity for KOReader progress synchronization."""

import hashlib
from dataclasses import dataclass
from pathlib import PurePosixPath

FINISHED_THRESHOLD = 0.98
"""Reading fraction at or beyond which a document is treated as finished.

KOReader reports `percentage` in `0.0` to `1.0`; the last pages of an epub often
never reach a literal `1.0`, so `0.98` is the pragmatic "done" line. The derived
Memory fires once per document ever, so the threshold decides when the single
capture happens, never how many.
"""


@dataclass(frozen=True, slots=True)
class ProgressUpdate:
    """A validated progress push before persistence.

    `device_id` is optional on the wire and normalized to `''` when absent.
    """

    document: str
    percentage: float
    progress: str
    device: str
    device_id: str


@dataclass(frozen=True, slots=True)
class LatestProgress:
    """The newest stored progress event for one document."""

    document: str
    percentage: float
    progress: str
    device: str
    device_id: str
    timestamp: int


def ebook_hash_for_filename(filename: str) -> str:
    """Return KOReader's filename-mode hash: `md5` of the path basename.

    >>> ebook_hash_for_filename("/mnt/onboard/Deep Work.epub")
    '0d2b8f...'  # doctest: +SKIP
    """
    basename = PurePosixPath(filename).name
    return hashlib.md5(basename.encode("utf-8")).hexdigest()  # noqa: S324
