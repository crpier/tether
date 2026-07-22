# Research: Wear OS capture tile — recording constraints, UX, and upload path

Resolves #248 (parent map ticket #183, blocks build ticket #192, which is blocked
on the shipped Android app #191/#225/#226/#227). Answers below are used to spec
#192 via `/to-spec`.

## Existing plumbing to reuse (`apps/capture-android`, from #191/#225)

The shipped Android client (`apps/capture-android/app/src/main/java/com/tether/capture/`):

- **`CaptureClient.kt`** — static request builders on OkHttp 4.12.0. Two calls
  relevant here:
  - `buildVoiceRequest(baseUrl, token, audio: File)` → `POST <host>/api/capture/voice`,
    multipart, part name `file`, `Content-Type: audio/mp4` (m4a container),
    `Authorization: Bearer <token>`. Response JSON has a `transcript` field
    (`CaptureClient.parseTranscript`).
  - Tight timeouts (connect 5s / write+read 30s / call 60s) — justified in-repo
    by "single-tenant local server... hangs surface fast" (see
    `CLAUDE.md` Performance characteristics). Same rationale applies on watch.
- **`MainActivity.kt`** — hold-to-record UX: `ACTION_DOWN` starts
  `MediaRecorder` (`AudioSource.MIC`, `OutputFormat.MPEG_4`, `AudioEncoder.AAC`)
  writing to `cacheDir`; `ACTION_UP`/`ACTION_CANCEL` stops, uploads, deletes the
  local file, shows the transcript in a Snackbar/Toast. Handles the
  "clip too short → `stop()` throws" case explicitly.
- **`SettingsRepository`** (DataStore/EncryptedSharedPreferences) — holds host
  base URL + bearer token, entered once via a Settings screen.
