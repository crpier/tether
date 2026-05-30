# Tether

Tether is a private continuity layer that connects saved personal context to future action.

## Language

**Memory**:
A durable, inspectable piece of personal context that Tether may reuse later, ideally with source/provenance and enough text to stand alone. Active Memories can be edited in v0 without changing their Memory Document path.
_Avoid_: Raw note, journal entry, artifact

**Memory Document**:
The human-readable Markdown representation of a Memory's content. It owns the Memory's authored text while SQLite owns operational metadata. Each Memory has one stable Markdown file whose path does not change when the Memory title changes.
_Avoid_: Export, cache, generated view

**Source Ref**:
A structured reference to where a Memory or other Tether record came from, such as manual entry, URL, file, conversation, or pi session.
_Avoid_: Source string, citation, backlink

**Deleted Memory**:
A Memory removed from active use by setting deletion state while leaving its Memory Document on disk. Deleted Memories are not shown, searched, or recalled in v0.
_Avoid_: Destroyed memory, purged memory, hidden note

**Restored Memory**:
A previously Deleted Memory returned to active use by clearing its deletion state. Restoration is not exposed in v0.
_Avoid_: Undone deletion, recovered note

**Recall Search**:
Search over active Memories intended to bring relevant personal context back into attention. v0 Recall Search is lexical only, while semantic recall remains a later extension.
_Avoid_: Full-text search, semantic search, database query

**Capture**:
The act of saving personal context into Tether before it is needed later. v0 Capture is optimized around quickly creating Memories.
_Avoid_: Note taking, data entry, ingestion
