# Tether

A single-user, self-hosted AI personal assistant. Its core loop is **capture → resurface**: get a thing out of your head reliably, and have it come back at the right moment. Its distinguishing value is **interconnection** — captured things reference each other, so resurfacing is informed by everything else you've stored.

Tether is a personal operating system: memory is the substrate it is built on, not the product. Capture → resurface is the first loop built on that substrate, not the whole of it — presentation (widgets and artifacts), ingestion (gates and telemetry), and typed verticals all layer on top of canonical Evidence and automatically curated Memory.

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
The automatic behavior that assimilates settled Evidence and maintains current Memory without a per-item approval inbox. A terminal interactive Conversation turn queues assimilation whenever it appended a user Message, even if agent execution failed or was cancelled; a scheduled turn alone does not. Dreaming is incremental, bounded, evidence-linked, correctable, and inspectable.
_Avoid_: reflection, review, extraction

**Dream run**:
One bounded execution of Dreaming over immutable Evidence bounds or a bounded set of due Topics. A successful no-op is still a completed Dream run.
_Avoid_: consolidation run, reflection run

**Suppression**:
A user instruction that selected retained Evidence must not produce or recreate Memory. It is policy over Evidence, not a hidden or deleted Topic.
_Avoid_: dismissal, soft delete, ignore

**Conversation**:
A durable Tether-owned chat thread and its canonical ordered Messages. It may span many disposable pi sessions, which are execution segments rather than conversational authority, and retains its own model profile across those sessions. A pi session is rotated after uncertain or interrupted accepted execution so its context cannot outrun canonical Messages. User Messages in every Conversation are global conversational Evidence; Conversation organization never limits Dreaming, Memory, or tools.
_Avoid_: pi session, chat session

**Main Conversation**:
The one permanent, untitled Conversation used for general chat and as the default target when no other Conversation is selected. Its identity is explicit rather than inferred from title or creation order, and it cannot be renamed or deleted.
_Avoid_: default chat, first Conversation, general thread

**Scoped Conversation**:
A Conversation with an editable display name and one durable scope brief that guides its prompt, Memory relevance, and output without restricting Memory or tools. The display name is presentation only. Scope revisions are snapshotted when a Conversation turn is submitted, so running and already-pending turns retain their prior scope while later submissions use the edit. It is active or archived; archiving hides it from ordinary navigation without deleting its Messages or Evidence, and it may later be restored. Archival is blocked while the Conversation has a pending or running turn, or while an active prompt Scheduled trigger targets it. Successful archival discards and rotates its pi session, so restored execution begins fresh while retaining canonical state. Names need not be unique because Conversation identity remains its UUID.
_Avoid_: side chat, workspace, restricted Conversation

**Conversation turn**:
One durable execution accepted from an idempotent interactive request, a serialized Voice capture, a Scheduled occurrence, or a Health moment. Turns use a durable per-Conversation enqueue sequence and serialize in FIFO order within one Conversation, while different Conversations may execute concurrently. A pending turn retains its prompt and model snapshot outside the transcript; only when it reaches the FIFO head does it append its initiating user Message or Scheduled Message, followed contiguously by its reasoning, tool, and assistant Messages. Cancellation before execution therefore retains a cancelled turn record without manufacturing a transcript Message. A Conversation turn records execution origin, model profile, and lifecycle: pending, running, succeeded, failed, or cancelled. Partial reasoning and tool Messages remain part of a failed turn because they may document side effects. A submitted turn snapshots the concrete provider, model, and thinking configuration, not only a profile ID, so restart or configuration changes cannot alter it. A scheduled Conversation turn retains Provenance to its Scheduled trigger and intended firing time across safe retries. Only an explicitly transient failure known to precede pi acceptance retries automatically, at most twice; unknown, permanent, exhausted, and post-acceptance failures are terminal. Durable failure state contains a typed code, phase, stable human summary, and trace run ID, while raw diagnostics remain in traces and logs. Cancellation targets one Conversation turn, never the Conversation or pi runtime globally. Host shutdown briefly drains running turns, marks unresolved accepted work failed, and leaves pending turns for startup reconciliation without rerunning uncertain execution.
_Avoid_: pi turn, agent run, Message

**Message**:
One canonical ordered transcript entry within a Conversation turn, including its speaker or execution role. User Messages are conversational Evidence. Scheduled Messages record a Scheduled occurrence; Health Messages record a Health moment. Both are context rather than fresh user Evidence and cannot authorize Product observation capture, email-to-Evidence promotion, or another capability requiring fresh active-turn user Evidence, though their turns retain ordinary tools. Because pi RPC accepts prompts only as user-role input, Tether wraps their canonical content with host-owned context instructions before execution while preserving the Message unchanged. Assistant, reasoning, and tool Messages are also context only unless they contain separately verified Evidence.
_Avoid_: pi message, session entry, Conversation turn

