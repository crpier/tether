# Verticals stay fully bespoke; shared structure is convention, not code, with named activation triggers

The Teaching vertical, Bucket items, and the upcoming Project vertical each reinvent the same shapes: lifecycle states (`active`/`completed`/`abandoned`), typed relationships (`lesson.supersedes`, project `blocked_by`, trigger→step links), per-kind closed registries, and per-domain panels. #233 asked whether Tether should extract cross-cutting building blocks — notably a generic typed edge/link table with a closed edge-kind registry (the one deliberate stored-structure exception to ADR 0006's search-not-graph stance ever seriously considered) and a shared lifecycle convention — so verticals become thin wrappers over shared infrastructure, or whether verticals stay fully bespoke. This ADR decides the extraction stance across three surfaces: edge storage, lifecycle, and vertical code shape.

The answer is **bespoke per vertical, governed by documented convention, not shared code** — on all three surfaces. This is "conventions, not code" (ADR 0015) adopted as the standing default, not a one-off call for teaching. Each surface gets a named, explicit trigger for revisiting the decision, rather than an open-ended "maybe later."

## Edge storage: bespoke per-vertical link tables, not a generic edge table

Relationships stay real foreign keys in typed, per-relationship link tables — one table per relationship kind, e.g. `lesson_supersedes`, `project_blocks` — governed by a shared shape convention (naming and column pattern) that lives in CONTEXT.md, not in code. The generic typed edge table with a closed edge-kind registry (the contemplated deliberate exception to ADR 0006) is **not** pre-built.

All edges that exist today are intra-vertical: a lesson supersedes another lesson in the same curriculum, a project blocks another project. A polymorphic edge table buys nothing over a real FK for these — it costs real integrity (no FK pointing at "whichever table `kind` says today") and invites a junk-drawer graph where anything can point at anything with no schema pressure keeping it honest.

**Activation trigger**: the first genuine cross-vertical edge — a relationship that cannot be expressed as an FK within one vertical's tables because it spans two. This is foreseen medium-term (Project blocking on a Teaching curriculum, say), not short-term; nothing on the roadmap needs it yet.

## Lifecycle: convention with a base set, not shared code

Every lifecycle-bearing vertical uses a `status` column. The base set of states is exactly `active` / `completed` / `abandoned`, with those names and semantics fixed across verticals. A vertical may add domain-specific states beyond the base set — Teaching's `learning_record.status` already adds `superseded` alongside `active`. There is no shared enum type, base class, or lifecycle library: each vertical declares its own typed enum, and the convention (base-set names, semantics, extension is additive-only) is documented in CONTEXT.md, not enforced in code.

The base set is expected to evolve in place as real verticals surface refinements to it; no backfill of existing tables is anticipated when it does; a convention update lands as a CONTEXT.md edit plus a decision for what it means going forward, not a migration.

Advisory-only status (a comment, a wiki note) drifts silently and blocks any future cross-vertical view ("show me everything abandoned") from ever being written reliably — hence a real column with fixed names is worth requiring. Shared lifecycle *code* is rejected as the entity-framework slippery slope: a shared status type invites a shared status-transition service, which invites a shared "entity" base table, which is exactly the generic framework this project's single-user "works for me" posture keeps rejecting.

## Vertical code shape: fully bespoke, no framework

A vertical stays a hand-built typed slice: its own tables, its own enums, its own panel. Idioms may be shared — the ADR 0014 closed-registry idiom (per-kind specs concatenated centrally, `ToolSpec`-shaped) is reused as a *pattern* by every vertical that needs a registry — but the implementation is never imported from a shared layer. Each vertical writes its own registry module, repeating the shape rather than depending on a common one.

**Extraction trigger**: rule of three. Shared code gets extracted only when a third vertical duplicates a shape already proven mechanical and stable across the first two — not on the second occurrence, and not speculatively ahead of a second. Extraction is itself a deliberate ADR-level act when it happens, not a refactor slipped into unrelated work. ADR 0015's deferred `(source_kind, source_id)` generalization for `StudyItem` is the template precedent: room was left, but the generalization itself waits for a second consumer and its own decision.

Two data points can't distinguish a real pattern from coincidence — the second vertical might look like the first only because it's early days for both. A wrong abstraction, once three verticals depend on it, costs more to unwind than the duplication it was meant to save, in a codebase built and read by one person.

## Considered options

- **Build the generic typed edge table with a closed edge-kind registry now** — rejected: every edge in scope today is intra-vertical, so the generic table has no genuine cross-vertical case to serve yet; it would trade real FK integrity for a `kind` column and a lookup, on spec.
- **A shared lifecycle base table or enum type (mini entity framework)** — rejected: the value of the base-set convention is the fixed vocabulary, not shared plumbing; a shared type is the first step toward a generic "entity" abstraction this project has repeatedly declined to build (ADR 0013's refusal to generalize telemetry storage makes the same call for a different surface).
- **Extract a shared vertical framework (base tables, generic CRUD, generic panel scaffolding) once Teaching and Project both exist** — rejected: two verticals is not enough evidence that a given shape is a pattern rather than a coincidence of two designs done close together; rule of three applies, and extraction happens as its own decision when a third instance duplicates a proven-stable shape.
- **No conventions at all, purely ad hoc per vertical** — rejected: without a documented base lifecycle set and link-table shape, "everything abandoned" and similar cross-vertical views become permanently unanswerable, and each new vertical reinvents naming from scratch instead of following a written pattern.

## Consequences

- No generic typed edge table or edge-kind registry gets built as part of this decision; Project's `blocked_by` and any other near-term relationship are plain per-relationship link tables, same shape as Teaching's `supersedes` FK.
- The link-table shape convention (naming, column pattern) and the lifecycle base-set convention (`active`/`completed`/`abandoned` fixed names and semantics, additive domain extensions) need to be written into CONTEXT.md as follow-up — this ADR records the decision and rationale; codifying the convention text in CONTEXT.md's `## Language` (or an adjacent conventions section) is separate follow-up work, matching how ADR 0013/0014/0015 each landed as ADR-only PRs with any CONTEXT.md canonization done separately.
- Every new lifecycle-bearing vertical must declare its own `status` enum including the exact base-set names; there is no shared type to import, so this is a manual convention check at review time until/unless a linter or codegen check is added.
- The `(kind, spec)`-registry idiom gets re-implemented per vertical (Teaching's tool belt, a future Project registry, etc.); expect near-identical registry boilerplate across verticals until the rule-of-three trigger fires on a third instance.
- Revisiting any of these three calls requires hitting its named trigger (first cross-vertical edge; lifecycle base-set refinement surfacing from real use; a third vertical duplicating a stable shared shape) — not a general "this feels like a lot of duplication" impression.
