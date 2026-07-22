# Tether

A single-user, self-hosted AI personal assistant. Its core loop is **capture → resurface**: get a thing out of your head reliably, and have it come back at the right moment. Its distinguishing value is **interconnection** — captured things reference each other, so resurfacing is informed by everything else you've stored.

Tether is a personal operating system: memory is the substrate it is built on, not the product. Capture → resurface is the first loop built on that substrate, not the whole of it — presentation (widgets and artifacts), ingestion (gates and telemetry), proposals and earned autonomy, and typed verticals all layer on top of the same faceted memory pool and the same trust gates.

## Language

**Memory**:
A fact you want to retain — an amorphous blob of information with no domain and no fixed schema, though it may carry Facets (annotations, not structure requirements). It has no lifecycle of its own: it stays true until deleted (e.g. "I prefer aisle seats"), and is never "completed". A Memory is either loose or tethered.
_Avoid_: note, fact, knowledge

**Facet**:
A key/value annotation on a Memory (e.g. `domain: finance`, `sensitivity: medical`) — metadata, not schema. The agent invents facet keys freely as needed; drift across near-duplicate keys is handled by curation, not enforced by validation.
_Avoid_: tag, property, attribute, field

**Loose**:
The state of a Memory that has been captured but not yet tethered (by either path — Review or Recall). Provisional and not yet trusted by the assistant.
_Avoid_: unreviewed, pending, draft

**Tethered**:
The state of a Memory that is trusted and part of the corpus the assistant searches and reasons over; a loose one is not. A Memory reaches this state by one of two paths: **Review** (a human asserted it is true) or **Recall** (the human proved they learned it). ("Connection" between memories is not stored structure — it emerges from Search at the moment the assistant needs context.)
_Avoid_: reviewed, confirmed, sediment

**Review**:
The human act of promoting a loose Memory to tethered — vetting it so it becomes trusted and searchable. The provisional-to-trusted gate every Memory passes through.
_Avoid_: triage, approve, accept

**Recall**:
The second path a Memory can take to become tethered: instead of being reviewed, the human must *prove they retained* the material by answering spaced recall prompts correctly across multiple rounds over days. Used for educational material (chiefly YouTube transcripts). Full completion tethers the memory; failure reschedules and extends. The counterpart to Review — asserted-true vs. demonstrably-learned.
_Avoid_: spaced-recall review, quiz gate, study review

**Recall prompt**:
A single challenge in a Recall — a multiple-choice or short-answer question (or essay) generated from the distilled learnings of a source. Answer correctness and response time feed adaptive scheduling of the next round.
_Avoid_: quiz, flashcard, question

**Study item**:
A loose Memory currently progressing through Recall — distilled from a source (e.g. a video transcript) and being drilled across scheduled rounds, not yet tethered.
_Avoid_: flashcard deck, course, lesson

**Curriculum**:
A learning objective broken into ordered units with progress state (e.g. "learn conversational Spanish").
_Avoid_: course, syllabus, program

**Lesson**:
A generated Artifact within a Curriculum. Its quiz results (Artifact events) feed regeneration of the next Lesson. Not Recall: a Lesson quiz is a feedback instrument shaping what's taught next, not a trust instrument that tethers a Memory.
_Avoid_: quiz, exercise, unit

**Knowledge base**:
The body of tethered Memories, exposed as derived read-only markdown files (Obsidian-compatible). The trusted corpus the assistant searches and reasons over. Its source of truth is SQLite; the markdown is a projection.
_Avoid_: vault, notes, sediment, wiki

**projection** (common noun, not a coined term):
The single read-only markdown file a tethered Memory is rendered into. A loose Memory has none; rejecting a Memory removes its projection. The *set* of all projections **is** the Knowledge base — projection is the part, Knowledge base the whole. Its filename is the Memory's opaque id (`<id>.md`), never a content slug (ADR 0007).
_Avoid_: note, page, file, entry

**Search**:
Reading Memories by query, by filters, or both, with relevance ranking. State-agnostic as a mechanism — the same operation lists the loose review queue and pulls tethered context — so the trust boundary is not in the word: when the *assistant* searches for context it searches only tethered Memories (ADR 0001), and a Search is recomputed every time, never cached (ADR 0006). The sole means by which Memories connect — there is no stored graph; relevance emerges at the moment context is needed.
_Avoid_: retrieval, lookup, recall, fetch

**Commons**:
The pool of faceted Memories where long-tail life domains live as conventions rather than code — no dedicated tables, panels, or lifecycle, just Memories plus Facets. The staging ground a domain occupies before it earns Promotion to a Vertical.
_Avoid_: pool, general memories, unstructured store

**Vertical**:
A hand-built, typed slice of the domain (e.g. Cooking, Health) with its own tables and lifecycle. Admitted only when a domain actually needs a lifecycle, typed queries over time, or a dedicated panel — not merely because it has accumulated many Memories.
_Avoid_: module, feature, app

