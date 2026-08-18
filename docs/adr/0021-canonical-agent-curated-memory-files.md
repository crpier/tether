---
status: accepted
---

# Memory is a canonical agent-curated Markdown workspace

Tether's current Memory is the small, current understanding that it carries forward about its user. It lives canonically as an agent-organized tree of Markdown topic files, not as structured Memory rows projected out of SQLite. A file normally gathers related evidence-backed Claims under one Topic, but a useful standalone Claim may remain alone; the dreaming agent chooses names, boundaries, directories, merges, and splits. Paths are meaningful current identities rather than stable opaque IDs, so a move or rename is recorded as delete-old/create-new.

Each Markdown file has parseable YAML frontmatter with a non-empty `title`. Recognized optional fields include source `evidence` URIs, `context: always`, and `review_after`; unknown fields are preserved, and invalid optional hints are ignored. Evidence references may also appear inline for claim-level precision. These conventions guide the agent without imposing a topic taxonomy or making Claims first-class rows.

SQLite remains canonical for source evidence, Tether Conversations and Messages, user suppressions, dreaming orchestration, and typed verticals. It stores complete historical versions of memory files and rebuildable indexing/reconciliation state, but the current filesystem content and organization win whenever the two disagree. LanceDB and per-turn context are disposable projections of current files. Valid external Markdown edits are therefore authoritative human corrections, while malformed external files are preserved, excluded from retrieval, and surfaced for repair.

Dreaming automatically assimilates settled evidence and maintains the workspace; it does not create a Review inbox. Authority is explicit correction or instruction, then explicit user assertion, appropriately scoped verified evidence, repeated implicit patterns, and finally assistant output as context only. Prior memory prose is state to revise, never independent evidence. User controls replace item approval: **Don't remember this** retains source conversation while suppressing and deleting affected memory files; **Delete this everywhere** also purges source material and every derived or historical copy.

This supersedes ADRs 0001 and 0004, ADR 0003 for Memory, ADR 0007, and ADR 0010. It amends ADR 0012: raw Telemetry still remains in typed vertical storage, but evidence-backed Distillations produced from it may now be applied automatically through Dreaming rather than entering a loose-to-tethered gate. Recall may remain a learning workflow, but no longer determines whether content enters Memory.

## Consequences

- There are no loose/tethered Memory states, Memory Review queue, stable Topic IDs, first-class Claim rows, or compatibility migration from the old Memory table. Production starts with an empty workspace.
- Topic quality and organization are prompt-shaped and maintained over time rather than schema-shaped.
- Exact source records stay durable outside Memory; deleting a topic does not normally delete its evidence.
- Whole-file deletion is the correction and erasure granularity. A later dream may reconstruct unrelated material from remaining evidence.
- Obsidian, Neovim, and similar editors can operate directly on canonical files; a built-in editor is not required.
