# Tether Capture (Android)

A deliberately dumb Android client for capturing into a running Tether host. It
does two things and nothing else:

1. **Share-target** — share plain text or a URL from any app into "Tether
   Capture"; it `POST`s the text to `<host>/api/memories` and toasts the result.
2. **Hold-to-record voice note** — the single button on the main screen records
   while held (m4a via `MediaRecorder`); on release it uploads the clip to
   `<host>/api/capture/voice`, shows the returned transcript, and deletes the
   local file.

Both endpoints authenticate with `Authorization: Bearer <token>`, where the
token is the host's `TETHER_API_TOKEN` (phase 1, PR #226).

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
just android-build          # gradle assembleDebug
```

or directly in this directory:

```sh
gradle assembleDebug        # or ./gradlew assembleDebug if a wrapper is present
gradle lintDebug            # lint
gradle testDebugUnitTest    # JVM unit tests
```

The debug APK lands at:

```
app/build/outputs/apk/debug/app-debug.apk
```

## Install

```sh
adb install -r app/build/outputs/apk/debug/app-debug.apk
```

## Configure

Open the app, tap **Settings**, and enter:

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
