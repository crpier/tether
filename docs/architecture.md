# Architecture overview

A map of Tether's stack and the load-bearing decisions, with pointers to the ADRs that record the hard-to-reverse ones. This is a map, not a spec — it says *what* and *why*, not *how* in detail.

## Shape

```
SolidJS UI ──HTTP/WS──▶ Python host ──spawns──▶ pi (RPC subprocess, JSONL/stdio)
  (frontend image)         │ owns state            │ runs generated TS tool shims
                           │ + logic               │ (pi.registerTool)
                           ▼                        │ tool.execute() ──┐
                        SQLite (snekql)             │  closed tool world │
                           ▲                        ▼                   │
                           └─────── loopback internal tool API ◀────────┘
                                    (per-process secret + session id)

  embeddings: in-host (FastEmbed/ONNX)   |   markdown KB + LanceDB search index: derived from SQLite
```

Two deploy containers: **host + Node/pi co-resident** (so the host can spawn pi subprocesses) and a **frontend** image (nginx/Caddy serving the prebuilt Solid app, reverse-proxying `/api` + WS). One shared persistent volume for all state. Dev runs everything natively; Nix is deferred until the build stabilizes.

## Components

**Python host** — the spine. Owns all state and business logic, the review/candidate gates, Search, the scheduler, and the internal tool API. Built on **Starlette**, fully async (no blocking IO). **WebSocket** for the chat surface (bidirectional, matches pi's mid-turn `steer`/`abort`); plain **REST** for everything else (memory CRUD, review queue, triage, bucket items, KB browse). Targets Python ≥3.14 (snekql floor).

**pi (agent runtime)** — earendil-works/pi in RPC mode, driven as a host-spawned subprocess. "One agent" is a *definition* (one tool belt, prompt, extensions), realized as multiple processes: one long-lived for foreground chat, ephemeral ones for background work. pi runs with built-in tools disabled — a **closed tool world** whose only surface is Tether's tools. See ADR 0002, ADR 0005.

**Tools** — every capability is a pi extension (`pi.registerTool`) whose `execute` is a thin TS shim that calls back into the host over the loopback internal tool API. All logic stays in Python; the shim only marshals `{params, session id, secret}`. Tool param schemas have a single source of truth — the host's Pydantic models — from which the shims are generated. See ADR 0005.

**Data layer** — **SQLite is the single source of truth** (ADR 0003), accessed through **snekql** (the author's async-first typed query builder over aiosqlite: typed models, pooled transactions, hand-authored ordered migrations, startup schema verification). WAL mode. Bucket items: one table with universal indexed columns (`item_type`, `state`, dedup key, `provenance`, `intent_context`, timestamps) plus a JSON `data` column holding the item-type's Pydantic payload. Memories are a separate table (amorphous, no item type).

**Search** — hybrid lexical + semantic over an **embedded LanceDB dataset** (`.tether/index/`), the two score lists fused with **RRF** (ADR 0009). Connection emerges from Search, not stored structure (CONTEXT.md). SQLite stays the single source of truth: it holds Memory rows *and* the canonical embedding vectors (BLOB); LanceDB is a **derived projection** (gitignored, not backed up, rebuilt from SQLite on restore — no re-embed), strictly parallel to the markdown KB. LanceDB runs **in-host** via its abi3 wheel behind a thin typed adapter (`SearchIndex`); the only module importing `lancedb`. We build only LanceDB's **native FTS index** (0.33 removed Tantivy) and **no vector ANN index** — at 10–50k Memories flat-scan exact cosine is single-digit ms and beats lossy IVF_PQ. Reads treat the index as a **candidate generator** — it returns ids+scores, which `MemoryService.search()` hydrates and re-filters `tethered ∧ ¬deleted` against SQLite, so index drift can never breach ADR 0001. Embeddings are **local and in-host** via FastEmbed/ONNX behind an `Embedder` seam (onnxruntime/tokenizers/fastembed all run on Python 3.14, unlike torch — so no separate worker is needed; the seam keeps the out-of-process option open for later RAM/crash isolation); inference runs on a `run_in_executor` pool to keep the loop free, on both the write path and the per-query read path (ADR 0006 forbids caching the query vector, but a warm embed is ~2ms). New and edited rows are searchable immediately on both paths (flat-scan of the unindexed tail), so an idempotent, SQLite-marker-driven reconciler (the sole LanceDB writer) converges the index with no visibility deadline; its periodic `optimize()` pass is pure hygiene (compaction, version pruning, folding the tail into the FTS index).

**Scheduler** — in-process, a ~30s tick polling SQLite for due work; firing a trigger spawns an ephemeral pi process. Durability/retries/backpressure live in the loop and SQLite state (no Redis). Due rows are marked `claimed` before dispatch; each job is an `asyncio` task gated behind a concurrency cap (backpressure); failures get `next_attempt_at` backoff (retries). The push half of capture → resurface.

**Time** — backend stores UTC for every timestamp; the browser supplies the offset to convert one-shot times at capture. Recurrence *rules* additionally store wall-clock time + IANA TZ, and each tick materializes the next fire as UTC (so daily/weekly survives DST).

**Frontend** — SolidJS SPA, served by the frontend image. Server state lives in `@tanstack/solid-query` (cache + invalidation), fed by the generated REST client. The single WebSocket is a *tagged event bus* (`{type: chat | invalidate | notify}`), not just chat: the host pushes dumb cache-invalidation signals from its mutation choke point, so background agent mutations (new Candidates, fired triggers) surface live without polling. The **chat transcript is host-owned SQLite data**, not pi's session (ADR 0005) — the host assembles settled messages from pi's RPC delta stream and persists them; the UI rehydrates history from REST and the WS carries only live deltas, so chat survives mobile refresh and pi restarts.

**Conversation import** — bootstraps memories/bucket items from external AI-chat exports (t3chat, Claude, ChatGPT — three provider parsers → one normalized conversation). Each conversation becomes one job draining through the **scheduler** (durability/retry/backpressure for free), idempotent on source-conversation id; import is async/backgrounded, Candidates trickle in. Extraction is **agentic** (ephemeral pi + capture tools) so it can dedup-check against the live corpus before proposing (ADR 0005). A long conversation is processed **stateless per chunk** (split on message boundaries, small overlap) — extraction state lives in the host, not pi's context — with write-time dedup-key enforcement backstopping the agent's best-effort dedup.

**Codegen** — Pydantic models are the single source of truth, feeding three consumers: the OpenAPI doc → TS API client (Solid), the tool JSON-Schemas → pi tool shims, and runtime validation (host). A `just` recipe orchestrates the cross-language pipeline (Python emits schemas → Node generators run). Generated code is committed; CI drift-checks that re-running codegen produces no diff.

**Markdown KB** — the knowledge base of tethered memories, derived **read-only** from SQLite (ADR 0003), Obsidian-compatible. Generated **synchronously** after the DB transaction commits, funneled through a single projection step (so no write path forgets), written temp-file-then-rename (atomic). Each file is **named by the Memory's UUIDv7** (`<id>.md`), an opaque stable id — the human-readable title lives *inside* the file, never in the filename, so KB consumers must not parse meaning out of the basename (ADR 0007). External edits are overwritten on next regen by design.

## Observability

Three needs, two sinks. **Logs** (agent introspection + system health): **structlog** structured JSON to stdout, captured by Docker — no aggregator yet. Every line servicing a turn carries the **pi session id + turn id** as correlation key, so background (ephemeral-pi) turns are reconstructable after the fact. pi's stdout is the RPC channel, so the host emits the agent's behavior *on its behalf*, rebuilt from pi's RPC events (`tool_execution_start/update/end`) and tool callbacks; pi's **stderr** is folded into the host log stream. **Audit** is *derived*, not a spine: per-table `created_at` + lifecycle history columns + provenance answer "what happened to X" per entity — no event-log table.

## Operations

**Backup/restore** runs *outside* the app: a backup **sidecar** on the shared volume uses SQLite's online `.backup` (consistent and WAL-safe while the app writes — never `cp` a live WAL DB), with retention/offsite via your infra; **Litestream** (continuous WAL replication) is the drop-in low-RPO upgrade. Backup set today = the **SQLite DB only** (it carries data + the canonical embedding vectors); the markdown KB **and the LanceDB search index** are derived projections, regenerated rather than backed up. Restore: drop the snapshot in place and start the app — snekql migrations bring an old schema forward, the KB regenerates, and the startup reconciliation pass rebuilds the LanceDB index from the vectors already in SQLite (no re-embed). A retained large-binary **blob store** is deferred (cross-cutting with media/provenance); when it lands it adds a second backup target.

## Security

Two separate auth domains:
- **Human → app**: defense in depth — Tailscale network isolation *plus* a single-password app login that mints a signed httpOnly session cookie (checked on REST and the WS handshake). The session layer is decoupled from the identity method, so OAuth can replace the password later. No multi-user model.
- **pi process → host**: the loopback internal tool API, authorized by a per-process secret injected at spawn; identity is the pi session id. Not reachable from the public surface.

## Models & cost

Cloud LLMs only (no local models), provider-agnostic via pi, not locked to frontier. "Self-hosted" refers to the application's deployment, not the model provider. Design for model portability: the host validates every tool input and tolerates malformed tool calls, so a weaker model can be less smart but never corrupt state.

## Decision records

- **0001** — memories are provisional (loose) until human review tethers them.
- **0002** — one agent *definition* with a tool belt; concurrency via multiple pi processes, not sub-agents.
- **0003** — SQLite is the single source of truth; markdown is a derived read-only projection.
- **0004** — two tethering paths: Review (asserted true) and Recall (proved learned).
- **0005** — pi as the agent runtime over RPC, with generated TS tool shims calling the Python host.
- **0006** — search is recomputed at the moment of use, never cached across actions.
- **0007** — knowledge-base filenames are the Memory's UUIDv7 (opaque id), not a title slug.
- **0008** — custom Starlette route contract layer.
- **0009** — hybrid Search is an embedded LanceDB projection, not FTS5 + sqlite-vec (refines 0003's retrieval-index clause).

## Build order

Spine first (capture → resurface: memories, bucket items, review, Search, scheduler, chat), verticals later. Re-grill each vertical (e.g. cooking) as it's built; cooking's glossary terms migrate from CONTEXT.md to `src/cooking/CONTEXT.md` (and a `CONTEXT-MAP.md` appears) at that point.