**Promotion**:
The graduation of a Commons domain into a Vertical, justified once accumulated Facet shapes make the case (recurring keys, values that want structure, lifecycle needs). One-directional in practice — Verticals aren't demoted back to Commons.
_Avoid_: migration, upgrade, graduation

**Sensitivity**:
A Facet governing presentation discretion only — which Memories are hidden in Public mode or suppressed from proactive surfacing. It never limits what the agent may reason over or send to an external LLM provider; that boundary doesn't exist in Tether.
_Avoid_: privacy, visibility, access level

**Public mode**:
A session state that excludes sensitivity-faceted Memories from display and proactive surfacing (e.g. presenting Tether on a shared screen). A presentation-layer switch, not a trust or reasoning boundary.
_Avoid_: private mode, incognito, safe mode

**Ingestion gate**:
A scheduled sync that brings external data in without a chat turn (Readwise, Gmail, Health Connect, ebooks). Content it produces carries machine-synced Provenance, trusted at capture.
_Avoid_: sync job, importer, connector

**Telemetry**:
Raw time-series data landing through an Ingestion gate (heart rate, location, read events). Vertical data — it never enters the Memory pool as-is; only a Distillation derived from it can.
_Avoid_: metrics, events, raw data

**Distillation**:
An agent-derived conclusion drawn from Telemetry or a Fusion (e.g. "sleep quality drops after late screen time"). Enters the Memory pool as agent-inferred content, so it takes the loose→tethered gate like any other agent guess.
_Avoid_: insight, summary, inference

**Fusion**:
Cross-source correlation across Telemetry and/or Memories (e.g. location × heart rate) that produces a Distillation. The mechanism, not the output — the output is always a Distillation.
_Avoid_: correlation, join, merge

**Widget**:
An inline, vetted, Tether-styled render spec placed in a chat turn (tables, Mermaid, Vega-Lite) — a constrained vocabulary, safe because it's constrained. Presentation only, never a source of truth.
_Avoid_: chart, component, embed

**Artifact**:
A freeform, agent-generated page — sandboxed (iframe, strict CSP), versioned, linked from chat. Free to be anything precisely because it's sandboxed; the agent never reads an Artifact back.
_Avoid_: page, app, generated UI

**Artifact event**:
An append-only JSON record an Artifact posts about itself (e.g. a quiz answer, a form submission) — the sole channel by which an Artifact talks back to Tether.
_Avoid_: callback, webhook, artifact message

**Synthetic panel**:
A saved faceted query over the Commons, rendered through Widgets — a panel assembled from convention, with no dedicated code.
_Avoid_: dashboard, view, report

**Scheduled trigger**:
A time-triggered action the human sets up: it fires once or on a recurrence (daily/weekly), and its action is either to deliver a fixed message or to run a prompt through the agent and deliver the result. The push half of the capture → resurface loop (a plain reminder is the fixed-message case).
_Avoid_: scheduled prompt task, reminder, cron job, alert

**Bucket item**:
An intention to act on something later. It lives in an active state and then moves to a terminal state — completed or deleted — where it is retained permanently as history (so dedup can warn you when you try to re-add something you have already dealt with). It is never tethered. It is of exactly one item type, which determines its structure, and records why it was saved (its intent context). The test that distinguishes it from a Memory: a Bucket item can be *finished*.
_Avoid_: backlog item, bucket-list entry

**Todo**:
One actionable thing to do — a single action, no steps ("bring the book next time I visit Ana", "dig out the grey shirt before the gala", "research the pension transfer"). Distinct from a Bucket item (which you *consume*) and a Project (multiple coordinated steps); a Todo is exactly *one action*. It is born active and reaches a terminal state — completed or abandoned. It may carry an optional *waiting condition*: a free-text condition and/or a linked Scheduled trigger (a deadline). Its *waiting* state is always **computed, never stored** — a Todo is waiting while it has an unmet text condition or an unfired linked trigger, and ready otherwise — so it can never get wedged in a stale waiting state. Ready Todos surface in the agent's standing digest; waiting ones are raised only when the conversation makes them relevant. The one-off actionable that had no home before the vertical existed.
_Avoid_: task, reminder, bucket item, project, waiting-on flag

**Item type**:
What kind of thing a Bucket item is (movie, book, place, travel, …). Different item types carry different fields, which is why Bucket items aren't all one shape. Applies only to Bucket items; Memories have none. (The word "domain" is deliberately avoided here to prevent confusion with domain-driven-design vocabulary.)
_Avoid_: domain, category, kind, tag

**Intent context**:
The human's subjective reason for saving a Bucket item — *why* it was worth capturing ("a podcast recommended it," "relates to my interest in X"). Immutable once set; it answers "why did I save this?" months later, when the item alone no longer explains itself. Bucket items only — a Memory is a self-justifying fact and has none.
_Avoid_: reason, rationale, note, why

