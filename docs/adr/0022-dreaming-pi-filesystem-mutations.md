---
status: superseded
superseded_by: 0030-open-webui-owns-assistant-runtime
---

# Dreaming mutates canonical Memory through confined native-shaped pi file tools

A dreaming run uses pi's familiar filesystem interaction instead of emitting a bespoke typed memory patch. It receives `read`, wrapped native `write` and `edit`, native-shaped `move` and `delete`, and `bash`; the mutators are confined to the Memory root, serialize their calls, reject symlinks/path escapes, and validate prospective Markdown frontmatter. Bash preserves its normal model-facing interface but executes inside Bubblewrap with the Memory root read-only, an ephemeral writable `/tmp`, no network, no credentials or unrelated host files, and bounded time/output. This gives the model the file ergonomics on which it is well trained without allowing shell mutation to bypass validation and history.

After a native mutation succeeds, its wrapper notifies the host using `(run_id, tool_call_id)` and waits for acknowledgement before returning to pi. The host rereads surviving canonical files and, in one idempotent SQLite transaction, records complete resulting file versions or tombstones and invalidates derived indexes. It stores no deltas. Lost acknowledgements retry only notification, never the filesystem operation; startup reconciliation repairs a crash after mutation but before recording.

Each acknowledged mutation becomes canonical immediately. Runs do not stage or atomically publish a multi-file patch: a failed run leaves its completed mutations in place, does not roll them back, and does not advance its evidence cursor. Retrying the same immutable evidence bounds reads the already-modified workspace and relies on model-driven convergence. This consciously trades isolated run-level transactions for a much smaller implementation and direct file workflow.

This supersedes ADR 0005's closed-tool-world rule for background Dreaming only. Foreground chat remains host-tool-driven and cannot mutate Memory files directly. SQLite continues to own validation policy, orchestration, history, and derived-state invalidation; filesystem mutation authority is the narrow exception.

## Consequences

- Every current Memory change is inspectable as full-content history grouped by tool call and dreaming run; ordinary deletion retains tombstoned history, and restore writes a new current version.
- Move is stored as delete-old/create-new. Directory moves expand to every contained Markdown file under one mutation; move never overwrites, and delete handles files or empty directories only.
- If acknowledgement ultimately fails, pi receives an explicit error saying the filesystem mutation succeeded, the run fails, and reconciliation records the authoritative files.
- Runtime shadow mode is unnecessary. Tests and offline model comparisons use temporary workspaces; production Dreaming writes canonically and can be operationally paused.

**Refinement (ADR 0026):** reconciliation accepts only recorded Dreaming mutations. A crash after filesystem mutation but before mutation recording repairs the file from recorded state and retries the run instead of treating unrecorded bytes as authoritative.
