---
status: superseded by ADR-0036
---

# Selected email promotion creates Evidence

Ordinary mailbox searches, reads, and Gmail ingestion records remain transient context or audit state rather than citeable Evidence. After explicit user authorization, the foreground agent may nominate a message it successfully read in the active interactive turn; the host then captures a bounded immutable local snapshot, links it to the authorizing user Message, and gives Dreaming a stable `tether://email/<id>` citation. Snapshots remain until explicit deletion rather than being garbage-collected when no current Claim cites them. This keeps mailbox volume, untrusted assertions, remote deletion, and assistant prose outside Memory authority while allowing selected durable email facts to survive as inspectable sources.