- **Auth**: host-side `TETHER_API_TOKEN`, constant-time-compared
  `Authorization: Bearer` header, checked in `AppSessionMiddleware`
  (`apps/host`, from #225 phase 1). No token-issuance flow — the same static
  token the phone app uses.
- **Manifest**: `minSdk 26`, `compileSdk`/`targetSdk 36`, `INTERNET` +
  `RECORD_AUDIO` permissions, `viewBinding`, no Compose.
- Server contract (`apps/host`, #225 phase 1): `POST /api/capture/voice`
  accepts m4a/ogg/wav ≤25MB, transcribes via an OpenAI-compatible
  `/audio/transcriptions` endpoint (Whisper-class), captures the transcript as
  a human-asserted memory with `source: voice` facet, and does **not** retain
  the audio file after transcription. A 192-era watch client should hit this
  exact endpoint — no new host work implied by this research.

This is the plumbing the recommendation below reuses as-is.

## 1. Tile constraints

Wear OS Tiles are ProtoLayout: declarative XML-like layout trees pushed from a
`TileService`, with **no arbitrary runtime code and no gesture handlers**.
Confirmed via official docs:

- A tile's only interactivity is `Clickable` bound to one of two actions:
  - **`LoadAction`** — re-invokes `TileService.onTileRequest()` to recompute
    tile content/state. Fastest possible response (no activity launch, no
    process-switch overhead) but **cannot start recording** — a `TileService`
    has no mic access and no UI to hold-and-release against.
  - **`LaunchAction`** — launches an `Activity` by `ComponentName`, optionally
    passing typed extras. This is the only way a tap gets to code that can run
    `MediaRecorder`.
- **Conclusion: no hold-to-record gesture can live on the tile.** A ProtoLayout
  tile cannot observe pointer-down/pointer-up, only a discrete "tap" click.
  The tile's only job is a single-tap `LaunchAction` into a recording activity;
  all the actual UX (hold-to-record, upload, feedback) has to happen in that
  activity, exactly like the phone app's `MainActivity`.
- **Fastest tap flow**: tap tile → `LaunchAction` → activity `onCreate` starts
  the recorder immediately (no extra confirmation tap) so the elapsed time is
  just system activity-launch latency, no user-visible intermediate screen.
  Docs don't quote a latency number; treat it as "one Android activity cold/warm
  start" — worth watching for on real Pixel Watch hardware but not tile-controllable.
- **Keyguard**: not separately documented for tiles; a `LaunchAction`-started
  activity is subject to the same keyguard/lock rules as any Wear OS activity
  launch (tiles are reachable from the always-visible tile carousel without
  unlocking on most watch lock configurations, but the launched activity can
  still be interrupted by a lock prompt depending on the user's watch lock
  settings — not something to engineer around, just note as a source of
  occasional flow interruption).

Sources: [Interact with tiles](https://developer.android.com/training/wearables/tiles/interactions), [Tiles overview](https://developer.android.com/training/wearables/tiles), [Ongoing activities](https://developer.android.com/design/ui/wear/guides/m2-5/behaviors-and-patterns/ongoing-activities)

## 2. Recording

- **API**: `MediaRecorder` — same API surface as phone Android, no Wear-specific
  variant. AudioRecord is lower-level (raw PCM) and unnecessary complexity here;
  the phone app already proved `MediaRecorder` → AAC/m4a is sufficient for
  Whisper-class server-side STT (#225's `/api/capture/voice` accepts m4a). Reuse
  the identical `AudioSource.MIC` / `OutputFormat.MPEG_4` / `AudioEncoder.AAC`
  config verbatim — no new format-support work needed host-side.
- **Permission UX**: identical `RECORD_AUDIO` runtime permission flow as phone
  (`ActivityResultContracts.RequestPermission`), first-use prompt. No
  watch-specific permission surface documented beyond the standard Android 6+
  runtime model.
- **Practical clip length**: not directly documented for Wear OS; general
  Android guidance (Android 9+/API 28) is that mic access requires the app to
  be foreground or running a foreground service with a persistent notification
  — background/screen-off access is blocked by default. For a "hold to record,
  release to upload" flow the activity is foreground for the whole clip, so
  this isn't a blocker as long as the user keeps the activity in front; there's
  no documented Wear-specific clip-length cap, but voice notes are inherently
  short (seconds), matching the existing phone app's assumption.
- **Screen-off / wrist-down**: recording does **not** survive the screen
  turning off or the watch going ambient/wrist-down without a foreground
  service — general Android mic-background restriction applies on Wear OS
  too. Since the intended UX is "hold physically while looking at the watch,"
  this is a non-issue for the target flow, but it does mean the recording
  activity must keep the screen/activity foregrounded for the duration (no
  design that expects recording to continue after the wearer drops their wrist).
- **Battery/thermal**: no Wear-specific numbers found beyond general platform
  guidance (avoid wake locks, let the CPU sleep between operations). Short
  voice-note clips (seconds) followed by immediate upload/delete — as the
  phone app already does — sidesteps sustained-recording battery concerns.

Sources: [Voice input | Wear OS](https://developer.android.com/training/wearables/user-input/voice), [MediaRecorder overview](https://developer.android.com/media/platform/mediarecorder), [Principles of Wear OS development](https://developer.android.com/training/wearables/principles)

## 3. Upload path

Two real options, evaluated against the existing `/api/capture/voice` endpoint
(bearer-token multipart HTTP):

### Option A — standalone HTTP from the watch (OkHttp, Wi-Fi/LTE)

- Identical code path to the phone app: reuse `CaptureClient.kt` almost
  verbatim (it's pure Kotlin/OkHttp, no Android-view dependency) in a Wear
  module.
  - **Pros**: dead simple, phone-absent reliability (works over watch Wi-Fi or
    LTE on cellular Pixel Watch models independent of phone proximity), no new
    transport code, no 100KB payload ceiling (HTTP body isn't capped like
    `MessageClient`), reuses host auth/endpoint as-is.
  - **Cons**: needs the bearer token *on the watch* — some distribution
    mechanism required (see below); needs a network path from the watch
    itself (Wi-Fi-only Pixel Watch models have no connectivity away from a
    known Wi-Fi network or paired/bridged phone; LTE models are fine
    standalone).
- **Token distribution**: no token-issuance flow exists in this system (v1
  is a static `TETHER_API_TOKEN`, works-for-me per #225). Getting it onto the
  watch has two practical options:
  1. Manual entry on the tiny watch keyboard/voice-to-text — poor UX but zero
     new code, consistent with the phone app's own "paste the static token"
     settings screen.
   2. Push it from the already-configured phone app via the Data Layer's
      `DataClient`/`MessageClient` (small payload, well under the 100KB cap)
      once, at watch-app install/pairing time — better UX, small amount of
      new code on both sides.
  Recommendation: ship v1 with manual entry (matches the phone app's existing
  precedent and needs no new host or phone-app code), leave DataClient sync as
  a fast-follow if manual entry proves painful in practice.

### Option B — Wearable Data Layer relay through the phone app

- `MessageClient`: **hard ~100KB payload cap** (confirmed:
  ["MessageClient... does not support data larger than 100 KB"](https://developer.android.com/training/wearables/data-layer/messages)).
  A several-second AAC voice clip can exceed this, so `MessageClient` alone is
  not viable for the audio payload itself (it's fine for tiny control
  messages, e.g. "start/stop", but not the file).
- `ChannelClient`: designed for exactly this — "reliably send a file too large
  for MessageClient... transfer streamed data such as voice data from the
  microphone" per the official docs. Streams the clip watch→phone, phone then
  re-uses its existing `CaptureClient`/token/network path to hit
  `/api/capture/voice`.
  - **Pros**: token never needs to exist on the watch — the already-configured
    phone app holds it. No manual watch-side token entry.
  - **Cons**: extra hop (watch → phone via Bluetooth/Data Layer → phone →
    host over Wi-Fi/LTE), added latency and a second point of failure; per
    Google's Data Layer docs, both `MessageClient` and `ChannelClient` "work
    offline: No" — **the phone must be present, paired, and reachable**, so
    this path has *worse* phone-absent reliability, not better, directly
    contradicting the ticket's phone-absent-reliability goal for a
    quick-capture tile. Also needs a `WearableListenerService` on the phone
    side (new code) and a `ChannelClient` sender on the watch side (new code).

### Recommendation: **Option A (standalone HTTP, manual token entry)**

Direct HTTP from the watch, reusing `CaptureClient.kt` verbatim, with the
bearer token entered once on the watch (mirroring the phone app's existing
settings screen). Rationale:

- The core value proposition of a watch tile is **quick capture without the
  phone**, so a transport that hard-depends on the phone being paired and
  reachable (Data Layer) undermines the goal. Google's own docs mark both
  Data Layer clients "offline: No."
- `/api/capture/voice` already exists, is bearer-authed, and accepts exactly
  the audio format the phone app already produces — zero host changes.
- `CaptureClient.kt` is pure Kotlin with no Android-view dependency, so it can
  be shared source (see project shape below) rather than reimplemented.
- Token-on-watch is the one new piece of friction, but it's a one-time manual
  setup, same UX debt the phone app already accepted for v1 (works-for-me,
  matches CLAUDE.md's "no token issuance flow" stance).

Background execution / WorkManager: not needed for v1 — the upload happens
synchronously while the recording activity is foreground (same pattern as the
phone app: record → immediately upload → toast/notify → done). Only worth
adding `WorkManager`-based retry if watch connectivity in the field proves
flaky enough to want offline queueing — treat as a fast-follow, not a v1
requirement.

Sources: [Choose a client type | Wear OS](https://developer.android.com/training/wearables/data-layer/messages), [Data Layer overview](https://developer.android.com/training/wearables/data/overview), [Principles of Wear OS development](https://developer.android.com/training/wearables/principles)

## 4. Project shape

- **Module, not separate project**: add a Wear OS Gradle module
  (`apps/capture-android/wear/`) inside the existing `apps/capture-android`
  Gradle project, sharing the root `settings.gradle`. This matches the ticket
  framing directly ("a small companion to the phone app, not a separate
  project", #192) and lets the Wear module depend on a shared Kotlin source
  set (or simply duplicate the small `CaptureClient.kt`/`SettingsRepository`
  files — the whole phone app is ~a few hundred LOC, low duplication cost) for
  request-building and settings storage.
- **Compose for Wear OS vs plain views**: the phone app uses plain
  `ViewBinding`, no Compose, "no Compose ceremony beyond necessity" per #225.
  For symmetry and lowest new-dependency footprint, prefer **plain Wear
  views/Activity** for the recording screen too — a single hold-to-record
  button doesn't need Compose's declarative UI machinery, and pulling in
  Compose for Wear OS only for this screen adds a large new dependency
  surface (Compose runtime + Wear Compose Material) for one button. Use
  `androidx.wear:wear` (classic Wear support views) if any Wear-specific chrome
  (e.g. `ConfirmationOverlay`, curved layouts) is wanted; otherwise a bare
  `Activity` with a full-screen button is sufficient — mirrors `MainActivity.kt`
  almost exactly.
- **Tile module**: a `TileService` subclass (ProtoLayout, `androidx.wear.tiles`
  / `androidx.wear.protolayout`) rendering one button with a `LaunchAction`
  pointing at the recording `Activity`'s `ComponentName`. This is unavoidably
  new code (no ProtoLayout exists in the repo yet) but is small — a handful of
  ProtoLayout DSL calls.
- **Min SDK / tooling**: current default AGP guidance moved Wear Tiles library
  minSdk from 21 to **23**; recent Wear Tiles/ProtoLayout releases raised
  **compileSdk to 35+**. The phone app already targets `minSdk 26` /
  `compileSdk 36`, comfortably above both floors — **no separate minSdk
  needed for the Wear module**, can match the phone module's existing
  26/36 to keep one consistent SDK story across the project. Confirm exact
  versions against the `androidx.wear.protolayout` / `androidx.wear.tiles`
  release notes at implementation time (they move independently of the phone
  API surface).
- **Horologist**: relevant pieces are `horologist-datalayer` (if Data Layer
  sync is added later, e.g. for token push) and the media/audio modules (not
  applicable — those are for media *playback*, not mic recording, so no use
  here). Not needed for the v1 standalone-HTTP recommendation; worth
  reconsidering only if a fast-follow adds phone-to-watch token sync over the
  Data Layer.

Sources: [Wear Tiles release notes](https://developer.android.com/jetpack/androidx/releases/wear-tiles), [Wear ProtoLayout release notes](https://developer.android.com/jetpack/androidx/releases/wear-protolayout), [Horologist](https://google.github.io/horologist/), [Get started with tiles](https://developer.android.com/training/wearables/tiles/get_started)

## Recommended architecture (summary)

| Concern | Decision |
|---|---|
| Tile role | Single-tap `LaunchAction` → recording `Activity`. No gesture logic on the tile itself (ProtoLayout can't do it). |
| Recording flow | Activity opens → immediately arms `MediaRecorder` (`MIC`/`MPEG_4`/`AAC`, identical config to the phone app) on hold; release stops and uploads. Foreground-only; no background/ambient recording. |
| Transport | Standalone HTTP from the watch straight to `POST /api/capture/voice`, reusing `CaptureClient.kt` verbatim. Rejected: Wearable Data Layer relay through the phone (offline:No on both `MessageClient`/`ChannelClient`, defeats phone-absent reliability; `MessageClient` also has a hard 100KB cap below typical clip size). |
| Auth | Same static bearer token model as the phone app; entered manually once on the watch in v1. Data-Layer-based token push from phone→watch is a viable fast-follow, not required for v1. |
| Project layout | New Wear Gradle module under `apps/capture-android/` (not a separate repo/project), plain Wear views (no Compose for Wear OS) for the recording activity, small new `TileService` for the tile itself. minSdk/compileSdk can match the phone module's existing 26/36. |
| Background execution | Not needed for v1 — record→upload is synchronous while foreground. WorkManager-based retry only as a later addition if field reliability demands it. |

## Open items for #192's spec

- Exact Pixel Watch 2/3 tap→activity latency isn't documented; validate on
  hardware once built, no way to bound it from docs alone.
- Manual-token-entry UX on a watch keyboard may prove bad enough in practice
  to warrant pulling forward the Data-Layer token-push fast-follow — worth a
  quick on-device gut-check before committing to it as launch UX.
