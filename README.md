# Tether

Tether is a private continuity layer that connects saved personal context to future action.

This is a rewrite of the original vibe-coded experiment. See [`TETHER_REWRITE_BRIEF.md`](./TETHER_REWRITE_BRIEF.md) for the north-star scope and [`CONTEXT.md`](./CONTEXT.md) for project language.

## Development

```bash
pnpm install
pnpm dev:web
```

Useful scripts:

```bash
pnpm dev:web
pnpm typecheck:web
```

- Server: planned Python ASGI app in `apps/server`
- Web: http://localhost:5173

## Current Stack

- Python backend planned in `apps/server`, managed with `uv`
- Starlette ASGI + Pydantic API DTOs planned
- SolidJS/TypeScript frontend
- OpenAPI-generated frontend API types planned
- SQLite + Markdown persistence planned for v0 Memory capture
- pi assistant integration planned after the first REST/UI slice

## License

MIT
