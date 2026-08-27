# Architecture overview

A map of Tether's stack and the load-bearing decisions, with pointers to the ADRs that record the hard-to-reverse ones. This is a map, not a spec — it says _what_ and _why_, not _how_ in detail.

## Shape

```
SolidJS UI ──HTTP/WS──▶ Python host ──spawns──▶ pi (RPC subprocess, JSONL/stdio)
    (built SPA)            │ owns state            │ runs generated TS tool shims
                           │ + logic               │ (pi.registerTool)
                           ▼                        │ tool.execute() ──┐
                        SQLite (snekql)             │  closed tool world │
                           ▲                        ▼                   │
                           └─────── loopback internal tool API ◀────────┘
                                    (per-process secret + session id)

  embeddings: in-host (FastEmbed/ONNX)   |   canonical Memory Markdown + SQLite mutation history
```

One deploy container: the **host + Node/pi co-resident** (so the host can spawn pi subprocesses), with the built Solid SPA served by the Python host alongside `/api` and `/ws`. Tailscale runs on the VM and terminates private HTTPS outside Compose. Named volumes hold durable data and the embedding-model cache. Dev runs everything natively.

## Components

**Python host** — the spine. Owns Evidence, Dreaming policy/history, typed vertical state, Search, scheduling, and the internal tool API. Built on **FastAPI**, fully async. **WebSocket** serves chat; plain **REST** serves current Memory Topics, exact `tether://` Evidence resolution, Dream history, triage, and typed verticals. There is no Memory CRUD or Review queue. Targets Python ≥3.14.

**pi (agent runtime)** — earendil-works/pi in RPC mode, driven as a host-spawned subprocess. "One agent" is a _definition_ (one tool belt, prompt, extensions), realized as multiple processes: one long-lived for foreground chat, ephemeral ones for background work. pi runs with built-in tools disabled — a **closed tool world** whose only surface is Tether's tools. See ADR 0002, ADR 0005.

**Tools** — every capability is a pi extension (`pi.registerTool`) whose `execute` is a thin TS shim that calls back into the host over the loopback internal tool API. All logic stays in Python; the shim only marshals `{params, session id, secret}`. Tool param schemas have a single source of truth — the host's Pydantic models — from which the shims are generated. For multi-call work, `execute_tools` runs a fresh confined TypeScript/JavaScript program over those same shims; it has no ambient filesystem, process, environment, network, imports, packages, or persistence, and every nested call still crosses the host boundary. See ADR 0005 and ADR 0032.

**Data layer** — **SQLite owns canonical Evidence, typed vertical state, Dream runs/cursors, suppressions, and complete Memory mutation/history records**, accessed through snekql. Current Memory is the recorded, Dreaming-authored Markdown workspace; there is no `Memory` row, trust state, facet table, or Todo→Memory link. Source integrations retain their own Evidence: Messages, Gmail records, Readwise highlights, reading progress, Health summaries, and other typed records.

**Health observations.** Health Connect keeps append-only raw Telemetry and current typed projections in `telemetry.sqlite3`. Its insight module turns bounded current records into deterministic sleep episodes, local sleep days, stage composition, efficiency, sleep-aligned heart rate, comparable seven-day windows, and personal baselines. Chat receives those compact observations, completeness counts, and exact Evidence URIs instead of joining raw sensor records. Dreaming receives the same distinction between computed measurements and interpretation, requires at least three comparable episodes for a pattern, and cannot make clinical claims. Tether does not invent an opaque health score.

**Search** — current Memory Search scans validated Topic files through the workspace service, ranks direct lexical title/body matches, and always reconciles bytes against recorded Dreaming state first. This small-corpus path guarantees that a fresh Dream mutation affects the next action without waiting for an index. Bucket-item and YouTube semantic Search retain their independent rebuildable LanceDB projections and local FastEmbed embeddings; they do not confer Memory authority.

**Scheduler** — in-process, a ~30s tick polling SQLite for due work; firing a trigger spawns an ephemeral pi process. Durability/retries/backpressure live in the loop and SQLite state (no Redis). Due rows are marked `claimed` before dispatch; each job is an `asyncio` task gated behind a concurrency cap (backpressure); failures get `next_attempt_at` backoff (retries). The push half of capture → resurface.

**Time** — backend stores UTC for every timestamp; the browser supplies the offset to convert one-shot times at capture. Recurrence _rules_ additionally store wall-clock time + IANA TZ, and each tick materializes the next fire as UTC (so daily/weekly survives DST).

**Frontend** — SolidJS SPA, built into the single production image and served by the Python host. Server state lives in `@tanstack/solid-query` (cache + invalidation), fed by the generated REST client. The single WebSocket is a _tagged event bus_ (`{type: chat | invalidate | notify}`), not just chat: the host pushes dumb cache-invalidation signals from its mutation choke point, so background agent mutations (new Candidates, fired triggers) surface live without polling. The **chat transcript is host-owned SQLite data**, not pi's session (ADR 0005) — the host assembles settled messages from pi's RPC delta stream and persists them; the UI rehydrates history from REST and the WS carries only live deltas, so chat survives mobile refresh and pi restarts.

**Conversation import** — imported user messages become canonical conversational Evidence. They feed the same bounded Dreaming assimilation path as live Messages; imports do not create provisional Memory rows or a separate Memory Candidate lifecycle.

