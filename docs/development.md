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

## Logs

In `just dev` the host logs print **colorized and readable** straight to the
terminal (a TTY selects the console renderer). Set
`TETHER_LOGGING_LEVEL=DEBUG` for more detail.

Every host log line servicing a chat turn carries a `run_id`, so you can follow
one turn end to end by grepping for its id. (In the docker path the container
emits JSON; `just logs <run_id>` renders it readable and filters to that turn.)

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
