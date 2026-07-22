# Tether Capture — Wear OS

A dumb capture tile for the wrist. One swipe to the tile, one tap, hold to
record, release to upload — no phone required.

- **Tile** (`CaptureTileService`) — ProtoLayout tiles have no gesture
  handlers, so the tile's only job is a single tappable surface with a
  `LaunchAction` into the recording activity.
- **Recording activity** (`RecordingActivity`) — mirrors `app`'s
  `MainActivity`: press-down starts a `MediaRecorder` (m4a), release stops and
  uploads to `<host>/api/capture/voice` via the shared `core` client. Stops
  the mic immediately if the activity loses foreground mid-hold (a
  notification, ambient mode, a keyguard prompt) rather than waiting for a
  touch-up event that may never arrive.
- **Settings** (`WearSettingsActivity`) — host base URL + API token, entered
  on the watch keyboard and persisted via DataStore, independent of the phone
  app's settings. No token push from phone to watch in v1 (see spec #192).

## Build / install

From the repo root: `just android-build` (builds `app` + `wear`, runs `core`'s
JVM tests). Directly: `gradle :wear:assembleDebug` /
`gradle :wear:lintDebug`. The debug APK lands at
`wear/build/outputs/apk/debug/wear-debug.apk`; install to a paired watch's ADB
(`adb -s <watch-serial> install -r ...`) or push over Wi-Fi ADB.

## What it deliberately does not do

- No background/offline retry queue (WorkManager fast-follow, not v1).
- No token sync from the phone app (manual entry, same UX debt the phone app
  already accepted).
- No complications, watch-face integration, or ongoing-activity chips.
- No emulator/instrumented tests, no CI — build-checked (`just android-build`)
  and manually validated on hardware.

## Known-pending on-device validation

Not exercised by the headless build; the developer should check on real
Pixel Watch hardware before relying on this:

- Tap→activity latency from the tile.
- Upload over Bluetooth proxy (phone paired) and over watch Wi-Fi/LTE
  (phone absent).
- Behavior when launching the recording activity over a locked watch.
