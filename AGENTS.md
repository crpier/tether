## GitHub workflow

- This project uses GitHub issues for tracking work.
- Do not hand-edit GitHub URLs or assume issue state; query with `gh issue view/list` when needed.
- Implementation work should reference the relevant GitHub issue.
- When starting a new unit of work, stash any uncommitted changes, run `git fetch`, then create a new branch from the latest `origin/main`.
- All work should be done in a branch, and when a unit of work is complete, open a PR against `main`. Only merge the PR if explicitly told to do so.
- When creating or editing a PR body with `gh`, write the markdown to a temporary file and use `--body-file`; do not pass multiline markdown through `--body`. Verify the rendered body with `gh pr view` afterward.
- When doing feature/bug-fixing/refactoring or any code-related work, use TDD.

## Testing and validation

- Use `snektest` for tests.
  - For snektest usage documentation, read its installed distribution metadata with `importlib.metadata.distribution("snektest").read_text("METADATA")`; the `METADATA` file embeds snektest's README.
- Use `pyright` for static typing validation.
- Use `ruff` for linting and formatting checks.

## Interacting with databases

- Use `snekql` for interacting with the database.
  - For `snekql` usage documentation, read its installed distribution metadata with `importlib.metadata.distribution("snekql").read_text("METADATA")`; the `METADATA` file embeds snektest's README.
