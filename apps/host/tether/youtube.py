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
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import ClassVar, Literal, Protocol, runtime_checkable
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
    insert,
    select,
    update,
)

from tether.events import EventPublisher, InvalidateEvent, NullEventPublisher
from tether.logging import Logger

type YouTubeSource = Literal["liked", "watch_later"]
"""Which saved list a video was ingested from."""

type IngestState = Literal["active", "ignored"]
"""Whether an ingested video is live in browse/search or purged from it."""


class Clock(Protocol):
    """A source of the current instant, injectable for controlled-clock tests."""

    def now(self) -> datetime:
        """Return the current time as an aware UTC datetime."""
        ...


class SystemClock:
    """The wall clock, in UTC."""

    def now(self) -> datetime:
        """Return the current UTC instant."""
        return datetime.now(UTC)


class YouTubeVideoNotFoundError(Exception):
    """Raised when an operation targets a video absent from ingestion."""


class YouTubeQuotaExceededError(Exception):
    """Raised when a live API call would exceed the day's remaining budget.

    The guard raises *before* calling out, so an exhausted budget never reaches
    the upstream API — the point of guarding quota/rate.
    """


class TranscriptUnavailableError(Exception):
    """Raised when the upstream API has no transcript for a video."""


class EmptyYouTubeSearchQueryError(Exception):
    """Raised when a keyword Search is asked to run on a blank query."""


class RawYouTubeVideo(BaseModel):
    """A video as the upstream API returns it, before local ingestion.

    The required fields are what a liked-list page yields; the optional fields
    are the richer metadata a batched detail fetch fills in (and the backup
    import carries across).

    >>> RawYouTubeVideo(video_id="v1", title="T", channel="C", topic="python").topic
    'python'
    """

    video_id: str
    title: str
    channel: str
    topic: str
    description: str = ""
    channel_id: str | None = None
    liked_at: datetime | None = None
    video_published_at: datetime | None = None
    duration_seconds: int | None = None
    category_id: str | None = None
    default_language: str | None = None
    default_audio_language: str | None = None
    caption_available: bool | None = None
    privacy_status: str | None = None
    licensed_content: bool | None = None
    made_for_kids: bool | None = None
    live_broadcast_content: str | None = None
    definition: str | None = None
    dimension: str | None = None
    statistics_view_count: int | None = None
    statistics_like_count: int | None = None
    statistics_comment_count: int | None = None
    statistics_fetched_at: datetime | None = None
    topic_categories: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    thumbnails: dict[str, str] = {}


class QuotaMeta(BaseModel):
    """The day's quota budget snapshot a guarded call reports.

    >>> QuotaMeta(limit=100, used=3, remaining=97).remaining
    97
    """

    limit: int
    used: int
    remaining: int


class CacheMeta(BaseModel):
    """Whether a result was served from the local cache or fetched live.

    >>> CacheMeta(hit=False, source="live").source
    'live'
    """

    hit: bool
    source: Literal["live", "cache"]


@dataclass(frozen=True, slots=True)
class LikedPage:
    """One page of liked videos plus the cursor to the next page."""

    videos: list[RawYouTubeVideo]
    next_page_token: str | None


@runtime_checkable
class YouTubeApi(Protocol):
    """The upstream YouTube surface ingestion depends on, **page at a time**.

    A structural interface (any object with these coroutines satisfies it), so
    tests inject `InMemoryYouTubeApi` and production injects a live OAuth client
    without a shared base class. The sync drives `list_liked_page` to control
    exactly how much it pulls per run, enriches via `fetch_video_metadata`, and
    pulls transcripts on demand.
    """

    async def list_liked_page(
        self, *, page_token: str | None, page_size: int
    ) -> LikedPage:
        """Return one page of the liked-videos list and the next-page cursor."""
        ...

    async def fetch_video_metadata(
        self, video_ids: Sequence[str]
    ) -> Mapping[str, RawYouTubeVideo]:
        """Return full metadata for each given video id, keyed by id."""
        ...

    async def fetch_transcript(self, video_id: str) -> str:
        """Return a video's transcript text, or raise `TranscriptUnavailableError`."""
        ...


