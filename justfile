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
    pnpm -C apps/agent install

# start host, exercise a few requests, then print captured stdout logs
validate-host-logs:
    ./scripts/validate-host-logs.sh

# code generation
codegen:
    cd apps/host && uv run python -m tether.tool_schemas ../agent/generated/tool-schemas.json
    pnpm -C apps/agent codegen
    pnpm -C apps/agent format:generated

# generated-code drift check
codegen-check:
    just codegen
    git diff --exit-code -- apps/agent/generated/tool-schemas.json apps/agent/src/generated

# host tests
test:
    uv run snektest

# host type check
typecheck:
    uv run pyright

# agent tests
agent-test:
    pnpm -C apps/agent test

# agent type check
agent-typecheck:
    pnpm -C apps/agent typecheck

# agent lint
agent-lint:
    pnpm -C apps/agent lint

# agent format check
agent-format-check:
    pnpm -C apps/agent format:check
