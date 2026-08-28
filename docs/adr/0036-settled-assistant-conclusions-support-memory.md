---
status: accepted
---

# Settled assistant conclusions may support Memory

A final assistant Message from a succeeded Conversation turn is canonical Evidence for an agent-derived Claim. This applies to every Conversation-turn origin, including interactive, Scheduled, and Health turns. Earlier assistant Messages within the turn, output from failed or cancelled turns, reasoning, and tool Messages remain context only.

Assistant conclusions have lower authority than explicit user corrections, instructions, and assertions. Repetition by the assistant does not independently corroborate a conclusion or increase its authority. Dreaming must preserve the uncertainty of an inference rather than rewrite it as a user-asserted fact. Prior Memory remains state rather than Evidence, so retrieving and restating a Claim cannot create a higher-authority provenance loop.

This lets Dreaming retain useful synthesis whose durable value exists in the answer rather than in a user Message—for example, a viewing pattern inferred from typed YouTube records or an interpretation produced by an unattended Health turn. Typed source records remain canonical where available, but Memory no longer requires a bespoke promotion path for every Integration.

Gmail's source-specific promotion and snapshot-writing workflow is therefore retired. Ordinary Gmail reads remain transient tool context; a durable conclusion derived from them can enter through the settled final assistant Message. Existing email snapshots and citations remain stored and inspectable read-only so retiring the writer does not delete source data or break current Memory.

This refines ADR 0021's authority order and ADR 0026's Dreaming-only write boundary. It supersedes ADR 0035.

## Considered options

- **Keep assistant output as context only** — rejected because it discards useful agent synthesis when the source records alone do not express the durable conclusion.
- **Build a promotion pipeline for every Integration** — rejected because it couples Memory authority to source-specific workflows and still misses conclusions spanning several sources.
- **Treat every assistant Message uniformly** — rejected because intermediate and failed output is not a settled conclusion and can contain partial or contradicted reasoning.
- **Give assistant conclusions the same authority as user assertions** — rejected because model inference must remain correctable and must not overrule the person it describes.

## Consequences

- Dreaming can cite an eligible assistant Message with the existing `tether://message/<id>` reference.
- Scheduled and Health conclusions can queue Dreaming without a user Message in the same Conversation window.
- Eligibility depends on durable turn lifecycle and Message order, not the transcript role alone.
- The Evidence inspector must reject intermediate and failed assistant Messages while resolving eligible final answers.
- Agent mistakes may become current Memory, but their provenance remains inspectable and lower-authority; later user Evidence supersedes them.
- No new email snapshots or promotions are created. Historical snapshot tables remain inert compatibility data.
