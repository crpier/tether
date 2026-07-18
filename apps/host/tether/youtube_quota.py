"""The YouTube Data API budget/backoff trio: `DailyQuota`, `YouTubeApiGate`,
`YouTubeApiClient`, and the small persisted key/value store they (and the
transcript-provider pause state) share.

Split out of `youtube.py` (#203) because this trio is self-contained: it does
not depend on the ingestion/sync/browse concerns that make up the rest of that
file. `youtube.py` re-exports every public name here so existing call sites
(and the live-API wiring in `server.py`) keep importing from `tether.youtube`
unchanged.
"""

from __future__ import annotations

from collections.abc import Awaitable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import ClassVar, Protocol, runtime_checkable

from pydantic import BaseModel
from snekql.sqlite import (
    Database,
    Fetched,
    Integer,
    Model,
    Pending,
    Text,
    Transaction,
    insert,
    select,
    update,
)

from tether.db_retry import run_in_transaction
from tether.escalating_pause import PauseKeys, PersistentEscalatingPause


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


class YouTubeQuotaExceededError(Exception):
    """Raised when a live API call would exceed the day's remaining budget.

    The guard raises *before* calling out, so an exhausted budget never reaches
    the upstream API — the point of guarding quota/rate.
    """


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


@dataclass(frozen=True, slots=True)
class LikedPage:
    """One page of liked videos, the next-page cursor, and the playlist size.

    `total_results` is the upstream `pageInfo.totalResults` for the whole liked
    playlist (not just this page); the sync compares it against the local corpus
    to detect drift. It is `None` when the source does not report a total.
    """

    videos: list[RawYouTubeVideo]
    next_page_token: str | None
    total_results: int | None = None