**Confined program**:
One fresh, bounded TypeScript/JavaScript execution inside an `execute_tools` call. It may sequence, parallelize, filter, and aggregate generated Tether tool calls, but has no ambient filesystem, process, environment, network, packages, persistence, or durable child identity. Each nested call still crosses its existing host authorization and tracing boundary; the outer tool call remains the one transcript Message.
_Avoid_: shell, agent, workflow, pi session

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

**Liked activity**:
A bounded YouTube viewing proxy derived from when videos entered the user's Liked playlist. Its duration is the sum of those videos' full lengths with explicit missing-data coverage; it is not measured playback time or proof that every second was watched.
_Avoid_: watch time, watch history, screen time

**Transcript status**:
The acquisition state of a saved video's transcript: pending, retrying, needs review, available, or unavailable. _Needs review_ means every configured provider has failed permanently and acquisition is paused for a human decision; it is not yet a claim that no transcript should be kept. _Unavailable_ means the human chose to give up, making transcript absence an explicit settled fact. Choosing to keep trying returns the status to pending.
_Avoid_: terminal, caption state, transcript error

**Telemetry**:
Raw time-series Evidence landing through an Ingestion gate (heart rate, location, read events). It remains in typed Vertical storage and never becomes Memory as-is.
_Avoid_: metrics, events, raw data

**Health plan**:
One explicit human-authored recurring exercise intention, anchored to an IANA timezone and one or more weekly Exercise windows. It names the settled exercise kinds that can satisfy those windows. Only fresh foreground user Evidence may create, revise, pause, or resume it. It is an intention rather than Telemetry, Memory, or a Scheduled trigger.
_Avoid_: workout telemetry, habit, goal, reminder

**Exercise window**:
One weekly local-time interval within a Health plan. Each dated realization becomes a Planned exercise occurrence. Its grace period delays absence detection so ordinary Health Connect sync latency is not mistaken for a missed workout.
_Avoid_: reminder, calendar event, streak

**Planned exercise occurrence**:
One dated realization of an Exercise window. A matching settled exercise episode overlaps its interval and has one of the Health plan's exercise kinds. It becomes missed only after its grace period ends without a match. A late upstream observation may correct its displayed state but does not manufacture another briefing.
_Avoid_: workout, Scheduled occurrence, score

**Health moment**:
One durable, host-detected reason for the agent to reconsider settled Health Evidence in the user's broader context, such as a primary sleep or exercise episode finishing, or a Planned exercise occurrence becoming missed. Its identity comes from the observed episode or Planned exercise occurrence, so retries and upstream corrections do not create repeated briefings. It initiates a Health Message and may deliver the resulting assistant Message through Web Push. Detection is deterministic; the resulting briefing is agent interpretation.
_Avoid_: alert, anomaly, Scheduled trigger, Distillation

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
A time-triggered definition the human sets up: it fires once or on a recurrence (daily/weekly), and its action is either to deliver a fixed message or to run a prompt through the agent and deliver the result. A prompt trigger targets the Main Conversation or one active Scoped Conversation. A recurring prompt captures the chat effort profile selected when the trigger is saved and keeps that profile across later firings. Edits and retargeting affect future Scheduled occurrences only. Deleting a trigger cancels pending occurrences and prevents future ones, but running and succeeded occurrences finish from their immutable snapshots. The push half of the capture → resurface loop (a plain reminder is the fixed-message case).
_Avoid_: scheduled prompt task, reminder, cron job, alert

**Scheduled occurrence**:
One durable, immutable firing of a Scheduled trigger. It snapshots the trigger version, intended firing time, action, payload, target Conversation, and model profile. A prompt occurrence links exactly one scheduled Conversation turn, whose Scheduled Message is context rather than fresh user Evidence. Turn success settles agent execution and advances recurrence independently of Web Push; push retries reuse the durable assistant Message and never rerun the turn. Deleting a trigger cancels a pending occurrence and turn, while an already-running or succeeded occurrence completes from its snapshot. The human can inspect an occurrence's outcome and follow its linked Conversation turn from the Scheduled trigger surface.
_Avoid_: dispatch attempt, scheduler job, trigger run

