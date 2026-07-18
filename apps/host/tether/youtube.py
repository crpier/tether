"""Concrete YouTube ingestion: background sync into a local cache, then read.

This is built **concretely** rather than as a general integration framework —
with so few external sources, an abstraction would cost more than it saves. The
external surface is a small **paginated** `YouTubeApi` protocol; the only
implementation here is `InMemoryYouTubeApi`, a seedable in-memory source that
doubles as the test fake. (A live OAuth-backed client is a separate slice — and
the YouTube Data API does not even expose the Watch Later playlist, so the real
boundary is necessarily a seam we own.)

The ingestion model is **sync-into-cache**, mirroring how `SearchReconciler`
converges a derived store:

* `browse` and `search` read only the local `IngestedVideo` corpus (SQLite).
  They never call upstream, so listing is instant and costs no quota.
* `YouTubeSyncService` owns all upstream traffic: an idempotent pass (run at
  startup and periodically) that pulls liked videos a page at a time — a few
  "hot" most-recent pages plus a slowly advancing backfill cursor through
  history, bounded by an optional cutoff date — enriches them with batched
  metadata, and **upserts** them into `IngestedVideo`, preserving any locally
  fetched transcript and any local ignore.
* API budget is a **persisted per-UTC-day counter** (`DailyQuota`): spend is
  remembered across restarts and the sync stops calling once the day's budget is
  exhausted, rolling over automatically at the next UTC day.

>>> api = InMemoryYouTubeApi(liked=[RawYouTubeVideo(
...     video_id="v1", title="Async Python", channel="PyConf", topic="python")])
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from functools import partial
from typing import TYPE_CHECKING, ClassVar, Literal, Protocol, cast, runtime_checkable
from uuid import uuid7

from opentelemetry.trace import Tracer
from pydantic import UUID7, BaseModel
from snekql.sqlite import (
    CurrentTimestamp,
    Database,
    Fetched,
    Index,
    Integer,
    Model,
    Pending,
    Text,
    Transaction,
    delete,
    insert,
    select,
    update,
)

from tether.db_retry import run_in_transaction
from tether.escalating_pause import (
    PauseKeys,
    PauseState,
    load_pause_state,
)
from tether.events import EventPublisher, InvalidateEvent, NullEventPublisher
from tether.logging import Logger

# `Clock`, `DailyQuota`, `SystemClock`, `YouTubeApiGate` and `YouTubeApiGateConfig`
# are not otherwise referenced in this module; the `as`-aliases are explicit
# re-exports (PEP 484) so callers can keep importing them from `tether.youtube`
# after the quota/gate/client trio moved to `tether.youtube_quota` (#203).
from tether.youtube_quota import (
    Clock,
    DailyQuota,
    LikedPage,
    QuotaMeta,
    RawYouTubeVideo,
    SystemClock,
    YouTubeApi,
    YouTubeApiClient,
    YouTubeApiGate,
    YouTubeApiGateConfig,
    YouTubeQuotaExceededError,
    YouTubeSyncState,
    state_get,
    state_set,
)

if TYPE_CHECKING:
    from tether.transcript_search import TranscriptSearchService

# `Clock`, `DailyQuota`, `SystemClock`, `YouTubeApiGate`, and `YouTubeApiGateConfig`
# live in `tether.youtube_quota` (#203) and are not otherwise referenced in this
# module; listing them here keeps them re-exported from `tether.youtube` for
# existing call sites without tripping the unused-import checks.
__all__ = [
    "Clock",
    "DailyQuota",
    "SystemClock",
    "YouTubeApiGate",
    "YouTubeApiGateConfig",
]

type YouTubeSource = Literal["liked"]
"""Which saved list a video was ingested from.

Only ``liked`` is ever written: the YouTube Data API does not expose the Watch
Later playlist, so liked videos are the sole ingestion source today."""

type IngestState = Literal["active", "ignored"]
"""Whether an ingested video is live in browse/search or purged from it."""

# The empty default for `TranscriptProvider.fetch`'s `paused_sources`, hoisted to a
# module constant so the value is not constructed in a parameter default expression.
_NO_PAUSED_SOURCES: frozenset[str] = frozenset()

# Cap on videos returned by semantic search when the caller passes no explicit
# limit, keeping assistant-facing results within the model's context.
_DEFAULT_SEMANTIC_LIMIT = 50

# Transcript sources skipped by default for a video with no manual captions. Only
# Supadata's paid `native` lookup is gated — it would spend a use to return nothing
# for a caption-less video — while the free library still runs (auto-captions can
# exist when the manual-caption flag is false).
_DEFAULT_CAPTION_GATED_SOURCES: frozenset[str] = frozenset({"supadata"})


def _empty_snippets() -> dict[str, str]:
    """Typed empty default for `SearchResult.snippets` (the lexical path)."""
    return {}


class YouTubeVideoNotFoundError(Exception):
    """Raised when an operation targets a video absent from ingestion."""


class TranscriptUnavailableError(Exception):
    """Raised when a provider has no transcript for a video (permanent).

    The *unavailable* outcome of the `TranscriptProvider` port: the video has no
    usable captions and never will, so the worker marks it terminal and stops
    retrying. Expected to be common with the captions-only first provider.
    """


class TranscriptExcludedError(Exception):
    """Raised when a video can never be transcribed by this provider (permanent).

    The *excluded* outcome: members-only, region-blocked, or otherwise barred
    content. The worker marks it terminal *and* purges it from active ingestion
    so it stops churning browse/search alongside the worker.
    """


class TranscriptTransientError(Exception):
    """Raised on a retryable transcript failure (rate limit, 5xx, network).

    The *transient* outcome: the worker increments the attempt count and schedules
    an exponentially backed-off retry rather than giving up.
    """


class TranscriptBlockedError(Exception):
    """Raised when a provider signals the host IP has been blocked / throttled.

    The *blocked* outcome of the `TranscriptProvider` port: distinct from a
    per-video transient failure because it is a property of the *provider*, not the
    video. The worker reacts by pausing the whole provider for an escalating
    cooldown (skipping the blockable source while it does) rather than retrying
    per-video, so an IP block does not get the host throttled into failing every
    fetch. `retry_after` carries any provider-supplied cooldown hint; the worker
    raises its escalating backoff to at least this when present.

    `source` names the blockable provider whose limit tripped (e.g.
    ``"youtube_transcript_api"`` or ``"supadata"``) so the worker pauses *that*
    provider's source independently. Leaf providers leave it ``None`` and the
    composite stamps the offending fallback's source as it propagates the error; a
    ``None`` source on a composite-raised block means "every blockable fallback was
    already paused and skipped" (a deferral, not a fresh block to escalate).
    """

    def __init__(
        self,
        message: str = "",
        *,
        retry_after: timedelta | None = None,
        source: str | None = None,
    ) -> None:
        super().__init__(message)
        self.retry_after: timedelta | None = retry_after
        self.source: str | None = source


class EmptyYouTubeSearchQueryError(Exception):
    """Raised when a keyword Search is asked to run on a blank query."""


class CacheMeta(BaseModel):
    """Whether a result was served from the local cache or fetched live.

    >>> CacheMeta(hit=False, source="live").source
    'live'
    """

    hit: bool
    source: Literal["live", "cache"]


class InMemoryYouTubeApi(YouTubeApi):
    """A seedable in-memory `YouTubeApi` + `TranscriptProvider` test double.

    Seeded with an ordered liked list (newest first), it serves fixed-size pages
    with synthetic cursors and counts its calls so tests can prove ingestion
    stays within budget. `fetch_video_metadata` returns the seeded objects with
    `liked_at` stripped, matching the live `videos.list` call, which has no
    playlist context and so never carries a liked timestamp — that field only
    arrives on liked-page items. It also satisfies `TranscriptProvider`
    via `fetch`, returning a seeded transcript or signalling unavailability — so
    one fake backs both the list/metadata surface and the transcript port.

    >>> import asyncio
    >>> api = InMemoryYouTubeApi(transcripts={"v1": "hello"})
    >>> asyncio.run(api.fetch("v1")).text
    'hello'
    """

    def __init__(
        self,
        *,
        liked: Sequence[RawYouTubeVideo] = (),
        transcripts: Mapping[str, str] | None = None,
        unavailable: Sequence[str] = (),
    ) -> None:
        self._liked: list[RawYouTubeVideo] = list(liked)
        # `unavailable` ids appear in the liked pages but yield no metadata,
        # standing in for members-only / private / deleted videos the real
        # `videos.list` call silently omits.
        unavailable_ids = set(unavailable)
        self._by_id: dict[str, RawYouTubeVideo] = {
            v.video_id: v.model_copy(update={"liked_at": None})
            for v in self._liked
            if v.video_id not in unavailable_ids
        }
        self._transcripts: dict[str, str] = dict(transcripts or {})
        self.list_calls: int = 0
        self.metadata_calls: int = 0
        self.transcript_calls: int = 0

    source: str = "in_memory"

    async def list_liked_page(
        self, *, page_token: str | None, page_size: int
    ) -> LikedPage:
        self.list_calls += 1
        start = int(page_token) if page_token is not None else 0
        size = max(1, page_size)
        page = self._liked[start : start + size]
        next_start = start + size
        next_token = str(next_start) if next_start < len(self._liked) else None
        return LikedPage(
            videos=list(page),
            next_page_token=next_token,
            total_results=len(self._liked),
        )

    async def fetch_video_metadata(
        self, video_ids: Sequence[str]
    ) -> Mapping[str, RawYouTubeVideo]:
        self.metadata_calls += 1
        return {vid: self._by_id[vid] for vid in video_ids if vid in self._by_id}

    async def fetch(
        self,
        video_id: str,
        *,
        paused_sources: frozenset[str] = _NO_PAUSED_SOURCES,
        skip_sources: frozenset[str] = _NO_PAUSED_SOURCES,
    ) -> FetchedTranscript:
        """Return a seeded transcript or raise `TranscriptUnavailableError`."""
        _ = (paused_sources, skip_sources)  # the fake has no blockable source
        self.transcript_calls += 1
        try:
            text = self._transcripts[video_id]
        except KeyError as error:
            raise TranscriptUnavailableError(video_id) from error
        return FetchedTranscript(text=text, segments=(), source="in_memory")


@dataclass(frozen=True, slots=True)
class TranscriptSegment:
    """One timed line of a transcript: its start offset and text.

    >>> TranscriptSegment(start_seconds=1.5, text="hello").text
    'hello'
    """

    start_seconds: float
    text: str


@dataclass(frozen=True, slots=True)
class FetchedTranscript:
    """A transcript a provider produced: the joined text, its timed segments, and
    a provider/source tag for provenance.

    >>> FetchedTranscript(text="hi", segments=(), source="captions").source
    'captions'
    """

    text: str
    segments: tuple[TranscriptSegment, ...]
    source: str


@runtime_checkable
class TranscriptProvider(Protocol):
    """The one new seam: fetch a video's transcript or signal why it cannot.

    Given a video id, `fetch` returns a `FetchedTranscript` or raises exactly one
    typed unavailability signal — `TranscriptUnavailableError` (no captions,
    permanent), `TranscriptExcludedError` (members-only / not transcribable,
    permanent), `TranscriptTransientError` (retryable, per-video), or
    `TranscriptBlockedError` (the host IP is blocked/throttled, a property of the
    provider). Distinct categories are distinct exceptions so the worker's state
    machine is complete: fallback providers (third-party libraries, Supadata) slot
    in behind this port without touching the worker.

    `source` is the provenance tag a provider stamps onto the transcripts it
    produces (e.g. ``"youtube_captions"``, ``"youtube_transcript_api"``,
    ``"supadata"``); the composite uses it to skip a specific paused fallback and
    the worker uses it to key each blockable source's independent pause.

    `paused_sources` is the worker's pause hook: the set of blockable provider
    sources currently in cooldown. A composite provider skips any fallback whose
    `source` is in the set and runs only the reachable ones (e.g. the captions API,
    or Supadata while the free library is paused). Leaf providers ignore it.

    `skip_sources` is the worker's per-video exclusion hook: sources to drop from
    this fetch entirely, as if unconfigured (used to skip Supadata for a video with
    no captions, saving its paid budget). Unlike a pause it never defers — the
    remaining sources decide the outcome — so a video with every reachable source
    unavailable still goes terminal. Leaf providers ignore it.
    """

    source: str
    """The provenance tag this provider stamps onto the transcripts it produces."""

    async def fetch(
        self,
        video_id: str,
        *,
        paused_sources: frozenset[str] = _NO_PAUSED_SOURCES,
        skip_sources: frozenset[str] = _NO_PAUSED_SOURCES,
    ) -> FetchedTranscript:
        """Return the video's transcript, or raise a typed unavailability signal."""
        ...