class InMemoryYouTubeApi(YouTubeApi):
    """A seedable in-memory `YouTubeApi`: the concrete source and the test fake.

    Seeded with an ordered liked list (newest first), it serves fixed-size pages
    with synthetic cursors and counts its calls so tests can prove ingestion
    stays within budget. `fetch_video_metadata` returns the same seeded objects,
    standing in for the live detail call.

    >>> import asyncio
    >>> api = InMemoryYouTubeApi(transcripts={"v1": "hello"})
    >>> asyncio.run(api.fetch_transcript("v1"))
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
            v.video_id: v for v in self._liked if v.video_id not in unavailable_ids
        }
        self._transcripts: dict[str, str] = dict(transcripts or {})
        self.list_calls: int = 0
        self.metadata_calls: int = 0
        self.transcript_calls: int = 0

    async def list_liked_page(
        self, *, page_token: str | None, page_size: int
    ) -> LikedPage:
        self.list_calls += 1
        start = int(page_token) if page_token is not None else 0
        size = max(1, page_size)
        page = self._liked[start : start + size]
        next_start = start + size
        next_token = str(next_start) if next_start < len(self._liked) else None
        return LikedPage(videos=list(page), next_page_token=next_token)

    async def fetch_video_metadata(
        self, video_ids: Sequence[str]
    ) -> Mapping[str, RawYouTubeVideo]:
        self.metadata_calls += 1
        return {vid: self._by_id[vid] for vid in video_ids if vid in self._by_id}

    async def fetch_transcript(self, video_id: str) -> str:
        self.transcript_calls += 1
        try:
            return self._transcripts[video_id]
        except KeyError as error:
            raise TranscriptUnavailableError(video_id) from error


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


class YouTubeQuotaDaily[S = Pending](Model[S, "YouTubeQuotaDaily[Fetched]"]):
    """Units spent against the YouTube Data API on one UTC day.

    Keyed by the day, so the budget is remembered across restarts and a new day
    starts fresh with no row (treated as zero used).
    """

    day: YouTubeQuotaDaily.Col[str] = Text(primary_key=True)
    used: YouTubeQuotaDaily.Col[int] = Integer(default=0)


class YouTubeSyncState[S = Pending](Model[S, "YouTubeSyncState[Fetched]"]):
    """A small key/value store for ingestion bookkeeping (cursor, last-run)."""

    key: YouTubeSyncState.Col[str] = Text(primary_key=True)
    value: YouTubeSyncState.Col[str] = Text(nullable=False)


_BACKFILL_CURSOR_KEY = "likes_backfill_next_page_token"
_LIKES_LAST_RUN_KEY = "likes_last_run_at"


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
    """A search across saved content + transcripts, with the day's quota/cache."""

    videos: list[IngestedVideo[Fetched]]
    cache: CacheMeta
    quota: QuotaMeta


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


def _debug(logger: Logger, event: str, **context: object) -> None:
    logger.debug(event, **context)


def _info(logger: Logger, event: str, **context: object) -> None:
    logger.info(event, **context)


class DailyQuota:
    """A persisted, per-UTC-day spend-down budget of opaque quota units.

    `spend` raises before mutating when the day's remaining budget cannot cover
    the request, so a guarded call can treat a successful `spend` as permission
    to call out. Spend is stored in SQLite, so it survives restarts; a new UTC
    day starts with no row and therefore zero used.
    """

    def __init__(self, database: Database, *, limit: int) -> None:
        self.database: Database = database
        self.limit: int = limit

    @staticmethod
    def _day(now: datetime) -> str:
        return now.astimezone(UTC).date().isoformat()

    async def used(self, *, now: datetime) -> int:
        """Return units spent so far on the given instant's UTC day."""
        async with self.database.transaction() as tx:
            row = await self._row(tx, self._day(now))
        return row.used if row is not None else 0

    async def snapshot(self, *, now: datetime) -> QuotaMeta:
        """Report the day's budget as an envelope-ready value."""
        used = await self.used(now=now)
        return QuotaMeta(limit=self.limit, used=used, remaining=self.limit - used)

    async def spend(self, units: int, *, now: datetime) -> None:
        """Consume `units` on today's budget, or raise if it cannot cover them."""
        day = self._day(now)
        async with self.database.transaction() as tx:
            row = await self._row(tx, day)
            used = row.used if row is not None else 0
            if used + units > self.limit:
                message = (
                    f"quota exhausted for {day}: {self.limit - used} of {self.limit} "
                    f"units remain, {units} requested"
                )
                raise YouTubeQuotaExceededError(message)
            if row is None:
                _ = await tx.execute(insert(YouTubeQuotaDaily(day=day, used=units)))
            else:
                _ = await tx.execute(
                    update(YouTubeQuotaDaily)
                    .set(YouTubeQuotaDaily.used.to(used + units))
                    .where(YouTubeQuotaDaily.day.eq(day))
                )

    async def _row(
        self, tx: Transaction, day: str
    ) -> YouTubeQuotaDaily[Fetched] | None:
        return await tx.fetch_one_or_none(
            select(YouTubeQuotaDaily).where(YouTubeQuotaDaily.day.eq(day))
        )


