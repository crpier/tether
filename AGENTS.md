# Agent guide

## GitHub workflow

- This project uses GitHub issues for tracking work.
- Do not hand-edit GitHub URLs or assume issue state. Query with
  `gh issue view/list` when needed.
- Implementation work should reference the relevant GitHub issue.
- When starting a new unit of work, stash uncommitted changes, run `git fetch`,
  and create a branch from the latest `origin/main`.
- Work in a branch and open a PR against `main` when the unit is complete. Merge
  only when explicitly told to do so.
- Write PR Markdown to a temporary file and pass it to `gh` with `--body-file`.
  Verify the rendered body with `gh pr view`.
- Use TDD for feature, bug-fix, and refactoring work.

## Architecture

- [ADR 0030](./docs/adr/0030-open-webui-owns-assistant-runtime.md) is the active
  assistant architecture decision. Stock Open WebUI owns accounts, sessions,
  conversations, models, tool continuation, approvals, voice, files, and native
  memory. Tether is a headless Python domain host.
- Open WebUI is pinned to
  `ghcr.io/open-webui/open-webui:v0.11.1@sha256:6bb1fbe8ab0a3e0456067f493044ffb66a30a65a34be47f6a5862176a370dd16`.
  Do not use a floating tag or patch Open WebUI.
- Do not recreate the Pi runtime, Tether SPA, chat or browser authentication,
  STT/TTS, model allowlist, assistant scheduler, or writable assistant memory.
- Open WebUI calls `http://host:8000` with the spec path
  `tools/openapi.json`. It authenticates with `TETHER_OPEN_WEBUI_TOKEN`.
- Android Health Connect keeps the host's HTTPS 443 origin and authenticates
  with the independent `TETHER_API_TOKEN`. Never reuse either bearer token.
- The first-release OpenAPI allowlist has exactly 17 Bucket, Todo, and Health
  Connect operations. Bucket search is deterministic SQLite.
- Keep Automations, code execution, the code interpreter, and Ollama disabled.
  Keep tool permissions enabled. Open WebUI `v0.11.1` approvals are
  experimental and do not protect Automations.

## Production deployment

- Canonical runbook: [`docs/deployment.md`](./docs/deployment.md).
- The Open WebUI migration remains local. Production cutover is a future,
  explicitly approved maintenance operation.
- Live tailnet target: `tether@tether`. Deploy only merged, validated `main`.
- The host remains on local `8000` behind existing Funnel HTTPS 443. Open WebUI
  uses local `3000` behind Funnel HTTPS 8443.
- Publish Open WebUI only with
  `sudo tailscale funnel --bg --https=8443 3000`. Remove only that listener with
  `sudo tailscale funnel --bg --https=8443 3000 off`. Do not use
  `tailscale funnel reset`, which would also remove the retained 443 listener.
- Pull the VM checkout before deployment when `compose.yaml` or `deploy/`
  changes.
- Backups must include the full Open WebUI volume as well as Tether SQLite data.
  A full migration rollback needs the old Git revision, old image, and preserved
  `/srv/tether/pi-agent` directory.

## Debugging

- Read host behavior from structured container logs with
  `docker compose logs -f host`.
- Read assistant and tool-continuation behavior from Open WebUI's logs and
  activity views with `docker compose logs -f open-webui`.
- Tether no longer has Pi session transcripts, Vite logs, chat `run_id` traces,
  or a `/trace` endpoint.
- Tool logs may contain operation, duration, and success. They must not contain
  prompts, request bodies, health values, or bearer tokens.
- Tether is single-user and local or low latency. Prefer short waits so a hang
  fails quickly.

## Testing and validation

- Use `snektest` for Python tests. Its installed distribution `METADATA`
  contains the usage guide.
- Use `pyright` for static typing and `ruff` for lint and format checks.
- Run the full changed surface before commit, push, PR, merge, or deploy.
- Do not relax production `pyright` or `ruff` rules to hide findings. A scoped,
  commented test-only exception is acceptable only for a genuine false positive.
- Do not invent `just` recipe names. Check `justfile` before documenting or
  running one.

Host gate, from `apps/host`:

```sh
uv run pyright
uv run ruff check .
uv run ruff format --check .
uv run python -m snektest tests/
```

Standalone `snekok` gate, from `packages/snekok`:

```sh
env -u UV_PROJECT -u VIRTUAL_ENV uv run pyright
env -u UV_PROJECT -u VIRTUAL_ENV uv run ruff check .
env -u UV_PROJECT -u VIRTUAL_ENV uv run ruff format --check .
env -u UV_PROJECT -u VIRTUAL_ENV uv run python -m snektest tests/
```

Repository gate:

```sh
TETHER_API_TOKEN=test-capture-token \
TETHER_OPEN_WEBUI_TOKEN=test-open-webui-token \
WEBUI_SECRET_KEY=test-webui-secret \
WEBUI_URL=http://127.0.0.1:3000 \
docker compose config --quiet
docker build .
just validate-host-logs
just validate-open-webui-smoke
```

The Open WebUI smoke must use the real pinned image, real host, a fake
OpenAI-compatible model, and Chromium. It must cover first-admin creation,
authenticated schema discovery, interactive approval, a read tool, Todo create
and list, refresh persistence, and Open WebUI restart persistence. Console
errors, page errors, 5xx responses, and unexpected request failures fail the
smoke.

Android gate, from `apps/capture-android` with a compatible SDK and JDK:

```sh
JAVA_HOME=/path/to/jdk17 ./gradlew \
  :app:assembleDebug \
  :app:testDebugUnitTest \
  :app:lintDebug \
  :core:test
```

Before production cutover, test the chosen provider and model's native function
calling, Open WebUI voice transcription and TTS on a physical phone over Funnel
HTTPS 8443, a full backup restore, and a physical Android Health Connect sync on
the unchanged HTTPS 443 host origin. Cut over only with explicit approval.

## Databases

- Use `snekql` for database access. Its installed distribution `METADATA`
  contains the usage guide.
- Keep old assistant tables inert during the migration. Do not drop them; the
  old image may need them during operational rollback.
