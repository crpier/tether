# Tether dev tasks. Run `just` (or `just --list`) to see everything.
#
# Backend  -> apps/server (Python, uv)
# Frontend -> apps/web    (SolidJS/Vite, pnpm workspace)

server_dir := "apps/server"

# List available recipes
default:
    @just --list

# Install everything (backend env + web deps)
setup: sync install

# Sync the Python backend environment (uv)
sync:
    uv sync --directory {{server_dir}}

# Install web / workspace dependencies (pnpm)
install:
    pnpm install

# Run the backend (apps/server)
dev-server:
    uv run --directory {{server_dir}} python main.py

# Run the web dev server (apps/web)
dev-web:
    pnpm --filter @tether/web dev

# Run backend tests (snektest)
test *args:
    uv run --directory {{server_dir}} snektest {{args}}

# Type-check the web app
typecheck-web:
    pnpm --filter @tether/web typecheck

# Build the web app for production
build-web:
    pnpm --filter @tether/web build

# Run all checks (backend tests + web type-check)
check: test typecheck-web