**Codegen** — Pydantic models are the single source of truth, feeding three consumers: the OpenAPI doc → TS API client (Solid), the tool JSON-Schemas → pi tool shims, and runtime validation (host). A `just` recipe orchestrates the cross-language pipeline (Python emits schemas → Node generators run). Generated code is committed; CI drift-checks that re-running codegen produces no diff.

**Memory workspace** — canonical, recursively organized Markdown under `/data/kb/memory` (ADR 0021). Dreaming is its sole writer (ADR 0026). Topic files require YAML frontmatter and exact Evidence citations; meaningful paths are current identities. The web app resolves cited Messages and exact historical Health Connect episode versions through one Evidence inspector. It collapses each Topic's complete provenance set until requested. SQLite records complete versions/tombstones and authorized mutations. Reads and startup reconciliation restore unauthorized edits/deletions, remove unknown valid files, and preserve exact recoverable pre-acknowledgement Dream mutations. Obsidian and Neovim are read-only inspection clients; corrections enter as Messages and queue Dreaming.

## Observability

Three needs, two sinks. **Logs** (agent introspection + system health): **structlog** structured JSON to stdout, captured by Docker — no aggregator yet. Every line servicing a turn carries the **pi session id + turn id** as correlation key, so background (ephemeral-pi) turns are reconstructable after the fact. pi's stdout is the RPC channel, so the host emits the agent's behavior _on its behalf_, rebuilt from pi's RPC events (`tool_execution_start/update/end`) and tool callbacks; pi's **stderr** is folded into the host log stream. **Audit** is _derived_, not a spine: per-table `created_at` + lifecycle history columns + provenance answer "what happened to X" per entity — no event-log table.

## Operations

**Backup/restore** runs outside Compose as a host systemd timer. It creates independent consistent `VACUUM INTO` snapshots of `tether.sqlite3` and `telemetry.sqlite3`, then sends both snapshots, `/data/kb/memory`, `/data/kb/pi-sessions`, and the production `.env` through one restic client-side-encrypted backup to Backblaze B2. Restic retains seven daily and four weekly snapshots; healthchecks.io supplies the dead-man's-switch. SQLite remains the source of truth. Provider credentials under `/srv/tether/pi-agent` and OAuth files outside `/data/kb` are not currently covered and must be reauthorized or protected separately. See [deployment.md](./deployment.md#backups) for the exact restore drill and current coverage.

## Security

Two separate auth domains:

- **Human → app**: defense in depth — Tailscale network isolation _plus_ a single-password app login that mints a signed httpOnly session cookie (checked on REST and the WS handshake). The session layer is decoupled from the identity method, so OAuth can replace the password later. No multi-user model.
- **pi process → host**: the loopback internal tool API, authorized by a per-process secret injected at spawn; identity is the pi session id. Not reachable from the public surface.

## Models & cost

Cloud LLMs only (no local models), provider-agnostic via pi, not locked to frontier. "Self-hosted" refers to the application's deployment, not the model provider. Design for model portability: the host validates every tool input and tolerates malformed tool calls, so a weaker model can be less smart but never corrupt state.

## Decision records

- **0001** — memories are provisional until Review (superseded by 0021).
- **0002** — one agent _definition_ with a tool belt; concurrency via multiple pi processes, not sub-agents.
- **0003** — SQLite is the source of truth and Markdown derived (superseded for Memory by 0021).
- **0004** — Review and Recall tether Memory (superseded by 0021).
- **0005** — pi as the agent runtime over RPC, with generated TS tool shims calling the Python host (refined by 0023/0032/0033).
- **0006** — search is recomputed at the moment of use, never cached across actions.
- **0007** — knowledge-base filenames are opaque Memory UUIDs (superseded by 0021).
- **0008** — custom Starlette route contract layer (superseded by 0020).
- **0009** — hybrid Search is an embedded LanceDB projection, not FTS5 + sqlite-vec (refined by 0021).
- **0010** — provenance classes govern Memory trust (superseded by 0021).
- **0012** — raw Telemetry remains typed Evidence outside Memory (refined by 0021).
- **0020** — FastAPI owns REST validation, routing, and OpenAPI generation.
- **0021** — Memory is a canonical agent-curated Markdown workspace.
- **0022** — Dreaming mutates Memory through confined native-shaped pi file tools (superseded by 0033).
- **0023** — Tether Conversations own history and receive fresh Memory projections independently of pi sessions.
- **0024** — Delete everywhere physically prunes all retained backups.
- **0026** — Dreaming is the sole writer of current Memory.
- **0027** — Provider-backed text-to-speech is a required host dependency.
- **0028** — Scheduled prompts use the same Conversation execution path as chat.
- **0029** — Conversation scope organizes work without becoming Memory authority.
- **0030** — Open WebUI owns the assistant runtime (superseded by 0031).
- **0031** — Tether owns the assistant runtime again after the Open WebUI trial failed daily-use evaluation.
- **0032** — Fresh confined programs may orchestrate host-owned tools without ambient authority.
- **0033** — Confined TypeScript/JavaScript is Tether's only programmable agent environment; Memory reads cross typed host tools.

## Build order

Spine first (Evidence → Dreaming-maintained Memory → resurface, plus scheduler and chat), verticals later. Re-grill each vertical as it is built.