class NullTranscriptProvider(TranscriptProvider):
    """A provider that reports every video as unavailable.

    The default when no captions/OAuth-backed provider is configured: on-demand
    fetch surfaces a clean "unavailable" rather than crashing, and the background
    worker never runs (the wiring only starts it for a real provider).
    """

    source: str = "null"

    async def fetch(
        self,
        video_id: str,
        *,
        paused_sources: frozenset[str] = _NO_PAUSED_SOURCES,
        skip_sources: frozenset[str] = _NO_PAUSED_SOURCES,
    ) -> FetchedTranscript:
        """Always signal absence — there is no configured transcript source."""
        _ = (paused_sources, skip_sources)  # nothing blockable to skip
        raise TranscriptUnavailableError(video_id)


class FallbackTranscriptProvider(TranscriptProvider):
    """Compose providers behind one port: try a primary, fall back on *unavailable*.

    The `primary` (the higher-quality captions API) is tried first and is never
    blockable; on its *unavailable* outcome each `fallbacks` provider (the wider
    but IP-block-prone library, then the paid Supadata last resort) is tried in
    order until one yields a transcript. Any *excluded*, *transient*, or *blocked*
    outcome surfaces immediately — only *unavailable* falls through — so the worker
    still sees the single best outcome. A video is *unavailable* (terminal) only
    when the primary **and** every fallback report unavailable.

    `paused_sources` is the worker's pause hook: a fallback whose `source` is in
    the set is skipped (its provider is in cooldown). The remaining reachable
    fallbacks still run, so while the free library is paused Supadata is still
    tried, and vice versa. A real block from a reached fallback propagates
    immediately, stamped with that fallback's `source` so the worker pauses the
    right provider. If every *remaining* source is unavailable but at least one
    blockable fallback was skipped, the composite raises `TranscriptBlockedError`
    with `source=None` (a deferral) rather than *unavailable*, so the worker keeps
    the video pending for after the cooldown instead of marking it terminal on a
    source it never tried.

    `skip_sources` (e.g. Supadata gated off a caption-less video) deprioritizes
    rather than excludes: a gated source is dropped from the normal pass so a
    clean *unavailable* from the rest of the chain still goes terminal without
    spending it (the cost-avoidance intent). But when the *only* reason nothing
    else succeeded is that a real fallback was paused (not cleanly unavailable),
    a gated source that is itself reachable (not also paused) is tried as a
    genuine last resort instead of leaving the video deferring forever — it
    should never be true that every configured, reachable source is excluded
    at once (issue #182).
    """

    def __init__(
        self,
        primary: TranscriptProvider,
        *,
        fallbacks: Sequence[TranscriptProvider],
    ) -> None:
        self._primary: TranscriptProvider = primary
        self._fallbacks: tuple[TranscriptProvider, ...] = tuple(fallbacks)

    @property
    def source(self) -> str:
        """The composite's own tag is the primary's — sub-providers stamp their own."""
        return self._primary.source

    def leaf_providers(self) -> tuple[TranscriptProvider, ...]:
        """The composed providers (primary first) for wiring to walk and late-bind."""
        return (self._primary, *self._fallbacks)

    async def _attempt(
        self,
        provider: TranscriptProvider,
        video_id: str,
        *,
        paused_sources: frozenset[str],
        skip_sources: frozenset[str],
    ) -> FetchedTranscript | TranscriptUnavailableError:
        """Fetch from one provider: the transcript, or its *unavailable* to fold in.

        Any other outcome (*excluded*, *transient*, *blocked*) surfaces immediately;
        a source-less block is stamped with this provider's source so the worker
        pauses the right one.
        """
        try:
            return await provider.fetch(
                video_id, paused_sources=paused_sources, skip_sources=skip_sources
            )
        except TranscriptUnavailableError as error:
            return error
        except TranscriptBlockedError as error:
            if error.source is None:
                error.source = provider.source
            raise

    async def fetch(
        self,
        video_id: str,
        *,
        paused_sources: frozenset[str] = _NO_PAUSED_SOURCES,
        skip_sources: frozenset[str] = _NO_PAUSED_SOURCES,
    ) -> FetchedTranscript:
        """Try the primary, then each reachable fallback, surfacing the best outcome.

        A source in `skip_sources` is dropped from the normal pass — the primary
        included — deprioritizing it rather than excluding it outright: it is
        retried as a last resort (see below) before this ever defers or goes
        terminal. A source in `paused_sources` is skipped and defers (keeps the
        video pending) rather than going terminal.
        """
        last_unavailable: TranscriptUnavailableError | None = None
        skipped_paused = False
        # Gated (skip_sources) providers that are still reachable (not also
        # paused) — tried only as a last resort, after the normal chain below.
        gated_last_resort: list[TranscriptProvider] = []
        # The primary and fallbacks are one ordered chain for skip/pause handling;
        # only the primary is exempt from the pause skip (it is never blockable).
        for provider in (self._primary, *self._fallbacks):
            if provider.source in skip_sources:
                if provider.source not in paused_sources:
                    gated_last_resort.append(provider)
                continue
            if provider is not self._primary and provider.source in paused_sources:
                skipped_paused = True
                continue
            outcome = await self._attempt(
                provider,
                video_id,
                paused_sources=paused_sources,
                skip_sources=skip_sources,
            )
            if not isinstance(outcome, TranscriptUnavailableError):
                return outcome
            last_unavailable = outcome
        # Every non-gated reachable source was unavailable. If a paused fallback
        # was skipped, a gated-but-reachable source (e.g. Supadata, normally
        # deprioritized to save its paid use) is the only way left to make
        # progress, so try it now as a genuine last resort rather than leaving
        # the video deferring indefinitely behind the paused fallback's cooldown.
        if skipped_paused:
            for provider in gated_last_resort:
                outcome = await self._attempt(
                    provider,
                    video_id,
                    paused_sources=paused_sources,
                    skip_sources=skip_sources,
                )
                if not isinstance(outcome, TranscriptUnavailableError):
                    return outcome
                last_unavailable = outcome
            message = f"provider paused; skipped blockable fallbacks for {video_id}"
            raise TranscriptBlockedError(message)
        raise last_unavailable or TranscriptUnavailableError(video_id)


class IngestedVideo[S = Pending](Model[S, "IngestedVideo[Fetched]"]):
    id: IngestedVideo.GenCol[UUID7] = Text(primary_key=True, default_factory=uuid7)
    video_id: IngestedVideo.Col[str] = Text(nullable=False, unique=True)
    """The upstream YouTube id; the stable identity ingestion mirrors against."""
    source: IngestedVideo.Col[YouTubeSource] = Text()
    """Which saved list the video came from."""
    title: IngestedVideo.Col[str] = Text()
    channel: IngestedVideo.Col[str] = Text()
    topic: IngestedVideo.Col[str] = Text()
    """The topic browse filters on."""
    description: IngestedVideo.Col[str] = Text()
    """Saved-content text searched alongside the transcript."""
    transcript: IngestedVideo.Col[str | None] = Text(default=None, nullable=True)
    """The transcript, present only once explicitly fetched."""
    transcript_segments_json: IngestedVideo.Col[str | None] = Text(
        default=None, nullable=True
    )
    """The transcript's timed segments as JSON, alongside the joined `transcript`;
    null for a text-only fetch or a video never transcribed."""
    transcript_source: IngestedVideo.Col[str | None] = Text(default=None, nullable=True)
    """Which provider produced the stored transcript (e.g. `supadata`,
    `youtube_transcript_api`); null until one is fetched."""
    ignored_at: IngestedVideo.Col[datetime | None] = Text(default=None, nullable=True)
    """When the video was purged from ingestion; null while it is active."""
    # --- Enriched metadata (nullable; filled by sync detail fetch / import). ---
    channel_id: IngestedVideo.Col[str | None] = Text(default=None, nullable=True)
    liked_at: IngestedVideo.Col[datetime | None] = Text(default=None, nullable=True)
    """When the user liked the video; the ordering key for browse."""
    video_published_at: IngestedVideo.Col[datetime | None] = Text(
        default=None, nullable=True
    )
    duration_seconds: IngestedVideo.Col[int | None] = Integer(
        default=None, nullable=True
    )
    category_id: IngestedVideo.Col[str | None] = Text(default=None, nullable=True)
    default_language: IngestedVideo.Col[str | None] = Text(default=None, nullable=True)
    default_audio_language: IngestedVideo.Col[str | None] = Text(
        default=None, nullable=True
    )
    caption_available: IngestedVideo.Col[int | None] = Integer(
        default=None, nullable=True
    )
    privacy_status: IngestedVideo.Col[str | None] = Text(default=None, nullable=True)
    licensed_content: IngestedVideo.Col[int | None] = Integer(
        default=None, nullable=True
    )
    made_for_kids: IngestedVideo.Col[int | None] = Integer(default=None, nullable=True)
    live_broadcast_content: IngestedVideo.Col[str | None] = Text(
        default=None, nullable=True
    )
    definition: IngestedVideo.Col[str | None] = Text(default=None, nullable=True)
    dimension: IngestedVideo.Col[str | None] = Text(default=None, nullable=True)
    statistics_view_count: IngestedVideo.Col[int | None] = Integer(
        default=None, nullable=True
    )
    statistics_like_count: IngestedVideo.Col[int | None] = Integer(
        default=None, nullable=True
    )
    statistics_comment_count: IngestedVideo.Col[int | None] = Integer(
        default=None, nullable=True
    )
    statistics_fetched_at: IngestedVideo.Col[datetime | None] = Text(
        default=None, nullable=True
    )
    topic_categories_json: IngestedVideo.Col[str | None] = Text(
        default=None, nullable=True
    )
    tags_json: IngestedVideo.Col[str | None] = Text(default=None, nullable=True)
    thumbnails_json: IngestedVideo.Col[str | None] = Text(default=None, nullable=True)
    created_at: IngestedVideo.GenCol[datetime] = Text(default=CurrentTimestamp)
    updated_at: IngestedVideo.GenCol[datetime] = Text(default=CurrentTimestamp)

    __indexes__: ClassVar = [Index(topic)]


