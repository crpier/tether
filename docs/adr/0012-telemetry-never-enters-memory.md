# Telemetry stays in vertical tables; only gated Distillations enter the memory pool

Ingestion gates (Health Connect, and future sources) will bring in raw time-series — heart rate, location, sleep stages — at a volume and shape nothing like a Memory: many points per minute, no standalone meaning, and no use to Search's relevance ranking. Landing it as Memories (even machine-synced, trusted-at-capture ones per ADR 0010) would flood the pool with rows that are never individually recalled and drag every Search over a corpus that no longer fits its purpose.

We keep raw Telemetry entirely in **vertical tables** — typed, source-specific storage outside the Memory pool. Nothing about it becomes a Memory as-is. Only a **Distillation** — an agent-derived conclusion drawn from Telemetry or a **Fusion** (cross-source correlation, e.g. location × heart rate) — can enter the memory pool, and it enters as **agent-inferred** provenance, so it takes the loose→tethered gate like any other agent guess (ADR 0010). The raw readings behind a Distillation stay queryable in their vertical table as its evidentiary basis; they are never promoted themselves.

This is hard to reverse in practice: Telemetry cannot be backfilled (a missed sync window is permanently missed), so the ingestion-side schema for a vertical's raw data has to be right from the first sync, well before any Distillation logic exists to consume it.

## Considered options

- **Land raw Telemetry as machine-synced Memories** — rejected: volume mismatch with the Memory pool's purpose (a corpus of individually meaningful, searchable facts), and it would make ADR 0010's "machine-synced is trusted at capture" apply to noisy sensor readings the agent hasn't yet made sense of.
- **Skip storing raw Telemetry, keep only Distillations** — rejected: throws away the evidence a Distillation is based on, and Telemetry can't be backfilled if a future Distillation approach needs the raw data a past one didn't use.
- **Cache/derive Distillations on read instead of storing them** — rejected: contradicts ADR 0006 (Search is recomputed, not cached) only superficially; a Distillation is a conclusion the agent commits to, not a search result, and it still needs its own loose→tethered trust lifecycle, which requires it to exist as a stored Memory.

## Consequences

- Every new Telemetry source needs its own vertical table before it can sync at all — there is no generic "just write a Memory" shortcut, which is a deliberate floor to keep the Memory pool's shape stable.
- Distillation and Fusion logic can evolve freely (new correlations, better inference) without touching how raw Telemetry was captured, since the raw data was never shaped around any particular Distillation.
- The Memory pool's size stays bounded by human-meaningful facts and agent conclusions, not sensor volume, which is what keeps Search (ADR 0006, recomputed every time) fast without needing telemetry-aware filtering.
