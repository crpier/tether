"""Unit tests for the captions `TranscriptProvider` and its SRT/track parsing.

These never import the real Google client libraries and never touch a socket.
The provider runs against a fake captions resource (track `list` + SRT
`download`) and asserts the chosen track (human over auto-generated), the parsed
transcript text/segments, and the typed unavailability outcomes for missing
tracks, empty downloads, and HTTP error categories. The pure SRT parser and the
track selector are exercised directly against fixture payloads.
"""

from __future__ import annotations

from typing import Any

from snektest import assert_eq, assert_raises, test

from tether.youtube import (
    TranscriptExcludedError,
    TranscriptTransientError,
    TranscriptUnavailableError,
)
from tether.youtube_oauth import (
    CaptionsTranscriptProvider,
    parse_srt_transcript,
)

SRT = (
    "1\n"
    "00:00:01,000 --> 00:00:03,500\n"
    "Async IO multiplexes one thread\n"
    "\n"
    "2\n"
    "00:00:03,500 --> 00:00:06,000\n"
    "over many awaited waits\n"
)


class FakeHttpError(Exception):
    """A stand-in for `googleapiclient.errors.HttpError` carrying a status."""

    def __init__(self, status: int) -> None:
        super().__init__(f"http {status}")
        self.resp = type("Resp", (), {"status": status})()


class FakeRequest:
    """A built request that returns a canned value or raises on `execute`."""

    def __init__(self, result: Any = None, *, error: Exception | None = None) -> None:
        self._result = result
        self._error = error

    def execute(self) -> Any:
        if self._error is not None:
            raise self._error
        return self._result


class FakeCaptions:
    """A fake `captions` collection: track `list` then SRT `download`."""

    def __init__(
        self,
        *,
        list_items: list[dict[str, Any]] | None = None,
        list_error: Exception | None = None,
        download_result: Any = b"",
        download_error: Exception | None = None,
    ) -> None:
        self._list_items = list_items if list_items is not None else []
        self._list_error = list_error
        self._download_result = download_result
        self._download_error = download_error
        self.download_ids: list[str] = []

    def list(self, **_kwargs: Any) -> FakeRequest:
        return FakeRequest({"items": self._list_items}, error=self._list_error)

    def download(self, **kwargs: Any) -> FakeRequest:
        self.download_ids.append(kwargs.get("id", ""))
        return FakeRequest(self._download_result, error=self._download_error)


class FakeResource:
    """A discovery resource exposing only the captions collection used here."""

    def __init__(self, captions: FakeCaptions) -> None:
        self._captions = captions

    def captions(self) -> FakeCaptions:
        return self._captions


def track(track_id: str, *, kind: str = "standard") -> dict[str, Any]:
    """Build a caption-track list item with the given id and `trackKind`."""
    return {"id": track_id, "snippet": {"trackKind": kind, "language": "en"}}


def provider(captions: FakeCaptions) -> CaptionsTranscriptProvider:
    """Build the captions provider over a fake captions resource."""
    return CaptionsTranscriptProvider(FakeResource(captions))  # pyright: ignore[reportArgumentType]


# --- SRT parsing (pure) ------------------------------------------------------


@test()
def srt_parse_joins_text_and_keeps_segment_offsets() -> None:
    """Parsing an SRT yields joined text and per-cue start offsets."""
    text, segments = parse_srt_transcript(SRT)

    assert_eq(text, "Async IO multiplexes one thread over many awaited waits")
    assert_eq(len(segments), 2)
    assert_eq(segments[0].start_seconds, 1.0)
    assert_eq(segments[1].start_seconds, 3.5)


@test()
def srt_parse_skips_cues_without_text_or_timing() -> None:
    """Blocks with no timing line or no text are dropped, not crashed on."""
    payload = "1\nno timing here\n\n2\n00:00:02,000 --> 00:00:04,000\nkept\n"

    text, segments = parse_srt_transcript(payload)

    assert_eq(text, "kept")
    assert_eq(len(segments), 1)


# --- Track selection ---------------------------------------------------------


@test()
async def fetch_prefers_a_human_track_over_asr() -> None:
    """A non-ASR track is downloaded in preference to an auto-generated one."""
    captions = FakeCaptions(
        list_items=[track("asr-1", kind="asr"), track("human-1", kind="standard")],
        download_result=SRT.encode("utf-8"),
    )

    result = await provider(captions).fetch("v1")

    assert_eq(captions.download_ids, ["human-1"])
    assert_eq(result.source, "youtube_captions")
    assert_eq(result.segments[0].start_seconds, 1.0)


@test()
async def fetch_falls_back_to_first_track_when_all_asr() -> None:
    """With only auto-generated tracks, the first is used rather than failing."""
    captions = FakeCaptions(
        list_items=[track("asr-1", kind="asr"), track("asr-2", kind="asr")],
        download_result=SRT.encode("utf-8"),
    )

    _ = await provider(captions).fetch("v1")

    assert_eq(captions.download_ids, ["asr-1"])


# --- Unavailability outcomes -------------------------------------------------


@test()
async def fetch_with_no_tracks_is_unavailable() -> None:
    """No caption tracks maps to the permanent unavailable outcome."""
    with assert_raises(TranscriptUnavailableError):
        _ = await provider(FakeCaptions(list_items=[])).fetch("v1")


@test()
async def fetch_with_empty_download_is_unavailable() -> None:
    """A track that downloads to empty text is unavailable, not a crash."""
    captions = FakeCaptions(list_items=[track("t1")], download_result=b"")

    with assert_raises(TranscriptUnavailableError):
        _ = await provider(captions).fetch("v1")


@test()
async def fetch_maps_404_to_unavailable() -> None:
    """A 404 from the caption list is the unavailable outcome."""
    captions = FakeCaptions(list_error=FakeHttpError(404))

    with assert_raises(TranscriptUnavailableError):
        _ = await provider(captions).fetch("v1")


@test()
async def fetch_maps_403_to_excluded() -> None:
    """A 403 (members-only / caption access disabled) is permanently excluded."""
    captions = FakeCaptions(list_error=FakeHttpError(403))

    with assert_raises(TranscriptExcludedError):
        _ = await provider(captions).fetch("v1")


@test()
async def fetch_maps_500_to_transient() -> None:
    """A 5xx on download is a retryable transient failure."""
    captions = FakeCaptions(list_items=[track("t1")], download_error=FakeHttpError(500))

    with assert_raises(TranscriptTransientError):
        _ = await provider(captions).fetch("v1")


@test()
async def provider_decodes_bytes_and_str_downloads() -> None:
    """A download returning str is parsed the same as one returning bytes."""
    captions = FakeCaptions(list_items=[track("t1")], download_result=SRT)

    fetched = await provider(captions).fetch("v1")

    assert_eq(fetched.text.startswith("Async IO"), True)
