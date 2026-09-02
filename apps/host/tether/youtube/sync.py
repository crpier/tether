"""YouTube liked-video synchronization policy and progress state."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import cast

from opentelemetry.trace import Tracer
from snekql.sqlite import Database, DoUpdate, Transaction, insert, select

from tether.events import EventPublisher, InvalidateEvent, NullEventPublisher
from tether.structured_logging import Logger
from tether.youtube.quota import (
    RawYouTubeVideo,
    YouTubeApiClient,
    YouTubeQuotaExceededError,
    YouTubeSyncState,
    state_get,
    state_set,
)
from tether.youtube.store import IngestedVideo, upsert_ingested_video


def _debug(logger: Logger, event: str, **context: object) -> None:
    logger.debug(event, **context)


def _info(logger: Logger, event: str, **context: object) -> None:
    logger.info(event, **context)


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


async def read_last_youtube_sync_at(database: Database) -> datetime | None:
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
        return await read_last_youtube_sync_at(self.database)

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

        async with self.database.transaction(mode="immediate") as tx:
            return await _mirror(tx)

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
        _ = await tx.execute(
            insert(
                YouTubeSyncState(key=_KNOWN_SKIPPED_IDS_KEY, value=value)
            ).on_conflict(
                YouTubeSyncState.key,
                action=DoUpdate(YouTubeSyncState.value.to_inserted()),
            )
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
