---
status: accepted
---

# Dreaming is the sole writer of Memory

Current Memory may change only through a recorded Dreaming mutation. Direct capture, Review promotion, filesystem editing, and history restore would create competing authorities and could bypass Evidence, suppression, and mutation history. Explicit "remember this" requests and corrections instead become high-authority conversational Evidence and queue Dreaming immediately.

Reconciliation accepts an unacknowledged Dreaming mutation only when its recorded before-content matches the latest recorded file and its after-content matches the workspace. It repairs every other edit or deletion from the latest recorded state and removes unknown valid topic files. External editors may read Memory but cannot author it.

This refines ADR 0021's filesystem-authority decision and ADR 0022's crash recovery. A crash after filesystem mutation but before mutation recording discards that unrecorded change and retries the Dream run; a recorded mutation remains recoverable before acknowledgement. User controls remain "Don't remember this" and "Delete this everywhere," not direct Memory editing.