class YouTubeApiClient:
    """The persisted-budget guard in front of a paginated `YouTubeApi`.

    Every live call spends from the `DailyQuota` (raising once the day is
    depleted, before calling out); each method returns the data plus the day's
    `QuotaMeta`, which the tool/REST seams put on the envelope.
    """

    # Per-call quota cost. The Data API charges one unit for each of
    # playlistItems.list, videos.list and a transcript fetch, so all are 1.
    _CALL_COST: ClassVar[int] = 1

    def __init__(
        self,
        api: YouTubeApi,
        quota: DailyQuota,
        *,
        clock: Clock | None = None,
    ) -> None:
        self._api: YouTubeApi = api
        self._quota: DailyQuota = quota
        self._clock: Clock = clock or SystemClock()

    def now(self) -> datetime:
        """Return the current instant from the shared clock.

        The sync service reads its bookkeeping time from here so spend and
        last-run timestamps come from one clock rather than diverging.
        """
        return self._clock.now()

    async def snapshot(self) -> QuotaMeta:
        """Report the day's remaining budget without spending."""
        return await self._quota.snapshot(now=self._clock.now())

    async def list_liked_page(
        self, *, page_token: str | None, page_size: int
    ) -> LikedPage:
        """Pull one liked-videos page, spending one guarded list unit."""
        await self._quota.spend(self._CALL_COST, now=self._clock.now())
        return await self._api.list_liked_page(
            page_token=page_token, page_size=page_size
        )

    async def fetch_video_metadata(
        self, video_ids: Sequence[str]
    ) -> Mapping[str, RawYouTubeVideo]:
        """Fetch batched metadata for the given ids, spending one guarded unit."""
        if not video_ids:
            return {}
        await self._quota.spend(self._CALL_COST, now=self._clock.now())
        return await self._api.fetch_video_metadata(video_ids)

    async def fetch_transcript(self, video_id: str) -> str:
        """Fetch a transcript, spending one guarded transcript unit."""
        await self._quota.spend(self._CALL_COST, now=self._clock.now())
        return await self._api.fetch_transcript(video_id)


