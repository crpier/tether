# Tether Capture (Android)

A deliberately dumb Android client for capturing into a running Tether host.
Two Gradle modules ship from here:

- **`app`** — the phone client:
  1. **Share-target** — shares text or URLs to `<host>/api/memories`.
  2. **Hold-to-record voice capture** — uploads m4a audio to
     `<host>/api/capture/voice`, then deletes the local file.
  3. **Health Connect ingestion gate** — after explicit permission, syncs
     granted Health Connect records to the host immediately and about every six
     hours. Health data is read and transferred, never analyzed on the phone.
- **`wear`** — a Wear OS companion: a tile (single tap) launches a
  hold-to-record screen that mirrors `app`'s voice-note flow and uploads
  directly to the same host, independent of the phone. See
  [`wear/README.md`](./wear/README.md).

Both request the same two endpoints, sharing request-building/parsing code via
the plain-Kotlin **`core`** module. Both authenticate with
`Authorization: Bearer <token>`, where the token is the host's
`TETHER_API_TOKEN` (phase 1, PR #226).

## Prerequisites

- Android SDK with platform **android-36** and build-tools **36.1.0**.
- A JDK compatible with Android Gradle Plugin 8.13 (JDK 17–21). The build pins
  `sourceCompatibility`/`jvmTarget` to 17.
- **`local.properties`** pointing the build at your SDK. It is machine-specific
  and git-ignored — create it yourself:

  ```properties
  sdk.dir=/absolute/path/to/Android/Sdk
  ```

- If your default `java` is too new for AGP (e.g. JDK 24+), run Gradle with a
  17–21 JDK, for example by exporting `JAVA_HOME` or adding
  `org.gradle.java.home=/path/to/jdk17` to a machine-local `gradle.properties`
  (do not commit that line).

## Build

From the repo root:

```sh
just android-build          # assembles app + wear, runs core's JVM tests
```

or directly in this directory:

```sh
gradle :app:assembleDebug :wear:assembleDebug  # both APKs
gradle :app:lintDebug :wear:lintDebug          # lint
gradle :core:test                              # shared-module JVM unit tests
```

The debug APKs land at:

```
app/build/outputs/apk/debug/app-debug.apk
wear/build/outputs/apk/debug/wear-debug.apk
```

## Install

```sh
adb install -r app/build/outputs/apk/debug/app-debug.apk
adb install -r wear/build/outputs/apk/debug/wear-debug.apk   # to a paired watch's ADB
```

## Configure

(`app`, the phone client.) Open the app, tap **Settings**, and enter:

- **Host base URL** — e.g. `https://tether.example.com` or `http://10.0.0.5:8000`
  (no trailing `/api`; the client appends the paths itself).
- **API token** — the value of the host's `TETHER_API_TOKEN`.

Grant the microphone permission when first recording a voice capture.

## Health Connect

Health Connect must be available (built into Android 14+, or installed on a
supported earlier Android release). In **Settings → Health Connect**:

1. Tap **Enable / grant**.
2. Grant the Health Connect categories you want Tether to sync. Tether requests
   every readable record type in the pinned SDK; denied categories are shown but
   do not block granted categories. History and background-read access are
   requested where the provider supports them.
3. Permission grant queues an immediate baseline when at least one readable
   category is granted. **Sync now** queues another unique sync without racing an
   active run.

Without history access, the authoritative baseline is limited to Health
Connect's normally accessible recent window. Missing background/history access
is shown in Settings. Unsupported devices retain text-share and voice capture.
The section also shows running state, last success, and a sanitized last failure;
raw values, notes, and opaque cursor tokens are never displayed or logged.

The host must include the Health Connect API from
[`docs/health-connect-wire-v3.md`](../../docs/health-connect-wire-v3.md).

### Manual worker validation

With the phone attached:

```sh
adb shell dumpsys jobscheduler | grep -A20 com.tether.capture
# Find the WorkManager JobScheduler id, then force it:
adb shell cmd jobscheduler run -f com.tether.capture <job-id>
```

Return to Settings to verify **Last success** or the actionable failure. For a
fresh baseline, revoke then grant the desired Health Connect categories, or
clear app data and configure the host again. Clearing app data creates a new
installation id.

Troubleshooting:

- **Install or update Health Connect**: open the Health Connect settings/store
  flow from **Enable / grant**.
- **Health permissions changed**: grant at least one readable Health Connect
  category again.
- **Host unavailable**: verify host URL, bearer token, network, and the host's
  telemetry API.
- A worker that cannot reach the host retries without acknowledging its page.

## What it deliberately does not do

- No image/screenshot share ingestion (text/URL only).
- No streaming STT, wake words, or on-device transcription.
- No token issuance/login flow — you paste the static host token.
- No retained audio: voice clips are deleted after a successful upload.
- No health interpretation, charts, alerts, aggregation, or Health Connect writes.
- No capture offline queue; failed text/voice captures report failure directly.
- Android verification uses phone/wear assemble, lint, and JVM unit tests.

See [`wear/README.md`](./wear/README.md) for the watch companion's own scope
and setup.
