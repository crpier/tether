# Architecture Notes

These notes describe the current implementation direction for Tether's backend.
They are intentionally lighter than ADRs and can evolve as the project teaches us
more. The durable "why we own chat data" rationale lives in `docs/adr/0001`.

Tether's backend is built in **Python** — Starlette (ASGI) + Pydantic, managed with
`uv`, tested with `snektest`, local-first on SQLite + Markdown.

## Runtime and Repo Shape

Tether is a monorepo with two apps:

- `apps/server` — **Python** backend: HTTP API, persistence, and the Pi assistant
  integration. Managed with **`uv`**; async-safe throughout (see the `write-python`
  conventions). Runs on **Starlette (ASGI)** under `uvicorn`.
- `apps/web` — SolidJS/TypeScript frontend on Vite, in the root `pnpm` workspace.

Domain logic starts in `apps/server` while the model is young. Extract modules only
when repetition proves the boundary.

## API Contract Between Backend and Frontend

The backend speaks HTTP under an **`/api` prefix** (so static-file serving never
shadows it), with **Pydantic models as the DTOs** at every boundary.

- **Starlette routes** handle requests; each route validates input by parsing it into
  a Pydantic model and serializes responses from Pydantic models. No domain logic
  lives in routes.
- DTOs are defined **once, in Python**. Unlike the eventual TypeScript target, there
  is no shared type that produces a typed client across the language boundary, so the
  web app maintains its own request/response types. If drift becomes painful, emit an
  **OpenAPI schema** from the Pydantic DTOs and generate the web client types from it
  — defer this until a second hand-sync actually hurts.
- Convert domain exceptions to HTTP responses **at the route boundary**, not inside
  application services.

### Dev and prod serving

- **Dev:** two processes — Vite for `apps/web`, `uvicorn` for `apps/server`. Vite's
  `server.proxy` forwards `/api/*` (and the chat stream path) to the backend, so the
  frontend always talks to a same-origin `/api`.
- **Prod (local-first, single machine):** the backend serves the built web assets as
  static files from the same origin — one process, one port.

### Streaming the Conversation

- Sending a user message is an ordinary **POST**.
- The assistant turn streams back over **SSE** (Starlette `StreamingResponse`) as a
  typed event union (`message-delta`, `tool-call-started`, `tool-call-result`,
  `turn-complete`, `error`), each event a Pydantic model. SSE (not WebSocket) because
  the flow is server→client only and passes cleanly through the Vite dev proxy and the
  prod static setup.
- The SSE event models are **shared with persistence**: the same events written as
  Conversation message/tool-call records are the ones serialized onto the wire. The
  browser consumes the stream via `EventSource`/fetch-stream.

## Python Structure

The `write-python` conventions are the rule. Concretely, for this backend:

- **Ports are `Protocol`s, wired by dependency injection** — `MemoryRepository`
  (SQLite), `MemoryDocumentStore` (filesystem), `Clock`/`IdGenerator`, `Config`, and
  later `AssistantRuntime`. A real adapter and a fake can satisfy the same `Protocol`.
- **Application services (use cases)** — `CaptureMemory`, `RecallSearch`, … — are
  async callables composing the ports. Domain policy (e.g. write ordering) lives here,
  not in adapters or routes.
- **All IO is async-safe.** SQLite has no first-class async driver; access it through
  `aiosqlite` or run the blocking `sqlite3` work in an executor — never block the
  event loop.
- **Boundary data is Pydantic:** HTTP DTOs, persisted-row parsing, config. Prefer
  custom domain types over bare primitives for validated concepts.
- **Errors are domain exceptions** raised from services and translated to HTTP at the
  route boundary — not stdlib exceptions, not raw HTTP errors deep in the stack.
- **Config is required.** The local state root is required configuration; the server
  **fails startup** when it is unset rather than choosing a default.

Use plain functions for pure, total, synchronous work (slug generation, path
derivation, text munging). Rule of thumb: no IO, can't fail, holds no resource → plain
function.

## Persistence

Tether is local-first: SQLite plus Markdown files with split authority.

- **Markdown owns** authored Memory content (the Memory Document).
- **SQLite owns** IDs, paths, deletion state, audit/index metadata, queryable fields.

### Approach

- SQLite is accessed **async-safely** (`aiosqlite` or executor-wrapped `sqlite3`); one
  connection/session owns transactions.