type TranscriptStatus = Literal["done", "retry", "terminal"]
"""The persisted per-video transcript state.

A video with no row is *pending* (eligible to fetch); ``done`` once its transcript
is stored, ``retry`` while transient failures back off, ``terminal`` once a
permanent outcome (unavailable / excluded) means it must never be tried again.
"""


class YouTubeTranscriptState[S = Pending](Model[S, "YouTubeTranscriptState[Fetched]"]):
    """Durable per-video transcript bookkeeping for the background worker.

    Keyed by the upstream `video_id`. Absence means *pending*; a row carries the
    state-machine status, the attempt count, the next-attempt time (for backed-off
    retries that survive restarts), and the last error for observability.
    """

    video_id: YouTubeTranscriptState.Col[str] = Text(primary_key=True)
    status: YouTubeTranscriptState.Col[TranscriptStatus] = Text(nullable=False)
    attempts: YouTubeTranscriptState.Col[int] = Integer(default=0)
    next_attempt_at: YouTubeTranscriptState.Col[str | None] = Text(
        default=None, nullable=True
    )
    """When the next retry becomes due, as an ISO-8601 UTC string; null unless
    `status` is ``retry``."""
    last_error: YouTubeTranscriptState.Col[str | None] = Text(
        default=None, nullable=True
    )
    updated_at: YouTubeTranscriptState.GenCol[datetime] = Text(default=CurrentTimestamp)


_BACKFILL_CURSOR_KEY = "likes_backfill_next_page_token"
_LIKES_LAST_RUN_KEY = "likes_last_run_at"
# When the backfill cursor last reached the end of history, as an ISO-8601 UTC
# string. Its presence is what stops the perpetual re-walk: once set, the sync
# leaves history alone until this is older than the configured re-walk interval
# (or drift forces an immediate restart, which clears it).
_BACKFILL_COMPLETED_AT_KEY = "likes_backfill_completed_at"
# The set of liked video ids whose `videos.list` detail lookup returned nothing
# (deleted, private, or members-only), persisted as a JSON array. Tracked so the
# drift alarm can fold this known, un-ingestable gap into its formula and fire only
# on genuine data loss; an id is dropped once the video later becomes fetchable.
_KNOWN_SKIPPED_IDS_KEY = "likes_known_skipped_ids"
# Provider-level transcript pause, persisted per blockable source in the sync-state
# store so it survives restarts: the instant that source may be tried again, and
# the consecutive-block streak its cooldown escalates with. Each blockable source
# (the free `youtube_transcript_api` library, the paid Supadata) pauses
# independently, so its key carries the source suffix.
_TRANSCRIPT_PAUSED_UNTIL_PREFIX = "transcript_provider_paused_until:"
_TRANSCRIPT_BLOCK_STREAK_PREFIX = "transcript_provider_block_streak:"


def provider_pause_keys(source: str) -> PauseKeys:
    """The sync-state keys one blockable transcript source's pause persists under."""
    return PauseKeys(
        paused_until=f"{_TRANSCRIPT_PAUSED_UNTIL_PREFIX}{source}",
        streak=f"{_TRANSCRIPT_BLOCK_STREAK_PREFIX}{source}",
    )


async def _read_last_run_at(database: Database) -> datetime | None:
    """The clock-sourced instant of the most recently completed likes sync pass.

    None when no pass has completed yet, or the persisted value is malformed.
    Shared by `YouTubeSyncService.last_run_at` and `YouTubeService.sync_status`
    so both read the last-run time through one decoder rather than the raw
    sync-state key.
    """
    raw_last_run = await state_get(database, _LIKES_LAST_RUN_KEY)
    if not raw_last_run:
        return None
    try:
        last_run = datetime.fromisoformat(raw_last_run)
    except ValueError:
        return None
    return (
        last_run.replace(tzinfo=UTC)
        if last_run.tzinfo is None
        else last_run.astimezone(UTC)
    )


def derive_ingest_state(video: IngestedVideo[Fetched]) -> IngestState:
    """Derive whether a video is live in ingestion or purged from it."""
    return "ignored" if video.ignored_at is not None else "active"


@dataclass(frozen=True, slots=True)
class BrowseResult:
    """A topic-filtered browse: the local videos plus the day's quota/cache."""

    videos: list[IngestedVideo[Fetched]]
    cache: CacheMeta
    quota: QuotaMeta


@dataclass(frozen=True, slots=True)
class SearchResult:
    """A search across saved content + transcripts, with the day's quota/cache.

    `snippets` maps a matched video's `video_id` to the transcript excerpt that
    explains the match; it is populated by the semantic path and empty for the
    lexical fallback."""

    videos: list[IngestedVideo[Fetched]]
    cache: CacheMeta
    quota: QuotaMeta
    snippets: dict[str, str] = field(default_factory=_empty_snippets)


@dataclass(frozen=True, slots=True)
class TranscriptResult:
    """A fetched transcript, the updated video row, and quota/cache."""

    video: IngestedVideo[Fetched]
    transcript: str
    cache: CacheMeta
    quota: QuotaMeta


@dataclass(frozen=True, slots=True)
class SyncReport:
    """The outcome of one ingestion sync pass."""

    pulled: int
    upserted: int
    pages: int
    backfill_exhausted: bool
    backfill_deferred: bool = False
    """True when a completed backfill was left settled this pass — not restarted by
    drift and not yet older than the re-walk interval — so only the hot pages ran."""
    drift_detected: bool = False
    """True when this pass detected likes drift and restarted the history walk."""


@dataclass(slots=True)
class _SyncTally:
    """Running counts a sync pass accumulates across its hot and backfill walks.

    Mutable and passed into the walk helpers so a mid-walk quota stop keeps the
    partial counts it managed before halting."""

    pulled: int = 0
    upserted: int = 0
    pages: int = 0


@dataclass(frozen=True, slots=True)
class TranscriptProviderPause:
    """A blockable transcript source currently inside its IP-block cooldown."""

    source: str
    paused_until: datetime


@dataclass(frozen=True, slots=True)
class SupadataUsage:
    """A snapshot of Supadata's own *monthly* billed-use budget.

    Distinct from `QuotaMeta` (the YouTube Data API's per-*day* budget): Supadata
    is a separate paid HTTP API with its own cap, reset cadence, and spend, so
    mixing the two into one number would be meaningless. `month` is the UTC
    calendar month (`YYYY-MM`) the count applies to; a new month starts fresh.
    """

    used: int
    limit: int
    remaining: int
    month: str


@runtime_checkable
class SupadataUsageReader(Protocol):
    """Reads Supadata's monthly usage without charging it."""

    async def snapshot(self, *, now: datetime) -> SupadataUsage | None:
        """The current month's Supadata usage, or None when Supadata isn't wired."""
        ...


@dataclass(frozen=True, slots=True)
class YouTubeSyncStatus:
    """A snapshot of the background ingestion's progress and health.

    The four counts partition the active corpus: every active video is either
    already transcribed (``transcripts_done``), still owed one
    (``transcripts_pending``), or permanently without one
    (``transcripts_unavailable``); their sum is ``videos_total``. `last_synced_at`
    is when the likes sync last ran, `quota` the day's YouTube Data API budget
    (only actual Data API usage — captions.list/download and the liked-list/
    metadata calls — counts against it), `supadata` Supadata's own separate
    monthly budget (`None` when Supadata isn't configured), and the two pause
    fields explain a stall (a live Data API block, or a per-source transcript
    provider block).
    """

    videos_total: int
    transcripts_done: int
    transcripts_pending: int
    transcripts_unavailable: int
    last_synced_at: datetime | None
    quota: QuotaMeta
    api_paused_until: datetime | None
    transcript_providers_paused: list[TranscriptProviderPause]
    supadata: SupadataUsage | None = None


@dataclass(frozen=True, slots=True)
class YouTubeSyncConfig:
    """Tunables for one ingestion sync pass.

    `hot_pages` are pulled from the head of the liked list every run (newest
    likes surface fast); `backfill_pages` advance a persisted cursor through
    history a little each run; `cutoff_date` bounds (and terminates) the
    backfill.
    """

    hot_pages: int = 2
    backfill_pages: int = 1
    page_size: int = 50
    cutoff_date: date | None = None
    min_interval: timedelta | None = None
    """When set, `maybe_sync` skips a pass if the persisted last-run is newer than
    this — so app restarts within the window don't re-spend quota. `None` (the
    default) disables the gate, so every `maybe_sync` runs."""
    rewalk_interval: timedelta | None = timedelta(days=30)
    """How long a completed backfill stays settled before the walk restarts. Once
    the cursor reaches the end of history the sync stops re-walking (only the hot
    pages keep refreshing); it re-walks from the tail once the completion is older
    than this. `None` walks history exactly once and never again (drift can still
    force a restart)."""
    drift_alarm_margin: int = 5
    """How far the upstream liked-playlist total may exceed the local corpus (after
    the known-skipped count is added back) before a completed backfill is treated as
    drifted and restarted. Deleted, private, and members-only videos are tracked by
    id and folded into the comparison precisely, so this margin only absorbs
    transient races (a like landing mid-pass); a larger shortfall trips the alarm."""


