---
status: superseded
superseded_by: 0030-open-webui-owns-assistant-runtime
---

# Conversation scope organizes work, not truth

Tether has one permanent untitled Main Conversation and any number of named Scoped Conversations. A Scoped Conversation has a presentation-only display name and one durable scope brief added to the normal Conversation prompt, but it uses the same persona, tools, global Memory projection, Todo digest, and Evidence rules as Main. Scope organizes work and navigation; it is not a security, tool, or Memory partition.

Each Conversation owns its model profile, pi runtime lifecycle, FIFO Conversation turns, transcript route, and durable read position. Model and scope revisions are snapshotted at turn submission, so edits affect later submissions without mutating running or pending turns. A scope-revision change rotates the warm pi runtime before the affected turn executes. Different Conversations may execute concurrently. User Messages from every Conversation remain global conversational Evidence, while Scheduled Messages remain context only. Active Scoped Conversations appear by latest Message activity, Main stays pinned, unread state follows Message sequence, and archived Conversations retain their Messages and Evidence outside ordinary navigation. Archival discards and rotates the Scoped Conversation's pi session so restored execution starts fresh. Archival is blocked by nonterminal turns or active targeted prompt Scheduled triggers. Transcript clearing is not a Conversation lifecycle operation because deleting Messages without retracting derived Evidence would be misleading.
