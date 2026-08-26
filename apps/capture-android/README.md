# Tether Capture (Android)

An Android Health Connect client for a running Tether host. After explicit
permission, the phone syncs granted Health Connect records immediately and about
every six hours. Health data is read and transferred, never analyzed on the
phone.

The **`app`** module contains the Android integration and the plain-Kotlin
**`core`** module contains the synchronization and host HTTP logic. Requests use
the configured host origin unchanged and authenticate with
`Authorization: Bearer <token>`, where the token is the host's
`TETHER_API_TOKEN`.

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
cd apps/capture-android
JAVA_HOME=/path/to/jdk17 ./gradlew :app:assembleDebug :app:testDebugUnitTest :core:test
```

Lint separately with:

```sh
JAVA_HOME=/path/to/jdk17 ./gradlew :app:lintDebug
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

Open the app and enter:

- **Host base URL** — e.g. `https://tether.example.com` or `http://10.0.0.5:8000`
  (no trailing `/api`; the client appends the paths itself).
- **API token** — the value of the host's `TETHER_API_TOKEN`.

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
is shown in Settings. The section also shows running state, last success, and a
sanitized last failure; raw values, notes, and opaque cursor tokens are never
displayed or logged.

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

- No Android share-target capture.
- No phone or Wear OS voice capture.
- No Wear OS tile or companion APK.
- No token issuance/login flow — you paste the static host token.
- No health interpretation, charts, alerts, aggregation, or Health Connect writes.
- Android verification uses phone assemble, lint, and app/core unit tests.
