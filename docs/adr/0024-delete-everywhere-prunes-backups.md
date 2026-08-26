---
status: superseded
superseded_by: 0030-open-webui-owns-assistant-runtime
---

# Delete everywhere physically removes all retained backups

“Delete this everywhere” is a strong erasure promise, not deletion from only Tether's live read model. It purges selected canonical source records, affected Memory files, file-version contents, derived indexes, pi copies, and other live projections; then a privileged deployment worker excludes concurrent backup activity, forgets and prunes every snapshot in Tether's dedicated Restic repository, creates one clean post-deletion backup, and resumes the normal schedule. The operation is complete only after remote pruning and the clean backup both succeed.

Restic cannot remove one file from an existing snapshot, so this deliberately sacrifices unrelated backup history whenever strong erasure is requested. If remote work fails, live material remains deleted, the durable content-free purge request remains visibly pending, and the worker retries. The Backblaze bucket must not enable object lock or another retention mechanism that prevents physical deletion.

We rejected retention-bound deletion because the user-facing word “everywhere” should not conceal recoverable weekly copies. Ordinary Memory deletion and “Don't remember this” do not invoke this protocol: they retain source evidence and file history and are reversible.
