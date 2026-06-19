## GitHub workflow

- This project uses GitHub issues for tracking work.
- Do not hand-edit GitHub URLs or assume issue state; query with `gh issue view/list` when needed.
- Implementation work should reference the relevant GitHub issue.
- When starting a new unit of work, stash any uncommitted changes, run `git fetch`, then create a new branch from the latest `origin/main`.
- All work should be done in a branch, and when a unit of work is complete, open a PR against `main`. Only merge the PR if explicitly told to do so.
- When doing feature/bug-fixing/refactoring or any code-related work, use TDD.
