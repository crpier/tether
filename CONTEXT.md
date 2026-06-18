# Tether

Tether is an AI-powered personal assistant.

## Language

**Memory**:
An inspectable piece of personal context that Tether may reuse later, ideally with source/provenance and enough text to stand alone. Every Memory is either Loose or Tethered. Memories can connect to other Memories across otherwise unrelated areas of life. Active Memories can be edited without changing their Memory Document path.
_Avoid_: Raw note, journal entry, artifact

**Loose Memory**:
A Memory captured into Tether (usually automatically) that the user has not yet reviewed. Loose Memories are visible to the assistant, which must know it is relying on unreviewed context. Sources filter for confidence/relevance before creating Loose Memories, and a Loose Memory left unreviewed for a long time quietly ages out — unlike a Discarded Memory, no rejection is recorded, so it may be captured again if the signal repeats.
_Avoid_: Staging area, staged memory, candidate, draft, inbox item

**Tethered Memory**:
A Memory accepted into permanent storage, either by the user reviewing a Loose Memory or by the user capturing it deliberately themselves (manual Capture is born Tethered). Tethering is the act of confirming a Loose Memory during Review.
_Avoid_: Permanent memory, confirmed memory, approved memory

**Review**:
The recurring act of examining Loose Memories and deciding whether each becomes Tethered or Discarded. Review is the gate between automatic capture and permanent storage.
_Avoid_: Triage, moderation, approval queue

**Connection**:
A stored link between two Memories, formed automatically by Tether with a confidence score. Connections are derived metadata: they do not pass Review and can be regenerated, but the user can pin or sever one manually. Review may show an item's Connections as context.
_Avoid_: Relationship, edge, backlink

**Domain Record**:
Fast-changing operational state Tether manages mechanically, such as pantry quantities, price watches, schedules, or cooking sessions. Domain Records are not Memories and do not pass through Review, but they can link to and from Memories.
_Avoid_: Artifact, entity, generic "item"

**Discarded Memory**:
A Loose Memory rejected during Review. It leaves active use, and Tether remembers the rejection so automatic Capture does not re-create the same Memory.
_Avoid_: Ignored item, blacklisted item, trash

**Memory Document**:
The human-readable Markdown representation of a Memory's content. It owns the Memory's authored text while SQLite owns operational metadata. Each Memory has one stable Markdown file whose path does not change when the Memory title changes.
_Avoid_: Export, cache, generated view

**Source Ref**:
A structured reference to where a Memory or other Tether record came from, such as manual entry, URL, file, conversation, or pi session.
_Avoid_: Source string, citation, backlink

**Deleted Memory**:
A Memory removed from active use at the user's request by setting deletion state while leaving its Memory Document on disk. Deleted Memories are not shown, searched, or recalled. Contrast with a Discarded Memory, which is rejected during Review before ever becoming Tethered.
_Avoid_: Destroyed memory, purged memory, hidden note

**Restored Memory**:
A previously Deleted Memory returned to active use by clearing its deletion state.
_Avoid_: Undone deletion, recovered note

**Recall Search**:
Search over active Memories intended to bring relevant personal context back into attention.
_Avoid_: Full-text search, semantic search, database query

**Conversation**:
The single continuous chat thread between the user and Tether — the product's front door. There is no user-facing session management; context-window handling happens invisibly. Conversations are also a Capture source: Tether may extract Loose Memories from them, with the Conversation as Source Ref.
_Avoid_: Session, daily chat, chat history list

**Schedule**:
Tether's single primitive for bringing something back to attention or action at a future time or condition. Practice intervals, Resurfacing, user reminders, and Review cadence are all policies built on Schedules.
_Avoid_: Cron job, scheduled task, timer loop

**Practice**:
A spaced-repetition session where the user answers recall prompts generated from Tethered knowledge, so consumed material becomes retained knowledge. Any knowledge Memory can enter Practice; the user opts it in, typically while tethering it during Review.
_Avoid_: Review, learning review, quiz, test

**Resurface**:
Tether bringing already-saved context back into the user's attention because it is stale, due, or actionable. Resurfacing is Tether-initiated, unlike a reminder the user schedules explicitly.
_Avoid_: Review, report, digest

**Capture**:
The act of bringing personal context into Tether before it is needed later, whether performed by the user directly or automatically by Tether on the user's behalf.
_Avoid_: Note taking, data entry, ingestion
