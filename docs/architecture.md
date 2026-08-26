# Architecture overview

This document describes the production architecture from
[ADR 0030](./adr/0030-open-webui-owns-assistant-runtime.md). Production cut over
to this architecture on 2026-08-26.

## Shape

```text
Browser / phone
      |
      | HTTPS :8443
      v
stock Open WebUI v0.11.1
  - accounts and browser sessions
  - conversations and model execution
  - native tool calls and interactive approvals
  - files, voice, memory, and optional web search
      |
      | Docker network
      | Bearer TETHER_OPEN_WEBUI_TOKEN
      v
Tether Python host :8000
  - allowlisted OpenAPI tools
  - Health Connect capture routes
  - Health Connect episode summaries
  - SQLite domain state
      ^
      |
      | HTTPS :443
      | Bearer TETHER_API_TOKEN
      |
Android Health Connect capture
```

Tailscale Funnel terminates HTTPS outside Compose. The existing host remains on
local port `8000` and the existing public HTTPS 443 origin so Health Connect
does not move. Open WebUI binds local port `3000` and uses a separate Funnel
listener on HTTPS 8443.

## Ownership

Open WebUI owns the generic assistant. This includes login, browser sessions,
the canonical chat transcript, model and provider configuration, inference,
tool-call continuation, interactive approvals, files, voice, native memory,
and optional built-in web search. Tether does not copy or synchronize Open
WebUI conversations.

Tether is a headless Python capability host. It owns typed domain state and
deterministic integrations that remain useful without the old assistant:

- Health Connect ingestion, projections, episode summaries, and read tools
- Bucket items and deterministic triage
- Todos
- SQLite data, structured logs, telemetry, and backups

The OpenAPI document exposes exactly 17 operations:

- Bucket: `add_movie`, `add_place`, `add_book`, `add_travel`, `add_purchase`,
  `complete_bucket_item`, `search_bucket_items`, `set_purchase_decision`,
  `set_bucket_item_intent`, and `triage_report`
- Todo: `create_todo`, `list_todos`, and `set_todo_status`
- Health Connect: `analyze_health_connect`, `health_connect_inventory`,
  `query_health_connect`, and `summarize_health_connect`

Bucket search reads SQLite deterministically.

The host does not own a chat UI, chat transcript, browser authentication, model
selection, model allowlist, STT, TTS, assistant scheduling, writable assistant
memory, or an agent runtime.

## Open WebUI deployment

Compose runs the official image without modifications:

```text
ghcr.io/open-webui/open-webui:v0.11.1@sha256:6bb1fbe8ab0a3e0456067f493044ffb66a30a65a34be47f6a5862176a370dd16
```

The image has its own `open-webui-data` volume. It does not mount the Docker
socket, Tether data, host files, or credentials. Tether does not fork Open
WebUI, rebuild its frontend, inject JavaScript, or add custom routes.

Environment-enforced configuration disables persistent configuration,
Automations, code execution, the code interpreter, and Ollama. Disabling
persistent configuration prevents restored or admin-modified database values
from overriding those defaults after restart. An authenticated admin can still
change in-memory values until the next restart, so the operator must not enable
the excluded features. Provider, Tether tool-server, native-memory, and optional
voice defaults come from supported environment inputs instead of global Admin UI
configuration. Tool permissions remain enabled. Open WebUI `v0.11.1` tool
permissions and approvals are experimental. Interactive approvals do not
protect Automations, so Automations remain disabled.

The first account is a private setup-and-recovery administrator. Regular browser
and phone sessions use a separate `user` role account. Open WebUI's admin tool
configuration endpoint can return server credentials to an administrator, so
the admin account is not the daily chat identity and is used only before the
public Funnel listener is enabled or through private maintenance access.

Open WebUI requires an API credential for a supported model provider. Pi's
ChatGPT/Codex subscription authentication is not compatible with Open WebUI.
The migration starts with one default model that supports native function
calling reliably.

## Tool boundary

Open WebUI connects to `http://host:8000` on the Compose network and reads
`tools/openapi.json`. The host exposes the schema at `/tools/openapi.json` and
selected operations at `/tools/<operation>`.

Both schema discovery and tool calls require:

```http
Authorization: Bearer <TETHER_OPEN_WEBUI_TOKEN>
```

`TETHER_OPEN_WEBUI_TOKEN` is a dedicated server-to-server credential. It must
not equal `TETHER_API_TOKEN`, which remains the Android Health Connect bearer
credential. The browser receives neither token from Tether.

Tool handlers reuse the retained Pydantic parameter models and domain services.
The route adapter validates inputs, returns the existing tool envelope, bounds
results, and logs operation name, duration, and success without request bodies,
prompts, or health values. There is no `session_id`, Pi secret, Tether
conversation, agent trace, MCP gateway, Pipe, or model endpoint in this path.

## Data and storage

Tether keeps its main and telemetry SQLite databases. Old assistant tables stay
in place but are inert. The migration does not destructively rewrite them,
which lets the old release use the existing databases during rollback.

Open WebUI keeps its own SQLite-backed state in `open-webui-data`. This volume
contains accounts, configuration, conversations, native memory, and tool-server
setup. The normal backup stops Open WebUI before either Tether SQLite snapshot,
keeps it stopped through the complete volume archive, and then restarts it
before sending the coherent backup set and production environment file to
restic.

## Security

There are three independent boundaries:

- Open WebUI authenticates people and owns its browser sessions on HTTPS 8443.
- The daily Open WebUI account has the `user` role and cannot read admin
  configuration endpoints containing server credentials.
- Open WebUI authenticates to Tether tools with `TETHER_OPEN_WEBUI_TOKEN` over
  the Compose network.
- Android Health Connect authenticates to the host with `TETHER_API_TOKEN` on
  the existing HTTPS 443 origin.

Generate the two bearer tokens independently. Do not expose the tool schema or
operations without the Open WebUI token. Health checks are the only
unauthenticated host exception.

## Operations and rollback

The two containers have separate durable volumes and health checks. Structured
host logs go to stdout. Open WebUI supplies its own activity views and logs for
assistant execution; Tether has no chat-run trace.

A normal post-cutover update preserves both volumes. A full migration rollback
requires all of the following:

- the recorded pre-migration Git revision
- the recorded pre-migration host image
- the old `/srv/tether/pi-agent` credential directory
- a pre-migration database backup if the live database cannot be reused

Stopping Open WebUI alone is not a full rollback because the old Compose file,
image, and Pi credentials are also required.

## Decisions

- [ADR 0013](./adr/0013-health-telemetry-separate-store.md) keeps raw Health
  telemetry in its separate SQLite store.
- [ADR 0019](./adr/0019-todo-vertical.md) records the retained Todo model.
- [ADR 0020](./adr/0020-fastapi-rest-contract.md) keeps FastAPI responsible for
  HTTP routing and validation.
- [ADR 0025](./adr/0025-one-interface-per-integration.md) keeps one domain
  interface per external integration.
- [ADR 0030](./adr/0030-open-webui-owns-assistant-runtime.md) replaces the Pi
  runtime and Tether SPA with stock Open WebUI.

Older ADRs remain as history. Their frontmatter records when ADR 0030
supersedes an earlier assistant, conversation, memory, scheduling, or voice
decision.
