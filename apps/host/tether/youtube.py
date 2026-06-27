"""Concrete YouTube ingestion: browse, search, transcripts, ignore/retry.

This is built **concretely** rather than as a general integration framework —
with so few external sources, an abstraction would cost more than it saves. The
external surface is a small `YouTubeApi` protocol; the only implementation today
is `InMemoryYouTubeApi`, a seedable in-memory source that doubles as the test
fake. (A live OAuth-backed client is deferred — and the YouTube Data API does
not even expose the Watch Later playlist, so the real boundary is necessarily a
seam we own.)

Three layers stack here:

* `YouTubeApiClient` wraps the raw API and is the **quota/rate guard**. Every
  live call spends from a fixed budget and a depleted budget raises rather than
  calling out; identical reads are served from an in-process cache (no quota
  spend), and each result carries `CacheMeta` + `QuotaMeta` so the tool seam can
  surface them in the response envelope.
* `IngestedVideo` is the local mirror of a browsed video. Browsing pulls the
  liked / watch-later lists through the client and **upserts** them, preserving
  any locally fetched transcript and any local ignore — so an ignored video
  stays ignored even though it is still upstream, and `retry` un-ignores it.
* `YouTubeService` is the capability surface the tool and REST layers call.

>>> api = InMemoryYouTubeApi(liked=[RawYouTubeVideo(
...     video_id="v1", title="Async Python", channel="PyConf", topic="python")])
>>> client = YouTubeApiClient(api, quota_limit=100)
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import ClassVar, Literal, Protocol, runtime_checkable
from uuid import uuid7

from opentelemetry.trace import Tracer
from pydantic import UUID7, BaseModel
from snekql.sqlite import (
    CurrentTimestamp,
    Database,
    Fetched,
    Index,
    Model,
    Pending,
    Text,
    Transaction,
    insert,
    select,
    update,
)
from snekql.sqlite._schema_ddl import scaffold_sqlite_statements

from tether.events import EventPublisher, InvalidateEvent, NullEventPublisher
from tether.logging import Logger

type YouTubeSource = Literal["liked", "watch_later"]
"""Which saved list a video was ingested from."""

type IngestState = Literal["active", "ignored"]
"""Whether an ingested video is live in browse/search or purged from it."""

_ALL_SOURCES: tuple[YouTubeSource, ...] = ("liked", "watch_later")


class YouTubeVideoNotFoundError(Exception):
    """Raised when an operation targets a video absent from ingestion."""


class YouTubeQuotaExceededError(Exception):
    """Raised when a live API call would exceed the remaining quota budget.

    The guard raises *before* calling out, so an exhausted budget never reaches
    the upstream API — the point of guarding quota/rate.
    """


class TranscriptUnavailableError(Exception):
    """Raised when the upstream API has no transcript for a video."""


class EmptyYouTubeSearchQueryError(Exception):
    """Raised when a keyword Search is asked to run on a blank query."""


class RawYouTubeVideo(BaseModel):
    """A video as the upstream API returns it, before local ingestion.

    >>> RawYouTubeVideo(video_id="v1", title="T", channel="C", topic="python").topic
    'python'
    """

    video_id: str
    title: str
    channel: str
    topic: str
    description: str = ""


class QuotaMeta(BaseModel):
    """The quota budget snapshot a guarded call reports.

    >>> QuotaMeta(limit=100, used=3, remaining=97).remaining
    97
    """

    limit: int
    used: int
    remaining: int


class CacheMeta(BaseModel):
    """Whether a result was served from cache or fetched live.

    >>> CacheMeta(hit=False, source="live").source
    'live'
    """

    hit: bool
    source: Literal["live", "cache"]


def _merge_cache(parts: Sequence[CacheMeta]) -> CacheMeta:
    """Summarise several call caches: a result is cached only if all parts were.

    Browsing can touch more than one source list; the aggregate is a cache hit
    only when every underlying read was, otherwise at least one live call was
    made.
    """
    hit = bool(parts) and all(part.hit for part in parts)
    return CacheMeta(hit=hit, source="cache" if hit else "live")


@dataclass(slots=True)
class _QuotaGuard:
    """A spend-down budget of opaque quota units shared across a client.

    `spend` raises before mutating when the budget cannot cover the request, so
    a guarded call can treat a successful `spend` as permission to call out.
    """

    limit: int
    used: int = 0

    def spend(self, units: int) -> None:
        """Consume `units`, or raise if the remaining budget cannot cover them."""
        if self.used + units > self.limit:
            message = (
                f"quota exhausted: {self.limit - self.used} of {self.limit} units "
                f"remain, {units} requested"
            )
            raise YouTubeQuotaExceededError(message)
        self.used += units

    def snapshot(self) -> QuotaMeta:
        """Report the current budget as an envelope-ready value."""
        return QuotaMeta(
            limit=self.limit, used=self.used, remaining=self.limit - self.used
        )


@dataclass(frozen=True, slots=True)
class CachedResult[T]:
    """A client result paired with its cache and quota metadata."""

    value: T
    cache: CacheMeta
    quota: QuotaMeta


@runtime_checkable
class YouTubeApi(Protocol):
    """The upstream YouTube surface ingestion depends on.

    A structural interface (any object with these coroutines satisfies it), so
    tests inject `InMemoryYouTubeApi` and production injects a live client
    without a shared base class.
    """

    async def list_source(self, source: YouTubeSource) -> Sequence[RawYouTubeVideo]:
        """Return the videos currently in a saved list (liked / watch-later)."""
        ...

    async def fetch_transcript(self, video_id: str) -> str:
        """Return a video's transcript text, or raise `TranscriptUnavailableError`."""
        ...


