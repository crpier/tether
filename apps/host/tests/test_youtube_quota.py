"""Behaviour tests for the YouTube Data API budget/backoff trio.

`DailyQuota`, `YouTubeApiGate`, and `YouTubeApiClient` moved to
`tether.youtube_quota` (#203) because they are self-contained: unlike the rest
of the YouTube ingestion surface, they never touch `IngestedVideo` or the sync
bookkeeping. These tests moved with them.
"""

from datetime import UTC, datetime, timedelta

from snekql.sqlite import Config, Database
from snektest import assert_eq, assert_raises, test

from tether.youtube import RawYouTubeVideo, create_youtube_schema
from tether.youtube_quota import (
    DailyQuota,
    LikedPage,
    YouTubeApi,
    YouTubeApiClient,
    YouTubeApiGate,
    YouTubeApiGateConfig,
    YouTubeQuotaExceededError,
)


class FakeClock:
    """A controllable clock so quota-rollover tests need not wait for midnight."""

    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        self._now += delta


async def quota_db() -> Database:
    """A fresh schema-initialised database for a standalone quota/gate/client."""
    db = await Database.initialize(backend=Config(database=":memory:"))
    await create_youtube_schema(db)
    return db


# --- DailyQuota: persistence, exhaustion, rollover ---


@test()
async def quota_persists_spend_across_instances() -> None:
    """A fresh DailyQuota over the same database sees prior spend."""
    db = await quota_db()
    quota = DailyQuota(db, limit=100)
    now = datetime(2026, 6, 1, tzinfo=UTC)
    await quota.spend(7, now=now)

    reopened = DailyQuota(db, limit=100)

    assert_eq(await reopened.used(now=now), 7)
    await db.close()


@test()
async def quota_raises_before_calling_out_when_exhausted() -> None:
    """A depleted day guards the upstream call rather than overspending."""
    db = await quota_db()
    quota = DailyQuota(db, limit=3)
    now = datetime(2026, 6, 1, tzinfo=UTC)
    await quota.spend(3, now=now)

    with assert_raises(YouTubeQuotaExceededError):
        await quota.spend(1, now=now)
    await db.close()


@test()
async def quota_rolls_over_at_the_next_utc_day() -> None:
    """Spend resets on a new UTC day so sync resumes automatically."""
    db = await quota_db()
    quota = DailyQuota(db, limit=5)
    day_one = datetime(2026, 6, 1, 23, 0, tzinfo=UTC)
    await quota.spend(5, now=day_one)

    day_two = day_one + timedelta(hours=2)

    assert_eq(await quota.used(now=day_two), 0)
    await quota.spend(5, now=day_two)
    assert_eq(await quota.used(now=day_two), 5)
    await db.close()


# --- YouTubeApiGate: global Data API backoff ---


_GATE_NOW = datetime(2026, 6, 1, tzinfo=UTC)


def gate_config() -> YouTubeApiGateConfig:
    """A 15-minute base / 6-hour cap gate, matching the production defaults."""
    return YouTubeApiGateConfig(
        pause_base=timedelta(minutes=15), pause_cap=timedelta(hours=6)
    )


class QuotaBlockingApi(YouTubeApi):
    """A `YouTubeApi` double that 403s on its first `fail_times` list calls.

    Stands in for the live Data API returning `quotaExceeded` despite local budget,
    so the gate's escalation/reset can be driven through `YouTubeApiClient`.
    """

    def __init__(self, *, fail_times: int) -> None:
        self._remaining: int = fail_times
        self.list_calls: int = 0

    async def list_liked_page(
        self, *, page_token: str | None, page_size: int
    ) -> LikedPage:
        _ = (page_token, page_size)
        self.list_calls += 1
        if self._remaining > 0:
            self._remaining -= 1
            raise YouTubeQuotaExceededError("live 403 quotaExceeded")
        return LikedPage(videos=[], next_page_token=None)

    async def fetch_video_metadata(
        self, video_ids: object
    ) -> dict[str, RawYouTubeVideo]:
        _ = video_ids
        return {}


