# pi is the agent runtime, driven over RPC, with generated TS tool shims calling the Python host

The agent loop is **pi** (earendil-works/pi, a coding agent) run in its **RPC mode** as a subprocess of a **Python host**. pi drives the loop ("pi drives, host serves tools"); the host owns all state and logic. Because pi's RPC protocol cannot register host tools, every Tether capability is a **pi extension** (`pi.registerTool`) whose `execute` is a thin shim that calls back into the host over a loopback HTTP **internal tool API**; all behaviour, validation, and state live in the host (SQLite, ADR 0003). pi runs with its built-in tools (bash, file edit) disabled — a **closed tool world** whose only surface is Tether's tools.

Identity and authorization are injected when the host spawns a pi process: the **pi session id** is the correlation key (the host keeps a registry of which sessions exist and what each maps to, but pi owns session lifecycle), and a **per-process secret** authorizes the loopback callbacks. Tool param schemas have a single source of truth — the host's **Pydantic** models — from which the pi TS shims and the OpenAPI/TS client are generated; the shims are committed and drift-checked, never hand-written.

## Why

pi gives a maintained agent loop (streaming, steering/abort, sessions) for free, and its extension model lets every capability be a typed tool — which is exactly Tether's "one agent, one tool belt" stance (ADR 0002). Keeping all logic in the Python host (not in the TS extensions) means the testing seam stays trivial — call the host tool endpoints directly, no LLM, no pi — and the agent cannot bypass the review/candidate gates, because the gates are host code and the agent's *only* side effects are host tool calls. A closed tool world makes that guarantee real and keeps behaviour deterministic for tests. Generating the shims from Pydantic removes the "same contract written twice" drift between pi's TypeBox schemas and the host's validation.

## Trade-offs and alternatives rejected

- **Embedding pi's `AgentSession` SDK in-process** was not chosen because the host is Python and pi is TypeScript; RPC is the language boundary that lets the host stay Python. The cost is RPC's limits (no host-registered tools — hence the extension shims — and some TUI methods are no-ops) and the latency of subprocess JSONL framing.
- **Topology A (host drives the loop, pi only completes)** would keep tool calls inside host transactions directly, but pi's RPC mode is built around pi driving; fighting that would mean reimplementing the loop. Accepted: pi drives, the host reaches consistency through the tool API instead.
- **Multiple coordinating agents** — rejected in ADR 0002; this ADR is how the single-agent-definition decision is physically realized.

## Why it is hard to reverse

The tool layer, the testing seam, the review/candidate gate enforcement, the spawn-time identity/secret scheme, and the codegen pipeline all assume this boundary. Moving the loop into the host, or letting tools hold logic on the TS side, would touch every tool and every test. pi being a third-party coding agent (closed tool world, RPC limits, extension API) is a dependency baked into the architecture, not an implementation detail. The Python↔pi boundary is a process boundary kept *inside one container* (host + Node/pi co-resident) precisely so the spawn/secret/session design works; splitting pi into its own container would force a pool-service redesign.
