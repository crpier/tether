# Trust is determined by provenance class, not uniformly by the loose→tethered gate

Until now every Memory, regardless of origin, entered the corpus loose and needed Review or Recall to become tethered (ADR 0001, ADR 0004). As Tether grows Ingestion gates (Readwise, Gmail, Health Connect, ebooks) and Fusion/Distillation over Telemetry, that uniform gate stops making sense: a human-asserted fact, a machine-synced calendar event, and an agent's inferred conclusion about your sleep are not the same kind of claim, and treating them identically either drowns the Review queue in machine noise or forces high-volume sync data through a gate it doesn't need.

We now trust by **provenance class**:

- **Human-asserted** — the human said it directly (chat, a form). Trusted at capture, as before.
- **Machine-synced** — an Ingestion gate wrote it verbatim from an external system of record (a calendar event, a Readwise highlight, a raw Health Connect reading as Telemetry). Trusted at capture: the sync itself is the assertion of fact, nothing is invented.
- **Agent-inferred** — the agent guessed, summarized, or derived it (Candidates, Distillations, Fusions). Still takes the loose→tethered gate — Review or Recall — exactly as ADR 0001 and ADR 0004 describe.

This amends, but does not replace, ADR 0001 and ADR 0004: the loose→tethered gate is unchanged in mechanism and still the only way agent-inferred content earns trust. What changes is scope — the gate is no longer applied to every Memory uniformly, only to the agent-inferred class. Human-asserted and machine-synced Memories skip it and are tethered immediately.

## Consequences

- Ingestion gates can sync at volume (calendar, highlights, health readings) without manufacturing a Review backlog; nothing enters loose that a human would have rubber-stamped anyway.
- The trust boundary the assistant reasons over (search only tethered Memories, ADR 0001) still holds — it now spans two provenance classes trusted at capture plus one gated class, instead of one path with a single gate.
- Telemetry itself never becomes a Memory (see ADR 0012); this ADR governs the trust class of what an Ingestion gate *does* write as a Memory (e.g. a synced calendar event), and of Distillations/Fusions derived from Telemetry.
- Provenance recording (already required on every Memory) now does double duty: besides answering "where did this come from," it determines whether the loose→tethered gate applies at all. A bug that mis-tags provenance class is a trust-boundary bug, not just a metadata bug.