class InMemoryYouTubeApi(YouTubeApi):
    """A seedable in-memory `YouTubeApi`: the concrete source and the test fake.

    Counts its calls so tests can prove ingestion never goes live more than
    expected (and that caching elides repeat calls).

    >>> import asyncio
    >>> api = InMemoryYouTubeApi(transcripts={"v1": "hello"})
    >>> asyncio.run(api.fetch_transcript("v1"))
    'hello'
    """

    def __init__(
        self,
        *,
        liked: Sequence[RawYouTubeVideo] = (),
        watch_later: Sequence[RawYouTubeVideo] = (),
        transcripts: Mapping[str, str] | None = None,
    ) -> None:
        self._sources: dict[YouTubeSource, list[RawYouTubeVideo]] = {
            "liked": list(liked),
            "watch_later": list(watch_later),
        }
        self._transcripts: dict[str, str] = dict(transcripts or {})
        self.list_calls: int = 0
        self.transcript_calls: int = 0

    async def list_source(self, source: YouTubeSource) -> Sequence[RawYouTubeVideo]:
        self.list_calls += 1
        return list(self._sources.get(source, []))

    async def fetch_transcript(self, video_id: str) -> str:
        self.transcript_calls += 1
        try:
            return self._transcripts[video_id]
        except KeyError as error:
            raise TranscriptUnavailableError(video_id) from error


class YouTubeApiClient:
    """The quota/rate guard and cache in front of a `YouTubeApi`.

    A live call spends from a fixed budget (raising once depleted, before
    calling out); a repeated read is served from cache without spending. Every
    method returns the data plus the `CacheMeta`/`QuotaMeta` the tool seam puts
    on the envelope.
    """

    def __init__(
        self,
        api: YouTubeApi,
        *,
        quota_limit: int,
        list_cost: int = 1,
        transcript_cost: int = 1,
    ) -> None:
        self._api: YouTubeApi = api
        self._guard: _QuotaGuard = _QuotaGuard(limit=quota_limit)
        self._list_cost: int = list_cost
        self._transcript_cost: int = transcript_cost
        self._source_cache: dict[YouTubeSource, list[RawYouTubeVideo]] = {}
        self._transcript_cache: dict[str, str] = {}

    async def list_source(
        self, source: YouTubeSource
    ) -> CachedResult[list[RawYouTubeVideo]]:
        """List a saved source, from cache when warm, else one guarded call."""
        cached = self._source_cache.get(source)
        if cached is not None:
            return CachedResult(
                value=list(cached),
                cache=CacheMeta(hit=True, source="cache"),
                quota=self._guard.snapshot(),
            )
        self._guard.spend(self._list_cost)
        videos = list(await self._api.list_source(source))
        self._source_cache[source] = videos
        return CachedResult(
            value=list(videos),
            cache=CacheMeta(hit=False, source="live"),
            quota=self._guard.snapshot(),
        )

    async def fetch_transcript(self, video_id: str) -> CachedResult[str]:
        """Fetch a transcript, from cache when warm, else one guarded call."""
        cached = self._transcript_cache.get(video_id)
        if cached is not None:
            return CachedResult(
                value=cached,
                cache=CacheMeta(hit=True, source="cache"),
                quota=self._guard.snapshot(),
            )
        self._guard.spend(self._transcript_cost)
        text = await self._api.fetch_transcript(video_id)
        self._transcript_cache[video_id] = text
        return CachedResult(
            value=text,
            cache=CacheMeta(hit=False, source="live"),
            quota=self._guard.snapshot(),
        )


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
    created_at: IngestedVideo.GenCol[datetime] = Text(default=CurrentTimestamp)
    updated_at: IngestedVideo.GenCol[datetime] = Text(default=CurrentTimestamp)

    __indexes__: ClassVar = [Index(topic)]


