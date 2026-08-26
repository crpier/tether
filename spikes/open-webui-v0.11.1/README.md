# Open WebUI v0.11.1 migration spike

Throwaway harness for issue #604. It tests the migration plan's Open WebUI
assumptions against the official image, a deterministic OpenAI-compatible model,
and a bearer-authenticated OpenAPI tool server.

## Pinned image

- Release: `v0.11.1`
- Published: 2026-08-25
- Digest: `sha256:6bb1fbe8ab0a3e0456067f493044ffb66a30a65a34be47f6a5862176a370dd16`

## Current smoke

The maintained standalone smoke supersedes the throwaway spike harness:

```sh
just validate-open-webui-smoke
```

It runs five Playwright tests from `tests/open-webui` against the real pinned
image, real host, fake OpenAI-compatible provider, and Chromium.

## Result

Passed locally on 2026-08-26:

- First-account admin creation while signup is disabled; second signup rejected.
- Global OpenAPI tool discovery with server-side bearer authentication.
- Invalid or absent bearer credentials rejected for schema and invocation.
- Native function schema sent to the model, tool invoked, result returned to the
  model, and final assistant turn rendered.
- `ask` approval prevented invocation until Allow, survived a browser reload,
  and resumed the same turn.
- Browser made no direct request to the tool server.
- Conversation history survived an Open WebUI container restart.
- No horizontal overflow at a 375 by 812 viewport.
- Stock voice recorder worked on localhost with a fake Chromium microphone; the
  authenticated STT transcript was inserted into the composer.
- No browser console or page errors occurred in the tool flow.

The spike uses `ENABLE_PERSISTENT_CONFIG=false` so environment configuration is
reapplied deterministically. Production should use the migration plan's durable
configuration policy.

## Not proved

- The production provider and model. Validate their native function calling.
- Physical-phone voice transcription and TTS over Tailscale Funnel HTTPS 8443.
- A full backup restore drill.
- Physical Android Health Connect sync at the unchanged HTTPS 443 origin.
- Production cutover approval.
