# Tether tasks. `just` from root; uv targets apps/host via UV_PROJECT (.envrc).

default:
    @just --list

# Python host (Starlette, auto-reload)
host:
    TETHER_RELOAD=true uv run python -m tether

# SolidJS web (Vite dev server)
web:
    pnpm -C apps/web dev

# sync/install all deps
install:
    uv sync
    pnpm -C apps/web install

# host tests
test:
    uv run snektest

# host type check
typecheck:
    uv run pyright