**Bucket item**:
An intention to act on something later. It lives in an active state and then moves to a terminal state — completed or deleted — where it is retained permanently as history (so dedup can warn you when you try to re-add something you have already dealt with). It is not Memory. It is of exactly one item type, which determines its structure, and records why it was saved (its intent context). The test that distinguishes it from Memory: a Bucket item can be _finished_.
_Avoid_: backlog item, bucket-list entry

**Todo**:
One actionable thing to do — a single action, no steps ("bring the book next time I visit Ana", "dig out the grey shirt before the gala", "research the pension transfer"). Distinct from a Bucket item (which you _consume_) and a Project (multiple coordinated steps); a Todo is exactly _one action_. It is born active and reaches a terminal state — completed or abandoned. It may carry an optional _waiting condition_: a free-text condition and/or a linked Scheduled trigger (a deadline). Its _waiting_ state is always **computed, never stored** — a Todo is waiting while it has an unmet text condition or an unfired linked trigger, and ready otherwise — so it can never get wedged in a stale waiting state. Ready Todos surface in the agent's standing digest; waiting ones are raised only when the conversation makes them relevant. The one-off actionable that had no home before the vertical existed.
_Avoid_: task, reminder, bucket item, project, waiting-on flag

**Item type**:
What kind of thing a Bucket item is (movie, book, place, travel, …). Different item types carry different fields, which is why Bucket items aren't all one shape. Applies only to Bucket items; Memory Topics have none. (The word "domain" is deliberately avoided here to prevent confusion with domain-driven-design vocabulary.)
_Avoid_: domain, category, kind, tag

**Intent context**:
The human's subjective reason for saving a Bucket item — _why_ it was worth capturing ("a podcast recommended it," "relates to my interest in X"). Immutable once set; it answers "why did I save this?" months later, when the item alone no longer explains itself. Bucket items only; Memory Claims instead carry supporting Evidence.
_Avoid_: reason, rationale, note, why

**Triage**:
An agent-produced report over the _active_ Bucket items that surfaces problems — under-specified, duplicate, and stale items — for the human to act on. A pull action, optionally run on a Scheduled trigger. It produces no new stored state and is unrelated to automatic Memory maintenance.
_Avoid_: review, grooming, cleanup, backlog review

**Candidate**:
An agent-derived typed thing awaiting human acceptance, such as a Bucket item inferred during conversation import. Memory has no Candidate state: Dreaming curates it automatically.
_Avoid_: suggestion, draft

**Product observation**:
An explicit account of friction, an unmet expectation, or an improvement idea encountered while using Tether. It preserves the user's wording and conversational origin without prematurely diagnosing a bug or feature. It remains open until the user considers it addressed; it is not itself a GitHub issue or implementation plan.
_Avoid_: issue, bug report, feature request, proposal

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
Recorded audio a Capture client uploads straight to the host, outside of chat. It becomes the initiating Message of a durable capture Conversation turn and therefore canonical conversational Evidence, the same as Voice input. The capture turn waits in the target Conversation's FIFO and settles without running the agent.
_Avoid_: voice memo, audio capture, voice note

## Cooking

A deferred vertical with its own entities. These terms will migrate to `src/cooking/CONTEXT.md` (and a `CONTEXT-MAP.md` will appear) when the vertical is actually built. It connects to the core at two points: the cooking profile is a view over relevant current Memory, and recipe import uses the Candidate pattern.

**Ingredient**:
A canonical, normalized food identity (e.g. "garlic", "all-purpose flour") that both Recipe lines and Pantry items reference. The shared key that makes pantry coverage and shopping-list diffing possible; messy ingredient text is normalized onto it (agent-assisted, sometimes via a Candidate pick).
_Avoid_: food, product, item

**Recipe**:
A stored, structured, in-app-editable dish definition — metadata (title, cuisine, servings, time, tags), Recipe lines, and ordered steps. Reference data (something you _can_ cook), distinct from a "dish I want to try" (a Bucket item), which is intentionally not coupled to it. Scaling servings is a transient view, never stored.
_Avoid_: dish, meal, formula

**Recipe line**:
One ingredient entry within a Recipe — a canonical Ingredient plus a quantity and unit ("2 cloves · garlic").
_Avoid_: ingredient (reserved for the canonical entity), row

**Recipe revision**:
An entry in a Recipe's append-only edit history, created either by a direct human edit or by accepting an agent-proposed Candidate edit. Prior revisions are retained; reverting makes an older one current.
_Avoid_: version, edit, history

**Pantry item**:
A canonical Ingredient the household has on hand, tracked as presence + a coarse level (out / low / have), an expiry estimate, and a location (pantry / fridge / freezer). Deliberately _not_ a precise quantity. Kept current by cooking (decrements) and shopping (increments), not by manual audits.
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