type TranscriptOutcome = Literal[
    "done", "unavailable", "excluded", "transient", "blocked"
]
"""How one transcript fetch attempt resolved (the worker tallies these)."""


@dataclass(frozen=True, slots=True)
class TranscriptAttempt:
    """The result of fetching + persisting one video's transcript.

    `video` and `text` are present only on a ``done`` outcome; the four failure
    outcomes carry just the classification, which both the worker (continue) and
    the on-demand path (translate to an error) act on. A ``blocked`` outcome leaves
    the per-video state untouched (it is a provider-level signal) and carries any
    `retry_after` hint plus the `source` whose limit tripped, so the worker pauses
    that provider's source independently. A ``blocked`` with `source` ``None`` is a
    deferral (the composite skipped an already-paused fallback) — nothing to
    escalate.
    """

    outcome: TranscriptOutcome
    video: IngestedVideo[Fetched] | None = None
    text: str | None = None
    retry_after: timedelta | None = None
    source: str | None = None


@dataclass(frozen=True, slots=True)
class TranscriptSyncConfig:
    """Tunables for the background transcript worker (gentle on quota).

    `recent_window` caps how many of the newest still-untranscribed videos one
    pass considers; `backoff_base`/`backoff_cap` bound the exponential per-video
    retry delay a transient failure schedules. `block_pause_base`/`block_pause_cap`
    bound the **global** provider pause an IP block trips: the cooldown grows
    exponentially in the consecutive-block streak, clamped to the cap, and is
    raised to any provider-supplied retry-after hint.
    """

    recent_window: int = 50
    backoff_base: timedelta = timedelta(minutes=10)
    backoff_cap: timedelta = timedelta(hours=6)
    block_pause_base: timedelta = timedelta(minutes=30)
    block_pause_cap: timedelta = timedelta(hours=6)
    transient_storm_threshold: int = 8
    """How many *consecutive* transient failures halt the pass. A systematic fault
    (a misshaped request every provider 400s on, a provider outage) otherwise
    marches through the whole `recent_window`, spending a call — and, for a paid
    source like Supadata, a billed credit — on every candidate before anything
    pauses it. Stopping after a short run bounds that waste; the next scheduled pass
    retries, so a transient fault still recovers on its own. A success, or any
    non-transient outcome, resets the run (see the worker loop)."""
    caption_gated_sources: frozenset[str] = _DEFAULT_CAPTION_GATED_SOURCES
    """Transcript sources to skip for a video the Data API marks as having no manual
    captions (`caption_available` false). Supadata's `native` mode only fetches an
    existing caption track, so it would spend a paid use to return nothing; skipping
    it there preserves the cap. The library still runs (auto-captions can exist even
    when the manual-caption flag is false), so the skip is not a hard terminal."""


@dataclass(frozen=True, slots=True)
class TranscriptSyncReport:
    """The outcome of one transcript worker pass."""

    fetched: int
    unavailable: int
    excluded: int
    retried: int
    quota_exhausted: bool
    blocked: int = 0
    paused: bool = False
    transient_storm: bool = False
    """Set when the pass stopped early on a run of consecutive transient failures
    (the storm breaker) rather than draining the candidate window."""


def _debug(logger: Logger, event: str, **context: object) -> None:
    logger.debug(event, **context)


def _info(logger: Logger, event: str, **context: object) -> None:
    logger.info(event, **context)


def _json_or_none(values: Sequence[str] | Mapping[str, str]) -> str | None:
    """Encode a non-empty sequence/mapping as JSON, else None."""
    return json.dumps(values) if values else None


def _decode_skipped_ids(raw: str | None) -> set[str]:
    """Decode the persisted known-skipped-ids JSON array into a set of ids.

    Tolerates absence and malformed values (returning an empty set) so a corrupt
    state row degrades to "nothing skipped" rather than crashing the sync."""
    if not raw:
        return set()
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        return set()
    if not isinstance(decoded, list):
        return set()
    return {str(item) for item in cast("list[object]", decoded)}


def _bool_to_int(*, value: bool | None) -> int | None:
    """Map an optional bool onto the 0/1 integer the column stores."""
    return None if value is None else int(value)


def _new_ingested_video(raw: RawYouTubeVideo) -> IngestedVideo[Pending]:
    """Build a fresh ingested-video row from a raw upstream video (source liked)."""
    return IngestedVideo(
        video_id=raw.video_id,
        source="liked",
        title=raw.title,
        channel=raw.channel,
        topic=raw.topic,
        description=raw.description,
        channel_id=raw.channel_id,
        liked_at=raw.liked_at,
        video_published_at=raw.video_published_at,
        duration_seconds=raw.duration_seconds,
        category_id=raw.category_id,
        default_language=raw.default_language,
        default_audio_language=raw.default_audio_language,
        caption_available=_bool_to_int(value=raw.caption_available),
        privacy_status=raw.privacy_status,
        licensed_content=_bool_to_int(value=raw.licensed_content),
        made_for_kids=_bool_to_int(value=raw.made_for_kids),
        live_broadcast_content=raw.live_broadcast_content,
        definition=raw.definition,
        dimension=raw.dimension,
        statistics_view_count=raw.statistics_view_count,
        statistics_like_count=raw.statistics_like_count,
        statistics_comment_count=raw.statistics_comment_count,
        statistics_fetched_at=raw.statistics_fetched_at,
        topic_categories_json=_json_or_none(raw.topic_categories),
        tags_json=_json_or_none(raw.tags),
        thumbnails_json=_json_or_none(raw.thumbnails),
    )


async def upsert_ingested_video(tx: Transaction, raw: RawYouTubeVideo) -> None:
    """Insert or refresh an ingested video from a raw liked video by `video_id`.

    A new id is inserted fresh; an existing one has its metadata overwritten in
    place. Either way the local-only columns — `transcript` and `ignored_at` —
    are left untouched, so a sync (or the backup import) never clobbers a fetched
    transcript or resurrects a video the user purged. Shared by the background
    sync and the active-workbench backup importer so both mirror likes the same
    way.
    """
    existing = await tx.fetch_one_or_none(
        select(IngestedVideo).where(IngestedVideo.video_id.eq(raw.video_id))
    )
    if existing is None:
        _ = await tx.execute(insert(_new_ingested_video(raw)))
        return
    _ = await tx.execute(
        update(IngestedVideo)
        .set(IngestedVideo.source.to("liked"))
        .set(IngestedVideo.title.to(raw.title))
        .set(IngestedVideo.channel.to(raw.channel))
        .set(IngestedVideo.topic.to(raw.topic))
        .set(IngestedVideo.description.to(raw.description))
        .set(IngestedVideo.channel_id.to(raw.channel_id))
        # A raw without liked_at (e.g. a detail-only record) must not clear a
        # timestamp an earlier liked-page pass already recorded.
        .set(
            IngestedVideo.liked_at.to(
                raw.liked_at if raw.liked_at is not None else existing.liked_at
            )
        )
        .set(IngestedVideo.video_published_at.to(raw.video_published_at))
        .set(IngestedVideo.duration_seconds.to(raw.duration_seconds))
        .set(IngestedVideo.category_id.to(raw.category_id))
        .set(IngestedVideo.default_language.to(raw.default_language))
        .set(IngestedVideo.default_audio_language.to(raw.default_audio_language))
        .set(
            IngestedVideo.caption_available.to(
                _bool_to_int(value=raw.caption_available)
            )
        )
        .set(IngestedVideo.privacy_status.to(raw.privacy_status))
        .set(
            IngestedVideo.licensed_content.to(_bool_to_int(value=raw.licensed_content))
        )
        .set(IngestedVideo.made_for_kids.to(_bool_to_int(value=raw.made_for_kids)))
        .set(IngestedVideo.live_broadcast_content.to(raw.live_broadcast_content))
        .set(IngestedVideo.definition.to(raw.definition))
        .set(IngestedVideo.dimension.to(raw.dimension))
        .set(IngestedVideo.statistics_view_count.to(raw.statistics_view_count))
        .set(IngestedVideo.statistics_like_count.to(raw.statistics_like_count))
        .set(IngestedVideo.statistics_comment_count.to(raw.statistics_comment_count))
        .set(IngestedVideo.statistics_fetched_at.to(raw.statistics_fetched_at))
        .set(
            IngestedVideo.topic_categories_json.to(_json_or_none(raw.topic_categories))
        )
        .set(IngestedVideo.tags_json.to(_json_or_none(raw.tags)))
        .set(IngestedVideo.thumbnails_json.to(_json_or_none(raw.thumbnails)))
        .set(IngestedVideo.updated_at.to(CurrentTimestamp))
        .where(IngestedVideo.video_id.eq(raw.video_id))
    )
    # Self-correction: when captions appear on a video the worker previously gave
    # up on (its manual-caption flag flipped false -> true), clear any terminal
    # transcript state so it re-enters the sweep instead of staying unavailable.
    if existing.caption_available == 0 and raw.caption_available is True:
        await _clear_terminal_transcript_state(tx, raw.video_id)


async def _clear_terminal_transcript_state(tx: Transaction, video_id: str) -> None:
    """Drop a video's transcript state row when it is terminal, re-opening the fetch.

    Absence of a row means *pending*, so deleting a terminal row is what returns the
    video to the eligible sweep; a `done` or `retry` row is left untouched."""
    existing = await _transcript_state(tx, video_id)
    if existing is None or existing.status != "terminal":
        return
    _ = await tx.execute(
        delete(YouTubeTranscriptState).where(
            YouTubeTranscriptState.video_id.eq(video_id)
        )
    )


