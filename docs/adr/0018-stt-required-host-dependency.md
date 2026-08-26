# STT is a required host dependency

STT (speech-to-text) was introduced off-by-default in the capture app v1 spec (#225), configured behind an `stt_enabled` flag with `stt_api_key` optional. Web voice input (#19) and the voice-as-chat pivot (#239) now make voice a first-class input path across the product — the web composer gets two voice buttons, and the existing Voice capture endpoint (spec #225) is set to be rewired onto the same chat path. A maybe-configured capability forces hidden/disabled UI states and 503 error paths for something that is no longer optional, and pushes conditional complexity into every voice surface for a case that shouldn't exist.

We remove the `stt_enabled` config flag. `stt_api_key` becomes a required host boot setting: the host fails fast and refuses to start if it is unconfigured. Dev, test, and CI harnesses (`just dev`, host test fixtures, `just validate-web-smoke`) are updated to supply dummy STT env vars so they keep working without a real STT credential.

## Consequences

- The host no longer boots at all without STT configured — a behavior change from today's off-by-default posture.
- `just dev`, host test fixtures, and `just validate-web-smoke` need dummy STT env vars added so local dev, tests, and the smoke gate keep passing without a real STT provider credential.
- The web UI can assume voice is always available: no hidden/disabled voice buttons, no "STT not configured" error state to design or handle.
- This reverses part of #225's original off-by-default decision for STT specifically; other capture-app-v1 stances from that spec are unaffected.
