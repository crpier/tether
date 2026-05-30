# PRD: Memory Capture and Recall v0

## Problem Statement

Tether needs its first meaningful feature slice: a way to Capture durable personal context and bring it back through Recall Search. Today the rewrite has only scaffolding, so there is no usable path for saving a Memory, storing its Memory Document, listing active Memories, editing mistaken content, or soft-deleting a Memory that should no longer appear in normal use.

The user needs this first slice to be useful enough to play with while also establishing the backend, persistence, and UI patterns that later Tether features can build on.

## Solution

Build Memory Capture and Recall v0 as a small end-to-end product slice.

The user can open Tether, create a Memory with title, body, tags, and optional Source Refs, see recent Memories, search active Memories lexically, inspect a Memory, edit title/body/tags/source refs, and soft-delete Memories. Tether persists each Memory to both SQLite and a stable Markdown Memory Document using split authority: Markdown owns authored content, SQLite owns operational metadata and queryable fields.

This slice should prioritize boring usefulness over generalized artifact architecture. It should create clean seams for later semantic search, pi tools, audit events, and richer artifact relationships without implementing those now.

## User Stories

1. As the Tether user, I want to create a Memory quickly, so that I can save useful personal context before I lose it.
2. As the Tether user, I want a capture-first page, so that saving context is the default action when I open the app.
3. As the Tether user, I want to enter a Memory title, so that I can recognize the Memory later.
4. As the Tether user, I want to enter a Memory body, so that the saved context can stand alone.
5. As the Tether user, I want to add tags to a Memory, so that I can loosely organize saved context.
6. As the Tether user, I want tags to be simple text values, so that tagging does not interrupt Capture.
7. As the Tether user, I want to save a Memory without Source Refs, so that manual capture remains low-friction.
8. As the Tether user, I want to optionally add Source Refs, so that I can preserve where a Memory came from.
9. As the Tether user, I want manual entry to be a valid Source Ref type, so that directly-created Memories still have provenance.
10. As the Tether user, I want Source Refs to support URLs, files, conversations, and pi sessions, so that future assistant and import workflows can preserve origin context.
11. As the Tether user, I want each Memory to produce a Markdown Memory Document, so that my content remains inspectable outside the app.
12. As the Tether user, I want each Memory Document path to stay stable, so that links, backups, and git history remain meaningful.
13. As the Tether user, I want Memory filenames to include a readable slug, so that browsing the vault by hand is pleasant.
14. As the Tether user, I want Memory filenames to include a stable ID, so that identity does not depend on title uniqueness.
15. As the Tether user, I want SQLite to track operational metadata, so that the app can list, search, edit, and delete Memories reliably.
16. As the Tether user, I want recent active Memories listed after capture, so that I can confirm what I saved.
17. As the Tether user, I want to search Memories by lexical Recall Search, so that I can bring saved context back into attention.
18. As the Tether user, I want Recall Search to search title and body, so that I can find Memories by their main content.
19. As the Tether user, I want Recall Search to search tags, so that organization metadata helps retrieval.
20. As the Tether user, I want Recall Search to exclude Deleted Memories, so that removed context does not resurface unexpectedly.
21. As the Tether user, I want to inspect a Memory's detail, so that I can read the full context and metadata.
22. As the Tether user, I want to edit a Memory title, so that typos or vague titles can be corrected.
23. As the Tether user, I want to edit a Memory body, so that saved context can be clarified without delete/recreate friction.
24. As the Tether user, I want to edit Memory tags, so that organization can improve over time.
25. As the Tether user, I want editing a Memory not to rename its Markdown file, so that the file path remains stable.
26. As the Tether user, I want edited Memories to update both SQLite and Markdown, so that app state and owned files stay aligned for app-written changes.
27. As the Tether user, I want to soft-delete a Memory, so that mistaken or obsolete content disappears from active use without destructive data loss.
28. As the Tether user, I want soft-deleted Memory Documents to remain on disk, so that v0 deletion is reversible at the data level even if restore is not exposed in the UI.
29. As the Tether user, I want Deleted Memories hidden from normal list and search views, so that v0 behaves like deletion from the user's perspective.
30. As the Tether user, I want clear failure feedback when a Memory cannot be saved, so that I know whether context was captured.
31. As the Tether user, I want the first implementation to be local-only, so that private personal context is not sent elsewhere.
32. As a future pi assistant integration, I want Memory operations to have clean application service boundaries, so that tools can call the same behavior as the REST API later.
33. As a future semantic search implementation, I want Recall Search behind an internal port, so that lexical v0 can evolve without changing product language.
34. As a developer learning Effect, I want the feature built with Effect services and layers, so that the first slice teaches the intended backend style.
35. As a developer maintaining Tether, I want persistence behavior isolated behind deep modules, so that SQLite and Markdown details are testable without coupling them to HTTP handlers.
36. As a developer maintaining Tether, I want shared schemas for API-facing Memory shapes, so that server and web agree on data contracts.
37. As a developer maintaining Tether, I want the Solid UI to call normal REST endpoints, so that the browser interface stays simple before assistant tools are added.
38. As a developer maintaining Tether, I want validation on create and update inputs, so that invalid Memory records are rejected before persistence.
39. As a developer maintaining Tether, I want startup to initialize required local directories and database schema, so that first run is smooth.
40. As a developer maintaining Tether, I want predictable local state locations, so that backup, inspection, and later migration are straightforward.