def derive_ingest_state(video: IngestedVideo[Fetched]) -> IngestState:
    """Derive whether a video is live in ingestion or purged from it."""
    return "ignored" if video.ignored_at is not None else "active"


@dataclass(frozen=True, slots=True)
class BrowseResult:
    """A topic-filtered browse: the live videos plus the call's quota/cache."""

    videos: list[IngestedVideo[Fetched]]
    cache: CacheMeta
    quota: QuotaMeta


@dataclass(frozen=True, slots=True)
class SearchResult:
    """A search across saved content + transcripts, with quota/cache."""

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


def _debug(logger: Logger, event: str, **context: object) -> None:
    logger.debug(event, **context)


def _info(logger: Logger, event: str, **context: object) -> None:
    logger.info(event, **context)


@dataclass(frozen=True, slots=True)
class _Pulled:
    """The aggregate of pulling one or more source lists through the client."""

    videos_by_source: list[tuple[YouTubeSource, list[RawYouTubeVideo]]]
    caches: list[CacheMeta]
    quota: QuotaMeta


class YouTubeService:
    """Capability surface for YouTube ingestion over snekql + the guarded client.

    Browse and Search pull the saved lists through the client (cached, so repeat
    reads cost no quota) and mirror them into `IngestedVideo`, then read back the
    local rows; the mirror preserves locally fetched transcripts and local
    ignores. Each mutation owns its transaction.
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

    async def _pull(self, sources: Sequence[YouTubeSource]) -> _Pulled:
        """Pull the given source lists through the guarded client."""
        videos_by_source: list[tuple[YouTubeSource, list[RawYouTubeVideo]]] = []
        caches: list[CacheMeta] = []
        quota = QuotaMeta(limit=0, used=0, remaining=0)
        for source in sources:
            result = await self.client.list_source(source)
            videos_by_source.append((source, result.value))
            caches.append(result.cache)
            quota = result.quota
        return _Pulled(videos_by_source=videos_by_source, caches=caches, quota=quota)

    async def _mirror(self, tx: Transaction, pulled: _Pulled) -> None:
        """Upsert pulled videos, preserving local transcript + ignore state."""
        for source, videos in pulled.videos_by_source:
            for raw in videos:
                existing = await tx.fetch_one_or_none(
                    select(IngestedVideo).where(IngestedVideo.video_id.eq(raw.video_id))
                )
                if existing is None:
                    _ = await tx.execute(
                        insert(
                            IngestedVideo(
                                video_id=raw.video_id,
                                source=source,
                                title=raw.title,
                                channel=raw.channel,
                                topic=raw.topic,
                                description=raw.description,
                            )
                        )
                    )
                    continue
                _ = await tx.execute(
                    update(IngestedVideo)
                    .set(IngestedVideo.source.to(source))
                    .set(IngestedVideo.title.to(raw.title))
                    .set(IngestedVideo.channel.to(raw.channel))
                    .set(IngestedVideo.topic.to(raw.topic))
                    .set(IngestedVideo.description.to(raw.description))
                    .set(IngestedVideo.updated_at.to(CurrentTimestamp))
                    .where(IngestedVideo.video_id.eq(raw.video_id))
                )

    async def browse(
        self,
        *,
        topic: str | None = None,
        source: YouTubeSource | None = None,
        logger: Logger,
    ) -> BrowseResult:
        """List active ingested videos, optionally filtered by topic and source.

        Pulls the relevant saved lists through the guarded client and mirrors
        them, then returns the active (non-ignored) rows newest-first. The
        client cache means repeated browses cost no further quota.
        """
        sources: tuple[YouTubeSource, ...] = (source,) if source else _ALL_SOURCES
        with self.tracer.start_as_current_span("YouTubeService.browse"):
            _debug(logger, "Browsing YouTube ingestion", topic=topic, source=source)
            pulled = await self._pull(sources)
            query = select(IngestedVideo).where(IngestedVideo.ignored_at.is_null())
            if source is not None:
                query = query.where(IngestedVideo.source.eq(source))
            if topic is not None:
                query = query.where(IngestedVideo.topic.like(topic))
            async with self.database.transaction() as tx:
                await self._mirror(tx, pulled)
                videos = await tx.fetch_all(
                    query.order_by(IngestedVideo.created_at.desc())
                )
        _debug(logger, "YouTube browse completed", result_count=len(videos))
        return BrowseResult(
            videos=videos, cache=_merge_cache(pulled.caches), quota=pulled.quota
        )

    async def search(
        self,
        query: str,
        *,
        logger: Logger,
    ) -> SearchResult:
        """Keyword Search across saved content and transcript text.

        Each whitespace term is matched case-insensitively against the title,
        description, or transcript and AND-ed; only active videos match, ordered
        newest-first. Ingestion is refreshed through the cached client first so
        newly saved videos are searchable.
        """
        terms = query.split()
        if not terms:
            message = "keyword Search requires a non-empty query"
            raise EmptyYouTubeSearchQueryError(message)
        _debug(logger, "Searching YouTube ingestion", terms_count=len(terms))
        pulled = await self._pull(_ALL_SOURCES)
        statement = select(IngestedVideo).where(IngestedVideo.ignored_at.is_null())
        for term in terms:
            pattern = f"%{term}%"
            statement = statement.where(
                IngestedVideo.title.like(pattern)
                | IngestedVideo.description.like(pattern)
                | IngestedVideo.transcript.like(pattern)
            )
        async with self.database.transaction() as tx:
            await self._mirror(tx, pulled)
            videos = await tx.fetch_all(
                statement.order_by(IngestedVideo.created_at.desc())
            )
        _debug(logger, "YouTube search completed", result_count=len(videos))
        return SearchResult(
            videos=videos, cache=_merge_cache(pulled.caches), quota=pulled.quota
        )

    async def fetch_transcript(
        self,
        video_id: str,
        *,
        logger: Logger,
    ) -> TranscriptResult:
        """Fetch and persist a transcript for an ingested video.

        The video must already be ingested (browse first). The fetch is guarded
        and cached by the client; the text is stored on the row so Search can
        match it thereafter.
        """
        _debug(logger, "Fetching YouTube transcript", video_id=video_id)
        await self._require_video(video_id)
        result = await self.client.fetch_transcript(video_id)
        async with self.database.transaction() as tx:
            _ = await tx.execute(
                update(IngestedVideo)
                .set(IngestedVideo.transcript.to(result.value))
                .set(IngestedVideo.updated_at.to(CurrentTimestamp))
                .where(IngestedVideo.video_id.eq(video_id))
            )
            video = await self._fetch(tx, video_id)
        _info(logger, "YouTube transcript fetched", video_id=video_id)
        await self.event_publisher.publish(InvalidateEvent(keys=["youtube"]))
        return TranscriptResult(
            video=video,
            transcript=result.value,
            cache=result.cache,
            quota=result.quota,
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
        """Fetch one ingested video by its upstream id, or raise when absent.

        A read-only lookup other capabilities (e.g. starting Recall) use to read
        a video's stored transcript and metadata without going back upstream.
        """
        async with self.database.transaction() as tx:
            return await self._fetch(tx, video_id)

    async def _require_video(self, video_id: str) -> None:
        """Raise if no ingested video carries `video_id` (a read-only check)."""
        async with self.database.transaction() as tx:
            _ = await self._fetch(tx, video_id)

    async def _fetch(self, tx: Transaction, video_id: str) -> IngestedVideo[Fetched]:
        """Fetch an ingested video by its upstream id or raise."""
        video = await tx.fetch_one_or_none(
            select(IngestedVideo).where(IngestedVideo.video_id.eq(video_id))
        )
        if video is None:
            raise YouTubeVideoNotFoundError(video_id)
        return video


async def create_youtube_schema(database: Database) -> None:
    """Create the ingested-video table and its index on an initialized database.

    Applied as its own migrations after the Memory, Bucket item, and
    Conversation schemas (prefix `004_`). The table carries a topic index, so
    scaffolding emits two statements (table, then index); each becomes its own
    ordered migration.
    """
    migrations = {
        f"004_{label}": sql
        for label, sql in scaffold_sqlite_statements([IngestedVideo])
    }
    await database.migrate(migrations)