class YouTubeSyncService:
    """Background ingestion: pull liked videos a page at a time into the cache.

    Reconciler-shaped (like `SearchReconciler`): an idempotent `sync` pass run at
    startup and on a periodic loop. Each pass pulls a few hot (most-recent) pages
    and advances a persisted backfill cursor through history, bounded by an
    optional cutoff date, enriches via the batched detail call, and upserts into
    `IngestedVideo` — preserving local ignore state and any fetched transcript.
    Stops calling once the day's budget is exhausted.
    """

    def __init__(
        self,
        database: Database,
        client: YouTubeApiClient,
        tracer: Tracer,
        *,
        config: YouTubeSyncConfig | None = None,
        event_publisher: EventPublisher | None = None,
    ) -> None:
        resolved = config or YouTubeSyncConfig()
        self.database: Database = database
        self.client: YouTubeApiClient = client
        self.tracer: Tracer = tracer
        self.hot_pages: int = max(1, resolved.hot_pages)
        self.backfill_pages: int = max(0, resolved.backfill_pages)
        self.page_size: int = max(1, resolved.page_size)
        self.cutoff_date: date | None = resolved.cutoff_date
        self.min_interval: timedelta | None = resolved.min_interval
        self.rewalk_interval: timedelta | None = resolved.rewalk_interval
        self.drift_alarm_margin: int = max(0, resolved.drift_alarm_margin)
        self.event_publisher: EventPublisher = event_publisher or NullEventPublisher()

    async def maybe_sync(self, *, logger: Logger) -> SyncReport | None:
        """Run a sync pass only if the gate window has elapsed since the last run.

        The startup path calls this rather than `sync` so app restarts within
        `min_interval` don't re-spend the YouTube budget. With no `min_interval`
        configured the gate is off and this always syncs. Returns the pass's
        report, or None when the pass was skipped.
        """
        elapsed = await self._interval_elapsed()
        if not elapsed:
            _debug(logger, "YouTube sync skipped: within min-interval gate")
            return None
        return await self.sync(logger=logger)

    async def _interval_elapsed(self) -> bool:
        """True if no gate is set, no prior run is recorded, or it is stale."""
        if self.min_interval is None:
            return True
        last_run = await self.last_run_at()
        if last_run is None:
            return True
        return self.client.now() - last_run >= self.min_interval

    async def last_run_at(self) -> datetime | None:
        """The clock-sourced instant of the most recently completed sync pass.

        None when no pass has completed yet, or the persisted value is
        malformed. The public counterpart of `_mark_run`'s write side; the
        interval gate and status surface both read the last-run time through
        this rather than the raw sync-state key.
        """
        return await _read_last_run_at(self.database)

    async def sync(self, *, logger: Logger) -> SyncReport:
        """Run one idempotent ingestion pass: hot pages then backfill pages."""
        with self.tracer.start_as_current_span("YouTubeSyncService.sync"):
            _debug(logger, "YouTube sync starting")
            tally = _SyncTally()
            backfill_exhausted = False
            backfill_deferred = False
            drift_detected = False
            quota_exhausted = False
            # Resume the persisted backfill cursor (the hot tail seeds it first run)
            # and the completion marker that stops the perpetual re-walk.
            cursor = await self.backfill_cursor()
            completed_at = await self.backfill_completed_at()
            try:
                hot_token, total_results = await self._pull_hot_pages(tally)
                # Decide whether to touch history this pass: a settled backfill is
                # left alone until it ages out or drifts from the upstream total.
                drift_detected = await self._detect_drift(
                    total_results, completed_at, logger=logger
                )
                active, cursor, completed_at = self._resolve_backfill(
                    cursor, completed_at, restart=drift_detected, now=self.client.now()
                )
                # A settled backfill that neither drifted nor aged out is deferred:
                # `_resolve_backfill` declines to walk history and only the hot pages
                # ran this pass.
                backfill_deferred = not active
                if active:
                    cursor, backfill_exhausted = await self._walk_backfill(
                        tally, cursor if cursor is not None else hot_token
                    )
            except YouTubeQuotaExceededError as error:
                # The day's budget is spent: stop calling out and resume next pass.
                quota_exhausted = True
                _debug(logger, "YouTube sync stopped on quota", error=str(error))
            if backfill_exhausted:
                # Record completion so the next pass leaves history settled.
                completed_at = self.client.now()
            await self._store_cursor(cursor)
            await self._store_completed_at(completed_at)
            await self._mark_run()

        _info(
            logger,
            "YouTube sync completed",
            pulled=tally.pulled,
            upserted=tally.upserted,
            pages=tally.pages,
            backfill_exhausted=backfill_exhausted,
            backfill_deferred=backfill_deferred,
            drift_detected=drift_detected,
            quota_exhausted=quota_exhausted,
        )
        if tally.upserted:
            await self.event_publisher.publish(InvalidateEvent(keys=["youtube"]))
        return SyncReport(
            pulled=tally.pulled,
            upserted=tally.upserted,
            pages=tally.pages,
            backfill_exhausted=backfill_exhausted,
            backfill_deferred=backfill_deferred,
            drift_detected=drift_detected,
        )

    async def _pull_hot_pages(self, tally: _SyncTally) -> tuple[str | None, int | None]:
        """Mirror the hot (newest) pages into `tally`; return the next-page cursor and
        the upstream playlist total from the first page (for the drift check)."""
        hot_token: str | None = None
        total_results: int | None = None
        for index in range(self.hot_pages):
            page = await self.client.list_liked_page(
                page_token=hot_token, page_size=self.page_size
            )
            if index == 0:
                total_results = page.total_results
            tally.pages += 1
            scoped, reached_cutoff = self._apply_cutoff(page.videos)
            # Count the page as pulled only once `_mirror_page` returns; a quota stop
            # mid-enrich must not overstate the report.
            tally.upserted += await self._mirror_page(scoped)
            tally.pulled += len(scoped)
            hot_token = page.next_page_token
            if hot_token is None or reached_cutoff:
                break
        return hot_token, total_results

    async def _walk_backfill(
        self, tally: _SyncTally, cursor: str | None
    ) -> tuple[str | None, bool]:
        """Advance the backfill cursor through history, mirroring pages into `tally`;
        return the resumable cursor and whether history was exhausted this pass."""
        for _ in range(self.backfill_pages):
            if cursor is None:
                return None, True
            page = await self.client.list_liked_page(
                page_token=cursor, page_size=self.page_size
            )
            tally.pages += 1
            scoped, hit_cutoff = self._apply_cutoff(page.videos)
            tally.upserted += await self._mirror_page(scoped)
            tally.pulled += len(scoped)
            cursor = None if hit_cutoff else page.next_page_token
            if cursor is None:
                return None, True
        return cursor, False

    async def sync_forever(self, *, interval_seconds: float, logger: Logger) -> None:
        """Run sync passes on the given interval until cancelled."""
        while True:
            await asyncio.sleep(interval_seconds)
            try:
                _ = await self.sync(logger=logger)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Mirror the search reconciler: keep the loop alive but preserve
                # the traceback for the swallowed failure.
                logger.exception("YouTube sync pass failed")

    def _apply_cutoff(
        self, videos: Sequence[RawYouTubeVideo]
    ) -> tuple[list[RawYouTubeVideo], bool]:
        """Drop videos liked before the cutoff; report if the cutoff was reached."""
        if self.cutoff_date is None:
            return list(videos), False
        kept: list[RawYouTubeVideo] = []
        reached = False
        for raw in videos:
            if (
                raw.liked_at is not None
                and raw.liked_at.astimezone(UTC).date() < self.cutoff_date
            ):
                reached = True
                continue
            kept.append(raw)
        return kept, reached

    async def _mirror_page(self, videos: Sequence[RawYouTubeVideo]) -> int:
        """Enrich and upsert a page, preserving local transcript + ignore state.

        A video the detail fetch omits is un-ingestable (members-only, private,
        deleted) and is skipped rather than mirrored from the thin liked-page
        entry, keeping the corpus clean.
        """
        if not videos:
            return 0
        details = await self.client.fetch_video_metadata(
            [raw.video_id for raw in videos]
        )

        async def _mirror(tx: Transaction) -> int:
            upserted = 0
            skipped: set[str] = set()
            ingested: set[str] = set()
            for raw in videos:
                enriched = details.get(raw.video_id)
                if enriched is None:
                    # No fetchable details: track the id so drift accounting can fold
                    # this known, un-ingestable gap in rather than alarming on it.
                    skipped.add(raw.video_id)
                    continue
                if raw.liked_at is not None:
                    # Only the liked-page item knows when the user liked the
                    # video; the detail fetch has no playlist context.
                    enriched = enriched.model_copy(update={"liked_at": raw.liked_at})
                await self._upsert(tx, enriched)
                # A previously-skipped video that now ingests self-corrects the set.
                ingested.add(raw.video_id)
                upserted += 1
            await self._update_known_skipped(tx, add=skipped, remove=ingested)
            return upserted

        return await run_in_transaction(self.database, _mirror)

    async def _update_known_skipped(
        self, tx: Transaction, *, add: set[str], remove: set[str]
    ) -> None:
        """Fold this page's skipped/ingested ids into the persisted skipped-id set.

        Adds ids whose details were missing and drops any that ingested, writing back
        only when the set actually changes so a clean page stays read-only."""
        if not add and not remove:
            return
        row = await tx.fetch_one_or_none(
            select(YouTubeSyncState).where(
                YouTubeSyncState.key.eq(_KNOWN_SKIPPED_IDS_KEY)
            )
        )
        current = _decode_skipped_ids(row.value if row is not None else None)
        updated = (current | add) - remove
        if updated == current:
            return
        value = json.dumps(sorted(updated))
        if row is None:
            _ = await tx.execute(
                insert(YouTubeSyncState(key=_KNOWN_SKIPPED_IDS_KEY, value=value))
            )
        else:
            _ = await tx.execute(
                update(YouTubeSyncState)
                .set(YouTubeSyncState.value.to(value))
                .where(YouTubeSyncState.key.eq(_KNOWN_SKIPPED_IDS_KEY))
            )

    async def _upsert(self, tx: Transaction, raw: RawYouTubeVideo) -> None:
        await upsert_ingested_video(tx, raw)

    def _resolve_backfill(
        self,
        cursor: str | None,
        completed_at: datetime | None,
        *,
        restart: bool,
        now: datetime,
    ) -> tuple[bool, str | None, datetime | None]:
        """Decide whether to walk history this pass, returning `(active, cursor,
        completed_at)`.

        Drift forces a fresh walk from the hot tail. Otherwise an un-completed
        backfill keeps advancing its cursor; a completed one stays settled until it
        is older than `rewalk_interval`, at which point it re-walks from the tail. A
        `None` interval settles forever once completed.
        """
        if restart:
            return True, None, None
        if completed_at is None:
            return True, cursor, None
        if self.rewalk_interval is not None and now - completed_at >= (
            self.rewalk_interval
        ):
            return True, None, None
        return False, cursor, completed_at

    async def _detect_drift(
        self,
        total_results: int | None,
        completed_at: datetime | None,
        *,
        logger: Logger,
    ) -> bool:
        """Whether a *completed* backfill has drifted far below the upstream total.

        Only meaningful once history has been walked (before then the local corpus
        is legitimately smaller). A shortfall beyond `drift_alarm_margin` means
        likes were added faster than the hot pages caught, so the walk is restarted
        and the gap logged loudly.
        """
        if completed_at is None or total_results is None:
            return False
        local = await self._local_liked_count()
        known_skipped = await self._known_skipped_count()
        if total_results - (local + known_skipped) <= self.drift_alarm_margin:
            return False
        logger.warning(
            "YouTube likes drift detected; restarting backfill",
            upstream_total=total_results,
            local_count=local,
            known_skipped_count=known_skipped,
            drift_alarm_margin=self.drift_alarm_margin,
        )
        return True

    async def known_skipped_ids(self) -> frozenset[str]:
        """The liked video ids tracked as un-ingestable (no fetchable details).

        Typed accessor over the persisted sync-state row so call sites (and
        tests) never need the private key or the raw JSON-array decoder.
        """
        raw = await state_get(self.database, _KNOWN_SKIPPED_IDS_KEY)
        return frozenset(_decode_skipped_ids(raw))

    async def _known_skipped_count(self) -> int:
        """Count the liked videos tracked as un-ingestable (no fetchable details)."""
        return len(await self.known_skipped_ids())

    async def _local_liked_count(self) -> int:
        """Count the liked videos mirrored locally (active and ignored alike)."""
        async with self.database.transaction() as tx:
            rows = await tx.fetch_all(
                select(IngestedVideo.video_id).where(IngestedVideo.source.eq("liked"))
            )
        return len(rows)

    async def reset_backfill(self) -> None:
        """Clear the cursor and completion marker so the next pass re-walks history.

        The manual escape hatch behind `just youtube-reset-backfill`: a full resync
        of liked history on demand, without waiting for the re-walk interval.
        """
        await self._store_cursor(None)
        await self._store_completed_at(None)

    async def backfill_cursor(self) -> str | None:
        """The resumable backfill page cursor, or None once exhausted/unset.

        Typed accessor over the persisted sync-state row so call sites (and
        tests) never need the private key.
        """
        value = await state_get(self.database, _BACKFILL_CURSOR_KEY)
        return value or None

    async def _store_cursor(self, cursor: str | None) -> None:
        # An exhausted cursor is stored as the empty string and reads back as
        # absent, so the next pass restarts the backfill from the hot tail.
        await state_set(self.database, _BACKFILL_CURSOR_KEY, cursor or "")

    async def backfill_completed_at(self) -> datetime | None:
        """When the backfill last reached the end of history, or None if it hasn't.

        Typed accessor over the persisted sync-state row so call sites (and
        tests) never need the private key.
        """
        raw = await state_get(self.database, _BACKFILL_COMPLETED_AT_KEY)
        return datetime.fromisoformat(raw) if raw else None

    async def _store_completed_at(self, completed_at: datetime | None) -> None:
        # An unset marker is stored as the empty string and reads back as absent,
        # so an incomplete or reset backfill keeps walking.
        await state_set(
            self.database,
            _BACKFILL_COMPLETED_AT_KEY,
            completed_at.isoformat() if completed_at is not None else "",
        )

    async def _mark_run(self) -> None:
        await state_set(
            self.database, _LIKES_LAST_RUN_KEY, self.client.now().isoformat()
        )


