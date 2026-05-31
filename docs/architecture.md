# Architecture Notes

These notes describe the current implementation direction. They are intentionally lighter than ADRs and can evolve as the rewrite teaches us more.

## Repo Shape

Tether is a monorepo with separate backend and frontend projects:

- `apps/server` — Python backend and HTTP API, managed with `uv`.
- `apps/web` — SolidJS/TypeScript frontend, managed with `pnpm`.

Domain logic should start in `apps/server` while the model is still young. Extract packages only when repetition proves the boundary. API-facing TypeScript types should be generated from the Python backend's OpenAPI contract rather than hand-maintained in a shared package.

## v0 Feature Slice

The first meaningful feature is Memory capture and recall:

- create Memory
- list recent Memories
- get Memory detail
- edit title/body/tags
- soft-delete Memory
- lexical Recall Search

## Persistence Direction

Tether uses SQLite plus Markdown files with split authority:

- Markdown owns authored Memory content.
- SQLite owns IDs, paths, deletion state, audit/index metadata, and queryable fields.

External Markdown edits are allowed as files, but v0 does not promise automatic reconciliation.

## Assistant Direction

Pi is the planned assistant runtime adapter. Tether owns durable state; pi supplies assistant/session/tool execution when we add that layer.