- **Schema lives in code**; migrations are **forward-only, applied on boot**.
- Keep hand-written SQL behind the `MemoryRepository` port so the eventual TypeScript
  rewrite swaps the adapter, not the callers.

Specific library choices (query helper, migration tool) are open; keep them light
and behind the `MemoryRepository` port.

### Split-authority write ordering (file-first)

A Memory write touches two stores that cannot share a transaction (a SQLite commit and
a filesystem write). The rule: **make the only reachable failure a harmless one.**

1. Write the Memory Document **atomically**: temp file in the same directory → `fsync`
   → `rename()` into the final path → `fsync` the directory.
2. **Then** commit the SQLite row in a transaction.

A Memory is "real" only when it has a SQLite row, so the only reachable crash state is
an **orphan `.md` file with no row** — invisible, not searched, not recalled,
sweepable later. The reverse ordering would leave a *visible* row pointing at a missing
Document, which is not tolerable.

Consequences:

- The path derives from the Memory's **immutable ID** (`{id}.md` or `{id}-{slug}.md`,
  slug cosmetic), generated up front via the `IdGenerator` port — never from the
  mutable title (paths are stable across title changes).
- Edits use the same atomic temp-write-and-rename, then update DB index fields.
- Soft-delete is **SQLite-only** (set deletion state); the Document stays on disk — a
  single-store write with no consistency problem.
- The orphan-file **sweeper/reconciliation is deferred**; the DB-commit-failed path
  must log loudly so orphans are traceable. v0 does not promise automatic
  reconciliation of external Markdown edits.

## Assistant Runtime (Pi)

Chat is Tether's front door: one continuous Conversation, no user-facing session
management. Pi — [`@earendil-works/pi-coding-agent`](https://pi.dev), a Node terminal
agent — runs behind an `AssistantRuntime` port. Tether drives it as a **subprocess in
RPC mode** (`pi --mode rpc`): newline-delimited JSON commands on stdin, streamed events
on stdout. RPC is the chosen transport because it needs no Node code on the conversation
path — the Python backend speaks the JSONL protocol directly.

### Driving Pi over RPC

- **Framing is strict JSONL**: split records on `\n` only and strip a trailing `\r` if
  present. Pi payloads can contain `U+2028`/`U+2029`, so generic line readers that treat
  them as newlines corrupt the stream.
- On user input, Tether **persists the user message first**, then sends
  `{"type":"prompt","message":…}`. Pi streams back `message_update` (text / thinking /
  tool-call deltas), `tool_execution_start|update|end`, `turn_end`, `agent_end`,
  `compaction_*`, and `queue_update` events.
- Tether **re-projects Pi's events onto its own SSE union** (`message-delta`,
  `tool-call-started`, `tool-call-result`, `turn-complete`, `error`) for the browser, and
  persists assistant messages and **first-class tool-call records** (name, args, result,
  status, provenance) as they finalize.

### Conversation ledger vs Pi sessions

For the initial phase Tether uses **Pi's native sessions** instead of reconstructing
context itself — Pi manages the working context and **autocompaction**, which is far less
to build. Ownership still follows ADR 0001: **Tether keeps its own conversation ledger.**

- **The UI shows one continuous Conversation; day-sessions are invisible.** The web app
  renders a single unbroken thread from **Tether's ledger**, spanning every day. Pi's
  per-day sessions are purely a backend detail — there is no user-facing session management
  (CONTEXT.md holds).
- **One Pi session per day, backend-side.** All chats on a given day share one Pi session
  (named by date); each new day starts a **fresh** one. Pi autocompacts when its context
  window fills.
- **Pi owns the runtime session; Tether owns the durable record.** Tether is already
  observing every RPC event (to stream SSE and persist tool-call records), so it appends
  each turn to its own ledger as a side effect. A Memory captured from chat points its
  Source Ref at **Tether's ledger entry**, not a Pi session file — chat is never hostage to
  the runtime (the coupling ADR 0001 exists to avoid).
- **Autocompaction is non-destructive to the record.** It shrinks only what the *model*
  sees; the full history remains in Pi's JSONL and in Tether's ledger.
