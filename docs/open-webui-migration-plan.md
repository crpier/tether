# Open WebUI migration implementation record

Tracking issue: [#604](https://github.com/crpier/tether/issues/604)

## Status

The local implementation is complete on the migration branch. Production still
runs the locked pre-migration release. Deployment requires the production gates
and explicit cutover approval recorded below.

The implementation uses stock Open WebUI `v0.11.1` at digest
`sha256:6bb1fbe8ab0a3e0456067f493044ffb66a30a65a34be47f6a5862176a370dd16`
from source commit `d3e8bf3405e848cfba377814d0aa7ba7290e414d`. Production
must not use `main`, `latest`, or a floating release tag.

## Final decision

Stock Open WebUI replaces Tether's Pi-backed assistant stack in one release.
Open WebUI owns accounts, browser sessions, conversations, provider and model
configuration, model execution, native tool calling, approvals, files, voice,
native memory, and optional built-in web search.

Tether is a headless Python domain host. It owns typed Health Connect, Bucket,
and Todo state behind a bearer-authenticated OpenAPI interface. It does not
retain compatibility adapters, a second agent runtime, old conversation APIs,
or a custom assistant UI.

Automations, code execution, the code interpreter, and Ollama remain disabled.
Open WebUI `v0.11.1` approvals are experimental and do not protect Automations.

## Implemented shape

```text
Browser / physical phone
      |
      | HTTPS :8443
      v
stock Open WebUI v0.11.1
      |
      | Docker network
      | Bearer TETHER_OPEN_WEBUI_TOKEN
      v
Tether Python host :8000
  - 17 allowlisted OpenAPI operations
  - Health Connect capture routes
  - SQLite domain state
      ^
      |
      | HTTPS :443
      | Bearer TETHER_API_TOKEN
      |
Android Health Connect capture
```

Open WebUI calls `http://host:8000` and reads `tools/openapi.json`. The browser
does not receive `TETHER_OPEN_WEBUI_TOKEN`. Android continues to use the host's
existing HTTPS 443 origin and the independent `TETHER_API_TOKEN`.

Open WebUI binds local port `3000`. The future cutover publishes it with a
separate Funnel listener on HTTPS 8443:

```sh
sudo tailscale funnel --bg --https=8443 3000
```

Remove only that listener with:

```sh
sudo tailscale funnel --bg --https=8443 3000 off
```

Do not use `tailscale funnel reset`; it would also remove the retained HTTPS 443
listener.

## Retained operations

The OpenAPI document exposes exactly these 17 operations.

Bucket:

- `add_movie`
- `add_place`
- `add_book`
- `add_travel`
- `add_purchase`
- `complete_bucket_item`
- `search_bucket_items`
- `set_purchase_decision`
- `set_bucket_item_intent`
- `triage_report`

Todo:

- `create_todo`
- `list_todos`
- `set_todo_status`

Health Connect:

- `analyze_health_connect`
- `health_connect_inventory`
- `query_health_connect`
- `summarize_health_connect`

The host validates each request with its Pydantic parameter model and returns a
bounded tool envelope. Tool logs contain operation, duration, and success, not
request bodies, prompts, health values, or bearer tokens. Tool calls do not need
a Tether session, conversation, Pi secret, agent trace, or model endpoint.

Bucket search is deterministic SQLite. The active host has no embedding model,
model cache, FastEmbed dependency, LanceDB projection, vector database, or
generic database-query tool.

## Completed local work

### Assistant replacement

- Added the exact pinned Open WebUI image with its own `open-webui-data` volume.
- Kept the official image unchanged. Tether does not fork or patch Open WebUI.
- Added the authenticated OpenAPI schema and operation routes under
  `apps/host/tether/open_webui/`.
- Separated the Open WebUI tool token from the Android Health Connect token.
- Added the checked-in Tether Workspace Model prompt and documented one-time
  admin setup.
- Kept Open WebUI away from the Docker socket, Tether data, host files, and old
  Pi credentials.

### Host slimming

- Reduced host composition to Health Connect, Bucket, Todo, tool routing,
  structured logs, and the two SQLite databases.
- Removed all model-backed host work, custom chat and voice services, assistant
  scheduling, writable Tether memory, provider authentication, and browser
  sessions.
- Left old assistant tables inert for rollback. The migration does not drop or
  rewrite them destructively.

### Removed dependency graphs

The final release does not retain the optional YouTube, Gmail, Readwise, Reader,
ebook, or KOReader integrations. Their tools, routes, credentials, libraries,
and workers were deleted because their dependency graphs prevented a small,
coherent host.

The old `apps/web` Solid application and `apps/agent` TypeScript application
were deleted with their Node and pnpm build paths. Pi RPC, generated TypeScript
tool shims, Tether chat, STT/TTS, old standalone web smoke tests, and the Node
production runtime were deleted with them.

### Backups

New-format backups contain:

- consistent `VACUUM INTO` snapshots of `tether.sqlite3` and
  `telemetry.sqlite3`
- a complete archive of the `open-webui-data` volume
- production `.env`

The backup script briefly stops Open WebUI while archiving its volume and
restarts it through success and failure paths. New-format backups do not archive
old assistant files. The locked old-stack backup remains the rollback copy for
the pre-migration release.

### Standalone integration smoke

The maintained harness is `tests/open-webui` and the entry point is:

```sh
just validate-open-webui-smoke
```

It runs the real pinned Open WebUI image, real Tether host, a fake
OpenAI-compatible model, and Chromium. Its five Playwright tests cover:

- first-admin creation while signup remains disabled
- authenticated schema discovery and rejection of invalid credentials
- interactive approval and a real tool result
- Todo creation and listing with refresh persistence
- conversation persistence across an Open WebUI restart

Console errors, page errors, 5xx responses, and unexpected request failures fail
the smoke. The local smoke passes all five tests.

The earlier spike in `spikes/open-webui-v0.11.1/` also proved the stock recorder
with Chromium's fake microphone on localhost. That result does not satisfy the
physical-phone production gate.

## Current validation

Run the grouped repository checks from the repository root:

```sh
just typecheck
just lint
just format-check
just test
just validate-host-logs
just validate-open-webui-smoke
```

Validate Compose and the production host image with explicit test credentials:

```sh
TETHER_API_TOKEN=test-capture-token \
TETHER_OPEN_WEBUI_TOKEN=test-open-webui-token \
WEBUI_SECRET_KEY=test-webui-secret \
WEBUI_URL=http://127.0.0.1:3000 \
docker compose config --quiet
docker build .
```

Run the Android gate with a compatible SDK and JDK:

```sh
just android-build
```

The raw host and standalone `snekok` commands remain documented in
[`development.md`](./development.md).

## Historical rollback record

The locked pre-migration production release is Git commit and image tag
`c956fff`. Its image digest is
`sha256:9c684b0ac3bb1863ff56eeb48dbcbf0bab4d523fd836948bda108ba7f39d238c`.

On 2026-08-26, production still had the historical `tether_data` and
`tether_model-cache` volumes. Funnel HTTPS 443 proxied to host port `8000`, and
`/srv/tether/pi-agent` was owned by `tether:tether` with mode `0700`. A fresh
old-stack restic backup completed at 06:53 UTC. Preserve that backup, the old
image, the old Git revision, and the Pi credential directory through the
migration trial.

This paragraph records the old stack only. The new stack has no model-cache
volume and does not use `/srv/tether/pi-agent`.

## Remaining production acceptance gates

Local implementation is not production migration. Complete all of these gates
against the proposed production deployment:

1. Configure the actual provider and model, then prove native function calling
   with Tether's schema and real tool continuation.
2. On a physical phone, prove Open WebUI voice transcription and TTS over
   Tailscale Funnel HTTPS 8443.
3. Run a full restore drill into a fresh Compose project. Recover both Tether
   SQLite databases, `.env`, the complete Open WebUI volume, admin access,
   provider configuration, tool-server configuration, and a persisted
   conversation.
4. Run a physical Android Health Connect sync against the unchanged HTTPS 443
   host origin with `TETHER_API_TOKEN`.
5. Review the complete results and obtain explicit production cutover approval.

Do not claim production is migrated until all five gates pass and cutover is
approved.

## Future production cutover

Cutover is one explicitly approved maintenance operation:

1. Merge a fully validated migration commit to `main`.
2. Record the merge SHA and verify the locked rollback assets remain available.
3. Run and verify one final old-stack backup.
4. Pull the VM checkout because `compose.yaml` and `deploy/` changed.
5. Deploy the new host image and pinned Open WebUI image with a fresh
   `open-webui-data` volume.
6. Complete private first-admin, provider, Workspace Model, prompt, and tool
   setup.
7. Run the linked-Todo cleanup only after reviewing its report.
8. Publish only the new HTTPS 8443 Funnel listener.
9. Run every production acceptance gate.
10. Start the trial only after explicit approval. Do not run the old Pi stack in
    parallel.

Do not import old Tether chats, mirror Open WebUI transcripts, or copy Open
WebUI state into Tether.

## Rollback

Rollback restores the old deployment. It is not a compatibility mode in the new
application.

1. Stop Open WebUI and remove only its HTTPS 8443 Funnel listener.
2. Preserve `open-webui-data`.
3. Restore the recorded pre-migration Git revision and old Compose definition.
4. Pin the host image to `c956fff`.
5. Verify `/srv/tether/pi-agent` ownership, mode, and contents.
6. Start the old stack and verify login, chat, tools, and Health Connect sync.
7. Restore the pre-migration databases only if the live databases are corrupt.

Domain mutations made through Open WebUI tools may appear in the restored old
UI. Open WebUI conversations and native memories will not. This is accepted.

## Completion

The local implementation and its record are complete. Production completion is
separate: every production acceptance gate must pass, and the user must
explicitly approve cutover.
