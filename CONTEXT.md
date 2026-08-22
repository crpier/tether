# Tether

A single-user, self-hosted AI personal assistant. Its core loop is **capture → resurface**: get a thing out of your head reliably, and have it come back at the right moment. Its distinguishing value is **interconnection** — captured things reference each other, so resurfacing is informed by everything else you've stored.

Tether is a personal operating system: memory is the substrate it is built on, not the product. Capture → resurface is the first loop built on that substrate, not the whole of it — presentation (widgets and artifacts), ingestion (gates and telemetry), proposals and earned autonomy, and typed verticals all layer on top of canonical Evidence and automatically curated Memory.

## Language

**Memory**:
Tether's small, current, user-centric understanding carried across actions. Dreaming is its sole writer and continuously revises its Topics and Claims from canonical Evidence; prior Memory is state to improve, never independent Evidence. Human corrections enter as high-authority Evidence, not direct Memory edits.
_Avoid_: knowledge base, note collection, source record

**Topic**:
A user- and model-facing grouping of related Claims within Memory. The agent chooses Topic boundaries and organization, preferring coherent summaries while allowing a useful standalone Claim to remain alone.
_Avoid_: category, folder, document

**Claim**:
A granular assertion in Memory supported by exact Evidence. Claims normally huddle within a Topic and are not independent lifecycle entities.
_Avoid_: memory row, fact record

**Evidence**:
Canonical durable source material that may support a Claim, such as a user Message or a scoped verified external record. Stable `tether://` references identify exact Evidence and resolve through Tether's inspector rather than through the browser as external links. Assistant output may explain Evidence in context but cannot independently support a Claim about the user.
_Avoid_: Memory, summary, inference

**Dreaming**:
The automatic behavior that assimilates settled Evidence and maintains current Memory without a per-item approval inbox. It is incremental, bounded, evidence-linked, correctable, and inspectable.
_Avoid_: reflection, review, extraction

**Dream run**:
One bounded execution of Dreaming over immutable Evidence bounds or a bounded set of due Topics. A successful no-op is still a completed Dream run.
_Avoid_: consolidation run, reflection run

**Suppression**:
A user instruction that selected retained Evidence must not produce or recreate Memory. It is policy over Evidence, not a hidden or deleted Topic.
_Avoid_: dismissal, soft delete, ignore

**Conversation**:
A durable Tether-owned chat thread and its canonical ordered Messages. It may span many disposable pi sessions, which are execution segments rather than conversational authority.
_Avoid_: pi session, chat session

**Message**:
One canonical ordered transcript entry within a Conversation, including its speaker or execution role. User Messages are conversational Evidence; assistant, reasoning, and tool Messages are context only unless they contain separately verified Evidence.
_Avoid_: pi message, session entry, turn

**Recall**:
A spaced learning workflow in which the human practices distilled material through scheduled prompts. It measures learning progress and does not gate whether content enters Memory.
_Avoid_: memory review, quiz gate, study review

**Recall prompt**:
A single challenge in a Recall — a multiple-choice or short-answer question (or essay) generated from the distilled learnings of a source. Answer correctness and response time feed adaptive scheduling of the next round.
_Avoid_: quiz, flashcard, question

**Study item**:
Material currently progressing through Recall, distilled from a source and practiced across scheduled rounds.
_Avoid_: Memory, flashcard deck, course, lesson

**Curriculum**:
A learning objective broken into ordered units with progress state (e.g. "learn conversational Spanish").
_Avoid_: course, syllabus, program

**Lesson**:
A generated Artifact within a Curriculum. Its quiz results (Artifact events) feed regeneration of the next Lesson. Not Recall: a Lesson quiz is a feedback instrument shaping what's taught next, not a measure of retained material.
_Avoid_: quiz, exercise, unit

**Search**:
Reading current Memory by query and relevance ranking. It is recomputed at the moment of use; topic organization is durable curated state, while prompt-specific relevance still emerges from Search.
_Avoid_: lookup, recall, fetch

**Commons**:
The part of Memory where long-tail life domains remain agent-organized Topics rather than typed code. It is the staging ground a domain occupies before it earns Promotion to a Vertical.
_Avoid_: pool, general memories, unstructured store

**Vertical**:
A hand-built, typed slice of the domain (e.g. Cooking, Health) with its own records and lifecycle. Admitted only when a domain needs typed queries over time, a dedicated lifecycle, or a dedicated panel — not merely because it has accumulated many Topics.
_Avoid_: module, feature, app

**Promotion**:
The graduation of a Commons domain into a Vertical, justified when repeated Evidence and workflows reveal stable structure or lifecycle needs. One-directional in practice — Verticals aren't demoted back to Commons.
_Avoid_: migration, upgrade, graduation

**Sensitivity**:
Topic metadata governing presentation discretion only — which Memory is hidden in Public mode or suppressed from proactive surfacing. It never limits what the agent may reason over or send to an external LLM provider; that boundary doesn't exist in Tether.
_Avoid_: privacy, visibility, access level

**Public mode**:
A session state that excludes sensitivity-marked Memory from display and proactive surfacing (e.g. presenting Tether on a shared screen). A presentation-layer switch, not a trust or reasoning boundary.
_Avoid_: private mode, incognito, safe mode

**Ingestion gate**:
A scheduled sync that brings canonical external Evidence in without a chat turn (Readwise, Gmail, Health Connect, ebooks). Dreaming may later assimilate appropriate settled Evidence into Memory.
_Avoid_: sync job, importer, connector

**Integration**:
A hand-built typed slice that brings one external service's Evidence into Tether and exposes tools over it. Each Integration presents exactly one interface to the rest of Tether; everything else inside it stays internal to the Integration.
_Avoid_: connector, provider wrapper, adapter package

