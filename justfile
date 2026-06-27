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
    uv run python -m tether.openapi_export openapi.json
    pnpm -C apps/web codegen
    pnpm -C apps/web format:generated
    uv run python -m tether.tool_schemas apps/agent/generated/tool-schemas.json
    pnpm -C apps/agent codegen
    pnpm -C apps/agent format:generated

# generated-code drift check
codegen-check:
    just codegen
    git diff --exit-code -- openapi.json apps/web/src/generated apps/agent/generated/tool-schemas.json apps/agent/src/generated

# host tests
host-test:
    cd apps/host && uv run python -m snektest tests/

# host type check
host-typecheck:
    uv run pyright

# host lint
host-lint:
    uv run ruff check .

# host format check
host-format-check:
    uv run ruff format --check .

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

# all tests
test: host-test agent-test

# all type checks
typecheck: host-typecheck agent-typecheck

# all lint checks
lint: host-lint agent-lint

# all format checks
format-check: host-format-check agent-format-check