def _json_or_none(values: Sequence[str] | Mapping[str, str]) -> str | None:
    """Encode a non-empty sequence/mapping as JSON, else None."""
    return json.dumps(values) if values else None


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
        .set(IngestedVideo.liked_at.to(raw.liked_at))
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
        self.event_publisher: EventPublisher = event_publisher or NullEventPublisher()

    async def sync(self, *, logger: Logger) -> SyncReport:
        """Run one idempotent ingestion pass: hot pages then backfill pages."""
        with self.tracer.start_as_current_span("YouTubeSyncService.sync"):
            _debug(logger, "YouTube sync starting")
            pulled = 0
            upserted = 0
            pages = 0
            backfill_exhausted = False
            quota_exhausted = False
            # Resume the persisted backfill cursor (the hot tail seeds it first run).
            cursor = await self._load_cursor()
            try:
                # Hot pages: always from the head of the liked list.
                hot_token: str | None = None
                for _ in range(self.hot_pages):
                    page = await self.client.list_liked_page(
                        page_token=hot_token, page_size=self.page_size
                    )
                    pages += 1
                    scoped, reached_cutoff = self._apply_cutoff(page.videos)
                    # Count the page as pulled only once `_mirror_page` returns;
                    # a quota stop mid-enrich must not overstate the report.
                    upserted += await self._mirror_page(scoped)
                    pulled += len(scoped)
                    hot_token = page.next_page_token
                    if hot_token is None or reached_cutoff:
                        break

                # Backfill: advance the cursor a little through history.
                if cursor is None:
                    cursor = hot_token
                for _ in range(self.backfill_pages):
                    if cursor is None:
                        backfill_exhausted = True
                        break
                    page = await self.client.list_liked_page(
                        page_token=cursor, page_size=self.page_size
                    )
                    pages += 1
                    scoped, hit_cutoff = self._apply_cutoff(page.videos)
                    upserted += await self._mirror_page(scoped)
                    pulled += len(scoped)
                    cursor = page.next_page_token
                    if hit_cutoff:
                        cursor = None
                    if cursor is None:
                        backfill_exhausted = True
                        break
            except YouTubeQuotaExceededError as error:
                # The day's budget is spent: stop calling out and resume next pass.
                quota_exhausted = True
                _debug(logger, "YouTube sync stopped on quota", error=str(error))
            await self._store_cursor(cursor)
            await self._mark_run()

        _info(
            logger,
            "YouTube sync completed",
            pulled=pulled,
            upserted=upserted,
            pages=pages,
            backfill_exhausted=backfill_exhausted,
            quota_exhausted=quota_exhausted,
        )
        if upserted:
            await self.event_publisher.publish(InvalidateEvent(keys=["youtube"]))
        return SyncReport(
            pulled=pulled,
            upserted=upserted,
            pages=pages,
            backfill_exhausted=backfill_exhausted,
        )

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
        upserted = 0
        async with self.database.transaction() as tx:
            for raw in videos:
                enriched = details.get(raw.video_id)
                if enriched is None:
                    continue
                await self._upsert(tx, enriched)
                upserted += 1
        return upserted

    async def _upsert(self, tx: Transaction, raw: RawYouTubeVideo) -> None:
        await upsert_ingested_video(tx, raw)

    async def _load_cursor(self) -> str | None:
        value = await _state_get(self.database, _BACKFILL_CURSOR_KEY)
        return value or None

    async def _store_cursor(self, cursor: str | None) -> None:
        # An exhausted cursor is stored as the empty string and reads back as
        # absent, so the next pass restarts the backfill from the hot tail.
        await _state_set(self.database, _BACKFILL_CURSOR_KEY, cursor or "")

    async def _mark_run(self) -> None:
        await _state_set(
            self.database, _LIKES_LAST_RUN_KEY, self.client.now().isoformat()
        )


async def _state_get(database: Database, key: str) -> str | None:
    async with database.transaction() as tx:
        row = await tx.fetch_one_or_none(
            select(YouTubeSyncState).where(YouTubeSyncState.key.eq(key))
        )
        return row.value if row is not None else None


async def _state_set(database: Database, key: str, value: str) -> None:
    async with database.transaction() as tx:
        existing = await tx.fetch_one_or_none(
            select(YouTubeSyncState).where(YouTubeSyncState.key.eq(key))
        )
        if existing is None:
            _ = await tx.execute(insert(YouTubeSyncState(key=key, value=value)))
        else:
            _ = await tx.execute(
                update(YouTubeSyncState)
                .set(YouTubeSyncState.value.to(value))
                .where(YouTubeSyncState.key.eq(key))
            )