# --- Per-video transcript state machine + the shared fetch/persist path --------


async def _transcript_state(
    tx: Transaction, video_id: str
) -> YouTubeTranscriptState[Fetched] | None:
    """Return a video's persisted transcript state row, or None when pending."""
    return await tx.fetch_one_or_none(
        select(YouTubeTranscriptState).where(
            YouTubeTranscriptState.video_id.eq(video_id)
        )
    )


@dataclass(frozen=True, slots=True)
class _StateWrite:
    """The mutable fields of one transcript-state transition."""

    status: TranscriptStatus
    attempts: int
    next_attempt_at: str | None
    last_error: str | None


async def _write_transcript_state(
    tx: Transaction, video_id: str, fields: _StateWrite
) -> None:
    """Insert or refresh a video's transcript-state row in place."""
    existing = await _transcript_state(tx, video_id)
    if existing is None:
        _ = await tx.execute(
            insert(
                YouTubeTranscriptState(
                    video_id=video_id,
                    status=fields.status,
                    attempts=fields.attempts,
                    next_attempt_at=fields.next_attempt_at,
                    last_error=fields.last_error,
                )
            )
        )
        return
    _ = await tx.execute(
        update(YouTubeTranscriptState)
        .set(YouTubeTranscriptState.status.to(fields.status))
        .set(YouTubeTranscriptState.attempts.to(fields.attempts))
        .set(YouTubeTranscriptState.next_attempt_at.to(fields.next_attempt_at))
        .set(YouTubeTranscriptState.last_error.to(fields.last_error))
        .set(YouTubeTranscriptState.updated_at.to(CurrentTimestamp))
        .where(YouTubeTranscriptState.video_id.eq(video_id))
    )


def _next_attempt_at(now: datetime, attempts: int, config: TranscriptSyncConfig) -> str:
    """Return the ISO time of the next retry: `base * 2**(attempts-1)`, capped.

    `attempts` is the post-increment count (>= 1), so the first retry waits one
    base interval and each subsequent one doubles up to the cap.
    """
    exponent = max(0, attempts - 1)
    delay = min(config.backoff_base * (2**exponent), config.backoff_cap)
    return (now + delay).isoformat()


async def _load_provider_pause(database: Database, source: str) -> PauseState:
    """Read one source's persisted pause (defaulting to not-paused, streak 0)."""
    return await load_pause_state(
        partial(state_get, database), keys=provider_pause_keys(source)
    )


async def load_all_provider_pauses(
    database: Database,
) -> dict[str, PauseState]:
    """Read every blockable source's persisted pause, keyed by source.

    Discovers sources from the persisted keys themselves (rather than a hardcoded
    list) so any provider that has ever blocked is reconstructed without the worker
    needing to know the chain's composition.
    """
    async with database.transaction() as tx:
        until_rows = await tx.fetch_all(
            select(YouTubeSyncState).where(
                YouTubeSyncState.key.like(f"{_TRANSCRIPT_PAUSED_UNTIL_PREFIX}%")
            )
        )
        streak_rows = await tx.fetch_all(
            select(YouTubeSyncState).where(
                YouTubeSyncState.key.like(f"{_TRANSCRIPT_BLOCK_STREAK_PREFIX}%")
            )
        )
    sources = {
        row.key.removeprefix(_TRANSCRIPT_PAUSED_UNTIL_PREFIX) for row in until_rows
    } | {row.key.removeprefix(_TRANSCRIPT_BLOCK_STREAK_PREFIX) for row in streak_rows}
    return {source: await _load_provider_pause(database, source) for source in sources}


@dataclass(frozen=True, slots=True)
class TranscriptFetchContext:
    """The collaborators one transcript fetch+persist needs, bundled.

    Built once by the worker and inline by the on-demand path, so both drive the
    same provider and persistence with the same retry/backoff config.
    """

    database: Database
    provider: TranscriptProvider
    config: TranscriptSyncConfig
    event_publisher: EventPublisher


async def fetch_and_store_transcript(
    context: TranscriptFetchContext,
    *,
    video_id: str,
    now: datetime,
    paused_sources: frozenset[str] = _NO_PAUSED_SOURCES,
    skip_sources: frozenset[str] = _NO_PAUSED_SOURCES,
) -> TranscriptAttempt:
    """Run one provider fetch for a video and persist the resulting state.

    The single code path shared by the background worker and the on-demand fetch:
    the caller charges the budget first, then this calls the provider and maps the
    outcome onto storage. Success stores the transcript and marks the state
    ``done``; *unavailable* marks terminal; *excluded* marks terminal and purges
    the video from active ingestion; *transient* increments attempts and schedules
    a backed-off retry; *blocked* leaves the per-video state untouched (it is a
    provider-level signal the worker handles by pausing the whole provider) and
    carries any retry-after hint back. It never raises for the four failure
    categories — it returns a typed `TranscriptAttempt` the caller acts on.

    `paused_sources` is forwarded to the provider so the worker can run only the
    reachable sources while some blockable provider is in cooldown.
    """
    database = context.database
    try:
        fetched = await context.provider.fetch(
            video_id, paused_sources=paused_sources, skip_sources=skip_sources
        )
    except TranscriptUnavailableError as error:
        await _mark_terminal(database, video_id, error=str(error), purge=False)
        return TranscriptAttempt(outcome="unavailable")
    except TranscriptExcludedError as error:
        await _mark_terminal(database, video_id, error=str(error), purge=True)
        return TranscriptAttempt(outcome="excluded")
    except TranscriptBlockedError as error:
        return TranscriptAttempt(
            outcome="blocked", retry_after=error.retry_after, source=error.source
        )
    except TranscriptTransientError as error:
        await _record_retry(
            database, video_id, now=now, config=context.config, error=str(error)
        )
        return TranscriptAttempt(outcome="transient")
    updated = await _store_transcript(database, video_id, fetched)
    await context.event_publisher.publish(InvalidateEvent(keys=["youtube"]))
    return TranscriptAttempt(
        outcome="done", video=updated, text=fetched.text, source=fetched.source
    )


def _segments_to_json(segments: tuple[TranscriptSegment, ...]) -> str | None:
    """Encode timed transcript segments as a JSON array, or None when there are none.

    Text-only providers yield no segments, so a null column distinguishes "not
    timed" from an empty list; the joined `transcript` still carries the text."""
    if not segments:
        return None
    return json.dumps(
        [
            {"start_seconds": segment.start_seconds, "text": segment.text}
            for segment in segments
        ]
    )


