# Tether

Tether is a private continuity layer that connects saved personal context to future action.

This is a rewrite of the original vibe-coded experiment. See [`TETHER_REWRITE_BRIEF.md`](./TETHER_REWRITE_BRIEF.md) for the north-star scope and [`CONTEXT.md`](./CONTEXT.md) for project language.

## Development

```bash
pnpm install
pnpm dev
```

Useful scripts:

```bash
pnpm dev:server
pnpm dev:web
pnpm typecheck
```

- Server: http://localhost:3000
- Web: http://localhost:5173

## Current Stack

- TypeScript
- Effect + Effect HTTP backend
- SolidJS frontend
- SQLite + Markdown persistence planned for v0 Memory capture
- pi assistant integration planned after the first REST/UI slice

## License

MIT
