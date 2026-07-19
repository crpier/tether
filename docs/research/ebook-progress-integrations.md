# Ebook reading-progress integrations — research (issue #194)

Survey of options for getting book/ebook reading progress and finished-book
events into Tether. Constraints: single user, self-hosting friendly, Android
devices, Python host that polls or receives webhooks. Readwise highlights are
already planned separately (issue #197). Research as of 2026-07.

## 1. KOReader — kosync progress sync (self-hosted sync server)

- **What data**: per-document furthest reading progress — `percentage`
  (0.0–1.0), `progress` (XPath position or page), `device` name, `device_id`,
  `timestamp`. Document is identified by an MD5 hash of the file (or, with
  "filename" matching mode, a hash of the filename) — no title/author metadata
  crosses the wire, so Tether must map hashes to books itself.
- **Requires**: KOReader (runs on Android — fits the user's devices — plus
  Kobo/Kindle-jailbroken/e-ink readers) and a kosync-compatible server. The
  official [koreader/koreader-sync-server](https://github.com/koreader/koreader-sync-server)
  is OpenResty + Lua + Redis, Dockerized, actively maintained (v2.1.1, May
  2026).
- **API shape**: tiny REST/JSON API — `POST /users/create`,
  `GET /users/auth`, `PUT /syncs/progress` (device pushes progress),
  `GET /syncs/progress/:document`. Auth via `x-auth-user` / `x-auth-key`
  (MD5 of password) headers. The protocol is simple and widely reimplemented
  (Komga, BookLore, KoInsight, Readest all speak it).
- **Reliability**: protocol stable for ~a decade; KOReader syncs on
  open/close/page-turn (configurable). Push is device-initiated; no webhooks.
- **Integration effort — low**: rather than polling someone else's server,
  Tether can *be* the kosync server: implement the 4 endpoints in the Python
  host and receive progress pushes directly. ~1–2 days including
  hash→book-title mapping (KOReader can also be set to sync by filename hash,
  which makes mapping trivial). Finished events: derive from
  `percentage >= ~0.98` crossing a threshold, or from statistics (below).

## 2. KOReader — statistics plugin (reading sessions)

- **What data**: `statistics.sqlite` on-device DB with rich data: `book`
  table (title, authors, pages, total read time, highlights count) and
  `page_stat_data` (per-page reading events: page, start_time, duration).
  Gives real session stats, reading speed, and a solid "finished" signal.
  KOReader itself marks books finished ("Set book status: finished"), stored
  in the per-book sidecar `metadata.epub.lua`, not in the sync protocol.
- **Requires**: getting the sqlite file (or its rows) off the device.
  Options: (a) [KoInsight](https://github.com/GeorgeSG/KoInsight) — TS/Docker
  dashboard with a KOReader Lua plugin that pushes stats to the server, and it
  doubles as a kosync server; active (v0.2.2, Jan 2026) but young, and no
  documented outbound API — you'd read its DB or fork the plugin. (b) Point a
  forked/patched plugin at Tether directly. (c) Periodic file sync
  (Syncthing) of `statistics.sqlite` + Tether parses it. Server-side stats
  sync is a still-open KOReader FR
  ([koreader#15182](https://github.com/koreader/koreader/issues/15182)).
- **API shape**: sqlite file with a stable, well-known schema; KoInsight's
  plugin posts JSON.
- **Reliability**: schema stable for years; the plugin ecosystem
  (KoInsight, BookOrbit) is newer and lower-bus-factor.
- **Integration effort — medium**: Syncthing-the-sqlite-file + a Tether
  parser is ~1–2 days and very robust; adopting/forking the KoInsight plugin
  to POST to Tether is ~2–4 days.

## 3. Kobo devices — native sync protocol (calibre-web / Komga / kobink style)

- **What data**: Kobo's own sync API reports reading state per book —
  current position, percent read, finished status. Self-hosted servers that
  reimplement it (calibre-web `cps/kobo.py`, Calibre-Web-Automated, Komga,
  BookLore, kobink) receive `PUT .../v1/library/<book-id>/state` from the
  device with progress + `ReadingState`/finished.
- **Requires**: an actual Kobo e-reader with `api_endpoint` redirected in
  its config file to the self-hosted server. Not applicable to Android-only
  reading. Alternative: KOReader installed on the Kobo (option 1/2), or
  parsing the on-device `KoboReader.sqlite` over USB (manual).
- **API shape**: REST/JSON (reverse-engineered Kobo store protocol); or piggyback:
  run calibre-web with Kobo sync enabled and have Tether poll calibre-web's
  DB (`book_read_link` / kobo reading state tables).
- **Reliability**: reverse-engineered; occasionally breaks on Kobo firmware
  updates, but calibre-web/CWA track it actively.
- **Integration effort — medium-high**, and moot unless a Kobo device is in
  the picture. If one ever is, easiest path is calibre-web + Tether polling
  its DB (~1–2 days), not reimplementing the protocol.

## 4. Kindle — locked down; Goodreads as proxy

- **Kindle direct**: no official API. Unofficial clients scrape
  `read.amazon.com` private endpoints
  ([Xetera/kindle-api](https://github.com/Xetera/kindle-api),
  [transitive-bullshit/kindle-api](https://github.com/transitive-bullshit/kindle-api),
  Python [Lector](https://github.com/msuozzo/Lector) — stale). They need
  session cookies harvested from a logged-in browser, and since 2023 must
  defeat Amazon TLS fingerprinting via a proxy tls-client. Fragile, ToS-risky,
  high maintenance. Data when it works: library, current position (kindle
  location), % read.
- **Goodreads proxy**: the official Goodreads API was shut down (2020; last
  grandfathered keys died ~Dec 2025). No API path. What remains: per-user
  **RSS feeds** (updates/shelf feeds, still public as of mid-2026, but
  undocumented and removable at any will) and profile scraping. Kindle's
  built-in Goodreads integration posts "X is 45% done with…" updates and
  "finished" shelf moves, so polling the RSS gives coarse progress + reliable
  finished events — if the user actually uses Kindle+Goodreads.
- **Requires**: Kindle hardware/app + Goodreads account with updates public
  (or scraping while logged in).
- **Integration effort**: RSS polling is low (~1 day) but brittle and
  coarse; direct Kindle scraping is high (days, plus ongoing breakage).
  Recommend avoiding unless Kindle is the primary reader.

## 5. Readwise Reader (overlaps issue #197)

- **What data**: `GET https://readwise.io/api/v3/list/` returns documents
  with `reading_progress` (0–1), `location` (`new`/`later`/`shortlist`/
  `archive` — archive ≈ finished), `category` (includes `epub` and `pdf`),
  `first_opened_at`/`last_opened_at`, `updated_at`. Supports `updatedAfter`
  for cheap incremental polling. No session-level stats.
- **Requires**: Readwise subscription, API token; must actually *read the
  epub in Reader* (Android app exists) for progress to populate. Books read
  in KOReader/Kobo/Kindle won't appear.
- **API shape**: clean documented REST, token auth, 20 req/min. Officially
  maintained SaaS — most reliable API in this list, but not self-hosted.
- **Overlap with #197**: same token and adjacent API (v2 highlights vs v3
  documents). If #197 lands, adding a `reading_progress`/`location` poll for
  `category in (epub, pdf)` is nearly free.
- **Integration effort — low** (~0.5–1 day on top of #197). Main caveat is
  behavioral: it only covers books read inside Reader.

## 6. calibre-based flows

- **What data**: calibre itself stores library metadata, not live progress.
  Progress enters calibre via companion pieces:
  - [koreader-calibre-plugin](https://github.com/harmtemolder/koreader-calibre-plugin):
    pulls percent-read + location from KOReader sidecars or a kosync server
    into calibre custom columns.
  - calibre-web / Calibre-Web-Automated Kobo sync (option 3) writes progress
    into its own DB.
- **Requires**: a running calibre or calibre-web instance; desktop-centric.
  Tether would poll `metadata.db` (sqlite, well-documented schema) or
  calibre-web's app DB.
- **Reliability**: calibre is extremely stable; the plugins are
  community-maintained and healthy.
- **Effort — medium**: it's an indirection — calibre only knows what KOReader
  or Kobo sync told it. For Tether it adds a hop with no extra data; only
  worth it if a calibre library becomes the canonical book catalog.

## Summary table

| Option | Progress % | Position | Finished event | Session stats | Highlights | Self-hosted | Interface | Effort | Fit (Android, single user) |
|---|---|---|---|---|---|---|---|---|---|
| KOReader kosync (Tether as server) | Yes | Yes (xpath/page) | Derived (≥98%) | No | No | Yes | 4-endpoint REST, device-push | Low | **Excellent** |
| KOReader statistics.sqlite | Yes | Per-page | Yes (status/sidecar) | Yes (rich) | Count only | Yes | sqlite file / KoInsight JSON | Medium | **Very good** |
| Kobo native sync (calibre-web etc.) | Yes | Yes | Yes | Partial | Via device DB | Yes | Reverse-engineered REST | Med-high | Poor (needs Kobo hardware) |
| Kindle scraping | Yes | Kindle loc | Weak | No | Via clippings | No | Cookie-auth private API | High + fragile | Poor |
| Goodreads RSS (Kindle proxy) | Coarse | No | Yes | No | No | No | RSS scrape, undocumented | Low but brittle | Weak |
| Readwise Reader API | Yes | No | archive location | No | Yes (#197) | No (SaaS) | Documented REST + token | Low | Good (Reader-read books only) |
| calibre / calibre-web polling | Yes (relayed) | Yes | Yes | No | No | Yes | sqlite polling | Medium | OK, redundant hop |

## Recommended shortlist

1. **KOReader + Tether-as-kosync-server** — primary. KOReader runs on the
   user's Android devices, the protocol is 4 trivial endpoints, data is
   pushed to Tether in near-real-time, fully self-hosted, no third party.
   Derive finished events at a percentage threshold. Lowest effort for the
   highest-quality progress signal.
2. **KOReader statistics.sqlite ingestion** — follow-up layer on top of #1
   for reading-session stats (time read, sessions/day) and an explicit
   finished status. Start with Syncthing + a Tether parser; revisit
   KoInsight's plugin if server-push is wanted.
3. **Readwise Reader progress poll** — cheap rider on the planned #197
   integration; covers epubs/PDFs read in Reader, `updatedAfter` polling,
   `location=archive` as finished. Complementary, not primary.

Not recommended for now: Kindle scraping (fragile, ToS risk), Goodreads RSS
(coarse, unofficial, only useful if Kindle becomes primary), Kobo native sync
(no Kobo hardware), calibre hop (adds no data Tether can't get directly).

## Sources

- https://github.com/koreader/koreader-sync-server
- https://github.com/koreader/koreader/issues/15182
- https://github.com/GeorgeSG/KoInsight
- https://komga.org/docs/guides/koreader/ and https://komga.org/docs/guides/kobo/
- https://github.com/crocodilestick/Calibre-Web-Automated/wiki/Kobo-Integration-&-Sync
- https://github.com/janeczku/calibre-web/issues/2036
- https://github.com/potatoeggy/kobink
- https://github.com/harmtemolder/koreader-calibre-plugin
- https://github.com/Xetera/kindle-api, https://github.com/transitive-bullshit/kindle-api, https://github.com/msuozzo/Lector
- https://readwise.io/reader_api
- https://www.goodreads.com/topic/show/21788520-api-deprecation
