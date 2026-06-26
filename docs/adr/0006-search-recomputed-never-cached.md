# Search is recomputed at the moment of use, never cached across actions

Every action that needs context re-runs its Search against the live corpus from scratch; no component caches, snapshots, or assumes the result of a prior Search. Memoizing a Search *within a single action* is fine (it is still "the moment of use" for that action), but nothing survives the action that produced it.

We accept recomputing Search on every action to buy two things: **freshness** (a tether, edit, or delete is reflected on the very next Search with no cache to invalidate) and **edit-safety** (because the corpus is never assumed, mutating a Memory can never leave a stale trusted view behind). This is what makes Memories freely editable without a cache-invalidation protocol — the property ADR 0001's trust gate depends on holding at *read* time, not just write time.

## Consequences

The obvious reflex for Search latency is to cache results; this ADR forbids that. If Search becomes a measured bottleneck, the answer is a faster query path (better indexes, the FTS5 + `sqlite-vec` hybrid), not a cache layer. A persistent Search cache would reintroduce exactly the stale-trusted-view problem this decision exists to prevent and must instead be reversed here first.