class YouTubeService:
    """Capability surface for the local YouTube ingested corpus.

    Browse and Search read only `IngestedVideo` (instant, no quota). Transcript
    fetch is the one capability that still calls upstream — guarded by the daily
    budget and short-circuited once a transcript is stored. Each mutation owns
    its transaction.
    """

    def __init__(
        self,
        database: Database,
        client: YouTubeApiClient,
        tracer: Tracer,
        event_publisher: EventPublisher | None = None,
    ) -> None:
        self.database: Database = database
        self.client: YouTubeApiClient = client
        self.tracer: Tracer = tracer
        self.event_publisher: EventPublisher = event_publisher or NullEventPublisher()

    async def browse(
        self,
        *,
        topic: str | None = None,
        source: YouTubeSource | None = None,
        logger: Logger,
    ) -> BrowseResult:
        """List active ingested videos from the local corpus, newest-liked-first.

        Reads only local state — the background sync is what refreshes the
        corpus, so a browse never calls upstream and costs no quota.
        """
        with self.tracer.start_as_current_span("YouTubeService.browse"):
            _debug(logger, "Browsing YouTube ingestion", topic=topic, source=source)
            query = select(IngestedVideo).where(IngestedVideo.ignored_at.is_null())
            if source is not None:
                query = query.where(IngestedVideo.source.eq(source))
            if topic is not None:
                query = query.where(IngestedVideo.topic.like(topic))
            async with self.database.transaction() as tx:
                videos = await tx.fetch_all(
                    query.order_by(
                        IngestedVideo.liked_at.desc(), IngestedVideo.created_at.desc()
                    )
                )
        _debug(logger, "YouTube browse completed", result_count=len(videos))
        return BrowseResult(
            videos=videos,
            cache=CacheMeta(hit=True, source="cache"),
            quota=await self.client.snapshot(),
        )

    async def search(self, query: str, *, logger: Logger) -> SearchResult:
        """Keyword Search across saved content and transcript text (local only).

        Each whitespace term is matched case-insensitively against the title,
        description, or transcript and AND-ed; only active videos match.
        """
        terms = query.split()
        if not terms:
            message = "keyword Search requires a non-empty query"
            raise EmptyYouTubeSearchQueryError(message)
        _debug(logger, "Searching YouTube ingestion", terms_count=len(terms))
        statement = select(IngestedVideo).where(IngestedVideo.ignored_at.is_null())
        for term in terms:
            pattern = f"%{term}%"
            statement = statement.where(
                IngestedVideo.title.like(pattern)
                | IngestedVideo.description.like(pattern)
                | IngestedVideo.transcript.like(pattern)
            )
        async with self.database.transaction() as tx:
            videos = await tx.fetch_all(
                statement.order_by(
                    IngestedVideo.liked_at.desc(), IngestedVideo.created_at.desc()
                )
            )
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
        short-circuits with no upstream call; otherwise the fetch is guarded by
        the daily budget and the text is stored so Search can match it.
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
        text = await self.client.fetch_transcript(video_id)
        async with self.database.transaction() as tx:
            _ = await tx.execute(
                update(IngestedVideo)
                .set(IngestedVideo.transcript.to(text))
                .set(IngestedVideo.updated_at.to(CurrentTimestamp))
                .where(IngestedVideo.video_id.eq(video_id))
            )
            updated = await self._fetch(tx, video_id)
        _info(logger, "YouTube transcript fetched", video_id=video_id)
        await self.event_publisher.publish(InvalidateEvent(keys=["youtube"]))
        return TranscriptResult(
            video=updated,
            transcript=text,
            cache=CacheMeta(hit=False, source="live"),
            quota=await self.client.snapshot(),
        )

    async def ignore(self, video_id: str, *, logger: Logger) -> IngestedVideo[Fetched]:
        """Purge a video from ingestion so browse/search no longer surface it."""
        _debug(logger, "Ignoring YouTube video", video_id=video_id)
        async with self.database.transaction() as tx:
            _ = await self._fetch(tx, video_id)
            _ = await tx.execute(
                update(IngestedVideo)
                .set(IngestedVideo.ignored_at.to(CurrentTimestamp))
                .set(IngestedVideo.updated_at.to(CurrentTimestamp))
                .where(IngestedVideo.video_id.eq(video_id))
                .where(IngestedVideo.ignored_at.is_null())
            )
            video = await self._fetch(tx, video_id)
        _info(logger, "YouTube video ignored", video_id=video_id)
        await self.event_publisher.publish(InvalidateEvent(keys=["youtube"]))
        return video

    async def retry(self, video_id: str, *, logger: Logger) -> IngestedVideo[Fetched]:
        """Un-ignore a previously purged video, returning it to ingestion."""
        _debug(logger, "Retrying YouTube video", video_id=video_id)
        async with self.database.transaction() as tx:
            _ = await self._fetch(tx, video_id)
            _ = await tx.execute(
                update(IngestedVideo)
                .set(IngestedVideo.ignored_at.to(None))
                .set(IngestedVideo.updated_at.to(CurrentTimestamp))
                .where(IngestedVideo.video_id.eq(video_id))
            )
            video = await self._fetch(tx, video_id)
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
