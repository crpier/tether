# Tether

A single-user, self-hosted AI personal assistant. Its core loop is **capture → resurface**: get a thing out of your head reliably, and have it come back at the right moment. Its distinguishing value is **interconnection** — captured things reference each other, so resurfacing is informed by everything else you've stored.

## Language

**Memory**:
A fact you want to retain — an amorphous blob of information with no domain and no fixed schema. It has no lifecycle of its own: it stays true until deleted (e.g. "I prefer aisle seats"), and is never "completed". A Memory is either loose or tethered.
_Avoid_: note, fact, knowledge

**Loose**:
The state of a Memory that has been captured but not yet tethered (by either path — Review or Recall). Provisional and not yet trusted by the assistant.
_Avoid_: unreviewed, pending, draft

**Tethered**:
The state of a Memory that is trusted and part of the corpus the assistant retrieves from and reasons over; a loose one is not. A Memory reaches this state by one of two paths: **Review** (a human asserted it is true) or **Recall** (the human proved they learned it). ("Connection" between memories is not stored structure — it emerges from retrieval at the moment the assistant needs context.)
_Avoid_: reviewed, confirmed, sediment

**Review**:
The human act of promoting a loose Memory to tethered — vetting it so it becomes trusted and retrievable. The provisional-to-trusted gate every Memory passes through.
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

**Knowledge base**:
The body of tethered Memories, exposed as derived read-only markdown files (Obsidian-compatible). The trusted corpus the assistant retrieves from and reasons over. Its source of truth is SQLite; the markdown is a projection.
_Avoid_: vault, notes, sediment, wiki

**Scheduled trigger**:
A time-triggered action the human sets up: it fires once or on a recurrence (daily/weekly), and its action is either to deliver a fixed message or to run a prompt through the agent and deliver the result. The push half of the capture → resurface loop (a plain reminder is the fixed-message case).
_Avoid_: scheduled prompt task, reminder, cron job, alert

**Bucket item**:
An intention to act on something later. It lives in an active state and then moves to a terminal state — completed or deleted — where it is retained permanently as history (so dedup can warn you when you try to re-add something you have already dealt with). It is never tethered. It is of exactly one item type, which determines its structure, and records why it was saved (its intent context). The test that distinguishes it from a Memory: a Bucket item can be *finished*.
_Avoid_: backlog item, task, todo, bucket-list entry

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
An agent-*proposed* capture awaiting human acceptance — produced when the agent guesses at memories or bucket items rather than the human directly asking (most notably during conversation import). For a Memory this coincides with the loose state (Review accepts it). For a Bucket item it is a pre-active holding state that must be accepted before the item becomes active. The gate follows authorship: human-authored bucket items skip it; agent-proposed ones do not. Confidence may order or weight a candidate but never bypasses acceptance.
_Avoid_: suggestion, proposal, draft

**Provenance**:
The objective origin of a captured thing — *where* it came from (a URL, a conversation import, a specific YouTube video, a manual entry). Recorded on every Memory and Bucket item. On a Memory it also calibrates Review: a capture from an unreliable source is scrutinized harder before it is tethered.
_Avoid_: source, source reference, citation, origin

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