**Triage**:
An agent-produced report over the *active* Bucket items that surfaces problems — under-specified, duplicate, and stale items — for the human to act on. A pull action, optionally run on a Scheduled trigger. It produces no new stored state. Distinct from Review, which is the Memory trust gate; the two never share vocabulary.
_Avoid_: review, grooming, cleanup, backlog review

**Candidate**:
An agent-*proposed* capture awaiting human acceptance — produced when the agent guesses at memories or bucket items rather than the human directly asking (most notably during conversation import). For a Memory this coincides with the loose state (Review accepts it). For a Bucket item it is a pre-active holding state that must be accepted before the item becomes active. The gate follows authorship: human-authored bucket items skip it; agent-proposed ones do not. Confidence may order or weight a candidate but never bypasses acceptance. Kin to Proposal: a Candidate awaits acceptance of a *thing*, a Proposal awaits approval of a *doing* — the gate follows authorship in both.
_Avoid_: suggestion, proposal, draft

**Proposal**:
A concrete, inspectable set of actions the agent wants to take, awaiting human approval before it executes. The doing-side counterpart to Candidate.
_Avoid_: suggestion, plan, action item

**Autonomy grant**:
An earned, per-action-category removal of the Proposal gate for a specific kind of action — visible to the human and revocable at any time.
_Avoid_: permission, trust level, auto-approve

**Provenance**:
The objective origin of a captured thing — *where* it came from (a URL, a conversation import, a specific YouTube video, a manual entry, a synced external source). Recorded on every Memory and Bucket item. On a Memory it now determines trust class: human-asserted and machine-synced content is trusted at the moment of capture, while agent-inferred content (guesses, Distillations, Fusions) still takes the loose→tethered gate — Review or Recall (ADR 0010).
_Avoid_: source, source reference, citation, origin

**Capture client**:
A deliberately dumb client (phone app, watch tile) whose only job is getting a capture off the human quickly — share-target, voice-to-text, a tap. All intelligence (parsing, tethering, scheduling) stays server-side.
_Avoid_: mobile app, frontend, client app

**Voice input**:
Speech recorded in the web chat client and transcribed to text, entering the conversation as a chat turn — either filled into the composer for the human to review before sending, or sent immediately. Never becomes a Memory directly; it is just another way of producing a chat turn.
_Avoid_: voice memo, dictation, voice command

**Voice capture**:
Recorded audio a Capture client uploads straight to the host, outside of chat. Today it lands directly as a Memory; this is slated to change so it instead becomes a chat turn, the same way Voice input does.
_Avoid_: voice memo, audio capture, voice note

## Cooking

A deferred vertical with its own entities. These terms will migrate to `src/cooking/CONTEXT.md` (and a `CONTEXT-MAP.md` will appear) when the vertical is actually built. It connects to the core at two points: the cooking profile is a view over relevant tethered Memories, and recipe import uses the Candidate pattern.

**Ingredient**:
A canonical, normalized food identity (e.g. "garlic", "all-purpose flour") that both Recipe lines and Pantry items reference. The shared key that makes pantry coverage and shopping-list diffing possible; messy ingredient text is normalized onto it (agent-assisted, sometimes via a Candidate pick).
_Avoid_: food, product, item

**Recipe**:
A stored, structured, in-app-editable dish definition — metadata (title, cuisine, servings, time, tags), Recipe lines, and ordered steps. Reference data (something you *can* cook), distinct from a "dish I want to try" (a Bucket item), which is intentionally not coupled to it. Scaling servings is a transient view, never stored.
_Avoid_: dish, meal, formula

**Recipe line**:
One ingredient entry within a Recipe — a canonical Ingredient plus a quantity and unit ("2 cloves · garlic").
_Avoid_: ingredient (reserved for the canonical entity), row

**Recipe revision**:
An entry in a Recipe's append-only edit history, created either by a direct human edit or by accepting an agent-proposed Candidate edit. Prior revisions are retained; reverting makes an older one current.
_Avoid_: version, edit, history

**Pantry item**:
A canonical Ingredient the household has on hand, tracked as presence + a coarse level (out / low / have), an expiry estimate, and a location (pantry / fridge / freezer). Deliberately *not* a precise quantity. Kept current by cooking (decrements) and shopping (increments), not by manual audits.
_Avoid_: inventory item, stock, supply

**Shopping list**:
The set of a Recipe's Ingredients that the pantry is out of or low on — a set difference, not quantity arithmetic.
_Avoid_: grocery list, cart, basket

**Cooking plan**:
A saved, reusable, human-adjustable granular execution plan generated from a specific Recipe revision — finer-grained than the recipe's raw steps (interleaving prep and cook time), with typed steps (auto-advance / manual-confirm / timer-start). Tied to the Recipe revision it came from: a new revision makes the plan stale and it must be regenerated. A template, not a run.
_Avoid_: cook plan, procedure, method

**Cooking session**:
A single run of a Cooking plan — the transient, resumable runtime state (current step, running timers) that survives a mobile refresh. On completion it decrements the relevant Pantry items. The instance; the Cooking plan is the template.
_Avoid_: cook, run, execution