@test()
async def api_gate_is_open_when_unpaused() -> None:
    """A fresh gate lets calls through without raising."""
    db = await quota_db()
    gate = YouTubeApiGate(db, config=gate_config())

    await gate.ensure_open(now=_GATE_NOW)

    await db.close()


@test()
async def api_gate_pauses_then_reopens_when_cooldown_elapses() -> None:
    """A quota error pauses for the base interval, reopening once it elapses."""
    db = await quota_db()
    gate = YouTubeApiGate(db, config=gate_config())

    paused_until = await gate.record_quota_error(now=_GATE_NOW)

    assert_eq(paused_until, _GATE_NOW + timedelta(minutes=15))
    with assert_raises(YouTubeQuotaExceededError):
        await gate.ensure_open(now=_GATE_NOW + timedelta(minutes=14))
    await gate.ensure_open(now=_GATE_NOW + timedelta(minutes=16))
    await db.close()


@test()
async def api_gate_success_clears_pause_and_resets_streak() -> None:
    """A clean call clears the pause, so the next error starts from the base."""
    db = await quota_db()
    gate = YouTubeApiGate(db, config=gate_config())
    _ = await gate.record_quota_error(now=_GATE_NOW)
    _ = await gate.record_quota_error(now=_GATE_NOW)

    await gate.record_success()

    await gate.ensure_open(now=_GATE_NOW)
    reset = await gate.record_quota_error(now=_GATE_NOW)
    assert_eq(reset - _GATE_NOW, timedelta(minutes=15))
    await db.close()


@test()
async def api_gate_pause_persists_across_instances() -> None:
    """A standing pause survives a restart (a fresh gate over the same db)."""
    db = await quota_db()
    await YouTubeApiGate(db, config=gate_config()).record_quota_error(now=_GATE_NOW)

    reopened = YouTubeApiGate(db, config=gate_config())

    with assert_raises(YouTubeQuotaExceededError):
        await reopened.ensure_open(now=_GATE_NOW + timedelta(minutes=10))
    await db.close()


@test()
async def client_live_quota_error_pauses_every_call() -> None:
    """A live 403 escalates the gate, so later calls short-circuit before YouTube."""
    db = await quota_db()
    clock = FakeClock(_GATE_NOW)
    api = QuotaBlockingApi(fail_times=99)
    client = YouTubeApiClient(
        api,
        DailyQuota(db, limit=1000),
        clock=clock,
        gate=YouTubeApiGate(db, config=gate_config()),
    )

    with assert_raises(YouTubeQuotaExceededError):
        _ = await client.list_liked_page(page_token=None, page_size=2)

    # The live call happened exactly once; the gate now closed, so the next list
    # and the (shared) transcript spend both raise before reaching upstream.
    assert_eq(api.list_calls, 1)
    with assert_raises(YouTubeQuotaExceededError):
        _ = await client.list_liked_page(page_token=None, page_size=2)
    assert_eq(api.list_calls, 1)
    with assert_raises(YouTubeQuotaExceededError):
        await client.charge_transcript()
    await db.close()


@test()
async def client_clears_gate_after_a_recovered_call() -> None:
    """Once the cooldown elapses, a successful call reopens the gate for all spend."""
    db = await quota_db()
    clock = FakeClock(_GATE_NOW)
    api = QuotaBlockingApi(fail_times=1)
    client = YouTubeApiClient(
        api,
        DailyQuota(db, limit=1000),
        clock=clock,
        gate=YouTubeApiGate(db, config=gate_config()),
    )
    with assert_raises(YouTubeQuotaExceededError):
        _ = await client.list_liked_page(page_token=None, page_size=2)

    clock.advance(timedelta(minutes=16))
    page = await client.list_liked_page(page_token=None, page_size=2)

    assert_eq(page.next_page_token, None)
    # The gate is clear: the shared transcript spend now passes too.
    await client.charge_transcript()
    await db.close()
