# Developing Tether

This guide describes development against the target Open WebUI architecture.
The migration remains local until the migration PR passes its full gate, is
merged with explicit approval, and a separate production cutover is approved.

## What runs locally

The application has two processes:

- the Python capability host on `http://127.0.0.1:8000`
- stock Open WebUI on `http://127.0.0.1:3000`

Open WebUI is an external product. Do not edit, patch, or rebuild its frontend.
Tether has no SPA, Pi process, chat WebSocket, browser login, STT/TTS service,
model allowlist, or generated TypeScript tool shims.

## First local start

Create the local environment with independent generated credentials:

```sh
just bootstrap
```

At minimum, set:

```dotenv
TETHER_API_TOKEN=<Android Health Connect token>
TETHER_OPEN_WEBUI_TOKEN=<Open WebUI tool-server token>
WEBUI_SECRET_KEY=<Open WebUI session secret>
WEBUI_URL=http://127.0.0.1:3000
OPENAI_API_BASE_URLS=<OpenAI-compatible provider base URL>
OPENAI_API_KEYS=<provider API credential>
```

`TETHER_OPEN_WEBUI_TOKEN` and `TETHER_API_TOKEN` must be different. Review the
generated `.env`, then start the real production-shaped stack:

```sh
just app-start
docker compose ps
curl --fail http://127.0.0.1:8000/health
curl --fail http://127.0.0.1:3000/health
```

Compose pulls this exact stock image:

```text
ghcr.io/open-webui/open-webui:v0.11.1@sha256:6bb1fbe8ab0a3e0456067f493044ffb66a30a65a34be47f6a5862176a370dd16
```

Use `docker compose down` to stop the stack. Do not pass `-v` unless you intend
to delete both applications' local state.

## Open WebUI setup

With `OPENAI_API_BASE_URLS` and `OPENAI_API_KEYS` set in `.env`, open
<http://127.0.0.1:3000> and perform the one-time setup documented in
[deployment.md](./deployment.md#one-time-open-webui-setup). For local work, the
environment-owned tool connection uses:

- URL `http://host:8000`
- spec path `tools/openapi.json`
- bearer key `TETHER_OPEN_WEBUI_TOKEN`
- ID `tether`

Use the checked-in prompt at
`deploy/open-webui/tether-system-prompt.md`. A provider API credential is a
prerequisite for real model turns. Open WebUI cannot use the former Pi
ChatGPT/Codex subscription login.

Keep Automations, code execution, the code interpreter, and Ollama disabled.
Keep persistent configuration disabled and tool permissions enabled. Approval
mode starts at `ask`, but approvals in Open WebUI `v0.11.1` are experimental and
do not protect Automations. Global Admin UI changes are runtime-only and reset
on restart; configure provider and voice defaults through `.env` instead.

## Python iteration

Install and test the host from its package directory:

```sh
cd apps/host
uv sync
uv run python -m snektest tests/
```

For a native host process, set its database paths and required bearer tokens in
the environment, then run:

```sh
cd apps/host
uv run python -m tether
```

Open WebUI in Compose resolves the Compose service name `host`, not a native
process on the workstation. Use the full Compose stack for end-to-end tool
testing unless you deliberately provide a Docker-reachable native-host URL.

## Logs and debugging

The host emits structured logs to stdout in Compose:

```sh
docker compose logs -f host
docker compose logs -f open-webui
```

Host tool logs include operation name, duration, and success. They must not
include request bodies, prompts, bearer tokens, or health values. Open
WebUI owns assistant traces and conversation history. There are no Tether Pi
session transcripts, chat `run_id` traces, Vite logs, or `/trace` endpoint.

When debugging a tool call, check both containers and call the schema with the
server token:

```sh
curl -i \
  -H "Authorization: Bearer $TETHER_OPEN_WEBUI_TOKEN" \
  http://127.0.0.1:8000/tools/openapi.json
```

Repeat without the header and verify that the host rejects the request.

## Health Connect development

Health Connect protocol changes must preserve the retained wire contract in
[`health-connect-wire-v3.md`](./health-connect-wire-v3.md).

## Validation

Install all retained development dependencies with `just install`.

Host:

```sh
cd apps/host
uv run pyright
uv run ruff check .
uv run ruff format --check .
uv run python -m snektest tests/
```

Standalone `snekok` package:

```sh
cd packages/snekok
env -u UV_PROJECT -u VIRTUAL_ENV uv run pyright
env -u UV_PROJECT -u VIRTUAL_ENV uv run ruff check .
env -u UV_PROJECT -u VIRTUAL_ENV uv run ruff format --check .
env -u UV_PROJECT -u VIRTUAL_ENV uv run python -m snektest tests/
```

Compose and image:

```sh
TETHER_API_TOKEN=test-capture-token \
TETHER_OPEN_WEBUI_TOKEN=test-open-webui-token \
WEBUI_SECRET_KEY=test-webui-secret \
WEBUI_URL=http://127.0.0.1:3000 \
OPENAI_API_BASE_URLS=https://provider.example/v1 \
OPENAI_API_KEYS=test-provider-token \
docker compose config --quiet
docker build .
```

The root recipes group the same checks:

```sh
just typecheck
just lint
just format-check
just test
just validate-host-logs
just validate-open-webui-smoke
```

`just validate-open-webui-smoke` starts the real pinned Open WebUI image, the
real host, a fake OpenAI-compatible provider, and Chromium. Its five tests cover
first-admin creation, admin-created daily-user isolation, authenticated schema
discovery, an approval that survives refresh, Todo create and list, conversation
persistence, and a fresh native tool call after Open WebUI restart. Browser
console errors, page errors, 5xx responses, and unexpected request failures fail
the smoke.

Android Capture:

```sh
cd apps/capture-android
JAVA_HOME=/path/to/jdk17 ./gradlew \
  :app:assembleDebug \
  :app:testDebugUnitTest \
  :app:lintDebug \
  :core:test
```

The local gate does not replace the production acceptance checks in
[`deployment.md`](./deployment.md#production-acceptance-gates).
