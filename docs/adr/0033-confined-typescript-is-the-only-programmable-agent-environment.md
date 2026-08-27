---
status: accepted
---

# Confined TypeScript is the only programmable agent environment

Tether uses fresh confined TypeScript/JavaScript programs as its only programmable agent environment. Direct host tools remain available for simple calls, while programs handle sequencing, parallelism, filtering, and aggregation over the same generated tool catalog. Tether will not add Bash or another shell runtime.

Confined programs receive capabilities, not ambient filesystem access. Memory reads cross typed host tools that return reconciled current Topic projections. Programs may apply JavaScript string, regular-expression, collection, and JSON operations to those results. Richer Memory reads must deepen the host tool interface with bounded listing, paging, or exact-Topic retrieval rather than expose `/data/kb/memory` or Node filesystem modules.

Dreaming remains Memory's sole writer. Its model returns bounded curated text; the Python host validates the result, writes canonical Markdown, and records complete mutation history. Foreground programs cannot write, move, or delete Memory files.

This supersedes ADR 0022 and refines ADR 0032. It rejects the planned Bubblewrap shell and native-shaped Dreaming filesystem tools because the capability-only interpreter has less authority and keeps validation, reconciliation, and persistence behind host interfaces.
