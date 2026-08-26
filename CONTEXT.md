# Tether

Tether is a single-user, self-hosted domain capability host for a personal assistant. It keeps typed personal state and deterministic integrations behind a small authenticated tool interface. Generic conversation, model execution, and assistant memory belong to the external assistant runtime rather than to Tether.

## Language

**Domain capability**:
A typed operation over state Tether owns independently of any assistant conversation, such as creating a Todo, completing a Bucket item, or querying Health Connect. Domain capabilities are exposed through an explicit allowlist; they are not generic database access.
_Avoid_: agent tool implementation, endpoint, plugin

**Integration**:
A hand-built typed slice that synchronizes or reads one external system and presents one interface to the rest of Tether. Transport, credentials, retries, quotas, and source-specific details stay internal to the Integration.
_Avoid_: connector, provider wrapper, adapter package

**Ingestion gate**:
A deterministic background or client-driven sync that admits records from an external system into typed Tether storage. It does not invoke an assistant model.
_Avoid_: agent schedule, prompt job, importer

**Telemetry**:
Raw time-series records received from a device or external Integration, such as heart rate, sleep, location, or reading events. Telemetry remains in typed storage and is not generic assistant memory.
_Avoid_: assistant memory, summary, metric cache

**Health episode**:
A deterministic grouping of related Health Connect records over a bounded interval, such as one sleep session. Episode summaries remain traceable to their typed source records and do not require model inference.
_Avoid_: health memory, dream, freeform summary

**Bucket item**:
An intention to consume, visit, buy, or otherwise act on something later. A Bucket item has exactly one Item type, records its immutable Intent context, and moves from active to completed or deleted while remaining available for history and duplicate detection.
_Avoid_: Todo, reminder, memory, backlog entry

**Item type**:
The structure of one Bucket item, such as movie, book, place, travel, or purchase. Item types apply only to Bucket items.
_Avoid_: domain, category, tag

**Intent context**:
The human's immutable reason for saving a Bucket item. It preserves why the item mattered when the item alone may no longer provide enough context.
_Avoid_: provenance, mutable note, description

**Triage**:
A deterministic report over active Bucket items that identifies under-specified, duplicate, or stale entries for human review. It creates no stored state.
_Avoid_: grooming, automatic cleanup, scheduled review

**Todo**:
One actionable thing to do. A Todo is born active and reaches a terminal state of completed or abandoned. It may have a free-text waiting condition; readiness is computed from current Todo state rather than maintained as a separate lifecycle flag.
_Avoid_: Bucket item, reminder, project, task list

**Capture client**:
The Android client that sends Health Connect records to Tether's retained HTTPS ingestion interface. It holds no assistant behavior and does not submit generic text or voice conversations.
_Avoid_: assistant frontend, chat client, mobile agent