- **A day boundary resets the *model's* working context, not the displayed thread.** The
  UI stays continuous, but because each day is a fresh Pi session the assistant does not
  natively hold prior days in its context window; its awareness of older context comes from
  Memories surfaced via `recall_search`. If the user references earlier scrollback that was
  never captured, the model won't have it until Tether-driven reseed (forking yesterday's
  session, or replaying specific turns) lands — deferred for the initial phase.

Tether tracks the day→session mapping itself: it resumes the day's session with
`pi --mode rpc --session <id>` (or rolls to a fresh one via the `new_session` command at
the day boundary), recording the session id from `get_state`.

### Tools: the bridge extension

Custom domain tools **cannot be registered over RPC** — Pi's RPC surface only toggles its
built-ins (`read`/`bash`/`edit`/…). A tool the model can call (`capture_memory`,
`recall_search`, …) must be registered Node-side via `pi.registerTool()`. So Tether ships
**one generic bridge extension** (TypeScript, loaded with `-e`) that:

- registers Tether's domain tools with Pi (definitions supplied by Tether), and
- in each tool's `execute()`, **forwards the call to a Tether internal loopback endpoint**
  (`127.0.0.1`, distinct from the public `/api`) that runs the *same application service*
  the web path uses, then returns its result as the tool result. Partial output streams via
  `onUpdate`; `ctx.signal` propagates abort.

The bridge is a thin, write-once forwarder; **all tool logic stays in Python.** A Memory
captured via chat and one captured via the web form run the identical application service,
differing only in the Source Ref they stamp.

### Tool execution: platform primitives vs envelope

The old project's tool envelope is kept as a concept but relocated:

- **Durable guarantees are platform primitives** applied uniformly to write use cases
  via a `Command` write-seam: idempotency check → run in a transaction → record an
  audit event → mint an undo token → stamp provenance. Identical regardless of entry
  adapter (web `/api` or the internal tool endpoint).
- **The "envelope" is the assistant-facing result serialization** the internal tool
  endpoint returns to the bridge (`{ ok, request_id, result, provenance, audit_event_id,
  undo_token, error }`), which the bridge hands back as the tool result. Public HTTP
  responses surface the same underlying values in their own per-endpoint DTO shapes.

On the boundary principle: assistant-facing tools and the public API remain **separate
adapters over shared application services**. The assistant never calls the public `/api`;
the bridge calls a dedicated **internal** tool-dispatch endpoint, which is simply the IPC
transport across the Node→Python boundary into the identical use cases.

## Testing

- **Real adapters by default**: integration tests through the application service using
  real SQLite on a temp file and a real filesystem in a tmp dir, via `snektest`. The
  risk in this app is the *consistency between* the two stores, which is meaningless
  against mocks.
- Reserve **in-memory / fault-injecting port fakes** for failure invariants that real
  adapters can't easily produce (e.g. injecting a DB-commit failure after the file
  write to prove the orphan-only invariant).
- Pure functions get plain unit tests.
- Follow the `write-python` testing rules: one behavior per test, fixtures for setup,
  no umbrella tests.

## Privacy Boundary

All durable state (Memories, Memory Documents, SQLite, indexes, chat history) lives on
machines the user controls. Model inference may use cloud LLM APIs as transient
traffic. Nothing durable is stored on third-party services.

## Build Order

Slice 1 is the **Memory Capture + Conversation slice** — the persistence base and the Pi
loop together, because the model capturing and recalling Memories in chat is the point of
the product. Build it in this internal order:

1. **Persistence base (no Pi):** create / list / get / edit / soft-delete / lexical Recall
   Search over the `/api` HTTP surface + a minimal web view. Every Memory here is created
   manually and is born Tethered (`CONTEXT.md`). This front-loads the unproven
   integrations — async SQLite access, file-first ordering, on-boot migrations.
2. **Pi conversation loop:** `pi --mode rpc` behind the `AssistantRuntime` port using Pi's
   native per-day session (autocompaction on), events re-projected to the web over SSE
   while Tether records each turn to its own ledger.
3. **Bridge extension + internal tool endpoint:** expose `capture_memory` and
   `recall_search` so the model captures and recalls Memories in chat, over the same
   application services as the web path.

Build the `Command` seam throughout, but implement only **provenance + a minimal audit
record**; defer idempotency and undo tokens to the slice that introduces
automatic/assistant-reviewed Capture.

Loose Memories, Review, automatic Capture, rejection records, and Connections come in
later slices; persistence should not preclude them, but no machinery for them is built
yet.
