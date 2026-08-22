# TTS is a required host dependency

Conversation mode initially used the browser Web Speech API. Mobile browsers usually had usable system voices, while desktop Chromium could expose `speechSynthesis` with no voices and fail every playback attempt. Browser and operating-system voices also produced different results across clients.

Tether now generates spoken replies through one host-configured, OpenAI-compatible text-to-speech provider. `tts_api_key` is required at host boot, independently from the required STT credential. Deployments may use different providers for transcription and speech generation. The browser requests ephemeral audio for normalized reply fragments and does not persist generated audio.

## Consequences

- Conversation mode uses one configured model and voice across clients.
- The host gains an authenticated speech endpoint and provider latency, cost, and failure modes.
- A missing TTS credential prevents host startup rather than leaving conversation mode partly functional.
- Local development and automated smoke tests use deterministic local speech and dummy credentials.
- Browser speech synthesis is removed; there is no client-specific fallback path.