async def _store_transcript(
    database: Database, video_id: str, fetched: FetchedTranscript
) -> IngestedVideo[Fetched]:
    """Persist a fetched transcript, its segments, and its source; mark state done."""

    async def _store(tx: Transaction) -> IngestedVideo[Fetched] | None:
        existing = await _transcript_state(tx, video_id)
        attempts = existing.attempts if existing is not None else 0
        _ = await tx.execute(
            update(IngestedVideo)
            .set(IngestedVideo.transcript.to(fetched.text))
            .set(
                IngestedVideo.transcript_segments_json.to(
                    _segments_to_json(fetched.segments)
                )
            )
            .set(IngestedVideo.transcript_source.to(fetched.source))
            .set(IngestedVideo.updated_at.to(CurrentTimestamp))
            .where(IngestedVideo.video_id.eq(video_id))
        )
        await _write_transcript_state(
            tx,
            video_id,
            _StateWrite(
                status="done",
                attempts=attempts,
                next_attempt_at=None,
                last_error=None,
            ),
        )
        return await tx.fetch_one_or_none(
            select(IngestedVideo).where(IngestedVideo.video_id.eq(video_id))
        )

    updated = await run_in_transaction(database, _store)
    if updated is None:
        raise YouTubeVideoNotFoundError(video_id)
    return updated


async def _mark_terminal(
    database: Database, video_id: str, *, error: str, purge: bool
) -> None:
    """Mark a video's transcript terminal; optionally purge it from ingestion."""

    async def _mark(tx: Transaction) -> None:
        existing = await _transcript_state(tx, video_id)
        attempts = existing.attempts if existing is not None else 0
        await _write_transcript_state(
            tx,
            video_id,
            _StateWrite(
                status="terminal",
                attempts=attempts,
                next_attempt_at=None,
                last_error=error,
            ),
        )
        if purge:
            _ = await tx.execute(
                update(IngestedVideo)
                .set(IngestedVideo.ignored_at.to(CurrentTimestamp))
                .set(IngestedVideo.updated_at.to(CurrentTimestamp))
                .where(IngestedVideo.video_id.eq(video_id))
                .where(IngestedVideo.ignored_at.is_null())
            )

    await run_in_transaction(database, _mark)


async def _record_retry(
    database: Database,
    video_id: str,
    *,
    now: datetime,
    config: TranscriptSyncConfig,
    error: str,
) -> None:
    """Increment a video's attempt count and schedule a backed-off retry."""

    async def _retry(tx: Transaction) -> None:
        existing = await _transcript_state(tx, video_id)
        attempts = (existing.attempts if existing is not None else 0) + 1
        await _write_transcript_state(
            tx,
            video_id,
            _StateWrite(
                status="retry",
                attempts=attempts,
                next_attempt_at=_next_attempt_at(now, attempts, config),
                last_error=error,
            ),
        )

    await run_in_transaction(database, _retry)


class YouTubeService:
    """Capability surface for the local YouTube ingested corpus.

    Browse and Search read only `IngestedVideo` (instant, no quota). Transcript
    fetch is the one capability that still calls upstream — through the
    `TranscriptProvider` port, guarded by the daily budget and short-circuited
    once a transcript is stored. Each mutation owns its transaction.
    """

    def __init__(
        self,
        database: Database,
        client: YouTubeApiClient,
        tracer: Tracer,
        *,
        provider: TranscriptProvider | None = None,
        event_publisher: EventPublisher | None = None,
    ) -> None:
        self.database: Database = database
        self.client: YouTubeApiClient = client
        self.tracer: Tracer = tracer
        self.provider: TranscriptProvider = provider or NullTranscriptProvider()
        self.event_publisher: EventPublisher = event_publisher or NullEventPublisher()
        # The retry/backoff config the on-demand fetch persists its transient retry
        # with. Defaults here and is late-bound by the composition root to the same
        # config the background worker uses, so both schedule retries on one cadence.
        self.config: TranscriptSyncConfig = TranscriptSyncConfig()
        # Optional, late-bound search collaborator (wired by the composition root
        # after construction). When set, semantic transcript search replaces the
        # lexical fallback; left None (search disabled), `search` keeps the SQLite
        # LIKE behaviour.
        self.transcript_search: TranscriptSearchService | None = None
        # Optional, late-bound reader for Supadata's separate monthly usage
        # (wired by the composition root after construction, once the provider
        # tree's Supadata leaf — if any — has its persisted guard bound). None
        # (the default) reports no Supadata usage on the status surface.
        self.supadata_usage: SupadataUsageReader | None = None

    async def browse(
        self,
        *,
        topic: str | None = None,
        source: YouTubeSource | None = None,
        limit: int | None = None,
        logger: Logger,
    ) -> BrowseResult:
        """List active ingested videos from the local corpus, newest-liked-first.

        Reads only local state — the background sync is what refreshes the
        corpus, so a browse never calls upstream and costs no quota. `limit`
        caps the rows returned (`None` is unbounded); assistant-facing callers
        pass a bound so a large corpus can't flood the model's context.
        """
        with self.tracer.start_as_current_span("YouTubeService.browse"):
            _debug(logger, "Browsing YouTube ingestion", topic=topic, source=source)
            query = select(IngestedVideo).where(IngestedVideo.ignored_at.is_null())
            if source is not None:
                query = query.where(IngestedVideo.source.eq(source))
            if topic is not None:
                query = query.where(IngestedVideo.topic.like(topic))
            query = query.order_by(
                IngestedVideo.liked_at.desc(), IngestedVideo.created_at.desc()
            )
            if limit is not None:
                query = query.limit(limit)
            async with self.database.transaction() as tx:
                videos = await tx.fetch_all(query)
        _debug(logger, "YouTube browse completed", result_count=len(videos))
        return BrowseResult(
            videos=videos,
            cache=CacheMeta(hit=True, source="cache"),
            quota=await self.client.snapshot(),
        )

    async def sync_status(self, *, logger: Logger) -> YouTubeSyncStatus:
        """Summarise the background ingestion's progress and health (local only).

        Reads the local corpus and bookkeeping — never upstream — so the UI can
        poll it cheaply: how many videos are ingested, the transcript backlog,
        when the likes sync last ran, the day's quota, and any active pause (a
        live Data API block or a per-source transcript provider block) that
        explains why progress has stalled.
        """
        with self.tracer.start_as_current_span("YouTubeService.sync_status"):
            now = self.client.now()
            terminal_ids = select(YouTubeTranscriptState.video_id).where(
                YouTubeTranscriptState.status.eq("terminal")
            )
            async with self.database.transaction() as tx:
                active = await tx.fetch_all(
                    select(IngestedVideo).where(IngestedVideo.ignored_at.is_null())
                )
                untranscribed = await tx.fetch_all(
                    select(IngestedVideo)
                    .where(IngestedVideo.ignored_at.is_null())
                    .where(IngestedVideo.transcript.is_null())
                )
                # Pending excludes the permanently-failed (terminal) videos, so the
                # remainder of the untranscribed set is the unavailable count.
                pending = await tx.fetch_all(
                    select(IngestedVideo)
                    .where(IngestedVideo.ignored_at.is_null())
                    .where(IngestedVideo.transcript.is_null())
                    .where(IngestedVideo.video_id.not_in_subquery(terminal_ids))
                )
            total = len(active)
            owed = len(untranscribed)
            pending_count = len(pending)
            last_run = await _read_last_run_at(self.database)
            api_paused_until = await self.client.api_paused_until(now=now)
            pauses = await load_all_provider_pauses(self.database)
        providers_paused = [
            TranscriptProviderPause(source=source, paused_until=pause.paused_until)
            for source, pause in sorted(pauses.items())
            if pause.is_paused(now) and pause.paused_until is not None
        ]
        supadata = (
            await self.supadata_usage.snapshot(now=now)
            if self.supadata_usage is not None
            else None
        )
        status = YouTubeSyncStatus(
            videos_total=total,
            transcripts_done=total - owed,
            transcripts_pending=pending_count,
            transcripts_unavailable=owed - pending_count,
            last_synced_at=last_run,
            quota=await self.client.snapshot(),
            api_paused_until=api_paused_until,
            transcript_providers_paused=providers_paused,
            supadata=supadata,
        )
        _debug(
            logger,
            "YouTube sync status computed",
            videos_total=total,
            transcripts_pending=pending_count,
        )
        return status

    async def search(
        self, query: str, *, limit: int | None = None, logger: Logger
    ) -> SearchResult:
        """Search saved content and transcript text (local only).

        When semantic transcript search is wired (`transcript_search`), the query
        is embedded and matched against the transcript-chunk index, ranking videos
        by relevance and returning the best-matching snippet per video. With
        search disabled it falls back to the lexical SQLite `LIKE` path: each
        whitespace term matched case-insensitively against title, description, or
        transcript and AND-ed. Only active videos match; `limit` caps the rows.
        """
        if not query.split():
            message = "keyword Search requires a non-empty query"
            raise EmptyYouTubeSearchQueryError(message)
        if self.transcript_search is not None:
            return await self._semantic_search(query, limit=limit, logger=logger)
        return await self._lexical_search(query, limit=limit, logger=logger)

    async def _semantic_search(
        self, query: str, *, limit: int | None, logger: Logger
    ) -> SearchResult:
        """Embed the query, rank videos by transcript relevance, attach snippets."""
        assert self.transcript_search is not None
        video_limit = limit if limit is not None else _DEFAULT_SEMANTIC_LIMIT
        _debug(logger, "Searching YouTube transcripts semantically", limit=video_limit)
        matches = await self.transcript_search.candidates(
            query, limit=video_limit, logger=logger
        )
        if not matches:
            _debug(logger, "YouTube semantic search completed", result_count=0)
            return SearchResult(
                videos=[],
                cache=CacheMeta(hit=True, source="cache"),
                quota=await self.client.snapshot(),
            )
        video_ids = [match.video_id for match in matches]
        async with self.database.transaction() as tx:
            videos = await tx.fetch_all(
                select(IngestedVideo)
                .where(IngestedVideo.video_id.in_(*video_ids))
                .where(IngestedVideo.ignored_at.is_null())
            )
        by_video_id = {video.video_id: video for video in videos}
        # Preserve relevance order and drop any match whose video has since been
        # ignored or deleted (index drift the next reconcile would clean up).
        ordered = [
            by_video_id[match.video_id]
            for match in matches
            if match.video_id in by_video_id
        ]
        snippets = {
            match.video_id: match.snippet
            for match in matches
            if match.video_id in by_video_id
        }
        _debug(logger, "YouTube semantic search completed", result_count=len(ordered))
        return SearchResult(
            videos=ordered,
            cache=CacheMeta(hit=True, source="cache"),
            quota=await self.client.snapshot(),
            snippets=snippets,
        )

    async def _lexical_search(
        self, query: str, *, limit: int | None, logger: Logger
    ) -> SearchResult:
        """The SQLite `LIKE` fallback used when semantic search is disabled."""
        terms = query.split()
        _debug(logger, "Searching YouTube ingestion", terms_count=len(terms))
        statement = select(IngestedVideo).where(IngestedVideo.ignored_at.is_null())
        for term in terms:
            pattern = f"%{term}%"
            statement = statement.where(
                IngestedVideo.title.like(pattern)
                | IngestedVideo.description.like(pattern)
                | IngestedVideo.transcript.like(pattern)
            )
        statement = statement.order_by(
            IngestedVideo.liked_at.desc(), IngestedVideo.created_at.desc()
        )
        if limit is not None:
            statement = statement.limit(limit)
        async with self.database.transaction() as tx:
            videos = await tx.fetch_all(statement)
        _debug(logger, "YouTube search completed", result_count=len(videos))
        return SearchResult(
            videos=videos,
            cache=CacheMeta(hit=True, source="cache"),
            quota=await self.client.snapshot(),
        )

    async def fetch_transcript(
        self, video_id: str, *, logger: Logger
    ) -> TranscriptResult:
        """Fetch and persist a transcript for an ingested video.

        The video must already be ingested (sync runs first). A stored transcript
        short-circuits with no provider call; otherwise the fetch runs through the
        same `TranscriptProvider` port and persistence the background worker uses,
        so manual and background fetches share one code path. Only the captions
        provider spends the YouTube Data API daily budget (it charges itself,
        right before its own live call — see `CaptionsTranscriptProvider`); a
        depleted day surfaces as `YouTubeQuotaExceededError` from the provider
        call below (translated to 429 at the boundary) rather than a pre-check
        here, so a chain without captions (the default) never spends or blocks on
        it. The three failure outcomes surface as typed errors:
        unavailable/excluded -> `TranscriptUnavailableError`, transient ->
        `TranscriptTransientError`.
        """
        _debug(logger, "Fetching YouTube transcript", video_id=video_id)
        video = await self.get_video(video_id)
        if video.transcript is not None:
            return TranscriptResult(
                video=video,
                transcript=video.transcript,
                cache=CacheMeta(hit=True, source="cache"),
                quota=await self.client.snapshot(),
            )
        context = TranscriptFetchContext(
            database=self.database,
            provider=self.provider,
            config=self.config,
            event_publisher=self.event_publisher,
        )
        attempt = await fetch_and_store_transcript(
            context, video_id=video_id, now=self.client.now()
        )
        if attempt.outcome in ("unavailable", "excluded"):
            raise TranscriptUnavailableError(video_id)
        if attempt.outcome == "blocked":
            message = f"transcript provider blocked while fetching {video_id}"
            raise TranscriptBlockedError(message, retry_after=attempt.retry_after)
        if attempt.outcome == "transient":
            message = f"transcript fetch for {video_id} failed transiently"
            raise TranscriptTransientError(message)
        # The `done` outcome always carries the stored video and its text.
        assert attempt.video is not None and attempt.text is not None
        _info(logger, "YouTube transcript fetched", video_id=video_id)
        return TranscriptResult(
            video=attempt.video,
            transcript=attempt.text,
            cache=CacheMeta(hit=False, source="live"),
            quota=await self.client.snapshot(),
        )

    async def ignore(self, video_id: str, *, logger: Logger) -> IngestedVideo[Fetched]:
        """Purge a video from ingestion so browse/search no longer surface it."""
        _debug(logger, "Ignoring YouTube video", video_id=video_id)

        async def _ignore(tx: Transaction) -> IngestedVideo[Fetched]:
            _ = await self._fetch(tx, video_id)
            _ = await tx.execute(
                update(IngestedVideo)
                .set(IngestedVideo.ignored_at.to(CurrentTimestamp))
                .set(IngestedVideo.updated_at.to(CurrentTimestamp))
                .where(IngestedVideo.video_id.eq(video_id))
                .where(IngestedVideo.ignored_at.is_null())
            )
            return await self._fetch(tx, video_id)

        video = await run_in_transaction(self.database, _ignore)
        _info(logger, "YouTube video ignored", video_id=video_id)
        await self.event_publisher.publish(InvalidateEvent(keys=["youtube"]))
        return video

    async def retry(self, video_id: str, *, logger: Logger) -> IngestedVideo[Fetched]:
        """Un-ignore a previously purged video, returning it to ingestion."""
        _debug(logger, "Retrying YouTube video", video_id=video_id)

        async def _retry(tx: Transaction) -> IngestedVideo[Fetched]:
            _ = await self._fetch(tx, video_id)
            _ = await tx.execute(
                update(IngestedVideo)
                .set(IngestedVideo.ignored_at.to(None))
                .set(IngestedVideo.updated_at.to(CurrentTimestamp))
                .where(IngestedVideo.video_id.eq(video_id))
            )
            return await self._fetch(tx, video_id)

        video = await run_in_transaction(self.database, _retry)
        _info(logger, "YouTube video retried", video_id=video_id)
        await self.event_publisher.publish(InvalidateEvent(keys=["youtube"]))
        return video

    async def get_video(self, video_id: str) -> IngestedVideo[Fetched]:
        """Fetch one ingested video by its upstream id, or raise when absent."""
        async with self.database.transaction() as tx:
            return await self._fetch(tx, video_id)

    async def _fetch(self, tx: Transaction, video_id: str) -> IngestedVideo[Fetched]:
        """Fetch an ingested video by its upstream id or raise."""
        video = await tx.fetch_one_or_none(
            select(IngestedVideo).where(IngestedVideo.video_id.eq(video_id))
        )
        if video is None:
            raise YouTubeVideoNotFoundError(video_id)
        return video


