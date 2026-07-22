# Developing Tether

This is the **fast local iteration loop**. Use it for day-to-day work.

> Do **not** iterate by running `docker compose up --build`. That rebuilds the
> production image (full SPA build + agent install + `uv sync`) on every change —
> it's for verifying a deploy, not for developing. See [deploy.md](./deploy.md)
> for when you actually want the image.

## The loop

```sh
just install      # uv sync + pnpm install for web and agent (one time / on dep changes)
just bootstrap    # one time: write .env with generated secrets + create the pi-agent dir
just pi-auth      # one time: log in to your model provider (writes pi auth.json)
just dev          # host (auto-reload) + web (Vite HMR) together; Ctrl-C stops both
```

Then open **<http://127.0.0.1:3000>** — the Vite dev server. It proxies `/api`
and `/ws` to the host on `:8000`, so you hit one origin and get HMR. Don't open
`:8000` directly; that's the host with no built SPA in dev.

What you get:

- **Edit Python** (`apps/host`) → uvicorn auto-reloads (`TETHER_RELOAD=true`).
- **Edit Solid** (`apps/web`) → instant HMR, no reload.
- **Edit a tool / agent** (`apps/agent`) → `pi` runs the TS extension directly
  (`apps/agent/src/generated/index.ts`), no build step; restart the host (or it
  reloads) to pick up changes to spawned pi processes.

`just dev` bakes in a dev login (`TETHER_APP_PASSWORD=dev`). Log in with `dev`.

## First-run setup, in detail

`just bootstrap` copies `.env.example` to `.env` and fills
`TETHER_APP_PASSWORD` / `TETHER_SESSION_SECRET` with generated secrets. It won't
touch an existing `.env`. It also creates `~/.local/share/tether/pi-agent`
(override with `TETHER_PI_AGENT_DIR`), the directory that holds pi's `auth.json`.

`just pi-auth` launches `pi` against that directory; run `/login openai-codex`
(or `/login opencode-go`) and exit. Chat and scheduled-prompt triggers need
this — without it, `just validate-env` warns and turns will fail at runtime.

YouTube ingestion is optional; see [deploy.md](./deploy.md#youtube-ingestion)
and `.env.example`. Locally, `just youtube-auth` runs the browser OAuth flow and
caches the token under `.tether/`.

### Gmail: re-consent for the backlog-purge write scope

The read-only ingestion gate needs only `gmail.readonly`. The backlog-purge
sweep (`TETHER_GMAIL_PURGE_ENABLED=1`) additionally *proposes* mailbox writes
(archive/label/trash), which need the `gmail.modify` scope. `just gmail-auth`
now requests both scopes — but **a token minted before `gmail.modify` was added
is not upgraded automatically**. Re-authorizing is the only manual dev step:

1. Delete the cached token: `rm .tether/gmail-oauth-token.json`.
2. Re-run `just gmail-auth` and complete the Google consent screen, ticking the
   new "read, compose, send and permanently delete" (modify) permission — the
   consent screen must show `gmail.modify` alongside `gmail.readonly`.
3. Confirm the bootstrap prints "Authorized" and lists recent subjects.

Until this is done, the purge executors fail *gracefully*: an approved
`gmail.*` action whose token still lacks the scope resolves `outcome: failed`
with a clear `gmail.modify scope missing (403)` detail — it never crashes the
host. No mailbox write is ever permanent: `gmail.delete` moves the message to
Trash, never `messages.delete`.

## Logs and session data

In `just dev` the host logs print **colorized and readable** straight to the
terminal (a TTY selects the console renderer). `just dev` also runs at
`TETHER_LOGGING_LEVEL=DEBUG` for more detail.

Every host log line servicing a chat turn carries a `run_id`, so you can follow
one turn end to end by grepping for its id. (In the docker path the container
emits JSON; `just logs <run_id>` renders it readable and filters to that turn.)

### Where to look when a bug is reported

`just dev` mirrors everything to files under `.tether/` (gitignored) so an
**agent can read back what the app actually did** without needing the live
terminal. These are the canonical debugging sources:

| What | Path | Format |
| --- | --- | --- |
| Host app logs (Python: requests, tools, scheduler, ingestion, tracebacks) | `.tether/logs/host.log` | one JSON object per line |
| Web dev-server output (Vite/HMR, proxy, build errors) | `.tether/logs/web.log` | raw Vite stdout/stderr |
| Agent session transcripts (per pi run: model turns + tool calls) | `.tether/pi-sessions/<conversation-id>/*.jsonl` | JSONL, one event per line |
| Scheduled/recall agent runs | `.tether/pi-sessions/{scheduled,recall}/<session-id>/*.jsonl` | JSONL |
| Live per-run agent trace (in-memory, last 200 runs) | `GET /trace` on the host (`:8000`) | JSON |

`just dev` **truncates** `host.log` and `web.log` at launch, so each run starts
clean; the host **appends** across auto-reloads within that run. Read the host
log as structured JSON, e.g. follow one chat turn:

```sh
tail -f .tether/logs/host.log | jq -r 'select(.run_id=="<run_id>")'
```

The file sink is driven by `TETHER_LOG_FILE` (set to `.tether/logs/host.log` by
`just dev`). The console output is unchanged whether or not the file sink is on —
it stays a colorized TTY view. Unset in production; the docker container logs to
stdout (`just logs`). To capture host logs to a file outside `just dev`, run e.g.
`TETHER_LOG_FILE=.tether/logs/host.log just host`.

## Codegen

Pydantic models are the single source of truth. After changing a model that
crosses the wire (REST schema or a tool param), regenerate the clients:

```sh
just codegen        # OpenAPI → TS client, tool JSON-Schemas → pi shims
just codegen-check  # what CI runs: re-running codegen produces no diff
```

Generated code is committed. The gate drift-checks it.

## Before you push

Run the full validation gate (see the repo `AGENTS.md` / `CLAUDE.md`):
`pyright`, `ruff check`, `ruff format --check`, the snektest suite,
`just codegen-check`, the web/agent typecheck+lint+format+test, and
`just validate-web-smoke`. `main` must stay green.