**Transcript status**:
The acquisition state of a saved video's transcript: pending, retrying, needs review, available, or unavailable. *Needs review* means every configured provider has failed permanently and acquisition is paused for a human decision; it is not yet a claim that no transcript should be kept. *Unavailable* means the human chose to give up, making transcript absence an explicit settled fact. Choosing to keep trying returns the status to pending.
_Avoid_: terminal, caption state, transcript error

**Telemetry**:
Raw time-series Evidence landing through an Ingestion gate (heart rate, location, read events). It remains in typed Vertical storage and never becomes Memory as-is.
_Avoid_: metrics, events, raw data

**Distillation**:
An evidence-backed agent-derived Claim drawn from Telemetry or a Fusion (e.g. "sleep quality drops after late screen time"). Dreaming may incorporate it into a current Topic while its supporting records remain canonical Evidence.
_Avoid_: summary, raw measurement

**Fusion**:
Cross-source correlation across Evidence that produces a Distillation. The mechanism, not the output — the output is always a Distillation.
_Avoid_: join, merge

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
A saved query over the Commons, rendered through Widgets — a panel assembled from convention, with no dedicated Vertical code.
_Avoid_: dashboard, view, report

**Scheduled trigger**:
A time-triggered action the human sets up: it fires once or on a recurrence (daily/weekly), and its action is either to deliver a fixed message or to run a prompt through the agent and deliver the result. The push half of the capture → resurface loop (a plain reminder is the fixed-message case).
_Avoid_: scheduled prompt task, reminder, cron job, alert

**Bucket item**:
An intention to act on something later. It lives in an active state and then moves to a terminal state — completed or deleted — where it is retained permanently as history (so dedup can warn you when you try to re-add something you have already dealt with). It is not Memory. It is of exactly one item type, which determines its structure, and records why it was saved (its intent context). The test that distinguishes it from Memory: a Bucket item can be *finished*.
_Avoid_: backlog item, bucket-list entry

**Todo**:
One actionable thing to do — a single action, no steps ("bring the book next time I visit Ana", "dig out the grey shirt before the gala", "research the pension transfer"). Distinct from a Bucket item (which you *consume*) and a Project (multiple coordinated steps); a Todo is exactly *one action*. It is born active and reaches a terminal state — completed or abandoned. It may carry an optional *waiting condition*: a free-text condition and/or a linked Scheduled trigger (a deadline). Its *waiting* state is always **computed, never stored** — a Todo is waiting while it has an unmet text condition or an unfired linked trigger, and ready otherwise — so it can never get wedged in a stale waiting state. Ready Todos surface in the agent's standing digest; waiting ones are raised only when the conversation makes them relevant. The one-off actionable that had no home before the vertical existed.
_Avoid_: task, reminder, bucket item, project, waiting-on flag

**Item type**:
What kind of thing a Bucket item is (movie, book, place, travel, …). Different item types carry different fields, which is why Bucket items aren't all one shape. Applies only to Bucket items; Memory Topics have none. (The word "domain" is deliberately avoided here to prevent confusion with domain-driven-design vocabulary.)
_Avoid_: domain, category, kind, tag

**Intent context**:
The human's subjective reason for saving a Bucket item — *why* it was worth capturing ("a podcast recommended it," "relates to my interest in X"). Immutable once set; it answers "why did I save this?" months later, when the item alone no longer explains itself. Bucket items only; Memory Claims instead carry supporting Evidence.
_Avoid_: reason, rationale, note, why

**Triage**:
An agent-produced report over the *active* Bucket items that surfaces problems — under-specified, duplicate, and stale items — for the human to act on. A pull action, optionally run on a Scheduled trigger. It produces no new stored state and is unrelated to automatic Memory maintenance.
_Avoid_: review, grooming, cleanup, backlog review

**Candidate**:
An agent-proposed typed thing awaiting human acceptance, such as a Bucket item inferred during conversation import. Memory has no Candidate state: Dreaming curates it automatically. Kin to Proposal: a Candidate awaits acceptance of a thing, while a Proposal awaits approval of a doing.
_Avoid_: suggestion, proposal, draft

**Proposal**:
A concrete, inspectable set of actions the agent wants to take, awaiting human approval before it executes. The doing-side counterpart to Candidate.
_Avoid_: suggestion, plan, action item

**Autonomy grant**:
An earned, per-action-category removal of the Proposal gate for a specific kind of action — visible to the human and revocable at any time.
_Avoid_: permission, trust level, auto-approve

**Provenance**:
The objective origin of Evidence or a captured typed thing — where it came from, such as a URL, Conversation, specific video, manual entry, or synced external system. Memory Claims cite Evidence rather than treating generated summaries as fresh Provenance.
_Avoid_: authority, confidence

**Capture client**:
A deliberately dumb client (phone app, watch tile) whose only job is getting a capture off the human quickly — share-target, voice-to-text, a tap. All intelligence (parsing, routing, scheduling) stays server-side.
_Avoid_: mobile app, frontend, client app

**Voice input**:
Speech recorded in the web chat client and transcribed to text, entering the Conversation as a Message — either filled into the composer for the human to review before sending, or sent immediately. It becomes canonical Evidence like any other user Message, not Memory directly.
_Avoid_: voice memo, dictation, voice command

**Voice capture**:
Recorded audio a Capture client uploads straight to the host, outside of chat. It becomes a Message and therefore canonical conversational Evidence, the same as Voice input.
_Avoid_: voice memo, audio capture, voice note

## Cooking

A deferred vertical with its own entities. These terms will migrate to `src/cooking/CONTEXT.md` (and a `CONTEXT-MAP.md` will appear) when the vertical is actually built. It connects to the core at two points: the cooking profile is a view over relevant current Memory, and recipe import uses the Candidate pattern.

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