# snekql replays a frozen, hand-authored migration chain and records each step by
# *name*, never re-running an applied one. The original `ingested_video` table +
# indexes are frozen verbatim under their first-shipped keys so existing
# databases skip them; enriched columns and the new bookkeeping tables arrive as
# their own forward migrations. Replaying the whole chain on a fresh database
# yields the current schema.
_INGESTED_VIDEO_COLUMNS: tuple[tuple[str, str], ...] = (
    ("channel_id", "TEXT"),
    ("liked_at", "TEXT"),
    ("video_published_at", "TEXT"),
    ("duration_seconds", "INTEGER"),
    ("category_id", "TEXT"),
    ("default_language", "TEXT"),
    ("default_audio_language", "TEXT"),
    ("caption_available", "INTEGER"),
    ("privacy_status", "TEXT"),
    ("licensed_content", "INTEGER"),
    ("made_for_kids", "INTEGER"),
    ("live_broadcast_content", "TEXT"),
    ("definition", "TEXT"),
    ("dimension", "TEXT"),
    ("statistics_view_count", "INTEGER"),
    ("statistics_like_count", "INTEGER"),
    ("statistics_comment_count", "INTEGER"),
    ("statistics_fetched_at", "TEXT"),
    ("topic_categories_json", "TEXT"),
    ("tags_json", "TEXT"),
    ("thumbnails_json", "TEXT"),
)


def _youtube_migrations() -> dict[str, str]:
    migrations: dict[str, str] = {
        # Original table + indexes, as first shipped (#76). Frozen verbatim.
        "004_create_ingested_video": (
            'CREATE TABLE "ingested_video" ('
            '"id" TEXT PRIMARY KEY NOT NULL, '
            '"video_id" TEXT NOT NULL, '
            '"source" TEXT, "title" TEXT, "channel" TEXT, "topic" TEXT, '
            '"description" TEXT, "transcript" TEXT, "ignored_at" TEXT, '
            "\"created_at\" TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')), "
            "\"updated_at\" TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))"
            ") STRICT"
        ),
        "004_create_index_ux_ingested_video_video_id": (
            'CREATE UNIQUE INDEX "ux_ingested_video_video_id" '
            'ON "ingested_video" ("video_id")'
        ),
        "004_create_index_ix_ingested_video_topic": (
            'CREATE INDEX "ix_ingested_video_topic" ON "ingested_video" ("topic")'
        ),
    }
    # Enriched metadata columns (sync-into-cache pivot, #80).
    for column, affinity in _INGESTED_VIDEO_COLUMNS:
        migrations[f"005_ingested_video_{column}"] = (
            f'ALTER TABLE "ingested_video" ADD COLUMN "{column}" {affinity}'
        )
    # Persisted daily budget + ingestion bookkeeping (#80). Table names match the
    # snekql model-derived names (`YouTubeQuotaDaily` -> `you_tube_quota_daily`).
    migrations["006_create_you_tube_quota_daily"] = (
        'CREATE TABLE "you_tube_quota_daily" ('
        '"day" TEXT PRIMARY KEY NOT NULL, "used" INTEGER'
        ") STRICT"
    )
    migrations["007_create_you_tube_sync_state"] = (
        'CREATE TABLE "you_tube_sync_state" ('
        '"key" TEXT PRIMARY KEY NOT NULL, "value" TEXT NOT NULL'
        ") STRICT"
    )
    # Per-video transcript state machine for the background transcript worker.
    migrations["008_create_you_tube_transcript_state"] = (
        'CREATE TABLE "you_tube_transcript_state" ('
        '"video_id" TEXT PRIMARY KEY NOT NULL, '
        '"status" TEXT NOT NULL, '
        '"attempts" INTEGER, '
        '"next_attempt_at" TEXT, '
        '"last_error" TEXT, '
        "\"updated_at\" TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))"
        ") STRICT"
    )
    # Transcript provenance: the timed segments and the producing provider, stored
    # alongside the joined `transcript` on every new fetch.
    migrations["009_ingested_video_transcript_segments_json"] = (
        'ALTER TABLE "ingested_video" ADD COLUMN "transcript_segments_json" TEXT'
    )
    migrations["009_ingested_video_transcript_source"] = (
        'ALTER TABLE "ingested_video" ADD COLUMN "transcript_source" TEXT'
    )
    return migrations


async def create_youtube_schema(database: Database) -> None:
    """Bring the YouTube ingestion schema to current on an initialized database.

    Applies the frozen migration chain: the original ingested-video table and
    indexes (skipped on databases that already have them), the enriched-metadata
    columns, and the persisted daily-budget + sync-state tables.

    >>> from snekql.sqlite import Config
    >>> database = await Database.initialize(backend=Config(database=":memory:"))
    >>> await create_youtube_schema(database)
    """
    await database.migrate(_youtube_migrations())
