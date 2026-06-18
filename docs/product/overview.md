# Tether — Product Overview

Tether is an AI-powered personal assistant.

> Tether keeps the thread between what you saved, why you saved it, and what you
> should do with it next.

This is the canonical statement of what Tether is for. Project language is defined in
[`../../CONTEXT.md`](../../CONTEXT.md); the backend implementation direction is in
[`../architecture.md`](../architecture.md); settled decisions live in
[`../adr/`](../adr/).

## Thesis

General assistants lose continuity. Notes and task apps preserve information but rarely
help reconnect it to the right moment. Tether sits between those worlds:

- capture information with low friction
- preserve why it mattered
- connect related information across domains
- resurface open loops at the right time
- guide action when the user is ready to do something

The core promise is not "store everything." It is to keep the thread between context,
the intent behind it, and the next action.

## Pillars

### 1. Capture

Make it easy to bring personal context into Tether before it is fully structured, and
before it is needed later — accept messy input, then enrich or structure it afterward.
Capture is either manual (born Tethered) or automatic (creating Loose Memories the user
later reviews). Intended surfaces include free-form Memories, open-loop/backlog items,
recipes and pantry state, purchases under consideration, reminders, saved media,
imported external conversations, and voice messages in the Conversation.

### 2. Connect

The theme to emphasize. Tether connects pieces of information across time, domains, and
workflows. Every durable record should carry provenance (a Source Ref), enough context
to stand alone, and Connections to related Memories. Connections are derived
automatically with a confidence score; the user can pin or sever them.

### 3. Review & Resurface

Stored information must not become a passive graveyard. Two distinct loops keep it
alive:

- **Review** — the gate where the user turns Loose Memories into Tethered ones, or
  discards them.
- **Resurface** — Tether bringing already-saved context back to attention because it is
  stale, due, or actionable.

(Spaced recall of knowledge is a third, related loop — **Practice**; see `CONTEXT.md`.)

### 4. Guided Action

Tether helps the user execute real workflows, not just retrieve facts — moving from
context to action with explicit, inspectable steps. Examples: guided cooking with
generated step plans and timers, shopping-list generation, purchase buy/wait decisions,
next-action recommendations on open loops, and scheduled prompts that act later.

## Product loops worth preserving

- **Capture → Connect → Review → Act** — save a research topic with its why/where
  context; connect it to the source Conversation, URL, or related Memories; Resurface
  it when it is under-specified or stale; act on it.
- **Save → Revisit → Use** — save educational material; find it later by content; if it
  is worth retaining, Practice turns it into recalled knowledge.
- **Choose → Prepare → Execute** — search recipes against pantry state; suggest fits and
  missing ingredients; run a guided cooking session; record preferences for next time.
- **Consider → Watch → Decide** — capture a purchase candidate; track price and decision
  factors; Resurface stale candidates; decide buy or wait.

## Positioning

Tether's value is continuity and connection, not generic storage. Center language on
tether, thread, continuity, context, provenance, Memory, Connection, Resurface,
Capture, and guided action. Avoid framing it as a workbench, a generic note app, or an
undifferentiated "second brain." The full domain vocabulary is canonical in
[`../../CONTEXT.md`](../../CONTEXT.md).

System metaphor:

> Memories are the things Tether knows: Loose until reviewed, Tethered once accepted.
> Connections are the links between them. Resurfacing is how forgotten context comes
> back into attention. Actions are how context becomes useful.
