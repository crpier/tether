---
status: accepted
---

# One constrained Ledger Vertical owns user-approved generic records

Tether will have one hand-built Ledger Vertical for structured histories that do not yet justify dedicated Vertical code. The agent may propose Ledger instances and schema revisions, but a later active user Message must approve the exact immutable proposal before entries can use it. This is a domain-specific approval flow owned by Ledger, not a generic Proposal queue.

Each Ledger has a stable identity and immutable definition revisions containing its name, purpose, lifecycle state, and flat field schema. Lifecycle uses ADR 0016's `active`, `completed`, and `abandoned` convention, and only an active Ledger accepts entries. Schemas use a closed scalar vocabulary: text, integer, decimal, boolean, date, datetime, and bounded enum. Field identities remain stable; a later revision may add or deprecate fields, but it cannot reuse an identity with a different type or meaning. Every entry names the exact revision that interprets it.

Ledger entries are immutable structured records with a server recording timestamp, an optional occurrence timestamp, and one or more exact canonical Evidence references. A correction appends a complete replacement entry that supersedes exactly one current entry in the same Ledger. Supersession cannot branch. Ordinary Ledger capabilities never edit or delete definitions, revisions, or entries; strong erasure under ADR 0024 remains the deliberate exception and physically removes them.

A Ledger entry is neither Memory nor independent Evidence. Ledger queries return the structured record together with its original Evidence, while Dreaming continues to derive Claims from permitted canonical Evidence rather than from the agent's structured copy. Ledger receives its own query interface; it does not silently broaden Memory Search, whose domain meaning remains reading current Memory.

This deliberately refines ADR 0016. Ledger is one bespoke Vertical with runtime-defined instances, not a framework for generating Verticals, tables, code, tools, workflows, permissions, relationships, or lifecycle rules. Existing Verticals remain authoritative. Tether does not pretend it can detect every semantic overlap from a free-text purpose; tool policy directs owned records to their established Vertical, and a Ledger never projects into or overrides that state. Promotion to a dedicated Vertical is a bespoke migration owned by the new Vertical, not a generic Ledger feature.

## Considered options

- **Store histories as Memory Topics**: rejected because Memory is current understanding, not an activity log.
- **Generate a bespoke Vertical or SQLite table for each request**: rejected because model-authored code and migrations would gain ambient authority and permanent maintenance cost.
- **Accept arbitrary JSON or full JSON Schema**: rejected because nested, executable, and weakly bounded shapes would turn Ledger into an application platform rather than a record capability.
- **Require a dedicated Vertical for every structured history**: rejected because many useful personal logs need stable fields and chronology but no custom queries, lifecycle, or panel.

## Consequences

- Ledger creation and schema evolution need an inspectable Ledger-specific proposal and later approval record.
- Schema revisions and entries remain interpretable without migrating historical payloads.
- Reads distinguish current entries from superseded history and always expose schema version, timestamps, and provenance.
- Resource bounds apply to Ledger count, schema size, field values, append batches, and query results.
- A future dedicated Vertical may import or reference a Ledger through its own explicit migration, then close the Ledger against further appends.
