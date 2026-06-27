# Hybrid Search is an embedded LanceDB projection, not FTS5 + sqlite-vec

Tether's hybrid Search (lexical + semantic, merged via RRF) is served by an **embedded LanceDB dataset** under `.tether/index/`, rather than the previously-planned in-SQLite FTS5 + `sqlite-vec` virtual tables. SQLite remains the single source of truth (ADR 0003): it holds Memory rows *and* the canonical embedding vectors (BLOB); LanceDB is a **derived projection** — gitignored, excluded from backup, rebuilt from SQLite on restore (no re-embed) — strictly parallel to the markdown KB. This refines, but does not reverse, ADR 0003: the one clause that placed "the retrieval index" inside SQLite no longer holds; everything else about 0003 stands.

We chose this because exposing FTS5/`vec0` virtual tables through snekql turned out badly — virtual tables don't fit snekql's typed model, leaving the lexical path as an untyped raw-SQL seam we didn't want to own. LanceDB ships an abi3 wheel (`cp39-abi3`) that runs on the host's Python 3.14 alongside `pyarrow` (cp314), so the index lives **in-host** behind a thin typed adapter (`SearchIndex`) — the only module importing `lancedb`. LanceDB also gives us native FTS, RRF fusion, and a multimodal path (a future photo-search option) for free.

## Embeddings run in-host

Embeddings are computed **in the host process** via FastEmbed (ONNX), not an out-of-process worker. We originally planned a separate embedder process on the assumption that the ML wheels lag Python 3.14. That premise was tested and does not hold: `onnxruntime` ships `cp314` wheels, `tokenizers` ships `cp310-abi3` (runs on 3.14), and `fastembed` is pure-Python — all three install and run together with `lancedb` in one 3.14 environment. Measured on that stack: model load ~3.5s once at startup, a warm single-query embed ~2.3ms. (This is specific to the FastEmbed/ONNX path; `torch`/`sentence-transformers` *do* lag 3.14, which is why we don't use them.)

Embedding is CPU-bound, so inference runs on a `run_in_executor` thread pool to keep the asyncio loop free — onnxruntime releases the GIL during inference, so this actually parallelises. The embedder sits behind a thin `Embedder` protocol so it can be moved out-of-process later (RAM isolation on a small VM, or native-crash isolation) without touching callers; for single-tenant scale that move is not justified today.

## Considered Options

- **FTS5 + `sqlite-vec` (status quo, the original #12 plan)** — rejected: forces virtual tables and raw SQL outside snekql's typed model, and `sqlite-vec`'s ANN exists for a scale (10–50k Memories) we never hit; brute-force exact cosine would be sub-millisecond and *more* accurate.
- **FTS5 + numpy BLOBs** — keeps one store, but still drags the FTS5 virtual-table seam into snekql.
- **Pure-Python BM25 (`bm25s`) + numpy cosine** — fully typed, single store, no extensions; viable, but we preferred offloading lexical + vector + fusion to a purpose-built store given the multimodal upside.
- **Meilisearch / Qdrant (server)** — best clients and typo tolerance, but a separate service to run and back up on a single-tenant $15/mo VM, for no relevance gain at our scale.
- **Out-of-process embedder worker** — rejected once the 3.14 premise was disproven (above): it adds an IPC surface, a read-path process hop, and a worker-down fallback path, for isolation benefits single-tenant doesn't need yet. Kept reachable via the `Embedder` seam.

## Consequences

- **No vector ANN index.** At our scale LanceDB flat-scans exact cosine in single-digit milliseconds; the default `create_index` is IVF_**PQ** (lossy quantization that measurably degrades exact matches and rank). We build only the **native FTS index**; vectors are flat-scanned. (LanceDB 0.33 removed Tantivy FTS — native FTS is the only option.)
- **New and edited rows are searchable immediately.** Native FTS flat-scans the unindexed tail and vector search flat-scans all rows, so a freshly tethered/edited Memory is findable on both paths *before* any `optimize()`. `optimize()` is therefore background hygiene only — fragment compaction, version pruning, folding the unindexed tail into the FTS index for speed — with **no visibility deadline**; a lazy periodic pass suffices.
- The index is a disposable projection: "schema migration" is **drop-and-rebuild from SQLite**, never in-place ALTER; an `index_schema_version` / active-embedding-model marker in a `search_meta` table triggers it (a model change re-embeds the whole corpus, since vector spaces can't be mixed).
- Reads treat LanceDB as a **candidate generator** only: `SearchIndex.search()` returns ids+scores, which `MemoryService.search()` hydrates and re-filters `tethered ∧ ¬deleted` against SQLite — so index drift can never violate ADR 0001, only cost a missing/extra candidate. The per-query embed happens in-host (ADR 0006 forbids caching it); there is no separate process to fall back from.
- A second derived store means dual-write can't be transactional: an idempotent, SQLite-marker-driven reconciler (sole LanceDB writer) converges the index, backstopped by startup and periodic passes; the same passes run `optimize()`.
