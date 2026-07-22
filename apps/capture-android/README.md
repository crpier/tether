# Tether Capture (Android)

A deliberately dumb Android client for capturing into a running Tether host.
Two Gradle modules ship from here:

- **`app`** — the phone client. It does two things and nothing else:
  1. **Share-target** — share plain text or a URL from any app into "Tether
     Capture"; it `POST`s the text to `<host>/api/memories` and toasts the
     result.
  2. **Hold-to-record voice note** — the single button on the main screen
     records while held (m4a via `MediaRecorder`); on release it uploads the
     clip to `<host>/api/capture/voice`, shows the returned transcript, and
     deletes the local file.
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

Grant the microphone permission when first recording a voice note.

## What it deliberately does not do

- No image/screenshot share ingestion (text/URL only).
- No streaming STT, wake words, or on-device transcription.
- No token issuance/login flow — you paste the static host token.
- No retained audio: voice clips are deleted after a successful upload.
- No background sync, notifications beyond the capture toast/snackbar, or offline
  queue. If the host is unreachable, the capture simply reports failure.
- Not part of the repo's JS/Python validation gate; verified via
  `gradle assembleDebug` + lint + JVM unit tests only.

See [`wear/README.md`](./wear/README.md) for the watch companion's own scope
and setup.
