## GitHub workflow

- This project uses GitHub issues for tracking work.
- Do not hand-edit GitHub URLs or assume issue state; query with `gh issue view/list` when needed.
- Implementation work should reference the relevant GitHub issue.
- When starting a new unit of work, stash any uncommitted changes, run `git fetch`, then create a new branch from the latest `origin/main`.
- All work should be done in a branch, and when a unit of work is complete, open a PR against `main`. Only merge the PR if explicitly told to do so.
- When creating or editing a PR body with `gh`, write the markdown to a temporary file and use `--body-file`; do not pass multiline markdown through `--body`. Verify the rendered body with `gh pr view` afterward.
- When doing feature/bug-fixing/refactoring or any code-related work, use TDD.

## Web UI work

- A headed Playwright MCP server (`playwright`, configured in the project-local `.mcp.json`) is available for driving and observing the running SPA. Use it to load the page, click through the affected flow, and read the browser console/network while iterating — not just at the end. It runs headed by default so the developer can watch the browser in real time.
- Before opening a PR that touches the web app, load the page through the MCP and confirm the console is clean (no errors) on the flows you changed. This catches runtime/integration breakage that static checks and jsdom unit tests miss — the same class of bug as the `/ws` 404. `just validate-web-smoke` (issue #63) is the automated backstop in the gate; the interactive check is the cheap first line of defence.
- The MCP manages its own browser binary. If the first launch reports a missing browser, install it with `npx playwright install chromium`. Headed mode needs a display (`$DISPLAY`/Wayland).

## Testing and validation

- Use `snektest` for tests.
  - For snektest usage documentation, read its installed distribution metadata with `importlib.metadata.distribution("snektest").read_text("METADATA")`; the `METADATA` file embeds snektest's README.
- Use `pyright` for static typing validation.
- Use `ruff` for linting and formatting checks.

### Validation gate (keep `main` clean)

- `main` must always pass every check. Never commit, push, open, or merge a PR until all of the checks below pass clean from the relevant package dir (e.g. `apps/host`):
  - `uv run pyright` — 0 errors.
  - `uv run ruff check .` — all checks passed.
  - `uv run ruff format --check .` — no files would be reformatted.
  - `uv run python -m snektest tests/` — all tests pass.
  - `just codegen-check` — generated tool shims have no drift.
  - `pnpm -C apps/agent typecheck` — 0 errors.
  - `pnpm -C apps/agent lint` — all checks passed.
  - `pnpm -C apps/agent format:check` — no files would be reformatted.
  - `pnpm -C apps/agent test` — all tests pass.
- Run the gate against the full changed surface, not just files you touched — formatting/typing issues often surface in neighbours. If any check fails, fix it before proceeding rather than committing and following up.
- Do not silence findings by relaxing the strict `pyright`/`ruff` config for production code. Fix the code. Config relaxations are only acceptable for genuine test-only false positives, scoped to `tests/` (ruff `per-file-ignores`, pyright `executionEnvironments`), and must be commented with the reason.

## Interacting with databases

- Use `snekql` for interacting with the database.
  - For `snekql` usage documentation, read its installed distribution metadata with `importlib.metadata.distribution("snekql").read_text("METADATA")`; the `METADATA` file embeds snektest's README.