## Implementation Decisions

- Build this as the first end-to-end feature slice in the existing pnpm monorepo.
- Use the current stack direction: TypeScript, Effect, Effect HTTP, SolidJS, SQLite, and Markdown.
- Keep domain and application logic in the server app for now. Extract shared libraries only when boundaries prove themselves.
- Use REST endpoints first for the Solid UI. Tool-envelope and pi custom tool endpoints are deferred but should be easy to wrap around the same application services later.
- Treat a Memory as durable, inspectable personal context suitable for future recall.
- Use split persistence authority:
  - Markdown owns authored Memory content.
  - SQLite owns ID, path, timestamps, deletion state, query fields, and indexing state.
- Use one stable Markdown file per Memory.
- Assign the Markdown path on create and never rename it in v0, even when the Memory title changes.
- Use ID plus slug in Memory Document filenames to balance stable identity with human readability.
- Allow external Markdown edits as normal filesystem edits, but do not support automatic reconciliation in v0.
- Store Source Refs as structured data, not free-text-only provenance.
- Supported v0 Source Ref types are manual, url, conversation, file, and pi_session.
- Make Source Refs optional on create.
- Soft-delete Memories by setting deletion state. Do not hard-delete SQLite rows or Markdown files in v0.
- Do not expose restore or undo in v0.
- Implement lexical Recall Search only in v0. Semantic search is explicitly deferred.
- Use a search boundary/interface internally so semantic or hybrid search can be added later.
- API endpoints should support create, list, get, update, delete, and search behavior for active Memories.
- List and search behavior should exclude Deleted Memories.
- The UI should be capture-first: create form first, then search/recent results.
- The UI should support creating, listing, searching, viewing, editing, and deleting Memories.
- Startup should initialize the SQLite schema and local vault directories.
- Errors should be represented explicitly enough that HTTP handlers can return meaningful responses without leaking implementation details.

## Testing Decisions

- Tests should verify externally observable behavior, not implementation details.
- The most important tests are around persistence boundaries and application services, not UI markup.
- Test Memory creation by asserting both a queryable Memory record and a Markdown Memory Document are produced.
- Test Memory editing by asserting app-written changes are reflected in both SQLite-backed reads and the Markdown document while the path remains unchanged.
- Test soft deletion by asserting Deleted Memories disappear from list and search but their Memory Document remains on disk.
- Test Recall Search by asserting lexical queries find active Memories by title/body/tags and do not find Deleted Memories.
- Test input validation for required fields and malformed Source Refs.
- Prefer temporary directories/databases in tests so they can exercise real filesystem and SQLite behavior safely.
- Add HTTP-level tests once the application service behavior is stable.
- UI tests are optional for v0; manual browser testing is acceptable until the interaction design settles.

## Out of Scope

- Semantic search, embeddings, LanceDB, sqlite-vec, or hybrid ranking.
- pi assistant chat integration.
- pi custom tools for Memory operations.
- Tool-envelope HTTP endpoints.
- Audit events.
- Idempotency records.
- Undo tokens.
- User-facing restore for Deleted Memories.
- Hard purge/delete.
- Automatic detection or reconciliation of manual Markdown edits.
- General Artifact graph, Relationships, Review Engine, scheduling, imports, YouTube, recipes, pantry, purchases, or notifications.
- Multi-user support, authentication, sync, mobile app, or distribution packaging.

## Further Notes

This feature should remain intentionally small but real. The goal is to create something useful enough to play with while establishing Tether's core feel: Capture personal context now, keep it inspectable, and make it available for future Recall Search.

The north-star scope remains in `TETHER_REWRITE_BRIEF.md`; this PRD covers only the first v0 Memory slice.
