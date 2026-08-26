---
status: superseded
superseded_by: 0030-open-webui-owns-assistant-runtime
---

# Tether Conversations own history; pi sessions receive fresh Memory context

Tether's host-owned Conversation and Message rows are canonical conversational history. A pi session is an execution/cache segment that may rotate after five idle minutes, be restarted, or be restored for operational diagnosis; its JSONL is backed up for audit and recovery but never supports a Memory claim or defines where a Tether Conversation begins or settles. Conversation Dreaming instead follows Tether `conversation_id` and monotonic Message bounds, and a range settles only after twenty minutes without another user message and completion of the corresponding foreground turn.

Before each foreground model call, the host recomputes a bounded context projection from current canonical Memory files: recognized `context: always` topics within budget, then prompt-relevant topics selected through the rebuildable lexical/semantic index. It injects complete selected files through pi's non-destructive `context` hook and records their path, exact version/hash, and selection reason in the run trace. Foreground memory search remains available as a fallback. The stable system prompt and pi JSONL do not absorb these changing projections.

Explicit “remember this” requests and corrections schedule the same assimilation pipeline immediately after the foreground turn, bypassing only the twenty-minute wait; they do not grant foreground pi write access. Snapshot bounds remain immutable, valid no-ops consume them, and cursors advance only after successful runs.

This refines ADR 0005: pi still drives each agent loop over RPC, but Tether owns conversational continuity, Memory selection, and durable truth independently of pi session lifecycle.