@runtime_checkable
class YouTubeApi(Protocol):
    """The upstream YouTube surface ingestion depends on, **page at a time**.

    A structural interface (any object with these coroutines satisfies it), so
    tests inject `InMemoryYouTubeApi` and production injects a live OAuth client
    without a shared base class. The sync drives `list_liked_page` to control
    exactly how much it pulls per run and enriches via `fetch_video_metadata`.
    Transcripts are a separate concern, fetched through the `TranscriptProvider`
    port rather than this list/metadata surface.
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


class QuotaMeta(BaseModel):
    """The day's quota budget snapshot a guarded call reports.

    >>> QuotaMeta(limit=100, used=3, remaining=97).remaining
    97
    """

    limit: int
    used: int
    remaining: int


class YouTubeSyncState[S = Pending](Model[S, "YouTubeSyncState[Fetched]"]):
    """A small key/value store for ingestion bookkeeping (cursor, last-run)."""

    key: YouTubeSyncState.Col[str] = Text(primary_key=True)
    value: YouTubeSyncState.Col[str] = Text(nullable=False)


class YouTubeQuotaDaily[S = Pending](Model[S, "YouTubeQuotaDaily[Fetched]"]):
    """Units spent against the YouTube Data API on one UTC day.

    Keyed by the day, so the budget is remembered across restarts and a new day
    starts fresh with no row (treated as zero used).
    """

    day: YouTubeQuotaDaily.Col[str] = Text(primary_key=True)
    used: YouTubeQuotaDaily.Col[int] = Integer(default=0)


async def state_get(database: Database, key: str) -> str | None:
    async with database.transaction() as tx:
        row = await tx.fetch_one_or_none(
            select(YouTubeSyncState).where(YouTubeSyncState.key.eq(key))
        )
        return row.value if row is not None else None


async def state_set(database: Database, key: str, value: str) -> None:
    async def _set(tx: Transaction) -> None:
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

    await run_in_transaction(database, _set)


# The shared, global Data API backoff gate, persisted so a live quota block
# survives restarts: the instant any Data API call may be tried again, and the
# consecutive-error streak its cooldown escalates with. One pair of keys gates
# every live call (the metadata sync and the transcript budget alike).
_API_GATE_PAUSED_UNTIL_KEY = "youtube_api_paused_until"
_API_GATE_STREAK_KEY = "youtube_api_block_streak"


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

        async def _spend(tx: Transaction) -> None:
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

        await run_in_transaction(self.database, _spend)

    async def _row(
        self, tx: Transaction, day: str
    ) -> YouTubeQuotaDaily[Fetched] | None:
        return await tx.fetch_one_or_none(
            select(YouTubeQuotaDaily).where(YouTubeQuotaDaily.day.eq(day))
        )


@dataclass(frozen=True, slots=True)
class YouTubeApiGateConfig:
    """Bounds for the shared YouTube Data API backoff gate.

    A live `403 quotaExceeded` (or other rate signal mapped to the quota error)
    escalates a *global* pause that gates every Data API call — the metadata sync
    and the transcript budget alike. The cooldown grows exponentially in the
    consecutive-error streak, clamped to `pause_cap`, and is raised to any
    upstream-supplied retry-after hint. Capped at six hours so a throttled host
    stops hammering an already-exhausted quota (which is what keeps it from
    refreshing) without parking ingestion for a whole day.
    """

    pause_base: timedelta = timedelta(minutes=15)
    pause_cap: timedelta = timedelta(hours=6)


class YouTubeApiGate:
    """A persisted, global exponential backoff in front of live Data API calls.

    `DailyQuota` models Google's budget to pre-empt it, but the live API can still
    return `403 quotaExceeded` (clock skew, or budget spent from elsewhere). Blindly
    retrying then hammers an already-spent quota and can keep it from refreshing.
    This gate reacts to that signal: a live quota error pauses *all* Data API calls
    for an escalating cooldown (capped at six hours), and the first clean call clears
    it. Because the pause is checked *before* the upstream call, a paused gate never
    reaches YouTube.
    """

    def __init__(
        self,
        database: Database,
        *,
        config: YouTubeApiGateConfig | None = None,
    ) -> None:
        gate_config = config or YouTubeApiGateConfig()
        self._pause: PersistentEscalatingPause = PersistentEscalatingPause(
            base=gate_config.pause_base,
            cap=gate_config.pause_cap,
            keys=PauseKeys(
                paused_until=_API_GATE_PAUSED_UNTIL_KEY, streak=_API_GATE_STREAK_KEY
            ),
            read_value=partial(state_get, database),
            write_value=partial(state_set, database),
        )

    async def ensure_open(self, *, now: datetime) -> None:
        """Raise `YouTubeQuotaExceededError` while the global pause is in effect."""
        state = await self._pause.load()
        if state.paused_until is not None and state.paused_until > now:
            when = state.paused_until.isoformat()
            message = (
                f"YouTube API paused until {when} (quota-error streak {state.streak})"
            )
            raise YouTubeQuotaExceededError(message)

    async def record_success(self) -> None:
        """Clear any standing pause and reset the streak after a clean live call.

        Read-only in the steady state: only writes when there is prior pause/streak
        to clear, so the common success path stays cheap.
        """
        await self._pause.clear()

    async def record_quota_error(
        self, *, now: datetime, retry_after: timedelta | None = None
    ) -> datetime:
        """Escalate the global pause after a live quota error; return `paused_until`.

        The cooldown is `pause_base * 2**(streak-1)` clamped to `pause_cap`, then
        raised to any provider-supplied `retry_after` hint.
        """
        return (await self._pause.trip(now=now, retry_after=retry_after)).paused_until

    async def paused_until(self, *, now: datetime) -> datetime | None:
        """The instant the global pause lifts, or None when the gate is open.

        Read-only; the status surface uses it to report *why* a sync is stalled
        without itself touching the upstream API.
        """
        state = await self._pause.load()
        if state.paused_until is not None and state.paused_until > now:
            return state.paused_until
        return None


class YouTubeApiClient:
    """The persisted-budget guard in front of a paginated `YouTubeApi`.

    Every live call spends from the `DailyQuota` (raising once the day is
    depleted, before calling out) and passes through the shared `YouTubeApiGate`
    (raising while a live quota block is in its global cooldown, before calling
    out); each method returns the data plus the day's `QuotaMeta`, which the
    tool/REST seams put on the envelope.
    """

    # Per-call quota cost. The Data API charges one unit for each of
    # playlistItems.list and videos.list, so both are 1.
    _CALL_COST: ClassVar[int] = 1
    # A transcript fetch (captions.list + captions.download) is charged as one
    # budgeted unit here; the worker and on-demand path both spend it before
    # calling the provider, so an exhausted budget never reaches the provider.
    _TRANSCRIPT_COST: ClassVar[int] = 1

    def __init__(
        self,
        api: YouTubeApi,
        quota: DailyQuota,
        *,
        clock: Clock | None = None,
        gate: YouTubeApiGate | None = None,
    ) -> None:
        self._api: YouTubeApi = api
        self._quota: DailyQuota = quota
        self._clock: Clock = clock or SystemClock()
        # One gate guards every live call, so a quota block tripped by the metadata
        # sync also pauses transcript spend (and vice versa).
        self._gate: YouTubeApiGate = gate or YouTubeApiGate(quota.database)

    def now(self) -> datetime:
        """Return the current instant from the shared clock.

        The sync service reads its bookkeeping time from here so spend and
        last-run timestamps come from one clock rather than diverging.
        """
        return self._clock.now()

    async def snapshot(self) -> QuotaMeta:
        """Report the day's remaining budget without spending."""
        return await self._quota.snapshot(now=self._clock.now())

    async def api_paused_until(self, *, now: datetime) -> datetime | None:
        """When the shared Data API gate's pause lifts, or None while it is open."""
        return await self._gate.paused_until(now=now)

    async def list_liked_page(
        self, *, page_token: str | None, page_size: int
    ) -> LikedPage:
        """Pull one liked-videos page, spending one guarded list unit."""
        now = self._clock.now()
        await self._gate.ensure_open(now=now)
        await self._quota.spend(self._CALL_COST, now=now)
        return await self._guarded(
            self._api.list_liked_page(page_token=page_token, page_size=page_size),
            now=now,
        )

    async def fetch_video_metadata(
        self, video_ids: Sequence[str]
    ) -> Mapping[str, RawYouTubeVideo]:
        """Fetch batched metadata for the given ids, spending one guarded unit."""
        if not video_ids:
            return {}
        now = self._clock.now()
        await self._gate.ensure_open(now=now)
        await self._quota.spend(self._CALL_COST, now=now)
        return await self._guarded(self._api.fetch_video_metadata(video_ids), now=now)

    async def charge_transcript(self) -> None:
        """Spend one guarded transcript unit, or raise if the day is exhausted.

        The transcript text itself comes from the `TranscriptProvider`; this only
        guards the budget so a depleted day stops before the provider is called.
        Passes through the shared gate, so a live quota block (tripped by the
        metadata sync) pauses transcript spend too.
        """
        now = self._clock.now()
        await self._gate.ensure_open(now=now)
        await self._quota.spend(self._TRANSCRIPT_COST, now=now)

    async def _guarded[T](self, call: Awaitable[T], *, now: datetime) -> T:
        """Run a live upstream call, escalating the gate on a quota error.

        A `YouTubeQuotaExceededError` from the upstream call is a *live* block (the
        Data API 403'd despite our local budget): escalate the global pause and
        re-raise. Any clean call clears a standing pause. The local-budget spend
        runs before this, so its own quota error never reaches here to escalate.
        """
        try:
            result = await call
        except YouTubeQuotaExceededError:
            _ = await self._gate.record_quota_error(now=now)
            raise
        await self._gate.record_success()
        return result
